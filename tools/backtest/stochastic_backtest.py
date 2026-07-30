"""
Backtest for Stochastic Oscillator strategy.

Stochastic Strategy Logic:
- BULLISH: %K crosses above %D while in oversold zone (<=20)
- BEARISH: %K crosses below %D while in overbought zone (>=80)

Usage:
    python backtest/stochastic_backtest.py
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


def run_stochastic_backtest():
    """
    Run backtest for Stochastic Oscillator strategy.
    """
    print("\n" + "="*70)
    print("STOCHASTIC OSCILLATOR STRATEGY BACKTEST")
    print("="*70)
    
    # Show current parameters
    print(f"\nCurrent Parameters:")
    print(f"  STOCHASTIC_K_PERIOD = {TechnicalAnalyser.STOCHASTIC_K_PERIOD}")
    print(f"  STOCHASTIC_D_PERIOD = {TechnicalAnalyser.STOCHASTIC_D_PERIOD}")
    print(f"  STOCHASTIC_UPPER = {TechnicalAnalyser.STOCHASTIC_UPPER}")
    print(f"  STOCHASTIC_LOWER = {TechnicalAnalyser.STOCHASTIC_LOWER}")
    
    print(f"\nStrategy Logic:")
    print(f"  BULLISH: %K crosses above %D while %K <= {TechnicalAnalyser.STOCHASTIC_LOWER}")
    print(f"  BEARISH: %K crosses below %D while %K >= {TechnicalAnalyser.STOCHASTIC_UPPER}")
    
    print(f"\nTest Stocks: {TEST_STOCKS}")
    print(f"Period: {START_DATE} to {END_DATE}")
    print(f"Risk/Reward: 3:1 (3% target, 1% stop loss)")
    
    # Initialize analyser
    analyser = TechnicalAnalyser()
    
    def stochastic_analyzer(stock):
        """Analyzer method for backtester."""
        return analyser.analyse_stochastic(stock)
    
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
                analyzer_methods=stochastic_analyzer,
                start_date=START_DATE,
                end_date=END_DATE,
                interval="day",
                initial_capital=100000,
                position_size=20000,
                stop_loss_pct=1.0,
                target_pct=3.0,
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
    
    print(f"\nTotal Trades: {total_trades}")
    print(f"Winning Trades: {total_wins}")
    print(f"Losing Trades: {total_losses}")
    print(f"Win Rate: {win_rate:.1f}%")
    print(f"Total PnL: ₹{total_pnl:.2f}")
    
    # Calculate profit factor
    gross_profit = sum(t.pnl for t in [t for r in all_results for t in r.trades if t.pnl > 0])
    gross_loss = abs(sum(t.pnl for t in [t for r in all_results for t in r.trades if t.pnl < 0]))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    print(f"Profit Factor: {profit_factor:.2f}")
    
    # Show sample trades
    if all_results and all_results[0].trades:
        print("\n" + "-"*70)
        print(f"Sample Trades (First Stock: {TEST_STOCKS[0]}):")
        print("-"*70)
        for i, trade in enumerate(all_results[0].trades[:10]):
            print(f"  {i+1}. {trade.signal_type}: Entry ₹{trade.entry_price:.2f} → Exit ₹{trade.exit_price:.2f} "
                  f"(PnL: ₹{trade.pnl:.2f}, {trade.exit_reason})")
    
    print("\n" + "="*70)
    print("ANALYSIS")
    print("="*70)
    
    if profit_factor > 1.2:
        print("\n✅ Strategy is PROFITABLE (PF > 1.2)")
    elif profit_factor > 1.0:
        print("\n⚠️ Strategy is MARGINAL (PF ~ 1.0)")
    else:
        print("\n❌ Strategy is NOT PROFITABLE (PF < 1.0)")
    
    print("\nPossible Improvements:")
    print("1. Add trend filter (only signal reversals against trend)")
    print("2. Add confirmation filter (require 2+ candles in zone)")
    print("3. Add divergence detection")
    print("4. Adjust thresholds (tighter zones)")
    
    return all_results


if __name__ == "__main__":
    run_stochastic_backtest()
