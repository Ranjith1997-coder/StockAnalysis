"""
Backtesting infrastructure for stock trading strategies
"""

from tools.backtest.backtest import Backtester, BacktestResult, Trade
from tools.backtest.optimizer import ThresholdOptimizer, BulkOptimizer, SEARCH_SPACES

__all__ = [
    'Backtester', 'BacktestResult', 'Trade',
    'ThresholdOptimizer', 'BulkOptimizer', 'SEARCH_SPACES',
]
