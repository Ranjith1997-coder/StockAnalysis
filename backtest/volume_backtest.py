"""
Backtest for Volume Analysis strategies.

Strategies tested:
1. VOLUME_BREAKOUT - Volume spike with price confirmation
2. OBV_DIVERGENCE - On-Balance Volume divergence
3. VOLUME_CLIMAX - Volume climax exhaustion reversal

Usage:
    python backtest/volume_backtest.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.backtest import Backtester
from analyser.VolumeAnalyser import VolumeAnalyser
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


def run_volume_breakout_backtest():
    """Run backtest for Volume Breakout strategy."""
    print("\n" + "="*70)
    print("VOLUME BREAKOUT STRATEGY BACKTEST")
    print("="*70)
    
    analyser = VolumeAnalyser()
    
    def analyzer_method(stock):
        return analyser.analyse_volume_breakout(stock)
    
    return _run_backtest("Volume Breakout", analyzer_method)


def run_obv_divergence_backtest():
    """Run backtest for OBV Divergence strategy."""
    print("\n" + "="*70)
    print("OBV DIVERGENCE STRATEGY BACKTEST")
    print("="*70)
    
    analyser = VolumeAnalyser()
    
    def analyzer_method(stock):
        return analyser.analyse_obv_divergence(stock)
    
    return _run_backtest("OBV Divergence", analyzer_method)


def run_volume_climax_backtest():
    """Run backtest for Volume Climax strategy."""
    print("\n" + "="*70)
    print("VOLUME CLIMAX STRATEGY BACKTEST")
    print("="*70)
    
    analyser = VolumeAnalyser()
    
    def analyzer_method(stock):
        return analyser.analyse_volume_climax(stock)
    
    return _run_backtest("Volume Climax", analyzer_method)


def _run_backtest(strategy_name, analyzer_method):
    """Helper function to run backtest."""
    print(f"\nTest Stocks: {TEST_STOCKS}")
    print(f"Period: {START_DATE} to {END_DATE}")
    print(f"Risk/Reward: 3:1 (3% target, 1% stop loss)")
    
    all_results = []
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_pnl = 0
    
    print("\n" + "-"*70)
    print("Running backtest...")
    
    for symbol in TEST_STOCKS:
        try:
            backtester = Backtester(
                stock_symbols=symbol,
                analyzer_methods=analyzer_method,
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
    
    return {
        'strategy': strategy_name,
        'trades': total_trades,
        'wins': total_wins,
        'losses': total_losses,
        'win_rate': win_rate,
        'pnl': total_pnl,
        'profit_factor': profit_factor
    }


def main():
    """Run all volume strategy backtests and compare."""
    results = []
    
    # Run all backtests
    results.append(run_volume_breakout_backtest())
    results.append(run_obv_divergence_backtest())
    results.append(run_volume_climax_backtest())
    
    # Print comparison
    print("\n" + "="*70)
    print("STRATEGY COMPARISON")
    print("="*70)
    
    print(f"\n{'Strategy':<25} {'Trades':>8} {'Win Rate':>10} {'PnL':>15} {'PF':>8}")
    print("-"*70)
    
    for r in results:
        print(f"{r['strategy']:<25} {r['trades']:>8} {r['win_rate']:>9.1f}% ₹{r['pnl']:>12.2f} {r['profit_factor']:>7.2f}")
    
    print("\n" + "="*70)
    print("RECOMMENDATIONS")
    print("="*70)
    
    # Find best strategy
    best_by_pnl = max(results, key=lambda x: x['pnl'])
    best_by_pf = max(results, key=lambda x: x['profit_factor'])
    best_by_wr = max(results, key=lambda x: x['win_rate'])
    
    print(f"\nBest by PnL: {best_by_pnl['strategy']} (₹{best_by_pnl['pnl']:.2f})")
    print(f"Best by Profit Factor: {best_by_pf['strategy']} ({best_by_pf['profit_factor']:.2f})")
    print(f"Best by Win Rate: {best_by_wr['strategy']} ({best_by_wr['win_rate']:.1f}%)")
    
    # Recommendations
    print("\n1. Volume Breakout: Good for trend confirmation")
    print("2. OBV Divergence: Best for reversal signals (smart money tracking)")
    print("3. Volume Climax: Best for exhaustion reversals (rare but high conviction)")


if __name__ == "__main__":
    main()
