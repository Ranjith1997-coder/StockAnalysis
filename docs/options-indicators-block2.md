# Block 2 design — volatility context: IV rank, VIX regime, term structure, expected move & POP

Status: **design** · 2026-11-24 · follows Block 1 (backtester, `b5bc7b0`)
Roadmap: I1, I3, I6 (and the measurement plumbing they need)

---

## 1. Purpose

Today's IV signals trust a vendor percentile (`atm_iv_percentile` from Sensibull)
and nothing measures *term structure* or *probability of profit*. Before any
strategy work, we need the volatility context to be (a) computed from data we can
audit, (b) visible to the backtester, and (c) convertible into strike placement
decisions that can be measured.

This block adds indicators only. **No live entry rule changes** — the POP gate in
§6 ships disabled and is turned on only after the Block 1 report justifies it.

## 2. What exists, and what is missing

| Piece | Today | Gap |
|---|---|---|
| IV percentile | `IVAnalyser.analyse_iv_rank` trusts Sensibull `atm_iv_percentile` | no local computation, no history, no VIX context |
| VIX | `PanicModeAnalyser` uses `app_ctx.india_vix_ltp` (level only) | no regime classification, no IV−VIX premium |
| Term structure | — | not computed anywhere; Sensibull publishes several expiries but analysis ignores all but nearest |
| Expected move | `LiveStraddleAnalyser.daily_expected_move()` (private) | not shared, not exposed per position |
| POP / strike placement | — | strikes chosen by fixed gap offsets (`select_strikes`) with no probability check |
| Backtest visibility | capture stores `vix`, `atm_strike`, `atm_straddle`, `max_pain` | no percentile, no second-expiry IV → these indicators would skip in replay |

## 3. Data prerequisite: four capture columns

The recorder runs inside market-data, which **does see** the data-gateway's
`data:sensibull:{SYM}` hash (per-expiry map for every expiry). Read it at capture
time and store the derived numbers, so replay has the same inputs as live.

Add to `CONTEXT_COLUMNS`:

| Column | Source | Meaning |
|---|---|---|
| `atm_iv_percentile` | nearest expiry `atm_iv_percentile` | vendor 252-day percentile (0–100) |
| `atm_iv_near` | nearest expiry `atm_iv` | ATM IV, % |
| `atm_iv_next` | next expiry `atm_iv` | ATM IV, % |
| `term_slope` | `atm_iv_next − atm_iv_near` | vol points (positive = contango) |

Changes: `ChainRecorder.__init__(objects, root, redis=None)`; `_capture_chain`
reads one hash per symbol per snapshot (cheap); nullable columns keep old days
readable. Update `docs/options-data-capture.md`, the contract test for the
capture schema, and `ReplayContext` to consume the new columns.

## 4. Pure math module — `lib/fno/option_math.py`

Single home for the formulas; everything else calls it.

```python
def daily_expected_move(straddle, dte_trading_days) -> float      # moved from LiveStraddleAnalyser
def expected_move_sigma(straddle) -> float                        # 1σ = straddle / 0.8
def d2(spot, strike, iv, tau, r=0.065, q=0.0) -> float
def prob_below(spot, strike, iv, tau) -> float                    # N(-d2)
def prob_above(spot, strike, iv, tau) -> float                    # N(d2)
def pop_short_put(strike, spot, iv, tau) -> float                 # 1 - P(S_T < K)
def pop_short_call(strike, spot, iv, tau) -> float                # 1 - P(S_T > K)
def pop_condor(put_k, call_k, spot, iv, tau) -> float             # P(put_k < S_T < call_k)
def iv_percentile(series, value, min_samples=20) -> float | None
def iv_rank(series, value) -> float | None
def suggest_condor_strikes(spot, gap, iv, tau, target_pop) -> tuple[float, float, float] | None
```

Rules:

- Reuse `norm_cdf`/`bs_price` from `tools/pinn_volatility/model/bs_utils.py`
  (no second Black-Scholes).
- `pop_condor` requires `put_k < call_k`; use per-strike IV when available, ATM
  IV otherwise.
- POP is the **risk-neutral** probability at expiry, documented as such — it is
  a pricing-implied number, not a forecast.
- `LiveStraddleAnalyser.daily_expected_move` becomes a re-export; the analyser's
  behaviour is unchanged (pinning test).

## 5. Indicators

### 5.1 `IVRegimeAnalyser` (I1)

Emits `NEUTRAL: IV_RANK` and `NEUTRAL: VIX_REGIME`.

`IV_RANK` fields: `ivp`, `ivp_source` (`local` | `vendor` | `insufficient`),
`iv`, `vix`, `iv_minus_vix`, `zone` (`VERY_LOW`/`LOW`/`NORMAL`/`HIGH`/`VERY_HIGH`).

- Prefer a **local** percentile from `iv_chart_history` (≥ 20 daily samples) over
  the vendor number; fall back to `atm_iv_percentile`, else report
  `insufficient` (no signal, debug log).
- Zones: <10 very low, <20 low, 20–70 normal, >70 high, >85 very high
  (same thresholds as the current analyser, so comparisons stay valid).
- `VIX_REGIME`: calm `<13`, normal `13–18`, elevated `18–25`, panic `≥25`;
  plus `vix_change_pct` from the session's first snapshot and `iv_minus_vix`
  (IV−VIX premium, vol points). Signal when `panic` or `iv_minus_vix > 3`
  (options richer than index vol) — text only, no trade.
- The existing `analyse_iv_rank` keeps emitting `IV_RANK`; the enhanced analyser
  replaces it in the orchestrator (old test updated deliberately).

### 5.2 `TermStructureAnalyser` (I3)

Emits `NEUTRAL: TERM_STRUCTURE`.

- From `per_expiry_map`: nearest vs next expiry ATM IV (skip expiries with dte<0).
- `slope = next − near` (vol points), `slope_pct = slope / near × 100`,
  `regime` = `CONTANGO` (`slope ≥ +0.25`), `BACKWARDATION` (`slope ≤ −0.25`),
  `FLAT` otherwise; `front_dte`, `back_dte`.
- Reports `event_week` when front DTE ≤ 2 (gamma week).
- Signal text: backwardation near expiry = event/stress pricing; contango = calm
  carry. Measurement only.
- Skips with `reason="single_expiry"` when the map has one entry (backtest days
  captured before the schema change, or the simulator before it publishes two).

### 5.3 `PlacementAnalyser` (I6)

Emits `NEUTRAL: EXPECTED_MOVE` and `NEUTRAL: POP_LADDER`.

- `EXPECTED_MOVE`: `straddle`, `daily_move` (reuses `daily_expected_move`),
  `sigma_1d` (= daily/0.8), `dte`, and the ±1σ price band around spot.
- `POP_LADDER`: for ±1..4 strike gaps (nearest expiry, ATM IV): POP of a short
  strangle at that width and of a condor with a 2-gap wing; `suggested` = the
  narrowest width meeting `target_pop` (default 0.70) and `target_pop_80`.
- Uses the chain if available for per-leg IV; otherwise ATM IV.

### 5.4 Position metadata (analysis only)

`strategy_builder.build_position` gains an optional `metrics: dict | None` out-param
(or returns a `BuiltPosition` wrapper) carrying `expected_move`, `pop`, `pop_condor`,
`term_regime`, `ivp`. The paper-trading path logs it and stores it in the position's
`signal_context` **only if** the existing schema already has room; otherwise log only.
No persistence schema change in this block.

## 6. Optional placement gate (off by default)

`PAPER_MIN_POP` (env/config, default `0.0`): when > 0, `select_strikes` widens the
short strikes (in gap steps) until `pop_condor ≥ PAPER_MIN_POP`, then applies the
normal wing rule. Default 0 reproduces today's behaviour exactly — proven by the
Block 1 live-vs-replay equality test still passing unchanged.

## 7. Simulator / capture support

- `tests/e2e/sim/market.py`: `term_slope_pct_per_week` scenario knob (default 0.30);
  `ContextPublisher` publishes a second expiry (`expiry + 7d`, IV × (1+slope)).
- `Harness` passes `redis` to `ChainRecorder` so the new context columns are
  populated in the recorded day.
- `ReplayContext` rebuilds `per_expiry_map` for both expiries from the new columns.
- Contract test: exact `CONTEXT_COLUMNS` tuple + doc mention; changing the schema
  requires updating both.

## 8. Tests (each task is not done without them)

| Area | Test |
|---|---|
| Math | hand-computed ATM straddle→daily move; d2/POP for a known (S,K,σ,T); monotonicity POP in strike distance; condor POP < single-leg POP; bounds 0..1 |
| IV percentile | synthetic 252-day series: rank/percentile exact; `min_samples` → None; vendor fallback; source field |
| Term structure | 2 expiries → contango/backwardation/flat boundaries; single expiry → skip reason; event_week |
| Placement | ladder widths monotone; suggested width meets target POP; no chain → ATM IV path |
| Capture | new columns round-trip; recorder with/without redis; old parquet still loads (nullable) |
| E2E | a scenario asserts `IV_RANK`, `VIX_REGIME`, `TERM_STRUCTURE`, `EXPECTED_MOVE`, `POP_LADDER` appear and are internally consistent |
| Regression | live-vs-replay equality (Block 1) still exact; existing IV tests updated deliberately |
| Gate off | `PAPER_MIN_POP=0` → identical trades to today |

## 9. Task breakdown (for hand-off)

| ID | Task | Depends | Acceptance |
|---|---|---|---|
| B2.1 | `lib/fno/option_math.py` + move `daily_expected_move` + tests | — | hand-computed values; no analyser behaviour change |
| B2.2 | Capture schema: 4 columns, recorder redis param, docs, contract test | — | new day has numeric columns; old day loads; replay sees them |
| B2.3 | `IVRegimeAnalyser` (local percentile, VIX bands, IV−VIX) + formatter + tests | B2.1 | zones/percentiles exact on fixtures; source field correct |
| B2.4 | `TermStructureAnalyser` + formatter + tests | B2.2, B2.3 | 2-expiry fixtures; skip reason on single expiry |
| B2.5 | `PlacementAnalyser` (expected move, POP ladder, suggestions) + formatter | B2.1 | monotone POP; suggested width meets target |
| B2.6 | `strategy_builder` metrics out-param + optional `PAPER_MIN_POP` gate | B2.5 | gate off = identical trades; gate on widens strikes |
| B2.7 | Simulator 2-expiry publishing + Harness redis wiring + E2E assertions | B2.2, B2.4 | E2E scenarios green with new keys |
| B2.8 | Register analysers in orchestrator (worker main, E2E, backtest), docs, replay check | B2.3–B2.5 | replay equal; `make backtest` on an E2E day shows the new keys |

Estimated 3–3.5 h. Deploy at the end (holiday), then smoke.

## 10. Risks

| Risk | Mitigation |
|---|---|
| POP misread as real probability | documented as risk-neutral; label in the signal text |
| Local IV percentile noisy with short history | `min_samples=20`, report `source`, keep vendor fallback |
| Capture schema change breaks replay of old days | nullable columns; loader tolerates missing |
| Term structure unavailable in replay on old days | explicit `single_expiry` skip, not an error |
| Gate changes live behaviour | default off; equality test proves it |
| Duplicate math (straddle helper, BS) | one module, analname imports; pinning test on the old helper |
