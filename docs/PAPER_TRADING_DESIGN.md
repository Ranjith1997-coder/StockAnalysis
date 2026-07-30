# Paper Trading System — Automated Option Selling

> **Status**: Design complete, implementation pending
> **Scope**: Automated paper trading system for option-selling strategies (Phase 1 — no PINN vol-surface)
> **Last Updated**: July 2026

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Signal Sources & Data Availability](#2-signal-sources--data-availability)
3. [Architecture](#3-architecture)
4. [SPAN Margin Calculation](#4-span-margin-calculation)
5. [Core Data Models](#5-core-data-models)
6. [Signal Router](#6-signal-router)
7. [Strategy Builder](#7-strategy-builder)
8. [MTM & Exit Engine](#8-mtm--exit-engine)
9. [Service Entry Point](#9-service-entry-point)
10. [Bot Commands](#10-bot-commands)
11. [Systemd Unit](#11-systemd-unit)
12. [Bottleneck Analysis](#12-bottleneck-analysis)
13. [Implementation Plan](#13-implementation-plan)
14. [Validation Plan](#14-validation-plan)

---

## 1. System Overview

An automated paper trading microservice that consumes option-seller signals from the existing analysis pipeline, opens virtual option-selling positions (Iron Condor, Strangle, Credit Spread, Naked CE/PE), marks them to market every 3 seconds using live tick data, and manages exits via a multi-rule engine. All positions, trades, and P&L are persisted in Redis — the system is crash-recoverable with zero in-memory state loss.

### What It Does

| Capability | Details |
|-----------|---------|
| Signal sources | 2 sources: OptionSeller composite setups (`RANGE_BOUND_SETUP`, `SKEW_FADE_SETUP`) + cross-layer confluence (LIVE + INTRADAY/POSITIONAL alignment) |
| Kill-switch | `GAMMA_TRAP` from analysis results → instant square-off |
| Strategies | Iron Condor, Strangle, Bull/Bear Credit Spread, Naked CE/PE |
| Universe | NIFTY, BANKNIFTY, SENSEX — live MTM from `data:options_live` |
| Capital | Rs 10,00,000 virtual, max 8 concurrent positions |
| Margin | Real SPAN margin via Zerodha margin calculator API |
| Cost model | Slippage (bid-ask spread), brokerage, STT, exchange charges |
| Exit rules | 200% SL, 50% profit target, theta decay, GAMMA_TRAP, 15:15 square-off |
| Modes | Intraday (current expiry, 15:15 close) + Positional (hold up to 3 days, roll expiry) |
| Persistence | All state in Redis hashes under `paper:*` namespace |
| Notifications | Entry + exit Telegram alerts, bot commands for monitoring |

### What It Does NOT Do (v1)

- No live broker integration (Zerodha `place_order` exists in `zerodha_connect.py` but is never called)
- No PINN volatility surface (planned as Phase 2 — third signal source via `vol:mispricing` stream)
- No stock F&O (limited to 3 indices where live option LTP is available)
- No portfolio-level delta hedging (each position is independent)
- No historical backtesting with option premiums (existing `backtest/` is directional equity only)

---

## 2. Signal Sources & Data Availability

### 2.1 Source 1: Analysis Results Stream (`analysis:results`)

The analysis-engine publishes results to `analysis:results` Redis stream every ~310 seconds (5 min intraday cycle). Each message contains:

| Field | Type | Description |
|-------|------|-------------|
| `analysis_json` | JSON string | Full `stock.analysis` dict with all analyser outputs |
| `trend_found` | `"true"`/`"false"` | Whether any signal crossed the notification threshold |
| `symbol` | string | Stock/index symbol |
| `is_index` | `"true"`/`"false"` | Whether this is an index |
| `score_result_json` | JSON string | Total score, priority, dominant sentiment |

**Composite setups** (from `OptionSellerCompositeAnalyser`) are stored in `analysis_json` → `NEUTRAL` bucket:

| Key | Namedtuple Type | Priority | Fields Used by Paper Trader |
|-----|----------------|----------|---------------------------|
| `RANGE_BOUND_SETUP` | `RangeBoundSetup` | HIGH | `put_wall_strike`, `call_wall_strike`, `setup_type` ("IRON_CONDOR"/"STRANGLE"), `iv_percentile`, `max_pain_dev_pct` |
| `SKEW_FADE_SETUP` | `SkewFadeSetup` | HIGH | `fade_direction` ("BULLISH"/"BEARISH"), `sr_level` (strike), `exhaustion_confidence` |
| `GAMMA_TRAP` | `GammaTrap` | CRITICAL | `direction`, `breach_signal`, `volume_signal` |
| `GAMMA_TRAP_ACTIVE` | bool (direct dict) | — | Suppresses RANGE_BOUND in same cycle; also check this for GAMMA_TRAP detection |

> **Type coercion note**: `json.dumps(stock.analysis, default=str)` in `worker.py:239` serializes namedtuple fields as strings. The signal router must coerce: `float(analysis_json["NEUTRAL"]["RANGE_BOUND_SETUP"]["put_wall_strike"])`.

> **Fast path**: If `trend_found == "false"` AND `PRIORITY_OVERRIDE` is not in the top-level keys, skip JSON parsing entirely. Composite setups always set `PRIORITY_OVERRIDE` (top-level key, a `NotificationPriority` enum serialized as string).

### 2.2 Source 2: Cross-Layer Confluence (`intelligence:confluence`)

Three processes (market-data, analysis-engine, monolith) each publish `Signal` objects to a
shared `intelligence:signals` Redis stream via `RedisSignalBus`. The standalone
`services/signal_intelligence/` service (built as part of fixing §2.4) is the one consumer that
combines them into a real `SignalCorrelator` and publishes the resulting `Confluence` events to
`intelligence:confluence`. **Paper trading consumes `intelligence:confluence` directly — not
the raw `intelligence:signals` stream** — since confluence detection already happened upstream.

Each `intelligence:confluence` message is produced by `Confluence.to_stream_fields()`
(`intelligence/correlator.py`) and should be parsed back with the matching
`Confluence.from_stream_fields()` classmethod rather than hand-rolled parsing:

| Field | Type | Notes |
|-------|------|-------|
| `symbol` | string | e.g. "NIFTY" |
| `direction` | string | `Direction.name` — "BULLISH"/"BEARISH" |
| `level` | string | "MODERATE" (2 layers) / "HIGH" (3 layers) |
| `score` | string | Confluence score (float, stringified) |
| `layer_count` | string | Number of distinct layers aligned |
| `layers` | string | Comma-separated `Layer.name` values, e.g. "LIVE,INTRADAY" |
| `has_contradiction` | string | "True"/"False" — opposing-direction signals present |
| `sources` | JSON string | List of `{layer, source, strength, timestamp}` — the individual signals behind this confluence |
| `timestamp` | string | Unix epoch |

> **Reconstruction**: `Confluence.from_stream_fields()` already handles enum-by-name lookups internally (`Direction[fields["direction"]]`, `Layer[l]` for each entry in `layers`) — the same care documented for raw `Signal` fields (`Layer.LIVE.value == "live"` but the stream stores `"LIVE"`, the `.name`) applies here too, so use the helper rather than re-deriving this logic.

### 2.3 Critical: Confluence Requires Multi-Layer Signals

The `SignalCorrelator` (`intelligence/correlator.py`) requires >=2 **different** layers aligned in the same direction to fire a confluence. The scoring formula:

```
score = (sum of signal strengths) + 5 * (layers - 1) + 3 * [LIVE present] - 3 * [contradiction]
```

Confluence level:
- **HIGH**: 3+ layers involved → naked directional selling
- **MODERATE**: 2 layers → credit spread (defined risk)

### 2.4 The Multi-Layer Problem — RESOLVED (signal-intelligence service)

**Original problem** (now fixed at the source, not worked around):
- `intelligence:signals` stream was **empty** (`XLEN = 0`) even with market-data active
- `RedisSignalBus` was set up in `services/market_data/main.py:486` → only `LiveStockEngine` and `LiveOptionsEngine` emitted to it → **only `Layer.LIVE` signals**
- `services/analysis_engine/main.py` never set `shared.app_ctx.signal_bus` → `_emit_signals()` silently no-op'd → no INTRADAY signals published
- The monolith's own `SignalCorrelator` only ever saw POSITIONAL signals (its only in-process caller), so it could never see 2+ layers either

**Fix applied**: `services/analysis_engine/main.py` and `intraday/intraday_monitor.py` now both wire `RedisSignalBus` at startup, so LIVE (market-data) + INTRADAY (analysis-engine) + POSITIONAL (monolith) signals all land in the same `intelligence:signals` stream. A new standalone, single-instance service — `services/signal_intelligence/` — is the one consumer that combines them into a real `SignalCorrelator` and publishes detected confluence to `intelligence:confluence`. It also sends the base Telegram alert directly (stateless call, no other process needed).

**Consequence for paper trading**: the paper trader does **not** need to reconstruct signals or run its own `SignalCorrelator` at all — that workaround is no longer necessary. It just subscribes to the real `intelligence:confluence` stream as a third input alongside `analysis:results`:

```
intelligence:confluence stream   (published by services/signal_intelligence)
  → Confluence.from_stream_fields(fields)   [shared wire-format helper, intelligence/correlator.py]
  → EntrySignal(
      strategy = "NAKED_CE" if confluence.direction == Direction.BEARISH else "NAKED_PE",
      symbol = confluence.symbol,
      direction = confluence.direction.name,
      score = confluence.score,
      level = confluence.level,          # "MODERATE" -> use CREDIT_SPREAD instead (defined risk)
      signal_source = "CONFLUENCE",
  )
  → entry_queue
```

This also resolves the old "Bug 3" — `intelligence:signals` published but never consumed — since `signal-intelligence` is now that consumer.

### 2.5 GAMMA_TRAP Detection Lag

`GAMMA_TRAP` is stored in `analysis_json` → `NEUTRAL.GAMMA_TRAP` and arrives via `analysis:results` every ~310 seconds. It is NOT published to `intelligence:signals` (NEUTRAL bucket is skipped by `_emit_signals`).

| Option | Lag | Risk | Recommendation |
|--------|-----|------|----------------|
| Accept 5-min lag from analysis:results | Up to 310s | In 5 min, NIFTY can move 100+ points | Acceptable for paper trading |
| Proxy detection in MTM engine | 3s | False positives on volatile moves | Add as supplement: if spot moves >2% in 60s, force-close |
| Modify _emit_signals to include NEUTRAL | 3s | Requires analysis-engine change | Phase 2 improvement |

**v1 decision**: Accept 5-min lag. Add proxy detection as supplement in the MTM engine.

### 2.6 Redis Data Keys (Verified)

| Key | Type | Fields | TTL | Available |
|-----|------|--------|-----|-----------|
| `data:options_live:{symbol}` | Hash | `{strike}_{CE|PE}` → JSON tick (`ltp, oi, prev_oi, volume, iv, gamma, delta, theta, vega, buy_qty, sell_qty, timestamp`) | -1 (no TTL) | Market hours only (DELETE+HSET every 1s) |
| `data:options_agg:{symbol}` | Hash | `atm_strike, max_oi_ce_strike, max_oi_pe_strike, live_pcr, total_ce_oi, total_pe_oi, atm_straddle_premium, atm_iv, atm_iv_percentile, max_pain_strike, gex_total, gex_regime, last_updated, option_tick_count, tick_count` | -1 | Market hours only |
| `data:tick:{symbol}` | Hash | `last_price, open, high, low, close, volume_traded, change, timestamp, tick_count` | -1 | Market hours only |
| `data:sensibull:{symbol}` | Hash | `current_json, historical_data_json, oi_chain_json, oi_chain_history_json, oi_history_json, iv_chart_history_json, last_fetch_time` | -1 | Always (data-gateway fetches) |
| `data:price:{symbol}` | Hash | yfinance OHLCV bars | -1 | Always (data-gateway fetches) |
| `auth:zerodha` | Hash | `enctoken` | -1 | Updated by auth-service at 09:00 + 18:50 |

> **Expiry source**: `data:options_agg` does NOT have an expiry field. Read from `data:sensibull:{symbol}` → `current_json` → `per_expiry_map` keys. Format: `['2026-07-21', '2026-07-28', ...]`. Sorted ascending, `[0]` = nearest expiry.

> **Greeks availability**: `iv, gamma, delta, theta, vega` in `data:options_live` ticks are only populated via Sensibull enrichment. Pure Zerodha ticks omit them. If `OPTIONS_SOURCE=zerodha`, Greeks will be missing. Paper trader must handle their absence (skip trade if `iv` missing or 0).

> **DELETE+HSET race**: `snapshot_publisher.py:112-113` does `DELETE` then `HSET` on `data:options_live`. Between the two, `hgetall` returns `{}`. MTM engine must skip the cycle if empty — do not error.

> **No stale data TTL**: All `data:*` keys have TTL=-1 (persist forever). If WS disconnects, stale ticks remain. MTM engine must check `data:options_agg:{symbol}` → `last_updated` age. If > 10 seconds, skip MTM.

---

## 3. Architecture

```
+------------------+    analysis:results (5 min)         +---------------------------+
| analysis-engine  |------------------------------------->|                           |
+------------------+    (composite setups: NEUTRAL bucket)|  paper-trading-service    |
                                                           |                           |
+--------------------+  intelligence:confluence           |  Thread 1: analysis       |
| signal-intelligence|------------------------------------>|    consumer (results)    |
| (already combines   |  (LIVE+INTRADAY+POSITIONAL         |    -> parse NEUTRAL bucket|
|  LIVE/INTRADAY/     |   confluence, already detected --  |    -> EntrySignal/ExitSig |
|  POSITIONAL itself) |   paper trader does NOT run its    |                           |
+--------------------+   own SignalCorrelator)             |  Thread 2: confluence     |
                                                           |    consumer               |
                                                           |    -> Confluence.from_    |
                                                           |      stream_fields()      |
                                                           |    -> EntrySignal         |
                                                           |      (source=CONFLUENCE)  |
                                                           |                           |
                                                           |  Thread 3: strategy       |
                                                           |    builder + SPAN margin  |
                                                           |    -> select strikes      |
                                                           |    -> compute premium     |
                                                           |    -> SPAN API call       |
                                                           |    -> open position       |
                                                           |                           |
                                                           |  Thread 4: MTM engine     |
                                                           |    (3s cycle)             |
                                                           |    -> read options_live   |
                                                           |    -> compute P&L         |
                                                           |    -> check exit rules    |
                                                           |    -> close + notify      |
                                                           |                           |
                                                           |  Thread 5: command        |
                                                           |    listener               |
                                                           |    (paper:commands)       |
                                                           |                           |
                                                           |  Thread 6: heartbeat      |
                                                           |    (30s)                  |
                                                           +---------------------------+
                                                                         |
                                                           +-------------v-----------+
                                                           |  Redis Store             |
                                                           |  paper:account            |
                                                           |  paper:positions:open     |
                                                           |  paper:positions:closed   |
                                                           |  paper:trades             |
                                                           |  paper:config             |
                                                           |  paper:cooldown:*         |
                                                           +---------------------------+
                                                                         |
                                                           +-------------v-----------+
                                                           |  notification:jobs       |
                                                           |  (entry + exit alerts)   |
                                                           +---------------------------+
```

### Thread Design

| Thread | Purpose | Interval | Blocking? |
|--------|---------|----------|-----------|
| 1. analysis consumer | `XREADGROUP` on `analysis:results` -- parses NEUTRAL bucket only (composite setups + GAMMA_TRAP) | 5s block | Yes |
| 2. confluence consumer | `XREADGROUP` on `intelligence:confluence` (own consumer group) -- reconstructs via `Confluence.from_stream_fields()`, no local correlation needed | 5s block | Yes |
| 3. strategy builder | Processes EntrySignal queue from threads 1+2 | Event-driven | No (queue poll) |
| 4. MTM engine | Read `data:options_live`, compute P&L, check exits | 3s sleep | No |
| 5. command listener | `XREADGROUP` on `paper:commands` | 5s block | Yes |
| 6. heartbeat | Write to `service:registry:paper-trading` | 30s | No |

Threads 1 and 2 feed `EntrySignal`/`ExitSignal` objects into a thread-safe `queue.Queue`. Thread 3 drains this queue and processes entries (strike selection, SPAN margin, position opening). This avoids blocking the stream consumers. Unlike the original design, thread 2 no longer needs to reconstruct raw `Signal` objects or run a `SignalCorrelator` -- confluence detection already happened upstream in the standalone `signal-intelligence` service (see §2.4); this thread only deserializes the already-detected `Confluence`.

### Confluence Consumption (no local SignalCorrelator)

The paper trader does **not** run its own `SignalCorrelator` — that would duplicate detection
already done by the standalone `signal-intelligence` service (§2.4) and risk drifting out of
sync with it. It only parses composite setups from `analysis:results` and deserializes
already-detected confluence from `intelligence:confluence`:

```python
from intelligence.correlator import Confluence
from intelligence.signal import Direction

# Thread 1 (analysis:results): composite setups only — NOT fed through any correlator
def on_analysis_result(fields):
    analysis_json = json.loads(fields["analysis_json"])
    neutral = analysis_json.get("NEUTRAL", {})
    if "GAMMA_TRAP" in neutral or "GAMMA_TRAP_ACTIVE" in neutral:
        exit_queue.put(ExitSignal(symbol=fields["symbol"], reason="GAMMA_TRAP"))
    if "RANGE_BOUND_SETUP" in neutral:
        setup = neutral["RANGE_BOUND_SETUP"]
        entry_queue.put(EntrySignal(
            strategy=setup["setup_type"],  # "IRON_CONDOR" or "STRANGLE"
            symbol=fields["symbol"],
            put_wall_strike=float(setup["put_wall_strike"]),
            call_wall_strike=float(setup["call_wall_strike"]),
            iv_percentile=float(setup.get("iv_percentile", 0)),
            signal_source="RANGE_BOUND_SETUP",
        ))
    if "SKEW_FADE_SETUP" in neutral:
        setup = neutral["SKEW_FADE_SETUP"]
        entry_queue.put(EntrySignal(
            strategy="CREDIT_SPREAD",
            symbol=fields["symbol"],
            direction=setup["fade_direction"],
            sr_level=float(setup["sr_level"]),
            signal_source="SKEW_FADE_SETUP",
        ))

# Thread 2 (intelligence:confluence): deserialize already-detected confluence.
# Confluence.from_stream_fields() is the shared wire-format helper defined in
# intelligence/correlator.py — the same one signal-intelligence uses to serialize.
def on_confluence_message(fields):
    confluence = Confluence.from_stream_fields(fields)
    if confluence.symbol not in LIVE_OPTIONS_INDICES:
        return
    strategy = "NAKED_CE" if confluence.direction == Direction.BEARISH else "NAKED_PE"
    if confluence.level == "MODERATE":
        strategy = "CREDIT_SPREAD"  # defined risk for 2-layer confluence
    entry_queue.put(EntrySignal(
        strategy=strategy,
        symbol=confluence.symbol,
        direction=confluence.direction.name,
        score=confluence.score,
        level=confluence.level,
        signal_source="CONFLUENCE",
    ))
```

---

## 4. SPAN Margin Calculation

### 4.1 API Details

The Zerodha SPAN margin calculator is a **public, unauthenticated** POST endpoint:

```
POST https://zerodha.com/margin-calculator/SPAN
Content-Type: application/x-www-form-urlencoded
User-Agent: Mozilla/5.0
Referer: https://zerodha.com/margin-calculator/SPAN/
```

### 4.2 Request Format

For single-leg option selling:
```
action=calculate
&exchange[]=NFO              # NFO for NIFTY/BANKNIFTY, BFO for SENSEX
&product[]=OPT               # OPT for options, FUT for futures
&scrip[]=NIFTY26JUL          # Scrip code (see 4.4 below)
&option_type[]=CE            # CE or PE
&strike_price[]=24000        # Strike price (empty for FUT)
&qty[]=65                    # Quantity in units (lot_size * num_lots)
&trade[]=sell                # sell or buy
```

For multi-leg (Iron Condor, Credit Spread), repeat `exchange[]`, `product[]`, `scrip[]`, `option_type[]`, `strike_price[]`, `qty[]`, `trade[]` for each leg.

### 4.3 Response Format

```json
{
  "last": {
    "span": 161540.75,
    "exposure": 31196.555,
    "netoptionvalue": 19041.75,
    "spread": 0,
    "total": 192737.305
  },
  "total": {
    "span": 161540.75,
    "exposure": 31196.555,
    "netoptionvalue": 19041.75,
    "spread": 0,
    "total": 192737.305
  }
}
```

| Field | Meaning | Use |
|-------|---------|-----|
| `span` | SPAN margin (core risk) | — |
| `exposure` | Exposure margin (additional) | — |
| `netoptionvalue` | Net option value credit | — |
| `spread` | Spread margin benefit | — |
| `total` | **Total margin required** | **Use this for position sizing** |

> **Multi-leg**: For Iron Condor/Credit Spread, use `total.total` (combined portfolio margin with spread benefit), NOT `last.total` (only the last leg's individual margin). Verified: Iron Condor `total.total` = Rs 79,803 vs sum of individual legs = Rs 3,86,568. Spread benefit = 80% margin reduction.

> **Empty response**: Invalid scrip → `{"last":[],"total":[]}`. Must handle as "margin unknown → skip trade".

### 4.4 Scrip Code Derivation

The scrip code format varies by symbol and expiry type. Verified from Zerodha instruments API:

| Symbol | Expiry Date | Tradingsymbol | SPAN Scrip | Format |
|--------|------------|---------------|-----------|--------|
| NIFTY | 2026-07-21 (weekly) | `NIFTY2672124050CE` | `NIFTY26721` | YY+M+DD |
| NIFTY | 2026-07-28 (monthly) | `NIFTY26JUL24050CE` | `NIFTY26JUL` | YY+MMM |
| BANKNIFTY | 2026-07-28 (monthly) | `BANKNIFTY26JUL57600CE` | `BANKNIFTY26JUL` | YY+MMM |
| SENSEX | 2026-07-23 (weekly) | `SENSEX2672377200CE` | `SENSEX26723` | YY+M+DD |

> **Critical**: `NIFTY26728` (weekly format for July 28) returns empty `[]` because July 28 is the **monthly** expiry — must use `NIFTY26JUL`.

**Derivation algorithm** (from tradingsymbol):
```python
def derive_scrip(tradingsymbol: str, strike: float, option_type: str) -> str:
    """Strip strike + option_type suffix from tradingsymbol to get scrip code."""
    suffix = str(int(strike)) + option_type  # "24050CE"
    if tradingsymbol.endswith(suffix):
        return tradingsymbol[:-len(suffix)]
    # Fallback: strip trailing digits + CE/PE
    import re
    match = re.match(r'^(.+?)(\d+)(CE|PE)$', tradingsymbol)
    if match:
        return match.group(1)
    return tradingsymbol
```

### 4.5 Instruments Cache

The paper trader needs the Zerodha instruments list to:
1. Derive scrip codes for SPAN API
2. Get correct lot sizes (NSE changes them periodically)
3. Get exchange (NFO vs BFO)
4. Get expiry dates

**Current lot sizes** (verified July 2026 from instruments API):

| Symbol | Lot Size | Exchange | `constants.py` (outdated) |
|--------|----------|----------|--------------------------|
| NIFTY | **65** | NFO | 75 |
| BANKNIFTY | **30** | NFO | 15 |
| SENSEX | **20** | BFO | 10 |

> **Action required**: Update `common/constants.py` `INDEX_LOT_SIZES` to match actual values, OR fetch lot sizes from instruments cache at startup (more robust).

**Cache strategy**:
- At startup: read enctoken from Redis `auth:zerodha` → call `kc.instruments()` → filter to NIFTY/BANKNIFTY/SENSEX options → cache in memory
- Refresh: when a new expiry date appears in `data:sensibull` (detected on first cycle of a new expiry week)
- Not more than once per day (instruments fetch is ~10-20MB)

**Enctoken dependency**: If enctoken is stale at startup, instruments fetch fails → paper trader waits and retries. Auth-service refreshes at 09:00 IST. Paper trader should log "waiting for valid enctoken" and retry every 30s.

### 4.6 API Characteristics

| Property | Value | Impact |
|----------|-------|--------|
| Latency (from server) | ~140ms per call | Acceptable for entry (not time-critical) |
| Auth required | No (public endpoint) | No enctoken needed for margin calculation |
| Rate limits | Unknown | Add 500ms delay between calls + cache |
| Error response | `{"last":[],"total":[]}` | Must check for empty array, not HTTP error |
| Multi-leg | Single POST with repeated array params | Use `total.total` field |

### 4.7 Margin Caching

SPAN margin changes slowly intraday (depends on volatility, not tick-by-tick). Cache per `(scrip, strike, option_type, qty, trade)` tuple with a 5-minute TTL. For multi-leg, cache per sorted legs tuple. This reduces API calls from ~8/entry to ~1/entry (first call) + cache hits.

### 4.8 Fallback If API Is Down

If `zerodha.com/margin-calculator/SPAN` is unreachable:
- Skip new entries (keep MTM running on existing positions — their margin is already computed and stored)
- Log warning every 60s
- Retry every 30s
- Do NOT fall back to approximation (15% notional is inaccurate — see bottleneck #13)

### 4.9 SPAN Margin Calculator Module

New module: `services/paper_trading/span_calculator.py`

```python
class SpanCalculator:
    """Real SPAN margin calculation via Zerodha margin calculator API."""

    API_URL = "https://zerodha.com/margin-calculator/SPAN"
    CACHE_TTL = 300  # 5 minutes

    def __init__(self, instruments: dict, redis: RedisProxy):
        self._instruments = instruments  # {symbol: {expiry: [(strike, opt_type, tradingsymbol, lot_size, exchange)]}}
        self._redis = redis
        self._cache: dict[str, float] = {}  # key -> total margin
        self._cache_times: dict[str, float] = {}

    def calculate_margin(self, legs: list[LegSpec]) -> float | None:
        """Calculate SPAN margin for a multi-leg position.

        Args:
            legs: list of (symbol, expiry, strike, option_type, qty, trade)
        Returns:
            Total margin in rupees, or None if API fails / invalid scrip
        """
        cache_key = self._make_cache_key(legs)
        if self._is_cached(cache_key):
            return self._cache[cache_key]

        # Build form data with repeated array params
        data = self._build_form_data(legs)
        try:
            resp = requests.post(self.API_URL, data=data, headers=self._headers, timeout=5)
            result = resp.json()
            total = result.get("total", {})
            if isinstance(total, list) or not total:
                return None  # invalid scrip
            margin = float(total.get("total", 0))
            self._cache[cache_key] = margin
            self._cache_times[cache_key] = time.time()
            return margin
        except Exception:
            return None
```

---

## 5. Core Data Models

### 5.1 Dataclasses (`services/paper_trading/models.py`)

```python
@dataclass
class OptionLeg:
    strike: float
    option_type: str        # "CE" | "PE"
    side: str               # "SELL" | "BUY"
    lots: int
    entry_premium: float    # per unit (after slippage)
    current_premium: float  # live MTM (0.0 initially)
    entry_timestamp: float

@dataclass
class PaperPosition:
    position_id: str        # UUID
    symbol: str             # "NIFTY", "BANKNIFTY", "SENSEX"
    strategy: str           # "IRON_CONDOR" | "STRANGLE" | "CREDIT_SPREAD" | "NAKED_CE" | "NAKED_PE"
    mode: str               # "intraday" | "positional"
    direction: str          # "NEUTRAL" | "BULLISH" | "BEARISH"
    legs: list[OptionLeg]
    expiry: str             # ISO date "2026-07-21"
    scrip: str              # SPAN scrip code e.g. "NIFTY26JUL"
    lot_size: int           # from instruments cache (65/30/20)
    entry_timestamp: float
    entry_credit: float     # net premium received (after slippage + costs)
    margin_blocked: float   # from SPAN API
    status: str             # "OPEN" | "CLOSED"
    exit_timestamp: float | None
    exit_premium: float | None
    pnl: float | None       # net P&L after all costs
    exit_reason: str | None # "STOP_LOSS" | "TARGET" | "THETA_DECAY" | "GAMMA_TRAP" | "SQUARE_OFF" | "EXPIRY" | "MANUAL"
    signal_source: str      # "RANGE_BOUND_SETUP" | "SKEW_FADE_SETUP" | "CONFLUENCE"
    signal_score: float | None
    signal_context: dict    # snapshot of trigger data
    # Cost model
    slippage_bps: int = 50     # 0.5% — sells fill at LTP*(1-slip), buys at LTP*(1+slip)
    brokerage_per_order: float = 20.0  # Rs 20 per leg per order (entry + exit)
    stt_pct: float = 0.001     # 0.1% on sell premium
    exchange_charges_pct: float = 0.0005  # 0.05% of premium

@dataclass
class PaperAccount:
    capital: float           # Rs 10,00,000
    realized_pnl: float
    unrealized_pnl: float
    margin_used: float
    available_margin: float
    open_positions: int
    day_start_capital: float
    max_drawdown: float
    daily_realized_pnl: float
    daily_trades: int
    daily_wins: int
    daily_losses: int
```

### 5.2 Redis Key Schema

| Key | Type | Fields | Purpose |
|-----|------|--------|---------|
| `paper:account` | Hash | `capital, realized_pnl, unrealized_pnl, margin_used, available_margin, open_positions, day_start_capital, max_drawdown, daily_realized_pnl, daily_trades, daily_wins, daily_losses` | Account state |
| `paper:positions:open` | Hash | `{position_id: json}` | All open positions |
| `paper:positions:closed:{YYYY-MM-DD}` | Hash | `{position_id: json}` | Per-day closed archive |
| `paper:trades` | Stream | append-only, `maxlen=5000` | Trade log |
| `paper:daily_pnl:{YYYY-MM-DD}` | Hash | `realized, unrealized, total, trades_count, wins, losses` | Daily P&L summary |
| `paper:config` | Hash | `max_positions, max_margin_pct, max_portfolio_margin_pct, daily_loss_limit_pct, sl_multiplier, target_pct, theta_exit_pct, theta_exit_time, slippage_bps, brokerage_per_order` | Config (editable via bot) |
| `paper:cooldown:{symbol}:{strategy}` | String + TTL 900s | `"1"` | 15-min cooldown per symbol+strategy |
| `paper:instruments:{symbol}` | Hash | `{expiry: json_of_options_list}` | Instruments cache (TTL 86400s) |

### 5.3 Cost Model (Slippage + Brokerage)

Realistic fill simulation for paper trading:

| Cost | Formula | Example (NIFTY 65 lot, Rs 85 premium) |
|------|---------|--------------------------------------|
| Slippage (sell) | `fill = LTP * (1 - slippage_bps/10000)` | 85 * 0.995 = Rs 84.575 |
| Slippage (buy) | `fill = LTP * (1 + slippage_bps/10000)` | 85 * 1.005 = Rs 85.425 |
| Brokerage | Rs 20 per leg per order (entry + exit) | 4 legs * 2 (entry+exit) * 20 = Rs 160 |
| STT (sell side) | `0.1% * premium * qty` | 0.001 * 84.575 * 65 = Rs 5.50 |
| Exchange charges | `0.05% * premium * qty` | 0.0005 * 84.575 * 65 = Rs 2.75 |
| Stamp duty | `0.003% * (premium * qty)` (sell side) | 0.00003 * 84.575 * 65 = Rs 0.16 |

Total friction per 4-leg Iron Condor trade: ~Rs 170. Without this, P&L is optimistic by 10-20%.

---

## 6. Signal Router

### 6.1 EntrySignal / ExitSignal

```python
@dataclass
class EntrySignal:
    strategy: str           # "IRON_CONDOR" | "STRANGLE" | "CREDIT_SPREAD" | "NAKED_CE" | "NAKED_PE"
    symbol: str
    direction: str = "NEUTRAL"
    # RANGE_BOUND_SETUP fields
    put_wall_strike: float | None = None
    call_wall_strike: float | None = None
    iv_percentile: float | None = None
    # SKEW_FADE_SETUP fields
    sr_level: float | None = None
    # CONFLUENCE fields
    score: float | None = None
    level: str | None = None      # "HIGH" | "MODERATE"
    # Common
    signal_source: str = ""
    signal_context: dict = field(default_factory=dict)

@dataclass
class ExitSignal:
    symbol: str
    reason: str              # "GAMMA_TRAP" | "MANUAL"
    position_id: str | None = None  # None = all positions on symbol
```

### 6.2 Entry Filters

| Filter | Rule | Redis Key |
|--------|------|-----------|
| Cooldown | 15 min between entries on same (symbol, strategy) | `paper:cooldown:{symbol}:{strategy}` TTL=900 |
| Max positions | <= 8 open positions | Check `paper:positions:open` HLEN |
| Max per symbol | 1 open position per (symbol, strategy) | Scan open positions |
| Available margin | margin_needed <= available_margin | Check `paper:account` |
| Max portfolio margin | total margin_used <= 40% of capital (Rs 4L) | Check `paper:account` |
| Daily loss limit | If daily_realized_pnl <= -3% of capital (-Rs 30K), stop new entries | Check `paper:account` |
| Correlated guard | Max 2 naked shorts in same direction across all symbols | Scan open positions |
| Strike in options_live | Required strikes must have live ticks | Check `data:options_live:{symbol}` |
| Greeks available | `iv` must be present and > 0 for all short legs | Check tick JSON |

---

## 7. Strategy Builder

### 7.1 Expiry Selection

Read from `data:sensibull:{symbol}` → `current_json` → `per_expiry_map` keys:

```python
def select_expiry(symbol: str, mode: str, redis: RedisProxy) -> str:
    raw = redis.hget(f"data:sensibull:{symbol}", "current_json")
    data = json.loads(raw)
    expiries = sorted(data["per_expiry_map"].keys())  # ["2026-07-21", "2026-07-28", ...]
    if mode == "intraday":
        return expiries[0]  # nearest weekly
    # Positional: current expiry if >= 3 days to expiry, else next
    today = date.today()
    for exp in expiries:
        dte = (date.fromisoformat(exp) - today).days
        if dte >= 3:
            return exp
    return expiries[0] if expiries else ""
```

### 7.2 Strike Selection

**Iron Condor / Strangle** (from RANGE_BOUND_SETUP):
- Short Put: `put_wall_strike` (sell PE)
- Short Call: `call_wall_strike` (sell CE)
- Long Put: `put_wall_strike - 2 * strike_gap` (buy PE — protection)
- Long Call: `call_wall_strike + 2 * strike_gap` (buy CE — protection)
- If `setup_type == "STRANGLE"`: no long legs (naked)

**Credit Spread** (from SKEW_FADE_SETUP):
- Bullish fade (sell put credit spread):
  - Short Put: `sr_level` strike (sell PE)
  - Long Put: `sr_level - 2 * strike_gap` (buy PE)
- Bearish fade (sell call credit spread):
  - Short Call: `sr_level` strike (sell CE)
  - Long Call: `sr_level + 2 * strike_gap` (buy CE)

**Naked Directional** (from Confluence):
- BULLISH confluence → sell PE at 1 strike OTM (`atm_strike - strike_gap`)
- BEARISH confluence → sell CE at 1 strike OTM (`atm_strike + strike_gap`)
- HIGH confluence (score > 15): sell 2 strikes OTM for more safety
- No long protection leg

### 7.3 Strike Gap

Read from `data:options_live:{symbol}` — sort all strike keys, find minimum difference:

```python
def compute_strike_gap(symbol: str, redis: RedisProxy) -> float:
    raw = redis.hgetall(f"data:options_live:{symbol}")
    if not raw:
        return 50.0  # NIFTY default
    strikes = sorted(set(float(k.rsplit("_", 1)[0]) for k in raw.keys()))
    if len(strikes) < 2:
        return 50.0
    gaps = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
    return min(gaps)
```

Expected values: NIFTY=50, BANKNIFTY=100, SENSEX=100.

### 7.4 Premium Lookup

Read LTP from `data:options_live:{symbol}`:

```python
def get_ltp(symbol: str, strike: float, option_type: str, redis: RedisProxy) -> float | None:
    raw = redis.hget(f"data:options_live:{symbol}", f"{float(strike)}_{option_type}")
    if not raw:
        return None  # illiquid strike — skip trade
    tick = json.loads(raw)
    ltp = float(tick.get("ltp", 0))
    if ltp <= 0:
        return None
    return ltp
```

### 7.5 Position Sizing

```python
def compute_lots(capital: float, margin_per_lot: float, risk_pct: float) -> int:
    """Compute number of lots based on capital allocation."""
    risk_amount = capital * risk_pct
    lots = int(risk_amount / margin_per_lot)
    return max(lots, 0)  # 0 if margin > risk allocation

# Risk percentages by strategy:
RISK_PCT = {
    "IRON_CONDOR": 0.08,    # 8%  — defined risk, SPAN spread benefit (verified: ~68.5K margin)
    "STRANGLE": 0.18,       # 18% — naked, no spread benefit (verified: ~186K margin)
    "CREDIT_SPREAD": 0.05,  # 5%  — defined risk (verified: ~37.4K margin)
    "NAKED_CE": 0.16,       # 16% — naked, single leg (verified: ~155K margin)
    "NAKED_PE": 0.16,       # 16% — naked, single leg (verified: ~155K margin)
}
```

> **Verified against the real Zerodha SPAN API** (NIFTY, 2026-07-21 expiry, ATM±1-2 strikes,
> lot_size=65, capital=₹10,00,000): naked legs carry ~4-5x the margin of defined-risk spreads
> because SPAN's spread-margin benefit only applies when a long leg offsets the short. The
> original table gave `STRANGLE`/`NAKED_CE`/`NAKED_PE` the same 5% budget as `CREDIT_SPREAD`
> despite ~4-5x higher real margin (₹1,86,266 and ₹1,55,069 respectively vs ₹37,370) — under
> the old constants `compute_lots()` returns **0 lots for every naked/strangle signal, always**,
> silently starving `RANGE_BOUND_SETUP` (`STRANGLE` variant) and every `CONFLUENCE`-sourced
> naked entry. The revised percentages are the minimum needed for 1 lot at these real margin
> levels, and stay consistent with the portfolio-level guards (§8.5): even at 18%, only ~2 such
> naked/strangle positions fit before the 40%-of-capital margin cap kicks in, which is a
> reasonable ceiling on undefined-risk exposure. Re-verify periodically — SPAN margin moves
> with volatility, so these are representative, not fixed constants to trust indefinitely.

### 7.6 Full Entry Flow

```
1. EntrySignal received from queue
2. Check entry filters (cooldown, max positions, margin, daily loss limit)
3. Select expiry from data:sensibull
4. Compute strike_gap from data:options_live
5. Select strikes based on strategy + signal fields
6. Read LTP for all legs from data:options_live
   → If any strike missing or LTP=0: skip trade, log "illiquid strike"
   → If iv missing or 0 for short legs: skip trade, log "no greeks"
7. Apply slippage to entry premiums
8. Compute entry_credit = sum(sell fills) - sum(buy fills)
9. Get lot_size from instruments cache
10. Compute qty = lot_size * num_lots
11. Build SPAN API request with all legs
12. Call SpanCalculator.calculate_margin(legs)
    → If None (API down / invalid scrip): skip trade
13. Compute lots = compute_lots(capital, margin, risk_pct)
    → If 0: skip trade, log "insufficient capital for margin"
14. Apply STT + exchange charges on sell legs
15. Create PaperPosition, store in Redis
16. Set cooldown key with TTL
17. Update paper:account (margin_used, available_margin, open_positions)
18. Send entry notification to notification:jobs
```

---

## 8. MTM & Exit Engine

### 8.1 MTM Cycle (every 3 seconds)

For each open position in `paper:positions:open`:

```
1. Read data:options_agg:{symbol} → check last_updated age
   → If age > 10s: skip MTM (stale data), log debug
   → If key missing (race condition): skip, log debug

2. Read data:options_live:{symbol} via hgetall
   → If empty (DELETE+HSET race): skip, log debug

3. For each leg:
   → Find tick by f"{strike}_{option_type}" in options_live hash
   → If missing: use last known premium (log warning), or skip position
   → Update leg.current_premium = tick.ltp

4. Compute current position value:
   → sell_value = sum(sell legs: current_premium * lots * lot_size)
   → buy_value = sum(buy legs: current_premium * lots * lot_size)
   → current_debit = sell_value - buy_value  (what it costs to close)
   → unrealized_pnl = entry_credit - current_debit (minus entry costs)
   → Apply exit slippage to current premiums

5. Check exit rules (priority order — see 8.2)

6. If exit triggered:
   → Apply exit costs (brokerage, STT on buy-back, exchange charges)
   → Compute net P&L = (entry_credit - exit_debit) - entry_costs - exit_costs
   → Update PaperPosition (status=CLOSED, exit_timestamp, exit_premium, pnl, exit_reason)
   → Move from paper:positions:open to paper:positions:closed:{date}
   → Append to paper:trades stream
   → Update paper:account (realized_pnl += pnl, margin_used -= margin, open_positions--)
   → Update paper:daily_pnl:{date}
   → Send exit notification to notification:jobs

7. Update paper:account unrealized_pnl (sum of all open positions)
```

### 8.2 Exit Rules (Priority Order)

**DTE-driven, not mode-driven.** `mode` ("intraday"/"positional") only ever
determines which expiry gets selected at entry (§7.1) — it was never a
meaningful input to how an *open* position should be managed, and deriving
exit behavior from it meant exit discipline depended on which pipeline
happened to emit the entry signal, not on the trade's own state (a position
opened via an 8pm positional composite setup and one opened via a 9:15
intraday cycle behave identically from here on if they end up with the same
DTE). Instead, every rule below keys off `dte` (days to expiry) directly:

| Priority | Rule | On expiry day (`dte == 0`) | Otherwise | Implementation |
|----------|------|----------|------------|----------------|
| 1 | GAMMA_TRAP | Instant close | Instant close | ExitSignal from analysis:results; also proxy: spot moves >2% in 60s |
| 2 | Stop Loss | Current loss >= 200% of entry credit | same | `unrealized_pnl <= -2 * entry_credit` |
| 3 | Profit Target | Current profit >= 50% of entry credit | same | `unrealized_pnl >= 0.5 * entry_credit` |
| 4 | Theta Decay | 75% decay by 14:00 IST | 75% decay once `dte <= 3` (no clock-time gate) | `current_debit <= 0.25 * entry_credit AND (dte==0 → time>=14:00, else dte<=3)` |
| 5 | Square-off / Expiry | 15:15 IST, tagged `EXPIRY` | 15:00 IST on the day *before* expiry (`dte==1`), tagged `SQUARE_OFF` | Same 15:15/15:00 clock check either way — only the trade-log tag differs |

This collapses what used to be 3 separate rows (Time Square-off / Expiry, split by mode) into one DTE-keyed rule, since a same-day square-off and an "it's expiry day" close are mechanically the same event — the `EXPIRY` vs `SQUARE_OFF` label is kept purely for readability in the trade log, not because the underlying check differs.

### 8.3 GAMMA_TRAP Proxy Detection (Supplement)

Since GAMMA_TRAP from `analysis:results` has a 5-min lag, add a proxy in the MTM engine:

```python
def check_gamma_trap_proxy(symbol: str, redis: RedisProxy) -> bool:
    """Proxy: if spot moves >2% in 60 seconds, force-close all positions."""
    tick = redis.hgetall(f"data:tick:{symbol}")
    if not tick:
        return False
    last_price = float(tick.get("last_price", 0))
    close = float(tick.get("close", 0))
    if close <= 0:
        return False
    change_pct = abs((last_price - close) / close * 100)
    # Also check rapid move: compare current price vs price 60s ago
    # (store last_price snapshot per symbol in memory)
    return change_pct >= 2.0
```

### 8.4 Daily Reset

| Time | Action |
|------|--------|
| 09:15 IST | Snapshot `day_start_capital = capital + realized_pnl`; reset `daily_realized_pnl=0, daily_trades=0, daily_wins=0, daily_losses=0` |
| 15:30 IST | Compute daily P&L summary → `paper:daily_pnl:{date}`; update `max_drawdown` if today's drawdown is larger |

### 8.5 Portfolio Risk Guards

| Guard | Threshold | Action |
|-------|-----------|--------|
| Max portfolio margin | 40% of capital (Rs 4L) | Block new entries |
| Daily loss limit | -3% of capital (-Rs 30K) | Block new entries for rest of day |
| Max correlated naked | 2 naked shorts in same direction | Block new naked entry |
| Max positions | 8 | Block new entries |
| Max per (symbol, strategy) | 1 | Block duplicate |

---

## 9. Service Entry Point

### 9.1 Thread Layout (`services/paper_trading/main.py`)

```
Startup:
  1. Read enctoken from Redis auth:zerodha
  2. Fetch Zerodha instruments → cache NIFTY/BANKNIFTY/SENSEX options
  3. Initialize SpanCalculator with instruments cache
  4. Load paper:config from Redis (or write defaults)
  5. Load paper:account from Redis (or initialize with Rs 10L)
  6. Install crash handler: install_crash_handler("paper-trading")
  7. Start 6 threads

Threads:
  1. analysis_consumer     — XREADGROUP on analysis:results (5s block) — composite setups only
  2. confluence_consumer   — XREADGROUP on intelligence:confluence (5s block) — no local
                             SignalCorrelator; deserializes via Confluence.from_stream_fields()
  3. strategy_processor    — drains entry_queue + exit_queue
  4. mtm_engine            — 3s sleep cycle
  5. command_listener      — XREADGROUP on paper:commands (5s block)
  6. heartbeat             — 30s to service:registry:paper-trading

Shutdown (SIGTERM/SIGINT):
  1. Set _running = False
  2. Join all threads (5s timeout each)
  3. Set heartbeat status="shutdown"
  4. Close Redis connection
```

### 9.2 Consumer Groups

| Stream | Consumer Group | Consumer Name |
|--------|---------------|---------------|
| `analysis:results` | `paper-trader` | `paper-trader-1` |
| `intelligence:confluence` | `paper-trader-confluence` | `paper-trader-1` |
| `paper:commands` | `paper-trader-cmd` | `paper-trader-1` |

> **Note**: `analysis:results` already has consumer groups `monolith` and `analysis-workers`-adjacent readers; `intelligence:confluence` already has `monolith-confluence` (the monolith's HIGH-only narrator consumer). The paper trader joins its **own** new groups on both streams — Redis consumer groups are independent, so every group gets a full copy of the stream regardless of how many other groups exist.

### 9.3 Market Hours Guard

All entry and MTM activity is gated to 09:15–15:30 IST. Outside market hours:
- Stream consumers still run (drain backlog)
- No new entries processed
- MTM engine sleeps
- Bot commands still work (read from Redis)

---

## 10. Bot Commands

### 10.1 Command Module (`notification/commands/paper.py`)

New module following existing pattern (`@guard` decorator, `HANDLERS` list):

| Command | Description | Chat Restriction |
|---------|-------------|-----------------|
| `/paper_positions` | List all open positions with live MTM, entry credit, current P&L % | Allowed chat |
| `/paper_pnl` | Today's P&L: realized, unrealized, total, win rate, max DD | Allowed chat |
| `/paper_trades [N]` | Last N closed trades (default 10). Strategy, symbol, entry/exit, P&L, exit reason | Allowed chat |
| `/paper_close [id\|all]` | Manually close position (or all). Records at current market premium | Allowed chat |
| `/paper_config` | Show current config (capital, max_positions, SL%, target%, etc.) | Allowed chat |
| `/paper_config set <key> <value>` | Update config in Redis | Debug chat only |
| `/paper_reset` | Reset account to Rs 10L, close all positions | Debug chat only |

> Commands write to `paper:commands` stream → paper-trading service consumes and executes. Display commands (`/paper_positions`, `/paper_pnl`, `/paper_trades`) read directly from Redis for instant response (same pattern as `/debugstats`).

### 10.2 Registration

In `notification/commands/__init__.py`:
1. Add `from notification.commands import paper` to imports
2. Add `paper` to the module tuple in `register_all()`

### 10.3 Notification Format

**Entry notification**:
```
📋 PAPER TRADE OPENED
Symbol: NIFTY | Strategy: IRON_CONDOR | Mode: intraday
Short PE 24000 @ ₹84.58 | Long PE 23900 @ ₹62.10
Short CE 24200 @ ₹77.42 | Long CE 24300 @ ₹55.28
Credit: ₹44.62 × 65 = ₹2,900 | Margin: ₹79,803
Signal: RANGE_BOUND_SETUP | IV Pct: 72%
```

**Exit notification**:
```
✅ PAPER TRADE CLOSED
Symbol: NIFTY | Strategy: IRON_CONDOR
Entry credit: ₹2,900 | Exit debit: ₹1,450
P&L: +₹1,372 (net of costs ₹178)
Exit reason: TARGET (50% profit)
Duration: 42 min | Lots: 1
```

---

## 11. Systemd Unit

`configs/stockanalysis-paper-trading.service`:

```ini
[Unit]
Description=StockAnalysis Paper Trading Service
After=network.target redis-server.service

[Service]
Type=simple
User=hacker
WorkingDirectory=/home/hacker/StockAnalysis
ExecStart=/home/hacker/StockAnalysis/.venv/bin/python -m services.paper_trading.main
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/hacker/StockAnalysis/.env
CPUQuota=15%
MemoryMax=150M

[Install]
WantedBy=multi-user.target
```

---

## 12. Bottleneck Analysis

### Critical Bottlenecks (System won't function without fixing)

| # | Bottleneck | Root Cause | Fix |
|---|-----------|------------|-----|
| 1 | **[FIXED]** `intelligence:signals` only had LIVE layer signals — confluence impossible | `analysis_engine/worker.py` never set `shared.app_ctx.signal_bus` → `_emit_signals()` returned early | Fixed at the source: `services/analysis_engine/main.py` + `intraday/intraday_monitor.py` now both wire `RedisSignalBus`, and a new standalone `services/signal_intelligence/` service runs the one real `SignalCorrelator`, publishing to `intelligence:confluence`. Paper trader just consumes that stream directly (§2.2/§2.4) — no local correlator needed |
| 2 | GAMMA_TRAP only arrives via `analysis:results` (5-min lag) | `_emit_signals()` skips NEUTRAL; GAMMA_TRAP stored in `stock.analysis["NEUTRAL"]` | Accept 5-min lag for paper trading; add proxy detection (spot >2% move in 60s) in MTM engine |
| 3 | No expiry data in `data:options_agg` or `data:options_live` | `SnapshotPublisher` doesn't publish expiry field | Read from `data:sensibull:{symbol}` → `current_json` → `per_expiry_map` keys |
| 4 | Lot sizes outdated in `constants.py` | NSE changed lot sizes; `INDEX_LOT_SIZES` not updated | Fetch from Zerodha instruments API at startup; update `constants.py` |
| 5 | No live data outside market hours | `data:tick:*`, `data:options_live:*` have no TTL, only written when WS ticks flow | Guard all entry/MTM with market hours check (09:15–15:30 IST) |
| 6 | SPAN scrip code format varies by symbol + expiry type | Weekly = `YY+M+DD`, monthly = `YY+MMM`; NIFTY26728 fails (monthly uses NIFTY26JUL) | Derive from actual tradingsymbol via instruments cache |
| 7 | SENSEX on BFO, not NFO | SPAN API returns `[]` if wrong exchange | Store exchange from instruments cache |

### Moderate Bottlenecks (Cause bugs or unrealistic results)

| # | Bottleneck | Impact | Fix |
|---|-----------|--------|-----|
| 8 | No slippage/brokerage modeling | P&L optimistic by 10-20% | Add cost model: slippage (0.5%), brokerage (Rs 20/leg), STT (0.1%), exchange charges (0.05%) |
| 9 | No entry notifications | Poor monitoring | Add entry alert to `notification:jobs` on position open |
| 10 | No portfolio-level risk guards | Over-exposure if all 3 indices fire simultaneously | Max portfolio margin 40%, daily loss limit -3%, max 2 correlated naked shorts |
| 11 | `data:options_live` DELETE+HSET race | `hgetall` returns `{}` between DELETE and HSET | Skip MTM cycle if empty; do not error |
| 12 | No stale data guard (no TTL on data keys) | WS disconnect = stale ticks persist → false exits | Check `data:options_agg:{symbol}` → `last_updated` age > 10s → skip MTM |
| 13 | Illiquid strikes not in `data:options_live` | OI wall strikes may be far OTM, not subscribed | Skip trade if strike not found in options_live |
| 14 | `analysis_json` values are strings | `json.dumps(default=str)` converts namedtuple fields to strings | Coerce types: `float(setup["put_wall_strike"])` |
| 15 | Greeks only from Sensibull enrichment | If `OPTIONS_SOURCE=zerodha`, iv/gamma/delta/theta/vega missing | Check `iv > 0` for short legs; skip trade if missing |
| 16 | SPAN API returns `[]` for invalid scrip | Parse error if not handled | Check `isinstance(total, list)` → return None → skip trade |
| 17 | SPAN API rate limiting unknown | Rapid entries could trigger rate limit | 500ms delay between calls + 5-min cache |
| 18 | SPAN API is hard dependency — no fallback if down | Can't compute margin → can't open positions | Skip new entries; keep MTM running on existing positions |
| 19 | **[FIXED]** `RISK_PCT` gave naked strategies the same budget as defined-risk spreads | Verified against real SPAN API: `STRANGLE`/`NAKED_CE`/`NAKED_PE` real margin (~₹155-186K) is ~4-5x `CREDIT_SPREAD`'s (~₹37K) since naked legs get no spread-margin offset — under the old 5% budget, `compute_lots()` always returned 0 lots for these, silently starving the `STRANGLE` variant of `RANGE_BOUND_SETUP` and every `CONFLUENCE`-sourced naked entry | §7.5 `RISK_PCT` raised to 0.16 (naked single-leg) / 0.18 (strangle) — minimum needed for 1 lot at verified margin levels, still bounded by the 40%-of-capital portfolio guard (§8.5) |

### Minor Bottlenecks (Low impact, easy fixes)

| # | Bottleneck | Fix |
|---|-----------|-----|
| 20 | `RedisProxy` lacks `hincrby` | Use `hset` with computed values (fine for single-threaded MTM) |
| 21 | `RedisProxy` lacks `xpending`/`xclaim` | Not critical for v1 (at-least-once with `xack` in `finally`) |
| 22 | Strike gap not explicitly defined | `compute_strike_gap()` helper from options_live keys |
| 23 | Instruments cache needs enctoken | Read from Redis `auth:zerodha`; retry every 30s if stale |
| 24 | Cooldown keys not defined | `paper:cooldown:{symbol}:{strategy}` TTL=900; `paper:cooldown:vol:{symbol}:{strike}:{type}` TTL=600 |
| 25 | Signal reconstruction enum mismatch | Use `Direction[name]` / `Layer[name]`, NOT `Direction(value)` / `Layer(value)` |
| 26 | `GAMMA_TRAP_ACTIVE` boolean flag | Check both `GAMMA_TRAP` and `GAMMA_TRAP_ACTIVE` in NEUTRAL bucket |

### Validated (Plan gets RIGHT)

| Aspect | Verification |
|--------|-------------|
| `INDEX_LOT_SIZES` constant name | `common/constants.py:29` — correct (but values outdated) |
| `LIVE_OPTIONS_INDICES` | `common/constants.py:25` — ["NIFTY", "BANKNIFTY", "SENSEX"] |
| `install_crash_handler(service_name)` | `services/common/crash_handler.py:25` — correct signature |
| `RedisProxy` supports consumer groups | `xreadgroup`, `xgroup_create`, `xack`, `xadd` — all present |
| `notification/commands/__init__.py` pattern | Add module to import tuple + `HANDLERS` list |
| `analysis:results` has `analysis_json` field | `worker.py:239` — confirmed |
| `SignalCorrelator` uses `on_confluence` callback | `correlator.py:64` — confirmed |
| `Confluence.level` = HIGH (3+ layers) / MODERATE (2 layers) | `correlator.py:39-43` — computed `@property`, no LOW |
| `data:options_agg` has `atm_strike`, `max_oi_ce_strike`, `max_oi_pe_strike` | `tick_store.py:52-81` — confirmed |
| `data:options_live` hash keys = `{strike}_{CE|PE}` | `snapshot_publisher.py:104-109` — confirmed |
| SPAN API latency from server | ~140ms — acceptable |
| SPAN API multi-leg spread benefit | 80% margin reduction for Iron Condor — verified |
| Neither `services/paper_trading/` nor `services/vol_surface/` exist | Greenfield — confirmed |

---

## 13. Implementation Plan

### Phase 1: Paper Trading System (7 tasks, no PINN)

| Task | Module | Description | Est. Effort |
|------|--------|-------------|-------------|
| 1 | `services/paper_trading/models.py` | Data models (OptionLeg, PaperPosition, PaperAccount), Redis schema, cost model, expiry/scrip helpers | 3h |
| 2 | `services/paper_trading/span_calculator.py` | SPAN margin calculator: instruments cache, scrip derivation, API client, caching, multi-leg support | 3h |
| 3 | `services/paper_trading/signal_router.py` | Stream consumers (analysis:results for composite setups, intelligence:confluence for confluence — no local SignalCorrelator needed), entry filters, cooldown keys | 3h |
| 4 | `services/paper_trading/strategy_builder.py` | Strike selection, premium lookup, slippage, position sizing, SPAN margin call, position creation | 3h |
| 5 | `services/paper_trading/engine.py` | MTM cycle (3s), exit rules (priority table), GAMMA_TRAP proxy, stale data guard, portfolio risk guards, daily reset, notifications | 4h |
| 6 | `services/paper_trading/main.py` | Service entry point: 6 threads, crash handler, version, heartbeat, graceful shutdown, config loading | 2h |
| 7 | `notification/commands/paper.py` + `configs/stockanalysis-paper-trading.service` + tests | Bot commands, systemd unit, ~30 unit tests | 4h |

**Total: ~22h** (1h saved on task 3 now that confluence detection is already handled upstream by `signal-intelligence`)

### Phase 2: PINN Volatility Surface (future, 7 tasks)

Tasks 8-14 from the original plan (collector, model, loss, training, inference, service, tests). Requires `torch` installation. Adds third signal source (`vol:mispricing` stream) to the signal router.

### Prerequisites

| Prerequisite | Action |
|-------------|--------|
| Update `INDEX_LOT_SIZES` in `common/constants.py` | NIFTY=65, BANKNIFTY=30, SENSEX=20 |
| Verify `OPTIONS_SOURCE=sensibull` in `.env` | Greeks required for paper trading |
| Verify enctoken in Redis `auth:zerodha` | Needed for instruments fetch at startup |

---

## 14. Validation Plan

1. **Unit tests**: ~30 tests in `tests/services/test_paper_trading.py` — models, signal router, strategy builder, MTM engine, SPAN calculator, bot commands
2. **Dry run**: Deploy on server. During market hours, observe `/paper_positions` — entries should appear within 15 min of market open if signals fire
3. **P&L sanity**: After 1 trading day, `/paper_pnl` should show realistic numbers. Cross-check: each closed trade's P&L = (entry_credit - exit_debit) * lot_size - costs
4. **Exit validation**: At 15:15 IST, all intraday positions auto-close. GAMMA_TRAP closes positions within 5 min of signal (analysis:results lag)
5. **Bot commands**: Test `/paper_positions`, `/paper_pnl`, `/paper_trades`, `/paper_close`, `/paper_config`, `/paper_reset` from allowed chat
6. **SPAN accuracy**: Compare SPAN API result with Zerodha margin calculator web UI for same position — should match exactly
7. **Cost model**: Verify entry/exit notifications show brokerage, STT, slippage deductions
8. **Resource check**: `systemctl status stockanalysis-paper-trading` — confirm < 150M RAM, 0 restarts, no errors in journalctl
9. **Stale data**: Disconnect market-data WS mid-day → verify MTM engine skips cycles and logs warnings (no false exits)
10. **Crash recovery**: Kill paper-trading service mid-position → restart → verify all open positions reload from Redis and MTM resumes
