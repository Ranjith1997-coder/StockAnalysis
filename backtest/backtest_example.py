"""
Example scripts demonstrating how to use the Backtester infrastructure.

This file shows various ways to backtest your strategies:
1. Single stock, single strategy
2. Multiple stocks, single strategy
3. Single stock, multiple strategies
4. Advanced configuration with stop loss and targets
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from backtest.backtest import Backtester
from analyser.TechnicalAnalyser import TechnicalAnalyser
from analyser.VolumeAnalyser import VolumeAnalyser
from analyser.candleStickPatternAnalyser import CandleStickAnalyser
from analyser.Futures_Analyser import FuturesAnalyser
from analyser.IVAnalyser import IVAnalyser
import common.shared as shared


def init_app_context():
    """Initialize application context for backtesting"""
    if shared.app_ctx.mode is None:
        # Create a simple context object
        shared.app_ctx.mode = shared.Mode.POSITIONAL


def example_1_simple_rsi_backtest():
    """
    Example 1: Simple RSI strategy backtest on single stock
    """
    print("\n=== Example 1: RSI Strategy on RELIANCE ===\n")
    
    # Initialize context
    init_app_context()
    
    # Initialize the analyzer
    technical_analyzer = TechnicalAnalyser()
    technical_analyzer.reset_constants()
    
    # Create backtester
    backtester = Backtester(
        stock_symbols='RELIANCE',
        analyzer_methods=technical_analyzer.analyse_rsi,
        start_date='2024-01-01',
        end_date='2025-12-31',
        initial_capital=100000,
        position_size=20000,
        stop_loss_pct=3,  # 3% stop loss
        target_pct=5,     # 5% target
        allow_short=True
    )
    
    # Run backtest
    results = backtester.run_all()
    
    # Generate report
    backtester.generate_report()
    
    # Get trades as DataFrame
    trades_df = backtester.get_trades_dataframe('RELIANCE')
    print("\nTrades Summary:")
    print(trades_df[['entry_date', 'entry_price', 'exit_date', 'exit_price', 
                     'signal_type', 'pnl', 'pnl_percent', 'exit_reason']].head(10))
    
    return backtester


def example_2_multiple_stocks_ema_crossover():
    """
    Example 2: EMA Crossover strategy on multiple stocks
    """
    print("\n=== Example 2: EMA Crossover on Multiple Stocks ===\n")
    
    # Initialize context
    init_app_context()
    
    technical_analyzer = TechnicalAnalyser()
    technical_analyzer.reset_constants()
    
    # Test on multiple stocks
    stocks = ['TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'RELIANCE']
    
    backtester = Backtester(
        stock_symbols=stocks,
        analyzer_methods=technical_analyzer.analyse_ema_crossover,
        start_date='2024-01-01',
        end_date='2025-12-31',
        initial_capital=500000,
        position_size_method='percentage',
        position_size=20,  # 20% of capital per trade
        stop_loss_pct=2,
        target_pct=4,
        allow_short=False  # Only long positions
    )
    
    results = backtester.run_all()
    backtester.generate_report(output_file='backtest_ema_results.json')
    
    return backtester


def example_3_combined_strategies():
    """
    Example 3: Multiple strategies combined (RSI + EMA + Volume)
    """
    init_app_context()
    
    # Initialize 
    print("\n=== Example 3: Combined Strategy (RSI + EMA + Volume) ===\n")
    
    # Initialize analyzers
    technical_analyzer = TechnicalAnalyser()
    volume_analyzer = VolumeAnalyser()
    technical_analyzer.reset_constants()
    volume_analyzer.reset_constants()
    
    # Combine multiple analyzer methods
    analyzer_methods = [
        technical_analyzer.analyse_rsi,
        technical_analyzer.analyse_ema_crossover,
        volume_analyzer.analyse_volume_and_price
    ]
    
    backtester = Backtester(
        stock_symbols='NIFTY',
        analyzer_methods=analyzer_methods,
        start_date='2024-01-01',
        end_date='2025-12-31',
        initial_capital=200000,
        position_size=50000,
        stop_loss_pct=2.5,
        target_pct=6,
        allow_short=True,
        max_positions=1  # Only one position at a time
    )
    
    results = backtester.run_all()
    backtester.generate_report()
    
    return backtester


def example_4_macd_strategy_with_trailing_stop():
    """
    Example 4: MACD strategy with advanced parameters
    # Initialize context
    init_app_context()
    
    """
    print("\n=== Example 4: MACD Strategy with Advanced Settings ===\n")
    
    technical_analyzer = TechnicalAnalyser()
    technical_analyzer.reset_constants()
    
    backtester = Backtester(
        stock_symbols=['BANKNIFTY', 'NIFTY'],
        analyzer_methods=technical_analyzer.analyze_macd,
        start_date='2023-06-01',
        end_date='2025-12-31',
        initial_capital=300000,
        position_size=100000,
        stop_loss_pct=3,
        target_pct=8,
        trailing_stop=False,
        allow_short=True,
        max_positions=2  # Can hold 2 positions simultaneously
    )
    
    results = backtester.run_all()
    backtester.generate_report()
    
    # Access individual stock results
    for symbol, result in backtester.results.items():
        print(f"\n{symbol} Metrics:")
        for key, value in result.metrics.items():
            print(f"  {key}: {value}")
    
    return backtester

    
    
def    example_5_candlestick_patterns():
    """
    Example 5: Candlestick pattern strategy
    """
    init_app_context()
    print("\n=== Example 5: Candlestick Pattern Strategy ===\n")
    
    candle_analyzer = CandleStickAnalyser()
    candle_analyzer.reset_constants()
    
    # Test all candlestick patterns
    analyzer_methods = [
        candle_analyzer.singleCandleStickPattern,
        candle_analyzer.doubleCandleStickPattern,
        candle_analyzer.tripleCandleStickPattern
    ]
    
    backtester = Backtester(
        stock_symbols=['TATASTEEL', 'HINDALCO', 'COALINDIA'],
        analyzer_methods=analyzer_methods,
        start_date='2024-01-01',
        end_date='2025-12-31',
        initial_capital=150000,
        position_size=30000,
        stop_loss_pct=2,
        target_pct=4,
        allow_short=True
    )
    
    results = backtester.run_all()
    backtester.generate_report()
    
    # Initialize context
    init_app_context()
    
    return backtester


def example_6_short_term_trading():
    """
    Example 6: Short-term trading with tight stops
    """
    print("\n=== Example 6: Short-term Trading Strategy ===\n")
    
    technical_analyzer = TechnicalAnalyser()
    volume_analyzer = VolumeAnalyser()
    technical_analyzer.reset_constants()
    volume_analyzer.reset_constants()
    
    backtester = Backtester(
        stock_symbols='ITC',
        analyzer_methods=[
            technical_analyzer.analyse_ema_crossover,
            volume_analyzer.analyse_volume_and_price
        ],
        start_date='2024-06-01',
        end_date='2025-12-31',
        initial_capital=100000,
        position_size=25000,
        stop_loss_pct=1.5,  # Tight stop loss
        target_pct=3,       # Quick target
        allow_short=False,
        max_positions=1
    )
    
    results = backtester.run_all()
    backtester.generate_report()
    
    # Analyze trade statistics
    trades_df = backtester.get_trades_dataframe('ITC')
    if not trades_df.empty:
        print("\nTrade Statistics:")
        print(f"Average Holding Period: {trades_df['holding_period'].mean():.1f} days")
        print(f"Max Holding Period: {trades_df['holding_period'].max():.0f} days")
        print(f"Min Holding Period: {trades_df['holding_period'].min():.0f} days")
        print(f"\nExit Reason Distribution:")
        print(trades_df['exit_reason'].value_counts())
    
    # Initialize context
    init_app_context()
    
    return backtester


def example_7_compare_strategies():
    """
    Example 7: Compare different strategies on the same stock
    """
    print("\n=== Example 7: Strategy Comparison ===\n")
    
    technical_analyzer = TechnicalAnalyser()
    volume_analyzer = VolumeAnalyser()
    technical_analyzer.reset_constants()
    volume_analyzer.reset_constants()
    
    stock_symbol = 'SBIN'
    start = '2024-01-01'
    end = '2025-12-31'
    
    strategies = {
        'RSI': [technical_analyzer.analyse_rsi],
        'EMA_Crossover': [technical_analyzer.analyse_ema_crossover],
        'MACD': [technical_analyzer.analyze_macd],
        'Volume': [volume_analyzer.analyse_volume_and_price],
    }
    
    results_comparison = {}
    
    for strategy_name, methods in strategies.items():
        print(f"\nTesting {strategy_name}...")
        backtester = Backtester(
            stock_symbols=stock_symbol,
            analyzer_methods=methods,
            start_date=start,
            end_date=end,
            initial_capital=100000,
            position_size=25000,
            stop_loss_pct=2,
            target_pct=5,
            allow_short=True
        )
        backtester.run_all()
        results_comparison[strategy_name] = backtester.results[stock_symbol].metrics
    
    # Print comparison
    print("\n" + "="*80)
    print(f"STRATEGY COMPARISON - {stock_symbol}")
    print("="*80)
    print(f"{'Strategy':<20} {'Trades':<10} {'Win Rate':<12} {'Total Return %':<15} {'Sharpe':<10}")
    print("-"*80)
    
    for strategy_name, metrics in results_comparison.items():
        print(f"{strategy_name:<20} {metrics['total_trades']:<10} "
              f"{metrics['win_rate']:<12.2f} {metrics['total_return_percent']:<15.2f} "
              f"{metrics['sharpe_ratio']:<10.2f}")
    # Initialize context
    init_app_context()
    
    
    print("="*80)
    
    return results_comparison


def example_8_custom_date_range():
    """
    Example 8: Test specific market conditions (e.g., bull run, bear market)
    """
    print("\n=== Example 8: Custom Date Range Testing ===\n")
    
    technical_analyzer = TechnicalAnalyser()
    technical_analyzer.reset_constants()
    
    # Test during different market conditions
    test_periods = [
        ('2024-Q1', '2024-01-01', '2024-03-31'),
        ('2024-Q2', '2024-04-01', '2024-06-30'),
        ('2024-Q3', '2024-07-01', '2024-09-30'),
        ('2024-Q4', '2024-10-01', '2024-12-31'),
    ]
    
    quarterly_results = {}
    
    for period_name, start, end in test_periods:
        print(f"\nTesting {period_name}: {start} to {end}")
        backtester = Backtester(
            stock_symbols='NIFTY',
            analyzer_methods=technical_analyzer.analyse_ema_crossover,
            start_date=start,
            end_date=end,
            initial_capital=100000,
            position_size=50000,
            stop_loss_pct=2,
            target_pct=4,
            allow_short=True
        )
        backtester.run_all()
        quarterly_results[period_name] = backtester.results['NIFTY'].metrics
    
    # Compare quarterly performance
    print("\n" + "="*80)
    print("QUARTERLY PERFORMANCE COMPARISON")
    print("="*80)
    for period, metrics in quarterly_results.items():
        print(f"\n{period}:")
        print(f"  Total Return: {metrics['total_return_percent']:.2f}%")
        print(f"  Win Rate: {metrics['win_rate']:.2f}%")
        print(f"  Total Trades: {metrics['total_trades']}")
        print(f"  Profit Factor: {metrics['profit_factor']:.2f}")
    
    return quarterly_results


if __name__ == "__main__":
    """
    Run examples - uncomment the one you want to test
    """
    
    # Example 1: Simple single strategy
    example_1_simple_rsi_backtest()
    
    # Example 2: Multiple stocks
    # example_2_multiple_stocks_ema_crossover()
    
    # Example 3: Combined strategies
    # example_3_combined_strategies()
    
    # Example 4: MACD with advanced settings
    # example_4_macd_strategy_with_trailing_stop()
    
    # Example 5: Candlestick patterns
    # example_5_candlestick_patterns()
    
    # Example 6: Short-term trading
    # example_6_short_term_trading()
    
    # Example 7: Compare strategies
    # example_7_compare_strategies()
    
    # Example 8: Quarterly analysis
    # example_8_custom_date_range()
