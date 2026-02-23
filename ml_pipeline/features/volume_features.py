"""
Volume-Based Feature Generator for Stock Movement Prediction.

This module generates features derived from volume data.
Volume is a key indicator of market participation and can confirm
or diverge from price movements.

PHASE 1 FEATURES (10 features):
- Volume_Ratio (10d, 20d): Current volume vs average volume
- OBV: On-Balance Volume
- OBV_EMA_5: OBV moving average
- Accumulation_Distribution: A/D line
- Chaikin_Money_Flow: CMF indicator
- Volume_Spike: Volume > 2x average
- Volume_Dry_Up: Volume < 0.5x average
- Intraday_Volume_Intensity: Volume relative to range
- Volume_Trend: Volume trend direction
"""

from typing import Dict, List, Optional, Tuple
import pandas as pd
import numpy as np
from dataclasses import dataclass

from ml_pipeline.config import VolumeFeatureConfig


class VolumeFeatureGenerator:
    """
    Generator for volume-based features.
    
    This class computes features from volume data to understand
    market participation and validate price movements.
    
    Key Concepts:
    - Volume confirms price: Price moves on high volume are more significant
    - Volume divergence: Price moves on low volume may be weak/false
    - Accumulation/Distribution: Smart money buying/selling patterns
    
    Usage:
        generator = VolumeFeatureGenerator(config)
        features_df = generator.generate(ohlcv_df)
    """
    
    def __init__(self, config: Optional[VolumeFeatureConfig] = None):
        """
        Initialize the volume feature generator.
        
        Args:
            config: Configuration for volume features.
                   If None, uses default configuration.
        """
        self.config = config or VolumeFeatureConfig()
    
    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate all volume-based features.
        
        Args:
            df: DataFrame with OHLCV columns (Open, High, Low, Close, Volume).
                Must have a DatetimeIndex.
                
        Returns:
            DataFrame with all volume-based features.
        """
        features = pd.DataFrame(index=df.index)
        
        if not self.config.enabled:
            return features
        
        # ==========================================
        # VOLUME RATIO FEATURES
        # ==========================================
        # Volume ratio compares current volume to historical average
        # High ratio = unusual activity, Low ratio = lack of interest
        for period in self.config.volume_ratio_periods:
            features[f'volume_ratio_{period}d'] = self._compute_volume_ratio(
                df['Volume'], period
            )
        
        # ==========================================
        # ON-BALANCE VOLUME (OBV)
        # ==========================================
        if self.config.include_obv:
            # OBV is a cumulative indicator that adds volume on up days
            # and subtracts volume on down days
            features['obv'] = self._compute_obv(df['Close'], df['Volume'])
            
            # OBV EMA shows the trend of OBV
            features['obv_ema_5'] = features['obv'].ewm(span=5, adjust=False).mean()
            
            # OBV trend direction
            features['obv_trend'] = self._compute_obv_trend(features['obv'], 5)
        
        # ==========================================
        # ACCUMULATION/DISTRIBUTION LINE
        # ==========================================
        if self.config.include_accumulation_distribution:
            # A/D line uses price position within the day's range
            # to determine if money is flowing in or out
            features['accumulation_distribution'] = self._compute_accumulation_distribution(
                df['High'], df['Low'], df['Close'], df['Volume']
            )
        
        # ==========================================
        # CHAIKIN MONEY FLOW (CMF)
        # ==========================================
        if self.config.include_chaikin_money_flow:
            # CMF measures buying and selling pressure over a period
            # Values range from -1 to +1
            # Positive = buying pressure, Negative = selling pressure
            features['chaikin_money_flow'] = self._compute_cmf(
                df['High'], df['Low'], df['Close'], df['Volume'], 20
            )
        
        # ==========================================
        # VOLUME SPIKE / DRY UP
        # ==========================================
        if self.config.include_volume_spikes:
            # Volume spike: Volume significantly above average
            # Often indicates institutional activity or news
            features['volume_spike'] = self._detect_volume_spike(
                df['Volume'], 20, threshold=2.0
            )
            
            # Volume dry up: Volume significantly below average
            # Often indicates lack of interest or consolidation
            features['volume_dry_up'] = self._detect_volume_dry_up(
                df['Volume'], 20, threshold=0.5
            )
        
        # ==========================================
        # INTRADAY VOLUME INTENSITY
        # ==========================================
        # Volume relative to price range
        # High intensity = lots of volume for small price movement (absorption)
        features['intraday_volume_intensity'] = self._compute_volume_intensity(
            df['High'], df['Low'], df['Close'], df['Volume']
        )
        
        # ==========================================
        # VOLUME TREND
        # ==========================================
        # Direction of volume change
        features['volume_trend'] = self._compute_volume_trend(df['Volume'], 5)
        
        return features
    
    def _compute_volume_ratio(
        self, 
        volume: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute volume ratio relative to moving average.
        
        Volume ratio compares current volume to its historical average.
        - Ratio > 1: Above average volume (high interest)
        - Ratio < 1: Below average volume (low interest)
        - Ratio > 2: Very high volume (potential climax or news)
        
        Formula:
            Volume_Ratio = Volume / SMA(Volume, period)
        
        Args:
            volume: Volume series.
            period: Moving average period.
            
        Returns:
            Volume ratio series.
        """
        avg_volume = volume.rolling(window=period).mean()
        return volume / avg_volume
    
    def _compute_obv(
        self, 
        close: pd.Series, 
        volume: pd.Series
    ) -> pd.Series:
        """
        Compute On-Balance Volume (OBV).
        
        OBV is a cumulative indicator that measures buying and selling pressure.
        - On up days: Add volume to running total
        - On down days: Subtract volume from running total
        - On unchanged days: No change to running total
        
        OBV rising with price confirms uptrend.
        OBV falling while price rises suggests weakness (divergence).
        
        Formula:
            OBV_t = OBV_{t-1} + Volume_t * sign(Close_t - Close_{t-1})
        
        Args:
            close: Close price series.
            volume: Volume series.
            
        Returns:
            OBV series (cumulative).
        """
        # Calculate price direction
        price_change = close.diff()
        
        # Initialize OBV
        obv = pd.Series(0.0, index=close.index)
        
        # Calculate cumulative OBV
        for i in range(1, len(close)):
            if price_change.iloc[i] > 0:
                obv.iloc[i] = obv.iloc[i-1] + volume.iloc[i]
            elif price_change.iloc[i] < 0:
                obv.iloc[i] = obv.iloc[i-1] - volume.iloc[i]
            else:
                obv.iloc[i] = obv.iloc[i-1]
        
        return obv
    
    def _compute_obv_trend(
        self, 
        obv: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute OBV trend direction.
        
        This measures whether OBV is generally increasing or decreasing.
        - Positive values: OBV trending up (accumulation)
        - Negative values: OBV trending down (distribution)
        
        Args:
            obv: OBV series.
            period: Lookback period for trend.
            
        Returns:
            OBV trend series (-1, 0, 1).
        """
        obv_change = obv.diff(period)
        return np.sign(obv_change)
    
    def _compute_accumulation_distribution(
        self, 
        high: pd.Series, 
        low: pd.Series, 
        close: pd.Series, 
        volume: pd.Series
    ) -> pd.Series:
        """
        Compute Accumulation/Distribution (A/D) Line.
        
        A/D Line is similar to OBV but uses the price position within
        the day's range to weight the volume.
        
        - Close near High: Most volume added (accumulation)
        - Close near Low: Most volume subtracted (distribution)
        - Close in middle: Less weight to volume
        
        Formula:
            CLV = ((Close - Low) - (High - Close)) / (High - Low)
            A/D = Previous_A/D + CLV * Volume
        
        Args:
            high: High price series.
            low: Low price series.
            close: Close price series.
            volume: Volume series.
            
        Returns:
            A/D Line series (cumulative).
        """
        # Calculate Close Location Value (CLV)
        # CLV ranges from -1 (close at low) to +1 (close at high)
        clv = ((close - low) - (high - close)) / (high - low)
        
        # Handle division by zero (when high == low)
        clv = clv.fillna(0)
        
        # Calculate A/D Line
        ad_line = (clv * volume).cumsum()
        
        return ad_line
    
    def _compute_cmf(
        self, 
        high: pd.Series, 
        low: pd.Series, 
        close: pd.Series, 
        volume: pd.Series,
        period: int
    ) -> pd.Series:
        """
        Compute Chaikin Money Flow (CMF).
        
        CMF measures the ratio of money flow over a period.
        It sums up the A/D values and divides by total volume.
        
        Values range from -1 to +1:
        - CMF > 0: Buying pressure (accumulation)
        - CMF < 0: Selling pressure (distribution)
        - CMF > 0.25: Strong buying pressure
        - CMF < -0.25: Strong selling pressure
        
        Formula:
            CMF = Sum(AD, period) / Sum(Volume, period)
        
        Args:
            high: High price series.
            low: Low price series.
            close: Close price series.
            volume: Volume series.
            period: CMF period (typically 20).
            
        Returns:
            CMF series (-1 to +1).
        """
        # Calculate CLV
        clv = ((close - low) - (high - close)) / (high - low)
        clv = clv.fillna(0)
        
        # Calculate money flow
        money_flow = clv * volume
        
        # CMF = Sum of Money Flow / Sum of Volume
        cmf = money_flow.rolling(window=period).sum() / volume.rolling(window=period).sum()
        
        return cmf
    
    def _detect_volume_spike(
        self, 
        volume: pd.Series, 
        period: int,
        threshold: float
    ) -> pd.Series:
        """
        Detect volume spikes (unusually high volume).
        
        Volume spikes often indicate:
        - Institutional buying/selling
        - News events
        - Breakout or climax
        
        Args:
            volume: Volume series.
            period: Moving average period.
            threshold: Multiple of average volume to consider a spike.
            
        Returns:
            Binary series (1 = spike detected, 0 = no spike).
        """
        avg_volume = volume.rolling(window=period).mean()
        return (volume > avg_volume * threshold).astype(int)
    
    def _detect_volume_dry_up(
        self, 
        volume: pd.Series, 
        period: int,
        threshold: float
    ) -> pd.Series:
        """
        Detect volume dry up (unusually low volume).
        
        Volume dry up often indicates:
        - Lack of interest
        - Consolidation
        - Potential breakout setup
        
        Args:
            volume: Volume series.
            period: Moving average period.
            threshold: Fraction of average volume to consider dry up.
            
        Returns:
            Binary series (1 = dry up detected, 0 = normal volume).
        """
        avg_volume = volume.rolling(window=period).mean()
        return (volume < avg_volume * threshold).astype(int)
    
    def _compute_volume_intensity(
        self, 
        high: pd.Series, 
        low: pd.Series, 
        close: pd.Series, 
        volume: pd.Series
    ) -> pd.Series:
        """
        Compute intraday volume intensity.
        
        Volume intensity measures volume relative to price range.
        High intensity with small range suggests absorption (smart money).
        Low intensity with large range suggests weak participation.
        
        Formula:
            Intensity = Volume / (High - Low)
        
        Args:
            high: High price series.
            low: Low price series.
            close: Close price series.
            volume: Volume series.
            
        Returns:
            Volume intensity series.
        """
        price_range = high - low
        # Avoid division by zero
        price_range = price_range.replace(0, np.nan)
        intensity = volume / price_range
        return intensity.fillna(0)
    
    def _compute_volume_trend(
        self, 
        volume: pd.Series, 
        period: int
    ) -> pd.Series:
        """
        Compute volume trend direction.
        
        This measures whether volume is generally increasing or decreasing.
        - Positive: Volume trending up (increasing participation)
        - Negative: Volume trending down (decreasing participation)
        
        Args:
            volume: Volume series.
            period: Lookback period for trend.
            
        Returns:
            Volume trend series (-1, 0, 1).
        """
        volume_change = volume.diff(period)
        return np.sign(volume_change)


def generate_volume_features(
    df: pd.DataFrame,
    config: Optional[VolumeFeatureConfig] = None
) -> pd.DataFrame:
    """
    Convenience function to generate volume features.
    
    Args:
        df: DataFrame with OHLCV columns.
        config: Optional configuration.
        
    Returns:
        DataFrame with volume-based features.
    """
    generator = VolumeFeatureGenerator(config)
    return generator.generate(df)
