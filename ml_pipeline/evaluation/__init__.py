"""
Evaluation and backtesting module for stock movement prediction.

This module provides:
- Backtesting engine for simulating trading strategies
- Evaluation metrics for model performance
- Performance reporting and visualization
"""

from ml_pipeline.evaluation.backtest import BacktestEngine, BacktestConfig, BacktestResult
from ml_pipeline.evaluation.metrics import (
    calculate_returns_metrics,
    calculate_trade_metrics,
    calculate_risk_metrics,
    generate_performance_report,
)

__all__ = [
    "BacktestEngine",
    "BacktestConfig",
    "BacktestResult",
    "calculate_returns_metrics",
    "calculate_trade_metrics",
    "calculate_risk_metrics",
    "generate_performance_report",
]
