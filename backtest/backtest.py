import sys
import os
sys.path.append(os.getcwd())

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Any, Callable, Optional, Union
import yfinance as yf
from collections import namedtuple
import json

from common.Stock import Stock
from common.logging_util import logger
from analyser.Analyser import BaseAnalyzer
import common.shared as shared
from common.helperFunctions import percentageChange


class Trade:
    """Represents a single trade with entry and exit details"""
    def __init__(self, entry_date, entry_price, signal_type, position_size=1, stop_loss=None, target=None):
        self.entry_date = entry_date
        self.entry_price = entry_price
        self.signal_type = signal_type  # 'BULLISH' or 'BEARISH'
        self.position_size = position_size
        self.stop_loss = stop_loss
        self.target = target
        
        self.exit_date = None
        self.exit_price = None
        self.exit_reason = None  # 'target', 'stop_loss', 'signal', 'end_of_data'
        self.pnl = 0.0
        self.pnl_percent = 0.0
        self.holding_period = 0
        self.analysis_details = {}  # Store the analysis that triggered the trade
        
    def close_trade(self, exit_date, exit_price, exit_reason='signal'):
        """Close the trade and calculate P&L"""
        self.exit_date = exit_date
        self.exit_price = exit_price
        self.exit_reason = exit_reason
        self.holding_period = (exit_date - self.entry_date).days if hasattr(exit_date, 'days') else 0
        
        if self.signal_type == 'BULLISH':
            self.pnl = (exit_price - self.entry_price) * self.position_size
            self.pnl_percent = percentageChange(exit_price, self.entry_price)
        else:  # BEARISH - short position
            self.pnl = (self.entry_price - exit_price) * self.position_size
            self.pnl_percent = percentageChange(self.entry_price, exit_price)
    
    def check_stop_loss(self, current_price):
        """Check if stop loss is hit"""
        if self.stop_loss is None:
            return False
        
        if self.signal_type == 'BULLISH':
            return current_price <= self.stop_loss
        else:  # BEARISH
            return current_price >= self.stop_loss
    
    def check_target(self, current_price):
        """Check if target is hit"""
        if self.target is None:
            return False
        
        if self.signal_type == 'BULLISH':
            return current_price >= self.target
        else:  # BEARISH
            return current_price <= self.target
    
    def to_dict(self):
        """Convert trade to dictionary for reporting"""
        return {
            'entry_date': self.entry_date,
            'entry_price': self.entry_price,
            'exit_date': self.exit_date,
            'exit_price': self.exit_price,
            'signal_type': self.signal_type,
            'position_size': self.position_size,
            'pnl': self.pnl,
            'pnl_percent': self.pnl_percent,
            'holding_period': self.holding_period,
            'exit_reason': self.exit_reason,
            'stop_loss': self.stop_loss,
            'target': self.target,
            'analysis_details': self.analysis_details
        }


class BacktestResult:
    """Container for backtest results and performance metrics"""
    def __init__(self, stock_symbol, start_date, end_date, initial_capital):
        self.stock_symbol = stock_symbol
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.final_capital = initial_capital
        
        self.trades: List[Trade] = []
        self.equity_curve = []
        self.metrics = {}
        
    def add_trade(self, trade: Trade):
        """Add a completed trade to results"""
        self.trades.append(trade)
        self.final_capital += trade.pnl
        
    def calculate_metrics(self):
        """Calculate performance metrics"""
        if not self.trades:
            self.metrics = {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0,
                'total_return': 0,
                'total_return_percent': 0
            }
            return self.metrics
        
        winning_trades = [t for t in self.trades if t.pnl > 0]
        losing_trades = [t for t in self.trades if t.pnl < 0]
        
        total_return = self.final_capital - self.initial_capital
        total_return_pct = (total_return / self.initial_capital) * 100
        
        avg_win = np.mean([t.pnl for t in winning_trades]) if winning_trades else 0
        avg_loss = np.mean([t.pnl for t in losing_trades]) if losing_trades else 0
        
        max_win = max([t.pnl for t in self.trades]) if self.trades else 0
        max_loss = min([t.pnl for t in self.trades]) if self.trades else 0
        
        # Calculate max drawdown
        equity_values = [self.initial_capital]
        for trade in self.trades:
            equity_values.append(equity_values[-1] + trade.pnl)
        
        peak = equity_values[0]
        max_dd = 0
        for value in equity_values:
            if value > peak:
                peak = value
            dd = (peak - value) / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        
        # Profit factor
        total_profit = sum([t.pnl for t in winning_trades])
        total_loss = abs(sum([t.pnl for t in losing_trades]))
        profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')
        
        # Sharpe ratio (simplified - annualized)
        returns = [t.pnl_percent for t in self.trades]
        if len(returns) > 1:
            avg_return = np.mean(returns)
            std_return = np.std(returns)
            sharpe = (avg_return / std_return) * np.sqrt(252) if std_return > 0 else 0
        else:
            sharpe = 0
        
        self.metrics = {
            'total_trades': len(self.trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': len(winning_trades) / len(self.trades) * 100 if self.trades else 0,
            'total_return': total_return,
            'total_return_percent': total_return_pct,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'max_win': max_win,
            'max_loss': max_loss,
            'profit_factor': profit_factor,
            'max_drawdown': max_dd,
            'sharpe_ratio': sharpe,
            'avg_holding_period': np.mean([t.holding_period for t in self.trades]) if self.trades else 0,
            'risk_reward_ratio': abs(avg_win / avg_loss) if avg_loss != 0 else 0,
            'expectancy': np.mean([t.pnl for t in self.trades]) if self.trades else 0
        }
        
        return self.metrics
    
    def get_trade_summary(self):
        """Get summary of all trades"""
        return pd.DataFrame([t.to_dict() for t in self.trades])
    
    def print_summary(self):
        """Print detailed summary of backtest results"""
        print("\n" + "="*80)
        print(f"BACKTEST RESULTS: {self.stock_symbol}")
        print("="*80)
        print(f"Period: {self.start_date} to {self.end_date}")
        print(f"Initial Capital: ₹{self.initial_capital:,.2f}")
        print(f"Final Capital: ₹{self.final_capital:,.2f}")
        print("-"*80)
        
        if not self.trades:
            print("No trades executed during the backtest period")
            return
        
        metrics = self.calculate_metrics()
        
        print(f"Total Trades: {metrics['total_trades']}")
        print(f"Winning Trades: {metrics['winning_trades']} ({metrics['win_rate']:.2f}%)")
        print(f"Losing Trades: {metrics['losing_trades']}")
        print("-"*80)
        print(f"Total Return: ₹{metrics['total_return']:,.2f} ({metrics['total_return_percent']:.2f}%)")
        print(f"Average Win: ₹{metrics['avg_win']:,.2f}")
        print(f"Average Loss: ₹{metrics['avg_loss']:,.2f}")
        print(f"Max Win: ₹{metrics['max_win']:,.2f}")
        print(f"Max Loss: ₹{metrics['max_loss']:,.2f}")
        print("-"*80)
        print(f"Profit Factor: {metrics['profit_factor']:.2f}")
        print(f"Risk/Reward Ratio: {metrics['risk_reward_ratio']:.2f}")
        print(f"Max Drawdown: {metrics['max_drawdown']:.2f}%")
        print(f"Sharpe Ratio: {metrics['sharpe_ratio']:.2f}")
        print(f"Expectancy: ₹{metrics['expectancy']:.2f}")
        print(f"Avg Holding Period: {metrics['avg_holding_period']:.1f} days")
        print("="*80 + "\n")


class Backtester:
    """
    Comprehensive backtesting infrastructure for stock trading strategies.
    
    Features:
    - Support for any analyzer function from the BaseAnalyzer classes
    - Configurable timeframes (intraday/positional)
    - Stop loss and target management
    - Position sizing
    - Multiple signal handling
    - Detailed performance metrics
    """
    
    def __init__(
        self,
        stock_symbols: Union[str, List[str]],
        analyzer_methods: Union[Callable, List[Callable]],
        start_date: Union[str, datetime],
        end_date: Union[str, datetime],
        initial_capital: float = 100000,
        position_size_method: str = 'equal',  # 'equal', 'percentage', 'kelly'
        position_size: float = 10000,  # Amount per trade or percentage
        stop_loss_pct: Optional[float] = None,  # Stop loss as percentage
        target_pct: Optional[float] = None,  # Target as percentage
        trailing_stop: bool = False,
        allow_short: bool = True,  # Allow bearish trades
        max_positions: int = 1,  # Max concurrent positions per stock
        timeframe: str = 'positional',  # 'intraday' or 'positional'
        interval: str = 'day'  # 'day', '5minute', etc.
    ):
        self.stock_symbols = [stock_symbols] if isinstance(stock_symbols, str) else stock_symbols
        self.analyzer_methods = [analyzer_methods] if callable(analyzer_methods) else analyzer_methods
        self.start_date = pd.to_datetime(start_date)
        self.end_date = pd.to_datetime(end_date)
        self.initial_capital = initial_capital
        self.position_size_method = position_size_method
        self.position_size = position_size
        self.stop_loss_pct = stop_loss_pct
        self.target_pct = target_pct
        self.trailing_stop = trailing_stop
        self.allow_short = allow_short
        self.max_positions = max_positions
        self.timeframe = timeframe
        self.interval = interval
        
        self.results: Dict[str, BacktestResult] = {}
        self.stocks: Dict[str, Stock] = {}
        
    def load_data(self, stock_symbol: str) -> pd.DataFrame:
        """Load historical data for a stock"""
        try:
            logger.info(f"Loading data for {stock_symbol} from {self.start_date} to {self.end_date}")
            
            # Determine yfinance symbol
            if stock_symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
                if stock_symbol == "NIFTY":
                    yf_symbol = "^NSEI"
                elif stock_symbol == "BANKNIFTY":
                    yf_symbol = "^NSEBANK"
                else:
                    yf_symbol = stock_symbol + ".NS"
            else:
                yf_symbol = stock_symbol + ".NS"
            
            # Download data with buffer for indicator calculation
            buffer_days = 200  # Extra days for indicators
            buffer_start = self.start_date - timedelta(days=buffer_days)
            
            data = yf.download(
                yf_symbol,
                start=buffer_start,
                end=self.end_date + timedelta(days=1),
                interval='1d' if self.interval == 'day' else self.interval,
                progress=False
            )
            
            if data.empty:
                raise ValueError(f"No data found for {stock_symbol}")
            
            # Handle MultiIndex columns from yfinance (flatten if needed)
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)
            
            # Ensure proper column names (capitalize first letter)
            data.columns = [col.capitalize() if isinstance(col, str) else col for col in data.columns]
            
            # For intraday, we need proper timezone handling
            if self.interval != 'day':
                data.index = pd.to_datetime(data.index)
                if data.index.tz is None:
                    data.index = data.index.tz_localize('Asia/Kolkata')
                else:
                    data.index = data.index.tz_convert('Asia/Kolkata')
            
            logger.info(f"Loaded {len(data)} data points for {stock_symbol}")
            return data
            
        except Exception as e:
            logger.error(f"Error loading data for {stock_symbol}: {e}")
            raise
    
    def create_stock_object(self, stock_symbol: str, price_data: pd.DataFrame) -> Stock:
        """Create Stock object with loaded data"""
        is_index = stock_symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY"]
        stock = Stock(stock_symbol, stock_symbol, is_index=is_index)
        stock._priceData = price_data
        
        # Set previous day OHLCV
        if len(price_data) > 1:
            prev_row = price_data.iloc[-2]
            stock.set_prev_day_ohlcv(
                prev_row['Open'],
                prev_row['Close'],
                prev_row['High'],
                prev_row['Low'],
                prev_row['Volume']
            )
        
        return stock
    
    def run_analysis_on_date(self, stock: Stock, current_idx: int, full_data: pd.DataFrame) -> Dict[str, Any]:
        """Run analyzer methods on stock data up to current date"""
        # Create a view of data up to current date (don't modify original)
        stock._priceData = full_data.iloc[:current_idx + 1].copy()
        
        # Reset analysis
        stock.analysis = {
            "Timestamp": stock.priceData.index[current_idx],
            "BULLISH": {},
            "BEARISH": {},
            "NEUTRAL": {},
            "NoOfTrends": 0,
        }
        
        # Run each analyzer method
        for analyzer_method in self.analyzer_methods:
            try:
                analyzer_method(stock)
            except Exception as e:
                logger.error(f"Error running analyzer {analyzer_method.__name__}: {e}")
        
        return stock.analysis
    
    def calculate_position_quantity(self, price: float, available_capital: float) -> float:
        """Calculate position size based on method"""
        if self.position_size_method == 'equal':
            quantity = self.position_size / price
        elif self.position_size_method == 'percentage':
            quantity = (available_capital * self.position_size / 100) / price
        else:  # Default to equal
            quantity = self.position_size / price
        
        return quantity
    
    def run_backtest(self, stock_symbol: str) -> BacktestResult:
        """Run backtest for a single stock"""
        logger.info(f"Starting backtest for {stock_symbol}")
        
        # Load data
        price_data = self.load_data(stock_symbol)
        
        # Initialize result
        result = BacktestResult(stock_symbol, self.start_date, self.end_date, self.initial_capital)
        
        # Create stock object
        stock = self.create_stock_object(stock_symbol, price_data)
        
        # Keep reference to full data
        full_price_data = price_data.copy()
        
        # Track open positions
        open_positions: List[Trade] = []
        available_capital = self.initial_capital
        
        # Filter to backtest period
        test_data = price_data[price_data.index >= self.start_date]
        test_indices = [price_data.index.get_loc(idx) for idx in test_data.index]
        
        # Iterate through each date
        for i, current_idx in enumerate(test_indices):
            current_date = price_data.index[current_idx]
            current_row = price_data.iloc[current_idx]
            current_price = current_row['Close']
            
            # Check open positions for exit signals
            for position in open_positions[:]:
                # Check stop loss
                if position.check_stop_loss(current_price):
                    position.close_trade(current_date, current_price, 'stop_loss')
                    result.add_trade(position)
                    available_capital += position.pnl
                    open_positions.remove(position)
                    logger.info(f"Stop loss hit: {position.signal_type} at {current_price}")
                    continue
                
                # Check target
                if position.check_target(current_price):
                    position.close_trade(current_date, current_price, 'target')
                    result.add_trade(position)
                    available_capital += position.pnl
                    open_positions.remove(position)
                    logger.info(f"Target hit: {position.signal_type} at {current_price}")
                    continue
            
            # Skip if not enough historical data for indicators
            if current_idx < 50:
                continue
            
            # Run analysis (pass full data to avoid index issues)
            analysis = self.run_analysis_on_date(stock, current_idx, full_price_data)
            
            # Check for entry signals
            has_bullish = len(analysis['BULLISH']) > 0
            has_bearish = len(analysis['BEARISH']) > 0 and self.allow_short
            
            # Close opposite positions if signal reverses
            if has_bullish:
                for position in [p for p in open_positions if p.signal_type == 'BEARISH']:
                    position.close_trade(current_date, current_price, 'signal_reversal')
                    result.add_trade(position)
                    available_capital += position.pnl
                    open_positions.remove(position)
            
            if has_bearish:
                for position in [p for p in open_positions if p.signal_type == 'BULLISH']:
                    position.close_trade(current_date, current_price, 'signal_reversal')
                    result.add_trade(position)
                    available_capital += position.pnl
                    open_positions.remove(position)
            
            # Open new positions if signal present and capacity available
            if len(open_positions) < self.max_positions:
                if has_bullish and not any(p.signal_type == 'BULLISH' for p in open_positions):
                    quantity = self.calculate_position_quantity(current_price, available_capital)
                    
                    # Calculate stop loss and target
                    stop_loss = current_price * (1 - self.stop_loss_pct / 100) if self.stop_loss_pct else None
                    target = current_price * (1 + self.target_pct / 100) if self.target_pct else None
                    
                    trade = Trade(current_date, current_price, 'BULLISH', quantity, stop_loss, target)
                    trade.analysis_details = analysis['BULLISH'].copy()
                    open_positions.append(trade)
                    available_capital -= quantity * current_price
                    logger.info(f"Opened BULLISH position at {current_price} on {current_date}")
                
                elif has_bearish and not any(p.signal_type == 'BEARISH' for p in open_positions):
                    quantity = self.calculate_position_quantity(current_price, available_capital)
                    
                    # Calculate stop loss and target for short
                    stop_loss = current_price * (1 + self.stop_loss_pct / 100) if self.stop_loss_pct else None
                    target = current_price * (1 - self.target_pct / 100) if self.target_pct else None
                    
                    trade = Trade(current_date, current_price, 'BEARISH', quantity, stop_loss, target)
                    trade.analysis_details = analysis['BEARISH'].copy()
                    open_positions.append(trade)
                    available_capital -= quantity * current_price
                    logger.info(f"Opened BEARISH position at {current_price} on {current_date}")
        
        # Close any remaining open positions at end of backtest
        if open_positions:
            final_date = price_data.index[-1]
            final_price = price_data.iloc[-1]['Close']
            for position in open_positions:
                position.close_trade(final_date, final_price, 'end_of_data')
                result.add_trade(position)
        
        # Calculate metrics
        result.calculate_metrics()
        
        logger.info(f"Backtest completed for {stock_symbol}: {len(result.trades)} trades")
        return result
    
    def run_all(self) -> Dict[str, BacktestResult]:
        """Run backtest for all stocks"""
        for stock_symbol in self.stock_symbols:
            try:
                result = self.run_backtest(stock_symbol)
                self.results[stock_symbol] = result
            except Exception as e:
                logger.error(f"Error backtesting {stock_symbol}: {e}")
        
        return self.results
    
    def generate_report(self, output_file: Optional[str] = None):
        """Generate comprehensive report"""
        print("\n" + "="*80)
        print("BACKTEST SUMMARY - ALL STOCKS")
        print("="*80)
        
        for symbol, result in self.results.items():
            result.print_summary()
        
        # Save to file if requested
        if output_file:
            report_data = {}
            for symbol, result in self.results.items():
                report_data[symbol] = {
                    'metrics': result.metrics,
                    'trades': result.get_trade_summary().to_dict('records')
                }
            
            with open(output_file, 'w') as f:
                json.dump(report_data, f, indent=4, default=str)
            
            logger.info(f"Report saved to {output_file}")
    
    def get_trades_dataframe(self, stock_symbol: Optional[str] = None) -> pd.DataFrame:
        """Get all trades as a DataFrame"""
        if stock_symbol:
            if stock_symbol in self.results:
                return self.results[stock_symbol].get_trade_summary()
            else:
                return pd.DataFrame()
        else:
            all_trades = []
            for symbol, result in self.results.items():
                trades_df = result.get_trade_summary()
                trades_df['stock_symbol'] = symbol
                all_trades.append(trades_df)
            return pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()