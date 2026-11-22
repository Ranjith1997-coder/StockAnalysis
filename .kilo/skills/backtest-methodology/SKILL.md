---
name: backtest-methodology
description: "Rules for backtesting and validating any StockAnalysis signal or options strategy. USE FOR: backtest, walk-forward, strategy validation, tuning thresholds, judging whether a strategy works, interpreting P&L stats. DO NOT USE FOR: live execution or production deploys."---

# Backtest methodology

The project's history contains many tuning attempts that looked good and made
things worse (docs/pinn-volatility-engine.md §0.7–0.7.3). These rules exist to
stop that repeating.

## Non-negotiables

1. **Walk-forward only.** Fit/tune on period A, evaluate on unseen period B.
   Never report in-sample numbers as results.
2. **Out-of-sample only** in the final table. Keep an untouched holdout period.
3. **Same cost model as paper trading** — import
   `services/paper_trading/models.py:compute_transaction_costs`. Include
   brokerage, STT (sell side), stamp duty (buy side), exchange charges
   (NSE/BSE), GST, SEBI fee.
4. **Realistic fills.** Bid/ask when available; otherwise LTP ± slippage.
   Never assume mid or LTP-only.
5. **Expiry-day handling.** No holding through expiry; account for STT on ITM
   settlement; model DTE 0 gamma separately.
6. **No survivorship bias.** Use full Bhavcopy including expired series.
7. **Determinism.** Fix seeds. For PINN set `torch.set_num_threads(1)`
   (`enable_deterministic_threads`) — CPU reductions are otherwise not
   bit-reproducible across runs.
8. **Record provenance** with every result: commit sha, config hash, data date
   range, seed(s).

## What to report

| Metric | Why |
|---|---|
| trades | sample size; < 30 is not evidence |
| win rate | alone it is meaningless |
| avg win / avg loss | tail shape |
| expectancy per trade (₹ and % of capital) | the actual edge |
| profit factor | robustness |
| max drawdown, worst day | survivability |
| per-regime breakdown (TREND/RANGE, IVR bucket, DTE bucket) | where the edge lives |
| sensitivity to ±1 tick slippage and 2× costs | fragility |

## Red flags

- Win rate > 70 % with negative expectancy (selling tails).
- Edge disappears with one extra tick of slippage.
- Results driven by 1–2 days.
- Tuning only on the evaluation window.
- Comparing against a baseline measured on different data/threads (see 0.7.3's
  caveat: only internal comparisons are valid).

## Workflow

1. Define the hypothesis and the exact entry/exit rules before coding.
2. Implement the strategy in the paper-trading path (not a parallel copy), so
   backtest and paper share code.
3. Run walk-forward; tune only in-sample; freeze parameters.
4. Report OOS stats + fragility checks.
5. Paper-trade 4–6 weeks; compare against the backtest distribution.
6. Only then consider live, at minimum size.
