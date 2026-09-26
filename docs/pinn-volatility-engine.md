# PINN Volatility Engine — Complete Design & Implementation Plan

> **Status**: Phase 1 (data pipeline, model, training, acceptance gate, CLI) **merged to master**. Phase 2 part 1 (live inference service core: model loading, live-vs-fair comparison, signal emission) **merged to master**. **Paper-trading integration (Section 9) is fully built** (0.4.3, merged). **Nightly training scheduling is built** (0.4.4: `services/pinn_trainer/main.py`, self-scheduling — not a systemd timer, see 0.4.4's deviation note) but not yet deployed to the server. `/pinn_status` and the inference-service systemd unit are still unbuilt. As of the most recent scheduled run (2026-09-18), **no model is currently accepted for either symbol** — see 0.6.1, which means neither the paper-trading wiring nor the trainer's alerts have anything live to report on yet. Every tuning attempt made since (0.7's 4-flag bundle, the 0.7.1 per-flag isolation, and 0.7.2's log-τ input encoding) made accuracy *worse*, not better — none adopted. See Section 0 for full status, empirical deviations from this design, and next work items.
> **Created**: 2026-08-01
> **Last updated**: 2026-09-26 (nightly training scheduling: self-scheduling service, not a systemd timer)
> **Branches**: Phase 1 merged via `feature/pinn-volatility-engine` (PR #54); Phase 2 part 1 merged via `feature/pinn-inference-service` (PR #56); experimental flags + 08-11/08-25 content merged via `feature/pinn-experimental-flags` (PR #57); paper-trading integration merged via `feature/pinn-paper-trading-integration` (PR #58). PR #55 (superseded duplicate of the 08-11/08-25 content, folded into #57) should still be closed without merging — not yet done. `feature/pinn-log-tau-normalization` was implemented, validated, found to be a severe regression, and deleted without merging (see 0.7.2) — the finding is preserved in this doc, the code was not kept. Nightly training scheduling (0.4.4) is on an unmerged branch.
> **Prerequisites**: Paper trading service deployed (commits `ef19db1`–`2fd67a1`), analysis-engine with composite analyser, market-data WS pipeline, SPAN margin calculator

---

## 0. Implementation Status (as of 2026-09-18)

This section is a living summary maintained on top of the original design below. The original design (Sections 1–18) is kept as-written for historical reference — where implementation diverged from it, that's called out here and inline, not by silently editing the original numbers.

### 0.1 What's built (Phase 1 — complete)

All of Steps 1–10 from Section 14's phased plan are implemented, tested, and validated against real NSE Bhavcopy data (not just synthetic/mocked data) at every stage:

| Step | Module | Status |
|---|---|---|
| 1 | `model/bs_utils.py` — Black-Scholes price/IV-inversion/Vega | ✅ Done |
| 2 | `data/bhavcopy_fetcher.py` — NSE fetch + cache + holiday-aware | ✅ Done |
| 3 | `data/dataset.py` — Bhavcopy rows → `(k, τ, w, vega)` samples, walk-forward split | ✅ Done |
| 4 | `data/collocation.py` — seedable Latin Hypercube collocation sampling | ✅ Done |
| 5 | `model/pinn.py` — `VolatilityPINN` + `RawInputModel` + Fourier features | ✅ Done |
| 6 | `losses/arbitrage.py`, `losses/data_loss.py` — β-NLL, calendar, butterfly | ✅ Done |
| 7 | `losses/composite.py` — combines all loss terms | ✅ Done |
| 8 | `training/trainer.py` — Adam → L-BFGS, gradient clipping, seeded | ✅ Done |
| 9 | `training/validate.py` — `evaluate_holdout` + `audit_arbitrage` + `check_acceptance_criteria` | ✅ Done |
| 10 | `training/run_training.py` — CLI: fetch → dataset → train → gate → save/fallback | ✅ Done |

`tools/pinn_volatility/config.py` (`PINNConfig`) also now exists — the plan's original "Step 0", never built until this pass. **Not** yet wired into `VolatilityPINN`/`PINNTrainer` as their actual parameter source; it's a documented snapshot of current defaults, consumed by `run_training.py`.

**218 tests passing** (`tests/pinn/`, up from 206 — see 0.7 for the 12 new ones), plus repeated real (non-mocked) end-to-end runs against live NSE data throughout development, including multiple full live runs of the `run_training.py` CLI itself (2026-08-14, 08-25, and 09-18).

### 0.1b What's built (Phase 2, part 1 — live inference service core)

`services/volatility_engine/` exists and is merged (PR #56): `model_manager.py`, `comparator.py`, `signal_emitter.py`, `main.py`, 24 tests. Implements Section 8's design, adapted to this project's real Redis schema and `VolatilityPINN`/`normalize()` interfaces — the doc's original section 8 pseudocode assumed helpers/fields that don't exist as such (see deviations below). Specifically:

- **`ModelManager`** — loads `{symbol}_latest.pt` checkpoints, reconstructs the exact trained architecture from checkpoint metadata (`hidden_dim`/`num_layers`/`num_fourier_bands`), hot-reloads when `run_training.py`'s symlink is repointed (tracked by target filename, not mtime — same-second re-symlinks don't reliably change mtime on all filesystems).
- **`comparator.py`** — pulls live spot (`data:tick:{symbol}` → `last_price`), live option ticks (`data:options_live:{symbol}`), and nearest expiry + forward price (`data:sensibull:{symbol}` → `current_json` → `stats.per_expiry_map`, matching `MaxPainAnalyser`'s existing convention — **not** the invented `read_future_price`/`read_nearest_expiry` helpers from Section 8.4's original pseudocode) from Redis; inverts live prices to IV via the same BS convention as training; runs the PINN forward pass; computes z-scores; emits `SKEW_FADE_SETUP`/`RANGE_BOUND_SETUP` signals. Also fixes an unpassed `tau` free-variable bug in the original `check_signal_thresholds` pseudocode.
- **`signal_emitter.py`** — publishes to `pinn:signals` stream + `pinn:zscore:{symbol}` confirmation cache, per Section 8.6/8.7.
- **`main.py`** — always-running service entry point (inference loop every 3s, arbitrage monitor every 30s, heartbeat), same pattern as `paper_trading/main.py`.

**Deviation from Section 8.2**: `ModelManager.SYMBOLS` defaults to `PINNConfig().symbols` (`["NIFTY", "BANKNIFTY"]`), not the doc's hardcoded `["NIFTY", "BANKNIFTY", "SENSEX"]` — SENSEX has no checkpoint (see 0.3's SENSEX exclusion, which was already true for Phase 1 and applies identically here).

**Not yet done for Phase 2 part 1**: no live validation against a real Redis instance during market hours — everything above has only been exercised with synthetic data and a stub model in tests. That's the natural next check before trusting it in production, same as Phase 1 needed a real Bhavcopy run before being trusted.

### 0.2 What's NOT built — Phase 2 (remaining) and beyond

- ~~**Paper-trading integration**~~ — **done (2026-09-20/21, see 0.4.3)**: `signal_router.parse_pinn_signal()`, the `strategy_builder.py` `sr_level`-presence fix, the 6th consumer thread (`pinn_signal_consumer`), and the confirmation-mode hook (`get_pinn_confirmation`) are all built, on `feature/pinn-paper-trading-integration` (not yet merged). Section 9's design is otherwise unchanged from the original plan.
- **`/pinn_status` bot command** — `lib/notification/commands/pinn_cmds.py` doesn't exist. Now easier: `pinn:model:{symbol}` (0.4.4) is populated, exactly the key this command needs to read.
- ~~No systemd units at all~~ — **nightly training scheduling is now built** (0.4.4): `services/pinn_trainer/main.py` + `configs/stockanalysis-pinn-trainer.service`, not yet deployed to the server. `stockanalysis-volatility-engine.service` (the inference service itself) still doesn't exist; it, once live-validated, would still need to be started by hand.
- No Greeks/backtesting (Section 10, Phase 3) or SSVI baseline (Phase 4).

### 0.3 Key deviations from this document's original design

Found and validated empirically during implementation — **treat these as superseding the corresponding numbers/claims elsewhere in this document**, not as new bugs:

1. **NSE Bhavcopy schema and URL are completely different from Section 4.1.** The plan's `archives.nseindia.com/.../fo{DD}{MON}{YYYY}bhav.csv.zip` 404s. Current, verified-live format: `nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip`, with entirely renamed columns (`FinInstrmTp`/`TckrSymb`/`XpryDt`/`StrkPric`/`SttlmPric`/`TtlTradgVol`/`UndrlygPric` replacing `INSTRUMENT`/`SYMBOL`/`EXPIRY_DT`/`STRIKE_PR`/`SETTLE_PR`/`CONTRACTS`). Full mapping in `bhavcopy_fetcher.py`'s module docstring.
2. **SENSEX is out of scope.** It's a BSE product — confirmed empirically absent from NSE's F&O file (0 rows). This document's assumption that all three symbols come from one NSE fetch (Sections 3, 4.1, 5.4) is wrong for SENSEX specifically. Current scope: **NIFTY + BANKNIFTY only**. A BSE fetcher would be new, separate work.
3. **The plan's β-NLL pseudocode (Section 6.2) has a real bug**, not a simplification: it detaches `v_squared`'s gradient inside both the NLL term and the re-weighting factor, which structurally prevents the network from ever learning to predict uncertainty (confirmed empirically: gradient to the variance head is `None`). Fixed in `losses/data_loss.py` — only the re-weighting *factor* is stop-gradiented, matching the actual Seitzer et al. paper.
4. **Fourier feature encoding was added — not in this document at all.** `model/pinn.py`'s `VolatilityPINN` now supports `num_fourier_bands` (default **3**, found empirically optimal via a frequency sweep: L=2 → 3.79% MAE, L=3 → 2.68%, L=4 → 3.32% with growing bias). This was the single highest-impact change in the whole project — it fixed the "wings are over-smoothed" spectral-bias problem that no amount of loss reweighting solved. Needed a companion fix: `RawInputModel` (also new, in `model/pinn.py`), so the arbitrage penalties can differentiate w.r.t. the true physical `k` while the network still only ever sees normalized input.
5. **A `vega_weight()` sample-reweighting scheme was tried and deliberately removed.** An isolation experiment found it actively hurt wing accuracy (both raw and √τ-normalized variants) — it suppresses gradient signal from low-vega (mostly deep-wing) samples by design, which is the opposite of what wing accuracy needs. `bs_utils.vega()` itself is kept; only its use as a loss weight was removed.
6. **Several trainer/collocation defaults changed from Section 12/7.1's values, based on real tuning:**

   | Parameter | Plan's original | Actual validated default |
   |---|---|---|
   | `adam_lr` | 1e-3 | **5e-4** (1e-3 oscillated; 1e-4 under-trained at a fixed 5000-epoch budget) |
   | `n_collocation` | 2000 | **512** |
   | `collocation_regen_every` | every 1000 epochs | **every epoch** (fixed an observed "collocation shock" — a real loss discontinuity at each regen boundary) |
   | `lambda_butterfly` | 0.5 | **0.7** (0.5 let real butterfly-arbitrage violations through, up to 7.6% of an audit grid on one day — see 0.4 below) |
   | `num_fourier_bands` | (didn't exist) | **3** |

### 0.4 Validated empirical results

Walk-forward holdout (train on the prior week, evaluate on a genuinely unseen next day) across **10 real trading days** (2026-08-10 through 2026-08-21, NIFTY+BANKNIFTY combined, `lambda_but=0.7`, `num_fourier_bands=3`, seed=42):

| Metric | Mean ± std | Target | Days meeting target |
|---|---|---|---|
| Overall MAE (σ) | 2.95 ± 0.26% | — | — |
| ATM MAE | 3.04 ± 0.30% | — | — |
| **Wings MAE** | **2.39 ± 0.37%** | < 2.5% | 6 / 10 |
| Bias (signed) | −1.45 ± 0.69 pts | — | — |
| **Butterfly violation rate** | **3.11 ± 1.07%** | < 5% | 9 / 10 |
| Calendar violation rate | 0.00% | < 1% | 10 / 10 |

**2026-08-11 failed both thresholds simultaneously** (wings 3.03%, butterfly violation 5.44%) — investigated and deliberately **not** chased by further tuning: forcing every single day under threshold risks overfitting to that one day's microstructure noise at the expense of the other 9. This is the reasoning behind Section 0.5's acceptance-gate design — model risk is handled operationally (reject-and-fall-back), not by assuming the math is perfect on every conceivable day.

#### 0.4.1 2026-08-11 root-cause investigation (2026-08-25)

Re-ran the exact walk-forward window (train 2026-08-05→08-10, holdout 08-11, NIFTY, same config, seed=42) with per-expiry/per-tau instrumentation. Two real findings, one methodological caveat:

- **Weekly-expiry rollover discontinuity.** 08-10 is the *last* trading day of that week's contract — its nearest-to-expiry bucket has τ≈0.003 (a few hours to expiry) where NSE index-option settlement prices are known to blow up (illiquid deep OTM strikes settling at distorted premiums; observed ATM IV ~14-16% but far-OTM strikes implying 60-110%+ IV). This extreme short-τ regime dominates the `τ < 0.01` slice of the training set (4.5% of train rows, all from 08-10). On the holdout day (08-11), that contract has expired — the new nearest-week contract sits at τ≈0.019 with a completely different, much calmer smile (ATM ~10%, max wing ~29%, not 100%+). The model's largest errors are concentrated exactly in this near-week bucket on 08-11 (6-7 vol-point overshoot at low |k|, τ≈0.019) — it's carrying over a short-τ IV *level* learned from the distorted expiring-contract data into the freshly-rolled contract, where that level doesn't apply. This is a structural, explainable failure mode tied to the weekly-expiry cycle, not random noise.
- **Sparse far-tenor extrapolation.** The single worst holdout error (20+ vol points) is a deep-OTM put on the Dec-2026 expiry (τ≈0.38, k≈−0.74) — a tenor/moneyness combination with very few training rows, i.e. an extrapolation gap rather than a rollover artifact.
- **Gate-outcome seed sensitivity (caveat).** This re-run, with the same config and data as the original 10-day validation, produced wings MAE 2.29% / butterfly violation 3.98% — both **under** threshold (ACCEPTED), not the originally logged 3.03%/5.44% (REJECTED) for the same calendar day. The discrepancy traces to nondeterminism between runs (this project's collocation resampling and Adam optimization are seeded, but the original 10-day sweep did not pin down every source of run-to-run variance as tightly as this isolated re-run did). Practical implication: on a borderline day, the accept/reject verdict itself has some variance — reinforcing that the gate should be treated as a noisy operational check, not a precise measurement, and that a single day's PASS/FAIL is not by itself strong evidence either way.

Neither finding changes the gate thresholds or defaults — the rollover effect is inherent to how NSE weekly options settle (not fixable by re-tuning `lambda_but`/Fourier bands without the overfitting trap already documented above), and it's exactly the scenario the accept/reject gate exists to catch operationally.

#### 0.4.2 2026-09-17 root-cause investigation (2026-09-18)

A scheduled-equivalent run (`run_training.py --symbols NIFTY BANKNIFTY --seed 42`, no `--end-date`, i.e. "today") on the next available live window **rejected both symbols**: NIFTY wings 3.41% / butterfly 7.68%, BANKNIFTY wings 3.69%. Re-investigated with the same per-expiry/per-tau instrumentation as 0.4.1.

- **Correction to this project's expiry assumption**: NIFTY's weekly expiry is **Tuesday**, confirmed directly from Bhavcopy expiry lists (`2026-09-15`, `2026-09-22`, ... are all Tuesdays) — not Thursday as assumed elsewhere in this codebase's design docs (`docs/DESIGN.md`'s "Expiry Selection" section). Doesn't change any of this project's code (it never hardcoded a weekday), but matters for interpreting *when* a rollover falls relative to a training window.
- **Same failure mode as 0.4.1, different timing.** The 09-15→09-16 rollover happened *inside* the training window this time (not at the train/holdout boundary), so the model had one day of the new contract to learn from. It still overshot badly: per-expiry breakdown showed the freshly-rolled contract (`2026-09-22`, τ=0.0137 — near the model's τ-domain floor of 0.003) at **5.76% MAE** vs. 1.2%-3.2% for every other expiry in the holdout set. Worst errors: near-ATM, actual σ≈9-12%, predicted σ≈20% (8-13 point overshoot) — even though training data one day earlier at nearly the same τ (0.016-0.019) showed the correct ~12-16% level. This is a short-τ **extrapolation** failure specifically at the edge of the model's τ domain, not the model parroting stale data.
- **Confirmed (again): the gate's verdict has real run-to-run variance.** Manually re-running the *identical* config/data/seed (42) as the production run got wings 2.53% / butterfly 1.48% — essentially passing — not the production run's 3.41%/7.68% (clearly rejected). Same code, same seed, different outcome, on a borderline case. Root cause identified this time (not just observed): PyTorch's CPU matmul/reduction kernels are not bit-reproducible across runs even with `torch.manual_seed()` fixed, because multi-threaded floating-point reduction order isn't guaranteed. This directly motivated the `enable_deterministic_threads` flag in 0.7.
- **Background, not causal**: ~9-14% of every single day's option rows (checked across all 8 days in the training window, not just this one) have 1-10 lot volume — a standing `min_volume=1` data-quality gap, not something specific to this week. Motivated the `min_volume=25` experiment in 0.7, though that experiment's own result (0.7) shows it isn't a clean win either.

**Operational consequence**: as of this run, `data/pinn_models/` has no accepted checkpoint for either symbol (the very first production-path run for both symbols happened to land on a difficult week). The service has nothing to serve yet — see 0.6.1.

#### 0.4.3 Paper-trading integration (2026-09-20/21) — Section 9 complete

Implements all three of Section 9's missing pieces (0.2), adapted to the real code (not the pseudocode's assumed field names):

- **`signal_router.parse_pinn_signal()`** — deserializes `pinn:signals` messages into `EntrySignal`. Deviates from Section 9.2's pseudocode: it **trusts** the `strategy`/`signal_source` fields the emitter (`services/volatility_engine/signal_emitter.py`) already sets correctly on the wire (`"CREDIT_SPREAD"`/`"PINN_MISPRICING"` for `SKEW_FADE_SETUP`, `"IRON_CONDOR"` for `RANGE_BOUND_SETUP`, per `comparator.py`'s `PinnSignal` construction) rather than re-deriving them from `signal_type` a second time — one source of truth for that mapping, not two.
- **`pinn_signal_consumer`** — the 6th consumer thread in `paper_trading/main.py`, wired identically to `confluence_consumer` (its own `xreadgroup` group `paper-trader-pinn` on the `pinn:signals` stream, feeding the shared `entry_queue`). Imports `PINN_SIGNALS_STREAM` directly from `signal_emitter.py` rather than duplicating the stream-name literal.
- **`strategy_builder.py`'s one-line fix** — `select_strikes()`'s `CREDIT_SPREAD` branch now checks `signal.sr_level is not None` instead of `signal.signal_source == "SKEW_FADE_SETUP"`, exactly as Section 9.2 specifies. This was a real bug, not just a documentation gap: `PINN_MISPRICING` signals carry `sr_level` too, so the old check silently misrouted them down the CONFLUENCE/`atm_strike` path instead of using the model's specific mispriced strike.
- **`signal_router.get_pinn_confirmation()`** + the confirmation-mode hook in `_handle_entry_signal()` (Section 9.3) — reads the `pinn:zscore:{symbol}` cache (`signal_emitter.write_fair_iv_to_redis`, TTL imported directly from there rather than a duplicated magic number) and returns the z-score at the *specific strike(s)* a non-PINN signal is about to trade. Below `PINN_CONFIRM_SUPPRESS_THRESHOLD` (0.5) the entry is suppressed; above `PINN_CONFIRM_BOOST_THRESHOLD` (1.0) it's tagged `pinn_confirmed`/`pinn_z` in `signal_context` for logging but not otherwise changed. Fails open (no data, or data older than the cache's own 30s TTL → don't suppress) — matching the plan doc's original design exactly. PINN-sourced signals skip this check entirely (confirming against itself would be circular). **These two threshold values are the design doc's original numbers, never empirically validated** — no accepted model has run against live composite signals yet (0.6.1) to tune them against real data; revisit once one has.
- 16 new tests total across `tests/services/test_signal_router.py` (`TestParsePinnSignal`, round-tripping fixtures through the real `emit_signal()` rather than hand-written field dicts), `test_strategy_builder.py` (one regression test), and `test_paper_trading_main.py` (`TestHandleEntrySignal`'s new suppress/boost/self-skip/fail-open cases).

**Net effect**: PINN-sourced signals now flow end-to-end from `pinn:signals` into open positions, and composite/confluence-sourced signals are checked against PINN's independent view before entering. Both require an accepted model to actually do anything — currently neither symbol has one (0.6.1), so this is built and tested but has nothing live to exercise it against yet.

#### 0.4.4 Nightly training scheduling (2026-09-26)

**Deviation from Section 7.5**: the original plan specifies a systemd `.timer` (`stockanalysis-pinn-training.timer`, `OnCalendar=*-*-* 21:00:00`). That's stale relative to this project's actual, deliberate architecture — every other scheduled behavior in this codebase runs inside a self-scheduling 24/7 service, not a systemd timer (`README.md`: "No timers. All services self-schedule."; the last vestigial timer-adjacent unit was removed from the server well before this work). Implemented the self-scheduling version instead, mirroring `services/auth_service/main.py`'s existing fixed-time pattern (09:00 + 18:50 refresh loop) rather than reintroducing timers.

- **New service**: `services/pinn_trainer/main.py` (+ `configs/stockanalysis-pinn-trainer.service`, not yet deployed to the server). Self-schedules once/day: waits until 21:00 (after `positional_analysis()`'s ~20:30 completion — no cross-service "done" signal exists to hook into instead, so this uses the same fixed-clock approach as auth-service), trains every symbol not already trained today, and retries every 15 minutes until 23:00 if Bhavcopy still isn't published (a real, previously-observed failure mode — see 0.4.2's background finding). One "give up for today" alert if still blocked at 23:00; otherwise idles until midnight once every symbol reaches the gate.
- **Day-idempotency**: `pinn:model:{symbol}`'s `train_date` field (section 11.1) is the single source of truth both for the scheduler's skip-check and for `/pinn_status` (not yet built, 0.2). **`run_training.py`'s `train_one_symbol()` didn't write this key at all before this change** — added it via `_write_model_status()`, called on every return path when an optional `redis` argument is passed (`None` by default, so validation scripts calling `train_one_symbol()` directly are unaffected). Deliberately writes `train_date` only once training reaches the acceptance gate (`holdout_date is not None`) — an early failure (no Bhavcopy data yet, too few samples) must NOT count as "trained today," or the scheduler would give up retrying after a single no-data attempt instead of catching a late Bhavcopy publish. A genuine gate verdict (accepted OR rejected) IS terminal for the day either way.
- **Alerts** (section 11.3, adapted): "training started" once/day at first attempt, "trained"/"failed" per symbol once it reaches the gate, and "Bhavcopy still unavailable" at most once/day at the 23:00 cutoff — deliberately NOT once per 15-minute retry (the doc's per-attempt assumption would spam Telegram every retry on a genuinely delayed Bhavcopy day).
- **CLI unaffected**: `python -m tools.pinn_volatility.training.run_training` now also attempts a Redis connection (via `REDIS_URL`, same convention as every other service) and writes the same status key when available — logs a warning and continues without it if Redis is down, so training itself is never gated on Redis being up.
- **Trading-day gate**: the scheduler idles on weekends/holidays via `common.market_calendar.is_trading_day()` before doing anything. Without it, `end_date`'s backward walk would re-train the previous session's Bhavcopy on Sat *and* Sun (3x the same run per week) and send "started"/"trained" Telegram alerts each of those days.
- **Exception guard**: `run_training_cycle()` is wrapped — an unexpected error (e.g. a torch `RuntimeError`) logs with traceback, sends at most one "unexpected error" alert per day, and lets the 15-min retry cadence continue. Unguarded, it would kill the `_run_schedule` daemon thread while the heartbeat loop kept reporting `healthy`, silently stopping training until the service was restarted.
- **Dependencies**: the trainer needs torch, which requirements.txt deliberately excludes. Added `requirements-ml.txt` (CPU-only `torch==2.8.0+cpu` + `scipy`, via PyTorch's CPU wheel index — the default PyPI wheel is the ~2.5 GB CUDA build) and a `make install-ml` target; the unit's `MemoryMax` was raised from 512M to 2G accordingly (torch import alone is ~250-300MB). The unit is **not installed on the server yet**.
- 17 new tests: `tests/pinn/test_run_training.py` (4, Redis-write behavior across the early-failure/accepted/rejected paths) + `tests/services/test_pinn_trainer_main.py` (13 — day-idempotency checks, the training cycle, alert formatting, and the two scheduling guards; the rest of the loop is integration-level, same convention as `test_paper_trading_main.py`).

### 0.5 Acceptance gate — supersedes Section 7.4

`training/validate.py`'s `check_acceptance_criteria()` replaces Section 7.4's untested thresholds with ones tiered by actual validation status:

- `max_wings_mae = 0.025` (2.5%) — this project's explicit target, validated per 0.4 above.
- `max_butterfly_violation_rate = 0.05` (5%) — kept from Section 7.4, re-validated per 0.4 above.
- `max_calendar_violation_rate = 0.01` (1%) — kept from Section 7.4, but **never actually exercised** (0.00% on every single day tested) — this is a cheap sanity floor, not a tight validated bound.
- `max_overall_mae` — **disabled by default** (`None`). Section 7.4's original `rmse < 0.01` / `mae_sigma < 0.02` were never validated and are inconsistent with the real numbers above (~3% overall MAE); gate on this explicitly only with a threshold informed by real data.

A rejected model is never deployed — `run_training.py` leaves the previously-accepted model (and its `_latest.pt` symlink) untouched and returns a nonzero exit code. Verified with a real (non-mocked) live run: NIFTY-only on 2026-08-14 correctly rejected (butterfly violation 5.52%), no artifact written.

### 0.6 Live re-validation on current data (2026-08-25)

Ran the production CLI unmodified against a fresh 8-trading-day window ending 2026-08-25 (`run_training.py --symbols NIFTY BANKNIFTY --end-date 2026-08-25 --seed 42`), fetching that day's real NSE Bhavcopy over the network rather than the cached historical files used elsewhere in this doc:

| Symbol | Verdict | Wings MAE | Butterfly violation |
|---|---|---|---|
| NIFTY | **ACCEPTED** | 2.08% | 4.58% |
| BANKNIFTY | **ACCEPTED** | 1.83% | 1.98% |

Both comfortably inside threshold (wings < 2.5%, butterfly < 5%), consistent with the 10-day validated average in 0.4 — confirms the pipeline still functions correctly end-to-end (network fetch → dataset → train → gate → save/symlink) two weeks after the original validation window, on data it had never seen before.

#### 0.6.1 2026-09-18 run — both symbols rejected

Ran the same production CLI against the next available live window. Both NIFTY and BANKNIFTY were **REJECTED** (see 0.4.2 for the root-cause investigation this triggered). Since `data/pinn_models/` had no prior accepted checkpoint for either symbol at this point, this run's rejection means **the pipeline currently has no model to serve** for either symbol — the gate's fallback ("keep the previous accepted model live") has nothing to fall back to yet. This is expected/correct behavior for a system that has never yet had an accepted run in its target deployment location, not a bug — but it does mean Phase 2's inference service, once live-validated, would start with zero usable models until a future scheduled run passes the gate.

### 0.7 Experimental flags (2026-09-18) — evaluated, not adopted

Four flags were implemented and validated in response to a proposal for fixing the 0.4.2 short-τ overshoot. All are **opt-in, default off** — none change any existing validated behavior:

| Flag | Where | Purpose |
|---|---|---|
| `enable_deterministic_threads` (`PINNConfig`) → `PINNTrainer.deterministic_threads` | `training/trainer.py` | Calls `torch.set_num_threads(1)` at the start of `train()`, closing the run-to-run non-determinism confirmed in 0.4.2. |
| `max_tau` | `data/dataset.py`'s `build_training_samples()` | Optional upper bound on time-to-expiry (years); drops samples beyond it (e.g. multi-year LEAPS-style expiries that already sit outside the model's normalized `TAU_RANGE=(0.003, 1.0)` domain). |
| `min_volume` (existing param, tested at a stricter value) | `data/dataset.py`'s `build_training_samples()` | Raise the liquidity floor from the default of 1 lot to strip thin/stale settlement prices. |
| `short_tau_collocation_boost` (`PINNConfig`) → `sample_collocation()`'s `short_tau_boost_frac`/`short_tau_boost_range` | `data/collocation.py` | Carves out a fraction of collocation points into a narrow τ window near the domain floor, spread across the full k range (not just ATM) — intended to better constrain the arbitrage penalty right at the boundary where 0.4.2's overshoot occurred. |

**Validation**: a 20-run (10-day × 2-symbol) walk-forward sweep — same 10 days and methodology as the 0.4 baseline, so directly comparable — with **all four flags on together** (`max_tau=0.5`, `min_volume=25`, deterministic threads, and the short-τ boost at its default 40%/[0.005, 0.02]):

| Metric | This test (4 flags on) | 0.4 baseline (flags off) |
|---|---|---|
| Wings MAE | **3.47 ± 0.91%** | 2.39 ± 0.37% |
| Overall MAE | 4.73 ± 1.32% | 2.95 ± 0.26% |
| ATM MAE | 4.97 ± 1.47% | 3.04 ± 0.30% |
| Butterfly violation | 2.62 ± 2.87% | 3.11 ± 1.07% |
| Worst-case wings | 5.52% (NIFTY 08-10) | 3.03% (NIFTY 08-11) |
| Worst-case butterfly | **10.80%** (NIFTY 08-13) — a new project-worst | 5.44% (NIFTY 08-11) |
| Gate acceptance | **3/20 (15%)** | ~9/10 combined |

**Verdict: net regression — do not adopt this combination.** Most likely mechanism: `short_tau_collocation_boost` reallocates 40% of an already-modest, fixed collocation budget (512 points) into a narrow τ slice; since collocation points only constrain the arbitrage/PDE penalty (never the data-fitting loss — they carry no market data), this measurably weakens arbitrage-penalty coverage across the *rest* of the domain, consistent with the worst-ever butterfly violation observed. `max_tau=0.5` and `min_volume=25` also shrink the training set meaningfully (confirmed separately: `min_volume=25` alone drops ~20% of rows and zeroes out several far-dated expiries entirely). `enable_deterministic_threads` was not implicated in the regression — it has no plausible mechanism for affecting model quality, only run-to-run reproducibility.

**Not done**: an isolated per-flag sweep (each flag alone, same 10 days) to identify which specific flag(s) are responsible — the bundled test only shows the combination is bad, not which piece(s). `enable_deterministic_threads` alone is the one flag worth adopting on its own reasoning (removes non-determinism, no plausible quality downside) without needing that isolation sweep first.

#### 0.7.1 Per-flag isolation sweep (2026-09-19) — resolves 0.7's "not done" item

Same 10-day × 2-symbol methodology, each of the three quality-affecting flags tested alone (`enable_deterministic_threads=True` in every config, including the control, so the comparison isn't confounded by run-to-run noise):

| Config | Wings MAE | ATM MAE | Overall MAE | Butterfly viol. | Accepted |
|---|---|---|---|---|---|
| `det_only` (control — determinism only) | 2.73 ± 0.80% | 4.10 ± 1.81% | 3.86 ± 1.60% | 3.25 ± 2.41% (max 8.58%) | 5/20 |
| `det_maxtau` (+ `max_tau=0.5`) | 3.68 ± 0.87% | 4.91 ± 1.72% | 4.69 ± 1.51% | 4.21 ± 4.11% (max **11.00%**) | **1/20** |
| `det_minvol25` (+ `min_volume=25`) | **2.46 ± 0.71%** | **3.90 ± 1.46%** | **3.67 ± 1.31%** | 4.69 ± 4.26% (max 10.96%) | 3/20 |
| `det_boost` (+ `short_tau_collocation_boost`) | 2.98 ± 0.86% | 4.64 ± 1.90% | 4.35 ± 1.68% | **2.52 ± 2.32%** (max 8.34%) | **6/20** |

**Verdict**: no individual flag beats the 0.4 baseline (2.39 ± 0.37% wings, ~9/10 combined acceptance) either — including the control, which reproduces determinism-only behavior but still shows materially higher variance than the original 0.4 measurement, most likely session/environment drift rather than a `enable_deterministic_threads` side effect (it has no plausible mechanism for changing model quality, only reproducibility). `max_tau=0.5` is unambiguously the worst single flag — new project-worst butterfly tail (11.00%), only 1/20 accepted — confirms it should not be pursued further in any form. `min_volume=25` gives the best raw accuracy of the four but the worst butterfly control. `short_tau_collocation_boost` gives the best butterfly control and best acceptance rate of the four but at a real accuracy cost. **None recommended individually either.**

#### 0.7.2 Five further improvement proposals analyzed (2026-09-19); log-τ input encoding implemented, validated, and reverted (2026-09-19/20)

A follow-up proposal suggested five further fixes for the 0.4.2 short-τ failure. Each was checked against the actual code before any implementation:

1. **GradNorm/SoftAdapt adaptive loss weighting** — not pursued. The claimed benefit ("reduces run-to-run variance") is already independently explained and fixed by `enable_deterministic_threads` (0.4.2); GradNorm wouldn't touch that. It also wouldn't fix a data-fit-level error at the τ-domain boundary, since `lambda_cal`/`lambda_but` only weight the synthetic-collocation PDE penalty (`losses/composite.py`), never the data-fitting term itself.
2. **Asymptotic Roger-Lee wing-slope bound penalty** — legitimate and cheap, but addresses far-wing (`|k|` large) behavior, not the short-τ rollover failure that's actually open. Deferred as a general hardening item, not a fix for 0.4.2.
3. **Short-τ exponential data-loss multiplier** — not implemented as literally proposed: the given formula (`e^{-τ}` for τ < 0.02) evaluates to ≈0.98–0.997 across that entire window, i.e. effectively a no-op, the opposite of the intended "treat short-expiry errors as fatal." The equivalent, correctly-signed mechanism already exists — `losses/data_loss.py`'s `tau_weight()` (1/τ², capped at 20×) behind `use_tau_weight` — but has never been validated at scale (defaults off). Recommended trying that before writing new code; not yet done.
4. **log(τ)/√τ time-domain input stretching** — **implemented and validated, then reverted.** See below.
5. **Normalized strike input `k/√τ`** — not implemented; deferred pending (4)'s outcome. Real derivative-correctness risk: `losses/arbitrage.py`'s `calendar_slope`/`durrleman_density` differentiate w.r.t. the *raw* (k, τ) via autograd through `normalize()` (`model/pinn.py`) — a `k/√τ` transform changes what those derivatives mean unless the chain rule is rebuilt carefully, the same bug class already caught once in `losses/data_loss.py`'s β-NLL implementation (0.3, deviation 3).
6. **Dual-branch gated architecture** — not implemented. Every current constant (`num_fourier_bands=3`, `lambda_butterfly=0.7`, `hidden_dim`/`num_layers`) was empirically tuned as a package for the existing single-branch architecture (0.3, deviations 4 and 6) — a gated architecture invalidates all of it and requires redoing every sweep from scratch, plus a smooth (not hard) gate to preserve the double-differentiability the butterfly penalty needs. Phase-3/4-scale effort, not a quick fix.

**Log-τ normalization (item 4) — full result.** Implemented as `use_log_tau` (opt-in, default `False`, exactly reproduces existing behavior) in `model/pinn.py`'s `normalize()`, threaded through `RawInputModel`, `losses/composite.py`, `training/trainer.py`, `training/validate.py`, and `config.py` (`PINNConfig.use_log_tau`), with 8 new tests, on `feature/pinn-log-tau-normalization`.

Motivation, confirmed directly from the code: `TAU_RANGE=(0.003, 1.0)` linear normalization squeezes the entire weekly-rollover window (τ ∈ [0.005, 0.02] — every ~3–14 day contract) into just **~1.5% of the model's [-1, 1] input range** (`tau_norm` ∈ [−0.996, −0.966]) — a 3-day and a 5-day contract are nearly indistinguishable network inputs. This is a mechanistic explanation for the short-τ extrapolation failures in both 0.4.1 and 0.4.2.

Validated via the same 10-day × 2-symbol walk-forward, against a same-session control:

| Metric | Control (`det_only`) | `use_log_tau=True` |
|---|---|---|
| Wings MAE | 2.73 ± 0.80% | **7.18 ± 3.39%** (max 13.58%) |
| ATM MAE | 4.10 ± 1.81% | **7.26 ± 3.47%** (max 14.74%) |
| Overall MAE | 3.86 ± 1.60% | **7.28 ± 3.29%** (max 14.12%) |
| Butterfly violation | 3.25 ± 2.41% | 5.05 ± 2.15% |
| Accepted | 5/20 | **0/20** |

Degraded roughly uniformly across all 20 runs, both symbols, and both ATM and wings — not a single bad day dragging the average. Not an implementation bug: the 8 unit tests confirm the transform does exactly what it's supposed to (correct range endpoints, ~30× wider short-τ span than linear, doesn't touch the `k` dimension at all), and the control config reproduced the 0.7.1 numbers exactly on re-run.

**Most likely mechanism**: linear normalization's compression of short-τ wasn't only hiding the 3-day/5-day distinction — it was also implicitly *smoothing over* the genuinely noisy short-dated data (0.4.2's finding: 9–14% of every day's rows have 1–10 lot volume, concentrated near rollover). Because linear normalization couldn't resolve individual short-τ contracts as distinct inputs, the network was structurally forced to fit a smooth extrapolation through that region. Log-τ normalization gives it enough resolution to fit that noise directly instead — and since none of `adam_lr`/the epoch schedule/`lambda_butterfly=0.7`/`num_fourier_bands=3` (all empirically tuned specifically against the linear mapping, 0.3) were retuned for the new input geometry, the optimizer converges to a substantially worse global fit, not just a locally-worse short-τ one.

**Verdict: do not adopt.** `feature/pinn-log-tau-normalization` was deleted without merging — the result is unambiguously negative, and the code touches the core `normalize()` function used by live inference too (`services/volatility_engine/comparator.py` was deliberately left unthreaded, since a model trained with `use_log_tau=True` requires the *same* flag at inference time or predictions are silently wrong), which isn't worth carrying as opt-in dead code for an idea that failed this badly. This finding is preserved here instead of in the code.

**Pattern across every tuning attempt to date** (0.7's 4-flag bundle, 0.7.1's per-flag isolation, and this log-τ test): every single change attempted so far to fix the 0.4.2 short-τ failure has made overall quality *worse*, never better. This model's hyperparameters (`adam_lr`, epoch budget, `lambda_butterfly`, `num_fourier_bands`) were empirically tuned as one interdependent package against the *current* linear-τ/fixed-weight setup (0.3) — perturbing any single piece of that package (data filtering, collocation allocation, or the input encoding itself) disturbs the balance rather than surgically fixing the boundary case. A structural fix (e.g. log-τ input, or item 5/6 above) likely needs a full re-tune of the whole package under the new geometry to have a fair chance — a substantially larger undertaking than any single-flag experiment tried so far. **Recommend pausing further single-change tuning experiments on this axis** in favor of higher-value remaining work (paper-trading integration, 0.8) until there's appetite for that larger re-tune.

### 0.8 Next work items, roughly in priority order

1. **Merge `feature/pinn-paper-trading-integration`** — Section 9 is fully built (0.4.3) but still sits unmerged; has nothing live to exercise until a model is accepted (0.6.1) regardless.
2. **Deploy `stockanalysis-pinn-trainer.service` to the server** — built and tested (0.4.4) but not yet installed/enabled there; training remains manual (CLI) until it is. Prerequisite: `requirements-ml.txt` (CPU torch) in the server venv — install with `make install-ml`.
3. **Live-validate Phase 2 part 1** against a real Redis instance during market hours — everything so far is synthetic-data/stub-model tested only (see 0.1b).
4. **`/pinn_status` bot command** — `lib/notification/commands/pinn_cmds.py` doesn't exist yet, though `pinn:model:{symbol}` (0.4.4) now has the data it needs. `stockanalysis-volatility-engine.service` (the inference service itself) also still doesn't exist.
5. **Tune the confirmation-mode thresholds** (`PINN_CONFIRM_SUPPRESS_THRESHOLD`/`PINN_CONFIRM_BOOST_THRESHOLD`, 0.4.3) against real data once an accepted model exists to generate it — currently the design doc's original, unvalidated numbers.
6. Phase 3 (Greeks/backtesting) and Phase 4 (SSVI baseline) remain as originally scoped in Sections 10 and the plan's Phase 4 — untouched.
7. Close PR #55 on GitHub without merging (superseded duplicate, folded into #57) — flagged repeatedly, still not done.
8. If there's appetite for a larger investment: a full hyperparameter re-tune (LR schedule, epoch budget, `lambda_butterfly`, Fourier bands) under a log-τ input encoding — 0.7.2 found the flag-toggle version is a severe regression precisely because nothing else was retuned for the new geometry.
9. ~~Investigate 2026-08-11~~ — done, see 0.4.1. ~~Investigate 2026-09-17~~ — done, see 0.4.2. ~~Open a PR~~ — done, PR #54 (Phase 1), PR #56 (Phase 2 part 1), PR #57 (experimental flags). ~~Isolate the 0.7 experimental flags individually~~ — done, see 0.7.1. ~~Analyze further short-τ fix proposals~~ — done, see 0.7.2. ~~Paper-trading integration~~ — done, see 0.4.3. ~~Nightly training scheduling design~~ — done, see 0.4.4.

---

## 1. Executive Summary

A Physics-Informed Neural Network (PINN) that learns the arbitrage-free total implied variance surface $w(k, \tau)$ for NIFTY, BANKNIFTY, and SENSEX index options. Trained nightly on NSE Bhavcopy data with no-arbitrage PDE constraints (calendar + butterfly/Durrleman). At inference, compares live market IV against the PINN's fair-value surface to detect structural mispricing — emitting `SKEW_FADE_SETUP` and `RANGE_BOUND_SETUP` signals into the existing paper-trading pipeline.

The PINN operates as a **shape comparator**, not a level predictor. Your existing `OptionSellerCompositeAnalyser` (921 lines, rule-based, using GEX walls, max pain, PCR, panic exhaustion) handles level-based signals (IV percentile, IV/HV ratio). The PINN adds **volatility surface shape distortion detection** — skew too steep, smile too flat, wings overpriced relative to ATM — which no rule-based system can do. Both signal sources feed the paper-trading engine independently.

---

## 2. Locked P0 Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Inference runtime | **PyTorch native** (no ONNX) | Need `torch.autograd.grad` for live $w'$, $w''$ (Durrleman density + Greeks). 50K-param MLP: <2ms/batch on i5-6200U CPU. |
| 2 | Macro conditioning | **No live macro adjustment (v1)** | Shifting $w$ breaks Durrleman $1/w$ terms. PINN compares **surface shape** vs live. Existing composite analyser handles level signals. |
| 3 | Training data source | **NSE Bhavcopy primary + Zerodha gap-fill** | Bhavcopy: single CSV, all strikes, no rate limits. Zerodha `historical_data()` for missing days. |
| 3b | Strike filtering | Volume > 0, 0 < IV < 200%, $\|k\| < 2.0$ | Removes zero-volume garbage IV from illiquid deep-OTM strikes. |
| 4a | Risk-free rate $r$ | **Fixed 7%** | India 10Y bond yield. 50bps error shifts $k$ by ~0.005 on 30D options — negligible vs strike gaps. |
| 4b | Dividend yield $q$ | **$q = 0$ for indices** | NIFTY/BANKNIFTY/SENSEX yield ~1-1.5%, negligible for short-dated options. Use `future_price` from `data:options_agg` when available (backed-out forward, no $r$/$q$ needed). |
| 5 | NLL variance collapse | **$\beta$-NLL loss** (Seitzer et al. 2022) | Detaches gradient w.r.t. $v^2$ from residual term. Prevents $v^2 \to 0$ collapse. One-line change. |

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        NIGHTLY (21:00 IST)                          │
│                                                                     │
│  NSE Bhavcopy ──→ Strike Filtering ──→ IV Inversion ──→ Dataset    │
│  (tools/pinn_volatility/data/)              (Brent's method)         │
│                                                                     │
│  Dataset ──→ PINN Training (Adam 5K epochs → L-BFGS)               │
│              Loss = β-NLL + Calendar + Butterfly (Durrleman)        │
│              Collocation: 2000 LHS points, density-weighted         │
│                                                                     │
│  Trained Model ──→ Validation ──→ Save .pt ──→ Update symlink      │
│  (data/pinn_models/{symbol}_{date}.pt → {symbol}_latest.pt)        │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    LIVE (09:15–15:30 IST)                           │
│                                                                     │
│  services/volatility_engine/ (always-running, 4 threads)           │
│                                                                     │
│  Thread 1: Model Loader                                            │
│    Watches data/pinn_models/{symbol}_latest.pt for updates         │
│    Hot-reloads without restart                                     │
│                                                                     │
│  Thread 2: Inference Loop (every 3s)                               │
│    Read data:options_live:{sym} → LTP per strike                   │
│    Read data:tick:{sym} → spot price                               │
│    Read data:options_agg:{sym} → future_price, atm_strike           │
│    Read data:sensibull:{sym} → expiry dates                        │
│    Compute (k, τ) per strike → PINN forward → (μ, v²)              │
│    Invert BS from LTP → live σ → w_market = σ²τ                    │
│    z-score = (w_market - μ) / √v²                                  │
│    Check thresholds → emit signal to pinn:signals stream           │
│    Write pinn:fair_iv:{sym}, pinn:zscore:{sym} to Redis            │
│                                                                     │
│  Thread 3: Arbitrage Monitor (every 30s)                           │
│    Compute g(k) at live strikes via autodiff                       │
│    Alert if g(k) < 0 (model degradation)                           │
│                                                                     │
│  Thread 4: Heartbeat + log level refresh                           │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    SIGNAL INTEGRATION                               │
│                                                                     │
│  pinn:signals stream                                                │
│    │                                                                │
│    ├──→ Paper Trading (new 3rd consumer thread)                    │
│    │      parse_pinn_signal() → EntrySignal                         │
│    │      → check_entry_filters() → build_position() → persist     │
│    │      (identical pipeline to existing 2 sources)                │
│    │                                                                │
│    └──→ Confirmation check in _handle_entry_signal()               │
│           When composite analyser fires SKEW_FADE/RANGE_BOUND,      │
│           reads pinn:zscore:{sym} from Redis:                       │
│           z > 1.0 → boost signal (PINN confirms mispricing)        │
│           z < 0.5 → suppress signal (PINN disagrees)               │
│           0.5 ≤ z ≤ 1.0 → neutral (no modification)                │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. Data Pipeline

### 4.1 NSE Bhavcopy Fetcher

**File**: `tools/pinn_volatility/data/bhavcopy_fetcher.py`

NSE publishes end-of-day derivatives Bhavcopy as a zipped CSV:
```
https://archives.nseindia.com/content/historical/DERIVATIVES/{YYYY}/{MON}/fo{DD}{MON}{YYYY}bhav.csv.zip
```
Example: `fo27JUL2026bhav.csv.zip`

**CSV columns** (after unzip):
```
INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,
SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,BLOCK_TRADE_VAL,
TRADES,OPEN_INT_NUM,CHG_IN_OI_NUM
```

**Fetch logic**:
```python
def fetch_bhavcopy(date: date) -> pd.DataFrame | None:
    """Download + unzip + parse NSE F&O Bhavcopy for a given date.
    
    Returns DataFrame with all F&O instruments (OPTIDX, OPTFUT, FUTIDX, FUTSTK).
    Returns None if the file is not yet published (weekend/holiday/before EOD).
    """
    url = f"https://archives.nseindia.com/content/historical/DERIVATIVES/{date.strftime('%Y')}/{date.strftime('%b').upper()}/fo{date.strftime('%d%b%Y').upper()}bhav.csv.zip"
    # Download with retry (NSE archives can be flaky)
    # Unzip in memory, parse CSV with pandas
    # Return DataFrame
```

**Key rows to extract**:
- `INSTRUMENT == "OPTIDX"` and `SYMBOL in ("NIFTY", "BANKNIFTY", "SENSEX")` → option strikes
- `INSTRUMENT == "FUTIDX"` and `SYMBOL in ("NIFTY", "BANKNIFTY", "SENSEX")` → futures prices (forward price $F$ per expiry)

### 4.2 Strike Filtering & IV Inversion

**File**: `tools/pinn_volatility/data/dataset.py`

```python
def build_training_samples(
    bhavcopy: pd.DataFrame,
    symbols: list[str],
    r: float = 0.07,
    q: float = 0.0,
    k_min: float = -2.0,
    k_max: float = 2.0,
    max_iv: float = 2.0,  # 200%
    min_volume: int = 1,
) -> list[TrainingSample]:
    """Convert Bhavcopy rows into (k, τ, w) training samples.
    
    Steps per row:
      1. Filter: CONTRACTS >= min_volume, SETTLE_PR > 0
      2. Get F (forward price) from FUTIDX row for same (SYMBOL, EXPIRY_DT)
         Fallback: F = S * exp((r-q)*τ) using underlying close
      3. τ = (expiry_date - trade_date).days / 365
      4. k = ln(K / F)
      5. Filter: |k| < k_max (skip deep ITM/OTM)
      6. σ = invert_bs(S, K, τ, r, q, SETTLE_PR, option_type) via Brent's method
         Skip if no solution or σ > max_iv
      7. w = σ² * τ
      8. Return TrainingSample(k=k, tau=τ, w=w, symbol=symbol, strike=K, 
                                option_type=option_type, expiry=expiry_date)
    """
```

**IV inversion** (Brent's method):
```python
from scipy.optimize import brentq

def invert_bs(S, K, tau, r, q, price, option_type, vol_lo=0.001, vol_hi=5.0):
    """Invert Black-Scholes to find implied vol from option price.
    
    Uses Brent's method on the interval [vol_lo, vol_hi].
    Returns None if no root exists (price below intrinsic value).
    """
    def objective(sigma):
        return bs_price(S, K, tau, r, q, sigma, option_type) - price
    
    try:
        # Check that a root exists in the interval
        f_lo = objective(vol_lo)
        f_hi = objective(vol_hi)
        if f_lo * f_hi > 0:
            return None  # No sign change → no solution
        return brentq(objective, vol_lo, vol_hi, xtol=1e-6, maxiter=100)
    except (ValueError, RuntimeError):
        return None
```

**Black-Scholes price** (standard, vectorizable):
```python
def bs_price(S, K, tau, r, q, sigma, option_type):
    if tau <= 0 or sigma <= 0:
        intrinsic = max(S * exp(-q*tau) - K * exp(-r*tau), 0) if option_type == "CE" \
                    else max(K * exp(-r*tau) - S * exp(-q*tau), 0)
        return intrinsic
    d1 = (log(S/K) + (r - q + sigma**2/2) * tau) / (sigma * sqrt(tau))
    d2 = d1 - sigma * sqrt(tau)
    if option_type == "CE":
        return S * exp(-q*tau) * norm_cdf(d1) - K * exp(-r*tau) * norm_cdf(d2)
    else:
        return K * exp(-r*tau) * norm_cdf(-d2) - S * exp(-q*tau) * norm_cdf(-d1)
```

### 4.3 Training Data Volume

Per day per index: ~200 strikes × 2 (CE/PE) = ~400 rows, filtered to ~150 valid (volume > 0, |k| < 2).
Rolling window: **7 days** × 3 indices × ~150 samples = ~3,150 training samples.
Collocation points: 2,000 per epoch (synthetic).
Total per epoch: ~5,150 evaluation points. Trains in <10 min on i5-6200U.

### 4.4 Data Caching

```
data/pinn_training/
  bhavcopy/
    fo27JUL2026bhav.csv     ← cached raw CSVs
    fo28JUL2026bhav.csv
    ...
  datasets/
    NIFTY_7d.parquet         ← preprocessed (k, τ, w) per symbol
    BANKNIFTY_7d.parquet
    SENSEX_7d.parquet
```

Training script checks for cached CSVs before downloading. Only fetches missing days. On weekend/holiday, reuses last available trading day's data.

### 4.5 Gap-Fill via Zerodha

If NSE Bhavcopy is unavailable (server down, archive delayed), fall back to Zerodha `historical_data()`:
- Uses existing `KiteConnect` from `lib/zerodha/zerodha_connect.py:625`
- Reads enctoken from Redis `auth:zerodha` hash
- Fetches daily OHLC for each option instrument (from instruments cache in `span_calculator.py`)
- 3 req/s rate limit → 3 indices × 50 strikes × 1 day = ~50s (only fetch current day, use cached for history)

---

## 5. Model Architecture

**File**: `tools/pinn_volatility/model/pinn.py`

### 5.1 Network Specification

```python
class VolatilityPINN(nn.Module):
    """
    Physics-Informed Neural Network for the implied variance surface.
    
    Input:  (k, τ) — 2D coordinate space
    Output: (μ, log_v²) — mean and log-variance of total implied variance w
    
    Architecture: narrow + deep + smooth (Softplus) for exact 2nd-order autodiff.
    """
    def __init__(self, hidden_dim: int = 128, num_layers: int = 4):
        super().__init__()
        layers = [nn.Linear(2, hidden_dim), nn.Softplus()]
        for _ in range(num_layers - 1):
            layers.extend([nn.Linear(hidden_dim, hidden_dim), nn.Softplus()])
        # Output: mean (μ) and raw log-variance (log_v²)
        # log_v² instead of v² to ensure v² > 0 without softplus on output
        # (softplus on output would limit v² > 0 but make gradients harder)
        layers.append(nn.Linear(hidden_dim, 2))
        self.net = nn.Sequential(*layers)
        
        # Xavier initialization for stable gradients in deep smooth networks
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_normal_(m.weight)
                nn.init.zeros_(m.bias)
    
    def forward(self, k_tau: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """Forward pass. Returns (mu, v_squared) where v_squared = exp(log_v²)."""
        out = self.net(k_tau)
        mu = out[:, 0:1]
        log_v2 = out[:, 1:2]
        v_squared = torch.exp(log_v2) + 1e-8  # variance floor
        return mu, v_squared
    
    def predict_w(self, k_tau: torch.Tensor) -> torch.Tensor:
        """Convenience: return only the mean (fair-value w)."""
        return self.forward(k_tau)[0]
```

### 5.2 Parameter Count

| Layer | Shape | Parameters |
|---|---|---|
| Input → Hidden 1 | 2 × 128 | 384 |
| Hidden 1 → Hidden 2 | 128 × 128 | 16,512 |
| Hidden 2 → Hidden 3 | 128 × 128 | 16,512 |
| Hidden 3 → Hidden 4 | 128 × 128 | 16,512 |
| Hidden 4 → Output | 128 × 2 | 258 |
| **Total** | | **~50,178** |

### 5.3 Input Normalization

Raw inputs have very different scales: $k \in [-2, 2]$, $\tau \in [0.003, 1.0]$. Without normalization, the network's gradients are dominated by $\tau$.

```python
# Normalize inputs to [-1, 1] range
K_RANGE = (-2.0, 2.0)
TAU_RANGE = (0.003, 1.0)

def normalize(k, tau):
    k_norm = 2 * (k - K_RANGE[0]) / (K_RANGE[1] - K_RANGE[0]) - 1
    tau_norm = 2 * (tau - TAU_RANGE[0]) / (TAU_RANGE[1] - TAU_RANGE[0]) - 1
    return torch.stack([k_norm, tau_norm], dim=-1)
```

### 5.4 Per-Symbol Models

Each index (NIFTY, BANKNIFTY, SENSEX) gets its own model. Surfaces differ structurally:
- NIFTY: classic equity skew (left tail heavier)
- BANKNIFTY: steeper skew, higher ATM vol
- SENSEX: similar to NIFTY but different strike spacing

Training 3 independent 50K-param models is cleaner than one conditioned model and avoids the macro-conditioning PDE problem (Decision #2).

---

## 6. Loss Function

**File**: `tools/pinn_volatility/losses/composite.py`

### 6.1 Composite Loss

$$\mathcal{L} = \lambda_{data}\mathcal{L}_{data} + \lambda_{cal}\mathcal{L}_{calendar} + \lambda_{but}\mathcal{L}_{butterfly}$$

Default weights (tunable via config):
- $\lambda_{data} = 1.0$
- $\lambda_{cal} = 1.0$
- $\lambda_{but} = 0.5$ (butterfly is more constraining but harder to satisfy — lower weight prevents it from dominating)

### 6.2 Data Loss — $\beta$-NLL

**File**: `tools/pinn_volatility/losses/data_loss.py`

From Seitzer et al. 2022, "On the Pitfalls of Heteroscedastic Uncertainty Estimation":

```python
def beta_nll_loss(mu, v_squared, target, beta=0.5):
    """β-NLL loss. Detaches v² from residual gradient to prevent variance collapse.
    
    When β=0: standard NLL (susceptible to collapse)
    When β=1: pure MSE (no uncertainty modeling)
    β=0.5: balanced (recommended)
    """
    # Detach v_squared so the network can't reduce NLL by shrinking variance
    v_squared_detached = v_squared.detach()
    
    # NLL with detached variance
    nll = 0.5 * (target - mu)**2 / v_squared_detached + 0.5 * torch.log(v_squared_detached)
    
    # Scale by v^(2β) to re-introduce gradient flow through v²
    # This allows the network to still learn appropriate uncertainty
    weighted = nll * v_squared_detached**beta
    
    return weighted.mean()
```

### 6.3 Calendar Arbitrage Penalty

**File**: `tools/pinn_volatility/losses/arbitrage.py`

Total variance must be non-decreasing with respect to time:

$$\frac{\partial w}{\partial \tau} \geq 0$$

```python
def calendar_penalty(model, k_tau_collocation):
    """Penalty for calendar arbitrage violations.
    
    Evaluated on collocation points (synthetic k, τ pairs).
    Uses autograd to compute ∂w/∂τ.
    """
    k_tau = k_tau_collocation.clone().requires_grad_(True)
    mu, _ = model(k_tau)
    
    # ∂w/∂τ — gradient of output w.r.t. τ (index 1 of input)
    grad_outputs = torch.ones_like(mu)
    grads = torch.autograd.grad(mu, k_tau, grad_outputs=grad_outputs,
                                 create_graph=True)[0]
    dw_dtau = grads[:, 1:2]  # ∂w/∂τ
    
    # Penalty: max(0, -∂w/∂τ)² — penalize only violations (negative derivative)
    violation = torch.clamp(-dw_dtau, min=0.0)
    return (violation ** 2).mean()
```

### 6.4 Butterfly Arbitrage Penalty (Durrleman Condition)

**File**: `tools/pinn_volatility/losses/arbitrage.py`

The risk-neutral density $g(k)$ must be non-negative:

$$g(k) = \left(1 - \frac{kw'}{2w}\right)^2 - \frac{(w')^2}{4}\left(\frac{1}{w} + \frac{1}{4}\right) + \frac{w''}{2} \geq 0$$

where $w' = \frac{\partial w}{\partial k}$ and $w'' = \frac{\partial^2 w}{\partial k^2}$.

```python
def butterfly_penalty(model, k_tau_collocation):
    """Penalty for butterfly arbitrage violations (negative density).
    
    Uses second-order autograd to compute w' and w'' w.r.t. k.
    Requires Softplus activation (ReLU has zero second derivative).
    """
    k_tau = k_tau_collocation.clone().requires_grad_(True)
    mu, _ = model(k_tau)  # w(k, τ)
    w = mu.squeeze(-1)   # (N,)
    
    # First derivative: w' = ∂w/∂k (index 0 of input)
    grad1 = torch.autograd.grad(
        w.sum(), k_tau, create_graph=True, retain_graph=True
    )[0]
    w_prime = grad1[:, 0]  # ∂w/∂k
    
    # Second derivative: w'' = ∂²w/∂k²
    grad2 = torch.autograd.grad(
        w_prime.sum(), k_tau, create_graph=True, retain_graph=True
    )[0]
    w_double_prime = grad2[:, 0]  # ∂²w/∂k²
    
    k = k_tau[:, 0]  # log-moneyness
    
    # Durrleman density g(k)
    # Protect against division by zero (w should be > 0 for valid vol surface)
    w_safe = w.clamp(min=1e-8)
    
    term1 = (1 - k * w_prime / (2 * w_safe))**2
    term2 = (w_prime**2 / 4) * (1 / w_safe + 0.25)
    term3 = w_double_prime / 2
    
    g = term1 - term2 + term3
    
    # Penalty: max(0, -g(k))² — penalize only negative densities
    violation = torch.clamp(-g, min=0.0)
    return (violation ** 2).mean()
```

### 6.5 Collocation Points

**File**: `tools/pinn_volatility/data/collocation.py`

```python
def sample_collocation(n_points=2000, k_range=(-2.0, 2.0), 
                       tau_range=(0.003, 1.0)):
    """Sample collocation points for PDE penalty evaluation.
    
    Strategy: Latin Hypercube Sampling with density weighting.
    60% of points concentrated near ATM (|k| < 0.5) and short-dated (τ < 0.1)
    where the surface has the most curvature. 40% spread across full domain.
    """
    n_concentrated = int(n_points * 0.6)
    n_spread = n_points - n_concentrated
    
    # Concentrated: near ATM, short-dated
    k_conc = np.random.uniform(-0.5, 0.5, n_concentrated)
    tau_conc = np.random.uniform(0.003, 0.15, n_concentrated)
    
    # Spread: full domain via Latin Hypercube
    lhs_k = latin_hypercube(n_spread, k_range[0], k_range[1])
    lhs_tau = latin_hypercube(n_spread, tau_range[0], tau_range[1])
    
    k_all = np.concatenate([k_conc, lhs_k])
    tau_all = np.concatenate([tau_conc, lhs_tau])
    
    # Shuffle
    idx = np.random.permutation(n_points)
    return torch.tensor(np.stack([k_all[idx], tau_all[idx]], axis=1), 
                        dtype=torch.float32)


def latin_hypercube(n, lo, hi):
    """Latin Hypercube Sampling in 1D — better space-filling than uniform."""
    edges = np.linspace(lo, hi, n + 1)
    samples = np.array([np.random.uniform(edges[i], edges[i+1]) for i in range(n)])
    np.random.shuffle(samples)
    return samples
```

Collocation points are **regenerated every 1000 epochs** to prevent the network from overfitting to specific synthetic points.

---

## 7. Training Pipeline

**File**: `tools/pinn_volatility/training/trainer.py`

### 7.1 Two-Stage Optimization

```python
class PINNTrainer:
    def __init__(self, model, config):
        self.model = model
        self.config = config  # PINNConfig from config.py
    
    def train(self, train_samples, val_samples):
        """Two-stage training: Adam (coarse) → L-BFGS (precision)."""
        
        # ── Stage 1: Adam — 5000 epochs, lr=1e-3 ──
        optimizer = torch.optim.Adam(self.model.parameters(), lr=1e-3)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer, T_max=self.config.adam_epochs, eta_min=1e-5
        )
        
        for epoch in range(self.config.adam_epochs):
            # Regenerate collocation every 1000 epochs
            if epoch % 1000 == 0:
                collocation = sample_collocation(self.config.n_collocation)
            
            loss = self._compute_loss(train_samples, collocation)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()
            
            if epoch % 500 == 0:
                metrics = self._validate(val_samples)
                self._log_metrics(epoch, loss, metrics)
        
        # ── Stage 2: L-BFGS — final convergence ──
        # L-BFGS uses full-batch (all training samples + collocation)
        lbfgs = torch.optim.LBFGS(
            self.model.parameters(),
            lr=1.0,
            max_iter=500,
            history_size=50,
            tolerance_grad=1e-7,
            tolerance_change=1e-9,
            line_search_fn="strong_wolfe",
        )
        
        collocation_final = sample_collocation(self.config.n_collocation)
        
        def closure():
            lbfgs.zero_grad()
            loss = self._compute_loss(train_samples, collocation_final)
            loss.backward()
            return loss
        
        lbfgs.step(closure)
        
        # Final validation
        return self._validate(val_samples)
```

### 7.2 Loss Computation

```python
def _compute_loss(self, train_samples, collocation):
    """Composite loss = β-NLL + calendar + butterfly."""
    k_tau_train = normalize(train_samples.k, train_samples.tau)
    mu, v_squared = self.model(k_tau_train)
    
    l_data = beta_nll_loss(mu, v_squared, train_samples.w)
    l_cal = calendar_penalty(self.model, collocation)
    l_but = butterfly_penalty(self.model, collocation)
    
    return (self.config.lambda_data * l_data 
            + self.config.lambda_cal * l_cal 
            + self.config.lambda_but * l_but)
```

### 7.3 Validation

**File**: `tools/pinn_volatility/training/validate.py`

```python
def validate_model(model, val_samples, r=0.07, q=0.0):
    """Check model quality on held-out validation samples.
    
    Returns ValidationMetrics with:
      - rmse: root mean square error on w
      - mae: mean absolute error on σ (IV)
      - calendar_violation_rate: fraction of collocation points where ∂w/∂τ < 0
      - butterfly_violation_rate: fraction where g(k) < 0
      - max_calendar_violation: worst ∂w/∂τ violation magnitude
      - max_butterfly_violation: worst g(k) violation magnitude
    """
    # Forward pass on validation data
    k_tau = normalize(val_samples.k, val_samples.tau)
    with torch.no_grad():
        mu, v_squared = model(k_tau)
    
    # RMSE on w
    rmse = torch.sqrt(((mu.squeeze() - val_samples.w) ** 2).mean())
    
    # MAE on σ (convert w back to IV: σ = sqrt(w/τ))
    sigma_pred = torch.sqrt(mu.squeeze() / val_samples.tau)
    sigma_actual = torch.sqrt(val_samples.w / val_samples.tau)
    mae_sigma = (sigma_pred - sigma_actual).abs().mean()
    
    # Arbitrage violation rates on dense collocation grid
    collocation = sample_collocation(n_points=5000)
    cal_violations = check_calendar_violations(model, collocation)
    but_violations = check_butterfly_violations(model, collocation)
    
    return ValidationMetrics(
        rmse=rmse.item(),
        mae_sigma=mae_sigma.item(),
        calendar_violation_rate=cal_violations['rate'],
        butterfly_violation_rate=but_violations['rate'],
        max_calendar_violation=cal_violations['max'],
        max_butterfly_violation=but_violations['max'],
    )
```

### 7.4 Acceptance Criteria

A trained model is **accepted** (deployed) only if:
- `calendar_violation_rate < 0.01` (less than 1% of collocation points violate)
- `butterfly_violation_rate < 0.05` (less than 5% — butterfly is harder)
- `rmse < 0.01` (on total variance $w$)
- `mae_sigma < 0.02` (2% IV error on average)

If any criterion fails, the **previous model is kept** and an alert is sent via Telegram.

### 7.5 Training Schedule

```
20:00 IST — Orchestrator positional_analysis() starts
~20:30    — Positional analysis completes (217 stocks × 17 analysers)
21:00 IST — PINN training starts (systemd timer or cron)
             Step 1: Fetch Bhavcopy for today + last 6 days (cache check)  ~30s
             Step 2: Parse + filter + IV inversion (3 indices × 7 days)    ~15s
             Step 3: Adam training (5000 epochs, batch=512)                ~5-8 min
             Step 4: L-BFGS convergence                                     ~2-5 min
             Step 5: Validation + acceptance check                          ~30s
             Step 6: Save model + update symlink                             ~1s
             Total: ~10-15 min per symbol, ~30-45 min for all 3 (sequential)
22:00 IST — Volatility-engine hot-reloads new model
```

Training runs as a **batch script** (`tools/pinn_volatility/training/run_training.py`), not a long-running service. Triggered via systemd timer:

**File**: `configs/stockanalysis-pinn-training.timer`
```ini
[Unit]
Description=PINN Nightly Training Timer

[Timer]
OnCalendar=*-*-* 21:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

---

## 8. Inference Service

**File**: `services/volatility_engine/main.py`

### 8.1 Service Overview

Always-running microservice (same pattern as `paper_trading/main.py`). 4 daemon threads + heartbeat. Reads live option data from Redis, runs PINN inference, emits signals.

### 8.2 Thread 1: Model Loader

```python
class ModelManager:
    """Manages per-symbol PINN model loading and hot-reload."""
    
    MODEL_DIR = "data/pinn_models"
    SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX"]
    
    def __init__(self):
        self.models: dict[str, VolatilityPINN] = {}
        self.model_dates: dict[str, str] = {}
        self._load_all()
    
    def _load_all(self):
        for symbol in self.SYMBOLS:
            self._load_symbol(symbol)
    
    def _load_symbol(self, symbol):
        path = os.path.join(self.MODEL_DIR, f"{symbol}_latest.pt")
        if not os.path.exists(path):
            logger.warning(f"[vol-engine] No model for {symbol}, skipping")
            return
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        model = VolatilityPINN()
        model.load_state_dict(checkpoint["model_state"])
        model.eval()
        self.models[symbol] = model
        self.model_dates[symbol] = checkpoint.get("train_date", "unknown")
        logger.info(f"[vol-engine] Loaded {symbol} model (trained {self.model_dates[symbol]})")
    
    def hot_reload_check(self):
        """Check if symlink target changed; reload if so."""
        for symbol in self.SYMBOLS:
            path = os.path.join(self.MODEL_DIR, f"{symbol}_latest.pt")
            if not os.path.exists(path):
                continue
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            if symbol not in self.model_dates:
                self._load_symbol(symbol)
            elif mtime.date().isoformat() != self.model_dates.get(symbol):
                logger.info(f"[vol-engine] New model detected for {symbol}, reloading")
                self._load_symbol(symbol)
    
    def get_model(self, symbol: str) -> VolatilityPINN | None:
        return self.models.get(symbol)
```

### 8.3 Thread 2: Inference Loop

Runs every 3 seconds during market hours (09:15–15:30 IST):

```python
def inference_loop(redis, model_manager):
    """Main inference loop — compare live IV vs PINN fair IV."""
    
    while _running:
        now = datetime.now()
        if not is_market_hours(now):
            time.sleep(30)
            continue
        
        model_manager.hot_reload_check()
        
        for symbol in model_manager.SYMBOLS:
            model = model_manager.get_model(symbol)
            if model is None:
                continue
            
            try:
                signals = evaluate_symbol(symbol, model, redis, now)
                for signal in signals:
                    emit_signal(redis, signal)
                    write_fair_iv_to_redis(redis, symbol, signal)
            except Exception as e:
                logger.error(f"[vol-engine] Error evaluating {symbol}: {e}", exc_info=True)
        
        time.sleep(INFERENCE_INTERVAL_SECONDS)  # 3s
```

### 8.4 Per-Symbol Evaluation

```python
def evaluate_symbol(symbol, model, redis, now) -> list[PinnSignal]:
    """Evaluate all live strikes for one symbol against the PINN model.
    
    Returns list of PinnSignal objects (may be empty if no mispricing detected).
    """
    # ── 1. Read live data from Redis ──
    spot = read_spot(redis, symbol)
    if spot is None or spot <= 0:
        return []
    
    options_live = redis.hgetall(f"data:options_live:{symbol}")
    if not options_live:
        return []
    
    # Get forward price — prefer futures, fall back to computed
    future_price = read_future_price(redis, symbol)
    expiry = read_nearest_expiry(redis, symbol)
    if not expiry:
        return []
    
    tau = compute_tau(expiry, now.date())
    if tau <= 0:
        return []
    
    F = future_price or spot * math.exp((RISK_FREE_RATE - 0.0) * tau)
    
    # ── 2. Compute live IV per strike via BS inversion ──
    strikes_data = []  # list of (strike, option_type, ltp, k, live_sigma, live_w)
    
    for key, raw in options_live.items():
        tick = parse_tick(raw)
        if tick is None or tick_ltp_stale(tick, now):
            continue
        
        ltp = tick.get("ltp", 0)
        if ltp <= 0:
            continue
        
        strike, option_type = parse_strike_key(key)
        k = math.log(strike / F)
        if abs(k) > 2.0:
            continue
        
        live_sigma = invert_bs(spot, strike, tau, RISK_FREE_RATE, 0.0, ltp, option_type)
        if live_sigma is None or live_sigma > 2.0:
            continue
        
        live_w = live_sigma ** 2 * tau
        strikes_data.append(StrikeData(strike, option_type, ltp, k, live_sigma, live_w))
    
    if not strikes_data:
        return []
    
    # ── 3. PINN forward pass ──
    k_tau = torch.tensor([[s.k, tau] for s in strikes_data], dtype=torch.float32)
    k_tau_norm = normalize(k_tau)
    
    with torch.no_grad():
        mu, v_squared = model(k_tau_norm)
    
    std = torch.sqrt(v_squared).squeeze()
    
    # ── 4. Compute z-scores ──
    results = []
    for i, sd in enumerate(strikes_data):
        fair_w = mu[i].item()
        v = std[i].item()
        z = (sd.live_w - fair_w) / max(v, 1e-8)
        results.append(EvalResult(sd, fair_w, v, z))
    
    # ── 5. Check signal thresholds ──
    signals = check_signal_thresholds(symbol, results, now, expiry)
    return signals
```

### 8.5 Signal Thresholds

```python
# Absolute minimum IV difference to avoid noise on near-zero variance
MIN_IV_DIFF_PCT = 2.0  # |σ_live - σ_fair| must be > 2 percentage points

# z-score thresholds
SKEW_FADE_Z_THRESHOLD = 2.0      # one-sided overpricing
RANGE_BOUND_Z_THRESHOLD = 1.5    # both wings overpriced
RANGE_BOUND_ATM_Z_MAX = 1.0      # ATM must NOT be overpriced (shape, not level)
GAMMA_TRAP_Z_THRESHOLD = 3.0     # ATM extremely overpriced + skew inversion


def check_signal_thresholds(symbol, results, now, expiry) -> list[PinnSignal]:
    """Check z-score patterns against signal thresholds.
    
    Three signal types:
      1. SKEW_FADE: one side (CE or PE) has z > 2.0, other side z < 1.0
         → sell the overpriced side via credit spread
      2. RANGE_BOUND: both CE and PE wings have z > 1.5, ATM z < 1.0
         → sell both wings (iron condor / strangle)
      3. (No GAMMA_TRAP from PINN — that's handled by the composite analyser)
    """
    signals = []
    
    # Separate CE and PE results, find max z-score per side
    ce_results = [r for r in results if r.strike_data.option_type == "CE"]
    pe_results = [r for r in results if r.strike_data.option_type == "PE"]
    
    # Find ATM result (k closest to 0)
    atm_result = min(results, key=lambda r: abs(r.strike_data.k))
    
    # ── SKEW_FADE: one side significantly overpriced ──
    ce_max = max(ce_results, key=lambda r: r.z) if ce_results else None
    pe_max = max(pe_results, key=lambda r: r.z) if pe_results else None
    
    if ce_max and ce_max.z > SKEW_FADE_Z_THRESHOLD:
        iv_diff = (ce_max.strike_data.live_sigma - math.sqrt(ce_max.fair_w / tau)) * 100
        if iv_diff >= MIN_IV_DIFF_PCT:
            # CE side overpriced → sell CE → bearish credit spread
            signals.append(PinnSignal(
                symbol=symbol,
                signal_type="SKEW_FADE_SETUP",
                strategy="CREDIT_SPREAD",
                direction="BEARISH",
                overpriced_strike=ce_max.strike_data.strike,
                overpriced_type="CE",
                sr_level=ce_max.strike_data.strike,  # maps to EntrySignal.sr_level
                z_score=ce_max.z,
                fair_iv=math.sqrt(ce_max.fair_w / tau),
                live_iv=ce_max.strike_data.live_sigma,
                expiry=expiry,
                timestamp=now,
            ))
    
    if pe_max and pe_max.z > SKEW_FADE_Z_THRESHOLD:
        iv_diff = (pe_max.strike_data.live_sigma - math.sqrt(pe_max.fair_w / tau)) * 100
        if iv_diff >= MIN_IV_DIFF_PCT:
            # PE side overpriced → sell PE → bullish credit spread
            signals.append(PinnSignal(
                symbol=symbol,
                signal_type="SKEW_FADE_SETUP",
                strategy="CREDIT_SPREAD",
                direction="BULLISH",
                overpriced_strike=pe_max.strike_data.strike,
                overpriced_type="PE",
                sr_level=pe_max.strike_data.strike,
                z_score=pe_max.z,
                fair_iv=math.sqrt(pe_max.fair_w / tau),
                live_iv=pe_max.strike_data.live_sigma,
                expiry=expiry,
                timestamp=now,
            ))
    
    # ── RANGE_BOUND: both wings overpriced, ATM not ──
    if (ce_max and pe_max 
            and ce_max.z > RANGE_BOUND_Z_THRESHOLD 
            and pe_max.z > RANGE_BOUND_Z_THRESHOLD
            and abs(atm_result.z) < RANGE_BOUND_ATM_Z_MAX):
        
        signals.append(PinnSignal(
            symbol=symbol,
            signal_type="RANGE_BOUND_SETUP",
            strategy="IRON_CONDOR" if ce_max and pe_max else "STRANGLE",
            direction="NEUTRAL",
            put_wall_strike=pe_max.strike_data.strike,   # maps to EntrySignal.put_wall_strike
            call_wall_strike=ce_max.strike_data.strike,   # maps to EntrySignal.call_wall_strike
            z_score=min(ce_max.z, pe_max.z),
            fair_iv_ce=math.sqrt(ce_max.fair_w / tau),
            fair_iv_pe=math.sqrt(pe_max.fair_w / tau),
            live_iv_ce=ce_max.strike_data.live_sigma,
            live_iv_pe=pe_max.strike_data.live_sigma,
            expiry=expiry,
            timestamp=now,
        ))
    
    return signals
```

### 8.6 Signal Emission

**Redis stream**: `pinn:signals`

```python
def emit_signal(redis, signal: PinnSignal):
    """Emit PINN signal to Redis stream for paper-trading consumption."""
    fields = {
        "signal_type": signal.signal_type,
        "symbol": signal.symbol,
        "strategy": signal.strategy,
        "direction": signal.direction,
        "z_score": str(signal.z_score),
        "expiry": signal.expiry,
        "timestamp": signal.timestamp.isoformat(),
        "signal_source": "PINN_MISPRICING",
    }
    if signal.signal_type == "SKEW_FADE_SETUP":
        fields["sr_level"] = str(signal.sr_level)
        fields["overpriced_type"] = signal.overpriced_type
        fields["fair_iv"] = str(signal.fair_iv)
        fields["live_iv"] = str(signal.live_iv)
    elif signal.signal_type == "RANGE_BOUND_SETUP":
        fields["put_wall_strike"] = str(signal.put_wall_strike)
        fields["call_wall_strike"] = str(signal.call_wall_strike)
        fields["fair_iv_ce"] = str(signal.fair_iv_ce)
        fields["fair_iv_pe"] = str(signal.fair_iv_pe)
        fields["live_iv_ce"] = str(signal.live_iv_ce)
        fields["live_iv_pe"] = str(signal.live_iv_pe)
    
    redis.xadd("pinn:signals", fields, maxlen=1000)
```

### 8.7 Fair IV Cache (for confirmation mode)

```python
def write_fair_iv_to_redis(redis, symbol, results):
    """Write per-strike fair IV + z-scores to Redis for confirmation checks.
    
    Paper-trading's _handle_entry_signal() reads this when a composite 
    analyser signal arrives, to check if PINN confirms the mispricing.
    """
    mapping = {}
    for r in results:
        key = f"{r.strike_data.strike}_{r.strike_data.option_type}"
        mapping[f"fair_iv_{key}"] = str(math.sqrt(r.fair_w / tau))
        mapping[f"zscore_{key}"] = str(r.z)
    mapping["last_updated"] = str(time.time())
    redis.hset(f"pinn:zscore:{symbol}", mapping=mapping)
    redis.expire(f"pinn:zscore:{symbol}", 30)  # 30s TTL — stale data expires fast
```

### 8.8 Thread 3: Arbitrage Monitor

```python
def arbitrage_monitor(redis, model_manager):
    """Every 30s, check if the PINN's own surface violates no-arbitrage.
    
    This catches model degradation (e.g., training failed to converge on
    a particular day). If g(k) < 0 at any live strike, alert immediately.
    """
    while _running:
        time.sleep(30)
        now = datetime.now()
        if not is_market_hours(now):
            continue
        
        for symbol in model_manager.SYMBOLS:
            model = model_manager.get_model(symbol)
            if model is None:
                continue
            
            try:
                violations = check_live_arbitrage(symbol, model, redis, now)
                if violations:
                    alert_msg = format_arbitrage_alert(symbol, violations)
                    TELEGRAM_NOTIFICATIONS.send_live_options_notification(
                        alert_msg, parse_mode="HTML", symbol=symbol
                    )
                    logger.warning(f"[vol-engine] Arbitrage violations for {symbol}: {violations}")
            except Exception as e:
                logger.error(f"[vol-engine] Arbitrage monitor error for {symbol}: {e}")
```

---

## 9. Signal Integration with Paper Trading

### 9.1 New Consumer Thread in Paper Trading

**File**: `services/paper_trading/main.py` — add 6th thread

```python
PINN_GROUP = "paper-trader-pinn"
PINN_STREAM = "pinn:signals"

def pinn_signal_consumer(redis):
    """Consume pinn:signals stream → entry_queue (same as analysis + confluence)."""
    try:
        redis.xgroup_create(PINN_GROUP, PINN_STREAM, mkstream=True)
    except Exception as e:
        logger.debug("[paper-trading] xgroup_create %s: %s", PINN_GROUP, e)
    
    while _running:
        try:
            messages = redis.xreadgroup(
                PINN_GROUP, CONSUMER_NAME,
                {PINN_STREAM: ">"}, count=10, block=5000,
            )
        except Exception as e:
            logger.error("[paper-trading] pinn consumer error: %s", e, exc_info=True)
            time.sleep(2)
            continue
        if not messages:
            continue
        entries = messages[0][1] if isinstance(messages, list) and messages else []
        for msg_id, fields in entries:
            try:
                signal = parse_pinn_signal(fields)
                if signal:
                    entry_queue.put(signal)
            except Exception as e:
                logger.exception("[paper-trading] Error parsing pinn signal %s: %s", msg_id, e)
            finally:
                try:
                    redis.xack(PINN_STREAM, PINN_GROUP, msg_id)
                except Exception:
                    pass
```

### 9.2 Signal Parser

**File**: `services/paper_trading/signal_router.py` — add new function

```python
def parse_pinn_signal(fields: dict) -> Optional[EntrySignal]:
    """Parse a pinn:signals stream message into an EntrySignal.
    
    Maps PINN signal types to the existing EntrySignal schema so
    strategy_builder.py can process it without modification.
    """
    signal_type = fields.get("signal_type", "")
    symbol = fields.get("symbol", "")
    
    if not symbol or symbol not in LIVE_OPTIONS_INDICES:
        return None
    
    if signal_type == "SKEW_FADE_SETUP":
        return EntrySignal(
            strategy="CREDIT_SPREAD",
            symbol=symbol,
            direction=fields.get("direction", "NEUTRAL"),
            sr_level=float(fields.get("sr_level", 0)),
            signal_source="PINN_MISPRICING",
            score=float(fields.get("z_score", 0)),
            mode="intraday",  # PINN signals are always intraday
            signal_context={
                "fair_iv": float(fields.get("fair_iv", 0)),
                "live_iv": float(fields.get("live_iv", 0)),
                "overpriced_type": fields.get("overpriced_type", ""),
            },
        )
    
    if signal_type == "RANGE_BOUND_SETUP":
        return EntrySignal(
            strategy=fields.get("strategy", "STRANGLE"),
            symbol=symbol,
            direction="NEUTRAL",
            put_wall_strike=float(fields.get("put_wall_strike", 0)) or None,
            call_wall_strike=float(fields.get("call_wall_strike", 0)) or None,
            signal_source="PINN_MISPRICING",
            score=float(fields.get("z_score", 0)),
            mode="intraday",
            signal_context={
                "fair_iv_ce": float(fields.get("fair_iv_ce", 0)),
                "fair_iv_pe": float(fields.get("fair_iv_pe", 0)),
            },
        )
    
    return None
```

This maps to `strategy_builder.py:select_strikes()`:
- `IRON_CONDOR` + `put_wall_strike`/`call_wall_strike` → sell at walls, buy 2 strikes away (**works directly, no change needed**)
- `CREDIT_SPREAD` + `sr_level` → **requires one-line change** to `select_strikes()`

**Required change to `strategy_builder.py:select_strikes()`** (line 101):

Current code checks `signal.signal_source == "SKEW_FADE_SETUP"` to decide whether to use `sr_level` for CREDIT_SPREAD strikes. PINN signals use `signal_source = "PINN_MISPRICING"`, so they'd fall through to the CONFLUENCE path (which uses `atm_strike ± strike_gap` instead of the specific mispriced strike).

Fix: replace the source-string check with a presence check:

```python
# Before:
if signal.signal_source == "SKEW_FADE_SETUP":
    if signal.sr_level is None:
        return []
    ...

# After:
if signal.sr_level is not None:
    # Any signal with sr_level set (SKEW_FADE_SETUP or PINN_MISPRICING)
    # uses the specific strike as the short leg.
    if signal.direction == "BULLISH":
        return [
            PlannedLeg(signal.sr_level, "PE", "SELL"),
            PlannedLeg(signal.sr_level - 2 * strike_gap, "PE", "BUY"),
        ]
    return [
        PlannedLeg(signal.sr_level, "CE", "SELL"),
        PlannedLeg(signal.sr_level + 2 * strike_gap, "CE", "BUY"),
    ]
```

This is more robust — any signal source that provides `sr_level` gets the correct behavior, not just `SKEW_FADE_SETUP`. The CONFLUENCE path (no `sr_level`, uses `atm_strike`) remains unchanged as the `else` branch.

**No changes needed to `engine.py`** — the exit rules work identically for PINN-sourced positions.

### 9.3 Confirmation Mode (Composite ↔ PINN)

**File**: `services/paper_trading/main.py` — modify `_handle_entry_signal()`

```python
def _handle_entry_signal(redis, span_calculator, signal) -> None:
    account = get_account(redis)
    open_positions = load_open_positions(redis)
    
    passed, reason = check_entry_filters(signal, redis, account, open_positions)
    if not passed:
        logger.debug("[paper-trading] Entry rejected for %s/%s: %s", signal.symbol, signal.strategy, reason)
        return
    
    # ── PINN confirmation check (only for non-PINN signals) ──
    if signal.signal_source != "PINN_MISPRICING":
        pinn_z = get_pinn_confirmation(redis, signal)
        if pinn_z is not None:
            if pinn_z < 0.5:
                logger.info("[paper-trading] %s %s suppressed by PINN (z=%.2f < 0.5)",
                           signal.symbol, signal.strategy, pinn_z)
                return  # PINN disagrees — suppress
            elif pinn_z > 1.0:
                logger.info("[paper-trading] %s %s boosted by PINN (z=%.2f > 1.0)",
                           signal.symbol, signal.strategy, pinn_z)
                # Boost: increase score in signal_context for logging
                signal.signal_context["pinn_confirmed"] = True
                signal.signal_context["pinn_z"] = pinn_z
    
    position = build_position(signal, redis, span_calculator, account, mode=signal.mode)
    if position is None:
        return
    persist_new_position(redis, position)


def get_pinn_confirmation(redis, signal) -> Optional[float]:
    """Read pinn:zscore:{symbol} from Redis and return max z-score
    on the relevant side for this signal.
    
    Returns None if PINN data is stale or unavailable (fail-open: allow trade).
    """
    raw = redis.hgetall(f"pinn:zscore:{signal.symbol}")
    if not raw:
        return None  # PINN not running — fail open
    
    last_updated = float(raw.get("last_updated", 0))
    if time.time() - last_updated > 30:
        return None  # stale — fail open
    
    # For SKEW_FADE: check z-score on the overpriced side
    # For RANGE_BOUND: check max z across both wings
    if signal.strategy == "CREDIT_SPREAD" and signal.sr_level:
        option_type = "PE" if signal.direction == "BULLISH" else "CE"
        key = f"zscore_{signal.sr_level}_{option_type}"
        z = raw.get(key)
        return float(z) if z else None
    
    if signal.strategy in ("IRON_CONDOR", "STRANGLE"):
        z_scores = [float(v) for k, v in raw.items() 
                    if k.startswith("zscore_") and float(v) > 0]
        return max(z_scores) if z_scores else None
    
    return None
```

---

## 10. Greeks Computation

**File**: `tools/pinn_volatility/model/greeks.py`

### 10.1 From PINN to Black-Scholes Greeks

The PINN outputs $w(k, \tau) = \sigma^2 \tau$. To get Greeks:

1. Extract $\sigma_{imp} = \sqrt{w / \tau}$
2. Feed into standard Black-Scholes Greek formulas

```python
def compute_greeks(model, S, K, tau, r, q, option_type, F=None):
    """Compute BS Greeks using PINN-derived implied volatility.
    
    The advantage: σ_imp is guaranteed arbitrage-free (if PINN training
    converged), so Greeks are consistent across the entire surface.
    
    Returns: dict with delta, gamma, theta, vega, vanna, volga
    """
    if F is None:
        F = S * math.exp((r - q) * tau)
    k = math.log(K / F)
    
    # PINN forward pass
    k_tau = torch.tensor([[k, tau]], dtype=torch.float32)
    k_tau_norm = normalize(k_tau)
    
    with torch.no_grad():
        mu, v_squared = model(k_tau_norm)
    
    sigma = math.sqrt(max(mu.item(), 1e-8) / tau)
    
    # Standard BS Greeks with PINN-derived sigma
    d1 = (math.log(S / K) + (r - q + sigma**2 / 2) * tau) / (sigma * math.sqrt(tau))
    d2 = d1 - sigma * math.sqrt(tau)
    
    # PDF of standard normal
    n_d1 = math.exp(-d1**2 / 2) / math.sqrt(2 * math.pi)
    
    delta = math.exp(-q * tau) * norm_cdf(d1) if option_type == "CE" \
            else -math.exp(-q * tau) * norm_cdf(-d1)
    gamma = n_d1 * math.exp(-q * tau) / (S * sigma * math.sqrt(tau))
    vega = S * math.exp(-q * tau) * n_d1 * math.sqrt(tau) / 100  # per 1% IV change
    theta = (-(S * n_d1 * sigma * math.exp(-q * tau)) / (2 * math.sqrt(tau))
             - r * K * math.exp(-r * tau) * norm_cdf(d2)
             + q * S * math.exp(-q * tau) * norm_cdf(d1)) / 365  # per day
    
    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "sigma_pinn": sigma,
    }
```

### 10.2 Integration with Paper Trading Exit Engine

**File**: `services/paper_trading/engine.py` — enhance exit rules (future phase)

Current crude exit rules → PINN-enhanced:

| Exit Rule | Current (Crude) | PINN-Enhanced (Future) |
|---|---|---|
| GAMMA_TRAP | Spot moved > 2% from close | $\Gamma_{portfolio} \times \Delta S$ exceeds threshold |
| Theta decay | 75% of entry credit | $\Theta_{portfolio} \times dt$ vs credit decay rate |
| Stop loss | 200% of entry credit | Delta-based: if $\|\Delta_{net}\|$ exceeds threshold, close |

**Note**: Greeks enhancement is **Phase 3** (after PINN signals are validated in paper trading). The existing exit rules work fine for v1.

---

## 11. Monitoring & Observability

### 11.1 Redis Keys

| Key | Type | TTL | Written by | Purpose |
|---|---|---|---|---|
| `pinn:signals` | Stream | maxlen=1000 | volatility-engine | Signal stream for paper-trading |
| `pinn:zscore:{sym}` | Hash | 30s | volatility-engine | Per-strike z-scores for confirmation mode |
| `pinn:fair_iv:{sym}` | Hash | 30s | volatility-engine | Per-strike fair IV (debugging) |
| `pinn:status` | Hash | 120s | volatility-engine | Service status + last inference time |
| `pinn:model:{sym}` | Hash | none | training script | Model metadata (train_date, metrics) |
| `service:registry:volatility-engine` | Hash | 120s | volatility-engine | Heartbeat for service registry |

### 11.2 Telegram Bot Command

**File**: `lib/notification/commands/pinn_cmds.py`

```python
async def cmd_pinn_status(update, context):
    """Show PINN model status, last training date, violation rates, current z-scores."""
    # Read from Redis:
    #   pinn:status → last inference, model dates
    #   pinn:model:{sym} → training metrics
    #   pinn:zscore:{sym} → current max z-scores per symbol
    
    # Format:
    # 🧮 PINN Volatility Engine Status
    # 
    # Models:
    #   NIFTY: trained 2026-08-01, RMSE=0.003, cal_viol=0.2%, but_viol=1.1%
    #   BANKNIFTY: trained 2026-08-01, RMSE=0.004, cal_viol=0.1%, but_viol=0.8%
    #   SENSEX: trained 2026-07-31, RMSE=0.002, cal_viol=0.3%, but_viol=2.1%
    # 
    # Live z-scores (max per side):
    #   NIFTY: CE max z=1.2 @24600, PE max z=2.8 @23800 [⚠️ PE SKEW_FADE]
    #   BANKNIFTY: CE max z=0.8, PE max z=0.9 [normal]
    #   SENSEX: CE max z=1.6, PE max z=1.7 [⚠️ RANGE_BOUND]

HANDLERS = [
    ("pinn_status", cmd_pinn_status),
]
```

Register in `lib/notification/commands/__init__.py`.

### 11.3 Training Alerts

The training script sends Telegram alerts on:
- **Training started**: "🧮 PINN training started for NIFTY/BANKNIFTY/SENSEX"
- **Training succeeded**: "✅ PINN trained: NIFTY RMSE=0.003, cal=0.2%, but=1.1%"
- **Training failed** (acceptance criteria not met): "❌ PINN training FAILED for NIFTY: but_viol=12% > 5% threshold. Previous model retained."
- **Bhavcopy unavailable**: "⚠️ NSE Bhavcopy unavailable for 2026-08-01. Using Zerodha fallback."

---

## 12. Configuration

**File**: `tools/pinn_volatility/config.py`

```python
from dataclasses import dataclass, field

@dataclass
class PINNConfig:
    # ── Model architecture ──
    hidden_dim: int = 128
    num_layers: int = 4
    activation: str = "softplus"  # only softplus supports 2nd-order autodiff
    
    # ── Training ──
    adam_epochs: int = 5000
    adam_lr: float = 1e-3
    adam_lr_min: float = 1e-5
    batch_size: int = 512
    lbfgs_max_iter: int = 500
    lbfgs_history_size: int = 50
    
    # ── Loss weights ──
    lambda_data: float = 1.0
    lambda_calendar: float = 1.0
    lambda_butterfly: float = 0.5
    beta_nll: float = 0.5  # β-NLL parameter
    
    # ── Collocation ──
    n_collocation: int = 2000
    collocation_regen_every: int = 1000
    k_range: tuple[float, float] = (-2.0, 2.0)
    tau_range: tuple[float, float] = (0.003, 1.0)
    collocation_concentrated_frac: float = 0.6  # 60% near ATM/short-dated
    
    # ── Data ──
    symbols: list[str] = field(default_factory=lambda: ["NIFTY", "BANKNIFTY", "SENSEX"])
    training_window_days: int = 7
    risk_free_rate: float = 0.07
    dividend_yield: float = 0.0
    max_iv: float = 2.0  # 200% — skip higher
    min_volume: int = 1
    k_max: float = 2.0
    
    # ── Validation acceptance ──
    max_calendar_violation_rate: float = 0.01
    max_butterfly_violation_rate: float = 0.05
    max_rmse: float = 0.01
    max_mae_sigma: float = 0.02
    
    # ── Inference ──
    inference_interval_seconds: float = 3.0
    skew_fade_z_threshold: float = 2.0
    range_bound_z_threshold: float = 1.5
    range_bound_atm_z_max: float = 1.0
    gamma_trap_z_threshold: float = 3.0
    min_iv_diff_pct: float = 2.0
    
    # ── Paths ──
    model_dir: str = "data/pinn_models"
    training_data_dir: str = "data/pinn_training"
    
    # ── Confirmation mode ──
    confirmation_suppress_z: float = 0.5   # suppress composite signal if z < this
    confirmation_boost_z: float = 1.0      # boost composite signal if z > this
```

---

## 13. File Structure

```
tools/pinn_volatility/
  __init__.py
  config.py                           ← PINNConfig dataclass
  data/
    __init__.py
    bhavcopy_fetcher.py               ← NSE Bhavcopy download + parse
    dataset.py                        ← Strike filtering, IV inversion, (k,τ,w) construction
    collocation.py                    ← LHS collocation point sampling
  model/
    __init__.py
    pinn.py                           ← VolatilityPINN nn.Module
    greeks.py                         ← BS Greeks from PINN-derived σ
    bs_utils.py                       ← Black-Scholes price + IV inversion (shared by training + inference)
  losses/
    __init__.py
    data_loss.py                      ← β-NLL loss
    arbitrage.py                      ← Calendar + butterfly (Durrleman) penalties
    composite.py                      ← Weighted combination
  training/
    __init__.py
    trainer.py                        ← Adam → L-BFGS two-stage loop
    validate.py                       ← Acceptance criteria + violation checks
    run_training.py                   ← CLI entry point (triggered by systemd timer)
  inference/
    __init__.py
    comparator.py                     ← Live IV vs PINN fair IV, z-score computation
    signal_emitter.py                 ← Threshold checking + signal emission to Redis

services/volatility_engine/
  __init__.py
  main.py                             ← Always-running inference service (4 threads)

lib/notification/commands/
  pinn_cmds.py                        ← /pinn_status bot command

configs/
  stockanalysis-volatility-engine.service   ← systemd unit for inference service
  stockanalysis-pinn-training.service       ← systemd unit for nightly training script
  stockanalysis-pinn-training.timer         ← systemd timer (21:00 IST)

tests/pinn/
  __init__.py
  conftest.py                         ← Shared fixtures (synthetic vol surface, mock model)
  test_dataset.py                     ← Bhavcopy parsing, IV inversion, filtering
  test_collocation.py                 ← LHS sampling, density weighting
  test_pinn_model.py                  ← Forward pass, derivative computation, parameter count
  test_losses.py                      ← β-NLL, calendar penalty, butterfly penalty
  test_trainer.py                     ← Training loop on synthetic data, convergence
  test_validator.py                   ← Acceptance criteria, violation detection
  test_comparator.py                  ← Z-score computation, threshold logic
  test_signal_emitter.py              ← Signal format, stream emission
  test_greeks.py                      ← Greeks from PINN σ vs analytical BS
  test_signal_router_pinn.py          ← parse_pinn_signal() → EntrySignal mapping
```

---

## 14. Implementation Plan (Phased)

### Phase 1: Core Training Pipeline (no inference, no integration)
**Effort: ~12h**

1. `config.py` — PINNConfig dataclass
2. `model/bs_utils.py` — Black-Scholes price + Brent's IV inversion
3. `data/bhavcopy_fetcher.py` — NSE download + parse + cache
4. `data/dataset.py` — Strike filtering, (k, τ, w) construction, train/val split
5. `data/collocation.py` — LHS sampling
6. `model/pinn.py` — VolatilityPINN nn.Module
7. `losses/data_loss.py` — β-NLL
8. `losses/arbitrage.py` — Calendar + butterfly penalties
9. `losses/composite.py` — Weighted combination
10. `training/trainer.py` — Adam → L-BFGS
11. `training/validate.py` — Acceptance criteria
12. `training/run_training.py` — CLI entry point
13. Tests: `test_dataset.py`, `test_collocation.py`, `test_pinn_model.py`, `test_losses.py`, `test_trainer.py`, `test_validator.py`

**Deliverable**: Can run `python -m tools.pinn_volatility.training.run_training` and produce a validated `.pt` model file from NSE Bhavcopy data.

### Phase 2: Inference Service + Signal Integration
**Effort: ~10h**

1. `inference/comparator.py` — Live data reading, z-score computation
2. `inference/signal_emitter.py` — Threshold logic, Redis stream emission
3. `services/volatility_engine/main.py` — 4-thread service
4. `services/paper_trading/signal_router.py` — Add `parse_pinn_signal()`
5. `services/paper_trading/strategy_builder.py` — One-line fix: `signal.signal_source == "SKEW_FADE_SETUP"` → `signal.sr_level is not None` in `select_strikes()` CREDIT_SPREAD branch
6. `services/paper_trading/main.py` — Add pinn_signal_consumer thread (6th) + confirmation check in `_handle_entry_signal()`
7. `lib/notification/commands/pinn_cmds.py` — `/pinn_status` command
8. `lib/notification/commands/__init__.py` — Register pinn commands
9. `configs/stockanalysis-volatility-engine.service` — systemd unit
10. `configs/stockanalysis-pinn-training.service` + `.timer` — nightly training
11. Tests: `test_comparator.py`, `test_signal_emitter.py`, `test_signal_router_pinn.py`

**Deliverable**: Volatility-engine running on server, emitting signals to paper-trading during market hours. Paper-trading opens/closes positions based on PINN signals.

### Phase 3: Greeks Enhancement + Backtesting
**Effort: ~12h**

1. `model/greeks.py` — BS Greeks from PINN σ
2. Enhance `engine.py` exit rules with delta/gamma/theta
3. Backtesting script: replay 30 days of Bhavcopy through PINN → simulate paper trades → compute win rate, Sharpe, max drawdown
4. Signal calibration: find optimal z-score thresholds from backtest
5. Tests: `test_greeks.py`, backtest validation

**Deliverable**: Greeks-enhanced exit engine + validated signal thresholds from historical data.

### Phase 4: SSVI Baseline + Comparison
**Effort: ~6h**

1. Implement SSVI parameterization (Gatheral & Jacquier 2014)
2. Fit SSVI to same training data
3. Compare PINN vs SSVI: RMSE, violation rates, signal quality
4. If SSVI performs comparably, use it as a fallback when PINN training fails

**Deliverable**: SSVI baseline model + comparison report. Decision: keep PINN, use SSVI, or ensemble.

---

## 15. Dependencies

### 15.1 New Python Packages

| Package | Purpose | Size | Server Impact |
|---|---|---|---|
| `torch` (CPU-only) | PINN model + autograd | ~200MB | `pip install torch --index-url https://download.pytorch.org/whl/cpu` |
| `scipy` | Brent's method (IV inversion) | ~40MB | `pip install scipy` |
| `pyarrow` | Parquet I/O for training data cache | ~30MB | `pip install pyarrow` |

numpy, pandas, requests already installed.

### 15.2 Server Resource Impact

| Resource | Current Usage | PINN Additional | Total | Limit |
|---|---|---|---|---|
| CPU (training) | ~30% at 20:00 | +60% at 21:00 (sequential, no overlap) | 60% peak | 80% quota |
| CPU (inference) | ~20% during market | +5% (3s interval, <2ms inference) | 25% | 80% quota |
| Memory (training) | 1.6GB used | +500MB (PyTorch + data) | 2.1GB | 7.6GB total |
| Memory (inference) | 1.6GB used | +100MB (3 models × 50K params) | 1.7GB | 7.6GB total |
| Disk | logs + SQLite | +~5MB/model × 3 symbols × 30 days = ~450MB | +450MB | ample |

Training at 21:00 does NOT overlap with positional analysis at 20:00 (completes by ~20:30). If positional runs late, training script waits for Redis `service:registry:orchestrator` to show `idle` or `shutdown` before starting.

### 15.3 requirements.txt Update

```diff
+ torch>=2.0.0  # CPU-only — install with: pip install torch --index-url https://download.pytorch.org/whl/cpu
+ scipy>=1.11.0
+ pyarrow>=14.0.0
```

---

## 16. Test Plan

### 16.1 Unit Tests (Phase 1)

**test_dataset.py**:
- `test_bhavcopy_parsing`: Parse a sample CSV, verify column extraction
- `test_strike_filtering`: Volume=0, IV>200%, |k|>2 → excluded
- `test_iv_inversion`: Known σ → BS price → invert → recover σ within 1e-4
- `test_iv_inversion_no_solution`: Price below intrinsic → returns None
- `test_tau_computation`: Expiry in 30 days → τ ≈ 0.082
- `test_k_computation`: K=24000, F=24250 → k = ln(24000/24250) ≈ -0.0104

**test_collocation.py**:
- `test_lhs_space_filling`: 1000 samples → all bins covered
- `test_density_weighting`: 60% concentrated → |k| < 0.5 for 60% of points
- `test_collocation_shape`: Returns (N, 2) tensor

**test_pinn_model.py**:
- `test_forward_shape`: Input (B, 2) → output mu (B, 1), v² (B, 1)
- `test_param_count`: ~50K parameters
- `test_v_squared_positive`: v² > 0 for all inputs (exp output)
- `test_second_derivative_exists`: w'' is non-zero (Softplus, not ReLU)
- `test_xavier_init`: Weights have reasonable distribution

**test_losses.py**:
- `test_beta_nll_zero_beta`: β=0 → standard NLL behavior
- `test_beta_nll_prevents_collapse`: Train on outlier → v² doesn't collapse to 0
- `test_calendar_penalty_no_violation`: Monotonic w → penalty = 0
- `test_calendar_penalty_violation`: Non-monotonic w → penalty > 0
- `test_butterfly_penalty_valid_surface`: Positive density → penalty = 0
- `test_butterfly_penalty_negative_density`: Smile too sharp → penalty > 0
- `test_durrleman_formula`: Hand-computed g(k) matches autograd g(k)

**test_trainer.py**:
- `test_adam_convergence`: Synthetic linear surface → RMSE decreases
- `test_lbfgs_precision`: After L-BFGS → violation rate < 1%
- `test_collocation_regen`: Collocation changes every 1000 epochs

**test_validator.py**:
- `test_acceptance_pass`: Good model → accepted
- `test_acceptance_fail_calendar`: cal_viol > 1% → rejected
- `test_acceptance_fail_butterfly`: but_viol > 5% → rejected
- `test_acceptance_fail_rmse`: RMSE > 0.01 → rejected

### 16.2 Integration Tests (Phase 2)

**test_comparator.py**:
- `test_evaluate_symbol`: Mock Redis data → z-scores computed correctly
- `test_stale_tick_skip`: Tick timestamp > 30s old → skipped
- `test_zero_ltp_skip`: ltp=0 → skipped
- `test_deep_otm_skip`: |k| > 2.0 → skipped
- `test_forward_price_fallback`: No future_price → uses S*exp((r-q)τ)

**test_signal_emitter.py**:
- `test_skew_fade_ce_overpriced`: CE z > 2.0, PE z < 1.0 → SKEW_FADE signal
- `test_skew_fade_pe_overpriced`: PE z > 2.0, CE z < 1.0 → SKEW_FADE signal
- `test_range_bound_both_wings`: CE z > 1.5, PE z > 1.5, ATM z < 1.0 → RANGE_BOUND signal
- `test_range_bound_atm_overpriced`: ATM z > 1.0 → no RANGE_BOUND (level, not shape)
- `test_min_iv_diff_filter`: z > 2.0 but |IV_diff| < 2% → no signal
- `test_no_signal_normal`: All z < 1.5 → no signal

**test_signal_router_pinn.py**:
- `test_parse_skew_fade`: Stream fields → EntrySignal(strategy=CREDIT_SPREAD, sr_level=X)
- `test_parse_range_bound`: Stream fields → EntrySignal(strategy=IRON_CONDOR, walls=X, Y)
- `test_parse_invalid_symbol`: Non-index symbol → None
- `test_parse_missing_fields`: Malformed message → None

### 16.3 Greeks Tests (Phase 3)

**test_greeks.py**:
- `test_delta_atm`: ATM call delta ≈ 0.5
- `test_delta_deep_itm`: Deep ITM call delta ≈ 1.0
- `test_delta_deep_otm`: Deep OTM call delta ≈ 0.0
- `test_gamma_positive`: Gamma > 0 for all strikes
- `test_gamma_atm_max`: ATM gamma > OTM gamma
- `test_theta_negative`: Theta < 0 for long options
- `test_vega_positive`: Vega > 0 for all options
- `test_greeks_vs_analytical`: PINN σ → BS Greeks match analytical formulas

---

## 17. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| NSE Bhavcopy unavailable | Medium | Training skipped | Zerodha `historical_data()` fallback; previous model kept |
| PINN training doesn't converge | Low | No new model | Acceptance criteria gate; previous model retained; alert sent |
| Server OOM during training | Low | Service crash | CPU/memory monitoring; PyTorch CPU-only (~500MB); 6GB free |
| Butterfly penalty gradient explosion | Medium | NaN loss | Gradient clipping (max_norm=1.0); log_v² output prevents v²→∞ |
| Live IV inversion fails for most strikes | Medium | No signals | Fallback to Sensibull ATM IV for approximate comparison |
| PINN signals too noisy (false positives) | Medium | Paper trading losses | Z-score threshold tuning via backtesting (Phase 3); cooldown in paper-trading prevents rapid re-entry |
| Model overfitting to 7-day window | Medium | Poor generalization | Validation on held-out day; increase window if RMSE unstable |
| Autograd memory leak in inference | Low | OOM over hours | `torch.no_grad()` for forward pass; `gc.collect()` every 30s in heartbeat |

---

## 18. Key Formulas Reference

### 18.1 Log-Moneyness
$$k = \ln\left(\frac{K}{F}\right), \quad F = S \cdot e^{(r-q)\tau}$$

### 18.2 Total Implied Variance
$$w(k, \tau) = \sigma_{imp}^2 \cdot \tau$$

### 18.3 β-NLL Loss
$$\mathcal{L}_{data} = \frac{1}{N} \sum_{i=1}^{N} \left[ \frac{(w_i - \mu_i)^2}{2 \hat{v}_i^{2\beta} \cdot v_i^{2(1-\beta)}} + \frac{\log v_i^2}{2} \right]$$

where $\hat{v}_i^2 = v_i^2.\text{detach}()$ (gradient detached).

### 18.4 Calendar Arbitrage
$$\mathcal{L}_{calendar} = \frac{1}{N} \sum \max\left(0, -\frac{\partial w}{\partial \tau}\right)^2$$

### 18.5 Butterfly Arbitrage (Durrleman Condition)
$$g(k) = \left(1 - \frac{kw'}{2w}\right)^2 - \frac{(w')^2}{4}\left(\frac{1}{w} + \frac{1}{4}\right) + \frac{w''}{2} \geq 0$$

$$\mathcal{L}_{butterfly} = \frac{1}{N} \sum \max(0, -g(k))^2$$

### 18.6 Z-Score (Signal Generation)
$$z = \frac{w_{market} - \hat{\mu}_{PINN}}{\sqrt{\hat{v}^2_{PINN}}}$$

### 18.7 Black-Scholes Greeks (from PINN σ)
$$\Delta = e^{-q\tau} N(d_1), \quad \Gamma = \frac{n(d_1) e^{-q\tau}}{S \sigma \sqrt{\tau}}, \quad \Theta = -\frac{S n(d_1) \sigma e^{-q\tau}}{2\sqrt{\tau}} - rKe^{-r\tau}N(d_2) + qSe^{-q\tau}N(d_1)$$

where $d_1 = \frac{\ln(S/K) + (r-q+\sigma^2/2)\tau}{\sigma\sqrt{\tau}}$, $d_2 = d_1 - \sigma\sqrt{\tau}$, $\sigma = \sqrt{w_{PINN}/\tau}$.
