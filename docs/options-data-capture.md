# Intraday options data capture (M2)

Status: **implemented** 2026-11-23 (chain/context/futures/index + manifest).
Remaining: GIFT Nifty subscription, signal/trade export, EOD summary.
Code: `services/market_data/chain_recorder.py`.

## Why

The live chain is overwritten every second, so nothing could be backtested or
measured beyond EOD Bhavcopy. This records what the pipeline currently throws
away, point-in-time, so strategies and signals can be evaluated on history.

## Choices (approved)

| Choice | Value |
|---|---|
| Chain snapshot cadence | **30 s** |
| Depth | L5 for **CORE** legs (|strike−spot| ≤ 1 %), L5 for futures 1 s |
| Retention | **180 days** hot |
| Format | **Parquet, zstd**, written directly on the server (`pyarrow`) |

## What is captured

| Tier | Rows | Cadence | Key columns |
|---|---|---|---|
| **chain** | one per option leg | 30 s | `snapshot_id, ts, ts_exchange, symbol, expiry, dte, strike, side, ltp, bid, ask, bid_qty, ask_qty, oi, open_oi, prev_oi, has_oi, volume, iv, iv_src, delta, gamma, theta, vega, iv_change, spot, is_core` + flattened `bid/ask_px_1..5, _qty_1..5, _ord_1..5` |
| **context** | one per symbol snapshot | 30 s | `snapshot_id, ts, symbol, spot, future_price, fut_oi, fut_volume, vix, atm_strike, atm_straddle, live_pcr, pcr_window_pct, max_pain_strike, expiry, dte, option_tick_count` |
| **futures** | one per contract | 1 s | `ts, symbol, expiry_key, ltp, oi, volume, change` + L5 depth columns |
| **index** | one per symbol | 10 s | `ts, symbol, ltp` (NIFTY/BANKNIFTY/SENSEX, INDIA_VIX, GIFTNIFTY when subscribed) |
| **eod manifest** | one per day | per flush | file/row counts, snapshots taken, expected snapshots |

Not captured (deliberate): every option tick, order-level data (unavailable),
full depth for all legs every second.

## Layout

```
data/options_history/
  chain/{SYM}/{YYYY-MM-DD}/{HHMM}.parquet
  context/{SYM}/{YYYY-MM-DD}/{HHMM}.parquet
  futures/{SYM}/{YYYY-MM-DD}/{HHMM}.parquet
  index/{YYYY-MM-DD}/{HHMM}.parquet
  eod/{YYYY-MM-DD}/manifest.json
```

- One file per 5-minute flush; atomic write (`*.tmp` → rename).
- 30 s × 600 legs × 750 snapshots ≈ **40–60 MB/day/index** compressed;
  futures ~2 MB/day; ~150–200 MB/day total. 180 days ≈ 30 GB (112 GB free).
- `data/options_history/` is git-ignored and pulled by `make backup`.

## Interpretation rules

- `has_oi = false` → quote-mode peripheral leg; `oi` is last-known. Exclude
  from OI-flow calculations.
- `open_oi` is the session baseline; a mid-session restart resets it (no
  `open_oi_source` column yet — assume session start when capture began before
  09:15, otherwise note the restart).
- `iv_src = "sensibull"` when enriched; empty means no IV was present (compute
  from mid offline if needed).
- `ts_exchange` vs `ts`: feed latency; use `ts_exchange` for event studies and
  flag gaps > 5 s.
- A leg missing from a snapshot means "not subscribed / no tick", not zero OI.
- Group analysis by `(date, expiry)`; moneyness is derived as
  `k = ln(strike / future_price)` from the context row.
- DTE 0 = expiry day; treat gamma/beta separately and exclude from
  multi-day statistics.
- 60 s/30 s cadence means intra-minute paths are not recoverable; only futures
  (1 s) have sub-minute resolution.

## How it runs

- Daemon thread inside `market-data`, active 09:15–15:30 on trading days.
- Reads in-process `TickStore` state (no Redis round-trip, no extra staleness)
  under the tick lock; never blocks the tick path.
- Flushes every 5 min and on shutdown/session end; stops itself below 5 GB free.
- Prunes capture directories older than 180 days once per day.
- `manifest.json` exposes capture quality (rows/files/snapshots vs expected).
- Pathological cases are caught and logged; capture failure never affects the
  live pipeline.

## Operations

- **Dependency:** `pyarrow==23.0.1`. `scripts/deploy.py` now runs
  `pip install -r requirements*.txt` on the server when a pull changes them.
- Disk: stop threshold 5 GB; alert via log (resource-monitor also watches disk).
- `make backup` pulls the tree; schedule it daily.

## How the data is used

1. **Signal outcome measurement** — forward returns at +15 m / +60 m / EOD and
   ATM-straddle change per signal → hit rate, expectancy; labels for X8.
2. **Strategy backtests** — delta condors, strangles, debit spreads, calendars
   with bid/ask fills and the paper-trading cost model.
3. **Indicator calibration** — IV rank, VRP, term structure, 25Δ skew, OI
   build-up, expected move vs realised.
4. **Execution research** — modelled vs book-walk fills, slippage by strike,
   liquidity band and time of day.
5. **Risk/stress** — reprice books on recorded paths; max adverse excursion.
6. **Model monitoring** — PINN/SSVI fair-vs-market IV and arbitrage audits.

## Remaining work

- **GIFT Nifty subscription**: recorder already captures it when the tick
  object exists; the NSEIX token registration/subscription is still to wire.
- **Signal/trade export (Tier E)**: signals live in Redis streams and trades in
  SQLite; add a daily exporter so outcomes can be joined to `signal_id`.
- **EOD summary (Tier F)**: daily per-symbol summary (ATM IV, IV rank, PCR,
  max pain, VIX) derived from the raw files.
