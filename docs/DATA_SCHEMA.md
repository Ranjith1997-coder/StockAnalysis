# Data Schema Reference

All external data sources, their raw API response schemas, and how data is stored in the `Stock` object.

---

## Table of Contents

1. [Sensibull Insights API](#1-sensibull-insights-api)
2. [Sensibull OI Chain API](#2-sensibull-oi-chain-api)
3. [Sensibull IV Chart API](#3-sensibull-iv-chart-api)
4. [Sensibull OI History API](#4-sensibull-oi-history-api)
5. [Zerodha WebSocket — Equity / Index Ticks](#5-zerodha-websocket--equity--index-ticks)
6. [Zerodha WebSocket — Live Options](#6-zerodha-websocket--live-options)
7. [Zerodha WebSocket — Live Futures](#7-zerodha-websocket--live-futures)
8. [Zerodha Kite Historical API — Futures OHLC](#8-zerodha-kite-historical-api--futures-ohlc)
9. [yfinance — Historical Price Data](#9-yfinance--historical-price-data)
10. [Stock Object — Full Context Summary](#10-stock-object--full-context-summary)
11. [FuturesAnalyser Namedtuples](#11-futuresanalyser-namedtuples)
12. [ANALYSIS_WEIGHTS](#12-analysis_weights)

---

## 1. Sensibull Insights API

| | |
|---|---|
| **File** | `fno/sensibull_fetcher.py` → `fetch_data()` |
| **Endpoint** | `GET https://oxide.sensibull.com/v1/compute/cache/insights/stock_info?tradingsymbol={symbol}` |
| **Auth** | None (unauthenticated) |
| **Called in** | Positional + Intraday loops |

### Raw Response

```json
{
  "success": true,
  "payload": {
    "underlying_info": { ... },
    "nse_stats": { ... },
    "stats": {
      "underlying_base_stats": {
        "volume_spike":       float | null,
        "volume_spike_type":  string | null,
        "future_oi_change":   float | null,
        "oi_change_type":     string | null,
        "total_pcr":          float | null
      },
      "per_expiry_map": {
        "YYYY-MM-DD": {
          "future_price":          float | null,
          "future_change_percent": float | null,
          "atm_strike":            float | null,
          "atm_iv":                float | null,
          "atm_iv_change":         float | null,
          "atm_iv_percentile":     float | null,
          "atm_ivp_type":          string | null,
          "max_pain_strike":       float | null,
          "max_pain_type":         string | null,
          "pcr":                   float | null,
          "pcr_type":              string | null,
          "lot_size":              int | null
        }
      }
    }
  }
}
```

> `per_expiry_map` has one key per active expiry (weekly + monthly). Multiple expiries are present for indices like NIFTY/BANKNIFTY; stocks typically have 1–2.

### Stored in `stock.sensibull_ctx`

```python
sensibull_ctx["last_fetch_time"]  = datetime

sensibull_ctx["current"] = {
    "underlying_info": dict,
    "stats":           dict,         # full stats block
    "per_expiry_map":  dict,         # same as payload.stats.per_expiry_map
    "nse_stats":       dict,
}

# historical_data: one row per fetch call, kept rolling
# Positional mode: last 30 rows
# Intraday mode:   last 5 days
sensibull_ctx["historical_data"] = pd.DataFrame(columns=[
    "timestamp",
    "volume_spike",
    "volume_spike_type",
    "future_oi_change",
    "oi_change_type",
    "total_pcr",
    # Dynamic per-expiry columns (suffix = expiry date without dashes e.g. 20260529):
    "future_price_{YYYYMMDD}",
    "future_change_pct_{YYYYMMDD}",
    "atm_strike_{YYYYMMDD}",
    "atm_iv_{YYYYMMDD}",
    "atm_iv_change_{YYYYMMDD}",
    "atm_iv_percentile_{YYYYMMDD}",
    "atm_ivp_type_{YYYYMMDD}",
    "max_pain_{YYYYMMDD}",
    "max_pain_type_{YYYYMMDD}",
    "pcr_{YYYYMMDD}",
    "pcr_type_{YYYYMMDD}",
    "lot_size_{YYYYMMDD}",
])
```

---

## 2. Sensibull OI Chain API

| | |
|---|---|
| **File** | `fno/sensibull_fetcher.py` → `fetch_oi_chain()` |
| **Endpoint** | `POST https://oxide.sensibull.com/v1/compute/1/oi_graphs/oi_chart` |
| **Auth** | None |
| **Called in** | Positional + Intraday loops (requires `fetch_data()` first) |

### Request Body

```json
{
  "underlying": "NIFTY",
  "expiries": {
    "YYYY-MM-DD": { "is_weekly": false, "is_enabled": true }
  },
  "atm_strike_selection": "twenty",
  "input_min_strike": null,
  "input_max_strike": null,
  "auto_update": "full_day",
  "show_prev_oi": true
}
```

> Only the nearest expiry has `"is_enabled": true`. `show_prev_oi: true` populates `prev_call_oi` / `prev_put_oi` per strike — used by OI capitulation and wall migration analysers.

### Raw Response

```json
{
  "success": true,
  "payload": {
    "input": {
      "date": "YYYY-MM-DD",
      "expiries": { "YYYY-MM-DD": { "is_enabled": true } }
    },
    "prev_ltp":              float,
    "current_ltp":           float,
    "date_ltp":              float,
    "atm_strike":            float,
    "total_call_oi":         int,
    "total_put_oi":          int,
    "total_call_oi_change":  int,
    "total_put_oi_change":   int,
    "pcr":                   float,
    "strike_list":           [float],
    "min_strike":            float,
    "max_strike":            float,
    "underlying_token":      string,
    "per_strike_data": {
      "24000": {
        "call_oi":       int,
        "put_oi":        int,
        "prev_call_oi":  int,
        "prev_put_oi":   int
      }
    }
  }
}
```

### Stored in `stock.sensibull_ctx`

```python
# Single latest snapshot
sensibull_ctx["oi_chain"] = {
    "timestamp":             datetime,
    "date":                  str,        # "YYYY-MM-DD"
    "expiry":                str,        # nearest enabled expiry
    "underlying_symbol":     str,
    "prev_ltp":              float,
    "current_ltp":           float,
    "date_ltp":              float,
    "atm_strike":            float,
    "total_call_oi":         int,
    "total_put_oi":          int,
    "total_call_oi_change":  int,
    "total_put_oi_change":   int,
    "pcr":                   float,
    "per_strike_data":       dict,       # keyed by strike string, values as above
    "strike_list":           list[float],
    "min_strike":            float,
    "max_strike":            float,
    "underlying_token":      str,
}

# Intraday: rolling list of snapshots (max 15)
# Positional: list with single entry
sensibull_ctx["oi_chain_history"] = [oi_chain, ...]
```

---

## 3. Sensibull IV Chart API

| | |
|---|---|
| **File** | `fno/sensibull_fetcher.py` → `fetch_iv_chart()` |
| **Endpoint** | `GET https://oxide.sensibull.com/v1/compute/iv_chart/{symbol}` |
| **Auth** | None |
| **Called in** | Once per positional cycle (fetch-once guard skips if already populated) |

### Raw Response

```json
{
  "success": true,
  "payload": {
    "iv_ohlc_data": {
      "YYYY-MM-DD": {
        "iv":    float,
        "close": float
      }
    }
  }
}
```

> `iv` is in percentage form (e.g. `18.04` = 18.04%). `close` is the underlying price close for that date. ~2 years of daily data (~730 rows).

### Stored in `stock.sensibull_ctx`

```python
sensibull_ctx["iv_chart_history"] = pd.DataFrame(columns=[
    "date",         # str "YYYY-MM-DD", sorted ascending
    "iv_close",     # float — ATM IV % (e.g. 18.04)
    "price_close",  # float — underlying closing price
])
```

---

## 4. Sensibull OI History API

| | |
|---|---|
| **File** | `fno/sensibull_fetcher.py` → `fetch_oi_history()` |
| **Endpoint** | `POST https://oxide.sensibull.com/v1/compute/compute_intraday` |
| **Auth** | None |
| **Called in** | Once per positional cycle (fetch-once guard; requires `fetch_data()` first) |
| **interval** | `"1D"` — returns ~181 trading days (~9 months) of daily data |

### Stored in `stock.sensibull_ctx`

```python
sensibull_ctx["oi_history"] = pd.DataFrame(columns=[
    "date",              # str "YYYY-MM-DD", sorted ascending
    "spot",              # float — underlying spot price
    "call_oi",           # int — total call OI for nearest expiry
    "put_oi",            # int — total put OI for nearest expiry
    "futures_oi",        # int — futures OI for next expiry
    "call_oi_change",    # int — call OI change vs previous day
    "put_oi_change",     # int — put OI change vs previous day
    "future_oi_change",  # int — futures OI change vs previous day
    "pcr",               # float — put-call ratio
    "max_pain",          # float — max pain strike
])
```

> Rows with `null` `call_oi` or `put_oi` are dropped at parse time. Used by `PCRAnalyser.analyse_pcr_trend` and `PCRAnalyser.analyse_pcr_pos_reversal`.

---

## 5. Zerodha WebSocket — Equity / Index Ticks

| | |
|---|---|
| **File** | `zerodha/zerodha_ticker.py` → `_parse_binary()` |
| **Protocol** | Kite Connect WebSocket — binary frames |
| **Auth** | Kite enctoken |
| **Modes** | `ltp` (8B), `quote` (44B equity / 28–32B index), `full` (184B equity) |

### LTP Mode (8 bytes)

```python
{
    "tradable":          bool,
    "mode":              "ltp",
    "instrument_token":  int,
    "last_price":        float,
}
```

### Quote Mode (44 bytes equity / 28–32 bytes index)

```python
{
    "tradable":               bool,
    "mode":                   "quote",
    "instrument_token":       int,
    "last_price":             float,
    "last_traded_quantity":   int,
    "average_traded_price":   float,    # VWAP
    "volume_traded":          int,
    "total_buy_quantity":     int,
    "total_sell_quantity":    int,
    "ohlc": {
        "open":  float,
        "high":  float,
        "low":   float,
        "close": float,
    },
    "change":                 float,    # % change from prev close
}
```

> **Index ticks (28/32 bytes) are smaller**: they carry `last_price`, `ohlc`, `change` but **no** `volume_traded`, `total_buy_quantity`, `total_sell_quantity`, or `oi`. `TickStore.update_zerodha_data()` uses `.get()` with safe defaults for these missing fields.

### Full Mode (184 bytes equity)

```python
{
    # All quote fields, plus:
    "last_trade_time":      datetime,
    "oi":                   int,
    "oi_day_high":          int,
    "oi_day_low":           int,
    "exchange_timestamp":   datetime,
    "depth": {
        "buy":  [{"quantity": int, "price": float, "orders": int}, ...],  # up to 5 levels
        "sell": [{"quantity": int, "price": float, "orders": int}, ...],
    },
}
```

### Stored in `stock` (via TickStore delegate)

```python
stock.zerodha_data = {
    "volume_traded":          int,
    "last_price":             float,
    "open":                   float,
    "high":                   float,
    "close":                  float,
    "low":                    float,
    "change":                 float,
    "average_traded_price":   float,    # VWAP
    "total_buy_quantity":     int,
    "total_sell_quantity":    int,
}
```

---

## 6. Zerodha WebSocket — Live Options

| | |
|---|---|
| **File** | `zerodha/tick_store.py` → `update_option_tick()` |
| **Protocol** | Same Kite WebSocket, full mode ticks for subscribed option instruments |
| **Subscribed strikes** | ATM ± 5% for NIFTY/BANKNIFTY (configured in `LIVE_OPTIONS_INDICES`) |

### Stored in `stock.options_live`

```python
stock.options_live = {
    24000.0: {                  # strike price as float key
        "CE": {
            "ltp":        float,
            "oi":         int,
            "prev_oi":    int,          # OI before this tick (computed by TickStore)
            "volume":     int,
            "buy_qty":    int,
            "sell_qty":   int,
            "timestamp":  datetime,
            "open":       float | None,
            "high":       float | None,
            "low":        float | None,
            "close":      float | None,
            # Greeks (Sensibull path only — absent for Zerodha-native):
            "delta":      float | None,
            "gamma":      float | None,
            "theta":      float | None,
            "vega":       float | None,
            "iv":         float | None,
            "iv_change":  float | None,
        },
        "PE": { ... },          # same fields
    },
    # more strikes ...
}
```

### Stored in `stock.options_aggregate` (recomputed every ~1s per symbol)

```python
stock.options_aggregate = {
    "total_ce_oi":          int,
    "total_pe_oi":          int,
    "live_pcr":             float,          # total_pe_oi / total_ce_oi
    "atm_strike":           float,
    "atm_straddle_premium": float,          # ATM CE ltp + ATM PE ltp
    "max_oi_ce_strike":     float,
    "max_oi_pe_strike":     float,
    "net_ce_oi_change":     int,            # sum(curr_oi - prev_oi) for CE
    "net_pe_oi_change":     int,            # sum(curr_oi - prev_oi) for PE
    "last_updated":         float,          # unix epoch timestamp
    # Sensibull-enriched fields (OPTIONS_SOURCE=sensibull only):
    "atm_iv":               float | None,
    "atm_iv_percentile":    float | None,
    "atm_ivp_type":         str   | None,
    "atm_iv_ce":            float | None,
    "atm_iv_pe":            float | None,
    "iv_skew":              float | None,
    "max_pain_strike":      float | None,
    "future_price":         float | None,
}
```

---

## 7. Zerodha WebSocket — Live Futures

| | |
|---|---|
| **File** | `zerodha/tick_store.py` → `update_futures_tick()` |
| **Protocol** | Kite WebSocket, full mode ticks for subscribed futures instruments |

### Stored in `stock.futures_live`

```python
stock.futures_live = {
    "current": {
        "ltp":       float,
        "oi":        int,
        "prev_oi":   int,       # OI before this tick (computed by TickStore)
        "volume":    int,
        "buy_qty":   int,
        "sell_qty":  int,
        "change":    float,
        "timestamp": datetime,
        "open":      float,
        "high":      float,
        "low":       float,
        "close":     float,
    },
    "next": { ... },            # same structure, next expiry
}
```

---

## 8. Zerodha Kite Historical API — Futures OHLC

| | |
|---|---|
| **File** | `zerodha/futures_fetcher.py` |
| **Method** | `kite.historical_data(instrument_token, from_date, to_date, interval, oi=True, continuous=False)` |
| **Positional** | interval=`"day"`, 90-day lookback, `continuous=False` (no rollover artifacts) |
| **Intraday** | interval=`"5minute"`, today only — one candle appended per cycle |

### Raw Response

```python
[
    {
        "date":   datetime,     # Asia/Kolkata timezone
        "open":   float,
        "high":   float,
        "low":    float,
        "close":  float,
        "volume": int,
        "oi":     int,          # Open Interest
    },
    # one entry per candle
]
```

### Stored in `stock.zerodha_ctx["futures_data"]`

Both `"current"` (current expiry) and `"next"` (next expiry) DataFrames have the same schema:

```python
stock.zerodha_ctx["futures_data"]["current"] = pd.DataFrame(
    index=pd.DatetimeIndex,   # Asia/Kolkata, normalized to 05:30 for daily candles
    columns=[
        "open",              # float
        "high",              # float
        "low",               # float
        "close",             # float
        "volume",            # int
        "oi",                # int — Open Interest
        "underlying_price",  # float — spot price for that day (from stock.priceData)
                             #         falls back to futures close if spot unavailable
    ]
)
```

> **Positional**: ~55 rows after startup noise is filtered (OI > 5% of contract max). Used by `FuturesAnalyser` positional methods with 10-day and 20-day windows.
>
> **Intraday**: grows by 1 row per 5-min cycle. Session-open OI cached in `FuturesAnalyser._session_open_oi` for the OI-from-open analysis.

### Also in `stock.zerodha_ctx`

```python
stock.zerodha_ctx["futures_mdata"] = {
    "current": pd.DataFrame(columns=["instrument_token", "tradingsymbol", "expiry", ...]),
    "next":    pd.DataFrame(...),
}

stock.zerodha_ctx["option_chain"] = {
    "current": pd.DataFrame(columns=["instrument_token", "tradingsymbol", "expiry",
                                      "strike", "instrument_type", ...]),
    "next":    pd.DataFrame(...),
}

stock.zerodha_ctx["last_notification_time"] = datetime | None
```

---

## 9. yfinance — Historical Price Data

| | |
|---|---|
| **Files** | `intraday/intraday_monitor.py` |
| **Method** | `yfinance.download(symbols, period, interval, group_by, auto_adjust)` |
| **Startup (intraday mode)** | `period="1y"`, `interval="1d"` — for morning bias |
| **Startup (positional mode)** | `period="5D"`, `interval="1d"` — prev day OHLCV only |
| **Intraday loop** | `period="5d"`, `interval="5m"` — current day 5-min data |
| **Positional loop** | `period="2y"` or `"3y"`, `interval="1d"` — full history |

### Symbol Conventions

| Type | Format | Example |
|---|---|---|
| NSE equity | `{SYMBOL}.NS` | `RELIANCE.NS` |
| NSE index | `^{NAME}` | `^NSEBANK`, `^NSEI` |
| Commodity | `{CODE}=F` | `BZ=F` (Brent), `GC=F` (Gold) |
| Currency | `{PAIR}=X` | `USDINR=X` |
| US index | `^{NAME}` | `^GSPC` (S&P 500) |

### Stored in `stock`

```python
stock.priceData = pd.DataFrame(
    columns=["Open", "High", "Low", "Close", "Volume"],
    index=pd.DatetimeIndex  # Asia/Kolkata tz after conversion
)

stock.prevDayOHLCV = {
    "OPEN":   float,
    "HIGH":   float,
    "LOW":    float,
    "CLOSE":  float,
    "VOLUME": int,
}

stock.ltp             = float   # latest Close value (set by update_latest_data())
stock.ltp_change_perc = float   # % change vs prevDayOHLCV["CLOSE"]
stock.daily_hv        = float   # annualised HV (%) from daily closes, cached at morning bias
```

---

## 10. Stock Object — Full Context Summary

Complete picture of all fields on a `Stock` instance after a full data fetch cycle.

```python
class Stock:
    # Identity
    stockName:              str
    stock_symbol:           str             # e.g. "NIFTY", "RELIANCE"
    stockSymbolYFinance:    str             # e.g. "^NSEBANK", "RELIANCE.NS"
    is_index:               bool

    # Price
    ltp:                    float | None
    ltp_change_perc:        float | None
    daily_hv:               float | None    # annualised HV %, cached at morning bias
    prevDayOHLCV:           dict | None     # OPEN/HIGH/LOW/CLOSE/VOLUME
    priceData:              pd.DataFrame    # OHLCV from yfinance

    # Sensibull data (see sections 1–4)
    sensibull_ctx: {
        "last_fetch_time":    datetime | None,
        "current": {
            "underlying_info": dict | None,
            "stats":           dict | None,
            "per_expiry_map":  dict | None,
            "nse_stats":       dict | None,
        },
        "historical_data":    pd.DataFrame,      # rolling snapshots (30 pos / 5d intraday)
        "oi_chain":           dict | None,        # latest OI chain snapshot
        "oi_chain_history":   list[dict],         # up to 15 intraday snapshots
        "iv_chart_history":   pd.DataFrame,       # 2yr daily IV closes (positional only)
        "oi_history":         pd.DataFrame,       # ~181 daily OI/PCR rows (positional only)
    }

    # Zerodha live data (see sections 5–7)
    zerodha_data:           dict | None     # last equity/index tick (via TickStore delegate)
    options_live:           dict            # {strike: {CE: {...}, PE: {...}}} (via TickStore)
    options_aggregate:      dict            # aggregated options metrics (via TickStore)
    futures_live:           dict            # {current: {...}, next: {...}} (via TickStore)

    # Zerodha historical / metadata (see section 8)
    zerodha_ctx: {
        "last_notification_time": datetime | None,
        "option_chain": {
            "current": pd.DataFrame | None,   # instruments for current expiry
            "next":    pd.DataFrame | None,
        },
        "futures_mdata": {
            "current": pd.DataFrame | None,   # instrument_token + expiry
            "next":    pd.DataFrame | None,
        },
        "futures_data": {
            "current": pd.DataFrame,          # OHLCV+OI+underlying_price
            "next":    pd.DataFrame,
        },
    }

    # Analysis results (populated by analysers each cycle, reset before next cycle)
    analysis: {
        "Timestamp":   datetime | None,
        "BULLISH":     dict[str, namedtuple | list[namedtuple]],
        "BEARISH":     dict[str, namedtuple | list[namedtuple]],
        "NEUTRAL":     dict[str, namedtuple | list[namedtuple]],
        "NoOfTrends":  int,
        "ScoreResult": ScoreResult | None,   # populated after orchestrator scoring
    }
```

> **Multiple signals for the same type**: if `set_analysis()` is called twice with the same `analysis_type`, the second value is appended as a list (`[first, second]`). Formatters in `MessageFormatter` handle both single and list forms.

---

## 11. FuturesAnalyser Namedtuples

All namedtuples emitted by `FuturesAnalyser` and stored in `stock.analysis`:

### `FutureActionAnalysis` — `analyse_intraday_check_future_action`

```python
FutureActionAnalysis = namedtuple("FutureActionAnalysis", [
    "expiry",           # str: "current" or "next"
    "action",           # str: "long_buildup" | "short_buildup" | "short_covering" | "long_unwinding"
    "price_percentage", # float: % price change (curr vs prev candle)
    "oi_percentage",    # float: % OI change (curr vs prev candle)
    "score",            # int: 0–100 signal confidence score
    "confidence",       # str: "HIGH" | "MEDIUM" | "LOW" | "VERY_LOW"
])
# Sentiment: BULLISH (long_buildup, short_covering), BEARISH (short_buildup, long_unwinding)
# Signal type key: "FUTURE_ACTION" (base), also "FUTURE_ACTION_LONG_BUILDUP", etc.
# Mode: @both (intraday + positional)
```

### `FuturesPVOPattern` — `analyse_intraday_price_volume_oi_pattern`

```python
FuturesPVOPattern = namedtuple("FuturesPVOPattern", [
    "pattern",          # str: e.g. "price_up_vol_oi_incr", "price_flat_vol_oi_dec"
    "price_pct",        # float
    "vol_pct",          # float
    "oi_pct",           # float
    "expiry",           # str: "current"
    "mtf_aligned",      # bool: multi-timeframe trend aligned
    "score",            # int: 0–100
    "confidence",       # str: "HIGH" | "MEDIUM" | "LOW" | "VERY_LOW"
])
# Sentiment: BULLISH / BEARISH / NEUTRAL depending on pattern
# Signal type key: "FUTURE_PVO_PATTERN"
# Mode: @both
```

### `FuturesBreakoutPattern` — `analyse_intraday_breakout_oi_confirmation`

```python
FuturesBreakoutPattern = namedtuple("FuturesBreakoutPattern", [
    "pattern",          # str: "orb_breakout_up_oi_confirmed" | "orb_breakout_down_oi_confirmed"
    "orb_high",         # float: high of opening range
    "orb_low",          # float: low of opening range
    "last_close",       # float
    "oi_pct",           # float: OI change %
    "vol",              # float: last bar volume
    "vol_avg",          # float: rolling average volume
    "oi_confirm",       # bool
    "vol_confirm",      # bool
    "expiry",           # str: "current"
    "mtf_aligned",      # bool
    "score",            # int: 0–100
    "confidence",       # str
])
# Sentiment: BULLISH (up) or BEARISH (down)
# Signal type key: "FUTURE_BREAKOUT_PATTERN"
# Mode: @intraday only
# Fires at most once per direction per session (_orb_fired_up/_orb_fired_down dedup flags)
```

### `FuturesOITrend` — `analyse_positional_oi_trend`

```python
FuturesOITrend = namedtuple("FuturesOITrend", [
    "action",           # str: "LONG_BUILDUP_TREND" | "SHORT_BUILDUP_TREND" |
                        #       "SHORT_COVERING_TREND" | "LONG_UNWINDING_TREND"
    "oi_chg_10d",       # float: % OI change over 10 days
    "oi_chg_20d",       # float | None: % OI change over 20 days (None if insufficient data)
    "price_chg_10d",    # float: % price change over 10 days
    "price_chg_20d",    # float | None
])
# OI threshold: >= 5% change over 10-day window
# Signal type key: "FUTURE_OI_TREND"
# Mode: @positional only
```

### `FuturesCostOfCarry` — `analyse_positional_cost_of_carry`

```python
FuturesCostOfCarry = namedtuple("FuturesCostOfCarry", [
    "action",           # str: "BACKWARDATION" | "HIGH_COST_OF_CARRY" | "BASIS_EXPANDING"
    "basis_pct",        # float: (futures_close - spot) / spot * 100
    "basis_5d_mean",    # float: 5-day mean basis %
    "basis_trend",      # float: curr_basis - prev_basis (positive = expanding)
    "ann_coc",          # float | None: annualised cost of carry % (suppressed < 10d to expiry)
    "days_to_expiry",   # int | None
])
# BACKWARDATION: basis_pct < -0.05% — valid in both modes
# HIGH_COST_OF_CARRY: ann_coc > 15% — positional only
# BASIS_EXPANDING: near expiry + basis growing + conviction — positional only
# Signal type key: "FUTURE_COST_OF_CARRY"
# Mode: @both (backwardation intraday; all 3 signals positional)
```

### `FuturesRollover` — `analyse_positional_rollover_pressure`

```python
FuturesRollover = namedtuple("FuturesRollover", [
    "action",       # str: "ROLLOVER_ACTIVE" | "ROLLOVER_STARTING"
    "curr_oi",      # int: current contract OI
    "next_oi",      # int: next contract OI
    "ratio",        # float: curr_oi / next_oi
    "ratio_trend",  # float: current ratio - ratio 5 days ago (negative = rollover accelerating)
])
# ROLLOVER_ACTIVE:   ratio < 2x
# ROLLOVER_STARTING: ratio 2–4x AND ratio_trend < -0.5
# Sentiment: always NEUTRAL — contextual only
# Signal type key: "FUTURE_ROLLOVER"
# Mode: @positional only
```

### `FuturesOIFromOpen` — `analyse_intraday_oi_buildup_from_open`

```python
FuturesOIFromOpen = namedtuple("FuturesOIFromOpen", [
    "action",               # str: "OI_BUILDUP_FROM_OPEN" | "OI_SHORT_BUILD_FROM_OPEN" |
                            #       "OI_UNWINDING_FROM_OPEN_UP" | "OI_UNWINDING_FROM_OPEN_DOWN"
    "oi_from_open_pct",     # float: % OI change from session open
    "price_from_open_pct",  # float: % price change from session open
    "open_oi",              # int: OI at session open (cached in _session_open_oi)
    "curr_oi",              # int: current OI
])
# OI threshold: >= 1.5% sustained buildup from open
# Session-open OI cached in FuturesAnalyser._session_open_oi on first call of the day
# Signal type key: "FUTURE_OI_FROM_OPEN"
# Mode: @intraday only
```

---

## 12. ANALYSIS_WEIGHTS

Full `ANALYSIS_WEIGHTS` dict from `common/constants.py` — weights used in the scoring engine:

```python
ANALYSIS_WEIGHTS = {
    # Technical Indicators
    "RSI":                          15,
    "RSI_CROSSOVER":                12,
    "MACD":                         15,
    "EMA_CROSSOVER":                12,
    "BOLLINGERBAND":                10,
    "VWAP_DEVIATION":               12,
    "ATR":                           8,
    "VOLUME":                       10,
    "VOLUME_BREAKOUT":              12,
    "OBV_DIVERGENCE":               16,
    "VOLUME_CLIMAX":                15,
    "BUY_SELL":                     10,
    "SUPERTREND":                   15,
    "RSI_DIVERGENCE":               18,
    "STOCHASTIC":                   12,
    "OBV":                          12,
    "PIVOT_POINTS":                 10,

    # Candlestick Patterns (backtest-optimised weights)
    "Single_candle_stick_pattern":       6,   # Marubozu — not reliable (PF 0.82)
    "Single_candle_reversal_pattern":    8,   # Hammer, Shooting Star — marginal (PF 1.03)
    "Double_candle_stick_pattern":      18,   # Engulfing, Piercing — most reliable (PF 1.06)
    "Double_candle_continuation_pattern": 4,  # 2 consecutive — not reliable (PF 0.96)
    "Triple_candle_stick_pattern":      16,   # Morning/Evening Star — reliable (PF 1.09)
    "Triple_candle_reversal_pattern":   16,   # Same split method
    "Triple_candle_continuation_pattern": 3,  # 3 consecutive — worst (PF 0.71)

    # Options & Derivatives
    "MAX_PAIN":                     15,
    "MAX_PAIN_TREND":               12,
    "MAX_PAIN_ALIGNMENT":           18,
    "PCR_EXTREME":                  14,
    "PCR_BIAS":                     10,
    "PCR_TREND":                    12,
    "PCR_INTRADAY_TREND":           13,
    "PCR_REVERSAL":                 16,
    "PCR_POS_REVERSAL":             17,
    "PCR_DIVERGENCE":               14,
    "IV_SPIKE":                     12,
    "IV_TREND":                     10,
    "IV_RANK":                      15,
    "IV_RANK_EXTREME":              18,

    # Futures
    "FUTURES_PREMIUM":              12,
    "OI_BUILDUP":                   14,
    "FUTURE_ACTION":                14,
    "FUTURE_ACTION_LONG_BUILDUP":   16,
    "FUTURE_ACTION_SHORT_BUILDUP":  16,
    "FUTURE_ACTION_SHORT_COVERING": 14,
    "FUTURE_ACTION_LONG_UNWINDING": 14,
    "FUTURE_BREAKOUT_PATTERN":      15,
    "FUTURE_BREAKOUT_CONFIRMED":    18,
    "FUTURE_BREAKOUT_MTF_ALIGNED":  20,
    "FUTURE_PVO_PATTERN":           10,
    "FUTURE_PVO_BUILDUP":           12,
    "FUTURE_SIGNAL_SCORE_HIGH":     20,
    "FUTURE_SIGNAL_SCORE_MEDIUM":   15,
    "FUTURE_SIGNAL_SCORE_LOW":      10,
    "FUTURE_OI_TREND":              16,
    "FUTURE_COST_OF_CARRY":         14,
    "FUTURE_ROLLOVER":               8,
    "FUTURE_OI_FROM_OPEN":          15,

    # OI Chain Analysis
    "OI_SUPPORT_RESISTANCE":        14,
    "OI_WALL":                      13,
    "OI_SHIFT":                     13,
    "OI_INTRADAY_TREND":            15,
    "OI_SR_SHIFT":                  14,
    "OI_CAPITULATION":              16,
    "OI_WALL_MIGRATION":            15,
    "OI_POSITIONAL_TREND":          16,
    "OI_ACCELERATION":              17,

    # IV vs HV
    "IV_PREMIUM":                   18,

    # Composite panic
    "PANIC_MODE":                   22,
    "PANIC_EXHAUSTION":             25,

    # Price levels
    "52-week-high":                  8,
    "52-week-low":                   8,

    # Default for unregistered types
    "DEFAULT":                      10,
}
```

### Scoring constants

```python
MIN_NOTIFICATION_SCORE = 110    # Minimum score to send Telegram alert

NOTIFICATION_PRIORITY = {
    "LOW":      35,    # 3–4 aligned signals
    "MEDIUM":   60,    # 4–5 aligned signals
    "HIGH":     90,    # Strong cross-category confirmation
    "CRITICAL": 130,   # Overwhelming conviction across 3+ categories
}

SIGNAL_ALIGNMENT_BONUS = {
    "ALL_BULLISH":    1.3,   # All signals bullish — 30% bonus
    "ALL_BEARISH":    1.3,   # All signals bearish — 30% bonus
    "MIXED":          1.0,   # Mixed signals — no bonus
    "CONFIRMATION":   1.5,   # Technical + Options aligned — 50% bonus
}

NEUTRAL_EXCLUDE_FROM_SCORE = {
    "MAX_PAIN_ALIGNMENT",    # When DIVERGENT — conflicting signals
    "MAX_PAIN_TREND",        # When DIVERGING — price moving away from max pain
    "OI_SUPPORT_RESISTANCE", # When neutral — informational S/R
    "OI_SR_SHIFT",           # When neutral — range squeeze/expand is informational
    "FUTURE_ROLLOVER",       # Context only — not directional
}
```

### Signal category sets (for alignment bonus detection)

```python
TECHNICAL_ANALYSES = {
    "RSI", "MACD", "EMA_CROSSOVER",
    "Single_candle_stick_pattern", "Single_candle_reversal_pattern",
    "Double_candle_stick_pattern", "Double_candle_continuation_pattern",
    "Triple_candle_stick_pattern", "Triple_candle_reversal_pattern",
    "Triple_candle_continuation_pattern",
    "SUPERTREND", "RSI_DIVERGENCE", "STOCHASTIC", "OBV", "PIVOT_POINTS",
}

OPTIONS_ANALYSES = {
    "MAX_PAIN", "MAX_PAIN_TREND", "MAX_PAIN_ALIGNMENT",
    "PCR_EXTREME", "PCR_BIAS", "PCR_TREND", "PCR_INTRADAY_TREND",
    "PCR_REVERSAL", "PCR_POS_REVERSAL", "PCR_DIVERGENCE",
    "IV_SPIKE", "IV_TREND", "IV_RANK", "IV_RANK_EXTREME",
    "OI_BUILDUP", "OI_SUPPORT_RESISTANCE", "OI_WALL", "OI_SHIFT",
    "OI_INTRADAY_TREND", "OI_SR_SHIFT",
    "OI_CAPITULATION", "OI_WALL_MIGRATION",
    "OI_POSITIONAL_TREND", "OI_ACCELERATION",
    "IV_PREMIUM",
    "PANIC_MODE", "PANIC_EXHAUSTION",
}

FUTURES_ANALYSES = {
    "FUTURES_PREMIUM", "FUTURE_ACTION", "FUTURE_ACTION_LONG_BUILDUP",
    "FUTURE_ACTION_SHORT_BUILDUP", "FUTURE_ACTION_SHORT_COVERING",
    "FUTURE_ACTION_LONG_UNWINDING", "FUTURE_BREAKOUT_PATTERN",
    "FUTURE_BREAKOUT_CONFIRMED", "FUTURE_BREAKOUT_MTF_ALIGNED",
    "FUTURE_PVO_PATTERN", "FUTURE_PVO_BUILDUP",
    "FUTURE_SIGNAL_SCORE_HIGH", "FUTURE_SIGNAL_SCORE_MEDIUM", "FUTURE_SIGNAL_SCORE_LOW",
    "FUTURE_OI_TREND", "FUTURE_COST_OF_CARRY", "FUTURE_ROLLOVER", "FUTURE_OI_FROM_OPEN",
}
```

---

## Key Notes

- **`prev_call_oi` / `prev_put_oi`** in `per_strike_data` are **previous day's closing OI** per strike. Only present when `show_prev_oi: true` (always the case in this codebase). Used by `OI_CAPITULATION` and `OI_WALL_MIGRATION` analysers.

- **`total_call_oi_change` / `total_put_oi_change`** at the OI chain payload level are always **vs yesterday's closing OI**. In intraday mode at 10 AM, these represent "OI added since yesterday's close through 10 AM". The value grows throughout the day. This is why `OI_BUILDUP_MIN_TOTAL_CHANGE_PCT` is lower in intraday mode (3%) than positional (5%).

- **`underlying_price` in `futures_data`**: populated from `stock.priceData` (yfinance daily closes mapped by date). Falls back to futures close when spot is unavailable. Used by `FuturesCostOfCarry` to compute basis. If `underlying_price == close` for all rows, cost-of-carry analysis is skipped.

- **`oi_history` vs `historical_data`**: `oi_history` contains ~181 days of multi-series daily data fetched once per positional cycle (compute_intraday 1D endpoint). `historical_data` is a rolling snapshot table appended every fetch cycle (30 rows positional, 5-day window intraday).

- **Intraday `futures_data` accumulates**: In intraday mode, `futures_data["current"]` grows by 1 row per 5-min cycle as new candles arrive. The `FuturesAnalyser` compares the last two rows for candle-to-candle signals, and uses the full session history for OI-from-open analysis.

- **`historical_data` column naming**: per-expiry columns use the expiry date with dashes removed as suffix (e.g. expiry `2026-05-29` → suffix `20260529`).
