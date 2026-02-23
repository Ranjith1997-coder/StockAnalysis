"""
Price-Based Feature Generator for Stock Movement Prediction.

This module generates features derived directly from price data (OHLC).
These features capture price action patterns, momentum, and volatility
without using complex technical indicators.

PHASE 1 FEATURES (15 features):
- Returns (1d, 5d, 10d): Historical returns at different timeframes
- Volatility (10d): Rolling standard deviation of returns
- High_Low_Pct: Intraday range as percentage
- Close_Open_Pct: Intraday momentum
- Gap_Size: Opening gap percentage
- Price_vs_EMA (21, 50): Distance from moving averages
- Distance_From_High_20: % from 20-day high
- Distance_From_Low_20: % from 20-day low
- Price_Position_20d: Position in 20-day range (0-100)
- Momentum_5: 5-day price momentum
- EMA_Cross_9_21: EMA crossover signal
- Consecutive_Days: Consecutive up/down days
"""

from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np
from dataclasses import dataclass

from ml_pipeline.config import PriceFeatureConfig


class PriceFeatureGenerator:
    """
    Generator for price-based features.
    
    This class computes features directly from OHLC price data.
    These features capture raw price action without complex transformations.
    
    Usage:
        generator = PriceFeatureGenerator(config)
        features_df = generator.generate(ohlcv_df)
    """
    
    def __init__(self, config: Optional[PriceFeatureConfig] = None):
        """
        Initialize the price feature generator.
        
        Args:
            config: Configuration for price features.
                   If None, uses default configuration.
        """
        self.config = config or PriceFeatureConfig()
    
    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate all price-based features.
        
        Args:
            df: DataFrame with OHLCV columns (Open, High, Low, Close, Volume).
                Must have a DatetimeIndex.
                
        Returns:
            DataFrame with all price-based features.
        """
        features = pd.DataFrame(index=df.index)
        
        if not self.config.enabled:
            return features
        
        # ==========================================
        # RETURNS FEATURES
        # ==========================================
        # Returns at different timeframes capture momentum at various scales
        for period in self.config.return_periods:
            features[f'returns_{period}d'] = self._compute_returns(df['Close'], period)
        
        # ==========================================
        # VOLATILITY FEATURES
        # ==========================================
        # Rolling volatility measures recent price variability
        for period in self.config.volatility_periods:
            features[f'volatility_{period}d'] = self._compute_volatility(df['Close'], period)
        
        # ==========================================
        # INTRADAY PRICE FEATURES
        # ==========================================
        # High-Low percentage: measures intraday range (volatility proxy)
        features['high_low_pct'] = self._compute_high_low_pct(df['High'], df['Low'], df['Close'])
        
        # Close-Open percentage: measures intraday momentum
        features['close_open_pct'] = self._compute_close_open_pct(df['Close'], df['Open'])
        
        # ==========================================
        # GAP FEATURES
        # ==========================================
        if self.config.include_gap_features:
            # Gap size: opening gap from previous close
            features['gap_size'] = self._compute_gap_size(df['Open'], df['Close'])
            
            # Gap direction: binary indicator
            features['gap_up'] = (features['gap_size'] > 0).astype(int)
            features['gap_down'] = (features['gap_size'] < 0).astype(int)
        
        # ==========================================
        # PRICE POSITION FEATURES
        # ==========================================
        if self.config.include_price_position:
            # Distance from moving averages
            features['price_vs_ema_21'] = self._compute_price_vs_ema(df['Close'], 21)
            features['price_vs_ema_50'] = self._compute_price_vs_ema(df['Close'], 50)
            
            # Distance from recent highs/lows
            features['distance_from_high_20'] = self._compute_distance_from_high(df['Close'], 20)
            features['distance_from_low_20'] = self._compute_distance_from_low(df['Close'], 20)
            
            # Position in N-day range (0 = at low, 100 = at high)
            features['price_position_20d'] = self._compute_price_position(df['Close'], 20)
        
        # ==========================================
        # MOMENTUM FEATURES
        # ==========================================
        # Price momentum (rate of change)
        features['momentum_5'] = self._compute_momentum(df['Close'], 5)
        
        # ==========================================
        # TREND FEATURES
        # ==========================================
        if self.config.include_swing_detection:
            # EMA crossover signal
            features['ema_cross_9_21'] = self._compute_ema_cross(df['Close'], 9, 21)
            
            # Consecutive up/down days
            features['consecutive_days'] = self._compute_consecutive_days(df['Close'])
            
            # Higher high / Lower low detection
            features['higher_high'] = self._detect_higher_high(df['High'], 5)
            features['lower_low'] = self._detect_lower_low(df['Low'], 5)
        
        return features
    
    def _compute_returns(self, close: pd.Series, period: int) -> pd.Series:
        """
        Compute percentage returns over a given period.
        
        Returns measure the percentage change in price over N days.
        Positive returns indicate upward momentum, negative indicates downward.
        
        Formula:
            Returns = (Close_t - Close_{t-period}) / Close_{t-period} * 100
        
        Args:
            close: Close price series.
            period: Number of periods to calculate returns over.
            
        Returns:
            Percentage returns series.
        """
        return close.pct_change(period) * 100
    
    def _compute_volatility(self, close: pd.Series, period: int) -> pd.Series:
        """
        Compute rolling volatility (standard deviation of returns).
        
        Volatility measures the dispersion of returns over a period.
        Higher volatility indicates more uncertainty/risk.
        
        Formula:
            Volatility = StdDev(daily_returns, period)
        
        Args:
            close: Close price series.
            period: Rolling window for volatility calculation.
            
        Returns:
            Volatility series (in percentage).
        """
        daily_returns = close.pct_change()
        return daily_returns.rolling(window=period).std() * 100
    
    def _compute_high_low_pct(
        self, 
        high: pd.Series, 
        low: pd.Series, 
        close: pd.Series
    ) -> pd.Series:
        """
        Compute intraday range as percentage of close.
        
        This measures the volatility within a single trading day.
        Larger values indicate more intraday price movement.
        
        Formula:
            High_Low_Pct = (High - Low) / Close * 100
        
        Args:
            high: High price series.
            low: Low price series.
            close: Close price series.
            
        Returns:
            Intraday range percentage series.
        """
        return (high - low) / close * 100
    
    def _compute_close_open_pct(
        self, 
        close: pd.Series, 
        open_: pd.Series
    ) -> pd.Series:
        """
        Compute intraday momentum (close vs open).
        
        This measures the price movement during the trading session.
        Positive values indicate the close was above the open (bullish).
        Negative values indicate the close was below the open (bearish).
        
        Formula:
            Close_Open_Pct = (Close - Open) / Open * 100
        
        Args:
            close: Close price series.
            open_: Open price series.
            
        Returns:
            Intraday momentum percentage series.
        """
        return (close - open_) / open_ * 100
    
    def _compute_gap_size(self, open_: pd.Series, close: pd.Series) -> pd.Series:
        """
        Compute opening gap size from previous close.
        
        Gap measures the difference between today's open and yesterday's close.
        Large gaps can indicate overnight news or sentiment shifts.
        
        Formula:
            Gap = (Open_t - Close_{t-1}) / Close_{t-1} * 100
        
        Args:
            open_: Open price series.
            close: Close price series.
            
        Returns:
            Gap size percentage series.
        """
        return (open_ - close.shift(1)) / close.shift(1) * 100
    
    def _compute_price_vs_ema(
        self, 
        close: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute percentage distance from EMA.
        
        This measures how far the current price is from its moving average.
        Large positive values indicate overbought conditions.
        Large negative values indicate oversold conditions.
        
        Formula:
            Price_vs_EMA = (Close - EMA) / EMA * 100
        
        Args:
            close: Close price series.
            period: EMA period.
            
        Returns:
            Percentage distance from EMA series.
        """
        ema = close.ewm(span=period, adjust=False).mean()
        return (close - ema) / ema * 100
    
    def _compute_distance_from_high(
        self, 
        close: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute percentage distance from period high.
        
        This measures how far the current price is from its recent high.
        Values near 0 indicate price is at or near its high (potential breakout).
        Larger values indicate price has pulled back from highs.
        
        Formula:
            Distance = (High - Close) / High * 100
        
        Args:
            close: Close price series.
            period: Lookback period.
            
        Returns:
            Percentage distance from high series.
        """
        period_high = close.rolling(window=period).max()
        return (period_high - close) / period_high * 100
    
    def _compute_distance_from_low(
        self, 
        close: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute percentage distance from period low.
        
        This measures how far the current price is from its recent low.
        Values near 0 indicate price is at or near its low (potential bounce).
        Larger values indicate price has rallied from lows.
        
        Formula:
            Distance = (Close - Low) / Low * 100
        
        Args:
            close: Close price series.
            period: Lookback period.
            
        Returns:
            Percentage distance from low series.
        """
        period_low = close.rolling(window=period).min()
        return (close - period_low) / period_low * 100
    
    def _compute_price_position(
        self, 
        close: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute price position within the period range.
        
        This normalizes the price position between 0 and 100.
        0 = price is at the period low
        100 = price is at the period high
        50 = price is in the middle of the range
        
        Formula:
            Position = (Close - Low) / (High - Low) * 100
        
        Args:
            close: Close price series.
            period: Lookback period.
            
        Returns:
            Price position series (0-100).
        """
        period_high = close.rolling(window=period).max()
        period_low = close.rolling(window=period).min()
        return (close - period_low) / (period_high - period_low) * 100
    
    def _compute_momentum(self, close: pd.Series, period: int) -> pd.Series:
        """
        Compute price momentum (rate of change).
        
        Momentum measures the speed of price changes.
        Positive momentum indicates upward price movement.
        Negative momentum indicates downward price movement.
        
        Formula:
            Momentum = Close - Close_{t-period}
        
        Args:
            close: Close price series.
            period: Momentum period.
            
        Returns:
            Momentum series.
        """
        return close - close.shift(period)
    
    def _compute_ema_cross(
        self, 
        close: pd.Series, 
        fast: int, 
        slow: int
    ) -> pd.Series:
        """
        Compute EMA crossover signal.
        
        This detects when a fast EMA crosses above/below a slow EMA.
        1 = Bullish crossover (fast crosses above slow)
        -1 = Bearish crossover (fast crosses below slow)
        0 = No crossover
        
        Crossovers are used to identify trend changes.
        
        Args:
            close: Close price series.
            fast: Fast EMA period.
            slow: Slow EMA period.
            
        Returns:
            Crossover signal series (-1, 0, 1).
        """
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        
        # Current and previous positions
        current_diff = ema_fast - ema_slow
        prev_diff = current_diff.shift(1)
        
        # Detect crossovers
        bullish_cross = (prev_diff <= 0) & (current_diff > 0)
        bearish_cross = (prev_diff >= 0) & (current_diff < 0)
        
        signal = pd.Series(0, index=close.index)
        signal[bullish_cross] = 1
        signal[bearish_cross] = -1
        
        return signal
    
    def _compute_consecutive_days(self, close: pd.Series) -> pd.Series:
        """
        Compute consecutive up/down days.
        
        This counts how many consecutive days the price has moved
        in the same direction.
        
        Positive values = consecutive up days
        Negative values = consecutive down days
        
        Long streaks may indicate exhaustion and potential reversal.
        
        Args:
            close: Close price series.
            
        Returns:
            Consecutive days series.
        """
        # Calculate daily direction
        direction = np.sign(close.diff())
        
        # Initialize result
        consecutive = pd.Series(0, index=close.index)
        
        for i in range(1, len(close)):
            if direction.iloc[i] == direction.iloc[i-1]:
                # Same direction, increment/decrement
                consecutive.iloc[i] = consecutive.iloc[i-1] + direction.iloc[i]
            else:
                # Direction changed, reset
                consecutive.iloc[i] = direction.iloc[i]
        
        return consecutive
    
    def _detect_higher_high(self, high: pd.Series, period: int) -> pd.Series:
        """
        Detect higher high formation.
        
        A higher high occurs when the current high is above
        the previous high within the lookback period.
        
        This is a bullish signal indicating upward momentum.
        
        Args:
            high: High price series.
            period: Lookback period.
            
        Returns:
            Binary series (1 = higher high detected, 0 = no).
        """
        prev_high = high.shift(1).rolling(window=period).max()
        return (high > prev_high).astype(int)
    
    def _detect_lower_low(self, low: pd.Series, period: int) -> pd.Series:
        """
        Detect lower low formation.
        
        A lower low occurs when the current low is below
        the previous low within the lookback period.
        
        This is a bearish signal indicating downward momentum.
        
        Args:
            low: Low price series.
            period: Lookback period.
            
        Returns:
            Binary series (1 = lower low detected, 0 = no).
        """
        prev_low = low.shift(1).rolling(window=period).min()
        return (low < prev_low).astype(int)


def generate_price_features(
    df: pd.DataFrame,
    config: Optional[PriceFeatureConfig] = None
) -> pd.DataFrame:
    """
    Convenience function to generate price features.
    
    Args:
        df: DataFrame with OHLCV columns.
        config: Optional configuration.
        
    Returns:
        DataFrame with price-based features.
    """
    generator = PriceFeatureGenerator(config)
    return generator.generate(df)
