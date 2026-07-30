"""
Data Preprocessor for Stock Movement Prediction.

This module handles all data preprocessing steps required before training:
1. Feature scaling (RobustScaler for outlier resistance)
2. Feature selection (remove low-correlation features)
3. Class imbalance handling (compute class weights)
4. Outlier detection and handling
5. Train/test split with time-series awareness

Usage:
    from ml_pipeline.data.preprocessor import DataPreprocessor
    
    preprocessor = DataPreprocessor(config)
    X_train, X_test, y_train, y_test = preprocessor.prepare_data(df)
"""

from typing import Dict, List, Optional, Tuple, Union
import pandas as pd
import numpy as np
from dataclasses import dataclass
from sklearn.preprocessing import RobustScaler, StandardScaler
from sklearn.model_selection import train_test_split
import warnings

from ml_pipeline.config import FeatureConfig, MLPipelineConfig


@dataclass
class PreprocessingConfig:
    """Configuration for data preprocessing."""
    
    # Feature selection
    # NOTE: For tree-based models (XGBoost, RF, LightGBM), correlation filtering
    # is NOT recommended. These models can find non-linear interactions between
    # features that individually have low correlation with the target.
    # Set min_correlation_threshold=0 to keep all features.
    min_correlation_threshold: float = 0.0  # Keep all features by default (tree models handle selection)
    max_missing_pct: float = 0.10  # Remove features with > this % missing
    remove_zero_variance: bool = True  # Remove features with zero variance
    
    # Scaling
    scaler_type: str = 'robust'  # 'robust' or 'standard'
    
    # Outlier handling
    clip_outliers: bool = True
    outlier_method: str = 'iqr'  # 'iqr' or 'zscore'
    outlier_threshold: float = 5.0  # For zscore method
    
    # Train/test split
    test_size: float = 0.2
    validation_size: float = 0.1  # From training data
    
    # Time-series settings
    use_time_series_split: bool = True
    purge_days: int = 5  # Days to exclude between train and test to prevent leakage
    
    # Class imbalance
    compute_class_weights: bool = True
    
    # Features to always exclude
    exclude_features: List[str] = None
    
    def __post_init__(self):
        if self.exclude_features is None:
            self.exclude_features = []


class DataPreprocessor:
    """
    Preprocessor for stock movement prediction data.
    
    This class handles all preprocessing steps required before training:
    - Feature selection based on correlation and variance
    - Feature scaling (RobustScaler recommended for financial data)
    - Outlier handling
    - Train/test/validation splitting with time-series awareness
    - Class weight computation for imbalanced datasets
    
    Attributes:
        config: Preprocessing configuration.
        scaler: Fitted scaler object.
        selected_features: List of features selected after preprocessing.
        class_weights: Computed class weights for imbalanced data.
    
    Example:
        >>> config = PreprocessingConfig()
        >>> preprocessor = DataPreprocessor(config)
        >>> X_train, X_test, y_train, y_test = preprocessor.prepare_data(df)
    """
    
    def __init__(self, config: Optional[PreprocessingConfig] = None):
        """
        Initialize the preprocessor.
        
        Args:
            config: Preprocessing configuration. If None, uses defaults.
        """
        self.config = config or PreprocessingConfig()
        self.scaler: Optional[RobustScaler] = None
        self.selected_features: List[str] = []
        self.excluded_features: List[str] = []
        self.class_weights: Dict[int, float] = {}
        self.feature_stats: Dict[str, Dict] = {}
        
    def fit(
        self, 
        df: pd.DataFrame, 
        target_col: str = 'label',
        feature_cols: Optional[List[str]] = None
    ) -> 'DataPreprocessor':
        """
        Fit the preprocessor on training data.
        
        This method:
        1. Identifies feature columns
        2. Selects features based on correlation/variance
        3. Fits the scaler
        4. Computes class weights
        
        Args:
            df: DataFrame with features and target.
            target_col: Name of the target column.
            feature_cols: Optional list of feature columns. If None, auto-detects.
            
        Returns:
            self (fitted preprocessor)
        """
        # Identify feature columns
        if feature_cols is None:
            feature_cols = self._identify_feature_columns(df, target_col)
        
        # Remove excluded features
        feature_cols = [f for f in feature_cols if f not in self.config.exclude_features]
        
        # Select features based on quality metrics
        self.selected_features, self.excluded_features = self._select_features(
            df[feature_cols], df[target_col]
        )
        
        # Fit scaler on selected features
        X = df[self.selected_features].values
        self._fit_scaler(X)
        
        # Compute class weights
        if self.config.compute_class_weights:
            self.class_weights = self._compute_class_weights(df[target_col])
        
        # Store feature statistics
        self._compute_feature_stats(df[self.selected_features])
        
        return self
    
    def transform(
        self, 
        df: pd.DataFrame,
        clip_outliers: bool = True
    ) -> pd.DataFrame:
        """
        Transform data using fitted preprocessor.
        
        Args:
            df: DataFrame with features.
            clip_outliers: Whether to clip outliers.
            
        Returns:
            DataFrame with preprocessed features.
        """
        if not self.selected_features:
            raise ValueError("Preprocessor not fitted. Call fit() first.")
        
        # Get feature matrix
        X = df[self.selected_features].copy()
        
        # Handle outliers
        if clip_outliers and self.config.clip_outliers:
            X = self._clip_outliers(X)
        
        # Scale features
        X_scaled = self.scaler.transform(X.values)
        
        # Create output DataFrame
        result = pd.DataFrame(
            X_scaled, 
            columns=self.selected_features, 
            index=df.index
        )
        
        # Preserve non-feature columns
        for col in df.columns:
            if col not in self.selected_features:
                result[col] = df[col]
        
        return result
    
    def fit_transform(
        self, 
        df: pd.DataFrame,
        target_col: str = 'label',
        feature_cols: Optional[List[str]] = None
    ) -> pd.DataFrame:
        """
        Fit and transform in one step.
        
        Args:
            df: DataFrame with features and target.
            target_col: Name of the target column.
            feature_cols: Optional list of feature columns.
            
        Returns:
            DataFrame with preprocessed features.
        """
        self.fit(df, target_col, feature_cols)
        return self.transform(df)
    
    def prepare_data(
        self,
        df: pd.DataFrame,
        target_col: str = 'label',
        feature_cols: Optional[List[str]] = None,
        return_indices: bool = False
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
        """
        Prepare data for training with train/test split.
        
        This is the main entry point for data preparation.
        It handles:
        - Feature selection
        - Scaling
        - Train/test split (time-series aware)
        - Purge gap to prevent leakage
        
        Args:
            df: DataFrame with features and target.
            target_col: Name of the target column.
            feature_cols: Optional list of feature columns.
            return_indices: Whether to return train/test indices.
            
        Returns:
            Tuple of (X_train, X_test, y_train, y_test)
            If return_indices=True, also returns (train_idx, test_idx)
        """
        # Fit and transform
        df_processed = self.fit_transform(df, target_col, feature_cols)
        
        # Get feature matrix and target
        X = df_processed[self.selected_features]
        y = df_processed[target_col]
        
        if self.config.use_time_series_split:
            # Time-series split: use most recent data for test
            split_idx = int(len(df) * (1 - self.config.test_size))
            
            # Add purge gap
            purge_idx = self.config.purge_days
            
            X_train = X.iloc[:split_idx - purge_idx]
            X_test = X.iloc[split_idx:]
            y_train = y.iloc[:split_idx - purge_idx]
            y_test = y.iloc[split_idx:]
            
            train_idx = X_train.index
            test_idx = X_test.index
        else:
            # Random split
            X_train, X_test, y_train, y_test, train_idx, test_idx = train_test_split(
                X, y, 
                test_size=self.config.test_size,
                shuffle=False,  # Don't shuffle for time series
                return_indices=True
            )
        
        if return_indices:
            return X_train, X_test, y_train, y_test, train_idx, test_idx
        return X_train, X_test, y_train, y_test
    
    def _identify_feature_columns(
        self, 
        df: pd.DataFrame, 
        target_col: str
    ) -> List[str]:
        """Identify feature columns from DataFrame."""
        # Exclude common non-feature columns
        exclude_patterns = [
            target_col, 'label', 'target',
            'symbol', 'ticker', 'stock',
            'date', 'time', 'timestamp',
            'forward_return', 'future_return',
            'Open', 'High', 'Low', 'Close', 'Volume',  # Raw OHLCV
            'Adj Close'
        ]
        
        feature_cols = [
            col for col in df.columns 
            if col not in exclude_patterns
            and not col.startswith('_')
        ]
        
        return feature_cols
    
    def _select_features(
        self, 
        X: pd.DataFrame, 
        y: pd.Series
    ) -> Tuple[List[str], List[str]]:
        """
        Select features based on quality metrics.
        
        Selection criteria:
        1. Non-zero variance
        2. Correlation with target above threshold
        3. Missing value percentage below threshold
        
        Args:
            X: Feature DataFrame.
            y: Target series.
            
        Returns:
            Tuple of (selected_features, excluded_features)
        """
        selected = []
        excluded = []
        
        for col in X.columns:
            # Skip non-numeric columns
            if not pd.api.types.is_numeric_dtype(X[col]):
                excluded.append((col, 'non_numeric'))
                continue
            
            # Check variance
            if self.config.remove_zero_variance:
                try:
                    if X[col].var() == 0:
                        excluded.append((col, 'zero_variance'))
                        continue
                except TypeError:
                    excluded.append((col, 'non_numeric'))
                    continue
            
            # Check missing values
            missing_pct = X[col].isna().sum() / len(X)
            if missing_pct > self.config.max_missing_pct:
                excluded.append((col, f'high_missing_{missing_pct:.2%}'))
                continue
            
            # Check correlation with target
            if y is not None and self.config.min_correlation_threshold > 0:
                try:
                    corr = X[col].corr(y)
                    if abs(corr) < self.config.min_correlation_threshold:
                        excluded.append((col, f'low_correlation_{corr:.4f}'))
                        continue
                except Exception:
                    pass  # Keep feature if correlation can't be computed
            
            selected.append(col)
        
        # Log excluded features
        if excluded:
            print(f"\nExcluded {len(excluded)} features:")
            for feat, reason in excluded[:10]:  # Show first 10
                print(f"  - {feat}: {reason}")
            if len(excluded) > 10:
                print(f"  ... and {len(excluded) - 10} more")
        
        return selected, [e[0] for e in excluded]
    
    def _fit_scaler(self, X: np.ndarray) -> None:
        """Fit the feature scaler."""
        if self.config.scaler_type == 'robust':
            self.scaler = RobustScaler()
        else:
            self.scaler = StandardScaler()
        
        self.scaler.fit(X)
    
    def _clip_outliers(self, X: pd.DataFrame) -> pd.DataFrame:
        """Clip outliers in features."""
        X_clipped = X.copy()
        
        if self.config.outlier_method == 'iqr':
            # IQR method: clip values outside 1.5 * IQR
            for col in X_clipped.columns:
                Q1 = X_clipped[col].quantile(0.25)
                Q3 = X_clipped[col].quantile(0.75)
                IQR = Q3 - Q1
                lower = Q1 - 1.5 * IQR
                upper = Q3 + 1.5 * IQR
                X_clipped[col] = X_clipped[col].clip(lower, upper)
        else:
            # Z-score method
            for col in X_clipped.columns:
                mean = X_clipped[col].mean()
                std = X_clipped[col].std()
                lower = mean - self.config.outlier_threshold * std
                upper = mean + self.config.outlier_threshold * std
                X_clipped[col] = X_clipped[col].clip(lower, upper)
        
        return X_clipped
    
    def _compute_class_weights(self, y: pd.Series) -> Dict[int, float]:
        """
        Compute class weights for imbalanced data.
        
        Uses balanced weighting: n_samples / (n_classes * class_count)
        """
        class_counts = y.value_counts()
        n_samples = len(y)
        n_classes = len(class_counts)
        
        weights = {}
        for label in class_counts.index:
            weights[label] = n_samples / (n_classes * class_counts[label])
        
        return weights
    
    def _compute_feature_stats(self, X: pd.DataFrame) -> None:
        """Compute and store feature statistics."""
        for col in X.columns:
            self.feature_stats[col] = {
                'mean': X[col].mean(),
                'std': X[col].std(),
                'min': X[col].min(),
                'max': X[col].max(),
                'median': X[col].median(),
                'skew': X[col].skew(),
                'kurtosis': X[col].kurtosis()
            }
    
    def get_feature_importance_preview(
        self, 
        df: pd.DataFrame, 
        target_col: str = 'label'
    ) -> pd.DataFrame:
        """
        Get feature importance preview based on correlation with target.
        
        Args:
            df: DataFrame with features and target.
            target_col: Name of the target column.
            
        Returns:
            DataFrame with feature correlations sorted by absolute value.
        """
        feature_cols = self._identify_feature_columns(df, target_col)
        
        correlations = []
        for col in feature_cols:
            corr = df[col].corr(df[target_col])
            correlations.append({
                'feature': col,
                'correlation': corr,
                'abs_correlation': abs(corr)
            })
        
        corr_df = pd.DataFrame(correlations)
        corr_df = corr_df.sort_values('abs_correlation', ascending=False)
        
        return corr_df[['feature', 'correlation', 'abs_correlation']]
    
    def get_preprocessing_summary(self) -> Dict:
        """Get summary of preprocessing steps."""
        return {
            'n_features_selected': len(self.selected_features),
            'n_features_excluded': len(self.excluded_features),
            'selected_features': self.selected_features,
            'excluded_features': self.excluded_features,
            'class_weights': self.class_weights,
            'scaler_type': self.config.scaler_type,
            'feature_stats': self.feature_stats
        }


def preprocess_for_training(
    df: pd.DataFrame,
    target_col: str = 'label',
    config: Optional[PreprocessingConfig] = None
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series, DataPreprocessor]:
    """
    Convenience function for preprocessing data for training.
    
    Args:
        df: DataFrame with features and target.
        target_col: Name of the target column.
        config: Optional preprocessing configuration.
        
    Returns:
        Tuple of (X_train, X_test, y_train, y_test, preprocessor)
    """
    preprocessor = DataPreprocessor(config)
    X_train, X_test, y_train, y_test = preprocessor.prepare_data(df, target_col)
    return X_train, X_test, y_train, y_test, preprocessor
