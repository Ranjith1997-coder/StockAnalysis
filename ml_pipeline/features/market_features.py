"""
Market-Wide Feature Generator for Stock Movement Prediction.

This module generates features derived from market indices and
market-wide data. These features capture the relationship between
individual stocks and the broader market.

PHASE 1 FEATURES (5 features):
- Nifty_Returns_1d: Nifty 50 daily returns
- Beta_20d: 20-day rolling beta vs Nifty
- Sector_Relative_Strength: Stock vs Nifty performance
- Correlation_Nifty_20d: Correlation with Nifty
- Nifty_Volatility_5d: Nifty 5-day volatility

These features help understand:
- Market regime (bull/bear/sideways)
- Stock sensitivity to market movements
- Relative performance vs benchmark
"""

from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np
from dataclasses import dataclass

from ml_pipeline.config import MarketFeatureConfig


class MarketFeatureGenerator:
    """
    Generator for market-wide features.
    
    This class computes features that relate individual stocks
    to the broader market (indices like Nifty 50, Bank Nifty).
    
    Key Concepts:
    - Beta: Measures stock's sensitivity to market movements
    - Correlation: Measures how closely stock follows market
    - Relative Strength: Measures stock performance vs benchmark
    
    Usage:
        generator = MarketFeatureGenerator(config)
        features_df = generator.generate(stock_df, index_df)
    """
    
    def __init__(self, config: Optional[MarketFeatureConfig] = None):
        """
        Initialize the market feature generator.
        
        Args:
            config: Configuration for market features.
                   If None, uses default configuration.
        """
        self.config = config or MarketFeatureConfig()
        self._index_data: Dict[str, pd.DataFrame] = {}
    
    def set_index_data(
        self, 
        index_symbol: str, 
        index_df: pd.DataFrame
    ) -> None:
        """
        Set index data for feature generation.
        
        Args:
            index_symbol: Symbol for the index (e.g., '^NSEI' for Nifty 50).
            index_df: DataFrame with index OHLCV data.
        """
        self._index_data[index_symbol] = index_df
    
    def generate(
        self, 
        df: pd.DataFrame,
        index_data: Optional[Dict[str, pd.DataFrame]] = None
    ) -> pd.DataFrame:
        """
        Generate all market-based features.
        
        Args:
            df: DataFrame with stock OHLCV columns.
            index_data: Optional dict of index DataFrames.
                       If not provided, uses previously set index data.
                
        Returns:
            DataFrame with all market-based features.
        """
        features = pd.DataFrame(index=df.index)
        
        if not self.config.enabled:
            return features
        
        # Use provided index data or stored data
        if index_data is not None:
            self._index_data = index_data
        
        # Get primary index (first in the list, typically Nifty 50)
        primary_index = self.config.index_symbols[0] if self.config.index_symbols else "^NSEI"
        
        if primary_index not in self._index_data:
            # Return empty features if no index data available
            return features
        
        index_df = self._index_data[primary_index]
        
        # Align index data with stock data
        index_aligned = index_df.reindex(df.index, method='ffill')
        
        # ==========================================
        # INDEX RETURNS
        # ==========================================
        # Nifty daily returns - captures market direction
        features['nifty_returns_1d'] = index_aligned['Close'].pct_change() * 100
        
        # ==========================================
        # BETA
        # ==========================================
        if self.config.include_beta:
            # Beta measures stock's sensitivity to market movements
            # Beta > 1: Stock moves more than market (aggressive)
            # Beta < 1: Stock moves less than market (defensive)
            # Beta < 0: Stock moves opposite to market
            features['beta_20d'] = self._compute_rolling_beta(
                df['Close'], index_aligned['Close'], 20
            )
        
        # ==========================================
        # CORRELATION
        # ==========================================
        if self.config.include_correlation:
            # Correlation measures how closely stock follows market
            # High correlation: Stock moves with market
            # Low correlation: Stock moves independently
            features['correlation_nifty_20d'] = self._compute_rolling_correlation(
                df['Close'], index_aligned['Close'], 20
            )
        
        # ==========================================
        # RELATIVE STRENGTH
        # ==========================================
        if self.config.include_relative_strength:
            # Relative strength compares stock performance to index
            # Positive: Stock outperforming market
            # Negative: Stock underperforming market
            features['sector_relative_strength'] = self._compute_relative_strength(
                df['Close'], index_aligned['Close'], 20
            )
        
        # ==========================================
        # INDEX VOLATILITY
        # ==========================================
        # Nifty volatility - captures market volatility regime
        features['nifty_volatility_5d'] = self._compute_volatility(
            index_aligned['Close'], 5
        )
        
        return features
    
    def _compute_rolling_beta(
        self, 
        stock_close: pd.Series, 
        index_close: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute rolling beta of stock vs index.
        
        Beta measures the stock's sensitivity to market movements.
        
        Formula:
            Beta = Cov(Stock, Market) / Var(Market)
        
        Interpretation:
            Beta = 1: Stock moves with market
            Beta > 1: Stock is more volatile than market
            Beta < 1: Stock is less volatile than market
            Beta < 0: Stock moves opposite to market
        
        Args:
            stock_close: Stock close price series.
            index_close: Index close price series.
            period: Rolling window for beta calculation.
            
        Returns:
            Rolling beta series.
        """
        # Calculate returns
        stock_returns = stock_close.pct_change()
        index_returns = index_close.pct_change()
        
        # Calculate rolling covariance and variance
        def rolling_beta(stock_ret, index_ret, window):
            """Calculate beta for a window."""
            if len(stock_ret) < window or len(index_ret) < window:
                return np.nan
            
            covariance = np.cov(stock_ret, index_ret)[0, 1]
            variance = np.var(index_ret)
            
            if variance == 0:
                return np.nan
            return covariance / variance
        
        # Apply rolling calculation
        beta = pd.Series(index=stock_close.index, dtype=float)
        
        for i in range(period, len(stock_close)):
            stock_window = stock_returns.iloc[i-period+1:i+1]
            index_window = index_returns.iloc[i-period+1:i+1]
            
            # Remove NaN values
            valid_mask = ~(stock_window.isna() | index_window.isna())
            stock_valid = stock_window[valid_mask]
            index_valid = index_window[valid_mask]
            
            if len(stock_valid) >= period // 2:  # Require at least half the data
                beta.iloc[i] = rolling_beta(stock_valid.values, index_valid.values, len(stock_valid))
        
        return beta
    
    def _compute_rolling_correlation(
        self, 
        stock_close: pd.Series, 
        index_close: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute rolling correlation between stock and index.
        
        Correlation measures how closely the stock follows the market.
        
        Formula:
            Correlation = Cov(Stock, Market) / (Std(Stock) * Std(Market))
        
        Interpretation:
            Correlation = 1: Perfect positive correlation
            Correlation = 0: No correlation
            Correlation = -1: Perfect negative correlation
        
        Args:
            stock_close: Stock close price series.
            index_close: Index close price series.
            period: Rolling window for correlation calculation.
            
        Returns:
            Rolling correlation series.
        """
        # Calculate returns
        stock_returns = stock_close.pct_change()
        index_returns = index_close.pct_change()
        
        # Use pandas rolling correlation
        correlation = stock_returns.rolling(window=period).corr(index_returns)
        
        return correlation
    
    def _compute_relative_strength(
        self, 
        stock_close: pd.Series, 
        index_close: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute relative strength of stock vs index.
        
        Relative strength compares the stock's performance to the index.
        
        Formula:
            RS = (Stock_Return - Index_Return) over period
        
        Interpretation:
            RS > 0: Stock outperforming market
            RS < 0: Stock underperforming market
        
        Args:
            stock_close: Stock close price series.
            index_close: Index close price series.
            period: Period for calculating returns.
            
        Returns:
            Relative strength series.
        """
        # Calculate returns over period
        stock_return = stock_close.pct_change(period) * 100
        index_return = index_close.pct_change(period) * 100
        
        # Relative strength is the difference
        relative_strength = stock_return - index_return
        
        return relative_strength
    
    def _compute_volatility(
        self, 
        close: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute rolling volatility (standard deviation of returns).
        
        Volatility measures the dispersion of returns.
        Higher volatility indicates more uncertainty/risk.
        
        Args:
            close: Close price series.
            period: Rolling window for volatility calculation.
            
        Returns:
            Volatility series (in percentage).
        """
        returns = close.pct_change()
        return returns.rolling(window=period).std() * 100


def generate_market_features(
    df: pd.DataFrame,
    index_data: Optional[Dict[str, pd.DataFrame]] = None,
    config: Optional[MarketFeatureConfig] = None
) -> pd.DataFrame:
    """
    Convenience function to generate market features.
    
    Args:
        df: DataFrame with stock OHLCV columns.
        index_data: Dict of index DataFrames.
        config: Optional configuration.
        
    Returns:
        DataFrame with market-based features.
    """
    generator = MarketFeatureGenerator(config)
    return generator.generate(df, index_data)


def fetch_index_data(
    index_symbols: List[str],
    start_date: str,
    end_date: str
) -> Dict[str, pd.DataFrame]:
    """
    Fetch index data from yfinance.
    
    This is a convenience function to fetch index data for
    feature generation.
    
    Args:
        index_symbols: List of index symbols (e.g., ['^NSEI', '^NSEBANK']).
        start_date: Start date in 'YYYY-MM-DD' format.
        end_date: End date in 'YYYY-MM-DD' format.
        
    Returns:
        Dictionary mapping index symbols to their DataFrames.
    """
    import yfinance as yf
    
    index_data = {}
    
    for symbol in index_symbols:
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(start=start_date, end=end_date)
            
            if not df.empty:
                # Standardize column names
                df.columns = [col.capitalize() for col in df.columns]
                index_data[symbol] = df
        except Exception as e:
            print(f"Warning: Could not fetch data for {symbol}: {e}")
    
    return index_data
