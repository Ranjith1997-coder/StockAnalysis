"""
Technical Indicator Feature Generator for Stock Movement Prediction.

This module generates technical analysis indicators from OHLCV data.
These indicators are widely used in trading to identify trends, momentum,
volatility, and potential reversal points.

PHASE 1 FEATURES (20 features):
- RSI (5, 10, 14): Relative Strength Index - momentum oscillator
- MACD, Signal, Histogram: Moving Average Convergence Divergence
- EMA (9, 21, 50): Exponential Moving Averages
- Bollinger Bands (Upper, Lower, Percent): Volatility bands
- ATR, ATR Percent: Average True Range - volatility measure
- Stochastic K, D: Momentum oscillator
- ADX, +DI, -DI: Average Directional Index - trend strength
- Supertrend: Trend following indicator
- Williams %R: Overbought/oversold indicator
"""

from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np
from dataclasses import dataclass

from ml_pipeline.config import TechnicalFeatureConfig


@dataclass
class TechnicalFeatures:
    """Container for technical indicator features."""
    # RSI features
    rsi_5: pd.Series = None
    rsi_10: pd.Series = None
    rsi_14: pd.Series = None
    
    # MACD features
    macd: pd.Series = None
    macd_signal: pd.Series = None
    macd_histogram: pd.Series = None
    
    # EMA features
    ema_9: pd.Series = None
    ema_21: pd.Series = None
    ema_50: pd.Series = None
    
    # Bollinger Bands
    bollinger_upper: pd.Series = None
    bollinger_lower: pd.Series = None
    bollinger_percent: pd.Series = None
    
    # ATR
    atr: pd.Series = None
    atr_percent: pd.Series = None
    
    # Stochastic
    stochastic_k: pd.Series = None
    stochastic_d: pd.Series = None
    
    # ADX
    adx: pd.Series = None
    adx_positive: pd.Series = None
    adx_negative: pd.Series = None
    
    # Supertrend
    supertrend: pd.Series = None
    
    # Williams %R
    williams_r: pd.Series = None


class TechnicalFeatureGenerator:
    """
    Generator for technical indicator features.
    
    This class computes various technical indicators from OHLCV data.
    All indicators are computed using only historical data (no look-ahead bias).
    
    Usage:
        generator = TechnicalFeatureGenerator(config)
        features_df = generator.generate(ohlcv_df)
    """
    
    def __init__(self, config: Optional[TechnicalFeatureConfig] = None):
        """
        Initialize the technical feature generator.
        
        Args:
            config: Configuration for technical indicators.
                   If None, uses default configuration.
        """
        self.config = config or TechnicalFeatureConfig()
    
    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate all technical indicator features.
        
        Args:
            df: DataFrame with OHLCV columns (Open, High, Low, Close, Volume).
                Must have a DatetimeIndex.
                
        Returns:
            DataFrame with all technical indicator features.
            Each row corresponds to the same index as the input.
        """
        features = pd.DataFrame(index=df.index)
        
        # Generate each category of features
        if self.config.enabled:
            # RSI features
            for period in self.config.rsi_periods:
                features[f'rsi_{period}'] = self._compute_rsi(df['Close'], period)
            
            # MACD features
            macd, signal, hist = self._compute_macd(
                df['Close'],
                self.config.macd_params[0],  # fast
                self.config.macd_params[1],  # slow
                self.config.macd_params[2],  # signal
            )
            features['macd'] = macd
            features['macd_signal'] = signal
            features['macd_histogram'] = hist
            
            # EMA features
            for period in self.config.ema_periods:
                features[f'ema_{period}'] = self._compute_ema(df['Close'], period)
            
            # Bollinger Bands
            upper, lower, percent = self._compute_bollinger_bands(
                df['Close'],
                self.config.bollinger_params[0],  # window
                self.config.bollinger_params[1],  # num_std
            )
            features['bollinger_upper'] = upper
            features['bollinger_lower'] = lower
            features['bollinger_percent'] = percent
            
            # ATR
            features['atr'] = self._compute_atr(
                df['High'], df['Low'], df['Close'],
                self.config.atr_period
            )
            features['atr_percent'] = features['atr'] / df['Close'] * 100
            
            # Stochastic
            stoch_k, stoch_d = self._compute_stochastic(
                df['High'], df['Low'], df['Close'],
                self.config.stochastic_params[0],  # k_period
                self.config.stochastic_params[1],  # d_period
            )
            features['stochastic_k'] = stoch_k
            features['stochastic_d'] = stoch_d
            
            # ADX
            adx, plus_di, minus_di = self._compute_adx(
                df['High'], df['Low'], df['Close'],
                self.config.adx_period
            )
            features['adx'] = adx
            features['adx_positive'] = plus_di
            features['adx_negative'] = minus_di
            
            # Supertrend
            features['supertrend'] = self._compute_supertrend(
                df['High'], df['Low'], df['Close'],
                self.config.supertrend_params[0],  # period
                self.config.supertrend_params[1],  # multiplier
            )
            
            # Williams %R
            features['williams_r'] = self._compute_williams_r(
                df['High'], df['Low'], df['Close'],
                self.config.williams_r_period
            )
        
        return features
    
    def _compute_rsi(self, close: pd.Series, period: int) -> pd.Series:
        """
        Compute Relative Strength Index (RSI).
        
        RSI measures the speed and magnitude of price movements.
        Values above 70 indicate overbought, below 30 indicate oversold.
        
        Formula:
            RSI = 100 - (100 / (1 + RS))
            RS = Average Gain / Average Loss
        
        Args:
            close: Close price series.
            period: RSI period (typically 14).
            
        Returns:
            RSI values (0-100).
        """
        delta = close.diff()
        
        # Separate gains and losses
        gains = delta.where(delta > 0, 0)
        losses = -delta.where(delta < 0, 0)
        
        # Use Wilder's smoothing (EMA with adjust=False)
        avg_gain = gains.ewm(span=period, adjust=False).mean()
        avg_loss = losses.ewm(span=period, adjust=False).mean()
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def _compute_macd(
        self, 
        close: pd.Series, 
        fast: int, 
        slow: int, 
        signal: int
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Compute MACD (Moving Average Convergence Divergence).
        
        MACD is a trend-following momentum indicator.
        - MACD Line: Fast EMA - Slow EMA
        - Signal Line: EMA of MACD Line
        - Histogram: MACD - Signal
        
        Crossovers between MACD and Signal indicate potential buy/sell signals.
        
        Args:
            close: Close price series.
            fast: Fast EMA period (typically 12).
            slow: Slow EMA period (typically 26).
            signal: Signal line period (typically 9).
            
        Returns:
            Tuple of (MACD line, Signal line, Histogram).
        """
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        
        return macd_line, signal_line, histogram
    
    def _compute_ema(self, close: pd.Series, period: int) -> pd.Series:
        """
        Compute Exponential Moving Average (EMA).
        
        EMA gives more weight to recent prices, making it more responsive
        to new information compared to Simple Moving Average.
        
        Formula:
            EMA = α * Price + (1 - α) * Previous_EMA
            α = 2 / (period + 1)
        
        Args:
            close: Close price series.
            period: EMA period.
            
        Returns:
            EMA values.
        """
        return close.ewm(span=period, adjust=False).mean()
    
    def _compute_bollinger_bands(
        self, 
        close: pd.Series, 
        window: int, 
        num_std: float
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Compute Bollinger Bands.
        
        Bollinger Bands consist of:
        - Middle Band: SMA of price
        - Upper Band: Middle + (StdDev * multiplier)
        - Lower Band: Middle - (StdDev * multiplier)
        
        They expand during high volatility and contract during low volatility.
        Price near upper band suggests overbought, near lower suggests oversold.
        
        Args:
            close: Close price series.
            window: Moving average window (typically 20).
            num_std: Standard deviation multiplier (typically 2).
            
        Returns:
            Tuple of (Upper Band, Lower Band, %B).
            %B shows where price is relative to the bands (0-1 scale).
        """
        middle = close.rolling(window=window).mean()
        std = close.rolling(window=window).std()
        
        upper = middle + (std * num_std)
        lower = middle - (std * num_std)
        
        # %B: Position within bands (0 = lower band, 1 = upper band)
        percent_b = (close - lower) / (upper - lower)
        
        return upper, lower, percent_b
    
    def _compute_atr(
        self, 
        high: pd.Series, 
        low: pd.Series, 
        close: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute Average True Range (ATR).
        
        ATR measures market volatility by decomposing the entire range
        of an asset price for that period.
        
        True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
        ATR = Moving average of True Range
        
        Higher ATR indicates higher volatility.
        
        Args:
            high: High price series.
            low: Low price series.
            close: Close price series.
            period: ATR period (typically 14).
            
        Returns:
            ATR values.
        """
        # Calculate True Range components
        high_low = high - low
        high_close = np.abs(high - close.shift(1))
        low_close = np.abs(low - close.shift(1))
        
        # True Range is the maximum of the three
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        
        # ATR is the moving average of True Range
        atr = true_range.rolling(window=period).mean()
        
        return atr
    
    def _compute_stochastic(
        self, 
        high: pd.Series, 
        low: pd.Series, 
        close: pd.Series,
        k_period: int,
        d_period: int
    ) -> Tuple[pd.Series, pd.Series]:
        """
        Compute Stochastic Oscillator.
        
        The Stochastic Oscillator compares a security's closing price
        to its price range over a given time period.
        
        %K = (Close - Lowest Low) / (Highest High - Lowest Low) * 100
        %D = Moving average of %K
        
        Values above 80 indicate overbought, below 20 indicate oversold.
        
        Args:
            high: High price series.
            low: Low price series.
            close: Close price series.
            k_period: %K period (typically 5 or 14).
            d_period: %D period (typically 3 or 5).
            
        Returns:
            Tuple of (%K, %D).
        """
        lowest_low = low.rolling(window=k_period).min()
        highest_high = high.rolling(window=k_period).max()
        
        stoch_k = (close - lowest_low) / (highest_high - lowest_low) * 100
        stoch_d = stoch_k.rolling(window=d_period).mean()
        
        return stoch_k, stoch_d
    
    def _compute_adx(
        self, 
        high: pd.Series, 
        low: pd.Series, 
        close: pd.Series,
        period: int
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """
        Compute Average Directional Index (ADX) and Directional Indicators.
        
        ADX measures the strength of a trend (not direction).
        - ADX > 25: Strong trend
        - ADX < 20: Weak or no trend
        
        +DI measures upward movement strength
        -DI measures downward movement strength
        
        When +DI > -DI, the trend is bullish.
        When -DI > +DI, the trend is bearish.
        
        Args:
            high: High price series.
            low: Low price series.
            close: Close price series.
            period: ADX period (typically 14).
            
        Returns:
            Tuple of (ADX, +DI, -DI).
        """
        # Calculate True Range
        high_low = high - low
        high_close = np.abs(high - close.shift(1))
        low_close = np.abs(low - close.shift(1))
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        
        # Calculate Directional Movement
        plus_dm = high - high.shift(1)
        minus_dm = low.shift(1) - low
        
        # Zero out negative movements and conflicting directions
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
        
        # Smooth the values
        atr = true_range.rolling(window=period).mean()
        plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
        minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)
        
        # Calculate ADX
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
        adx = dx.rolling(window=period).mean()
        
        return adx, plus_di, minus_di
    
    def _compute_supertrend(
        self, 
        high: pd.Series, 
        low: pd.Series, 
        close: pd.Series,
        period: int,
        multiplier: float
    ) -> pd.Series:
        """
        Compute Supertrend indicator.
        
        Supertrend is a trend-following indicator that shows the current
        trend direction and provides support/resistance levels.
        
        It's based on ATR and shifts when price crosses the band.
        
        Output:
        - Returns a value representing the trend direction:
          - 1: Uptrend (price above supertrend line)
          - -1: Downtrend (price below supertrend line)
        
        Args:
            high: High price series.
            low: Low price series.
            close: Close price series.
            period: ATR period (typically 10 or 14).
            multiplier: ATR multiplier (typically 2 or 3).
            
        Returns:
            Supertrend signal: 1 for uptrend, -1 for downtrend.
        """
        # Calculate ATR
        atr = self._compute_atr(high, low, close, period)
        
        # Calculate Basic Upper and Lower Bands
        hl_avg = (high + low) / 2
        upper_band = (hl_avg + (multiplier * atr)).to_numpy()
        lower_band = (hl_avg - (multiplier * atr)).to_numpy()
        close_vals = close.to_numpy()
        
        # Initialize arrays for final bands and supertrend
        final_upper = np.full(len(close), np.nan)
        final_lower = np.full(len(close), np.nan)
        supertrend = np.full(len(close), np.nan)
        
        # First valid value (after ATR period)
        first_valid = period
        
        # Initialize first values
        if first_valid < len(close):
            final_upper[first_valid] = upper_band[first_valid]
            final_lower[first_valid] = lower_band[first_valid]
            supertrend[first_valid] = upper_band[first_valid]  # Start with upper band
        
        # Determine Supertrend direction using numpy for efficiency
        for i in range(first_valid + 1, len(close)):
            # Lower band can only rise (or stay same)
            if lower_band[i] > final_lower[i-1] or close_vals[i-1] < final_lower[i-1]:
                final_lower[i] = lower_band[i]
            else:
                final_lower[i] = final_lower[i-1]
            
            # Upper band can only fall (or stay same)
            if upper_band[i] < final_upper[i-1] or close_vals[i-1] > final_upper[i-1]:
                final_upper[i] = upper_band[i]
            else:
                final_upper[i] = final_upper[i-1]
            
            # Determine trend
            if supertrend[i-1] == final_upper[i-1]:
                # Previous trend was downtrend (using upper band)
                if close_vals[i] <= final_upper[i]:
                    supertrend[i] = final_upper[i]  # Continue downtrend
                else:
                    supertrend[i] = final_lower[i]  # Switch to uptrend
            else:
                # Previous trend was uptrend (using lower band)
                if close_vals[i] >= final_lower[i]:
                    supertrend[i] = final_lower[i]  # Continue uptrend
                else:
                    supertrend[i] = final_upper[i]  # Switch to downtrend
        
        # Convert to trend signal: 1 for uptrend, -1 for downtrend
        # This is more useful for ML than the actual supertrend price level
        trend_signal = np.where(close_vals > supertrend, 1, -1)
        
        # Handle NaN values (set to 0 for initial periods)
        trend_signal = np.where(np.isnan(supertrend), 0, trend_signal)
        
        return pd.Series(trend_signal, index=close.index, name='supertrend')
    
    def _compute_williams_r(
        self, 
        high: pd.Series, 
        low: pd.Series, 
        close: pd.Series,
        period: int
    ) -> pd.Series:
        """
        Compute Williams %R.
        
        Williams %R is a momentum indicator that measures overbought
        and oversold levels.
        
        %R = (Highest High - Close) / (Highest High - Lowest Low) * -100
        
        Values range from -100 to 0:
        - Above -20: Overbought
        - Below -80: Oversold
        
        Args:
            high: High price series.
            low: Low price series.
            close: Close price series.
            period: Lookback period (typically 14).
            
        Returns:
            Williams %R values (-100 to 0).
        """
        highest_high = high.rolling(window=period).max()
        lowest_low = low.rolling(window=period).min()
        
        williams_r = (highest_high - close) / (highest_high - lowest_low) * -100
        
        return williams_r


def generate_technical_features(
    df: pd.DataFrame,
    config: Optional[TechnicalFeatureConfig] = None
) -> pd.DataFrame:
    """
    Convenience function to generate technical features.
    
    Args:
        df: DataFrame with OHLCV columns.
        config: Optional configuration.
        
    Returns:
        DataFrame with technical indicator features.
    """
    generator = TechnicalFeatureGenerator(config)
    return generator.generate(df)
