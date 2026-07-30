"""
Backtesting engine for stock movement prediction models.

This module provides a comprehensive backtesting framework that simulates
trading based on model predictions and calculates performance metrics.
"""

from typing import Any, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
import warnings

from ml_pipeline.evaluation.metrics import (
    generate_performance_report,
    print_performance_report,
    calculate_returns_metrics,
    calculate_trade_metrics,
    calculate_risk_metrics,
)


@dataclass
class BacktestConfig:
    """Configuration for backtesting."""
    
    # Position sizing
    initial_capital: float = 1_000_000.0  # ₹10 Lakh
    position_size_pct: float = 0.10  # 10% per position
    max_positions: int = 10  # Maximum concurrent positions
    
    # Trading rules
    min_confidence: float = 0.0  # Minimum confidence to trade
    trade_up: bool = True  # Trade UP predictions
    trade_down: bool = True  # Trade DOWN predictions (short)
    trade_flat: bool = False  # Trade FLAT predictions (no position)
    
    # Transaction costs
    brokerage_pct: float = 0.0003  # 0.03% brokerage
    slippage_pct: float = 0.0005  # 0.05% slippage
    stt_pct: float = 0.00025  # 0.025% STT (sell side)
    exchange_txn_pct: float = 0.00003  # 0.003% exchange charges
    sebi_turnover_pct: float = 0.000001  # SEBI turnover fee
    gst_pct: float = 0.18  # 18% GST on brokerage
    
    # Risk management
    stop_loss_pct: Optional[float] = 0.05  # 5% stop loss (None to disable)
    take_profit_pct: Optional[float] = 0.10  # 10% take profit (None to disable)
    max_holding_days: int = 10  # Maximum holding period
    
    # Prediction settings
    min_probability: float = 0.40  # Minimum probability to consider
    use_confidence_sizing: bool = False  # Size positions by confidence


@dataclass
class BacktestResult:
    """Container for backtest results."""
    
    # Core results
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    daily_returns: pd.Series
    
    # Performance metrics
    performance_report: Dict
    
    # Metadata
    config: BacktestConfig
    start_date: datetime
    end_date: datetime
    total_days: int
    
    # Detailed results
    position_history: pd.DataFrame = field(default_factory=pd.DataFrame)
    prediction_analysis: pd.DataFrame = field(default_factory=pd.DataFrame)
    
    def get_summary(self) -> Dict:
        """Get summary of backtest results."""
        return {
            "period": f"{self.start_date.date()} to {self.end_date.date()}",
            "total_days": self.total_days,
            "initial_capital": self.config.initial_capital,
            "final_capital": self.equity_curve['equity'].iloc[-1],
            **self.performance_report,
        }
    
    def print_summary(self) -> None:
        """Print formatted summary."""
        print("\n" + "=" * 70)
        print(" BACKTEST RESULTS")
        print("=" * 70)
        print(f"\nPeriod: {self.start_date.date()} to {self.end_date.date()}")
        print(f"Total Days: {self.total_days}")
        print(f"Initial Capital: ₹{self.config.initial_capital:,.2f}")
        print(f"Final Capital: ₹{self.equity_curve['equity'].iloc[-1]:,.2f}")
        print_performance_report(self.performance_report)


class BacktestEngine:
    """
    Backtesting engine for simulating trading strategies.
    
    This engine simulates a trading strategy based on model predictions,
    handling position management, transaction costs, and risk management.
    """
    
    def __init__(self, config: Optional[BacktestConfig] = None):
        """
        Initialize the backtest engine.
        
        Args:
            config: Backtest configuration. Uses defaults if None.
        """
        self.config = config or BacktestConfig()
        
    def run(
        self,
        predictions: pd.DataFrame,
        price_data: pd.DataFrame,
        show_progress: bool = True,
    ) -> BacktestResult:
        """
        Run backtest on predictions.
        
        Args:
            predictions: DataFrame with columns:
                - date: Prediction date
                - symbol: Stock symbol
                - prediction: -1, 0, 1 (DOWN, FLAT, UP)
                - probability: Probability of predicted class
                - confidence: Confidence score
            price_data: DataFrame with columns:
                - date: Date
                - symbol: Stock symbol
                - open: Opening price
                - high: High price
                - low: Low price
                - close: Closing price
            show_progress: Whether to show progress messages.
            
        Returns:
            BacktestResult with all backtest data and metrics.
        """
        # Validate inputs
        self._validate_inputs(predictions, price_data)
        
        # Initialize
        capital = self.config.initial_capital
        positions: Dict[str, Dict] = {}  # Active positions
        trades_list: List[Dict] = []
        equity_history: List[Dict] = []
        
        # Get unique dates
        dates = sorted(predictions['date'].unique())
        
        if show_progress:
            print(f"Running backtest on {len(dates)} trading days...")
        
        for i, date in enumerate(dates):
            # Get predictions for this date
            day_preds = predictions[predictions['date'] == date].copy()
            
            # Get prices for this date
            day_prices = price_data[price_data['date'] == date].copy()
            
            # Process exits first (before new entries)
            positions, capital = self._process_exits(
                positions, capital, day_prices, date, trades_list
            )
            
            # Process new entries
            positions, capital = self._process_entries(
                positions, capital, day_preds, day_prices, date
            )
            
            # Calculate current equity
            equity = self._calculate_equity(positions, capital, day_prices)
            
            equity_history.append({
                'date': date,
                'equity': equity,
                'cash': capital,
                'positions_value': equity - capital,
                'num_positions': len(positions),
            })
            
            if show_progress and (i + 1) % 50 == 0:
                print(f"  Processed {i + 1}/{len(dates)} days...")
        
        # Close all remaining positions
        last_date = dates[-1]
        last_prices = price_data[price_data['date'] == last_date]
        positions, capital = self._close_all_positions(
            positions, capital, last_prices, last_date, trades_list
        )
        
        # Create results
        equity_curve = pd.DataFrame(equity_history)
        equity_curve['date'] = pd.to_datetime(equity_curve['date'])
        equity_curve = equity_curve.set_index('date').sort_index()
        
        # Calculate daily returns
        equity_curve['daily_return'] = equity_curve['equity'].pct_change()
        daily_returns = equity_curve['daily_return'].dropna()
        
        # Create trades DataFrame
        trades_df = pd.DataFrame(trades_list)
        if len(trades_df) > 0:
            trades_df['entry_date'] = pd.to_datetime(trades_df['entry_date'])
            trades_df['exit_date'] = pd.to_datetime(trades_df['exit_date'])
        
        # Generate performance report
        performance_report = generate_performance_report(
            returns=daily_returns,
            trades=trades_df,
        )
        
        return BacktestResult(
            equity_curve=equity_curve,
            trades=trades_df,
            daily_returns=daily_returns,
            performance_report=performance_report,
            config=self.config,
            start_date=pd.to_datetime(dates[0]),
            end_date=pd.to_datetime(dates[-1]),
            total_days=len(dates),
        )
    
    def _validate_inputs(
        self,
        predictions: pd.DataFrame,
        price_data: pd.DataFrame,
    ) -> None:
        """Validate input DataFrames."""
        required_pred_cols = ['date', 'symbol', 'prediction', 'probability']
        missing_pred = [c for c in required_pred_cols if c not in predictions.columns]
        if missing_pred:
            raise ValueError(f"Predictions missing columns: {missing_pred}")
        
        required_price_cols = ['date', 'symbol', 'open', 'high', 'low', 'close']
        missing_price = [c for c in required_price_cols if c not in price_data.columns]
        if missing_price:
            raise ValueError(f"Price data missing columns: {missing_price}")
    
    def _process_entries(
        self,
        positions: Dict[str, Dict],
        capital: float,
        predictions: pd.DataFrame,
        prices: pd.DataFrame,
        date: Any,
    ) -> Tuple[Dict[str, Dict], float]:
        """Process new position entries."""
        if len(positions) >= self.config.max_positions:
            return positions, capital
        
        # Filter predictions by trading rules
        valid_preds = self._filter_predictions(predictions)
        
        # Sort by confidence/probability
        if 'confidence' in valid_preds.columns:
            valid_preds = valid_preds.sort_values('confidence', ascending=False)
        else:
            valid_preds = valid_preds.sort_values('probability', ascending=False)
        
        for _, pred in valid_preds.iterrows():
            if len(positions) >= self.config.max_positions:
                break
            
            symbol = pred['symbol']
            
            # Skip if already in position
            if symbol in positions:
                continue
            
            # Get price data
            stock_price = prices[prices['symbol'] == symbol]
            if len(stock_price) == 0:
                continue
            
            entry_price = float(stock_price['open'].iloc[0])
            
            # Calculate position size
            position_value = capital * self.config.position_size_pct
            
            if self.config.use_confidence_sizing and 'confidence' in pred:
                position_value *= pred['confidence']
            
            # Check if we have enough capital
            if position_value > capital * 0.95:
                continue
            
            # Calculate transaction costs
            entry_cost = self._calculate_entry_cost(position_value)
            total_cost = position_value + entry_cost
            
            if total_cost > capital:
                continue
            
            # Create position
            direction = 1 if pred['prediction'] == 1 else -1
            shares = int(position_value / entry_price)
            
            if shares <= 0:
                continue
            
            positions[symbol] = {
                'entry_date': date,
                'entry_price': entry_price,
                'shares': shares,
                'direction': direction,
                'initial_value': shares * entry_price,
                # For LONG: stop loss below entry, take profit above entry
                # For SHORT: stop loss above entry, take profit below entry
                'stop_loss': entry_price * (1 - self.config.stop_loss_pct) if self.config.stop_loss_pct and direction == 1
                             else entry_price * (1 + self.config.stop_loss_pct) if self.config.stop_loss_pct else None,
                'take_profit': entry_price * (1 + self.config.take_profit_pct) if self.config.take_profit_pct and direction == 1
                               else entry_price * (1 - self.config.take_profit_pct) if self.config.take_profit_pct else None,
                'holding_days': 0,
                'prediction': pred['prediction'],
                'probability': pred.get('probability', 0.5),
                'confidence': pred.get('confidence', 0.5),
            }
            
            # Deduct from capital
            capital -= total_cost
        
        return positions, capital
    
    def _process_exits(
        self,
        positions: Dict[str, Dict],
        capital: float,
        prices: pd.DataFrame,
        date: Any,
        trades_list: List[Dict],
    ) -> Tuple[Dict[str, Dict], float]:
        """Process position exits."""
        positions_to_close = []
        
        for symbol, pos in positions.items():
            stock_price = prices[prices['symbol'] == symbol]
            
            if len(stock_price) == 0:
                # No price data, increment holding days
                pos['holding_days'] += 1
                continue
            
            high = float(stock_price['high'].iloc[0])
            low = float(stock_price['low'].iloc[0])
            close = float(stock_price['close'].iloc[0])
            
            exit_price = None
            exit_reason = None
            
            # Check stop loss
            if pos['stop_loss'] and pos['direction'] == 1:
                if low <= pos['stop_loss']:
                    exit_price = pos['stop_loss']
                    exit_reason = 'stop_loss'
            elif pos['stop_loss'] and pos['direction'] == -1:
                if high >= pos['stop_loss']:
                    exit_price = pos['stop_loss']
                    exit_reason = 'stop_loss'
            
            # Check take profit
            if exit_price is None and pos['take_profit'] and pos['direction'] == 1:
                if high >= pos['take_profit']:
                    exit_price = pos['take_profit']
                    exit_reason = 'take_profit'
            elif exit_price is None and pos['take_profit'] and pos['direction'] == -1:
                if low <= pos['take_profit']:
                    exit_price = pos['take_profit']
                    exit_reason = 'take_profit'
            
            # Check max holding period
            if exit_price is None and pos['holding_days'] >= self.config.max_holding_days:
                exit_price = close
                exit_reason = 'max_holding'
            
            if exit_price is not None:
                positions_to_close.append((symbol, exit_price, exit_reason))
            else:
                pos['holding_days'] += 1
        
        # Close positions
        for symbol, exit_price, exit_reason in positions_to_close:
            pos = positions[symbol]
            
            # Calculate P&L
            if pos['direction'] == 1:  # Long
                pnl_per_share = exit_price - pos['entry_price']
            else:  # Short
                pnl_per_share = pos['entry_price'] - exit_price
            
            gross_pnl = pnl_per_share * pos['shares']
            exit_value = exit_price * pos['shares']
            
            # Calculate exit costs
            exit_cost = self._calculate_exit_cost(exit_value)
            net_pnl = gross_pnl - exit_cost
            
            # Record trade
            trades_list.append({
                'symbol': symbol,
                'entry_date': pos['entry_date'],
                'exit_date': date,
                'entry_price': pos['entry_price'],
                'exit_price': exit_price,
                'shares': pos['shares'],
                'direction': 'LONG' if pos['direction'] == 1 else 'SHORT',
                'gross_pnl': gross_pnl,
                'costs': exit_cost,
                'pnl': net_pnl,
                'return': net_pnl / pos['initial_value'],
                'exit_reason': exit_reason,
                'holding_days': pos['holding_days'],
                'prediction': pos['prediction'],
                'probability': pos['probability'],
            })
            
            # Return capital
            capital += exit_value + net_pnl
            
            # Remove position
            del positions[symbol]
        
        return positions, capital
    
    def _close_all_positions(
        self,
        positions: Dict[str, Dict],
        capital: float,
        prices: pd.DataFrame,
        date: Any,
        trades_list: List[Dict],
    ) -> Tuple[Dict[str, Dict], float]:
        """Close all remaining positions."""
        for symbol in list(positions.keys()):
            pos = positions[symbol]
            stock_price = prices[prices['symbol'] == symbol]
            
            if len(stock_price) > 0:
                exit_price = float(stock_price['close'].iloc[0])
            else:
                exit_price = pos['entry_price']  # No change if no price data
            
            # Calculate P&L
            if pos['direction'] == 1:
                pnl_per_share = exit_price - pos['entry_price']
            else:
                pnl_per_share = pos['entry_price'] - exit_price
            
            gross_pnl = pnl_per_share * pos['shares']
            exit_value = exit_price * pos['shares']
            exit_cost = self._calculate_exit_cost(exit_value)
            net_pnl = gross_pnl - exit_cost
            
            trades_list.append({
                'symbol': symbol,
                'entry_date': pos['entry_date'],
                'exit_date': date,
                'entry_price': pos['entry_price'],
                'exit_price': exit_price,
                'shares': pos['shares'],
                'direction': 'LONG' if pos['direction'] == 1 else 'SHORT',
                'gross_pnl': gross_pnl,
                'costs': exit_cost,
                'pnl': net_pnl,
                'return': net_pnl / pos['initial_value'],
                'exit_reason': 'end_of_backtest',
                'holding_days': pos['holding_days'],
                'prediction': pos['prediction'],
                'probability': pos['probability'],
            })
            
            capital += exit_value + net_pnl
            del positions[symbol]
        
        return positions, capital
    
    def _filter_predictions(self, predictions: pd.DataFrame) -> pd.DataFrame:
        """Filter predictions based on trading rules."""
        filtered = predictions.copy()
        
        # Filter by minimum probability
        filtered = filtered[filtered['probability'] >= self.config.min_probability]
        
        # Filter by minimum confidence
        if 'confidence' in filtered.columns:
            filtered = filtered[filtered['confidence'] >= self.config.min_confidence]
        
        # Filter by prediction type
        valid_predictions = []
        if self.config.trade_up:
            valid_predictions.append(1)
        if self.config.trade_down:
            valid_predictions.append(-1)
        if self.config.trade_flat:
            valid_predictions.append(0)
        
        filtered = filtered[filtered['prediction'].isin(valid_predictions)]
        
        return filtered
    
    def _calculate_entry_cost(self, value: float) -> float:
        """Calculate total entry transaction costs."""
        brokerage = value * self.config.brokerage_pct
        gst = brokerage * self.config.gst_pct
        exchange_txn = value * self.config.exchange_txn_pct
        sebi = value * self.config.sebi_turnover_pct
        slippage = value * self.config.slippage_pct
        
        return brokerage + gst + exchange_txn + sebi + slippage
    
    def _calculate_exit_cost(self, value: float) -> float:
        """Calculate total exit transaction costs."""
        brokerage = value * self.config.brokerage_pct
        gst = brokerage * self.config.gst_pct
        exchange_txn = value * self.config.exchange_txn_pct
        sebi = value * self.config.sebi_turnover_pct
        stt = value * self.config.stt_pct  # STT on sell side
        slippage = value * self.config.slippage_pct
        
        return brokerage + gst + exchange_txn + sebi + stt + slippage
    
    def _calculate_equity(
        self,
        positions: Dict[str, Dict],
        capital: float,
        prices: pd.DataFrame,
    ) -> float:
        """Calculate current equity (cash + positions value)."""
        positions_value = 0
        
        for symbol, pos in positions.items():
            stock_price = prices[prices['symbol'] == symbol]
            
            if len(stock_price) > 0:
                current_price = float(stock_price['close'].iloc[0])
            else:
                current_price = pos['entry_price']
            
            if pos['direction'] == 1:
                positions_value += pos['shares'] * current_price
            else:
                # For shorts, calculate unrealized P&L
                unrealized_pnl = (pos['entry_price'] - current_price) * pos['shares']
                positions_value += pos['initial_value'] + unrealized_pnl
        
        return capital + positions_value


def run_simple_backtest(
    predictions: pd.DataFrame,
    price_data: pd.DataFrame,
    initial_capital: float = 1_000_000,
    position_size_pct: float = 0.10,
    max_positions: int = 10,
) -> BacktestResult:
    """
    Run a simple backtest with default settings.
    
    Args:
        predictions: DataFrame with predictions.
        price_data: DataFrame with price data.
        initial_capital: Starting capital.
        position_size_pct: Position size as fraction of capital.
        max_positions: Maximum concurrent positions.
        
    Returns:
        BacktestResult object.
    """
    config = BacktestConfig(
        initial_capital=initial_capital,
        position_size_pct=position_size_pct,
        max_positions=max_positions,
    )
    engine = BacktestEngine(config)
    return engine.run(predictions, price_data)
