"""
Backtest script to compare Bollinger Band strategies:
- Mean Reversion (old): Price > Upper = BEARISH, Price < Lower = BULLISH
- Momentum (new): Price > Upper = BULLISH, Price < Lower = BEARISH

This script runs the optimizer to find optimal parameters for the momentum approach
and compares performance against the mean reversion approach.

Usage:
    python backtest/bb_momentum_backtest.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.logging_util import get_logger
logger = get_logger("backtest")

from tools.backtest.optimizer import ThresholdOptimizer
from services.analysis_engine.analyser.TechnicalAnalyser import TechnicalAnalyser
import common.shared as shared


# Stock universe for testing
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

# Date ranges
TRAIN_START = "2020-01-01"
TRAIN_END = "2024-06-30"
TEST_START = "2024-07-01"
TEST_END = "2026-02-14"


def optimize_bb_momentum():
    """
    Optimize Bollinger Band parameters for the momentum approach.
    
    The momentum approach:
    - Price > Upper Band = BULLISH (strong momentum continuation)
    - Price < Lower Band = BEARISH (strong downward momentum)
    """
    print("\n" + "="*70)
    print("BOLLINGER BAND MOMENTUM STRATEGY OPTIMIZATION")
    print("="*70)
    print("\nStrategy Logic:")
    print("  - Price > Upper Band → BULLISH (momentum breakout)")
    print("  - Price < Lower Band → BEARISH (momentum breakdown)")
    print("\n" + "-"*70 + "\n")
    
    optimizer = ThresholdOptimizer(
        analyser_class_name="TechnicalAnalyser",
        method_name="analyse_Bolinger_band",
        stock_symbols=STOCKS_DIVERSIFIED,
        train_start=TRAIN_START,
        train_end=TRAIN_END,
        test_start=TEST_START,
        test_end=TEST_END,
        metric="profit_factor",  # Optimize for profit factor
        n_trials=100,
        stop_loss_pct=3.0,
        target_pct=5.0,
        initial_capital=100000,
        position_size=20000,
        allow_short=True,
        mode="positional",
    )
    
    result = optimizer.optimize()
    optimizer.print_results()
    
    print("\n" + "="*70)
    print("OPTIMIZED PARAMETERS FOR MOMENTUM APPROACH")
    print("="*70)
    print(optimizer.generate_constants_code())
    
    return optimizer


def run_quick_optimization():
    """
    Quick optimization with fewer stocks and trials for faster results.
    """
    print("\n" + "="*70)
    print("QUICK BOLLINGER BAND OPTIMIZATION (Fast)")
    print("="*70)
    
    # Use smaller stock pool for faster testing
    quick_stocks = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK"]
    
    optimizer = ThresholdOptimizer(
        analyser_class_name="TechnicalAnalyser",
        method_name="analyse_Bolinger_band",
        stock_symbols=quick_stocks,
        train_start=TRAIN_START,
        train_end=TRAIN_END,
        test_start=TEST_START,
        test_end=TEST_END,
        metric="profit_factor",
        n_trials=50,  # Fewer trials for faster results
        stop_loss_pct=3.0,
        target_pct=5.0,
        mode="positional",
    )
    
    result = optimizer.optimize()
    optimizer.print_results()
    
    print("\n" + "="*70)
    print("OPTIMIZED PARAMETERS")
    print("="*70)
    print(optimizer.generate_constants_code())
    
    return optimizer


def main():
    """Main entry point for BB momentum backtest."""
    print("\n" + "="*70)
    print("BOLLINGER BAND STRATEGY ANALYSIS")
    print("="*70)
    print("\nThis script analyzes the momentum-based Bollinger Band strategy:")
    print("  - Price breaks ABOVE upper band → BULLISH signal")
    print("  - Price breaks BELOW lower band → BEARISH signal")
    print("\nThis aligns with trend-following indicators and reduces signal conflicts.")
    
    print("\n" + "="*70)
    print("STRATEGY CHANGE SUMMARY")
    print("="*70)
    print("\nOLD (Mean Reversion):")
    print("  - Price > Upper Band → BEARISH (expect reversion)")
    print("  - Price < Lower Band → BULLISH (expect reversion)")
    print("\nNEW (Momentum):")
    print("  - Price > Upper Band → BULLISH (momentum continuation)")
    print("  - Price < Lower Band → BEARISH (momentum continuation)")
    
    print("\n" + "="*70)
    print("RUNNING OPTIMIZATION")
    print("="*70)
    
    # Run quick optimization first
    optimizer = run_quick_optimization()
    
    print("\n" + "="*70)
    print("NEXT STEPS")
    print("="*70)
    print("\n1. Review the optimized parameters above")
    print("2. Update TechnicalAnalyser.py with the new parameters:")
    print("   - BB_WINDOW = <optimized_value>")
    print("   - BB_NUM_STD = <optimized_value>")
    print("3. Run full optimization with more stocks for production:")
    print("   optimizer = optimize_bb_momentum()")
    print("\n4. The momentum strategy is now active in TechnicalAnalyser.py")
    print("   Run your normal analysis to see the improved signals")
    
    return optimizer


if __name__ == "__main__":
    main()
