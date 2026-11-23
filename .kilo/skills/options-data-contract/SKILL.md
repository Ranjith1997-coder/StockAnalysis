---
name: options-data-contract
description: "Redis contract for live index-options data in StockAnalysis. USE FOR: reading or writing options/futures data, new Redis field, producer/consumer mismatch, stale or missing option fields, PCR/OI semantics, Sensibull vs Zerodha ownership. DO NOT USE FOR: strategy rules or exit logic."---

# Live options data contract

**Rule: when you add or rename a field, update this file, the producer, and
every consumer in the same PR.** Field drift across services has caused real
bugs (OI units, `buy_qty` as a price, `max_pain` vs `max_pain_strike`,
`iv_percentile=None`).

## Scope

Live chains exist for `LIVE_OPTIONS_INDICES` = NIFTY, BANKFUT, SENSEX
(`common/constants.py`), **nearest expiry only**, strikes uploaded ATM ±5 %
(peripheral in quote mode). Index list excludes INDIA_VIX/FINNIFTY.

## Keys

### `data:options_live:{SYM}` — hash, 1 s, no TTL
Field `"{strike}_{CE|PE}"` → JSON:
`ltp, bid, ask, oi, prev_oi, open_oi, has_oi, volume, buy_qty, sell_qty,
timestamp, open, high, low, close, depth`, plus `delta, gamma, theta, vega,
iv, iv_change` when Sensibull is present.

- `buy_qty`/`sell_qty` are **quantities**; best prices are `bid`/`ask`.
- `has_oi=false` for quote-mode peripheral legs (OI is last-known, not fresh).
- `open_oi` is the session baseline (first non-zero OI of the day).
- Greeks/IV are only present when Sensibull enrichment is running.

### `data:options_agg:{SYM}` — hash, 1 s
`total_ce_oi, total_pe_oi, live_pcr, pcr_window_pct, pcr_strikes,
atm_strike, atm_straddle_premium, atm_iv_ce, atm_iv_pe, iv_skew,
max_oi_ce_strike, max_oi_pe_strike, max_pain_strike, future_price, expiry,
net_ce_oi_change, net_pe_oi_change, net_ce_oi_change_tick,
net_pe_oi_change_tick, atm_iv, atm_iv_percentile, atm_ivp_type, last_updated,
option_tick_count, tick_count`.

A **stale mirror** of the GEX fields is also published here:
`gex_total, gex_ce, gex_pe, gex_regime, gex_flip_level`. They are copied from
the TickStore by the 1 s publisher, so they lag the 5-min GEXAnalyser run — read
`data:gex_state:{SYM}` when the value must be authoritative.

- `net_ce/pe_oi_change` = **since session open**.
  `*_tick` = tick-to-tick (do not use for daily flow).
- PCR and OI totals count only legs with `oi > 0`; `pcr_window_pct` states the
  effective window (roughly ATM ±3 %).
- `expiry` is `YYYY-MM-DD` (the expiry the live chain belongs to).

### `data:futures_live:{SYM}` — hash, 1 s
`current_{ltp,oi,volume,change,buy_qty,sell_qty}` and `next_*`.
Depth (`bid/ask` + 5 levels) is added by the O0 task.

### `data:sensibull:{SYM}` — hash, ~5 min
`last_fetch_time, current_json, historical_data_json, oi_chain_json,
oi_chain_history_json, iv_chart_history_json, oi_history_json`.

- `current_json.stats.per_expiry_map[expiry]` has ATM IV, IV percentile, PCR,
  max pain, future price **for every expiry** — use this for term structure.
- `iv_chart_history_json` ≈ 2 years of daily ATM IV (IV rank input).
- `oi_history_json` ≈ 181 days of OI/PCR (positional).

### `data:tick:{SYM}` — hash, 1 s
`last_price, open, high, low, close, volume_traded, ...` (includes INDIA_VIX).

## Ownership (single-writer rule)

- `OPTIONS_SOURCE=zerodha` (default): Zerodha is the **full writer** of
  `options_live`/`options_agg`. Sensibull is **enrichment-only** — it may add
  greeks/IV only and must never overwrite `ltp`, `oi`, `prev_oi`, `timestamp`
  or recompute aggregates.
- `OPTIONS_SOURCE=sensibull`: Sensibull is the full writer.
- Producers: `services/market_data/snapshot_publisher.py`,
  `lib/zerodha/tick_store.py`, `lib/fno/sensibull_adapter.py`,
  `services/data_gateway/sensibull_fetcher.py`.

## Semantics that commonly trip consumers

- Nearest expiry only: positional trades on another expiry have **no live
  price** here. Check `agg.expiry` before pricing.
- On expiry day the PINN engine sees `tau = 0` and skips the symbol (no 0DTE
  surface).
- `live_pcr` is a windowed number, not the full-chain PCR (use Sensibull
  `oi_chain_json` for the full chain).
- GEX is computed in `data:gex_state:{SYM}`, not in `options_agg`.

## Debug

```bash
redis-cli HGETALL data:options_agg:NIFTY
redis-cli HKEYS data:options_live:NIFTY | head
redis-cli HGET data:options_live:NIFTY "24000.0_CE"
redis-cli --scan --pattern 'data:*NIFTY*'
redis-cli HGETALL service:registry:market-data
```
