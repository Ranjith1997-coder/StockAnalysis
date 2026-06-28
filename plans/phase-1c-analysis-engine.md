# Phase 1C: Analysis-Engine Service — Implementation Plan

> **Status**: Draft — pending review
> **Date**: June 2026
> **Prerequisite**: Phase 1A (data-gateway) complete, Phase 1B (notification-service) complete
> **Depends on**: `services/common/redis_proxy.py`, `services/common/stock_loader.py`, `services/common/cycle_subscriber.py`

---

## 1. Problem Statement

The monolith (`intraday/intraday_monitor.py`) uses a 20-worker `ThreadPoolExecutor` to analyze ~215 symbols (209 stocks + 6 indices) per 5-min cycle. This causes:

1. **12 PM stall** — Thread pool saturates, process freezes, ticks dropped
2. **No horizontal scaling** — Analysis is pinned to one process on one machine
3. **GIL contention** — 20 threads fighting for one GIL during CPU-bound analyser work
4. **Single point of failure** — One analyser crash kills the whole cycle

Phase 1C extracts the analysis work into a separate `analysis-engine` service that consumes jobs from a Redis Stream. Multiple worker processes can join the same consumer group — scaling is just starting more processes.

---

## 2. Goals & Non-Goals

### Goals
- Replace `ThreadPoolExecutor` with Redis Stream job distribution
- Create `services/analysis_engine/` — a standalone service that runs analysers
- Zero changes to analyser code (`analyser/*.py` — same classes, same registration order)
- Feature flag for gradual rollout (`USE_ANALYSIS_ENGINE=1`)
- Monolith remains the orchestrator (scheduling, mode selection, reporting, intelligence, WS)
- Horizontal scaling: start N workers, they share the same consumer group

### Non-Goals (deferred to later phases)
- Extract coordinator (Phase 1D) — monolith keeps `_run_daily_loop()`
- Extract intelligence service — SignalBus/Correlator/Narrator stay in monolith
- Move Zerodha WebSocket to a tick-service (Phase 1E)
- Move `FuturesFetcher` out of monolith (stays until auth-service, Phase 1E)
- Redis-backed SignalBus — signals re-emitted by monolith from results (interim solution)
- Auto-scaling workers based on stream lag

---

## 3. Architecture Overview

### Current Flow (ThreadPoolExecutor)

```
Monolith (single process)
├── intraday_analysis() loop
│   ├── _wait_for_cycle_ready()          ← CycleSubscriber (Redis Pub/Sub)
│   ├── fetch_and_analyze_stocks()
│   │   ├── load_price_data_from_redis() ← Read data:price:* from Redis
│   │   ├── ThreadPoolExecutor.submit(process_stock, stock) × 215
│   │   │   └── monitor(stock)
│   │   │       ├── load_sensibull_from_redis()
│   │   │       ├── load_zerodha_from_redis()
│   │   │       ├── orchestrator.run_all_intraday(stock)
│   │   │       ├── orchestrator._emit_signals(stock)  → in-memory SignalBus
│   │   │       └── TELEGRAM_NOTIFICATIONS.send_notification()  → notification:jobs stream
│   │   └── as_completed(timeout=90s)
│   ├── process_monitor_results()
│   ├── report_*() functions             ← Read in-memory Stock objects
│   └── sleep(310s)
```

### Phase 1C Flow (Stream-based)

```
Monolith (orchestrator + reporting + intelligence + WS)
├── intraday_analysis() loop
│   ├── _wait_for_cycle_ready()
│   ├── fetch_and_analyze_stocks()
│   │   ├── load_price_data_from_redis()    ← Still loads prices into in-memory Stocks
│   │   ├── _dispatch_analysis_jobs()       ← XADD to orchestrator:analysis_jobs × 215
│   │   └── _collect_analysis_results()     ← XREADGROUP from analysis:results (90s timeout)
│   ├── _process_stream_results(results)    ← Update Stocks, re-emit signals, 52W lists
│   ├── process_monitor_results()
│   ├── report_*() functions
│   └── sleep(310s)
│
Analysis-Engine Service (N worker processes)
├── main loop
│   ├── XREADGROUP orchestrator:analysis_jobs (consumer group: analysis-workers)
│   ├── Reconstruct Stock from Redis hashes (price + sensibull + zerodha)
│   ├── Set mode (from job message)
│   ├── Run orchestrator.run_all_intraday/positional(stock)
│   │   └── _emit_signals() → no-op (signal_bus is None in worker)
│   ├── Generate analysis message if trend found
│   ├── XADD analysis:results {result, trend_found, message, analysis_json, ...}
│   ├── XACK job
│   └── gc.collect()
```

---

## 4. New Components

### 4.1 Directory Structure

```
services/
├── analysis_engine/
│   ├── __init__.py
│   ├── main.py              # Entry point: sync consumer loop
│   └── worker.py            # Job processor: reconstruct Stock, run analysers, publish result
```

### 4.2 `services/analysis_engine/main.py` — Worker Entry Point

Sync loop (matches data-gateway and notification-service patterns — no asyncio).

```python
"""
Analysis-Engine Service

Consumes analysis jobs from `orchestrator:analysis_jobs` Redis Stream,
runs all analysers on each stock, publishes results to `analysis:results`.

Horizontal scaling: start N processes, all join the same `analysis-workers`
consumer group. Redis distributes jobs round-robin.
"""
```

**Structure** (mirrors `services/notification-service/main.py`):

1. **Config**: Redis URL from env, worker name from `--worker-name` arg (default `worker-1`), stream/group constants
2. **Signal handling**: SIGTERM/SIGINT → graceful shutdown
3. **Redis init**: `RedisProxy(redis_url)`, ping test, create consumer group `analysis-workers` on `orchestrator:analysis_jobs` (idempotent)
4. **Heartbeat**: `HSET service:registry:analysis-engine:{worker_name}` with status, pid, stats (every 10s via background thread or inline between jobs)
5. **Orchestrator init**: Build `AnalyserOrchestrator` with same 11 analysers in same registration order as monolith
6. **Main loop**:
   ```python
   while _running:
       messages = redis.xreadgroup(
           group="analysis-workers",
           consumer=worker_name,
           streams={"orchestrator:analysis_jobs": ">"},
           count=10,       # batch up to 10 jobs
           block=5000,     # 5s block
       )
       for msg_id, fields in messages:
           try:
               result = worker.process_job(redis, orchestrator, fields)
               redis.xadd("analysis:results", result, maxlen=500)
           except Exception as e:
               # Publish error result so monolith doesn't wait forever
               redis.xadd("analysis:results", {
                   "job_id": fields.get("job_id", ""),
                   "cycle_id": fields.get("cycle_id", ""),
                   "symbol": fields.get("symbol", ""),
                   "result": "ERROR",
                   "trend_found": "false",
                   "message": "",
                   "error": str(e),
                   "duration_ms": "0",
                   "timestamp": str(time.time()),
               }, maxlen=500)
           finally:
               redis.xack("orchestrator:analysis_jobs", "analysis-workers", msg_id)
       
       # Periodic heartbeat + gc
       _update_heartbeat(redis, worker_name)
       gc.collect()
   ```
7. **Shutdown**: set status to `shutdown` in registry, close Redis

### 4.3 `services/analysis_engine/worker.py` — Job Processor

```python
"""
Single-job processor: reconstruct Stock from Redis, run all analysers,
return result dict for XADD to analysis:results stream.
"""
```

**`process_job(redis, orchestrator, job_fields) -> dict`**:

1. **Parse job**: `symbol`, `is_index`, `mode` ("intraday" / "positional"), `cycle_id`, `job_id`
2. **Set mode**: `shared.app_ctx.mode = Mode.INTRADAY` or `Mode.POSITIONAL` (process-local)
3. **Reconstruct Stock**:
   ```python
   stock = load_stock_from_redis(redis, symbol, is_index=is_index)
   if stock is None:
       return _result(job_id, cycle_id, symbol, "NO_DATA", False, None, ...)
   ```
4. **Pre-analysis gates** (same as `monitor()` in intraday_monitor.py:226-254):
   - Check `priceData` length (≥3 intraday, ≥2 positional)
   - `stock.reset_analysis()` + `stock.update_latest_data()`
   - Positional min-move gate (<0.75% → skip)
   - `INDEX_ANALYSIS_EXCLUDE` check
5. **Load context data from Redis**:
   ```python
   sensibull_ok = load_sensibull_from_redis(redis, stock)
   if not sensibull_ok:
       return _result(job_id, cycle_id, symbol, "NO_DATA", False, None, ...)
   load_zerodha_from_redis(redis, stock)  # optional — analysers handle missing data
   ```
6. **Run analysers** (same as monitor.py:275-286):
   ```python
   orchestrator.reset_all_constants()
   if stock.is_index:
       trend_found, score_result = (
           orchestrator.run_all_positional(stock, index=True)
           if mode == "positional"
           else orchestrator.run_all_intraday(stock, index=True)
       )
   else:
       trend_found, score_result = (
           orchestrator.run_all_positional(stock)
           if mode == "positional"
           else orchestrator.run_all_intraday(stock)
       )
   ```
   Note: `orchestrator._emit_signals()` will no-op because `shared.app_ctx.signal_bus` is `None` in the worker process (line 106-107 of Analyser.py already guards this).

7. **Generate message** if trend found:
   ```python
   message = ""
   if trend_found:
       message = orchestrator.generate_analysis_message(stock)
   ```

8. **Extract 52-week status** from analysis dict:
   ```python
   is_52w_high = "52-week-high" in stock.analysis.get("BULLISH", {})
   is_52w_low = "52-week-low" in stock.analysis.get("BEARISH", {})
   ```

9. **Build result dict**:
   ```python
   return {
       "job_id": job_id,
       "cycle_id": cycle_id,
       "symbol": symbol,
       "is_index": str(is_index).lower(),
       "result": "SUCCESS",
       "trend_found": str(trend_found).lower(),
       "message": message,
       "analysis_json": json.dumps(stock.analysis, default=str),
       "score_result_json": json.dumps(
           score_result.__dict__ if score_result else {}, default=str
       ),
       "is_52w_high": str(is_52w_high).lower(),
       "is_52w_low": str(is_52w_low).lower(),
       "duration_ms": str(int((time.time() - start) * 1000)),
       "timestamp": str(time.time()),
   }
   ```

---

## 4.5 Reporting Functions — No Changes Needed

The 5 intraday/positional report functions all read from **in-memory Stock objects** in the monolith, and **none depend on analysis results** (except 52-week):

| Report function | Reads from | Data fields used | Depends on analysis? |
|-----------------|-----------|-----------------|---------------------|
| `report_top_gainers_and_losers()` (695) | `shared.app_ctx.stock_token_obj_dict` | `priceData['Close']`, `prevDayOHLCV` | No — pure price data |
| `report_index_data()` (702) | `shared.app_ctx.index_token_obj_dict` | `ltp`, `ltp_change_perc` | No — pure price data |
| `report_commodity_data()` (722) | `shared.app_ctx.commodity_token_obj_dict` | `priceData['Close']`, `prevDayOHLCV` | No — pure price data |
| `report_global_indices_data()` (773) | `shared.app_ctx.global_indices_token_obj_dict` | `priceData['Close']`, `prevDayOHLCV` | No — pure price data |
| `report_52_week_high_low()` (848) | `shared.ticker_52_week_high_list/low_list` | Stock objects that hit 52w | **Yes** — populated by `Stock.set_analysis()` |

**Why reports work unchanged in the stream path:**

`fetch_and_analyze_stocks()` calls `load_price_data_from_redis()` (intraday_monitor.py:596) **before** dispatching any jobs — this populates `priceData`, `ltp`, `ltp_change_perc`, `prevDayOHLCV` on the in-memory Stock objects in the monolith. The reports run **after** `fetch_and_analyze_stocks()` returns (lines 1440-1443), reading this already-loaded price data.

In the stream path, this sequence is identical:
1. `load_price_data_from_redis()` → populates in-memory Stocks with price data from Redis
2. `_dispatch_and_collect_stream()` → sends analysis jobs to workers, collects results
3. `report_top_gainers_and_losers()` / `report_index_data()` / etc. → read price data from in-memory Stocks (unchanged)

The analysis-engine never owns the price data — it only borrows it from Redis to run analysers. The monolith's in-memory Stock objects remain the source of truth for reporting.

**52-week report**: The `is_52w_high`/`is_52w_low` fields in the stream result handle this. The monolith's `_convert_stream_result()` adds the in-memory Stock object to `shared.ticker_52_week_high_list/low_list` based on these flags. The 52-week report then reads these lists as before.

---

## 5. Redis Stream Contracts

### 5.1 `orchestrator:analysis_jobs` — Job Stream

**Producer**: Monolith (`_dispatch_analysis_jobs()`)
**Consumer group**: `analysis-workers`
**Maxlen**: 500 (keeps ~5 cycles of history)

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `job_id` | str | `"a1b2c3d4"` | UUID for matching result to job |
| `cycle_id` | str | `"2026-06-28-1"` | `{date}-{cycle_number}` for grouping |
| `symbol` | str | `"NIFTY"` | Stock/index symbol |
| `is_index` | str | `"true"` / `"false"` | Whether symbol is an index |
| `mode` | str | `"intraday"` / `"positional"` | Analysis mode |

### 5.2 `analysis:results` — Result Stream

**Producer**: Analysis-engine workers
**Consumer group**: `monolith` (created by monolith on startup)
**Maxlen**: 500

| Field | Type | Example | Description |
|-------|------|---------|-------------|
| `job_id` | str | `"a1b2c3d4"` | Matches job for correlation |
| `cycle_id` | str | `"2026-06-28-1"` | Groups results by cycle |
| `symbol` | str | `"NIFTY"` | Stock/index symbol |
| `is_index` | str | `"true"` | Whether symbol is an index |
| `result` | str | `"SUCCESS"` / `"NO_DATA"` / `"ERROR"` | Outcome |
| `trend_found` | str | `"true"` / `"false"` | Whether a trend was detected |
| `message` | str | `"<html>..."` | HTML analysis message (empty if no trend) |
| `analysis_json` | str | `"{...}"` | Serialized `stock.analysis` dict |
| `score_result_json` | str | `"{...}"` | Serialized score result |
| `is_52w_high` | str | `"true"` / `"false"` | 52-week high detected |
| `is_52w_low` | str | `"true"` / `"false"` | 52-week low detected |
| `error` | str | `""` / `"timeout"` | Error description (empty if none) |
| `duration_ms` | str | `"1250"` | Job processing time |
| `timestamp` | str | `"1234567890.123"` | Unix timestamp |

### 5.3 Consumer Group Setup

```python
# Analysis-engine (on startup, idempotent):
redis.xgroup_create("orchestrator:analysis_jobs", "analysis-workers", id="0", mkstream=True)

# Monolith (on startup, idempotent):
redis.xgroup_create("analysis:results", "monolith", id="0", mkstream=True)
```

### 5.4 Redis Hash — `orchestrator:state`

The monolith already sets `shared.app_ctx.mode`. For observability, it also writes:

```python
redis.hset("orchestrator:state", mapping={
    "mode": "intraday",  # or "positional"
    "cycle_count": str(cycle),
    "last_cycle_id": cycle_id,
    "last_cycle_time": str(time.time()),
})
```

Workers don't read this (mode comes per-job), but it's useful for debug commands and future bot-service.

---

## 6. Monolith Changes

### 6.1 Feature Flag

New env var: `USE_ANALYSIS_ENGINE=1` (default: not set = ThreadPoolExecutor mode).

```python
# common/constants.py
ENV_USE_ANALYSIS_ENGINE = "USE_ANALYSIS_ENGINE"
USE_ANALYSIS_ENGINE = os.environ.get(ENV_USE_ANALYSIS_ENGINE, "").lower() in ("1", "true", "yes")
```

### 6.2 New Stream Constants

```python
# common/constants.py
ANALYSIS_JOBS_STREAM = "orchestrator:analysis_jobs"
ANALYSIS_RESULTS_STREAM = "analysis:results"
ANALYSIS_WORKERS_GROUP = "analysis-workers"
ANALYSIS_RESULTS_GROUP = "monolith"
```

### 6.3 `fetch_and_analyze_stocks()` — Refactored

**File**: `intraday/intraday_monitor.py:581`

When `USE_ANALYSIS_ENGINE` is true, the function takes the stream path instead of the ThreadPoolExecutor path:

```python
def fetch_and_analyze_stocks() -> List[Tuple[MonitorResult, bool, Optional[str]]]:
    logger.info("Fetching and analyzing data for all stocks")

    stock_objs = list(shared.app_ctx.stock_token_obj_dict.values())
    index_objs = list(shared.app_ctx.index_token_obj_dict.values())
    commodity_objs = list(shared.app_ctx.commodity_token_obj_dict.values())
    global_indices_objs = list(shared.app_ctx.global_indices_token_obj_dict.values())

    orchestrator.reset_all_constants()

    # Load price data into in-memory Stock objects (needed for reporting regardless of path)
    load_price_data_from_redis(
        redis_proxy, stock_objs, index_objs,
        commodity_objs, global_indices_objs,
    )

    if USE_ANALYSIS_ENGINE:
        return _dispatch_and_collect_stream(stock_objs, index_objs)
    else:
        return _dispatch_and_collect_threadpool(stock_objs, index_objs)
```

The existing ThreadPoolExecutor code (`_collect`, `thread_pool.submit`) moves into `_dispatch_and_collect_threadpool()` unchanged — it's the fallback path.

### 6.4 `_dispatch_and_collect_stream()` — New Function

```python
def _dispatch_and_collect_stream(
    stock_objs: list, index_objs: list
) -> List[Tuple[MonitorResult, bool, Optional[str]]]:
    """Dispatch analysis jobs to Redis Stream, collect results."""
    import uuid

    cycle_id = f"{datetime.now().strftime('%Y-%m-%d')}-{shared.app_ctx.intraday_cycle_count}"
    mode_str = "positional" if shared.app_ctx.mode == shared.Mode.POSITIONAL else "intraday"
    
    # Write mode to Redis for observability
    redis_proxy.hset("orchestrator:state", mapping={
        "mode": mode_str,
        "cycle_id": cycle_id,
        "last_cycle_time": str(time.time()),
    })

    # Build job list: indices first, then stocks (same order as ThreadPoolExecutor path)
    jobs = []
    for obj in index_objs + stock_objs:
        job_id = uuid.uuid4().hex[:8]
        jobs.append((job_id, obj))

    # Dispatch all jobs to stream
    for job_id, obj in jobs:
        redis_proxy.xadd(ANALYSIS_JOBS_STREAM, {
            "job_id": job_id,
            "cycle_id": cycle_id,
            "symbol": obj.stock_symbol,
            "is_index": str(obj.is_index).lower(),
            "mode": mode_str,
        }, maxlen=500)

    logger.info(f"[stream] Dispatched {len(jobs)} analysis jobs (cycle={cycle_id})")

    # Collect results with 90s timeout
    expected = len(jobs)
    job_ids = {jid for jid, _ in jobs}
    results_by_job = {}
    deadline = time.time() + 90

    while len(results_by_job) < expected and time.time() < deadline:
        remaining_ms = int((deadline - time.time()) * 1000)
        block_ms = min(remaining_ms, 5000)
        if block_ms <= 0:
            break

        messages = redis_proxy.xreadgroup(
            ANALYSIS_RESULTS_GROUP,
            "prod-1",
            {ANALYSIS_RESULTS_STREAM: ">"},
            count=expected - len(results_by_job),
            block=block_ms,
        )

        for msg_id, fields in messages:
            jid = fields.get("job_id", "")
            if jid in job_ids:
                results_by_job[jid] = fields
            redis_proxy.xack(ANALYSIS_RESULTS_STREAM, ANALYSIS_RESULTS_GROUP, msg_id)

    # Build result list in same order as jobs (indices first, then stocks)
    results = []
    for job_id, obj in jobs:
        fields = results_by_job.get(job_id)
        if fields is None:
            logger.warning(f"[stream] No result for {obj.stock_symbol} (job={job_id}) — timeout")
            results.append((MonitorResult.ERROR, False, "stream_timeout"))
        else:
            results.append(_convert_stream_result(fields, obj))

    # Log any missing
    missing = expected - len(results_by_job)
    if missing > 0:
        logger.warning(f"[stream] {missing}/{expected} jobs timed out")

    logger.info(f"[stream] Collected {len(results_by_job)}/{expected} results")
    return results
```

### 6.5 `_convert_stream_result()` — Result Processing

Converts a stream result dict into the `Tuple[MonitorResult, bool, Optional[str]]` format AND performs side effects:

```python
def _convert_stream_result(fields: dict, stock_obj) -> Tuple[MonitorResult, bool, Optional[str]]:
    """Convert stream result to tuple format + update in-memory Stock + emit signals."""
    result_str = fields.get("result", "ERROR")
    trend_found = fields.get("trend_found", "false").lower() == "true"
    message = fields.get("message", "")
    error = fields.get("error", "")

    # Map result string to enum
    monitor_result = {
        "SUCCESS": MonitorResult.SUCCESS,
        "NO_DATA": MonitorResult.NO_DATA,
        "ERROR": MonitorResult.ERROR,
    }.get(result_str, MonitorResult.ERROR)

    # ── Update in-memory Stock object ──
    # Restore analysis dict so reporting functions and intelligence can use it
    analysis_json = fields.get("analysis_json", "{}")
    try:
        stock_obj.analysis = json.loads(analysis_json)
    except Exception:
        stock_obj.analysis = {"BULLISH": {}, "BEARISH": {}, "NEUTRAL": {}, "NoOfTrends": 0}

    # ── 52-week lists ──
    is_52w_high = fields.get("is_52w_high", "false").lower() == "true"
    is_52w_low = fields.get("is_52w_low", "false").lower() == "true"
    if is_52w_high:
        shared.ticker_52_week_high_list.append(stock_obj)
    if is_52w_low:
        shared.ticker_52_week_low_list.append(stock_obj)

    # ── Re-emit signals to in-memory SignalBus ──
    # The worker's _emit_signals() no-oped (signal_bus=None). We re-emit here
    # so the monolith's Correlator/Narrator see the signals.
    if shared.app_ctx.signal_bus and trend_found:
        _re_emit_signals_from_analysis(
            stock_obj,
            Layer.POSITIONAL if shared.app_ctx.mode == shared.Mode.POSITIONAL else Layer.INTRADAY,
        )

    # ── Send notification if trend found ──
    # (Same as monitor() line 295 — sends via notification:jobs stream)
    if trend_found and message:
        TELEGRAM_NOTIFICATIONS.send_notification(message, parse_mode="HTML")

    return (monitor_result, trend_found, message if trend_found else None)
```

### 6.6 `_re_emit_signals_from_analysis()` — Signal Re-emission

Extracts signals from the analysis dict and emits them to the local SignalBus. This replicates what `AnalyserOrchestrator._emit_signals()` does, but is called by the monolith after receiving results:

```python
def _re_emit_signals_from_analysis(stock, layer: Layer):
    """Re-emit signals from analysis dict to in-memory SignalBus."""
    bus = shared.app_ctx.signal_bus
    if not bus:
        return
    for sentiment in ("BULLISH", "BEARISH"):
        direction = Direction[sentiment]
        for analysis_type in stock.analysis.get(sentiment, {}):
            weight = constant.ANALYSIS_WEIGHTS.get(
                analysis_type, constant.ANALYSIS_WEIGHTS.get("DEFAULT", 10)
            )
            bus.emit(Signal(
                symbol=stock.stock_symbol,
                direction=direction,
                source=analysis_type.lower(),
                layer=layer,
                strength=weight_to_strength(weight),
            ))
```

**Note**: This duplicates the logic in `AnalyserOrchestrator._emit_signals()` (Analyser.py:101-120). In a future refactor, `_emit_signals` could be extracted to a shared utility. For Phase 1C, duplication is acceptable to avoid touching the orchestrator code.

### 6.7 Score Gating in Re-emission

Currently `_emit_signals()` in the orchestrator is only called when `score_result.total_score >= MIN_NOTIFICATION_SCORE` (or PRIORITY_OVERRIDE set). The re-emission should respect the same gate:

```python
def _re_emit_signals_from_analysis(stock, layer: Layer):
    bus = shared.app_ctx.signal_bus
    if not bus:
        return
    
    # Check score gate (same as Analyser.py:150-155)
    score_result_json = ...  # from fields
    score_result = ...  # deserialize
    score = score_result.get("total_score", 0) if score_result else 0
    has_priority_override = stock.analysis.get("PRIORITY_OVERRIDE") is not None
    
    if score < constant.MIN_NOTIFICATION_SCORE and not has_priority_override:
        return  # Don't emit signals below threshold
    
    # ... emit signals as above ...
```

Actually, to keep it simpler: the worker already ran `_emit_signals()` (which no-oped). The worker's `AnalyserOrchestrator.run_all_intraday()` checked the score gate before calling `_emit_signals()`. So if the result has `trend_found=True`, the score gate was already passed. We can re-emit unconditionally when `trend_found=True`.

But `trend_found` is based on `should_notify()` which uses the same score threshold. So re-emitting when `trend_found=True` is equivalent to the original gated emission. **This is correct** — no separate score gate needed in re-emission.

### 6.8 Monolith Startup — Consumer Group Creation

Add to `init()` (after Redis proxy initialization):

```python
if USE_ANALYSIS_ENGINE:
    try:
        redis_proxy.xgroup_create(ANALYSIS_RESULTS_STREAM, ANALYSIS_RESULTS_GROUP, id="0", mkstream=True)
        logger.info("[stream] Created analysis:results consumer group 'monolith'")
    except Exception:
        pass  # Group already exists
```

### 6.9 Existing `monitor()` — Unchanged

The `monitor()` function (intraday_monitor.py:213) and `process_stock()` (line 569) stay **unchanged**. They're the fallback path when `USE_ANALYSIS_ENGINE` is not set. This ensures we can switch back to ThreadPoolExecutor if needed.

---

## 7. Coupling Resolution

### 7.1 Mode Propagation

| Issue | Solution |
|-------|----------|
| Analysers read `shared.app_ctx.mode` | Worker sets `shared.app_ctx.mode` per-job from the `mode` field in the job message |
| `reset_all_constants()` reads mode | Called per-job in worker (before each job) |

The worker has its own `shared.app_ctx` singleton (process-local). It sets `mode` before processing each job. This is safe because the worker is single-threaded (one job at a time).

### 7.2 52-Week Lists

| Issue | Solution |
|-------|----------|
| `Stock.set_analysis()` appends to `shared.ticker_52_week_high_list` (process-global) | Worker detects 52-week status from analysis dict, includes `is_52w_high`/`is_52w_low` in result. Monolith adds in-memory Stock object to global lists. |

The worker's `shared.ticker_52_week_high_list` is process-local and unused. The monolith reconstructs the lists from result fields. The 52-week lists are cleared at the start of each positional cycle (intraday_monitor.py:942-943) — this stays in the monolith.

### 7.3 Cross-Cycle Analyser State

| Analyser | State | Impact | Solution for Phase 1C |
|----------|-------|--------|----------------------|
| `GEXAnalyser._prev_gex_by_strike` | Previous-cycle GEX per strike | Delta detection between cycles | **Accept reset on restart.** Worker creates a fresh `GEXAnalyser()` at startup. First cycle after restart has no previous GEX baseline (same as monolith restart). Cross-cycle state accumulates within a worker's lifetime — this is acceptable. |
| `FuturesAnalyser._dynamic_thresholds_cache` | Dynamic threshold cache | Adaptive thresholds | **Accept reset on restart.** Cache rebuilds naturally as the worker processes jobs. |

**Rationale**: These caches are optimization features, not correctness requirements. The monolith already loses them on restart. The worker losing them on restart is equivalent. If a worker crashes mid-cycle, its pending jobs are XCLAIMed by another worker which has its own cache state — this is fine because the cache is per-symbol, not per-cycle.

### 7.4 Signal Emission

| Issue | Solution |
|-------|----------|
| `AnalyserOrchestrator._emit_signals()` writes to `shared.app_ctx.signal_bus` (in-memory) | Worker's `signal_bus` is `None` → `_emit_signals()` no-ops (already guarded at Analyser.py:106-107). Monolith re-emits signals from `analysis_json` in the result via `_re_emit_signals_from_analysis()`. |

**Flow**: Worker runs analysers → `_emit_signals()` no-ops → worker publishes `analysis_json` → monolith reads result → monolith calls `_re_emit_signals_from_analysis()` → signals flow to in-memory `SignalBus` → `SignalCorrelator` processes them → confluence detection → `Narrator` if HIGH.

This preserves the entire intelligence pipeline with zero changes to `intelligence/*.py`.

### 7.5 `monitor_result_counts` and Debug Counters

| Counter | Current | Phase 1C |
|---------|---------|----------|
| `monitor_result_counts` | Incremented in `process_monitor_results()` from ThreadPoolExecutor results | **Unchanged** — same function processes stream results (same tuple format) |
| `error_count` | Incremented in `process_monitor_results()` for ERROR results | **Unchanged** |
| `intraday_cycle_count` | Set in `intraday_analysis()` loop | **Unchanged** |
| `last_cycle_time` | Set in `intraday_analysis()` loop | **Unchanged** |

The debug counters work unchanged because `process_monitor_results()` receives the same `List[Tuple[MonitorResult, bool, Optional[str]]]` format from both paths.

### 7.6 FuturesFetcher

`FuturesFetcher` stays in the monolith (Phase 1E dependency). In the stream path:
- Worker does NOT run `FuturesFetcher` — if zerodha data is missing from Redis, the worker runs analysers without it (options/IV analysers will produce no signals, which is correct behavior)
- The monolith does NOT run `FuturesFetcher` as a fallback in the stream path (it was a per-stock inline call in `monitor()`)
- If `ENABLE_ZERODHA_DERIVATIVES` is true, the data-gateway should be the one fetching futures data. If it's missing, it's a data-gateway issue, not a monolith issue.

**Acceptable tradeoff**: In the current ThreadPoolExecutor path, `FuturesFetcher` is a fallback when zerodha data is missing from Redis. In the stream path, this fallback is removed. If zerodha data is consistently missing, it should be fixed in the data-gateway, not patched in the analysis path.

---

## 8. Deployment

### 8.1 systemd Unit File

New file: `scripts/system_config/stockanalysis-analysis-engine.service`

```ini
[Unit]
Description=StockAnalysis Analysis Engine
After=redis-server.service stockanalysis-data-gateway.service
Wants=network-online.target
Requires=redis-server.service

[Service]
Type=simple
User=hacker
WorkingDirectory=/home/hacker/StockAnalysis
EnvironmentFile=/home/hacker/StockAnalysis/.env
Environment=PYTHONPATH=/home/hacker/StockAnalysis
Environment=REDIS_URL=redis://localhost:6379
ExecStart=/home/hacker/StockAnalysis/.venv/bin/python services/analysis_engine/main.py --worker-name prod-1
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
CPUQuota=40%
MemoryMax=800M

[Install]
WantedBy=multi-user.target
```

### 8.2 Monolith Unit Update

Update `scripts/system_config/stockanalysis.service`:
- Add `stockanalysis-analysis-engine.service` to `After=` and `Requires=`

```ini
[Unit]
Description=Stock Analysis (always running — self-scheduling)
After=redis-server.service stockanalysis-notification.service stockanalysis-data-gateway.service stockanalysis-analysis-engine.service
Wants=network-online.target
Requires=redis-server.service stockanalysis-notification.service stockanalysis-data-gateway.service stockanalysis-analysis-engine.service
```

### 8.3 Environment Variables

Add to `.env`:
```bash
# Phase 1C: Analysis engine stream mode
USE_ANALYSIS_ENGINE=1
```

### 8.4 Makefile Targets

```makefile
# Analysis engine
analysis-engine: ## Start analysis engine locally (dev)
	.venv/bin/python services/analysis_engine/main.py --worker-name dev-1

analysis-engine-prod: ## Install + enable analysis engine systemd service on server
	ssh hacker@100.92.21.31 "sudo cp $(PWD)/scripts/system_config/stockanalysis-analysis-engine.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable stockanalysis-analysis-engine && sudo systemctl restart stockanalysis-analysis-engine"
```

---

## 9. Testing Strategy

### 9.1 Unit Tests

**`tests/services/test_analysis_worker.py`**:
- `test_process_job_success` — Mock Redis with price/sensibull data, verify result dict structure
- `test_process_job_no_data` — Missing price data → `result="NO_DATA"`
- `test_process_job_no_sensibull` — Missing sensibull → `result="NO_DATA"`
- `test_process_job_positional_skip` — Small price move in positional mode → `result="SUCCESS", trend_found=false`
- `test_process_job_index_excluded` — Symbol in `INDEX_ANALYSIS_EXCLUDE` → `result="SUCCESS", trend_found=false`
- `test_52week_detection` — Verify `is_52w_high`/`is_52w_low` fields in result
- `test_signal_bus_noop` — Verify `signal_bus=None` doesn't crash
- `test_mode_propagation` — Verify `shared.app_ctx.mode` set correctly from job fields

**`tests/intraday/test_stream_dispatch.py`**:
- `test_dispatch_and_collect` — Mock Redis stream, verify job dispatch + result collection
- `test_timeout_handling` — Jobs not completed within 90s → ERROR results
- `test_result_conversion` — Stream result dict → tuple format + side effects
- `test_52w_list_update` — Result with `is_52w_high=true` → Stock added to global list
- `test_signal_re_emission` — Result with `trend_found=true` → signals emitted to SignalBus
- `test_notification_dispatch` — Result with `trend_found=true` → `TELEGRAM_NOTIFICATIONS.send_notification()` called
- `test_feature_flag_off` — `USE_ANALYSIS_ENGINE=False` → uses ThreadPoolExecutor path

### 9.2 Integration Test

**Manual verification** (can't automate without live Redis + market data):
1. Start Redis, data-gateway, notification-service
2. Start analysis-engine with `--worker-name test-1`
3. Set `USE_ANALYSIS_ENGINE=1` in monolith's env
4. Start monolith in dev mode (`DEV_INTRADAY=1`)
5. Verify: jobs dispatched to `orchestrator:analysis_jobs`, results appear in `analysis:results`
6. Verify: notifications sent, signals emitted, 52-week lists populated
7. Verify: debug commands (`/debug`, `/debugcycle`) show correct counters

### 9.3 Parallel Run Test

Before switching to stream mode in production:
1. Run monolith with `USE_ANALYSIS_ENGINE=0` (ThreadPoolExecutor) — capture analysis output
2. Run analysis-engine service in parallel (consuming jobs from a test stream)
3. Compare results: same trends found, same signals emitted, same messages generated
4. Switch to `USE_ANALYSIS_ENGINE=1` only after verification

---

## 10. Rollout Plan

### Step 1: Implement & Test (local)
- Create `services/analysis_engine/main.py` + `worker.py`
- Add feature flag, stream constants, monolith changes
- Write unit tests
- Run in dev mode with `DEV_INTRADAY=1`

### Step 2: Deploy to Server (parallel run)
- Deploy analysis-engine service to server
- Set `USE_ANALYSIS_ENGINE=0` (monolith still uses ThreadPoolExecutor)
- Analysis-engine is running but monolith isn't dispatching to it yet
- Verify: service starts, joins consumer group, heartbeat in Redis

### Step 3: Enable Stream Mode
- Set `USE_ANALYSIS_ENGINE=1` in server `.env`
- Restart monolith
- Monitor via `/debug` and `/debugcycle` commands:
  - `intraday_cycle_count` increments
  - `monitor_result_counts` shows SUCCESS/NO_DATA/ERROR
  - Stream lag: `XLEN orchestrator:analysis_jobs` should drain quickly
  - No timeout errors in logs

### Step 4: Verify During Market Hours
- Monday market open: verify both paths work (ThreadPoolExecutor fallback if needed)
- Check 12 PM: no stall (the original problem should be solved)
- Check positional analysis at 20:00: results flow correctly
- Check 52-week report: populated from stream results

### Step 5: Remove Feature Flag (future)
- After 1 week of stable operation, remove the ThreadPoolExecutor fallback path
- Delete `process_stock()`, `monitor()`, `thread_pool` from monolith
- This is a cleanup task, not blocking

---

## 11. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Stock reconstruction from Redis produces different analysis results | Medium | High | Parallel run test (Step 3 above) — compare results before enabling |
| Stream result timeout (worker too slow) | Low | Medium | 90s timeout matches current ThreadPoolExecutor timeout. Uncompleted jobs logged as ERROR. |
| Worker crash mid-job | Low | Low | XCLAIM by another worker after timeout. Job is reprocessed — idempotent (analysis overwrites, doesn't append). |
| Redis stream grows unbounded | Low | Low | MAXLEN ~500 on both streams. Redis LRU eviction as backup. |
| `analysis_json` too large for stream | Low | Low | ~5-20 KB per result. Redis streams handle MB-sized messages. MAXLEN 500 × 20 KB = 10 MB max. |
| Cross-cycle GEX state lost on worker restart | Medium | Low | GEX delta detection resets — first cycle after restart has no baseline. Same as monolith restart. Acceptable. |
| Signal re-emission duplicates signals | Low | Low | Worker's `_emit_signals()` no-ops (signal_bus=None). Only monolith emits. No duplication. |
| FuturesFetcher fallback removed | Medium | Low | Data-gateway should publish zerodha data. If missing, analysers skip gracefully. Fix in data-gateway, not analysis path. |

---

## 12. Files to Create/Modify

### New Files
| File | Purpose |
|------|---------|
| `services/analysis_engine/__init__.py` | Package init |
| `services/analysis_engine/main.py` | Worker entry point (sync consumer loop) |
| `services/analysis_engine/worker.py` | Job processor (Stock reconstruction + analyser execution) |
| `scripts/system_config/stockanalysis-analysis-engine.service` | systemd unit |
| `tests/services/test_analysis_worker.py` | Worker unit tests |
| `tests/intraday/test_stream_dispatch.py` | Stream dispatch unit tests |

### Modified Files
| File | Changes |
|------|---------|
| `common/constants.py` | Add `USE_ANALYSIS_ENGINE`, stream/group constants |
| `intraday/intraday_monitor.py` | Refactor `fetch_and_analyze_stocks()`, add `_dispatch_and_collect_stream()`, `_convert_stream_result()`, `_re_emit_signals_from_analysis()`, consumer group creation in `init()` |
| `scripts/system_config` (monolith unit) | Add analysis-engine to `After=`/`Requires=` |
| `Makefile` | Add `analysis-engine` and `analysis-engine-prod` targets |
| `.env` (server) | Add `USE_ANALYSIS_ENGINE=1` |

### Unchanged Files (intentionally)
| File | Why |
|------|-----|
| `analyser/*.py` | Analysers are pure functions of (Stock, mode, constants) — no changes |
| `analyser/Analyser.py` | Orchestrator unchanged — `_emit_signals()` already guards `signal_bus=None` |
| `intelligence/*.py` | SignalBus/Correlator/Narrator stay in monolith — signals re-emitted from results |
| `common/Stock.py` | Stock model unchanged |
| `services/common/stock_loader.py` | Loaders unchanged — already build Stock from Redis |
| `services/common/redis_proxy.py` | Already has all needed methods |
| `services/common/cycle_subscriber.py` | Monolith cycle sync unchanged |
| `notification/Notification.py` | Already XADDs to `notification:jobs` stream |

---

## 13. Future Evolution (Post-Phase 1C)

### Phase 1D: Extract Coordinator
- Move `_run_daily_loop()` scheduling to a standalone `services/coordinator/` service
- Coordinator publishes `orchestrator:cycle_trigger` + `orchestrator:analysis_jobs`
- Monolith becomes "reporting + intelligence + bot" (compact mode)

### Intelligence Service Extraction
- Replace in-memory `SignalBus` with `RedisSignalBus` (XADD to `intelligence:signals`)
- Worker publishes signals directly to stream (no re-emission needed)
- `SignalCorrelator` runs as separate consumer of `intelligence:signals`
- Requires `ContextBuilder` to read from Redis instead of `shared.app_ctx` (blocked on tick-service)

### Tick Service (Phase 1E)
- Move `ZerodhaTickerManager` + dual WS to `services/tick-service/`
- Publish ticks to Redis streams (`ticks:equity:*`, `ticks:option:*`)
- Unblocks intelligence service extraction (ContextBuilder reads from Redis)
- Requires auth-service for enctoken management
