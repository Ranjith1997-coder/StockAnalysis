"""
Evaluation metrics for stock movement prediction models.

This module provides functions to calculate various performance metrics
including returns, risk metrics, and trade-level statistics.
"""

from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np
from dataclasses import dataclass


@dataclass
class ReturnsMetrics:
    """Container for returns-based metrics."""
    total_return: float
    annualized_return: float
    benchmark_return: float
    excess_return: float
    monthly_return_std: float
    positive_months_pct: float
    

@dataclass
class TradeMetrics:
    """Container for trade-level metrics."""
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    expectancy: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    

@dataclass
class RiskMetrics:
    """Container for risk-based metrics."""
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    max_drawdown_duration: int
    calmar_ratio: float
    volatility: float
    downside_deviation: float
    var_95: float  # Value at Risk 95%


def calculate_returns_metrics(
    returns: pd.Series,
    benchmark_returns: Optional[pd.Series] = None,
    trading_days_per_year: int = 252,
) -> ReturnsMetrics:
    """
    Calculate returns-based performance metrics.
    
    Args:
        returns: Daily returns series.
        benchmark_returns: Optional benchmark returns (e.g., Nifty).
        trading_days_per_year: Number of trading days per year.
        
    Returns:
        ReturnsMetrics object with calculated metrics.
    """
    # Total return
    total_return = (1 + returns).prod() - 1
    
    # Annualized return
    n_years = len(returns) / trading_days_per_year
    annualized_return = (1 + total_return) ** (1 / n_years) - 1 if n_years > 0 else 0
    
    # Benchmark return
    if benchmark_returns is not None:
        benchmark_total = (1 + benchmark_returns).prod() - 1
        excess_return = total_return - benchmark_total
    else:
        benchmark_total = 0
        excess_return = total_return
    
    # Monthly statistics
    if isinstance(returns.index, pd.DatetimeIndex):
        monthly_returns = returns.resample('ME').apply(lambda x: (1 + x).prod() - 1)
        monthly_return_std = monthly_returns.std()
        positive_months_pct = (monthly_returns > 0).mean()
    else:
        monthly_return_std = returns.std() * np.sqrt(21)  # Approximate monthly
        positive_months_pct = (returns > 0).mean()
    
    return ReturnsMetrics(
        total_return=total_return,
        annualized_return=annualized_return,
        benchmark_return=benchmark_total,
        excess_return=excess_return,
        monthly_return_std=monthly_return_std,
        positive_months_pct=positive_months_pct,
    )


def calculate_trade_metrics(
    trades: pd.DataFrame,
) -> TradeMetrics:
    """
    Calculate trade-level performance metrics.
    
    Args:
        trades: DataFrame with trade information including 'pnl' column.
        
    Returns:
        TradeMetrics object with calculated metrics.
    """
    if len(trades) == 0:
        return TradeMetrics(
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate=0,
            avg_win=0,
            avg_loss=0,
            profit_factor=0,
            expectancy=0,
            max_consecutive_wins=0,
            max_consecutive_losses=0,
        )
    
    pnls = trades['pnl'].values
    
    # Basic counts
    total_trades = len(trades)
    winning_trades = int(np.sum(pnls > 0))
    losing_trades = int(np.sum(pnls < 0))
    
    # Win rate
    win_rate = winning_trades / total_trades if total_trades > 0 else 0
    
    # Average win/loss
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    avg_win = np.mean(wins) if len(wins) > 0 else 0
    avg_loss = np.mean(losses) if len(losses) > 0 else 0
    
    # Profit factor
    total_wins = np.sum(wins) if len(wins) > 0 else 0
    total_losses = np.abs(np.sum(losses)) if len(losses) > 0 else 0
    profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')
    
    # Expectancy
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * np.abs(avg_loss))
    
    # Consecutive wins/losses
    max_consecutive_wins = 0
    max_consecutive_losses = 0
    current_wins = 0
    current_losses = 0
    
    for pnl in pnls:
        if pnl > 0:
            current_wins += 1
            current_losses = 0
            max_consecutive_wins = max(max_consecutive_wins, current_wins)
        elif pnl < 0:
            current_losses += 1
            current_wins = 0
            max_consecutive_losses = max(max_consecutive_losses, current_losses)
        else:
            current_wins = 0
            current_losses = 0
    
    return TradeMetrics(
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        expectancy=expectancy,
        max_consecutive_wins=max_consecutive_wins,
        max_consecutive_losses=max_consecutive_losses,
    )


def calculate_risk_metrics(
    returns: pd.Series,
    risk_free_rate: float = 0.06,
    trading_days_per_year: int = 252,
) -> RiskMetrics:
    """
    Calculate risk-based performance metrics.
    
    Args:
        returns: Daily returns series.
        risk_free_rate: Annual risk-free rate (default 6%).
        trading_days_per_year: Number of trading days per year.
        
    Returns:
        RiskMetrics object with calculated metrics.
    """
    if len(returns) == 0:
        return RiskMetrics(
            sharpe_ratio=0,
            sortino_ratio=0,
            max_drawdown=0,
            max_drawdown_duration=0,
            calmar_ratio=0,
            volatility=0,
            downside_deviation=0,
            var_95=0,
        )
    
    # Daily risk-free rate
    daily_rf = risk_free_rate / trading_days_per_year
    
    # Excess returns
    excess_returns = returns - daily_rf
    
    # Volatility (annualized)
    volatility = returns.std() * np.sqrt(trading_days_per_year)
    
    # Sharpe ratio
    if volatility > 0:
        sharpe_ratio = (excess_returns.mean() * trading_days_per_year) / volatility
    else:
        sharpe_ratio = 0
    
    # Downside deviation
    negative_returns = returns[returns < 0]
    downside_deviation = np.sqrt(np.mean(negative_returns ** 2)) * np.sqrt(trading_days_per_year)
    
    # Sortino ratio
    if downside_deviation > 0:
        sortino_ratio = (excess_returns.mean() * trading_days_per_year) / downside_deviation
    else:
        sortino_ratio = 0
    
    # Maximum drawdown
    cumulative = (1 + returns).cumprod()
    running_max = cumulative.cummax()
    drawdown = (cumulative - running_max) / running_max
    max_drawdown = drawdown.min()
    
    # Maximum drawdown duration
    is_drawdown = drawdown < 0
    drawdown_periods = is_drawdown.astype(int).groupby(
        (is_drawdown != is_drawdown.shift()).cumsum()
    ).sum()
    max_drawdown_duration = drawdown_periods.max() if len(drawdown_periods) > 0 else 0
    
    # Calmar ratio
    n_years = len(returns) / trading_days_per_year
    annualized_return = (1 + returns).prod() ** (1 / n_years) - 1 if n_years > 0 else 0
    calmar_ratio = annualized_return / np.abs(max_drawdown) if max_drawdown != 0 else 0
    
    # Value at Risk (95%)
    var_95 = np.percentile(returns, 5)
    
    return RiskMetrics(
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        max_drawdown=max_drawdown,
        max_drawdown_duration=int(max_drawdown_duration),
        calmar_ratio=calmar_ratio,
        volatility=volatility,
        downside_deviation=downside_deviation,
        var_95=var_95,
    )


def generate_performance_report(
    returns: pd.Series,
    trades: pd.DataFrame,
    benchmark_returns: Optional[pd.Series] = None,
    risk_free_rate: float = 0.06,
) -> Dict:
    """
    Generate a comprehensive performance report.
    
    Args:
        returns: Daily returns series.
        trades: DataFrame with trade information.
        benchmark_returns: Optional benchmark returns.
        risk_free_rate: Annual risk-free rate.
        
    Returns:
        Dictionary containing all performance metrics.
    """
    returns_metrics = calculate_returns_metrics(returns, benchmark_returns)
    trade_metrics = calculate_trade_metrics(trades)
    risk_metrics = calculate_risk_metrics(returns, risk_free_rate)
    
    return {
        "returns": {
            "total_return": returns_metrics.total_return,
            "annualized_return": returns_metrics.annualized_return,
            "benchmark_return": returns_metrics.benchmark_return,
            "excess_return": returns_metrics.excess_return,
            "monthly_return_std": returns_metrics.monthly_return_std,
            "positive_months_pct": returns_metrics.positive_months_pct,
        },
        "trades": {
            "total_trades": trade_metrics.total_trades,
            "winning_trades": trade_metrics.winning_trades,
            "losing_trades": trade_metrics.losing_trades,
            "win_rate": trade_metrics.win_rate,
            "avg_win": trade_metrics.avg_win,
            "avg_loss": trade_metrics.avg_loss,
            "profit_factor": trade_metrics.profit_factor,
            "expectancy": trade_metrics.expectancy,
            "max_consecutive_wins": trade_metrics.max_consecutive_wins,
            "max_consecutive_losses": trade_metrics.max_consecutive_losses,
        },
        "risk": {
            "sharpe_ratio": risk_metrics.sharpe_ratio,
            "sortino_ratio": risk_metrics.sortino_ratio,
            "max_drawdown": risk_metrics.max_drawdown,
            "max_drawdown_duration": risk_metrics.max_drawdown_duration,
            "calmar_ratio": risk_metrics.calmar_ratio,
            "volatility": risk_metrics.volatility,
            "downside_deviation": risk_metrics.downside_deviation,
            "var_95": risk_metrics.var_95,
        },
    }


def print_performance_report(report: Dict) -> None:
    """Print a formatted performance report."""
    print("\n" + "=" * 70)
    print(" PERFORMANCE REPORT")
    print("=" * 70)
    
    print("\nRETURNS:")
    print("-" * 40)
    r = report["returns"]
    print(f"  Total Return:        {r['total_return']*100:.2f}%")
    print(f"  Annualized Return:   {r['annualized_return']*100:.2f}%")
    print(f"  Benchmark Return:    {r['benchmark_return']*100:.2f}%")
    print(f"  Excess Return:       {r['excess_return']*100:.2f}%")
    print(f"  Monthly Std:         {r['monthly_return_std']*100:.2f}%")
    print(f"  Positive Months:     {r['positive_months_pct']*100:.1f}%")
    
    print("\nTRADES:")
    print("-" * 40)
    t = report["trades"]
    print(f"  Total Trades:        {t['total_trades']}")
    print(f"  Winning Trades:      {t['winning_trades']}")
    print(f"  Losing Trades:       {t['losing_trades']}")
    print(f"  Win Rate:            {t['win_rate']*100:.1f}%")
    print(f"  Average Win:         {t['avg_win']*100:.2f}%")
    print(f"  Average Loss:        {t['avg_loss']*100:.2f}%")
    print(f"  Profit Factor:       {t['profit_factor']:.2f}")
    print(f"  Expectancy:          {t['expectancy']*100:.2f}%")
    print(f"  Max Consecutive Wins: {t['max_consecutive_wins']}")
    print(f"  Max Consecutive Loss: {t['max_consecutive_losses']}")
    
    print("\nRISK METRICS:")
    print("-" * 40)
    rk = report["risk"]
    print(f"  Sharpe Ratio:        {rk['sharpe_ratio']:.2f}")
    print(f"  Sortino Ratio:       {rk['sortino_ratio']:.2f}")
    print(f"  Max Drawdown:        {rk['max_drawdown']*100:.2f}%")
    print(f"  Max DD Duration:     {rk['max_drawdown_duration']} days")
    print(f"  Calmar Ratio:        {rk['calmar_ratio']:.2f}")
    print(f"  Volatility:          {rk['volatility']*100:.2f}%")
    print(f"  Downside Deviation:  {rk['downside_deviation']*100:.2f}%")
    print(f"  VaR (95%):           {rk['var_95']*100:.2f}%")
    
    print("\n" + "=" * 70)
