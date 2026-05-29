# StockAnalysis — Claude Context

Indian NSE/BSE equity & derivatives intraday/positional analysis system.
Zerodha KiteConnect WebSocket + Sensibull REST/WS + yfinance + Telegram alerts.

## Commands

```bash
make venv && make install-dev   # first-time setup
make run-dev                    # dev intraday loop (PRODUCTION=0 DEV_INTRADAY=1)
make run-dev-positional         # dev EOD run
make run-dev-stock-intraday STOCK=RELIANCE   # single stock
make run-dev-index-intraday INDEX=NIFTY      # single index
make test                       # full pytest suite
make test-fast                  # stop on first failure
make lint                       # ruff check
make format                     # ruff format
make typecheck                  # pyright
make deploy                     # rsync + SSH to production server
make server-ssh                 # open SSH session to production
make server-logs-500            # last 500 lines of stock_monitor.log on server
```

Entry point: `intraday/intraday_monitor.py`

## Architecture

### Analyser Framework
- `BaseAnalyzer` (decorator pattern): `@BaseAnalyzer.intraday`, `@BaseAnalyzer.positional`, `@BaseAnalyzer.index_intraday`, `@BaseAnalyzer.index_positional`
- `AnalyserOrchestrator`: collects all registered analysers, calls them in order
- **Registration order matters** — `OptionSellerCompositeAnalyser` MUST be registered last (reads signals from all other analysers including `PANIC_EXHAUSTION`)
- Current registration order (10 analysers): VolumeAnalyser → TechnicalAnalyser → CandleStickAnalyser → IVAnalyser → FuturesAnalyser → PCRAnalyser → MaxPainAnalyser → OIChainAnalyser → GEXAnalyser → PanicModeAnalyser → **OptionSellerCompositeAnalyser**

### Scoring & Notifications
- `ANALYSIS_WEIGHTS` in `common/constants.py` — each signal key has a weight
- `MIN_NOTIFICATION_SCORE = 110` (intraday), `MIN_NOTIFICATION_SCORE_POSITIONAL = 150` (EOD)
- `PRIORITY_OVERRIDE` flag bypasses score gate entirely (used by composite setups)
- `OptionSellerCompositeAnalyser` has weight=0 — never inflates batch scores; bypasses via `PRIORITY_OVERRIDE` only
- `NEUTRAL_EXCLUDE_FROM_SCORE` — signals excluded from score aggregation (composite keys)

### OPTIONS_SOURCE Modes
Three modes controlled by `OPTIONS_SOURCE` env var:
- `zerodha` (default): tick-accurate OI/ltp/depth, no Greeks
- `sensibull`: Greeks/IV/max pain, ~10-30s snapshots, no auth required
- `both`: Zerodha is **authoritative** (ltp, OI, engine trigger); Sensibull **enriches only** existing strikes with Greeks/IV

**Critical invariant for `OPTIONS_SOURCE=both`:**
- `SensibullAdapter.apply(enrichment_only=True)` writes ONLY `{delta, gamma, theta, vega, iv, iv_change}` via `TickStore.update_option_tick(merge=True)`
- Sensibull NEVER calls `recompute_options_aggregate()` or `on_aggregate_updated()` in this mode — Zerodha owns those
- `TickStore.update_option_tick(merge=True)` silently skips strikes not yet created by Zerodha (Zerodha is authoritative creator)

### Threading Model
- `ThreadPoolExecutor` is **module-level** (initialised once in `init()`, not inside `with` block per cycle)
- `max_workers` defaults to 20 (i5-6200U, I/O-bound); tunable via `THREAD_POOL_WORKERS` env var
- `price_future.result(timeout=60)` — yfinance bulk download has hard 60s timeout
- `as_completed(..., timeout=90)` — per-stock analysis batch has hard 90s timeout with future cancellation
- `_stale_alerts_sent` set is protected by `_stale_alerts_lock` (threading.Lock)
- `AppContext` singleton (`common/shared.py`) is read-only in worker threads — safe

### Intelligence Layer
- `SignalBus` (pub/sub) → `SignalCorrelator` (cross-layer confluence detection) → `MarketNarrator` (Gemini Flash LLM)
- Morning bias: runs positional analysers on 1y daily data before market open, emits signals to `SignalBus`
- Requires `ENABLE_INTELLIGENCE=1`; LLM requires `ENABLE_NARRATOR=1` + `GEMINI_API_KEY`

## Coding Conventions

- **Never use `print()`** — always `logger.info/debug/warning/error` from `common/logging_util.py`
- **HTTP timeouts**: always split connect/read — `requests.get(url, timeout=(5, 10))` not `timeout=10`
- **Env vars**: all defined as constants in `common/constants.py` (e.g. `ENV_PRODUCTION = "PRODUCTION"`); read via `os.getenv(constant.ENV_PRODUCTION)`
- **`load_dotenv(override=True)`** is called in `init()` — never call it again elsewhere
- Do not add `@BaseAnalyzer.intraday` / `@BaseAnalyzer.positional` decorators to `OptionSellerCompositeAnalyser` methods — it overrides `run_all_intraday_analyses()` and `run_all_positional_analyses()` directly
- Python version: **3.12.3** on production server, **3.13.3** on local dev machine

### Analyser Logging Pattern

Every analyser method **must** follow this exact logging structure (see `IVAnalyser.py` and `GEXAnalyser.py` as canonical examples):

```python
def analyse_signal_name(self, stock: Stock) -> bool:
    try:
        logger.debug(f"[SIGNAL_KEY] {stock.stock_symbol} — start")

        # Gate checks (skip silently at DEBUG)
        if <no data>:
            logger.debug(f"[SIGNAL_KEY] {stock.stock_symbol} — <reason>, skip")
            return False

        # Source data log (what raw data was fetched)
        logger.debug(
            f"[SIGNAL_KEY] {stock.stock_symbol} | "
            f"SOURCE <raw_field>=<value> ..."
        )

        # Condition evaluation log (computed values vs thresholds)
        logger.debug(
            f"[SIGNAL_KEY] {stock.stock_symbol} | "
            f"INPUT <processed>=<value> | "
            f"CONDITION <value> <op> <threshold>"
        )

        if <condition met>:
            stock.set_analysis(...)
            logger.info(
                f"[SIGNAL_KEY] {stock.stock_symbol} — EMITTED | "
                f"<key>=<value> ..."
            )
            return True

        logger.debug(
            f"[SIGNAL_KEY] {stock.stock_symbol} — no signal | "
            f"<reason why condition failed>"
        )
        return False

    except Exception as e:
        logger.error(f"[SIGNAL_KEY] {stock.stock_symbol} — exception: {e}")
        logger.error(traceback.format_exc())
        return False
```

**Rules:**
- `[SIGNAL_KEY]` prefix must match the key used in `stock.set_analysis()` and `ANALYSIS_WEIGHTS`
- `start` log always first line in try block
- `SOURCE` line: raw input values before any processing
- `INPUT ... | CONDITION ...` line: computed values and the threshold being tested
- Signal emitted → `logger.info` with `EMITTED` keyword and key metrics
- Signal not emitted → `logger.debug` with `no signal | <reason>`
- All exceptions → `logger.error` + `traceback.format_exc()`
- Never `logger.info` for skipped/no-signal paths — keep INFO clean for actionable events only

## Environment & Deployment

- **Production server**: `hacker@100.92.21.31` (Tailscale VPN — NOT an EC2 instance, physical machine)
- **OS**: Ubuntu 24.04.4 LTS, hostname `Hacker-computer`
- **App dir on server**: `/home/hacker/StockAnalysis`
- **Log file**: `logs/stock_monitor.log` (local and server)
- **Systemd timers**: `stockanalysis.timer` (9:00 AM IST) + `stockanalysis-positional.timer` (8:00 PM IST)
- **Auth service**: `stockanalysis-auth.service` runs `auth/auth_login.py` (TOTP via `pyotp`) before each analysis run, writes fresh `ZERODHA_ENC_TOKEN` to `.env`
- `.env` is git-ignored; copy `.env.template` and fill in values
- Do not suggest AWS EC2 CLI commands — deployment uses direct SSH + rsync

## Testing

- Tests in `tests/`, mirrors package structure (e.g. `tests/analyser/`, `tests/common/`)
- `pytest` with `asyncio_mode = auto` — no `@pytest.mark.asyncio` needed
- `DeprecationWarning` treated as error except for `telegram`, `anyio`, `pytest_asyncio`
- Run a single module: `make test-module MODULE=analyser`

## Key Files

| File | Purpose |
|---|---|
| `intraday/intraday_monitor.py` | Main entry point, loop orchestration, `init()`, `fetch_and_analyze_stocks()` |
| `common/constants.py` | All env var names, scoring weights, thresholds |
| `common/shared.py` | `AppContext` singleton (`app_ctx`) |
| `common/Stock.py` | Stock data model (`priceData`, `options_live`, `options_aggregate`, `analysis`) |
| `analyser/Analyser.py` | `BaseAnalyzer` + `AnalyserOrchestrator` |
| `analyser/OptionSellerCompositeAnalyser.py` | Cross-analyser composite (GAMMA_TRAP, RANGE_BOUND_SETUP, SKEW_FADE_SETUP) |
| `fno/sensibull_adapter.py` | Translates Sensibull snapshots → Stock fields (enrichment_only mode) |
| `zerodha/tick_store.py` | Thread-safe live tick container (`merge=True` for enrichment) |
| `auth/auth_login.py` | TOTP-based automated Zerodha login |
| `common/scoring.py` | `calculate_score`, `should_notify`, `NotificationPriority`, `ScoreResult` |
