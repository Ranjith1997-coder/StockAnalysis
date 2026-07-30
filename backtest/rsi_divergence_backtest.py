"""
Backtest for RSI Divergence strategy.

RSI Divergence Logic:
- Bullish Divergence: Price makes lower low, but RSI makes higher low → BULLISH
- Bearish Divergence: Price makes higher high, but RSI makes lower high → BEARISH

This is a stronger reversal signal than simple overbought/oversold.

Usage:
    python backtest/rsi_divergence_backtest.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.backtest import Backtester
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


def run_rsi_divergence_backtest():
    """
    Run backtest for RSI Divergence strategy.
    """
    print("\n" + "="*70)
    print("RSI DIVERGENCE STRATEGY BACKTEST")
    print("="*70)
    
    # Show current parameters
    print(f"\nCurrent Parameters:")
    print(f"  RSI_DIVERGENCE_LOOKBACK = {TechnicalAnalyser.RSI_DIVERGENCE_LOOKBACK}")
    print(f"  RSI_DIVERGENCE_SWING_ORDER = {TechnicalAnalyser.RSI_DIVERGENCE_SWING_ORDER}")
    print(f"  RSI_LOOKUP_PERIOD = {TechnicalAnalyser.RSI_LOOKUP_PERIOD}")
    
    print(f"\nStrategy Logic:")
    print(f"  Bullish Divergence: Price LL + RSI HL → BULLISH")
    print(f"  Bearish Divergence: Price HH + RSI LH → BEARISH")
    
    print(f"\nTest Stocks: {TEST_STOCKS}")
    print(f"Period: {START_DATE} to {END_DATE}")
    
    # Initialize analyser
    analyser = TechnicalAnalyser()
    
    def rsi_divergence_analyzer(stock):
        """Analyzer method for backtester."""
        return analyser.analyse_rsi_divergence(stock)
    
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
                analyzer_methods=rsi_divergence_analyzer,
                start_date=START_DATE,
                end_date=END_DATE,
                interval="day",
                initial_capital=100000,
                position_size=20000,
                stop_loss_pct=1.0,  # 1% stop loss
                target_pct=3.0,  # 3% target (3:1 reward/risk)
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
    print("NEXT STEPS")
    print("="*70)
    print("\n1. Compare with RSI overbought/oversold backtest")
    print("2. If profit factor > 1.0, divergence is working")
    print("3. Consider combining RSI overbought/oversold + divergence for stronger signals")
    
    return all_results


if __name__ == "__main__":
    run_rsi_divergence_backtest()
