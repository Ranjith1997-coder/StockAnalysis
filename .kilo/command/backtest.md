---
description: Run a walk-forward backtest for a strategy and report OOS stats
---

Run a backtest following the `backtest-methodology` skill. $ARGUMENTS
(e.g. `strategy=delta_iron_condor start=2025-01-01 end=2026-06-30`).

If `tools/options_backtest/` does not exist yet (Milestone M2 in
`.kilo/plans/options-programme-plan.md`), say so and stop — do not invent a
parallel backtester.

Steps:
1. Confirm the strategy's entry/exit rules are the ones used by paper trading
   (`services/paper_trading/`), not a copy.
2. Split the range walk-forward: tune only on the first segment, evaluate on
   the untouched remainder. State the split explicitly.
3. Use `services/paper_trading/models.compute_transaction_costs` for costs and
   bid/ask (or LTP ± slippage) for fills.
4. Report: trades, win rate, avg win/loss, expectancy, profit factor, max
   drawdown, worst day, and a per-regime/DTE/IVR breakdown.
5. Run fragility checks: +1 tick slippage, 2× costs, exclude the best 2 days.
6. Save results under `data/backtest_results/options/` with a JSON sidecar
   containing commit sha, parameters, data range and seeds.
7. End with a verdict: evidence of edge, marginal, or no edge — and whether it
   clears the bar for paper trading.
