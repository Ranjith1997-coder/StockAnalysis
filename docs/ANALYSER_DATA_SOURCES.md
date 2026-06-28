# Analyser Data Sources & Method Reference

> **Purpose:** Complete reference of every data source in the system, every batch analyser, and every analyser method — documenting exactly which data each method reads. Used to track microservice migration coverage (which Redis hashes feed which analysers).

---

## Table of Contents

1. [Data Sources Overview](#1-data-sources-overview)
2. [Redis Hash Schema](#2-redis-hash-schema)
3. [Stock Object Attributes](#3-stock-object-attributes)
4. [TickStore Live Data Structures](#4-tickstore-live-data-structures)
5. [Sensibull Live (WebSocket) vs Sensibull Web (HTTP)](#5-sensibull-live-websocket-vs-sensibull-web-http)
   - [5.1 Zerodha WebSocket (Dual Connection)](#51-zerodha-websocket-dual-connection)
   - [5.2 Zerodha Futures HTTP (Web-based)](#52-zerodha-futures-http-web-based)
   - [5.3 Positional Sources — Gaps](#53-positional-sources--gaps)
6. [Analyser Registration Order](#6-analyser-registration-order)
7. [Per-Analyser Method Reference](#7-per-analyser-method-reference)
   - [7.1 VolumeAnalyser](#71-volumeanalyser)
   - [7.2 TechnicalAnalyser](#72-technicalanalyser)
   - [7.3 CandleStickAnalyser](#73-candlestickanalyser)
   - [7.4 IVAnalyser](#74-ivanalyser)
   - [7.5 FuturesAnalyser](#75-futuresanalyser)
   - [7.6 PCRAnalyser](#76-pcranalyser)
   - [7.7 MaxPainAnalyser](#77-maxpainanalyser)
   - [7.8 OIChainAnalyser](#78-oichainanalyser)
   - [7.9 GEXAnalyser](#79-gexanalyser)
   - [7.10 PanicModeAnalyser](#710-panicmodeanalyser)
   - [7.11 OptionSellerCompositeAnalyser](#711-optionsellercompositeanalyser)
   - [7.12 Mode-Dependent Data Source Summary](#712-mode-dependent-data-source-summary)
8. [Cross-Reference: Data Source → Analysers](#8-cross-reference-data-source--analysers)
9. [Migration Coverage Matrix](#9-migration-coverage-matrix)

---

## 1. Data Sources Overview

The system has **five** external data sources (Zerodha has two separate subsystems: WS + HTTP), plus one internal composite layer:

| Source | Type | Auth | Transport | Update Frequency | Owner (microservice) |
|--------|------|------|-----------|-----------------|---------------------|
| **yfinance** | Price/OHLCV | None | HTTP REST | Per cycle (5-min intraday / 2yr daily positional) | data-gateway |
| **Sensibull Web (HTTP)** | OI chain, IV, PCR, max pain, per-cycle history | None | HTTP REST (poll) | Per cycle (~5 min) | data-gateway |
| **Sensibull Live (WS)** | Per-strike greeks (gamma/delta/theta/vega/iv), live OI, LTP | None | WebSocket (push) | Real-time | monolith |
| **Zerodha WS** | Live equity/index ticks, live option ticks (NO greeks, NO futures) | Enctoken (TOTP) | WebSocket (push) | Real-time | monolith |
| **Zerodha HTTP** | Futures OHLC+OI (historical) | Enctoken (TOTP) | HTTP REST (poll) | Per cycle (5-min intraday / 90d daily positional) | data-gateway |
| **stock.analysis (composite)** | Signals emitted by upstream analysers | N/A | In-memory | Per cycle | analysis-engine |

> **Note:** Zerodha WS and Zerodha HTTP are separate subsystems with different transports, owners, and data. The WS provides live ticks (equity/index/option) but NO futures and NO greeks. The HTTP provides historical futures data only. See [Section 5.1](#51-zerodha-websocket-dual-connection) and [Section 5.2](#52-zerodha-futures-http-web-based).

### What Each Source Provides

| Data | yfinance | Sensibull Web (HTTP) | Sensibull Live (WS) | Zerodha WS | Zerodha HTTP |
|------|----------|---------------------|---------------------|------------|--------------|
| Daily OHLCV price | ✅ | — | — | — | — |
| Intraday 5-min OHLCV | ✅ | — | — | — | — |
| Previous day OHLCV | ✅ | — | — | — | — |
| Daily HV (historical vol) | ✅ | — | — | — | — |
| LTP / LTP change % | ✅ | — | — | ✅ (live tick) | — |
| Per-expiry ATM IV / IVP / IV rank | — | ✅ | ✅ (aggregate) | — | — |
| Per-expiry max pain strike | — | ✅ | ✅ (aggregate) | — | — |
| Per-expiry PCR / total PCR | — | ✅ | ✅ (aggregate) | — | — |
| Per-strike OI (call/put) | — | ✅ | ✅ | ✅ | — |
| Per-strike prev OI | — | ✅ | ✅ (derived) | ✅ | — |
| **Per-strike gamma** | — | **❌** | ✅ | **❌** | — |
| **Per-strike delta/theta/vega** | — | **❌** | ✅ | **❌** | — |
| **Per-strike IV** | — | **❌** (only aggregate per-expiry) | ✅ | **❌** | — |
| Per-strike LTP / volume | — | — | ✅ | ✅ | — |
| Bid/ask depth | — | — | ❌ (price proxy) | ✅ | — |
| Futures OHLC + OI (historical, intraday) | — | — | — | — | ✅ (5min, today, current expiry) |
| Futures OHLC + OI (historical, positional) | — | — | — | — | ✅ (daily, 90d, both expiries) |
| Futures live tick (LTP/OI/volume) | — | — | — | ⚠️ dead code¹ | — |
| India VIX LTP | ✅ (via price data) | — | — | — | — |
| **2yr daily IV history (positional)** | — | ⚠️ endpoint² (dead code) | — | — | — |
| **~181 daily OI/PCR history (positional)** | — | ⚠️ endpoint² (dead code) | — | — | — |

**Key insights:**
- Sensibull Live (WS) is the **only source of per-strike greeks** (gamma, delta, theta, vega, IV). Without it, GEXAnalyser cannot function. Sensibull Web (HTTP) provides OI but no greeks.
- ¹ Zerodha WS has `_process_future_tick` + `futures_live` in TickStore, but **futures tokens are never registered or subscribed** on any WS connection — this is dead code. All futures data comes via REST only.
- ² Sensibull HTTP has endpoints for `iv_chart_history` and `oi_history`, and the monolith has `fetch_iv_chart()` / `fetch_oi_history()` methods, but they have **zero callers** (dead code). The data-gateway serializes empty DataFrames to Redis. These positional sources are **never fetched** — 5 positional analyser methods silently skip. See [Section 5.3](#53-positional-sources--gaps).

---

## 2. Redis Hash Schema

### `data:price:{symbol}` — published by data-gateway (yfinance)

| Field | Type | Description |
|-------|------|-------------|
| `priceData_json` | JSON (split-orient DataFrame) | OHLCV DataFrame: Open, High, Low, Close, Volume |
| `prevDayOHLCV_json` | JSON dict | `{OPEN, HIGH, LOW, CLOSE, VOLUME}` from previous day |
| `ltp` | string (float) | Last traded price |
| `ltp_change_perc` | string (float) | LTP change percentage |
| `daily_hv` | string (float) | Annualized daily historical volatility (%) |
| `last_price_update` | string (timestamp) | Last update timestamp |

### `data:sensibull:{symbol}` — published by data-gateway (Sensibull HTTP)

| Field | Type | Description |
|-------|------|-------------|
| `last_fetch_time` | string | Timestamp of last fetch |
| `current_json` | JSON dict | `{underlying_info, stats{underlying_base_stats, per_expiry_map, nse_stats}, per_expiry_map, nse_stats}` |
| `historical_data_json` | JSON (split-orient DataFrame) | Per-cycle rows with per-expiry columns (`atm_iv_{exp}`, `max_pain_{exp}`, `future_price_{exp}`, `pcr_{exp}`, etc.) |
| `oi_chain_json` | JSON dict or `"null"` | Latest OI chain snapshot (per-strike `call_oi`/`put_oi`/`prev_call_oi`/`prev_put_oi` + meta) |
| `oi_chain_history_json` | JSON list | List of OI chain snapshots (max 15 intraday) |
| `iv_chart_history_json` | JSON (split-orient DataFrame) | Daily IV close history (2yr) |
| `oi_history_json` | JSON (split-orient DataFrame) | Daily OI history (~181 rows): `call_oi, put_oi, futures_oi, call_oi_change, put_oi_change, pcr, max_pain, spot, date` |

### `data:zerodha:{symbol}` — published by data-gateway (Zerodha HTTP)

| Field | Type | Description |
|-------|------|-------------|
| `futures_data_current_json` | JSON (split-orient DataFrame) | Current-expiry futures: `open, high, low, close, volume, oi, underlying_price` |
| `futures_data_next_json` | JSON (split-orient DataFrame) | Next-expiry futures (same schema; empty in intraday mode) |
| `futures_mdata_json` | JSON dict | `{"current": [{instrument_token, tradingsymbol, expiry}], "next": [...]}` |

### NOT YET in Redis (live tick data — migration gap)

| Planned hash | Source | Fields |
|-------------|--------|--------|
| `data:options_live:{symbol}` | monolith TickStore (Sensibull WS) | Per-strike `{CE/PE: {gamma, delta, theta, vega, iv, oi, prev_oi, ltp, volume}}` |
| `data:options_agg:{symbol}` | monolith TickStore | `{gex_total, gex_ce, gex_pe, gex_regime, gex_flip_level, gex_by_strike, live_pcr, atm_strike, ...}` |
| `data:tick:{symbol}` (optional) | monolith TickStore (Zerodha WS) | `{total_buy_quantity, total_sell_quantity, last_price, ohlc, volume_traded}` |

---

## 3. Stock Object Attributes

The `Stock` object is reconstructed in the analysis-engine worker via `stock_loader.py`:

### Populated from Redis (via loaders)

| Attribute | Source | Loader | Redis Hash |
|-----------|--------|--------|------------|
| `stock.priceData` | yfinance | `load_stock_from_redis` | `data:price:*` |
| `stock.prevDayOHLCV` | yfinance | `load_stock_from_redis` | `data:price:*` |
| `stock.ltp` | yfinance | `load_stock_from_redis` | `data:price:*` |
| `stock.ltp_change_perc` | yfinance | `load_stock_from_redis` | `data:price:*` |
| `stock.daily_hv` | yfinance | `load_stock_from_redis` | `data:price:*` |
| `stock.sensibull_ctx` | Sensibull HTTP | `load_sensibull_from_redis` | `data:sensibull:*` |
| `stock.zerodha_ctx` | Zerodha HTTP | `load_zerodha_from_redis` | `data:zerodha:*` |

### NOT populated from Redis (remain empty in worker — migration gap)

| Attribute | Source | Current Owner |
|-----------|--------|---------------|
| `stock.options_live` | Sensibull WS / Zerodha WS | monolith TickStore (in-memory only) |
| `stock.options_aggregate` | TickStore recompute + GEXAnalyser | monolith TickStore (in-memory only) |
| `stock.zerodha_data` | Zerodha WS (equity tick) | monolith TickStore (in-memory only) |
| `stock.futures_live` | Zerodha WS (futures tick) | monolith TickStore (in-memory only) |

### Derived attributes (computed from priceData)

| Attribute | Derivation |
|-----------|------------|
| `stock.current_equity_data` | Last row of `priceData` (mode-aware) |
| `stock.previous_equity_data` | Second-to-last row of `priceData` |
| `stock.previous_previous_equity_data` | Third-to-last row of `priceData` |
| `stock.is_index` | Boolean flag (set during Stock construction) |

### External context (not per-stock)

| Attribute | Source | Used by |
|-----------|--------|---------|
| `shared.app_ctx.mode` | Config (INTRADAY/POSITIONAL) | All analysers (decorator dispatch) |
| `shared.app_ctx.india_vix_ltp` | App context (from VIX price data) | PanicModeAnalyser (adaptive threshold) |

---

## 4. TickStore Live Data Structures

`zerodha/tick_store.py` — thread-safe container for live WebSocket data. **Only populated by WS feeds in the monolith.**

### `options_live` — per-strike option ticks

```python
options_live: {
    24000.0: {
        "CE": {
            "prev_oi": int,           # previous OI (tracked across snapshots)
            "ltp": float,             # last traded price
            "oi": int,                # current open interest
            "volume": int,            # volume traded
            "buy_qty": int,           # total buy quantity
            "sell_qty": int,          # total sell quantity
            "timestamp": datetime,
            "delta": float,           # ⚠️ ONLY if Sensibull WS (Zerodha never writes)
            "gamma": float,           # ⚠️ ONLY if Sensibull WS (default 0.0)
            "theta": float,           # ⚠️ ONLY if Sensibull WS (default 0.0)
            "vega": float,            # ⚠️ ONLY if Sensibull WS (default 0.0)
            "iv": float,              # ⚠️ ONLY if Sensibull WS (default 0.0)
            "iv_change": float,       # ⚠️ ONLY if Sensibull WS (default 0.0)
            "open/high/low/close": float,  # if ohlc in tick
            "depth": dict,            # if depth in tick (Zerodha only)
        },
        "PE": { ... same structure ... },
    },
    ...
}
```

**Greeks trigger key:** `if "delta" in tick` — only Sensibull WS ticks contain `delta`. Zerodha WS ticks never trigger greeks writing.

### `options_aggregate` — computed aggregate state

```python
options_aggregate: {
    "total_ce_oi": int,            # sum of all CE oi
    "total_pe_oi": int,            # sum of all PE oi
    "live_pcr": float,             # total_pe_oi / total_ce_oi
    "atm_strike": float|None,      # strike nearest to spot
    "atm_straddle_premium": float, # CE.ltp + PE.ltp at ATM
    "atm_iv_ce": float,            # ⚠️ Sensibull only: nearest OTM call IV
    "atm_iv_pe": float,            # ⚠️ Sensibull only: nearest OTM put IV
    "iv_skew": float,              # ⚠️ Sensibull only: (pe_iv - ce_iv) * 100
    "max_oi_ce_strike": float|None,
    "max_oi_pe_strike": float|None,
    "net_ce_oi_change": int,
    "net_pe_oi_change": int,
    "last_updated": float,
    # Sensibull-only aggregate fields:
    "atm_iv": float,
    "atm_iv_percentile": float,
    "atm_ivp_type": str|None,
    "max_pain_strike": float|None,
    "future_price": float,
    # GEX fields (written by GEXAnalyser):
    "gex_total": float,            # net GEX in ₹ crores
    "gex_ce": float,
    "gex_pe": float,
    "gex_regime": str|None,        # "POSITIVE" / "NEGATIVE"
    "gex_flip_level": float|None,  # strike where GEX crosses zero
    "gex_by_strike": dict,         # {strike: net_gex}
}
```

### `zerodha_data` — live equity/index tick

```python
zerodha_data: {
    "volume_traded": int,
    "last_price": float,
    "open": float, "high": float, "low": float, "close": float,
    "change": float,
    "average_traded_price": float,
    "total_buy_quantity": int,     # used by TechnicalAnalyser.buy_sell_quantity
    "total_sell_quantity": int,    # used by TechnicalAnalyser.buy_sell_quantity
}
```

### `futures_live` — live futures tick

```python
futures_live: {
    "current": { "prev_oi", "ltp", "oi", "volume", "buy_qty", "sell_qty", "change", "timestamp", "open/high/low/close" },
    "next": { ... same ... },
}
```

---

## 5. Sensibull Live (WebSocket) vs Sensibull Web (HTTP)

### Sensibull Live (WebSocket)

| Aspect | Detail |
|--------|--------|
| **File** | `fno/sensibull_feed.py` (WS client), `fno/sensibull_adapter.py` (translator) |
| **URL** | `wss://wsrelay.sensibull.com/broker/1?consumerType=platform_no_plan` |
| **Auth** | None |
| **Origin** | `https://web.sensibull.com` |
| **Subscription** | `dataSource: "option-chain"`, `underlyingExpiry: [{underlying: <NSE_token>, expiry: "YYYY-MM-DD"}]` |
| **Frame format** | Binary: `[1-byte type][4-byte BE token][8-byte ASCII expiry "YYYYMMDD"][gzip JSON]` |
| **Update frequency** | Real-time push (full chain snapshot per frame) |
| **Applies to** | NIFTY, BANKNIFTY, SENSEX only (`LIVE_OPTIONS_INDICES`) |

**Decoded snapshot structure:**
```python
{
    "chain": {
        "<strike_str>": {
            "greeks": { "call_delta", "gamma", "theta", "vega", "iv" },  # per-strike
            "iv_change": float,
            "call": { "last_price", "oi", "volume", "oi_change_quantity", "best_buy_price", "best_sell_price" },
            "put":  { ... same ... },
        },
    },
    "future_price": float,
    "atm_iv": float, "atm_iv_percentile": float, "atm_ivp_type": str|None,
    "max_pain_strike": float|None,
}
```

**What the adapter writes to TickStore:**
- `options_live[strike][CE/PE]`: ltp, oi, volume, buy_qty, sell_qty, delta, gamma, theta, vega, iv, iv_change
- `options_aggregate`: atm_iv, atm_iv_percentile, atm_ivp_type, max_pain_strike, future_price, atm_iv_ce, atm_iv_pe, iv_skew

**Two modes (`OPTIONS_SOURCE` env):**
| Mode | Behavior |
|------|----------|
| `sensibull` | Full tick: writes ltp/oi/volume + greeks; recomputes aggregate; triggers LiveOptionsEngine |
| `both` | Enrichment-only: writes greeks via `merge=True` onto Zerodha-subscribed strikes; does NOT recompute aggregate or trigger engine. Zerodha remains authoritative for OI/LTP/depth |

### Sensibull Web (HTTP)

| Aspect | Detail |
|--------|--------|
| **File** | `fno/sensibull_fetcher.py` (monolith), `services/data_gateway/sensibull_fetcher.py` (gateway) |
| **Endpoints** | `GET oxide.sensibull.com/v1/compute/cache/insights/stock_info`, `POST .../1/oi_graphs/oi_chart`, `GET .../iv_chart/{symbol}`, `POST .../compute_intraday` |
| **Auth** | None |
| **Update frequency** | Poll per cycle (~5 min intraday) |
| **Applies to** | All symbols (stocks + indices) |
| **Writes to** | `stock.sensibull_ctx` / Redis `data:sensibull:{symbol}` |
| **Touches TickStore?** | ❌ No — completely separate from `options_live`/`options_aggregate` |

**`oi_chain.per_strike_data` structure (HTTP):**
```python
{
    "<strike>": {
        "call_oi": int,
        "put_oi": int,
        "prev_call_oi": int,
        "prev_put_oi": int,
        # NO greeks, NO delta, NO gamma, NO iv per strike
    },
}
```

### Comparison for GEX

| Requirement | Sensibull Live (WS) | Sensibull Web (HTTP) |
|-------------|--------------------|--------------------|
| Per-strike gamma | ✅ | ❌ |
| Per-strike delta/theta/vega | ✅ | ❌ |
| Per-strike IV | ✅ | ❌ (only aggregate per-expiry `atm_iv`) |
| Per-strike OI | ✅ | ✅ |
| Writes `options_live` | ✅ | ❌ (writes `sensibull_ctx` only) |
| Writes `options_aggregate` | ✅ | ❌ |
| Real-time | ✅ push | ❌ poll |
| Auth required | ❌ | ❌ |

**Conclusion:** Only Sensibull Live (WS) can serve GEXAnalyser. Sensibull Web (HTTP) correctly serves OIChainAnalyser, PCRAnalyser, MaxPainAnalyser, IVAnalyser.

---

### 5.1 Zerodha WebSocket (Dual Connection)

**Files:** `zerodha/zerodha_ticker.py` (custom KiteTicker WS client), `zerodha/zerodha_analysis.py` (`ZerodhaTickerManager`), `zerodha/tick_store.py` (TickStore), `common/token_registry.py` (TokenRegistry)

The monolith opens **two separate WS connections** to `wss://ws.zerodha.com` using enctoken auth:

| Connection | Instruments | Mode | When Subscribed |
|-----------|-------------|------|-----------------|
| **WS1 (`_kt_base`)** | Equities, indices, commodities, global indices (~215 tokens) | `MODE_FULL` | On `on_connect_base` at startup |
| **WS2 (`_kt_options`)** | Option tokens (CE/PE) for NIFTY/BANKNIFTY/SENSEX only | Mixed per zone | After index ticks deliver spot prices |

**Option zone modes** (dynamic re-centering as spot moves):
| Zone | Distance from ATM | Mode |
|------|------------------|------|
| CORE | ±1% | `MODE_FULL` (depth, ohlc) |
| ACTIVE | 1-3% | `MODE_FULL` |
| PERIPHERAL | 3-5% | `MODE_QUOTE` (no depth) |

Re-centering: when spot moves ≥2 strikes, peripheral strikes are unsubscribed and new strikes subscribed.

**Tick routing by `TokenInfo.token_type`:**

| Tick type | Routes to | Fields written | Notes |
|-----------|----------|---------------|-------|
| Equity/Index | `TickStore.zerodha_data` | last_price, change, ohlc, volume_traded, total_buy_quantity, total_sell_quantity, average_traded_price | Index ticks (28/32-byte) lack volume/qty fields |
| Option | `TickStore.options_live[strike][CE/PE]` | ltp, oi, prev_oi, volume, buy_qty, sell_qty, ohlc, depth (MODE_FULL only) | **NO greeks** — Zerodha WS never provides gamma/delta/theta/vega/iv |
| Future | `TickStore.futures_live[expiry]` | ltp, oi, prev_oi, volume, buy_qty, sell_qty, change, ohlc | ⚠️ **DEAD CODE** — futures tokens never registered/subscribed |

**Critical finding — Futures NOT on WS:** `TokenRegistry` defines `TokenType.FUTURE` and `_process_future_tick` exists in `zerodha_analysis.py`, but futures tokens are **never registered** in the registry and **never subscribed** on WS1 or WS2. The `_process_future_tick` branch is unreachable. All futures data comes via REST only (`kc.historical_data()`). `TickStore.futures_live` is wired but never populated.

### 5.2 Zerodha Futures HTTP (Web-based)

**Files:** `zerodha/futures_fetcher.py` (monolith, orphaned), `services/data_gateway/zerodha_fetcher.py` (data-gateway, active)

Uses `kc.historical_data()` (requires enctoken, rate-limited ~3 calls/s):

| Mode | Interval | Lookback | Expiries | Next expiry? |
|------|----------|----------|----------|-------------|
| Intraday | `5minute` | Today only | Current only | No |
| Positional | `day` | 90 days | Both current + next | Yes |

Published to Redis `data:zerodha:{symbol}` as `futures_data_current_json`, `futures_data_next_json`, `futures_mdata_json`. ✅ Correctly migrated to data-gateway.

### 5.3 Positional Sources — Gaps

Two Sensibull HTTP endpoints provide positional-specific daily history data. The monolith has fetcher methods but they have **zero callers** (dead code). The data-gateway serializes empty DataFrames to Redis. This means multiple positional analyser methods **silently skip on every run**.

#### Gap 1: `iv_chart_history` (2yr daily IV)

| Aspect | Detail |
|--------|--------|
| **Endpoint** | `GET oxide.sensibull.com/v1/compute/iv_chart/{symbol}` |
| **Monolith method** | `fno/sensibull_fetcher.py:fetch_iv_chart()` — defined, **0 callers (dead code)** |
| **Data-gateway** | ❌ Not implemented — serializes empty DataFrame to `iv_chart_history_json` |
| **Intended fetch frequency** | Once per day (positional) |
| **Redis plumbing** | ✅ `iv_chart_history_json` serialize/deserialize exists in `stock_loader.py` |
| **Data structure** | DataFrame: `date, iv_close, price_close` (~2yr daily ATM IV closes, already in %) |
| **Consumers** | `IVAnalyser.analyse_trend_in_ATM_IV` (positional path — prefers `iv_chart_history` over `historical_data` fallback) |
| **Impact** | IVAnalyser positional IV trend falls back to `historical_data` (5-min intraday bars), which has different semantics. The intended daily-IV trend analysis is broken. |

#### Gap 2: `oi_history` (~181 daily OI/PCR)

| Aspect | Detail |
|--------|--------|
| **Endpoint** | `POST oxide.sensibull.com/v1/compute/compute_intraday` with `interval="1D"` |
| **Monolith method** | `fno/sensibull_fetcher.py:fetch_oi_history()` — defined, **0 callers (dead code)** |
| **Data-gateway** | ❌ Not implemented — serializes empty DataFrame to `oi_history_json` |
| **Intended fetch frequency** | Once per day (positional) |
| **Redis plumbing** | ✅ `oi_history_json` serialize/deserialize exists in `stock_loader.py` |
| **Data structure** | DataFrame: `date, spot, call_oi, put_oi, futures_oi, call_oi_change, put_oi_change, future_oi_change, pcr, max_pain` (~181 daily rows) |
| **Consumers** | `PCRAnalyser.analyse_pcr_trend`, `analyse_pcr_positional_reversal`; `MaxPainAnalyser.analyse_max_pain_trend` (positional); `OIChainAnalyser.analyse_positional_oi_trend`, `analyse_oi_acceleration` |
| **Impact** | **5 positional methods across 3 analysers silently skip on every run.** PCR trend, PCR positional reversal, max pain trend (positional), OI positional trend, and OI acceleration all hit `oi_history is None or empty → return False`. |

#### Positional Sources That ARE Covered

| Source | What it provides | When fetched | Gateway? | Consumers |
|--------|------------------|--------------|----------|-----------|
| Zerodha futures REST (positional) | Daily OHLC+OI, 90-day, both expiries | Every positional cycle | ✅ | FuturesAnalyser (positional_oi_trend, cost_of_carry, rollover_pressure) |
| Zerodha futures metadata (both expiries) | instrument_token, tradingsymbol, expiry | Once at startup | ✅ | FuturesAnalyser (expiry/days_to_expiry) |
| yfinance (positional cycle) | 2yr daily OHLCV | Every positional cycle | ✅ | Positional analysers using priceData |
| yfinance (positional initial) | 5D daily (prevDayOHLCV, daily_hv) | Gateway startup | ✅ | prevDayOHLCV, daily_hv |
| Sensibull insights + OI chain (positional) | current snapshot, historical_data (tail 30), oi_chain | Every positional cycle | ✅ | IVAnalyser, PCRAnalyser, MaxPainAnalyser, OIChainAnalyser |

#### Morning Bias

`compute_morning_bias()` / `update_morning_bias()` in `intraday/intraday_monitor.py` is **not a data source** — it is an analysis pass that re-runs positional analysers on daily `priceData` at intraday startup. It loads `sensibull_ctx` from Redis, which inherits the `iv_chart_history`/`oi_history` gaps above. No separate fetcher exists.

---

## 6. Analyser Registration Order

The 11 batch analysers are registered in this order (order matters for composite analysers):

| # | Analyser | Class | File | Type |
|---|----------|-------|------|------|
| 1 | VolumeAnalyser | `VolumeAnalyser` | `analyser/VolumeAnalyser.py` | Source (price) |
| 2 | TechnicalAnalyser | `TechnicalAnalyser` | `analyser/TechnicalAnalyser.py` | Source (price + live tick) |
| 3 | CandleStickAnalyser | `CandleStickAnalyser` | `analyser/candleStickPatternAnalyser.py` | Source (price) |
| 4 | IVAnalyser | `IVAnalyser` | `analyser/IVAnalyser.py` | Source (Sensibull HTTP + price) |
| 5 | FuturesAnalyser | `FuturesAnalyser` | `analyser/Futures_Analyser.py` | Source (Zerodha HTTP) |
| 6 | PCRAnalyser | `PCRAnalyser` | `analyser/PCRAnalyser.py` | Source (Sensibull HTTP) |
| 7 | MaxPainAnalyser | `MaxPainAnalyser` | `analyser/MaxPainAnalyser.py` | Source (Sensibull HTTP + price) |
| 8 | OIChainAnalyser | `OIChainAnalyser` | `analyser/OIChainAnalyser.py` | Source (Sensibull HTTP) |
| 9 | GEXAnalyser | `GEXAnalyser` | `analyser/GEXAnalyser.py` | Source (Sensibull WS / live tick) |
| 10 | PanicModeAnalyser | `PanicModeAnalyser` | `analyser/PanicModeAnalyser.py` | Composite (reads stock.analysis) |
| 11 | OptionSellerCompositeAnalyser | `OptionSellerCompositeAnalyser` | `analyser/OptionSellerCompositeAnalyser.py` | Composite (reads stock.analysis) |

**Order constraint:** PanicModeAnalyser (#10) MUST run after #1-9 (reads their signals). OptionSellerCompositeAnalyser (#11) MUST run after #10 (reads `PANIC_EXHAUSTION`).

### Decorator System

| Decorator | Runs in | Asset class |
|-----------|---------|-------------|
| `@intraday` | Intraday mode only | Stocks |
| `@positional` | Positional mode only | Stocks |
| `@both` | Both modes | Stocks |
| `@index_intraday` | Intraday mode only | Indices |
| `@index_positional` | Positional mode only | Indices |
| `@index_both` | Both modes | Indices |

Most analysers stack `@both` + `@index_both` (run in all 4 combinations). Some use `@intraday` + `@index_intraday` or `@positional` + `@index_positional`.

---

## 7. Per-Analyser Method Reference

### 7.1 VolumeAnalyser

**File:** `analyser/VolumeAnalyser.py`
**Data sources:** `stock.priceData` only (`data:price:*`)
**Mode difference:** Intraday reads 5-min bars (last 5 days); positional reads daily bars (2yr). Same columns, different timeframe — affects lookback window lengths and threshold constants (`reset_constants` adjusts per mode).

| Method | Decorators | Intraday Data Sources | Positional Data Sources | Guard/Skip Conditions |
|--------|-----------|-----------------|----------------------|
| `analyse_volume_breakout` | `@both` | `priceData['Volume']`, `priceData['Close']` (5-min) | `priceData['Volume']`, `priceData['Close']` (daily) | `len < VOLUME_MA_PERIOD+5`; requires vol above MA + above prev + price change > threshold + rising vol trend |
| `analyse_obv_divergence` | `@both` | `priceData['Close']` (5-min, OBV from Close+Volume) | `priceData['Close']` (daily, OBV from Close+Volume) | `len < OBV_LOOKBACK+20`; requires ≥2 swing highs/lows with HH/LH or LL/HL divergence |
| `analyse_volume_climax` | `@both` | `priceData['Close']`, `priceData['High']`, `priceData['Low']`, `priceData['Volume']` (5-min) | `priceData['Close']`, `priceData['High']`, `priceData['Low']`, `priceData['Volume']` (daily) | `len < CLIMAX_LOOKBACK+20`; `vol_ratio < CLIMAX_VOLUME_MULT`; buying climax (close position <0.3) / selling climax (>0.7) |

**Redis coverage:** ✅ Fully covered by `data:price:*`

---

### 7.2 TechnicalAnalyser

**File:** `analyser/TechnicalAnalyser.py`
**Data sources:** `stock.priceData`, `stock.prevDayOHLCV`, `stock.zerodha_data` (live tick, one method only)

| Method | Decorators | Intraday Data Sources | Positional Data Sources | Guard/Skip Conditions |
|--------|-----------|----------------------|------------------------|----------------------|
| `analyse_rsi` | `@both`, `@index_both` | `priceData['Close']` (last 100 bars, 5-min) | `priceData['Close']` (daily, 1-2yr) | `len < 100`; zone (overbought/oversold) or crossover |
| `analyse_Bolinger_band` | `@both`, `@index_both` | `priceData['Close']` (5-min); `current_equity_data['Close']` | `priceData['Close']` (daily); `current_equity_data['Close']` | `len < BB_WINDOW`; requires 2 consecutive candles outside band + trend filter |
| `analyse_is_52_week` | `@positional`, `@index_positional` | N/A (positional only) | `stock.check_52_week_status()` (indirect: priceData High/Low) | Positional only; always returns False (sets 52w high/low flag) |
| `analyse_vwap` | `@intraday` | `priceData['High','Low','Close','Volume']` (today's 5-min rows only, filtered by `.index.date == today`) | N/A (intraday only) | Intraday only; deviation > threshold + days above/below |
| `analyze_macd` | `@both`, `@index_both` | `priceData['Close']` (5-min) | `priceData['Close']` (daily) | Bullish/bearish crossover + histogram monotonic + strength |
| `analyse_atr` | `@both`, `@index_both` | `priceData['High']`, `priceData['Low']`, `priceData['Close']` (5-min) | `priceData['High']`, `priceData['Low']`, `priceData['Close']` (daily) | High vol + expanding ATR |
| `analyze_buy_sell_quantity` | `@intraday` | **`zerodha_data['total_buy_quantity']`**, **`zerodha_data['total_sell_quantity']`** (live Zerodha WS tick) | N/A (intraday only) | Intraday only; buy > ratio×sell (BULLISH) or sell > ratio×buy (BEARISH) |
| `analyse_ema_crossover` | `@both`, `@index_both` | `priceData['Close']` (5-min, fast/slow EMA + ADX from High/Low/Close) | `priceData['Close']` (daily, fast/slow EMA + ADX from High/Low/Close) | `len < max(FAST,SLOW)+3`; ADX gate `< ADX_TREND_THRESHOLD`; fast/slow crossover + slope |
| `analyse_supertrend` | `@both`, `@index_both` | `priceData['High']`, `priceData['Low']`, `priceData['Close']` (5-min) | `priceData['High']`, `priceData['Low']`, `priceData['Close']` (daily) | `len < SUPERTREND_PERIOD+2`; direction flip |
| `analyse_rsi_divergence` | `@both`, `@index_both` | `priceData['Close']` (5-min, last 50 bars) | `priceData['Close']` (daily, last 50 bars) | `len < 51`; ≥2 swing highs/lows; RSI HH/LH or LL/HL + trend weakening |
| `analyse_stochastic` | `@both`, `@index_both` | `priceData['High']`, `priceData['Low']`, `priceData['Close']` (5-min) | `priceData['High']`, `priceData['Low']`, `priceData['Close']` (daily) | `len < K+D+1`; K/D crossover at extreme zones |
| `analyse_pivot_points` | `@both`, `@index_both` | `prevDayOHLCV['HIGH/LOW/CLOSE']` (previous calendar day); `current_equity_data['Close']`, `previous_equity_data['Close']` | `previous_equity_data['High/Low/Close']` (previous daily bar); `current_equity_data['Close']`, `previous_equity_data['Close']` | Intraday: `prevDayOHLCV is None`; Positional: `len < 3`; min 0.3% breakout through R2/R1/PP or S2/S1/PP |

**Redis coverage:** ✅ 10/11 methods covered by `data:price:*`. ⚠️ `analyze_buy_sell_quantity` reads `zerodha_data` (live Zerodha WS tick) — NOT in Redis. Silently returns False in analysis-engine worker.

---

### 7.3 CandleStickAnalyser

**File:** `analyser/candleStickPatternAnalyser.py`
**Data sources:** `stock.priceData`, `stock.current_equity_data`, `stock.previous_equity_data`, `stock.previous_previous_equity_data`
**Mode difference:** All methods use `@both` so same columns. Trend context (`_get_trend_context`) slices `priceData.iloc[start_idx:end_idx]` where `end_idx=-3` (intraday) vs `end_idx=-2` (positional) — i.e. intraday excludes the last 2 bars for trend context, positional excludes the last 1 bar.

| Method | Decorators | Intraday Data Sources | Positional Data Sources | Guard/Skip Conditions |
|--------|-----------|-----------------|----------------------|
| `singleCandleStickPattern` | `@both`, `@index_both` | `current_equity_data['Open/High/Low/Close']` (5-min bar) | `current_equity_data['Open/High/Low/Close']` (daily bar) | Marubozu pattern (bullish/bearish) |
| `singleCandleReversalPattern` | `@both`, `@index_both` | `current_equity_data['Open/High/Low/Close']` (5-min); trend context from `priceData['Close/Open'].iloc[:-3]` | `current_equity_data['Open/High/Low/Close']` (daily); trend context from `priceData['Close/Open'].iloc[:-2]` | Hammer (requires downtrend context) / Shooting Star (requires uptrend context) |
| `doubleCandleStickPattern` | `@both`, `@index_both` | `current_equity_data`, `previous_equity_data` (5-min bars); trend context from `priceData.iloc[:-3]` | `current_equity_data`, `previous_equity_data` (daily bars); trend context from `priceData.iloc[:-2]` | Bullish/Bearish Engulfing, Piercing Line, Dark Cloud Cover (reversal patterns require trend context) |
| `doubleCandleStickContinuationPattern` | `@both`, `@index_both` | `current_equity_data['Open/Close']`, `previous_equity_data['Open/Close']` (5-min) | `current_equity_data['Open/Close']`, `previous_equity_data['Open/Close']` (daily) | 2 continuous increase/decrease (no trend context needed) |
| `tripleCandleStickReversalPattern` | `@both`, `@index_both` | `current_equity_data`, `previous_equity_data`, `previous_previous_equity_data` (5-min); trend context from `priceData.iloc[:-3]` | `current_equity_data`, `previous_equity_data`, `previous_previous_equity_data` (daily); trend context from `priceData.iloc[:-2]` | Morning Star / Evening Star (requires trend context) |
| `tripleCandleStickContinuationPattern` | `@both`, `@index_both` | `current_equity_data`, `previous_equity_data`, `previous_previous_equity_data` (5-min) | `current_equity_data`, `previous_equity_data`, `previous_previous_equity_data` (daily) | 3 continuous increase/decrease (no trend context needed) |

**Redis coverage:** ✅ Fully covered by `data:price:*` (equity_data props are derived from priceData)

---

### 7.4 IVAnalyser

**File:** `analyser/IVAnalyser.py`
**Data sources:** `stock.sensibull_ctx` (Sensibull HTTP), `stock.daily_hv`, `stock.priceData`

| Method | Decorators | Intraday Data Sources | Positional Data Sources | Guard/Skip Conditions |
|--------|-----------|----------------------|------------------------|----------------------|
| `analyse_spike_in_ATM_IV` | `@both`, `@index_both` | `sensibull_ctx['historical_data']` (col `atm_iv_{exp}`, `.iloc[-2]`/`.iloc[-1]`); stocks: nearest expiry only, indices: all expiries | `sensibull_ctx['current']['stats']['per_expiry_map']` (atm_iv, atm_iv_change — normalized from decimal if <1.0) | Empty per_expiry_map; `prev_iv == 0`; `iv_change_pct < IV_PERCENTAGE_CHANGE` |
| `analyse_trend_in_ATM_IV` | `@both`, `@index_both` | `sensibull_ctx['historical_data']` (col `atm_iv_{exp}`, `.iloc[-n:]`); source_tag=`historical_data_5min` | `sensibull_ctx['iv_chart_history']` (col `iv_close`, daily — ⚠️ **NOT FETCHED**); fallback: `historical_data` (col `atm_iv_{exp}`); source_tag=`iv_chart_daily` or `historical_data(fallback)` | `iv_col is None`; `len < IV_TREND_CONTINUATION_DAYS`; `ivs[0] == 0`; monotonic + `pct >= IV_TREND_PERCENTAGE_CHANGE` |
| `analyse_iv_rank` | `@both`, `@index_both` | `sensibull_ctx['current']['stats']['per_expiry_map']` (atm_iv, atm_iv_percentile, atm_ivp_type — nearest expiry) | Same as intraday | Empty per_expiry_map; `ivp` None; zone: >85 VERY_HIGH, >70 HIGH, <10 VERY_LOW, <20 LOW |
| `analyse_iv_vs_hv` | `@both`, `@index_both` | `stock.daily_hv` (cached, computed at startup from 1y daily priceData); if `daily_hv` is None, **falls back** to computing HV from `priceData['Close']` (last `IV_HV_PERIOD_BARS` rows, annualized ×√(252×75)); `sensibull_ctx['current']['stats']['per_expiry_map']` (atm_iv); `stock.is_index` | Always computes HV from `stock.priceData['Close']` (last `IV_HV_PERIOD_BARS` rows, annualized ×√252) — **never** uses `daily_hv` even if present; `sensibull_ctx['current']['stats']['per_expiry_map']` (atm_iv) | Empty per_expiry_map; `atm_iv <= 0`; insufficient rows for HV; `std == 0`; zone EXPENSIVE/EXTREME only |

**Redis coverage:** ✅ Intraday fully covered by `data:sensibull:*` + `data:price:*`. ⚠️ **Positional trend broken:** `analyse_trend_in_ATM_IV` positional path prefers `iv_chart_history` which is never fetched (dead code). Falls back to `historical_data` with different semantics. See [Section 5.3](#53-positional-sources--gaps).

---

### 7.5 FuturesAnalyser

**File:** `analyser/Futures_Analyser.py`
**Data sources:** `stock.zerodha_ctx` (Zerodha HTTP futures data)

| Method | Decorators | Intraday Data Sources | Positional Data Sources | Guard/Skip Conditions |
|--------|-----------|----------------------|------------------------|----------------------|
| `analyse_intraday_check_future_action` | `@both`, `@index_both` | `zerodha_ctx['futures_data']['current']` (close, oi — 5-min bars, today only); `['next']` (close, oi) | `zerodha_ctx['futures_data']['current']` (close, oi — daily bars, 90d); `['next']` (close, oi) | Data None/empty; `len < 2`; price % + OI % vs dynamic thresholds (ATR/OI-vol based) |
| `analyse_intraday_price_volume_oi_pattern` | `@both`, `@index_both` | `zerodha_ctx['futures_data']['current']` (close, volume, oi — 5-min bars) | `zerodha_ctx['futures_data']['current']` (close, volume, oi — daily bars) | Data None/empty; `len < 2`; pattern match (LBC/SCC/LU/SU/PVO) |
| `analyse_intraday_breakout_oi_confirmation` | `@intraday`, `@index_intraday` | `zerodha_ctx['futures_data']['current']` (high, low, close, oi, volume — today's 5-min rows, first bar must be 09:15) | N/A (positional only method) | Intraday only; first bar must be 09:15; `len < ORB_CANDLES+2`; ORB breakout + OI confirmation |
| `analyse_positional_oi_trend` | `@positional`, `@index_positional` | N/A (intraday only method) | `zerodha_ctx['futures_data']['current']` (oi, close — daily 90d); `zerodha_ctx['futures_mdata']['current']` (expiry for days_to_expiry) | Positional only; Data None/empty; `max_oi <= 0`; startup noise filter; `len < 10`; expiry suppression (LONG_UNWINDING_TREND if days_to_exp ≤ 4) |
| `analyse_positional_cost_of_carry` | `@both`, `@index_both` | `zerodha_ctx['futures_data']['current']` (close, underlying_price — 5-min bars); `futures_mdata['current']` (expiry); `min_rows = 1` | `zerodha_ctx['futures_data']['current']` (close, underlying_price — daily 90d); `futures_mdata['current']` (expiry); `min_rows = 3` | Data None/empty; no `underlying_price` column; spot == close for all rows; `len < min_rows`; basis % threshold |
| `analyse_positional_rollover_pressure` | `@positional`, `@index_positional` | N/A (positional only method) | `zerodha_ctx['futures_data']['current']` (oi — daily); `['next']` (oi — daily, next expiry populated in positional) | Positional only; either data None/empty; `next_oi <= 0`; rollover ratio threshold |
| `analyse_intraday_oi_buildup_from_open` | `@intraday`, `@index_intraday` | `zerodha_ctx['futures_data']['current']` (oi, close — 5-min bars, today from open) | N/A (intraday only method) | Intraday only; `len < 3`; session open OI cache; `open_oi <= 0` or `curr_oi <= 0`; OI % + price % from open |

**Redis coverage:** ✅ Fully covered by `data:zerodha:*` (now published by data-gateway)

---

### 7.6 PCRAnalyser

**File:** `analyser/PCRAnalyser.py`
**Data sources:** `stock.sensibull_ctx` (Sensibull HTTP)
**Mode difference:** Intraday methods read `oi_chain_history` (per-cycle snapshots, max 15); positional methods read `oi_history` (daily rows, ~181 — ⚠️ NOT FETCHED). Shared methods (`extreme_zones`, `directional_bias`, `divergence`) read `current` snapshot only.

| Method | Decorators | Intraday Data Sources | Positional Data Sources | Guard/Skip Conditions |
|--------|-----------|----------------------|------------------------|----------------------|
| `analyse_pcr_extreme_zones` | `@both`, `@index_both` | `sensibull_ctx['current']['stats']['underlying_base_stats']['total_pcr']`; `oi_chain_history` (pcr per snapshot) via `_count_consecutive_extreme` | Same as intraday; `oi_history` (col `pcr`) as fallback for consecutive count | No current/stats; `total_pcr` None; not extreme (≤0.3 or ≥1.5) |
| `analyse_pcr_directional_bias` | `@both`, `@index_both` | `sensibull_ctx['current']['stats']['underlying_base_stats']['total_pcr']`, `['per_expiry_pcr']`; `oi_chain_history` (prev_pcr via `_get_prev_pcr`) | Same as intraday; `oi_history` (col `pcr`, `.iloc[-2]`) as fallback for prev_pcr | No current/stats; `total_pcr` None; not in bias zone (<0.5 or >1.2) |
| `analyse_pcr_trend` | `@positional`, `@index_positional` | N/A (positional only) | `sensibull_ctx['oi_history']` (col `pcr`, `.tail(5)` — ⚠️ **NOT FETCHED**) | Positional only; `oi_history` None/empty; `len < PCR_TREND_DAYS(5)`; monotonic + pct ≥ 8% + abs ≥ 0.08 |
| `analyse_pcr_positional_reversal` | `@positional`, `@index_positional` | N/A (positional only) | `sensibull_ctx['oi_history']` (col `pcr`, `.tail(6)` — ⚠️ **NOT FETCHED**) | Positional only; `len < 6`; zone crossover or trend reversal pattern |
| `analyse_pcr_intraday_trend` | `@intraday`, `@index_intraday` | `sensibull_ctx['oi_chain_history']` (pcr per snapshot, last 5) | N/A (intraday only) | Intraday only; `< PCR_INTRADAY_MIN_SNAPSHOTS(3)`; monotonic + pct ≥ 5% |
| `analyse_pcr_reversal` | `@intraday`, `@index_intraday` | `sensibull_ctx['oi_chain_history']` (pcr per snapshot, last 4) | N/A (intraday only) | Intraday only; `< PCR_REVERSAL_MIN_SNAPSHOTS(4)`; zone crossover or trend reversal |
| `analyse_pcr_divergence` | `@both`, `@index_both` | `sensibull_ctx['current']['stats']['underlying_base_stats']['per_expiry_pcr']` | Same as intraday | No current/stats; `< 2` expiries; `pcr_diff ≤ PCR_DIVERGENCE_THRESHOLD(0.35)` |

**Redis coverage:** ✅ Intraday fully covered by `data:sensibull:*`. ⚠️ **Positional broken:** `analyse_pcr_trend` and `analyse_pcr_positional_reversal` read `oi_history` which is never fetched (dead code). Both silently skip on every run. See [Section 5.3](#53-positional-sources--gaps).

---

### 7.7 MaxPainAnalyser

**File:** `analyser/MaxPainAnalyser.py`
**Data sources:** `stock.sensibull_ctx` (Sensibull HTTP), `stock.ltp`, `stock.priceData`

| Method | Decorators | Intraday Data Sources | Positional Data Sources | Guard/Skip Conditions |
|--------|-----------|----------------------|------------------------|----------------------|
| `analyse_max_pain_deviation` | `@both`, `@index_both` | `sensibull_ctx['current']['stats']['per_expiry_map']` (max_pain_strike, max_pain_value, max_pain_type, future_price, pcr); `stock.ltp` (fallback `priceData['Close'].iloc[-1]`); expiry gate: ≤7 days | Same sources; expiry gate: ≤12 days | No current/stats; empty per_expiry_map; `max_pain_strike` None; expiry proximity gate; deviation < threshold (WEAK) |
| `analyse_max_pain_trend` | `@both`, `@index_both` | `sensibull_ctx['historical_data']` (cols `max_pain_{exp}`, `future_price_{exp}` — 5-min snapshots, `.tail(6)`) | `sensibull_ctx['oi_history']` (cols `max_pain`, `spot`, `date` — daily, `.tail(5)` — ⚠️ **NOT FETCHED**) | No per_expiry_map; intraday: historical_data None/empty, `< 3` rows; positional: oi_history None/empty, `< 2` rows; convergence/divergence gates |
| `analyse_max_pain_alignment` | `@both`, `@index_both` | `sensibull_ctx['current']['stats']['per_expiry_map']` (max_pain_type, pcr_type, max_pain_strike, pcr) | Same as intraday | No current/stats; empty per_expiry_map; `max_pain_type` or `max_pain_strike` falsy; requires aligned or divergent |

**Redis coverage:** ✅ Intraday fully covered by `data:sensibull:*` + `data:price:*`. ⚠️ **Positional trend broken:** `analyse_max_pain_trend` positional path reads `oi_history` which is never fetched (dead code). Silently skips on every run. See [Section 5.3](#53-positional-sources--gaps).

---

### 7.8 OIChainAnalyser

**File:** `analyser/OIChainAnalyser.py`
**Data sources:** `stock.sensibull_ctx` (Sensibull HTTP)
**Mode difference:** `@both` methods read `oi_chain` (single latest snapshot); `@intraday` methods read `oi_chain_history` (per-cycle snapshots); `@positional` methods read either `oi_chain` (single snapshot for capitulation/migration) or `oi_history` (daily rows for trend/acceleration — ⚠️ NOT FETCHED).

| Method | Decorators | Intraday Data Sources | Positional Data Sources | Guard/Skip Conditions |
|--------|-----------|----------------------|------------------------|----------------------|
| `analyse_oi_support_resistance` | `@both`, `@index_both` | `sensibull_ctx['oi_chain']` (per_strike_data: call_oi, put_oi; meta: current_ltp, atm_strike, pcr, expiry) | Same as intraday | No oi_chain; `current_ltp` falsy; no max OI strikes; dominance gate; breach only |
| `analyse_oi_buildup` | `@both`, `@index_both` | `sensibull_ctx['oi_chain']` (per_strike_data: call_oi, put_oi, prev_call_oi, prev_put_oi; meta: current_ltp, total_call_oi, total_put_oi, total_call_oi_change, total_put_oi_change, expiry) | Same as intraday | No oi_chain; `current_ltp` falsy; total change < threshold; heavy/dominant call/put writing |
| `analyse_oi_wall` | `@both`, `@index_both` | `sensibull_ctx['oi_chain']` (per_strike_data: call_oi, put_oi; meta: current_ltp, expiry) | Same as intraday | No oi_chain; `current_ltp` falsy; `< 5` OI values; distance filter; statistical wall threshold; asymmetry |
| `analyse_oi_shift` | `@both`, `@index_both` | `sensibull_ctx['oi_chain']` (per_strike_data: call_oi, put_oi, prev_call_oi, prev_put_oi; meta: current_ltp, prev_ltp, expiry) | Same as intraday | No oi_chain; `current_ltp` falsy; no new OI; weighted-avg strike shift classification |
| `analyse_oi_capitulation` | `@positional`, `@index_positional` | N/A (positional only) | `sensibull_ctx['oi_chain']` (per_strike_data: prev_call_oi, prev_put_oi, call_oi, put_oi; meta: current_ltp, total_call_oi, total_put_oi, expiry) | Positional only; expiry guard (OI drop >80%); distance filter; min strikes; macro weight threshold |
| `analyse_oi_wall_migration` | `@positional`, `@index_positional` | N/A (positional only) | `sensibull_ctx['oi_chain']` (per_strike_data: call_oi, put_oi, prev_call_oi, prev_put_oi; meta: current_ltp, prev_ltp, expiry) | Positional only; `prev_ltp` falsy; expiry guard; min migration points; wall retreat/migration direction |
| `analyse_positional_oi_trend` | `@positional`, `@index_positional` | N/A (positional only) | `sensibull_ctx['oi_history']` (cols: call_oi, put_oi, futures_oi, pcr, date — ⚠️ **NOT FETCHED**) | Positional only; `len < OI_POSITIONAL_TREND_DAYS(5)`; call/put dominant or balanced |
| `analyse_oi_acceleration` | `@positional`, `@index_positional` | N/A (positional only) | `sensibull_ctx['oi_history']` (cols: call_oi_change, put_oi_change, date — ⚠️ **NOT FETCHED**) | Positional only; `len < 6`; recent vs prev velocity ratio; min velocity/base thresholds |
| `analyse_intraday_oi_trend` | `@intraday`, `@index_intraday` | `sensibull_ctx['oi_chain_history']` (per snapshot: total_call_oi, total_put_oi, pcr, current_ltp, timestamp, expiry) | N/A (intraday only) | Intraday only; `< OI_TREND_MIN_SNAPSHOTS(5)`; call/put OI change + PCR change thresholds |
| `analyse_intraday_oi_sr_shift` | `@intraday`, `@index_intraday` | `sensibull_ctx['oi_chain_history']` (per snapshot: per_strike_data call_oi/put_oi, current_ltp, expiry) | N/A (intraday only) | Intraday only; `< OI_SR_SHIFT_MIN_SNAPSHOTS(5)`; min strike width shift; consistency gate |

**Redis coverage:** ✅ Intraday fully covered by `data:sensibull:*`. ⚠️ **Positional broken:** `analyse_positional_oi_trend` and `analyse_oi_acceleration` read `oi_history` which is never fetched (dead code). Both silently skip on every run. See [Section 5.3](#53-positional-sources--gaps).

---

### 7.9 GEXAnalyser

**File:** `analyser/GEXAnalyser.py`
**Data sources:** `stock.options_live` (Sensibull WS), `stock.options_aggregate`, `stock.ltp`
**Applies to:** NIFTY, BANKNIFTY, SENSEX only (`LIVE_OPTIONS_INDICES`)

**`_is_applicable()` guard:** Returns `False` unless:
1. `stock.stock_symbol in LIVE_OPTIONS_INDICES`
2. `stock.options_live` is non-empty
3. At least one strike has non-zero `gamma` in `options_live[strike][CE/PE]`

Without Sensibull WS greeks, all gamma values are 0 → GEX silently skips.

| Method | Decorators | Intraday Data Sources | Positional Data Sources | Guard/Skip Conditions |
|--------|-----------|----------------------|------------------------|----------------------|
| `analyse_gex_regime` | `@both`, `@index_both` | `options_live[strike][CE/PE]['gamma']`, `['oi']` (via `_compute_gex`); `options_aggregate['gex_regime']` (prev cycle); `stock.ltp`; `constant.INDEX_LOT_SIZES` | Same as intraday | `_is_applicable` False; `ltp <= 0`. **Writes:** `options_aggregate` gex_total/ce/pe/regime/flip_level/by_strike + `set_analysis("NEUTRAL", "GEX_REGIME", ...)` |
| `analyse_gex_flip_proximity` | `@both`, `@index_both` | `options_aggregate['gex_flip_level']`, `['gex_total']`; `stock.ltp` | Same as intraday | `_is_applicable` False; `flip_level` None; `abs(gex_total) < GEX_NOISE_FLOOR_CR`; `distance_pct > FLIP_PROXIMITY_THRESHOLD_PCT` |
| `analyse_gex_wall` | `@both`, `@index_both` | `options_aggregate['gex_by_strike']`; `stock.ltp` | Same as intraday | `_is_applicable` False; `< 5` strikes; `ltp <= 0`; no strike exceeds threshold within ±5% of spot |
| `analyse_gex_wall_breach` | `@intraday`, `@index_intraday` | `options_aggregate['gex_by_strike']` (current); **`self._prev_gex_by_strike[symbol]`** (previous cycle — instance state); `stock.ltp` | N/A (intraday only) | Intraday only; `_is_applicable` False; no prev cycle data; `< 5` prev strikes; GEX drop + spot beyond threshold |
| `analyse_gex_imbalance` | `@both`, `@index_both` | `options_aggregate['gex_ce']`, `['gex_pe']` | Same as intraday | `_is_applicable` False; either side < `IMBALANCE_MIN_SIDE_CR`; ratio < `IMBALANCE_RATIO_THRESHOLD(2.5)` |

**⚠️ Stateful cross-cycle dependency:** `analyse_gex_wall_breach` uses `self._prev_gex_by_strike` (instance attribute) to compare current vs previous cycle's `gex_by_strike`. Stateless workers that rotate/restart will lose this state, breaking wall-breach detection. Must be persisted in Redis or pre-computed by monolith.

**Redis coverage:** ❌ **NOT covered.** `options_live` and `options_aggregate` are not published to Redis. Requires:
1. Monolith to snapshot `_tick_store.options_live` → `HSET data:options_live:{symbol}` at cycle boundary
2. Monolith to snapshot `_tick_store.options_aggregate` → `HSET data:options_agg:{symbol}` at cycle boundary
3. Analysis-engine worker to load these into a local TickStore before running GEX
4. `gex_by_strike` previous-cycle state must be persisted for wall-breach detection

---

### 7.10 PanicModeAnalyser

**File:** `analyser/PanicModeAnalyser.py`
**Data sources:** `stock.analysis` (composite — reads signals from all upstream analysers), `stock.ltp_change_perc`, `shared.app_ctx.india_vix_ltp` (optional)
**Must be registered AFTER analysers #1-9.**
**Mode difference:** `stock.analysis` contents differ by mode — intraday has `OI_INTRADAY_TREND`, `OI_SR_SHIFT`, `PCR_INTRADAY_TREND`, `PCR_REVERSAL`, `GEX_WALL_BREACH` (from intraday-only analysers); positional has `PCR_TREND`, `PCR_POS_REVERSAL`, `OI_CAPITULATION`, `OI_WALL_MIGRATION`, `OI_POSITIONAL_TREND`, `OI_ACCELERATION` (from positional-only analysers). Both modes share `IV_*`, `OI_BUILDUP`, `OI_WALL`, `FUTURE_*`, `VOLUME_*`, `PCR_BIAS`, candlestick patterns. `india_vix_ltp` threshold adapts per mode. Note: PanicModeAnalyser itself reads `PCR_BIAS`/`PCR_TREND`/`PCR_REVERSAL` in **both** modes (it does NOT branch on mode for PCR keys, and never reads `PCR_POS_REVERSAL`).

| Method | Decorators | Intraday Data Sources | Positional Data Sources | Guard/Skip Conditions |
|--------|-----------|----------------------|------------------------|----------------------|
| `analyse_panic_mode` | `@both`, `@index_both` | `stock.ltp_change_perc`; `shared.app_ctx.india_vix_ltp`; `stock.analysis` — reads same keys in both modes: `PCR_BIAS`, `PCR_TREND`, `PCR_REVERSAL`, `PCR_CROSSOVER_*` (live), `PCR_SUSTAINED_*` (live), `OI_INTRADAY_TREND`, `OI_SR_SHIFT`, `CE_WALL_BREACH`/`PE_WALL_BREACH` (live). Intraday-only keys may be absent in positional. | Same as intraday — `stock.analysis` keys are the same; positional-only keys (`PCR_POS_REVERSAL`, `OI_CAPITULATION`, etc.) are NOT read by this analyser | `ltp_change_perc` None; price change within VIX-adaptive threshold; `count < PANIC_MIN_CONDITIONS(4)` |
| `analyse_panic_exhaustion` | `@both`, `@index_both` | `stock.ltp_change_perc`; `stock.analysis` — reads `IV_RANK_EXTREME`, `IV_PREMIUM`, `PCR_EXTREME`, `PCR_REVERSAL`, `VOLUME_CLIMAX`, `OI_WALL`, `FUTURE_ACTION_SHORT_COVERING`/`LONG_UNWINDING`, candlestick reversals | Same as intraday — same keys read in both modes; `PCR_POS_REVERSAL` is NOT read by this analyser | `ltp_change_perc` None; price change within threshold; `count < EXHAUSTION_MIN_CONDITIONS(3)` |

#### `analyse_panic_mode` — `stock.analysis` keys read

| Condition | Sentiment | Keys Read |
|-----------|-----------|-----------|
| C2 — IV Fear | `NEUTRAL` | `IV_SPIKE`, `IV_TREND` (attr `.trend == "UPWARD"`), `IV_RANK_EXTREME`, `IV_RANK`, `IV_PREMIUM` (attr `.zone ∈ {EXPENSIVE, EXTREME}`), `IV_EXPANDING` (live), `IV_TREND_RISING` (live) |
| C3 — OI Confirming | `{direction}` | `OI_INTRADAY_TREND` (intraday), `OI_BUILDUP`, `OI_SHIFT`, `OI_SUPPORT_RESISTANCE`, `OI_WALL`, `OI_SR_SHIFT` (intraday); `CE_WALL_BREACH` (if BEARISH, live), `PE_WALL_BREACH` (if BULLISH, live) |
| C4 — Futures | `BEARISH` | `FUTURE_ACTION_SHORT_BUILDUP`, `FUTURE_ACTION_LONG_UNWINDING`, `FUTURE_ACTION`, `FUTURE_SIGNAL_SCORE_HIGH`, `FUTURE_SIGNAL_SCORE_MEDIUM`, `FUTURE_BREAKOUT_CONFIRMED`, `FUTURE_BREAKOUT_MTF_ALIGNED` |
| C4 — Futures | `BULLISH` | `FUTURE_ACTION_LONG_BUILDUP`, `FUTURE_ACTION_SHORT_COVERING`, `FUTURE_ACTION`, `FUTURE_SIGNAL_SCORE_HIGH`, `FUTURE_SIGNAL_SCORE_MEDIUM`, `FUTURE_BREAKOUT_CONFIRMED`, `FUTURE_BREAKOUT_MTF_ALIGNED` |
| C5 — Volume | `{direction}` | `VOLUME_BREAKOUT`, `VOLUME_CLIMAX`, `OBV_DIVERGENCE` |
| C6 — PCR | `{direction}` | `PCR_BIAS`, `PCR_TREND`, `PCR_REVERSAL` (all read in both modes — NOT mode-branched), `PCR_CROSSOVER_{direction}` (live), `PCR_SUSTAINED_{direction}` (live) |

#### `analyse_panic_exhaustion` — `stock.analysis` keys read

| Condition | Sentiment | Keys Read |
|-----------|-----------|-----------|
| E1 — IV Extreme | `NEUTRAL` | `IV_RANK_EXTREME` (attrs `.iv_percentile`, `.ivp_type`, `.category`), `IV_RANK`, `IV_PREMIUM` (attr `.zone == "EXTREME"`) |
| E2 — Contrarian PCR | `{contrarian}` | `PCR_EXTREME`, `PCR_REVERSAL` (same key in both modes — does NOT read `PCR_POS_REVERSAL`) |
| E3 — Volume Climax | `{panic_direction}` | `VOLUME_CLIMAX`, `VOLUME_BREAKOUT` (fallback) |
| E4 — Structural Hold | `{contrarian}` | `OI_WALL`; `NEUTRAL`: `OI_SUPPORT_RESISTANCE` (fallback); `BULLISH`: `FUTURE_ACTION_SHORT_COVERING` (if panic=BEARISH); `BEARISH`: `FUTURE_ACTION_LONG_UNWINDING` (if panic=BULLISH) |
| E5 — Candle Reversal | `{contrarian}` | `Double_candle_stick_pattern`, `Triple_candle_stick_pattern`, `Triple_candle_reversal_pattern`, `Single_candle_reversal_pattern` |

**Redis coverage:** ✅ Covered indirectly (depends on all upstream analysers having their data). `ltp_change_perc` from `data:price:*`. `india_vix_ltp` is optional (degrades gracefully).

---

### 7.11 OptionSellerCompositeAnalyser

**File:** `analyser/OptionSellerCompositeAnalyser.py`
**Data sources:** `stock.analysis` (composite only — reads signals from all upstream analysers including PanicMode)
**Must be registered AFTER PanicModeAnalyser (#10).**
**Custom dispatch:** Runs in fixed order: Gamma Trap → Range Bound → Skew Fade.

| Method | Dispatch | Intraday Data Sources | Positional Data Sources | Guard/Skip Conditions |
|--------|----------|----------------------|------------------------|----------------------|
| `detect_gamma_trap_warning` | Both modes | `stock.analysis` — reads `OI_CAPITULATION`, `OI_WALL_MIGRATION`, `GEX_WALL_BREACH` (intraday-only), `VOLUME_BREAKOUT`, `VOLUME_CLIMAX`, `FUTURE_BREAKOUT_*`, `FUTURE_ACTION_*`, `IV_SPIKE` | `stock.analysis` — reads `OI_CAPITULATION`, `OI_WALL_MIGRATION` (no `GEX_WALL_BREACH`), `VOLUME_BREAKOUT`, `VOLUME_CLIMAX`, `FUTURE_BREAKOUT_*`, `FUTURE_ACTION_*`, `IV_SPIKE` | G1 (breach) not met; `count < GAMMA_TRAP_MIN_CONDITIONS(3 of 4)`. **Writes:** `GAMMA_TRAP` + `GAMMA_TRAP_ACTIVE=True` + `PRIORITY_OVERRIDE=CRITICAL` |
| `detect_range_bound_premium_setup` | Both modes | `stock.analysis` — reads `IV_RANK_*`, `IV_PREMIUM`, `OI_WALL`, `VOLUME_BREAKOUT` (absence), `RSI_DIVERGENCE` (absence), `FUTURE_ACTION_*` (absence), `MAX_PAIN`, `GEX_REGIME` | Same as intraday | Suppressed if `GAMMA_TRAP_ACTIVE`; `count < RANGE_BOUND_MIN_CONDITIONS(4 of 6)`. **Writes:** `RANGE_BOUND_SETUP` + `PRIORITY_OVERRIDE=HIGH` |
| `detect_skew_fade_setup` | Both modes | `stock.analysis` — reads `PANIC_EXHAUSTION`, `OI_SUPPORT_RESISTANCE`, candlestick reversals, `PCR_EXTREME`, **`PCR_REVERSAL`** (intraday key), `PCR_INTRADAY_TREND` (fallback) | `stock.analysis` — reads `PANIC_EXHAUSTION`, `OI_SUPPORT_RESISTANCE`, candlestick reversals, `PCR_EXTREME`, **`PCR_POS_REVERSAL`** (positional key, no fallback to `PCR_INTRADAY_TREND`) | S1: no `PANIC_EXHAUSTION`; S2: no `OI_SUPPORT_RESISTANCE`/no reversal candle; S3: no `PCR_EXTREME`/no PCR reversal. `count < 3 of 3 strict`. **Writes:** `SKEW_FADE_SETUP` + `PRIORITY_OVERRIDE=HIGH` |

#### `detect_gamma_trap_warning` — `stock.analysis` keys read

| Condition | Sentiment | Keys Read |
|-----------|-----------|-----------|
| G1 — Wall Breach | `BULLISH`, `BEARISH` | `OI_CAPITULATION` (attrs `.side`, `.unwound_pct`, `.top_strikes`), `OI_WALL_MIGRATION` (attrs `.migration_direction`, `.side`, `.migration_pts`), `GEX_WALL_BREACH` (attrs `.breach_side`, `.gex_drop_pct`, `.breached_strike`) |
| G2 — Volume Surge | `{direction}`, `BULLISH`, `BEARISH` | `VOLUME_BREAKOUT` (attr `.volume_ratio`), `VOLUME_CLIMAX` (attr `.volume_ratio`) |
| G3 — Futures Fuel | `BULLISH` | `FUTURE_BREAKOUT_MTF_ALIGNED`, `FUTURE_BREAKOUT_CONFIRMED`, `FUTURE_ACTION_LONG_BUILDUP` (attrs `.oi_percentage`, `.oi_change_pct`) |
| G3 — Futures Fuel | `BEARISH` | `FUTURE_BREAKOUT_MTF_ALIGNED`, `FUTURE_BREAKOUT_CONFIRMED`, `FUTURE_ACTION_SHORT_BUILDUP` |
| G4 — Vol Expansion | `NEUTRAL` | `IV_SPIKE` (attrs `.atm_iv`/`.iv`, `.atm_iv_change`/`.iv_change`) |

#### `detect_range_bound_premium_setup` — `stock.analysis` keys read

| Condition | Sentiment | Keys Read |
|-----------|-----------|-----------|
| R1 — Overpriced Vol | `NEUTRAL` | `IV_RANK_EXTREME`, `IV_RANK`, `IV_PREMIUM` (attrs `.category`, `.iv_percentile`, `.atm_iv`, `.zone`, `.iv_hv_ratio`, `.iv_premium_pct`, `.hv`) |
| R2 — Ceiling & Floor | `BULLISH`, `BEARISH` | `OI_WALL` (attrs `.wall_type`, `.nearest_call_wall`, `.nearest_put_wall`) — looks for `wall_type == "BOTH_WALLS"` |
| R3 — Neutral Momentum | `BULLISH`, `BEARISH` | `VOLUME_BREAKOUT` (absence check), `RSI_DIVERGENCE` (absence check) |
| R4 — No Inst. Push | `BULLISH`, `BEARISH` | `FUTURE_ACTION_LONG_BUILDUP` (absence), `FUTURE_ACTION_SHORT_BUILDUP` (absence) |
| R5 — Max Pain Magnet | `BULLISH`, `BEARISH` | `MAX_PAIN` (attr `.deviation_pct`) |
| R6 — GEX Range | `NEUTRAL` | `GEX_REGIME` (attrs `.regime`, `.magnitude`, `.gex_total`) |

#### `detect_skew_fade_setup` — `stock.analysis` keys read

| Condition | Sentiment | Keys Read |
|-----------|-----------|-----------|
| S1 — Exhaustion | `BULLISH`, `BEARISH` | `PANIC_EXHAUSTION` (attrs `.panic_direction`, `.confidence`, `.iv_percentile`) |
| S2 — Brick Wall | `{fade_direction}` | `OI_SUPPORT_RESISTANCE` (attrs `.current_price`, `.support_strike`/`.resistance_strike`); `Double_candle_stick_pattern`, `Single_candle_reversal_pattern`, `Triple_candle_stick_pattern`, `Triple_candle_reversal_pattern` |
| S3 — PCR Trap | `BULLISH`, `BEARISH`, `NEUTRAL` | `PCR_EXTREME`; `{fade_direction}`: `PCR_REVERSAL` (intraday), `PCR_POS_REVERSAL` (positional), `PCR_INTRADAY_TREND` (intraday fallback) |

**Redis coverage:** ✅ Covered indirectly (reads only `stock.analysis` populated by upstream analysers). Depends on GEX (#9) working for `GEX_WALL_BREACH` and `GEX_REGIME` signals.

---

### 7.12 Mode-Dependent Data Source Summary

Methods where intraday and positional read **different** data sources or structures (not just different timeframe on the same source):

| Analyser | Method | Intraday reads | Positional reads | Key difference |
|----------|--------|---------------|-----------------|----------------|
| IVAnalyser | `analyse_spike_in_ATM_IV` | `historical_data` (col `atm_iv_{exp}`, 5-min snapshots, `.iloc[-2/-1]`) | `per_expiry_map` (atm_iv + atm_iv_change, single snapshot) | Different source entirely: time-series vs point-in-time |
| IVAnalyser | `analyse_trend_in_ATM_IV` | `historical_data` (col `atm_iv_{exp}`, 5-min) | `iv_chart_history` (col `iv_close`, daily 2yr) ⚠️ **NOT FETCHED**; fallback: `historical_data` | Different source: 5-min vs daily IV |
| IVAnalyser | `analyse_iv_vs_hv` | `daily_hv` (cached if available, else falls back to computing from `priceData['Close']` ×√(252×75)) | Always computes HV from `priceData['Close']` (last 20 daily bars, ×√252); never uses `daily_hv` | Cached (with fallback) vs always compute; different annualization |
| MaxPainAnalyser | `analyse_max_pain_deviation` | `per_expiry_map` + `ltp`; expiry gate ≤7d | Same sources; expiry gate ≤12d | Only threshold differs |
| MaxPainAnalyser | `analyse_max_pain_trend` | `historical_data` (cols `max_pain_{exp}`, `future_price_{exp}`, 5-min, `.tail(6)`) | `oi_history` (cols `max_pain`, `spot`, `date`, daily, `.tail(5)`) ⚠️ **NOT FETCHED** | Different source: 5-min vs daily |
| TechnicalAnalyser | `analyse_pivot_points` | `prevDayOHLCV['HIGH/LOW/CLOSE']` (previous calendar day) | `previous_equity_data['High/Low/Close']` (previous daily bar) | Different Stock attribute |
| FuturesAnalyser | `analyse_positional_cost_of_carry` | `futures_data['current']` (5-min bars); `min_rows = 1` | `futures_data['current']` (daily 90d); `min_rows = 3` | Same source, different timeframe + threshold |
| FuturesAnalyser | `analyse_intraday_check_future_action` | `futures_data['current'/'next']` (5-min, today, current expiry only) | `futures_data['current'/'next']` (daily 90d, both expiries) | Different data fetched by data-gateway per mode |
| PCRAnalyser | `analyse_pcr_trend` | N/A (positional only) | `oi_history` (col `pcr`, daily) ⚠️ **NOT FETCHED** | Positional-only method |
| PCRAnalyser | `analyse_pcr_positional_reversal` | N/A (positional only) | `oi_history` (col `pcr`, daily) ⚠️ **NOT FETCHED** | Positional-only method |
| PCRAnalyser | `analyse_pcr_intraday_trend` | `oi_chain_history` (pcr per snapshot, 5-min) | N/A (intraday only) | Intraday-only method |
| PCRAnalyser | `analyse_pcr_reversal` | `oi_chain_history` (pcr per snapshot, 5-min) | N/A (intraday only) | Intraday-only method |
| OIChainAnalyser | `analyse_intraday_oi_trend` | `oi_chain_history` (per snapshot: total_call/put_oi, pcr, 5-min) | N/A (intraday only) | Intraday-only method |
| OIChainAnalyser | `analyse_intraday_oi_sr_shift` | `oi_chain_history` (per snapshot: per_strike_data, 5-min) | N/A (intraday only) | Intraday-only method |
| OIChainAnalyser | `analyse_positional_oi_trend` | N/A (positional only) | `oi_history` (cols call_oi, put_oi, futures_oi, pcr, daily) ⚠️ **NOT FETCHED** | Positional-only method |
| OIChainAnalyser | `analyse_oi_acceleration` | N/A (positional only) | `oi_history` (cols call_oi_change, put_oi_change, daily) ⚠️ **NOT FETCHED** | Positional-only method |
| OptionSellerComposite | `detect_skew_fade_setup` | `PCR_REVERSAL` (intraday key) + `PCR_INTRADAY_TREND` (fallback) | `PCR_POS_REVERSAL` (positional key, no fallback) | Different `stock.analysis` key per mode |

> **Note on `historical_data` timeframe:** In intraday mode, `priceData` is 5-min bars (last 5 days) and `historical_data` has 5-min IV snapshots. In positional mode, `priceData` is daily bars (2yr) and `historical_data` has per-cycle snapshots (retained `tail(30)`). The `oi_chain_history` is retained max 15 snapshots intraday, single snapshot positional. `oi_chain` is always the latest snapshot in both modes.
>
> **Note on PanicModeAnalyser:** Unlike OptionSellerCompositeAnalyser, PanicModeAnalyser does NOT branch on mode for PCR keys. It reads `PCR_BIAS`/`PCR_TREND`/`PCR_REVERSAL` identically in both intraday and positional modes. `PCR_POS_REVERSAL` is never read by PanicModeAnalyser.

---

## 8. Cross-Reference: Data Source → Analysers

| Data Source | Redis Hash | Analysers That Read It |
|-------------|-----------|----------------------|
| `priceData` (yfinance) | `data:price:*` | VolumeAnalyser (all), TechnicalAnalyser (10/11), CandleStickAnalyser (all), IVAnalyser (iv_vs_hv HV fallback), MaxPainAnalyser (ltp fallback), PanicModeAnalyser (ltp_change_perc) |
| `prevDayOHLCV` (yfinance) | `data:price:*` | TechnicalAnalyser (pivot_points intraday) |
| `daily_hv` (yfinance) | `data:price:*` | IVAnalyser (iv_vs_hv intraday fast path) |
| `ltp` (yfinance) | `data:price:*` | MaxPainAnalyser (deviation), GEXAnalyser (spot), PanicModeAnalyser (via ltp_change_perc) |
| `sensibull_ctx.current` (Sensibull HTTP) | `data:sensibull:*` | IVAnalyser (spike, rank, iv_vs_hv), PCRAnalyser (extreme, bias, divergence), MaxPainAnalyser (deviation, alignment) |
| `sensibull_ctx.historical_data` (Sensibull HTTP) | `data:sensibull:*` | IVAnalyser (spike intraday, trend), MaxPainAnalyser (trend intraday) |
| `sensibull_ctx.iv_chart_history` (Sensibull HTTP) | `data:sensibull:*` | IVAnalyser (trend positional) |
| `sensibull_ctx.oi_chain` (Sensibull HTTP) | `data:sensibull:*` | OIChainAnalyser (SR, buildup, wall, shift, capitulation, wall_migration) |
| `sensibull_ctx.oi_chain_history` (Sensibull HTTP) | `data:sensibull:*` | PCRAnalyser (intraday_trend, reversal, extreme helper), OIChainAnalyser (intraday_trend, intraday_sr_shift) |
| `sensibull_ctx.oi_history` (Sensibull HTTP) | `data:sensibull:*` | PCRAnalyser (trend, positional_reversal, helpers), MaxPainAnalyser (trend positional), OIChainAnalyser (positional_trend, acceleration) |
| `zerodha_ctx.futures_data` (Zerodha HTTP) | `data:zerodha:*` | FuturesAnalyser (all methods) |
| `zerodha_ctx.futures_mdata` (Zerodha HTTP) | `data:zerodha:*` | FuturesAnalyser (positional_oi_trend, cost_of_carry — expiry/days_to_expiry) |
| `options_live` (Sensibull WS) | ⚠️ NOT in Redis | GEXAnalyser (all methods via `_compute_gex`) |
| `options_aggregate` (TickStore recompute) | ⚠️ NOT in Redis | GEXAnalyser (flip_proximity, wall, wall_breach, imbalance) |
| `zerodha_data` (Zerodha WS) | ⚠️ NOT in Redis | TechnicalAnalyser (buy_sell_quantity only) |
| `futures_live` (Zerodha WS) | ⚠️ Dead code (no WS subscription) | None — futures tokens never subscribed |
| `india_vix_ltp` (app context) | N/A (in-memory) | PanicModeAnalyser (adaptive threshold, optional) |
| `stock.analysis` (composite) | N/A (in-memory) | PanicModeAnalyser (all), OptionSellerCompositeAnalyser (all) |

> **⚠️ Positional sources broken:** `iv_chart_history` and `oi_history` are published to Redis but **never fetched** — the monolith's `fetch_iv_chart()`/`fetch_oi_history()` have zero callers (dead code), and the data-gateway serializes empty DataFrames. See [Section 5.3](#53-positional-sources--gaps).

---

## 9. Migration Coverage Matrix

| # | Analyser | data:price | data:sensibull | data:zerodha | options_live (WS) | options_agg (WS) | zerodha_data (WS) | Status |
|---|----------|-----------|---------------|-------------|-------------------|------------------|-------------------|--------|
| 1 | VolumeAnalyser | ✅ | — | — | — | — | — | ✅ Complete |
| 2 | TechnicalAnalyser | ✅ | — | — | — | — | ⚠️ 1 method | ✅ 10/11 methods |
| 3 | CandleStickAnalyser | ✅ | — | — | — | — | — | ✅ Complete |
| 4 | IVAnalyser | ✅ | ✅ (current/historical) ⚠️ (iv_chart) | — | — | — | — | ⚠️ Intraday complete; positional trend broken (iv_chart_history empty) |
| 5 | FuturesAnalyser | — | — | ✅ | — | — | — | ✅ Complete |
| 6 | PCRAnalyser | — | ✅ (current/oi_chain_history) ⚠️ (oi_history) | — | — | — | — | ⚠️ Intraday complete; positional trend/reversal broken (oi_history empty) |
| 7 | MaxPainAnalyser | ✅ | ✅ (current/historical) ⚠️ (oi_history) | — | — | — | — | ⚠️ Intraday complete; positional trend broken (oi_history empty) |
| 8 | OIChainAnalyser | — | ✅ (oi_chain/oi_chain_history) ⚠️ (oi_history) | — | — | — | — | ⚠️ Intraday complete; positional trend/acceleration broken (oi_history empty) |
| 9 | GEXAnalyser | ✅ (ltp) | — | — | ❌ | ❌ | — | ❌ **Gap** |
| 10 | PanicModeAnalyser | ✅ | — | — | — | — | — | ✅ Indirect |
| 11 | OptionSellerComposite | — | — | — | — | — | — | ✅ Indirect |

### Gaps to Close

1. **GEXAnalyser (#9):** Requires `options_live` (per-strike gamma + OI) and `options_aggregate` (gex_by_strike, gex_total, etc.) published to Redis from monolith's TickStore at cycle boundary. Source: Sensibull Live WS (only source of gamma).

2. **TechnicalAnalyser `analyze_buy_sell_quantity` (#2):** Requires `zerodha_data` (total_buy_quantity, total_sell_quantity) from Zerodha WS live tick. Optional — single method, silently returns False without it.

3. **GEX wall-breach cross-cycle state:** `analyse_gex_wall_breach` uses `self._prev_gex_by_strike` (instance state). Stateless workers need this persisted in Redis or pre-computed by monolith.

4. **India VIX LTP (optional):** `shared.app_ctx.india_vix_ltp` for PanicModeAnalyser adaptive threshold. Degrades gracefully if absent, but should be published to Redis for full functionality.

5. **Positional `iv_chart_history` (Gap — affects IVAnalyser):** `fetch_iv_chart()` endpoint exists but has zero callers. Data-gateway must port the fetch and call it once/day (positional window). Writes `iv_chart_history_json` to `data:sensibull:{symbol}`. Without this, `IVAnalyser.analyse_trend_in_ATM_IV` positional path falls back to intraday `historical_data` (different semantics).

6. **Positional `oi_history` (Gap — affects PCRAnalyser, MaxPainAnalyser, OIChainAnalyser):** `fetch_oi_history()` endpoint exists but has zero callers. Data-gateway must port the fetch and call it once/day (positional window). Writes `oi_history_json` to `data:sensibull:{symbol}`. Without this, **5 positional methods silently skip on every run**: `PCRAnalyser.analyse_pcr_trend`, `PCRAnalyser.analyse_pcr_positional_reversal`, `MaxPainAnalyser.analyse_max_pain_trend` (positional), `OIChainAnalyser.analyse_positional_oi_trend`, `OIChainAnalyser.analyse_oi_acceleration`.

7. **Zerodha futures WS (dead code — no action needed):** `_process_future_tick` and `TickStore.futures_live` exist but futures tokens are never subscribed on any WS connection. All futures data comes via REST. No fix needed — just be aware `futures_live` is always empty.
