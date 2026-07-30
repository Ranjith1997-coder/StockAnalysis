"""
Simple backtest for RSI strategy with existing parameters.

This script tests the current RSI strategy implementation with default parameters:
- RSI_UPPER_THRESHOLD = 85 (positional) / 80 (intraday)
- RSI_LOWER_THRESHOLD = 30 (positional) / 20 (intraday)
- RSI_LOOKUP_PERIOD = 14

RSI Strategy Logic:
- RSI > Upper Threshold → BEARISH (overbought)
- RSI < Lower Threshold → BULLISH (oversold)

Usage:
    python backtest/rsi_backtest_simple.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.backtest.backtest import Backtester
from services.analysis_engine.analyser.TechnicalAnalyser import TechnicalAnalyser
import common.shared as shared


# Initialize the shared context for positional mode
shared.app_ctx.mode = shared.Mode.POSITIONAL  # type: ignore[assignment]


# Test stocks
TEST_STOCKS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "HCLTECH"
]

# Date ranges
START_DATE = "2024-01-01"
END_DATE = "2026-02-14"


def run_rsi_backtest():
    """
    Run backtest for RSI strategy with existing parameters.
    """
    print("\n" + "="*70)
    print("RSI STRATEGY BACKTEST")
    print("="*70)
    
    # Show current parameters
    print(f"\nCurrent Parameters:")
    print(f"  RSI_UPPER_THRESHOLD = {TechnicalAnalyser.RSI_UPPER_THRESHOLD}")
    print(f"  RSI_LOWER_THRESHOLD = {TechnicalAnalyser.RSI_LOWER_THRESHOLD}")
    print(f"  RSI_LOOKUP_PERIOD = {TechnicalAnalyser.RSI_LOOKUP_PERIOD}")
    print(f"  RSI_TREND_PERIODS = {TechnicalAnalyser.RSI_TREND_PERIODS}")
    
    print(f"\nStrategy Logic:")
    print(f"  RSI > {TechnicalAnalyser.RSI_UPPER_THRESHOLD} → BEARISH (overbought)")
    print(f"  RSI < {TechnicalAnalyser.RSI_LOWER_THRESHOLD} → BULLISH (oversold)")
    
    print(f"\nTest Stocks: {TEST_STOCKS}")
    print(f"Period: {START_DATE} to {END_DATE}")
    
    # Initialize analyser
    analyser = TechnicalAnalyser()
    
    def rsi_analyzer(stock):
        """Analyzer method for backtester."""
        return analyser.analyse_rsi(stock)
    
    print("\n" + "-"*70)
    print("Running backtest...")
    
    # Run backtest for each stock and aggregate results
    all_results = []
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_pnl = 0
    
    for symbol in TEST_STOCKS:
        try:
            backtester = Backtester(
                stock_symbols=symbol,
                analyzer_methods=rsi_analyzer,
                start_date=START_DATE,
                end_date=END_DATE,
                interval="day",
                initial_capital=100000,
                position_size=20000,
                stop_loss_pct=2.0,  # 1% stop loss
                target_pct=3.0,  # 5% target (5:1 reward/risk)
                allow_short=True,
            )
            
            result = backtester.run_backtest(symbol)
            all_results.append(result)
            
            # Aggregate stats
            trades = result.trades
            wins = sum(1 for t in trades if t.pnl > 0)
            losses = sum(1 for t in trades if t.pnl <= 0)
            pnl = sum(t.pnl for t in trades)
            
            total_trades += len(trades)
            total_wins += wins
            total_losses += losses
            total_pnl += pnl
            
            print(f"  {symbol}: {len(trades)} trades, {wins}W/{losses}L, PnL: ₹{pnl:.2f}")
            
        except Exception as e:
            print(f"  {symbol}: Error - {str(e)}")
    
    # Print summary
    print("\n" + "="*70)
    print("BACKTEST SUMMARY")
    print("="*70)
    
    win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
    avg_win = total_pnl / total_wins if total_wins > 0 else 0
    avg_loss = total_pnl / total_losses if total_losses > 0 else 0
    
    print(f"\nTotal Trades: {total_trades}")
    print(f"Winning Trades: {total_wins}")
    print(f"Losing Trades: {total_losses}")
    print(f"Win Rate: {win_rate:.1f}%")
    print(f"Total PnL: ₹{total_pnl:.2f}")
    print(f"Average Win: ₹{avg_win:.2f}")
    print(f"Average Loss: ₹{avg_loss:.2f}")
    
    if total_losses > 0 and avg_loss != 0:
        profit_factor = abs(total_wins * avg_win / (total_losses * avg_loss)) if avg_loss != 0 else 0
        print(f"Profit Factor: {profit_factor:.2f}")
    
    # Print detailed trades for first stock
    if all_results and len(all_results[0].trades) > 0:
        print("\n" + "-"*70)
        print(f"Sample Trades (First Stock: {TEST_STOCKS[0]}):")
        print("-"*70)
        for i, trade in enumerate(all_results[0].trades[:10]):  # Show first 10 trades
            print(f"  {i+1}. {trade.signal_type}: Entry ₹{trade.entry_price:.2f} → Exit ₹{trade.exit_price:.2f} "
                  f"(PnL: ₹{trade.pnl:.2f}, {trade.exit_reason})")
    
    return all_results


def main():
    """Main entry point."""
    print("\n" + "="*70)
    print("RSI STRATEGY BACKTEST")
    print("="*70)
    print("\nThis backtest tests the RSI overbought/oversold strategy:")
    print("  - RSI > Upper Threshold → BEARISH (expect reversal)")
    print("  - RSI < Lower Threshold → BULLISH (expect reversal)")
    
    results = run_rsi_backtest()
    
    print("\n" + "="*70)
    print("NEXT STEPS")
    print("="*70)
    print("\n1. If profit factor > 1.0, the RSI strategy is working")
    print("2. Run optimizer to find better parameters:")
    print("   python backtest/optimizer.py")
    print("3. Consider adding divergence detection for better signals")
    
    return results


if __name__ == "__main__":
    main()
