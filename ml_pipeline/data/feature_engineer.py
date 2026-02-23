"""
Unified Feature Engineer for Stock Movement Prediction.

This module provides a unified interface for generating all features
from OHLCV data. It combines technical, price, volume, and market features
into a single feature matrix ready for ML training.

Usage:
    from ml_pipeline.data.feature_engineer import FeatureEngineer
    
    engineer = FeatureEngineer(config)
    features_df = engineer.generate_features(ohlcv_df, index_data)
"""

from typing import Dict, List, Optional, Tuple, Union
import pandas as pd
import numpy as np
from dataclasses import dataclass
import warnings

from ml_pipeline.config import FeatureConfig, MLPipelineConfig
from ml_pipeline.features.technical_features import TechnicalFeatureGenerator
from ml_pipeline.features.price_features import PriceFeatureGenerator
from ml_pipeline.features.volume_features import VolumeFeatureGenerator
from ml_pipeline.features.market_features import MarketFeatureGenerator


class FeatureEngineer:
    """
    Unified feature engineering pipeline.
    
    This class orchestrates the generation of all features from OHLCV data.
    It combines features from multiple generators and handles:
    - Feature alignment
    - NaN handling
    - Feature selection
    - Feature naming conventions
    
    Attributes:
        config: Feature configuration.
        technical_generator: Technical indicator generator.
        price_generator: Price-based feature generator.
        volume_generator: Volume-based feature generator.
        market_generator: Market-wide feature generator.
    
    Example:
        >>> config = MLPipelineConfig.from_yaml('configs/ml_config.yaml')
        >>> engineer = FeatureEngineer(config.features)
        >>> features = engineer.generate_features(stock_df, index_data)
    """
    
    def __init__(
        self, 
        config: Optional[FeatureConfig] = None,
        verbose: bool = False
    ):
        """
        Initialize the feature engineer.
        
        Args:
            config: Feature configuration. If None, uses defaults.
            verbose: Whether to print progress information.
        """
        self.config = config or FeatureConfig()
        self.verbose = verbose
        
        # Initialize feature generators
        self.technical_generator = TechnicalFeatureGenerator(self.config.technical)
        self.price_generator = PriceFeatureGenerator(self.config.price)
        self.volume_generator = VolumeFeatureGenerator(self.config.volume)
        self.market_generator = MarketFeatureGenerator(self.config.market)
        
        # Track feature names for later reference
        self._feature_names: List[str] = []
        self._feature_stats: Dict[str, Dict] = {}
    
    def generate_features(
        self,
        df: pd.DataFrame,
        index_data: Optional[Dict[str, pd.DataFrame]] = None,
        drop_nan: bool = True,
        fill_method: str = 'ffill'
    ) -> pd.DataFrame:
        """
        Generate all features from OHLCV data.
        
        This is the main entry point for feature generation.
        It combines features from all generators into a single DataFrame.
        
        Args:
            df: DataFrame with OHLCV columns (Open, High, Low, Close, Volume).
                Must have a DatetimeIndex.
            index_data: Optional dict of index DataFrames for market features.
                       Keys are index symbols (e.g., '^NSEI'), values are DataFrames.
            drop_nan: Whether to drop rows with NaN values after feature generation.
            fill_method: Method to fill NaN values ('ffill', 'bfill', 'interpolate').
            
        Returns:
            DataFrame with all generated features.
            Index matches the input DataFrame.
        """
        if self.verbose:
            print("Starting feature generation...")
            print(f"Input shape: {df.shape}")
        
        # Validate input
        self._validate_input(df)
        
        # Initialize features DataFrame with same index
        features = pd.DataFrame(index=df.index)
        
        # ==========================================
        # 1. TECHNICAL INDICATORS
        # ==========================================
        if self.verbose:
            print("Generating technical indicators...")
        
        technical_features = self.technical_generator.generate(df)
        features = pd.concat([features, technical_features], axis=1)
        
        if self.verbose:
            print(f"  Generated {technical_features.shape[1]} technical features")
        
        # ==========================================
        # 2. PRICE-BASED FEATURES
        # ==========================================
        if self.verbose:
            print("Generating price features...")
        
        price_features = self.price_generator.generate(df)
        features = pd.concat([features, price_features], axis=1)
        
        if self.verbose:
            print(f"  Generated {price_features.shape[1]} price features")
        
        # ==========================================
        # 3. VOLUME-BASED FEATURES
        # ==========================================
        if self.verbose:
            print("Generating volume features...")
        
        volume_features = self.volume_generator.generate(df)
        features = pd.concat([features, volume_features], axis=1)
        
        if self.verbose:
            print(f"  Generated {volume_features.shape[1]} volume features")
        
        # ==========================================
        # 4. MARKET-WIDE FEATURES
        # ==========================================
        if self.verbose:
            print("Generating market features...")
        
        market_features = self.market_generator.generate(df, index_data)
        features = pd.concat([features, market_features], axis=1)
        
        if self.verbose:
            print(f"  Generated {market_features.shape[1]} market features")
        
        # ==========================================
        # 5. POST-PROCESSING
        # ==========================================
        # Store feature names
        self._feature_names = list(features.columns)
        
        # Handle infinite values
        features = self._handle_infinite(features)
        
        # Handle NaN values - fill first, then drop remaining
        if fill_method:
            features = self._fill_nan(features, method=fill_method)
        
        # Drop NaN rows if requested (only drop rows with ALL NaN)
        if drop_nan:
            initial_rows = len(features)
            # Only drop rows where ALL features are NaN
            features = features.dropna(how='all')
            # For remaining NaN values, drop rows with ANY NaN in critical features only
            # Critical features are the first 20 (basic technical and price features)
            critical_cols = features.columns[:20].tolist()
            features = features.dropna(subset=critical_cols)
            dropped_rows = initial_rows - len(features)
            if self.verbose and dropped_rows > 0:
                print(f"  Dropped {dropped_rows} rows with NaN values")
        
        # Fill any remaining NaN with 0 (for market features that might be missing)
        features = features.fillna(0)
        
        # Calculate feature statistics
        self._calculate_feature_stats(features)
        
        if self.verbose:
            print(f"\nFeature generation complete!")
            print(f"Total features: {features.shape[1]}")
            print(f"Output shape: {features.shape}")
        
        return features
    
    def _validate_input(self, df: pd.DataFrame) -> None:
        """
        Validate input DataFrame.
        
        Args:
            df: Input DataFrame to validate.
            
        Raises:
            ValueError: If required columns are missing.
        """
        required_columns = ['Open', 'High', 'Low', 'Close', 'Volume']
        
        # Check for required columns (case-insensitive)
        df_columns_lower = [col.lower() for col in df.columns]
        missing_columns = []
        
        for col in required_columns:
            if col.lower() not in df_columns_lower:
                missing_columns.append(col)
        
        if missing_columns:
            raise ValueError(
                f"Missing required columns: {missing_columns}. "
                f"DataFrame must have OHLCV columns."
            )
        
        # Check for DatetimeIndex
        if not isinstance(df.index, pd.DatetimeIndex):
            warnings.warn(
                "DataFrame index is not DatetimeIndex. "
                "Some features may not work correctly."
            )
    
    def _handle_infinite(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Handle infinite values in features.
        
        Args:
            df: Feature DataFrame.
            
        Returns:
            DataFrame with infinite values replaced.
        """
        # Replace inf with NaN
        df = df.replace([np.inf, -np.inf], np.nan)
        
        return df
    
    def _fill_nan(self, df: pd.DataFrame, method: str = 'ffill') -> pd.DataFrame:
        """
        Fill NaN values in features.
        
        Args:
            df: Feature DataFrame.
            method: Fill method ('ffill', 'bfill', 'interpolate').
            
        Returns:
            DataFrame with NaN values filled.
        """
        if method == 'ffill':
            df = df.fillna(method='ffill')
        elif method == 'bfill':
            df = df.fillna(method='bfill')
        elif method == 'interpolate':
            df = df.interpolate(method='linear')
        
        # Fill any remaining NaN with 0
        df = df.fillna(0)
        
        return df
    
    def _calculate_feature_stats(self, df: pd.DataFrame) -> None:
        """
        Calculate statistics for each feature.
        
        Args:
            df: Feature DataFrame.
        """
        self._feature_stats = {}
        
        for col in df.columns:
            self._feature_stats[col] = {
                'mean': df[col].mean(),
                'std': df[col].std(),
                'min': df[col].min(),
                'max': df[col].max(),
                'nan_count': df[col].isna().sum(),
            }
    
    def get_feature_names(self) -> List[str]:
        """
        Get list of generated feature names.
        
        Returns:
            List of feature names.
        """
        return self._feature_names
    
    def get_feature_stats(self) -> Dict[str, Dict]:
        """
        Get statistics for each feature.
        
        Returns:
            Dictionary mapping feature names to their statistics.
        """
        return self._feature_stats
    
    def get_feature_importance_order(self) -> List[str]:
        """
        Get features ordered by expected importance.
        
        This returns features grouped by category, which can be useful
        for feature selection or understanding feature contributions.
        
        Returns:
            List of feature names ordered by category importance.
        """
        # Define category order (most important first)
        category_order = [
            # Technical indicators (most predictive)
            ['rsi_', 'macd', 'ema_', 'bollinger_', 'atr', 'stochastic_', 'adx'],
            # Price features
            ['returns_', 'volatility_', 'high_low', 'close_open', 'gap_', 'price_', 'momentum_', 'ema_cross'],
            # Volume features
            ['volume_', 'obv', 'accumulation', 'chaikin'],
            # Market features
            ['nifty_', 'beta_', 'correlation_', 'sector_'],
        ]
        
        ordered_features = []
        remaining_features = list(self._feature_names)
        
        for category_prefixes in category_order:
            for prefix in category_prefixes:
                matching = [f for f in remaining_features if f.startswith(prefix)]
                ordered_features.extend(matching)
                remaining_features = [f for f in remaining_features if f not in matching]
        
        # Add any remaining features
        ordered_features.extend(remaining_features)
        
        return ordered_features
    
    def select_features(
        self,
        df: pd.DataFrame,
        n_features: int = 50,
        method: str = 'importance'
    ) -> pd.DataFrame:
        """
        Select top N features.
        
        Args:
            df: Feature DataFrame.
            n_features: Number of features to select.
            method: Selection method ('importance', 'variance').
            
        Returns:
            DataFrame with selected features.
        """
        if method == 'importance':
            ordered_features = self.get_feature_importance_order()
            selected = ordered_features[:n_features]
            # Only keep features that exist in df
            selected = [f for f in selected if f in df.columns]
        
        elif method == 'variance':
            # Select features with highest variance
            variance = df.var()
            selected = variance.nlargest(n_features).index.tolist()
        
        else:
            raise ValueError(f"Unknown selection method: {method}")
        
        return df[selected]
    
    def __repr__(self) -> str:
        """String representation."""
        return (
            f"FeatureEngineer("
            f"technical={self.config.technical.enabled}, "
            f"price={self.config.price.enabled}, "
            f"volume={self.config.volume.enabled}, "
            f"market={self.config.market.enabled})"
        )


def create_feature_engineer(
    config_path: Optional[str] = None,
    verbose: bool = False
) -> FeatureEngineer:
    """
    Convenience function to create a feature engineer.
    
    Args:
        config_path: Path to configuration YAML file.
        verbose: Whether to print progress information.
        
    Returns:
        Configured FeatureEngineer instance.
    """
    if config_path:
        config = MLPipelineConfig.from_yaml(config_path)
        return FeatureEngineer(config.features, verbose=verbose)
    
    return FeatureEngineer(verbose=verbose)


def generate_all_features(
    df: pd.DataFrame,
    index_data: Optional[Dict[str, pd.DataFrame]] = None,
    config: Optional[FeatureConfig] = None,
    verbose: bool = False
) -> pd.DataFrame:
    """
    Convenience function to generate all features in one call.
    
    Args:
        df: DataFrame with OHLCV columns.
        index_data: Optional dict of index DataFrames.
        config: Optional feature configuration.
        verbose: Whether to print progress information.
        
    Returns:
        DataFrame with all generated features.
    """
    engineer = FeatureEngineer(config, verbose=verbose)
    return engineer.generate_features(df, index_data)
