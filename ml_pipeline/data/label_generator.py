"""
Label Generator Module for ML Stock Prediction Pipeline.

This module handles the creation of target labels for stock movement prediction.
Labels are generated based on future price movements relative to thresholds.

Label Definition:
- UP (+1): Return > +up_threshold (default 1%)
- DOWN (-1): Return < -down_threshold (default 1%)
- FLAT (0): Return between -down_threshold and +up_threshold

Key Components:
- LabelGenerator: Main class for generating labels
- LabelConfig: Configuration for label generation
- LabelStatistics: Statistics about generated labels

Usage:
    from ml_pipeline.data.label_generator import LabelGenerator
    
    generator = LabelGenerator(
        up_threshold=0.01,    # 1% up
        down_threshold=0.01,  # 1% down
        forward_days=1        # Next day prediction
    )
    
    # Generate labels from OHLCV data
    labels = generator.generate_labels(df)
    
    # Get label statistics
    stats = generator.get_statistics(labels)
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union
from enum import Enum

import pandas as pd
import numpy as np

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class LabelClass(Enum):
    """Enumeration of label classes."""
    DOWN = -1
    FLAT = 0
    UP = 1
    
    @classmethod
    def from_return(cls, return_val: float, up_threshold: float, down_threshold: float) -> 'LabelClass':
        """
        Determine label class from return value.
        
        Args:
            return_val: The return value (e.g., 0.02 for 2%)
            up_threshold: Threshold for UP label
            down_threshold: Threshold for DOWN label
            
        Returns:
            LabelClass enum value
        """
        if return_val > up_threshold:
            return cls.UP
        elif return_val < -down_threshold:
            return cls.DOWN
        else:
            return cls.FLAT


@dataclass
class LabelConfig:
    """
    Configuration for label generation.
    
    Attributes:
        up_threshold: Minimum return for UP label (default: 0.01 = 1%)
        down_threshold: Minimum negative return for DOWN label (default: 0.01 = 1%)
        forward_days: Number of days forward to calculate return (default: 1)
        price_column: Column to use for price (default: 'Close')
        use_adjusted: Whether to use adjusted close if available
        min_samples: Minimum samples required for each class
        handle_missing: How to handle missing future data ('drop', 'ignore')
    """
    up_threshold: float = 0.01
    down_threshold: float = 0.01
    forward_days: int = 1
    price_column: str = 'Close'
    use_adjusted: bool = True
    min_samples: int = 30
    handle_missing: str = 'drop'
    
    def __post_init__(self):
        """Validate configuration parameters."""
        if self.up_threshold <= 0:
            raise ValueError("up_threshold must be positive")
        if self.down_threshold <= 0:
            raise ValueError("down_threshold must be positive")
        if self.forward_days < 1:
            raise ValueError("forward_days must be at least 1")
        if self.handle_missing not in ['drop', 'ignore']:
            raise ValueError("handle_missing must be 'drop' or 'ignore'")
    
    def to_dict(self) -> Dict:
        """Convert config to dictionary."""
        return {
            'up_threshold': self.up_threshold,
            'down_threshold': self.down_threshold,
            'forward_days': self.forward_days,
            'price_column': self.price_column,
            'use_adjusted': self.use_adjusted,
            'min_samples': self.min_samples,
            'handle_missing': self.handle_missing
        }


@dataclass
class LabelStatistics:
    """
    Statistics about generated labels.
    
    Attributes:
        total_samples: Total number of samples
        up_count: Number of UP labels
        down_count: Number of DOWN labels
        flat_count: Number of FLAT labels
        up_pct: Percentage of UP labels
        down_pct: Percentage of DOWN labels
        flat_pct: Percentage of FLAT labels
        class_balance: Ratio of minority to majority class
        avg_return_up: Average return for UP labels
        avg_return_down: Average return for DOWN labels
        avg_return_flat: Average return for FLAT labels
        return_distribution: Distribution of returns
    """
    total_samples: int
    up_count: int
    down_count: int
    flat_count: int
    up_pct: float
    down_pct: float
    flat_pct: float
    class_balance: float
    avg_return_up: float
    avg_return_down: float
    avg_return_flat: float
    return_distribution: Dict[str, float] = field(default_factory=dict)
    
    def __str__(self) -> str:
        """String representation of statistics."""
        lines = [
            "Label Statistics",
            "=" * 40,
            f"Total Samples: {self.total_samples}",
            "",
            "Class Distribution:",
            f"  UP (+1):   {self.up_count:5d} ({self.up_pct:.1%})",
            f"  FLAT (0):  {self.flat_count:5d} ({self.flat_pct:.1%})",
            f"  DOWN (-1): {self.down_count:5d} ({self.down_pct:.1%})",
            "",
            f"Class Balance: {self.class_balance:.2f}",
            "",
            "Average Returns by Class:",
            f"  UP:   {self.avg_return_up:+.2%}",
            f"  FLAT: {self.avg_return_flat:+.2%}",
            f"  DOWN: {self.avg_return_down:+.2%}",
        ]
        return "\n".join(lines)
    
    def to_dict(self) -> Dict:
        """Convert statistics to dictionary."""
        return {
            'total_samples': self.total_samples,
            'up_count': self.up_count,
            'down_count': self.down_count,
            'flat_count': self.flat_count,
            'up_pct': self.up_pct,
            'down_pct': self.down_pct,
            'flat_pct': self.flat_pct,
            'class_balance': self.class_balance,
            'avg_return_up': self.avg_return_up,
            'avg_return_down': self.avg_return_down,
            'avg_return_flat': self.avg_return_flat,
            'return_distribution': self.return_distribution
        }


class LabelGenerator:
    """
    Generates target labels for stock movement prediction.
    
    This class creates labels based on future price movements:
    - UP (+1): If forward return exceeds up_threshold
    - DOWN (-1): If forward return is below -down_threshold
    - FLAT (0): Otherwise
    
    Features:
    - Configurable thresholds for UP/DOWN classification
    - Support for multi-day forward returns
    - Class imbalance detection and reporting
    - Return distribution analysis
    - Support for adjusted or unadjusted prices
    
    Example:
        generator = LabelGenerator(
            up_threshold=0.01,
            down_threshold=0.01,
            forward_days=1
        )
        
        # Generate labels
        labels_df = generator.generate_labels(stock_df)
        
        # Get statistics
        stats = generator.get_statistics(labels_df)
        print(stats)
    """
    
    def __init__(
        self,
        up_threshold: float = 0.01,
        down_threshold: float = 0.01,
        forward_days: int = 1,
        price_column: str = 'Close',
        use_adjusted: bool = True,
        min_samples: int = 30,
        handle_missing: str = 'drop'
    ):
        """
        Initialize the label generator.
        
        Args:
            up_threshold: Minimum return for UP label (default: 0.01 = 1%)
            down_threshold: Minimum negative return for DOWN label (default: 0.01 = 1%)
            forward_days: Number of days forward to calculate return
            price_column: Column to use for price
            use_adjusted: Whether to use adjusted close if available
            min_samples: Minimum samples required for each class
            handle_missing: How to handle missing future data
        """
        self.config = LabelConfig(
            up_threshold=up_threshold,
            down_threshold=down_threshold,
            forward_days=forward_days,
            price_column=price_column,
            use_adjusted=use_adjusted,
            min_samples=min_samples,
            handle_missing=handle_missing
        )
        
        self._labels: Optional[pd.DataFrame] = None
        self._statistics: Optional[LabelStatistics] = None
    
    def generate_labels(
        self,
        df: pd.DataFrame,
        return_column: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Generate labels from OHLCV data.
        
        This method:
        1. Calculates forward returns (next N days)
        2. Assigns labels based on thresholds
        3. Handles missing future data
        4. Returns DataFrame with labels and returns
        
        Args:
            df: DataFrame with OHLCV data (must have price_column)
            return_column: Optional pre-calculated return column
            
        Returns:
            DataFrame with columns:
            - 'forward_return': The calculated forward return
            - 'label': The assigned label (-1, 0, 1)
            - 'label_name': Human-readable label name
        """
        if df is None or df.empty:
            raise ValueError("Input DataFrame is empty")
        
        # Get price series
        price_col = self.config.price_column
        if price_col not in df.columns:
            raise ValueError(f"Price column '{price_col}' not found in DataFrame")
        
        prices = df[price_col].copy()
        
        # Calculate forward returns
        # For forward_days=1, this is the return from today's close to tomorrow's close
        forward_returns = self._calculate_forward_returns(prices)
        
        # Create labels DataFrame
        labels_df = pd.DataFrame(index=df.index)
        labels_df['forward_return'] = forward_returns
        labels_df['label'] = self._assign_labels(forward_returns)
        labels_df['label_name'] = labels_df['label'].map({
            1: 'UP',
            0: 'FLAT',
            -1: 'DOWN'
        })
        
        # Handle missing values
        if self.config.handle_missing == 'drop':
            labels_df = labels_df.dropna()
        
        # Store for statistics
        self._labels = labels_df
        
        logger.info(f"Generated {len(labels_df)} labels")
        
        return labels_df
    
    def _calculate_forward_returns(self, prices: pd.Series) -> pd.Series:
        """
        Calculate forward returns.
        
        Forward return is calculated as:
        return = (price[t+forward_days] - price[t]) / price[t]
        
        This represents the return you would get if you bought at
        today's close and sold at the close N days later.
        
        Args:
            prices: Series of prices
            
        Returns:
            Series of forward returns
        """
        # Shift prices backward to get future prices
        future_prices = prices.shift(-self.config.forward_days)
        
        # Calculate return
        forward_returns = (future_prices - prices) / prices
        
        return forward_returns
    
    def _assign_labels(self, returns: pd.Series) -> pd.Series:
        """
        Assign labels based on return thresholds.
        
        Label Assignment:
        - UP (+1): return > up_threshold
        - DOWN (-1): return < -down_threshold
        - FLAT (0): otherwise
        
        Args:
            returns: Series of forward returns
            
        Returns:
            Series of labels (-1, 0, 1)
        """
        labels = pd.Series(0, index=returns.index)  # Default FLAT
        
        # UP labels
        labels[returns > self.config.up_threshold] = 1
        
        # DOWN labels
        labels[returns < -self.config.down_threshold] = -1
        
        # Handle NaN (will be dealt with later based on config)
        labels[returns.isna()] = np.nan
        
        return labels
    
    def get_statistics(self, labels_df: Optional[pd.DataFrame] = None) -> LabelStatistics:
        """
        Calculate statistics for generated labels.
        
        This method provides insights into:
        - Class distribution (balance/imbalance)
        - Average returns per class
        - Return distribution percentiles
        
        Args:
            labels_df: DataFrame with labels (uses stored labels if None)
            
        Returns:
            LabelStatistics object with distribution info
        """
        if labels_df is None:
            labels_df = self._labels
        
        if labels_df is None or labels_df.empty:
            raise ValueError("No labels available. Run generate_labels() first.")
        
        # Count classes
        label_counts = labels_df['label'].value_counts()
        
        up_count = label_counts.get(1, 0)
        down_count = label_counts.get(-1, 0)
        flat_count = label_counts.get(0, 0)
        total = len(labels_df)
        
        # Calculate percentages
        up_pct = up_count / total if total > 0 else 0
        down_pct = down_count / total if total > 0 else 0
        flat_pct = flat_count / total if total > 0 else 0
        
        # Calculate class balance (ratio of minority to majority)
        counts = [up_count, down_count, flat_count]
        non_zero_counts = [c for c in counts if c > 0]
        if len(non_zero_counts) >= 2:
            class_balance = min(non_zero_counts) / max(non_zero_counts)
        else:
            class_balance = 0.0
        
        # Calculate average returns per class
        returns = labels_df['forward_return']
        
        avg_return_up = returns[labels_df['label'] == 1].mean() if up_count > 0 else 0
        avg_return_down = returns[labels_df['label'] == -1].mean() if down_count > 0 else 0
        avg_return_flat = returns[labels_df['label'] == 0].mean() if flat_count > 0 else 0
        
        # Return distribution
        return_dist = {
            'min': returns.min(),
            'p5': returns.quantile(0.05),
            'p25': returns.quantile(0.25),
            'median': returns.median(),
            'p75': returns.quantile(0.75),
            'p95': returns.quantile(0.95),
            'max': returns.max(),
            'mean': returns.mean(),
            'std': returns.std()
        }
        
        stats = LabelStatistics(
            total_samples=total,
            up_count=up_count,
            down_count=down_count,
            flat_count=flat_count,
            up_pct=up_pct,
            down_pct=down_pct,
            flat_pct=flat_pct,
            class_balance=class_balance,
            avg_return_up=avg_return_up,
            avg_return_down=avg_return_down,
            avg_return_flat=avg_return_flat,
            return_distribution=return_dist
        )
        
        self._statistics = stats
        
        return stats
    
    def get_class_weights(self, labels_df: Optional[pd.DataFrame] = None) -> Dict[int, float]:
        """
        Calculate class weights for handling imbalanced data.
        
        Class weights are calculated using the inverse frequency method:
        weight = total_samples / (n_classes * class_count)
        
        These weights can be passed to models like XGBoost, LightGBM
        to handle class imbalance during training.
        
        Args:
            labels_df: DataFrame with labels (uses stored labels if None)
            
        Returns:
            Dictionary mapping label to weight
        """
        if labels_df is None:
            labels_df = self._labels
        
        if labels_df is None or labels_df.empty:
            raise ValueError("No labels available. Run generate_labels() first.")
        
        label_counts = labels_df['label'].value_counts()
        total = len(labels_df)
        n_classes = len(label_counts)
        
        weights = {}
        for label in [-1, 0, 1]:
            count = label_counts.get(label, 0)
            if count > 0:
                weights[label] = total / (n_classes * count)
            else:
                weights[label] = 0.0
        
        return weights
    
    def get_sample_weights(
        self,
        labels_df: Optional[pd.DataFrame] = None,
        method: str = 'balanced'
    ) -> pd.Series:
        """
        Calculate sample weights for each sample.
        
        Sample weights can be used during training to give more
        importance to underrepresented classes.
        
        Methods:
        - 'balanced': Inverse of class frequency
        - 'sqrt': Square root of inverse frequency (less aggressive)
        - 'effective': Effective number of samples method
        
        Args:
            labels_df: DataFrame with labels
            method: Weighting method
            
        Returns:
            Series of sample weights
        """
        if labels_df is None:
            labels_df = self._labels
        
        if labels_df is None or labels_df.empty:
            raise ValueError("No labels available. Run generate_labels() first.")
        
        class_weights = self.get_class_weights(labels_df)
        
        if method == 'balanced':
            sample_weights = labels_df['label'].map(class_weights)
        elif method == 'sqrt':
            sqrt_weights = {k: np.sqrt(v) for k, v in class_weights.items()}
            sample_weights = labels_df['label'].map(sqrt_weights)
        elif method == 'effective':
            # Effective number of samples method
            beta = 0.9999
            effective_weights = {}
            label_counts = labels_df['label'].value_counts()
            for label in [-1, 0, 1]:
                count = label_counts.get(label, 0)
                if count > 0:
                    effective_weights[label] = (1 - beta) / (1 - beta ** count)
                else:
                    effective_weights[label] = 0.0
            # Normalize
            total = sum(effective_weights.values())
            effective_weights = {k: v / total * 3 for k, v in effective_weights.items()}
            sample_weights = labels_df['label'].map(effective_weights)
        else:
            raise ValueError(f"Unknown method: {method}")
        
        return sample_weights
    
    def check_class_imbalance(
        self,
        labels_df: Optional[pd.DataFrame] = None,
        threshold: float = 0.3
    ) -> Tuple[bool, str]:
        """
        Check if there's significant class imbalance.
        
        Class imbalance is considered significant if:
        - Any class has less than threshold * average_count samples
        - Or class_balance ratio is below threshold
        
        Args:
            labels_df: DataFrame with labels
            threshold: Imbalance threshold (default: 0.3)
            
        Returns:
            Tuple of (is_imbalanced, message)
        """
        stats = self.get_statistics(labels_df)
        
        if stats.class_balance < threshold:
            return True, (
                f"Class imbalance detected. Balance ratio: {stats.class_balance:.2f}. "
                f"Distribution: UP={stats.up_pct:.1%}, FLAT={stats.flat_pct:.1%}, "
                f"DOWN={stats.down_pct:.1%}. Consider using class weights or resampling."
            )
        
        # Check minimum samples per class
        min_count = min(stats.up_count, stats.down_count, stats.flat_count)
        if min_count < self.config.min_samples:
            return True, (
                f"Insufficient samples in some classes. "
                f"Minimum class count: {min_count} (required: {self.config.min_samples})"
            )
        
        return False, "Class distribution is acceptable."
    
    def suggest_threshold_adjustment(
        self,
        df: pd.DataFrame,
        target_balance: float = 0.5
    ) -> Dict[str, float]:
        """
        Suggest threshold adjustments to achieve better class balance.
        
        This method analyzes the return distribution and suggests
        thresholds that would result in more balanced classes.
        
        Args:
            df: DataFrame with OHLCV data
            target_balance: Target class balance ratio
            
        Returns:
            Dictionary with suggested thresholds
        """
        # Calculate all forward returns
        prices = df[self.config.price_column]
        forward_returns = self._calculate_forward_returns(prices)
        forward_returns = forward_returns.dropna()
        
        # Get percentiles
        p_up = forward_returns.quantile(1 - target_balance)
        p_down = forward_returns.quantile(target_balance)
        
        # Current distribution with suggested thresholds
        up_count = (forward_returns > p_up).sum()
        down_count = (forward_returns < p_down).sum()
        flat_count = len(forward_returns) - up_count - down_count
        
        suggested_balance = min(up_count, down_count, flat_count) / max(up_count, down_count, flat_count)
        
        return {
            'current_up_threshold': self.config.up_threshold,
            'current_down_threshold': self.config.down_threshold,
            'suggested_up_threshold': abs(p_up),
            'suggested_down_threshold': abs(p_down),
            'current_balance': self._statistics.class_balance if self._statistics else None,
            'suggested_balance': suggested_balance,
            'up_count_with_suggested': up_count,
            'down_count_with_suggested': down_count,
            'flat_count_with_suggested': flat_count
        }
    
    def generate_labels_with_features(
        self,
        features_df: pd.DataFrame,
        price_df: pd.DataFrame
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Generate labels aligned with feature data.
        
        This method ensures that features and labels are properly aligned,
        handling the forward shift for labels.
        
        Args:
            features_df: DataFrame with generated features
            price_df: DataFrame with OHLCV price data
            
        Returns:
            Tuple of (aligned_features, aligned_labels)
        """
        # Generate labels from price data
        labels_df = self.generate_labels(price_df)
        
        # Find common dates
        common_dates = features_df.index.intersection(labels_df.index)
        
        if len(common_dates) == 0:
            raise ValueError("No common dates between features and labels")
        
        # Align both DataFrames
        aligned_features = features_df.loc[common_dates]
        aligned_labels = labels_df.loc[common_dates]
        
        # Remove rows with NaN in features
        valid_mask = ~aligned_features.isna().any(axis=1)
        aligned_features = aligned_features[valid_mask]
        aligned_labels = aligned_labels[valid_mask]
        
        # Remove rows with NaN in labels
        valid_mask = ~aligned_labels.isna().any(axis=1)
        aligned_features = aligned_features[valid_mask]
        aligned_labels = aligned_labels[valid_mask]
        
        logger.info(f"Aligned {len(aligned_features)} samples with features and labels")
        
        return aligned_features, aligned_labels


def create_label_generator(
    up_threshold: float = 0.01,
    down_threshold: float = 0.01,
    forward_days: int = 1
) -> LabelGenerator:
    """
    Convenience function to create a LabelGenerator.
    
    Args:
        up_threshold: Minimum return for UP label
        down_threshold: Minimum negative return for DOWN label
        forward_days: Number of days forward
        
    Returns:
        Configured LabelGenerator instance
    """
    return LabelGenerator(
        up_threshold=up_threshold,
        down_threshold=down_threshold,
        forward_days=forward_days
    )


def generate_labels_from_data(
    df: pd.DataFrame,
    up_threshold: float = 0.01,
    down_threshold: float = 0.01,
    forward_days: int = 1
) -> Tuple[pd.DataFrame, LabelStatistics]:
    """
    Convenience function to generate labels from OHLCV data.
    
    Args:
        df: DataFrame with OHLCV data
        up_threshold: Minimum return for UP label
        down_threshold: Minimum negative return for DOWN label
        forward_days: Number of days forward
        
    Returns:
        Tuple of (labels_df, statistics)
    """
    generator = LabelGenerator(
        up_threshold=up_threshold,
        down_threshold=down_threshold,
        forward_days=forward_days
    )
    
    labels_df = generator.generate_labels(df)
    stats = generator.get_statistics(labels_df)
    
    return labels_df, stats
