# Index-options programme plan (living document)

Status: **draft for review** · Owner: Ranjith · Last updated: 2026-11-22

This is the working plan for evolving the index-options side of StockAnalysis
from a heuristic signal generator into a measured, risk-controlled decision
engine. Update the **Status** and **Progress log** as work lands. Task-level
implementation specs live in
[`options-indicators-roadmap.md`](./options-indicators-roadmap.md); this file
is the sequencing, gates and decisions.

Legend: ⬜ not started · 🟨 in progress · ✅ done · ⏸ blocked · ❌ dropped

---

## Guiding principles

1. **Measure before adding.** No strategy ships before M2 can show its
   out-of-sample hit rate, expectancy and drawdown.
2. **Paper only** until 8–12 weeks of paper results match the backtest.
3. **One task per PR**, tests included; full suite green before deploy.
4. **Thresholds are named constants**, tuned only on walk-forward
   out-of-sample data — never inline magic numbers.
5. **No paid data** until M1 shows a concrete gap that it would close.
6. **Order book and tick data have a hard ceiling** at retail latency:
   confirmation and execution only, never market making.

---

## Milestone 0 — Ship what is done (0.5 d) 🟨

**Status (2026-11-22):** 0.1 ✅ pushed; CI created and green (it caught two real
logger-format bugs, now guarded by `tests/test_logging_format.py`, and pinned
ruff for CI/local parity). 0.2 ✅ deployed `7ec9294` — all 11 services active,
heartbeats healthy. 0.3 ⬜ Monday session verification.

| # | Item | Output | Acceptance |
|---|---|---|---|
| 0.1 | Push fix commits `b55e04b` `988b379` `a5362c1` `5b93edf` + plans | master synced | full suite green |
| 0.2 | `make deploy`; restart market-data, analysis-engine, paper-trading | services on new commit | all 12 services active |
| 0.3 | Observe one trading session | session log review | composite cards render; paper fills use bid/ask; charges correct; daily counters reset 09:15 |

Blocking decision: deploy now or hold for diff review?

---

## Milestone 1 — Foundations (≈4 d) ⬜

| ID | Item | Files | Output | Acceptance | Effort |
|---|---|---|---|---|---|
| I6 | Option math: expected move (1d / to expiry), P(ITM)≈\|Δ\|, P(touch), POP, DTE annualisation | `lib/fno/option_math.py` (new) | pure functions | 10+ unit cases vs BS | 1 d |
| I15 | Liquidity filter: spread %, OI floor, tradable strikes | `lib/fno/option_math.py` | `is_tradable(leg)` | thin/crossed quote tests | 0.5 d |
| I1 | True IV Rank (252 d) + VIX regime (LOW/NORMAL/HIGH/PANIC/RISING) | `IVAnalyser.py`, `options_agg` | `IV_RANK_TRUE`, `VIX_REGIME` | synthetic IV history tests | 1 d |
| I10 | Market regime: ADX, efficiency ratio, ATR%, VWAP + GEX + VIX → TREND/RANGE/MIXED | `RegimeAnalyser.py` (new) | `MARKET_REGIME` | trend/range fixtures | 1 d |
| I14 | Event calendar (RBI, Budget, FOMC, CPI, expiry, rebalance) | `common/event_calendar.py`, `configs/events.json` | `EVENT_RISK` | date-window tests | 0.5 d |

**Gate M1:** review emitted regime values on one live session.

---

## Milestone 2 — Measurement (≈5 d) ⬜ · **start M1 capture immediately**

| ID | Item | Output | Acceptance | Effort |
|---|---|---|---|---|
| M1 | Intraday chain recorder, 60 s snapshots → parquet | `data/options_history/{SYM}/{date}.parquet` | 375 snapshots/day/index, <50 MB, restart-safe | 1.5 d |
| O0 | Publish + record futures L5 depth | `data/options_history/depth/...` | depth rows present, no CPU regression | 1 d |
| M2 | Options backtester + signal outcome logger (same cost model as paper) | `tools/options_backtest/` | reproduces hand-computed IC P&L on a fixture | 2.5 d |

**Gate M2:** report of current-signal hit rates before any strategy work.

---

## Milestone 3 — Volatility, skew, flow (≈4 d) ⬜

| ID | Item | Output | Effort |
|---|---|---|---|
| I2 | VRP: ATM IV − realised vol (Garman–Klass 20 d; close-to-close 10/20 d) | `VRP` | 1 d |
| I3 | Term structure (nearest 3 expiries) → CONTANGO/BACKWARDATION/FLAT | `IV_TERM_STRUCTURE` | 0.5 d |
| I4 | 25Δ risk reversal + butterfly, 20-day z-score | `SKEW_25D` | 1 d |
| I5 | Per-strike OI build-up + fresh writing strikes (needs `open_ltp`) | `OPTION_OI_BUILDUP` | 1 d |
| I9 | Straddle scorecard (opening vs current, realised move, day type) | `STRADDLE_SCORECARD` | 0.5 d |
| I7 | Volume + turnover PCR vs OI-PCR divergence | `PCR_FLOW` | 0.5 d |

**Gate M3:** each indicator must show out-of-sample edge in M2 or stay
context-only.

---

## Milestone 4 — Strategy v1 + portfolio risk (≈4 d) ⬜

| ID | Item | Detail | Acceptance |
|---|---|---|---|
| S1 | Delta iron condor | RANGE + IVR>50 + VRP>0 + VIX not rising + no high event + liquid. Short ±16Δ, long ±5Δ. Exit 50 % credit / short Δ>30 / max-loss stop / DTE≤1 | backtest 2 y + paper |
| R1 | Portfolio risk rules | net delta & vega limits; 1 % risk/trade; ≤6 % total open max loss; no opposing structures per index | rejection-path tests |
| S9 | Trend-day stand-down | block new short vol in TREND or move/straddle>1.0; tighten open stops | entry-block tests |

**Gate:** 4–6 weeks paper before evaluation.

---

## Milestone 5 — Strategy v2 (≈6 d) ⬜

| ID | Strategy | Key gate |
|---|---|---|
| S3 | Directional debit spreads | TREND + ORB/buildup + OI bias |
| S2 | Expected-move strangle with rolling | RANGE + IVR>50 |
| S5 | Expiry-day iron fly | DTE 0 + positive GEX + near pin |
| S4 | Calendar spread | backwardation ≥2 pts or event |
| S6/S7 | Event long straddle / post-event short vol | event + IVR |
| S8 | Jade lizard / broken-wing fly | PUT_RICH skew + mild bullish |

One at a time: backtest → paper → review.

---

## Milestone 6 — Order book & execution (≈7 d) ⏸ decision

| ID | Item | Detail | Acceptance |
|---|---|---|---|
| O1 | Book imbalance + microprice (futures) | EMA-smoothed, emit on sustained \|OBI\|>0.3 | measured lift in M2 |
| O2 | Order-flow imbalance (Cont–Kukanov–Stoikov) | OFI 1 m/5 m, z-scored | predicts 5-min return on recorded data |
| O3 | Options aggressor classification | Lee–Ready; premium bought/sold per strike | separates writing from covering |
| O4 | Depth walls + liquidity vacuums | alerts only | no auto-trading |
| O5 | Execution engine | walk-the-book fills, mid→touch limits, freeze slicing, liquid leg last | paper fills within X bps |

Blocking decision: now or after Strategy v1 is validated?

---

## Milestone 7 — Institutional analytics (≈12 d) ⬜

Recommended first: **X1 SSVI surface**, **X4 HAR-RV/GARCH forecast**,
**X6 Greeks P&L attribution**, **X8 meta-labelling filter**.
Then X2 vanna/charm, X5 stress/CVaR, X9 Kelly sizing, X7 seasonality,
X11 FII positioning, X13 data-quality kill switch.
Needs new data: X3 dispersion (index weights), X10 gap model (GIFT Nifty),
X12 news.

---

## Milestone 8 — Live execution (optional) ❌ until paper validates

Defined-risk only, manual confirmation, hard kill switch that flattens,
fill reconciliation, separate audit log. Requires broker access, capital
and explicit risk limits from the owner.

---

## Cross-cutting delivery rules

- One task per PR; task spec from `options-indicators-roadmap.md`.
- Synthetic-fixture unit tests; full suite green (currently 1894).
- New Redis keys documented in the task and README.
- Deploy after tests only; paper-only.
- Backtest before any strategy affects sizing.

---

## Open decisions

| # | Decision | Options | Recommendation | Status |
|---|---|---|---|---|
| D1 | Deploy Milestone 0 now? | yes / review diffs first | Push, let CI run, then deploy today (Sunday, market closed) and verify Monday | ✅ deployed 2026-11-22; Monday session verification pending (M0.3) |
| D2 | Paper capital & risk defaults | ₹10 L, 1 %/trade, 6 % open max loss | ₹10 L; 1 % risk/trade by **max loss**; 5 % total open max loss; max 6 positions, max 2 per index | ⬜ |
| D3 | Index priority | NIFTY+BANKNIFTY → SENSEX | Agree; add SENSEX after a BSE Bhavcopy fetcher exists | ⬜ |
| D4 | Order-book work timing | now / after Strategy v1 | Record depth early (O0 in M2); build O1–O5 only after S1 is validated | ⬜ |
| D5 | Buy expired-contract intraday history? | no (build own) / yes (vendor) | No — accumulate via M1; revisit after ~3 months of data | ⬜ |
| D6 | GIFT Nifty source | broker/vendor / free | **Kite NSEIX "GIFT NIFTY" index tick — `instrument_token` 1140, verified in the public instrument dump** (exchange `NSEIX`, type `EQ`, segment `INDICES`). Free with the existing broker connection: resolve the token from `instruments()` at startup, subscribe on the index feed, publish `data:tick:GIFTNIFTY` | ⬜ |
| D7 | Live execution target | paper-only / within 3 months | Paper-only for ≥3 months; revisit after Milestone 5 + 8–12 weeks paper | ⬜ |

Cross-cutting: fix the server's power reliability (battery/UPS) **before** any
live execution — the box currently has no runtime protection.

---

## Risks

| Risk | Mitigation |
|---|---|
| Current thresholds unvalidated | M2 tunes out-of-sample; constants only |
| Overfitting to 2026 | walk-forward; report OOS only |
| Expiry-day / survivorship bias | full Bhavcopy incl. expired series |
| Order-book expectations too high | 5-level retail snapshots: confirmation only |
| Data quality (stale/locked/crossed) | X13 kill switch; M1 raw audit trail |
| Scope creep | milestone gates |
| Paper ≠ live | O5 fill model; min size before scaling |

---

## Progress log

| Date | Milestone | Note |
|---|---|---|
| 2026-11-22 | M0 | Plan created; tooling (skills/commands/CI/backups) added |
| 2026-11-22 | M0 | CI green after fixing 2 logger-format bugs; fixed options/paper/analysis/data layers deployed (`7ec9294`); 11/11 services active. PINN: only an accepted BANKNIFTY model (2026-10-01) is loaded; recent nightly runs reject both symbols (NIFTY 4.77 %, BANKNIFTY 5.38 % wings) |
| 2026-11-22 | — | **History rewritten** to purge 254 MB of runtime artefacts accidentally committed; `.git` 86 MB → 7.8 MB, zero data blobs remain. All commit SHAs before this entry changed. Backup: `/Users/rkumark/StockAnalysis-pre-filter-20261122_1659.bundle` |
| 2026-11-22 | M0 | Versioning repaired: `dirty` ignores untracked data artefacts; `/version` covers all 11 services; deploy restarts every long-running service (auth included — it was wrongly treated as a oneshot); monolith heartbeat fixed (it used `time.time()` where `time` is `datetime.time`, so it had never been written) and now runs on a 60 s thread. All 11 report `1.0.0+c218df7`, clean |

---

## Related documents

- [`options-indicators-roadmap.md`](./options-indicators-roadmap.md) — task-level specs (I/M/O/S/X items)
- `docs/pinn-volatility-engine.md` — PINN model status and experiment log
- `docs/PAPER_TRADING_DESIGN.md` — paper trading design
