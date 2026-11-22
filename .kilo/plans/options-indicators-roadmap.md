# Index options: missing indicators & strategies — implementation spec

Audience: an implementing model working ONE task at a time. Each task lists
inputs that ALREADY exist in Redis, the formula, where to put the code, the
output contract, and tests. Do not start a strategy task (S*) before the
measurement tasks (M1, M2) exist — otherwise there is no way to tell whether
it makes money.

## Ground rules for every task

- Read `CLAUDE.md` first. Logging: `from lib.logging_util import get_logger`;
  DEBUG for gates/thresholds, INFO for emitted signals.
- New analysers follow the existing pattern in
  `services/analysis_engine/analyser/Analyser.py`: subclass, use the
  `@index_intraday` / `@index_positional` / `@index_both` decorators, write
  into `stock.analysis["BULLISH" | "BEARISH" | "NEUTRAL"][KEY]`.
  Register in `services/analysis_engine/main.py` (order matters: composites
  last). Add the weight for every new key in `common/constants.py`.
- Indices in scope: `common/constants.py:LIVE_OPTIONS_INDICES`
  (NIFTY, BANKNIFTY, SENSEX).
- One task per commit. Add unit tests with synthetic fixtures
  (see `tests/analyser/conftest.py`). Run
  `PYTHONPATH=. .venv/bin/python -m pytest tests -q` before finishing.
- Never touch `services/paper_trading/engine.py` exit rules or
  `strategy_builder.py` sizing in an indicator task.
- Numbers below are starting thresholds, not truths. Every threshold must be
  a named constant so M2 can tune it.

## Data already available (do not re-fetch)

| Key | Useful fields |
|---|---|
| `data:options_live:{SYM}` | per `"{strike}_{CE\|PE}"`: `ltp, bid, ask, oi, open_oi, has_oi, volume, delta, gamma, theta, vega, iv, iv_change, depth` (nearest expiry only) |
| `data:options_agg:{SYM}` | `atm_strike, atm_straddle_premium, atm_iv, atm_iv_percentile, iv_skew, live_pcr, pcr_window_pct, max_pain_strike, future_price, expiry, net_ce/pe_oi_change` |
| `data:sensibull:{SYM}` | `current_json.stats.per_expiry_map[exp].{atm_iv, atm_iv_percentile, pcr, max_pain_strike, future_price}` **for every expiry**; `iv_chart_history_json` (~2y daily ATM IV); `oi_history_json` (~181d OI/PCR); `oi_chain_json` (±20 strikes) |
| `data:zerodha:{SYM}` | futures current/next: 5-min bars (intraday), 90 daily bars (positional), OI |
| `data:futures_live:{SYM}` | current/next futures `ltp, oi, volume` |
| `data:tick:{SYM}` | spot, incl. `INDIA_VIX` |
| `data/pinn_training/bhavcopy/*.csv` | full EOD chain, **all strikes, all expiries**, settlement + OI + volume (NIFTY, BANKNIFTY) |
| post-market `F&O participant OI` | FII/Pro/Client index futures & options positioning (`services/orchestrator/post_market_analysis/analysis.py`) |

---

## Phase M — measurement (do these first)

### M1. Intraday options-chain recorder
Why: there is no intraday history, so no intraday strategy can be backtested.
- New module `services/market_data/chain_recorder.py`, started from
  `services/market_data/main.py` as a thread.
- Every 60 s during 09:15–15:30 on trading days, for each live index, read
  `data:options_live:{SYM}` + `data:options_agg:{SYM}` + spot + VIX and append
  one row per strike/side to `data/options_history/{SYM}/{YYYY-MM-DD}.parquet`
  (columns: ts, spot, future, expiry, strike, side, ltp, bid, ask, oi,
  open_oi, volume, iv, delta, gamma, theta, vega, vix).
- Buffer in memory, flush every 5 min (atomic write to temp then rename).
- Test: feed a fake Redis, assert rows/columns and that a flush survives.
- Acceptance: one trading day produces ~375 snapshots per index, < 50 MB/day.

### M2. Signal outcome logger + options backtester
Why: thresholds everywhere are unvalidated; nothing measures hit rate.
- `tools/options_backtest/outcomes.py`: consume `analysis:results` and
  `intelligence:confluence` (read from Redis stream history) and record, per
  signal, the forward spot/futures return at +15 m, +60 m, EOD and the ATM
  straddle change. Output a CSV and per-signal-key hit rate / mean return.
- `tools/options_backtest/engine.py`: replay strategies on
  (a) Bhavcopy for EOD/positional structures, (b) M1 parquet for intraday.
  Must reuse `services/paper_trading/models.compute_transaction_costs` and
  bid/ask fills (or LTP ± slippage) so results match paper trading.
- Report per strategy: trades, win rate, avg win/loss, expectancy, max
  drawdown, profit factor, worst day.
- Acceptance: reproduces a hand-computed iron-condor P&L on a fixture day.

---

## Phase I — indicators you are missing

Priority = value for an index option trader ÷ effort. All are pure maths on
data you already have.

### I1. True IV Rank + VIX regime  (priority: highest, effort: S)
- IV Rank = (IV_now − min_252d) / (max_252d − min_252d) × 100 from
  `iv_chart_history_json`. Keep IVP alongside (you only have IVP today).
- VIX regime from `data:tick:INDIA_VIX` + 20-day history:
  `LOW` (< 12), `NORMAL` (12–18), `HIGH` (18–25), `PANIC` (> 25), plus
  `VIX_RISING` if 1-day change > +8 %.
- Emit NEUTRAL `IV_RANK_TRUE` {iv_rank, ivp, iv_now} and `VIX_REGIME`
  {regime, vix, change_1d}. Publish both into `data:options_agg:{SYM}` too
  (paper trading will gate on them).
- Use: sell premium when IVR > 50 and VIX not rising; buy debit/long vol when
  IVR < 20.

### I2. Volatility risk premium (VRP) with a proper realised vol  (S)
- Realised vol from futures 5-min bars: Garman–Klass (uses OHLC) and
  close-to-close, annualised (√(252·75)); and 10/20-day daily RV.
- VRP = ATM IV − RV_20d (vol points) and ratio IV/RV. Replace/extend the
  existing `IV_PREMIUM` logic in `IVAnalyser.py`.
- Key: `VRP` {iv, rv_gk, rv_cc_20d, vrp_pts, ratio, zone}. Selling edge is
  historically strongest when VRP is positive and stable.

### I3. IV term structure  (S)
- From `per_expiry_map` take ATM IV for the nearest 3 expiries. Compute
  slope = IV_next − IV_near (vol points) and ratio.
- States: `CONTANGO` (normal, near < next), `BACKWARDATION` (near > next by
  > 2 pts = event/stress), `FLAT`.
- Key `IV_TERM_STRUCTURE` {near, next, far, slope, state}. Feeds S4
  (calendars) and event detection.

### I4. Delta-based skew: 25Δ risk reversal & butterfly  (M)
- From per-strike `delta` + `iv` (nearest expiry): interpolate IV at
  +25Δ call and −25Δ put (linear in delta between bracketing strikes).
- RR25 = IV(25Δ put) − IV(25Δ call); BF25 = (IV25p + IV25c)/2 − ATM IV.
- Track z-score of RR25 vs its own 20-day history (store daily close in a
  Redis list `data:skew_history:{SYM}`).
- Key `SKEW_25D` {rr25, bf25, rr25_z, state: PUT_RICH/CALL_RICH/NORMAL}.
  Replaces the adjacent-strike `iv_skew` for decisions. This is what
  SKEW_FADE should actually be using.

### I5. Per-strike OI build-up classification  (M)
- For each strike/side, compare `ltp` vs session-open price and `oi` vs
  `open_oi` (now published):
  price↑ OI↑ = LONG_BUILDUP, price↓ OI↑ = SHORT_BUILDUP (writing),
  price↑ OI↓ = SHORT_COVERING, price↓ OI↓ = LONG_UNWINDING.
- Aggregate: total fresh CE writing vs PE writing near ATM (±5 strikes),
  and the strike with the biggest fresh writing on each side
  ("live resistance/support").
- Key `OPTION_OI_BUILDUP` {ce_writing, pe_writing, ce_covering, pe_covering,
  top_ce_write_strike, top_pe_write_strike, bias}. Directional: PE writing
  dominant → BULLISH; CE writing dominant → BEARISH.
- Needs the session-open price per leg: add `open_ltp` alongside `open_oi`
  in `lib/zerodha/tick_store.py` (same pattern).

### I6. Expected move, probability of profit & strike placement helper  (S)
- 1σ move to expiry = S · IV · √(T) with T in years (trading days/252);
  daily = S · IV/√252. Also straddle-implied move (≈ straddle × 1.25 ≈ 1σ).
- Per strike: P(ITM at expiry) ≈ |delta| (or N(d2)); P(touch) ≈ 2·P(ITM).
  POP for a short strangle ≈ 1 − P(ITM_call) − P(ITM_put).
- Module `lib/fno/option_math.py` (pure functions + tests). Used by every
  strategy task to pick strikes by delta/probability instead of fixed gaps.
- Publish `expected_move_1d`, `expected_move_expiry` into `options_agg`.

### I7. Volume PCR & premium-weighted (turnover) PCR  (S)
- Volume PCR = Σ PE volume / Σ CE volume; turnover PCR = Σ(PE vol·ltp) /
  Σ(CE vol·ltp), within ±5 % of spot. Intraday flow is better captured by
  volume than OI.
- Key `PCR_FLOW` {oi_pcr, vol_pcr, turnover_pcr, divergence}. Flag when
  OI-PCR and volume-PCR disagree strongly.

### I8. Proper GEX zero-gamma level + charm/vanna on expiry  (M)
- Re-price gamma across a spot grid (spot ± 3 %, 0.1 % steps) with
  Black–Scholes gamma from each strike's IV, sum γ·OI·S²·0.01, find the
  zero crossing = true gamma flip. Replace the cumulative-from-ATM heuristic
  in `GEXAnalyser.py`.
- On expiry day add charm (∂delta/∂t) exposure: large negative charm into the
  close = hedging pressure.
- Keys: update `GEX_REGIME` with `zero_gamma_level`, `distance_pct`; new
  `EXPIRY_HEDGE_FLOW` on DTE 0.

### I9. Straddle decay vs realised move (seller's scorecard)  (S)
- Intraday: track ATM straddle from the open; compare actual move from open
  (|spot − open|) to the opening straddle. Ratio < 0.5 by 13:00 → range day
  (sellers winning); > 1.0 → trend day (stop selling, consider debit).
- Key `STRADDLE_SCORECARD` {open_straddle, current_straddle, decay_pct,
  realised_move, move_to_straddle_ratio, day_type}.

### I10. Market regime classifier (trend vs range)  (M)
- On futures 5-min and daily bars: ADX(14), ATR(14)/price, efficiency ratio
  (|net move| / Σ|moves|), and position vs VWAP.
- `TREND` if ADX > 25 and ER > 0.4; `RANGE` if ADX < 20 and ER < 0.3; else
  `MIXED`. Combine with I1 VIX regime and I8 GEX sign.
- Key `MARKET_REGIME` {regime, adx, er, atr_pct, gex_sign, vix_regime}.
  This is the single most important gate for choosing sell vs buy
  strategies.

### I11. Futures basis & rollover  (S)
- Basis % = (future − spot)/spot, annualised carry vs risk-free; rollover
  % = next-OI / (current+next OI) in the last 5 days before monthly expiry.
- Abnormal discount/premium is a positioning tell. Extend
  `Futures_Analyser.FUTURE_COST_OF_CARRY` if present rather than duplicate.

### I12. FII positioning daily bias  (S)
- From participant OI: FII index-futures net long %, FII index call/put net
  positions; 5-day change.
- Key `FII_POSITIONING` {fut_long_pct, fut_long_pct_chg5, call_net, put_net,
  bias}. Positional only; feed next morning's bias.

### I13. Breadth & heavyweight confirmation  (M)
- For NIFTY/BANKNIFTY constituents (equity ticks / analysis already in the
  system — verify availability): advance/decline ratio, % above VWAP, and
  weighted contribution of the top 5 heavyweights.
- Key `INDEX_BREADTH` {adr, pct_above_vwap, heavyweight_contrib, divergence}.
  Divergence (index up, breadth down) is a fade signal.

### I14. Event calendar  (S)
- `common/event_calendar.py` with a maintained YAML/JSON list: RBI policy,
  Union Budget, FOMC, US CPI, India CPI, monthly expiry, election results,
  index rebalancing. Function `events_within(date, days)`.
- Key `EVENT_RISK` {next_event, days_to_event, severity}. Gate: no short
  vol across a HIGH-severity event; I3 backwardation + event = expected.

### I15. Liquidity filter  (S)
- Bid-ask spread % = (ask − bid)/mid per strike (now published). Strikes with
  spread > 3 % or OI below a floor are untradeable.
- Helper in `lib/fno/option_math.py`; every strategy must call it before
  choosing a strike.

---

## Phase S — strategies you are missing

Implement in `services/paper_trading/strategy_builder.py` (strike selection
via I6 + I15) and route from `signal_router.py`. Each must define: entry
gate, strikes, exits, max loss, and be backtested (M2) before going live in
paper.

| ID | Strategy | Entry gate (uses) | Strikes | Exit |
|---|---|---|---|---|
| S1 | **Delta iron condor** (replace OI-wall IC) | I10 RANGE, I1 IVR > 50, VIX not rising, no I14 event, I2 VRP > 0 | short ±16Δ, long ±5Δ | 50 % credit; short Δ > 30; 2× credit stop; exit DTE ≤ 1 |
| S2 | **Expected-move strangle with adjustments** | same as S1, VIX regime NORMAL | short at ±1σ (I6) | roll untested side in when tested side Δ > 0.30; 50 % target |
| S3 | **Directional debit spreads** | I10 TREND + futures ORB/buildup + I5 bias | buy 50Δ, sell 25Δ, same expiry | 2× debit target / 50 % stop / time stop |
| S4 | **Calendar spread** | I3 BACKWARDATION ≥ 2 pts (or event-driven near IV spike), spot near ATM | sell near ATM, buy next-week ATM | +25 % of debit; spot move > 1σ; close before near expiry |
| S5 | **Expiry-day iron fly / butterfly** | DTE 0, I8 positive GEX, spot within 0.3 % of max-OI/max-pain strike, before 11:00 | centre at pin strike, wings ±1σ_day | flat by 14:30; 1× credit stop; tiny size |
| S6 | **Event long straddle / strangle** | I14 event in 1–3 days, I1 IVR < 30 | ATM straddle or ±25Δ strangle | exit before/at event; 30 % gain or 40 % loss |
| S7 | **Post-event short vol** | event passed, IV crush detected (`IV_CRUSH`), I10 not TREND | S1 structure | as S1 |
| S8 | **Jade lizard / broken-wing put fly** | I4 PUT_RICH (rr25_z > 1.5), mild bullish bias | short put 25Δ + short call spread with no upside risk | 50 % credit; put Δ > 40 |
| S9 | **Trend-day stand-down rule** (risk, not entry) | I9 move/straddle > 1.0 or I10 TREND | — | block new short-vol entries; tighten stops on open ones |

Portfolio rules to add with S1 (in `signal_router.py`):
- Net portfolio delta limit per index (e.g. |Σ delta × lot| < 0.3 lots
  equivalent per ₹10L) and vega limit; reject entries that breach.
- Max loss per trade 1–2 % of capital, total open max loss ≤ 6 %.
- No opposite directional structures on the same index simultaneously.

---

## Suggested order (each is one small PR)

1. M1 chain recorder (start collecting data immediately — it compounds)
2. I6 option_math + I15 liquidity helper (used by everything)
3. I1 IV rank + VIX regime, I10 market regime, I14 event calendar (the gates)
4. M2 outcome logger + backtester
5. I2 VRP, I3 term structure, I4 25Δ skew, I5 per-strike buildup, I9 scorecard
6. S1 delta iron condor + portfolio rules → backtest → paper
7. S3 debit spreads, S9 stand-down → S2, S5, S4, S6/S7, S8
8. I7, I8, I11, I12, I13 as refinements
9. Phase O (order book) after M1 is extended to record depth (O0)
10. Phase X (institutional) items as capacity allows; X1, X4, X6, X8 first

---

## Phase O — order-book (microstructure) signals

### What is realistically possible with this data

Zerodha KiteConnect `MODE_FULL` gives **5 bid + 5 ask levels** (price,
quantity, order count) per instrument, sent as throttled snapshots (not every
order, not tick-by-tick), with retail latency (tens to hundreds of ms).
Already parsed in `lib/zerodha/zerodha_ticker.py:829-843`.

- FEASIBLE: short-horizon pressure/imbalance features used as **confirmation
  and execution timing** (seconds to minutes) on index futures and ATM
  options.
- NOT FEASIBLE (needs exchange tick-by-tick / order-level data and
  co-location): market making, queue-position strategies, latency
  arbitrage, spoofing detection, true sweep detection. Do not attempt.

### O0. Capture depth (prerequisite)
- Publish futures depth: extend `services/market_data/snapshot_publisher.py`
  `data:futures_live:{SYM}` with `bid, ask, bid_qty_l1..l5, ask_qty_l1..l5,
  bid_px_l1..l5, ask_px_l1..l5, bid_orders_l1, ask_orders_l1` for current
  and next month (depth is already in the tick; it is dropped today).
- Keep `exchange_timestamp` / `last_trade_time` from the tick (used for
  staleness and trade classification).
- Extend M1 recorder: record futures L5 depth + ATM ±3 strikes depth every
  1 s (separate parquet `depth/{SYM}/{date}.parquet`). ~25k rows/day/inst.

### O1. Order-book imbalance (OBI) and microprice  (S)
- OBI_L1 = (bidQ1 − askQ1)/(bidQ1 + askQ1); OBI_L5 = same over Σ5 levels,
  optionally weighted 1/level.
- Microprice = (ask·bidQ1 + bid·askQ1)/(bidQ1 + askQ1); signal =
  (microprice − mid)/tick_size.
- Smooth with EMA(10 s). Emit futures `OB_PRESSURE` {obi_l1, obi_l5,
  microprice_edge, side} only when |OBI_L5| > 0.3 sustained ≥ 30 s.
- Use: confirm a directional entry (S3) or delay a short-vol entry into
  one-sided pressure.

### O2. Order-flow imbalance (OFI, Cont–Kukanov–Stoikov)  (M)
- Per snapshot: e_t = 1[bid_t ≥ bid_{t−1}]·bidQ_t − 1[bid_t ≤ bid_{t−1}]·bidQ_{t−1}
  − 1[ask_t ≤ ask_{t−1}]·askQ_t + 1[ask_t ≥ ask_{t−1}]·askQ_{t−1}.
- OFI over 1/5 min = Σ e_t, normalised by average depth. Empirically linear
  in the next short-horizon price change.
- Key `FUT_OFI` {ofi_1m, ofi_5m, z}. Strongest use: intraday entry timing
  and the trend-day stand-down (S9).

### O3. Trade aggressor classification for options flow  (M)
- Between snapshots, Δvolume at a leg classified by LTP vs previous bid/ask
  (Lee–Ready): LTP ≥ ask → buyer-initiated, ≤ bid → seller-initiated.
- Aggregate per strike: net aggressive buying in calls vs puts, premium
  bought vs sold (Δvol × ltp × lot).
- Key `OPTION_FLOW` {call_buy_prem, call_sell_prem, put_buy_prem,
  put_sell_prem, net_bias, top_strike}. "Unusual activity" =
  Δvolume at a strike > 5× its 20-snapshot average with one-sided aggressor.
- Combine with I5: OI↑ + seller-initiated = fresh writing (stronger S/R).

### O4. Depth walls & liquidity vacuums  (S)
- Large resting quantity at a level (> 5× median level size) within 0.2 %
  of price on futures = short-term support/resistance; absorption = price
  touches it repeatedly while quantity refills.
- Vacuum = thin total depth (Σ5 levels < 30 % of its intraday median) →
  expect slippage and fast moves; block market-style exits, widen limits.

### O5. Execution engine features (paper now, live later)  (M)
- Fill model: use L1–L5 to estimate fill price for the requested quantity
  (walk the book) instead of flat slippage; compare against M2 fills.
- Limit placement: start at mid, step toward touch every N s, cap at
  touch ± 1 tick; leg into spreads short-leg-last only when liquid; prefer
  exchange spread/basket orders when going live.
- Freeze-quantity slicing (NIFTY/BANKNIFTY/SENSEX freeze limits change —
  keep as config).

---

## Phase X — institutional-grade additions

| ID | Item | What it is | Data | Effort |
|---|---|---|---|---|
| X1 | **SSVI volatility surface** | Fit Gatheral SSVI (3 params/expiry, arbitrage-free by construction) to the live chain each minute; mispricing = market IV − surface IV per strike. Cheaper and more robust than PINN; it is the PINN doc's own Phase-4 baseline | Have (live chain, all expiries via Sensibull/Bhavcopy) | M |
| X2 | Vanna / charm / vega exposure (dealer positioning beyond GEX) | Σ OI × vanna (∂Δ/∂σ), charm (∂Δ/∂t) across strikes → flows from IV changes and time decay, strongest on expiry day | Have (BS greeks from IV) | M |
| X3 | Implied correlation & dispersion | Index IV vs weighted constituent IVs → implied correlation; high = sell index vol / buy single-stock vol (dispersion) | Have for F&O stocks (`data:sensibull:{stock}` ATM IV) + index weights (need weights file) | L |
| X4 | **Volatility forecasting** (HAR-RV, GARCH) | Forecast next-1/5-day realised vol from intraday RV; trade when IV − forecast RV is large (a forward-looking VRP) | Have (futures 5-min bars) | S |
| X5 | Scenario / stress P&L and VaR | Re-price the open book on spot ±1/2/3/5 % × IV ±5/10 pts × 1 day; CVaR of the book; block entries breaching limits | Have | M |
| X6 | **Greeks P&L attribution** | Daily P&L split into delta, gamma, theta, vega, residual → tells you *why* a strategy makes/loses money | Have | S |
| X7 | Seasonality / calendar effects | Day-of-week, expiry-week, time-of-day returns and straddle decay curves per index | Build from M1 + Bhavcopy | S |
| X8 | **Meta-labelling ML filter** | Train a LightGBM classifier on M2 outcomes to accept/reject each raw signal (features: regime, IVR, VRP, skew, OFI…). Cheap and effective; never predicts price directly | From M2 | M |
| X9 | Fractional-Kelly / risk-parity sizing across strategies | Allocate capital by each strategy's measured edge and correlation; cap at ¼ Kelly | From M2 | S |
| X10 | Cross-asset & global risk regime | GIFT Nifty gap, USDINR, US 10y, DXY, US VIX, crude; overnight gap model for positional short vol | Partially (yfinance in pre-market); **GIFT Nifty needed** | S |
| X11 | Participant positioning model | FII/Pro/Client index futures & options OI changes as daily bias (extends I12) | Have (NSE participant OI) | S |
| X12 | News / filings sentiment | NSE announcements, RBI/MPC statements summarised by the existing LLM; event severity tags for I14 | Free (NSE/RBI sites) | M |
| X13 | Data-quality monitor | Stale/locked quotes, crossed books, IV outliers, missing strikes → auto-disable strategies when data is bad | Have | S |

---

## Data requirements summary

| Data | Status | Source / note |
|---|---|---|
| Live chain (nearest expiry), greeks, IV, L5 options depth | Have | Zerodha WS + Sensibull |
| All-expiry ATM IV, IVP, PCR, max pain | Have | Sensibull `per_expiry_map` |
| Next-expiry live chain (calendars, term structure intraday) | Collected, not subscribed | Subscribe next-expiry tokens in `services/market_data/main.py` (watch the WS token limit) |
| Futures L5 depth | Received, not published | O0 |
| Intraday option chain history | **Missing** | M1 recorder (free, starts now). Expired-contract history needs a paid vendor (e.g. TrueData, Global Datafeeds) — only if you want years of intraday backtests immediately |
| EOD full chain history | Have (NIFTY/BANKNIFTY) | NSE Bhavcopy; **SENSEX needs BSE Bhavcopy** (free, add a fetcher) |
| India VIX history | Have (spot); history via NSE/yfinance | |
| Event / economic calendar | **Missing** | Maintain a JSON (RBI, Budget, FOMC, CPI, holidays, expiry) — free |
| GIFT Nifty | **Missing** | NSE IX; free sources are unreliable — consider a vendor feed or broker API |
| Index constituent weights | **Missing** | NSE monthly factsheets (free, monthly) — needed for X3/I13 |
| FII/DII, participant OI, bulk/block deals | Have / free | NSE daily reports |
| 20-level+ depth | Not available via KiteConnect | Kite API is 5 levels; some brokers offer deeper depth APIs — verify before relying on it |
| Tick-by-tick / order-level (MBO) data | Not feasible | Exchange TBT feed via authorised vendors + co-location; expensive and pointless at retail latency |

