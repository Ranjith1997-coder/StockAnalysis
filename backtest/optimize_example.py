"""
Example scripts demonstrating how to use the ThresholdOptimizer.

Shows various optimisation scenarios:
1. Quick RSI optimisation (minimal — for testing the setup)
2. Robust RSI optimisation (production-grade — large & diverse stock pool)
3. EMA Crossover with train/test split
4. Supertrend optimisation
5. Custom search space for Stochastic
6. Candlestick pattern optimisation
7. Bulk optimisation of all methods
8. Full workflow: optimise → apply → backtest

Stock selection guidelines:
  - Use 10-20 stocks for robust results (avoids overfitting to a handful)
  - Cover multiple sectors: IT, Banking, Pharma, Metals, FMCG, Auto, Energy
  - Include both large-cap and mid-cap for variety
  - Training period should span 4-5+ years covering different market regimes
    (bull run, correction, consolidation, crash recovery)
  - Always hold out a test period (12-18 months) to detect overfitting
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.optimizer import ThresholdOptimizer, BulkOptimizer, SEARCH_SPACES
import common.shared as shared


# ---------------------------------------------------------------------------
# Common stock universes (reuse across examples)
# ---------------------------------------------------------------------------

# Small pool — fast iteration, good for testing
STOCKS_SMALL = ["RELIANCE", "TCS", "HDFCBANK"]

# Diversified pool — covers IT, Banking, Pharma, Metals, FMCG, Auto, Energy
STOCKS_DIVERSIFIED = [
    # IT
    "TCS", "INFY", "WIPRO",
    # Banking / Finance
    "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK",
    # Pharma
    "SUNPHARMA", "DRREDDY",
    # Metals & Mining
    "TATASTEEL", "HINDALCO",
    # FMCG
    "ITC", "HINDUNILVR",
    # Auto
    "MARUTI", "M&M",
    # Energy / Oil & Gas
    "RELIANCE", "ONGC",
    # Infrastructure
    "LT",
]

# Extended pool — even broader for the most rigorous optimisation
STOCKS_EXTENDED = STOCKS_DIVERSIFIED + [
    "BAJFINANCE", "BHARTIARTL", "HCLTECH", "TATAMOTORS",
    "COALINDIA", "NTPC", "POWERGRID", "ADANIENT",
]


def example_1_quick_rsi():
    """
    Example 1: Quick RSI optimisation — small pool for fast sanity check.
    Uses 3 stocks and 50 trials to verify the pipeline works (~2-3 min).
    For production-quality RSI results, use example_2.
    """
    print("\n=== Example 1: Quick RSI (pipeline check) ===\n")

    optimizer = ThresholdOptimizer(
        analyser_class_name="TechnicalAnalyser",
        method_name="analyse_rsi",
        stock_symbols=STOCKS_SMALL,
        train_start="2020-01-01",
        train_end="2024-12-31",
        test_start="2025-01-01",
        test_end="2026-02-14",
        metric="profit_factor",
        n_trials=50,
        stop_loss_pct=3.0,
        target_pct=5.0,
    )

    result = optimizer.optimize()
    optimizer.print_results()

    print("Copy-paste these into your analyser class:\n")
    print(optimizer.generate_constants_code())

    return optimizer


def example_2_robust_rsi():
    """
    Example 2: Production-grade RSI optimisation.

    Why this is better:
      - 18 stocks across 8 sectors → parameters must generalise
      - 5 years of training (2020-01 → 2024-12) covering:
          * COVID crash (Mar 2020)
          * Recovery rally (2020-2021)
          * Consolidation / correction (2022)
          * Bull run (2023-2024)
      - 14 months of held-out test data (2025-01 → 2026-02)
      - 200 trials for thorough exploration
      - profit_factor as metric (more robust than Sharpe alone since it
        penalises outsized losses even if average return is high)
    """
    print("\n=== Example 2: Robust RSI (production-grade) ===\n")

    optimizer = ThresholdOptimizer(
        analyser_class_name="TechnicalAnalyser",
        method_name="analyse_rsi",
        stock_symbols=STOCKS_DIVERSIFIED,
        train_start="2020-01-01",
        train_end="2024-12-31",
        test_start="2025-01-01",
        test_end="2026-02-14",
        metric="profit_factor",
        n_trials=200,
        stop_loss_pct=3.0,
        target_pct=5.0,
    )

    result = optimizer.optimize()
    optimizer.print_results()
    optimizer.export_results("rsi_robust_optimization.json")

    print("Copy-paste these into your analyser class:\n")
    print(optimizer.generate_constants_code())

    return optimizer


def example_3_ema_crossover():
    """
    Example 3: Production-grade EMA Crossover optimisation.

    EMA crossover is a trend-following strategy — 4 params (fast/slow period,
    diff threshold, min slope).  200 trials across 18 stocks, 5yr train.
    """
    print("\n=== Example 3: EMA Crossover (production-grade) ===\n")

    optimizer = ThresholdOptimizer(
        analyser_class_name="TechnicalAnalyser",
        method_name="analyse_ema_crossover",
        stock_symbols=STOCKS_DIVERSIFIED,
        train_start="2020-01-01",
        train_end="2024-12-31",
        test_start="2025-01-01",
        test_end="2026-02-14",
        metric="profit_factor",
        n_trials=200,
        stop_loss_pct=3.0,
        target_pct=5.0,
        allow_short=True,
    )

    result = optimizer.optimize()
    optimizer.print_results()
    optimizer.export_results("ema_crossover_optimization.json")

    print("Copy-paste these into your analyser class:\n")
    print(optimizer.generate_constants_code())

    return optimizer


def example_4_supertrend():
    """
    Example 4: Production-grade Supertrend optimisation.

    Only 2 parameters (period, multiplier) so the search space is small,
    but 200 trials ensures thorough exploration of all combinations.
    """
    print("\n=== Example 4: Supertrend (production-grade) ===\n")

    optimizer = ThresholdOptimizer(
        analyser_class_name="TechnicalAnalyser",
        method_name="analyse_supertrend",
        stock_symbols=STOCKS_DIVERSIFIED,
        train_start="2020-01-01",
        train_end="2024-12-31",
        test_start="2025-01-01",
        test_end="2026-02-14",
        metric="profit_factor",
        n_trials=200,
        stop_loss_pct=3.0,
        target_pct=5.0,
    )

    result = optimizer.optimize()
    optimizer.print_results()
    optimizer.export_results("supertrend_optimization.json")

    print("Copy-paste these into your analyser class:\n")
    print(optimizer.generate_constants_code())

    return optimizer


def example_5_stochastic():
    """
    Example 5: Production-grade Stochastic optimisation.

    Uses an expanded search space (wider K period range) for more
    thorough exploration.  4 parameters, 200 trials.
    """
    print("\n=== Example 5: Stochastic (production-grade) ===\n")

    custom_space = {
        "STOCHASTIC_K_PERIOD": {"type": "categorical", "choices": [5, 7, 9, 14, 21, 28]},
        "STOCHASTIC_D_PERIOD": {"type": "categorical", "choices": [3, 5, 7]},
        "STOCHASTIC_UPPER":    {"type": "int", "low": 70, "high": 90, "step": 5},
        "STOCHASTIC_LOWER":    {"type": "int", "low": 10, "high": 30, "step": 5},
    }

    optimizer = ThresholdOptimizer(
        analyser_class_name="TechnicalAnalyser",
        method_name="analyse_stochastic",
        stock_symbols=STOCKS_DIVERSIFIED,
        train_start="2020-01-01",
        train_end="2024-12-31",
        test_start="2025-01-01",
        test_end="2026-02-14",
        metric="profit_factor",
        n_trials=200,
        stop_loss_pct=3.0,
        target_pct=5.0,
        custom_search_space=custom_space,
    )

    result = optimizer.optimize()
    optimizer.print_results()
    optimizer.export_results("stochastic_optimization.json")

    print("Copy-paste these into your analyser class:\n")
    print(optimizer.generate_constants_code())

    return optimizer


def example_6_candlestick():
    """
    Example 6: Production-grade Candlestick REVERSAL pattern optimisation.

    Optimises double candlestick REVERSAL pattern thresholds only:
    - ENGULFING_MIN_BODY_RATIO: minimum body ratio for engulfing patterns
    - PIERCING_MIN_PENETRATION: minimum penetration for piercing line
    - DARK_CLOUD_MIN_PENETRATION: minimum penetration for dark cloud cover
    
    Note: Continuation patterns (2 Continuous Increase/Decrease) are now in a
    separate method 'doubleCandleStickContinuationPattern' for independent testing.
    
    IMPROVED SETTINGS:
    - Extended search space boundaries (see optimizer.py)
    - 500 trials for better coverage
    - Proper out-of-sample test period (2024, a completed historical year)
    """
    print("\n=== Example 6: Candlestick REVERSAL Patterns (production-grade) ===\n")

    optimizer = ThresholdOptimizer(
        analyser_class_name="CandleStickAnalyser",
        method_name="doubleCandleStickPattern",
        stock_symbols=STOCKS_DIVERSIFIED,
        train_start="2019-01-01",
        train_end="2023-12-31",
        test_start="2024-01-01",
        test_end="2024-12-31",
        metric="profit_factor",
        n_trials=500,
        stop_loss_pct=3.0,
        target_pct=5.0,
    )

    result = optimizer.optimize()
    optimizer.print_results()
    optimizer.export_results("candlestick_reversal_optimization.json")

    print("Copy-paste these into your analyser class:\n")
    print(optimizer.generate_constants_code())

    return optimizer


def example_6b_candlestick_continuation():
    """
    Example 6b: Candlestick CONTINUATION pattern optimisation.
    
    Optimises double candlestick CONTINUATION pattern thresholds:
    - TWO_CONT_INC_OR_DEC_THRESHOLD: threshold for 2 continuous increase/decrease
    
    WARNING: These patterns tend to have NEGATIVE EXPECTANCY because they
    buy after price has already moved up (chasing) and sell after price
    has already moved down (panic selling). Use with caution.
    """
    print("\n=== Example 6b: Candlestick CONTINUATION Patterns ===\n")
    print("WARNING: These patterns historically have negative expectancy!\n")

    optimizer = ThresholdOptimizer(
        analyser_class_name="CandleStickAnalyser",
        method_name="doubleCandleStickContinuationPattern",
        stock_symbols=STOCKS_DIVERSIFIED,
        train_start="2019-01-01",
        train_end="2023-12-31",
        test_start="2024-01-01",
        test_end="2024-12-31",
        metric="profit_factor",
        n_trials=200,
        stop_loss_pct=3.0,
        target_pct=5.0,
    )

    result = optimizer.optimize()
    optimizer.print_results()
    optimizer.export_results("candlestick_continuation_optimization.json")

    print("Copy-paste these into your analyser class:\n")
    print(optimizer.generate_constants_code())

    return optimizer


def example_6c_single_candle_momentum():
    """
    Example 6c: Single candle MOMENTUM pattern optimisation.
    
    Optimises single candlestick MOMENTUM pattern thresholds:
    - MARUBASU_THRESHOLD: minimum body size for Marubozu patterns
    - WICK_PERCENTAGE: maximum wick size as percentage
    
    These patterns (Bullish/Bearish Marubozu) show strong buying/selling pressure
    and work well in most market contexts.
    """
    print("\n=== Example 6c: Single Candle MOMENTUM Patterns ===\n")

    optimizer = ThresholdOptimizer(
        analyser_class_name="CandleStickAnalyser",
        method_name="singleCandleStickPattern",
        stock_symbols=STOCKS_DIVERSIFIED,
        train_start="2019-01-01",
        train_end="2023-12-31",
        test_start="2024-01-01",
        test_end="2024-12-31",
        metric="profit_factor",
        n_trials=200,
        stop_loss_pct=3.0,
        target_pct=5.0,
    )

    result = optimizer.optimize()
    optimizer.print_results()
    optimizer.export_results("single_candle_momentum_optimization.json")

    print("Copy-paste these into your analyser class:\n")
    print(optimizer.generate_constants_code())

    return optimizer


def example_6d_single_candle_reversal():
    """
    Example 6d: Single candle REVERSAL pattern optimisation.
    
    Optimises single candlestick REVERSAL pattern thresholds:
    - HAMMER_BODY_RATIO: maximum body size ratio for Hammer/Shooting Star
    - HAMMER_WICK_MULTIPLIER: minimum wick-to-body ratio
    
    WARNING: These patterns (Hammer, Shooting Star) are context-dependent.
    They work best after downtrends (Hammer) or uptrends (Shooting Star).
    Without trend context, they may produce false signals.
    """
    print("\n=== Example 6d: Single Candle REVERSAL Patterns ===\n")
    print("Note: These patterns work best with trend context.\n")

    optimizer = ThresholdOptimizer(
        analyser_class_name="CandleStickAnalyser",
        method_name="singleCandleReversalPattern",
        stock_symbols=STOCKS_DIVERSIFIED,
        train_start="2019-01-01",
        train_end="2023-12-31",
        test_start="2024-01-01",
        test_end="2024-12-31",
        metric="profit_factor",
        n_trials=200,
        stop_loss_pct=3.0,
        target_pct=5.0,
    )

    result = optimizer.optimize()
    optimizer.print_results()
    optimizer.export_results("single_candle_reversal_optimization.json")

    print("Copy-paste these into your analyser class:\n")
    print(optimizer.generate_constants_code())

    return optimizer


def example_7_bulk_optimize():
    """
    Example 7: Production-grade bulk optimisation of ALL analyser methods.

    Matches the robust RSI configuration (Example 2) applied uniformly:
      - 18 stocks across 8 sectors (IT, Banking, Pharma, Metals, FMCG, Auto, Energy, Infra)
      - 5 years of training data (2020-01 → 2024-12) covering:
          * COVID crash & recovery (2020)
          * Post-COVID rally (2021)
          * Rate-hike correction (2022)
          * Bull run (2023-2024)
      - 14 months of held-out test data (2025-01 → 2026-02-14)
      - 200 trials per method for thorough exploration
      - profit_factor as metric (penalises large losses, more robust than Sharpe)
      - 3% stop loss, 5% target — same risk parameters as robust RSI

    Estimated runtime: ~5-8 hours (13 methods × 200 trials × 18 stocks).
    Recommended: run overnight.

    Output:
      - Per-method results printed to console
      - Summary table comparing train vs test across all methods
      - bulk_optimization_results.json with all optimal parameters
      - Copy-pasteable Python code for every optimised threshold
    """
    print("\n=== Example 7: Production-Grade Bulk Optimisation ===\n")
    print(f"Stocks       : {len(STOCKS_DIVERSIFIED)} across 8 sectors")
    print(f"Training     : 2020-01-01 → 2024-12-31 (5 years)")
    print(f"Test         : 2025-01-01 → 2026-02-14 (14 months)")
    print(f"Trials/method: 200")
    print(f"Metric       : profit_factor")
    print(f"Risk         : 3% SL / 5% target")
    print()

    bulk = BulkOptimizer(
        stock_symbols=STOCKS_DIVERSIFIED,
        train_start="2020-01-01",
        train_end="2024-12-31",
        test_start="2025-01-01",
        test_end="2026-02-14",
        metric="profit_factor",
        n_trials=200,
        stop_loss_pct=3.0,
        target_pct=5.0,
        allow_short=True,
        mode="positional",
        output_file="bulk_optimization_results.json",
    )

    results = bulk.optimize_all()
    bulk.print_summary()

    print("\n" + "=" * 80)
    print("COPY-PASTE THE FOLLOWING INTO YOUR ANALYSER CLASSES:")
    print("=" * 80)
    print(bulk.generate_all_constants_code())

    return bulk


def example_8_apply_and_backtest():
    """
    Example 8: Full workflow — optimise → apply → backtest.
    Demonstrates how to take the best params and verify them
    on held-out data with a standard Backtester run.
    """
    print("\n=== Example 8: Optimise → Apply → Backtest ===\n")

    from backtest.backtest import Backtester
    from analyser.TechnicalAnalyser import TechnicalAnalyser

    # Step 1: Optimise on a large, diverse stock pool
    optimizer = ThresholdOptimizer(
        analyser_class_name="TechnicalAnalyser",
        method_name="analyse_rsi",
        stock_symbols=STOCKS_DIVERSIFIED,
        train_start="2020-01-01",
        train_end="2024-12-31",
        test_start="2025-01-01",
        test_end="2026-02-14",
        metric="profit_factor",
        n_trials=200,
        stop_loss_pct=3.0,
        target_pct=5.0,
    )
    result = optimizer.optimize()
    optimizer.print_results()

    # Step 2: Apply best params
    optimizer.apply_best_params()

    # Step 3: Backtest with optimised params on the held-out test period
    ta = TechnicalAnalyser()
    ta.reset_constants()

    bt = Backtester(
        stock_symbols=STOCKS_DIVERSIFIED,
        analyzer_methods=ta.analyse_rsi,
        start_date="2025-01-01",
        end_date="2026-02-14",
        initial_capital=100000,
        position_size=20000,
        stop_loss_pct=3.0,
        target_pct=5.0,
    )
    bt.run_all()
    bt.generate_report()

    return optimizer, bt


def example_9_all_candlestick_patterns():
    """
    Example 9: Run optimization for ALL candlestick pattern methods.
    
    This runs optimization for all 6 candlestick pattern methods:
    1. singleCandleStickPattern - Marubozu (momentum)
    2. singleCandleReversalPattern - Hammer, Shooting Star (reversal with trend context)
    3. doubleCandleStickPattern - Engulfing, Piercing, Dark Cloud (reversal with trend context)
    4. doubleCandleStickContinuationPattern - 2 Cont. Inc/Dec (continuation)
    5. tripleCandleStickReversalPattern - Morning/Evening Star (reversal with trend context)
    6. tripleCandleStickContinuationPattern - 3 Cont. Inc/Dec (continuation)
    
    NEW FEATURE: Trend Context for Reversal Patterns
    - Bullish reversal patterns (Hammer, Bullish Engulfing, Piercing, Morning Star)
      now only trigger after a confirmed DOWNTREND
    - Bearish reversal patterns (Shooting Star, Bearish Engulfing, Dark Cloud, Evening Star)
      now only trigger after a confirmed UPTREND
    
    This significantly reduces false signals by ensuring reversal patterns
    occur at appropriate trend turning points.
    
    Configuration:
      - 18 stocks across 8 sectors
      - 5 years training (2019-2023)
      - 1 year test (2024)
      - 300 trials per method
      - profit_factor as metric
      - 3% stop loss, 5% target
    
    Estimated runtime: ~3-4 hours (6 methods × 300 trials × 18 stocks).
    """
    print("\n" + "=" * 80)
    print("CANDLESTICK PATTERN OPTIMIZATION - ALL METHODS")
    print("=" * 80)
    print(f"\nStocks       : {len(STOCKS_DIVERSIFIED)} across 8 sectors")
    print(f"Training     : 2019-01-01 → 2023-12-31 (5 years)")
    print(f"Test         : 2024-01-01 → 2024-12-31 (1 year)")
    print(f"Trials/method: 300")
    print(f"Metric       : profit_factor")
    print(f"Risk         : 3% SL / 5% target")
    print("\nNEW: Trend context filtering for reversal patterns!")
    print("=" * 80)
    
    results = {}
    
    # Common parameters for all candlestick optimizations
    common_params = {
        "analyser_class_name": "CandleStickAnalyser",
        "stock_symbols": STOCKS_DIVERSIFIED,
        "train_start": "2019-01-01",
        "train_end": "2023-12-31",
        "test_start": "2024-01-01",
        "test_end": "2024-12-31",
        "metric": "profit_factor",
        "n_trials": 300,
        "stop_loss_pct": 3.0,
        "target_pct": 5.0,
    }
    
    # -------------------------------------------------------------------------
    # 1. Single Candle MOMENTUM Patterns (Marubozu)
    # -------------------------------------------------------------------------
    print("\n\n>>> [1/6] Optimizing Single Candle MOMENTUM Patterns (Marubozu)...\n")
    
    optimizer1 = ThresholdOptimizer(
        method_name="singleCandleStickPattern",
        **common_params
    )
    optimizer1.optimize()
    optimizer1.print_results()
    optimizer1.export_results("opt_single_candle_momentum.json")
    results["singleCandleStickPattern"] = optimizer1
    print("\n" + "-" * 80)
    
    # -------------------------------------------------------------------------
    # 2. Single Candle REVERSAL Patterns (Hammer, Shooting Star)
    #    NOW WITH TREND CONTEXT!
    # -------------------------------------------------------------------------
    print("\n\n>>> [2/6] Optimizing Single Candle REVERSAL Patterns (Hammer, Shooting Star)...")
    print("    NOTE: Now includes trend context filtering!\n")
    
    optimizer2 = ThresholdOptimizer(
        method_name="singleCandleReversalPattern",
        **common_params
    )
    optimizer2.optimize()
    optimizer2.print_results()
    optimizer2.export_results("opt_single_candle_reversal.json")
    results["singleCandleReversalPattern"] = optimizer2
    print("\n" + "-" * 80)
    
    # -------------------------------------------------------------------------
    # 3. Double Candle REVERSAL Patterns (Engulfing, Piercing, Dark Cloud)
    #    NOW WITH TREND CONTEXT!
    # -------------------------------------------------------------------------
    print("\n\n>>> [3/6] Optimizing Double Candle REVERSAL Patterns...")
    print("    NOTE: Now includes trend context filtering!\n")
    
    optimizer3 = ThresholdOptimizer(
        method_name="doubleCandleStickPattern",
        **common_params
    )
    optimizer3.optimize()
    optimizer3.print_results()
    optimizer3.export_results("opt_double_candle_reversal.json")
    results["doubleCandleStickPattern"] = optimizer3
    print("\n" + "-" * 80)
    
    # -------------------------------------------------------------------------
    # 4. Double Candle CONTINUATION Patterns (2 Cont. Inc/Dec)
    #    WARNING: Historically negative expectancy
    # -------------------------------------------------------------------------
    print("\n\n>>> [4/6] Optimizing Double Candle CONTINUATION Patterns...")
    print("    WARNING: These patterns historically have negative expectancy!\n")
    
    optimizer4 = ThresholdOptimizer(
        method_name="doubleCandleStickContinuationPattern",
        **common_params
    )
    optimizer4.optimize()
    optimizer4.print_results()
    optimizer4.export_results("opt_double_candle_continuation.json")
    results["doubleCandleStickContinuationPattern"] = optimizer4
    print("\n" + "-" * 80)
    
    # -------------------------------------------------------------------------
    # 5. Triple Candle REVERSAL Patterns (Morning/Evening Star)
    #    NOW WITH TREND CONTEXT!
    # -------------------------------------------------------------------------
    print("\n\n>>> [5/6] Optimizing Triple Candle REVERSAL Patterns...")
    print("    NOTE: Morning/Evening Star now includes trend context filtering!\n")
    
    optimizer5 = ThresholdOptimizer(
        method_name="tripleCandleStickReversalPattern",
        **common_params
    )
    optimizer5.optimize()
    optimizer5.print_results()
    optimizer5.export_results("opt_triple_candle_reversal.json")
    results["tripleCandleStickReversalPattern"] = optimizer5
    print("\n" + "-" * 80)
    
    # -------------------------------------------------------------------------
    # 6. Triple Candle CONTINUATION Patterns (3 Cont. Inc/Dec)
    #    WARNING: Historically negative expectancy
    # -------------------------------------------------------------------------
    print("\n\n>>> [6/6] Optimizing Triple Candle CONTINUATION Patterns...")
    print("    WARNING: These patterns historically have negative expectancy!\n")
    
    optimizer6 = ThresholdOptimizer(
        method_name="tripleCandleStickContinuationPattern",
        **common_params
    )
    optimizer6.optimize()
    optimizer6.print_results()
    optimizer6.export_results("opt_triple_candle_continuation.json")
    results["tripleCandleStickContinuationPattern"] = optimizer6
    print("\n" + "-" * 80)
    
    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print("\n\n" + "=" * 80)
    print("OPTIMIZATION COMPLETE - SUMMARY")
    print("=" * 80)
    
    print("\n{:<45} {:>12} {:>12} {:>12}".format(
        "Method", "Train PF", "Test PF", "Trades"))
    print("-" * 85)
    
    for method_name, optimizer in results.items():
        train_metric = optimizer.best_train_metric
        test_metric = optimizer.test_metrics.get("profit_factor", 0)
        test_trades = optimizer.test_metrics.get("total_trades", 0)
        
        print("{:<45} {:>12.3f} {:>12.3f} {:>12}".format(
            method_name, train_metric, test_metric, test_trades))
    
    print("\n" + "=" * 80)
    print("COPY-PASTE THE FOLLOWING INTO CandleStickAnalyser:")
    print("=" * 80)
    
    for method_name, optimizer in results.items():
        print(f"\n# {method_name}")
        print(optimizer.generate_constants_code())
    
    return results


def example_10_triple_candlestick_only():
    """
    Example 10: Run optimization ONLY for triple candlestick patterns.
    
    This runs optimization for the 2 triple candlestick pattern methods:
    1. tripleCandleStickReversalPattern - Morning/Evening Star (reversal with trend context)
    2. tripleCandleStickContinuationPattern - 3 Cont. Inc/Dec (continuation)
    
    The triple candlestick patterns have been split into separate reversal and
    continuation methods for independent optimization and testing.
    
    Configuration:
      - 18 stocks across 8 sectors
      - 5 years training (2019-2023)
      - 1 year test (2024)
      - 300 trials per method
      - profit_factor as metric
      - 3% stop loss, 5% target
    
    Estimated runtime: ~1 hour (2 methods × 300 trials × 18 stocks).
    """
    print("\n" + "=" * 80)
    print("TRIPLE CANDLESTICK PATTERN OPTIMIZATION")
    print("=" * 80)
    print(f"\nStocks       : {len(STOCKS_DIVERSIFIED)} across 8 sectors")
    print(f"Training     : 2019-01-01 → 2023-12-31 (5 years)")
    print(f"Test         : 2024-01-01 → 2024-12-31 (1 year)")
    print(f"Trials/method: 300")
    print(f"Metric       : profit_factor")
    print(f"Risk         : 3% SL / 5% target")
    print("\nPatterns are split into REVERSAL and CONTINUATION methods.")
    print("=" * 80)
    
    results = {}
    
    # Common parameters for all candlestick optimizations
    common_params = {
        "analyser_class_name": "CandleStickAnalyser",
        "stock_symbols": STOCKS_DIVERSIFIED,
        "train_start": "2019-01-01",
        "train_end": "2023-12-31",
        "test_start": "2024-01-01",
        "test_end": "2024-12-31",
        "metric": "profit_factor",
        "n_trials": 300,
        "stop_loss_pct": 3.0,
        "target_pct": 5.0,
    }
    
    # -------------------------------------------------------------------------
    # 1. Triple Candle REVERSAL Patterns (Morning/Evening Star)
    #    WITH TREND CONTEXT!
    # -------------------------------------------------------------------------
    print("\n\n>>> [1/2] Optimizing Triple Candle REVERSAL Patterns...")
    print("    NOTE: Morning/Evening Star includes trend context filtering!\n")
    
    optimizer1 = ThresholdOptimizer(
        method_name="tripleCandleStickReversalPattern",
        **common_params
    )
    optimizer1.optimize()
    optimizer1.print_results()
    optimizer1.export_results("opt_triple_candle_reversal.json")
    results["tripleCandleStickReversalPattern"] = optimizer1
    print("\n" + "-" * 80)
    
    # -------------------------------------------------------------------------
    # 2. Triple Candle CONTINUATION Patterns (3 Cont. Inc/Dec)
    #    WARNING: Historically negative expectancy
    # -------------------------------------------------------------------------
    print("\n\n>>> [2/2] Optimizing Triple Candle CONTINUATION Patterns...")
    print("    WARNING: These patterns historically have negative expectancy!\n")
    
    optimizer2 = ThresholdOptimizer(
        method_name="tripleCandleStickContinuationPattern",
        **common_params
    )
    optimizer2.optimize()
    optimizer2.print_results()
    optimizer2.export_results("opt_triple_candle_continuation.json")
    results["tripleCandleStickContinuationPattern"] = optimizer2
    print("\n" + "-" * 80)
    
    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print("\n\n" + "=" * 80)
    print("OPTIMIZATION COMPLETE - SUMMARY")
    print("=" * 80)
    
    print("\n{:<45} {:>12} {:>12} {:>12}".format(
        "Method", "Train PF", "Test PF", "Trades"))
    print("-" * 85)
    
    for method_name, optimizer in results.items():
        train_metric = optimizer.best_train_metric
        test_metric = optimizer.test_metrics.get("profit_factor", 0)
        test_trades = optimizer.test_metrics.get("total_trades", 0)
        
        print("{:<45} {:>12.3f} {:>12.3f} {:>12}".format(
            method_name, train_metric, test_metric, test_trades))
    
    print("\n" + "=" * 80)
    print("COPY-PASTE THE FOLLOWING INTO CandleStickAnalyser:")
    print("=" * 80)
    
    for method_name, optimizer in results.items():
        print(f"\n# {method_name}")
        print(optimizer.generate_constants_code())
    
    return results


if __name__ == "__main__":
    """
    Run examples — uncomment the one you want.

    All individual examples use production-grade parameters:
      - 18 stocks across 8 sectors
      - 5yr train (2020-2024), 14mo test (2025-2026)
      - 200 trials, profit_factor metric, 3% SL / 5% target

    Recommended order:
      1. example_1  — quick sanity check (~2-3 min, 3 stocks, 50 trials)
      2. example_2  — robust RSI (~30 min)
      3. example_3 to 6 — individual indicators (~20-30 min each)
      7. example_7  — ★ bulk optimise ALL methods (5-8 hours; run overnight)
      8. example_8  — full workflow: optimise → apply → backtest
      9. example_9  — ★ ALL candlestick patterns with trend context (~3-4 hours)
     10. example_10 — ★ TRIPLE candlestick patterns only (~1 hour)
    """

    # Quick pipeline check (3 stocks, 50 trials)
    # example_1_quick_rsi()

    # Production RSI (18 stocks, 200 trials)
    # example_2_robust_rsi()

    # Production EMA Crossover (18 stocks, 200 trials)
    # example_3_ema_crossover()

    # Production Supertrend (18 stocks, 200 trials)
    # example_4_supertrend()

    # Production Stochastic (18 stocks, 200 trials)
    # example_5_stochastic()

    # Production Candlestick REVERSAL Patterns (18 stocks, 500 trials)
    # example_6_candlestick()

    # Candlestick CONTINUATION Patterns (warning: typically negative expectancy)
    # example_6b_candlestick_continuation()

    # Single Candle MOMENTUM Patterns (Marubozu)
    # example_6c_single_candle_momentum()

    # Single Candle REVERSAL Patterns (Hammer, Shooting Star)
    # example_6d_single_candle_reversal()

    # ★ PRODUCTION: Bulk optimise ALL methods (200 trials, 18 stocks, 5yr train)
    # example_7_bulk_optimize()

    # Full workflow: optimise → apply → backtest
    # example_8_apply_and_backtest()
    
    # ★ ALL CANDLESTICK PATTERNS with trend context filtering (~3-4 hours)
    # example_9_all_candlestick_patterns()
    
    # ★ TRIPLE CANDLESTICK PATTERNS only (~1 hour)
    example_10_triple_candlestick_only()
