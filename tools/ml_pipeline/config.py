"""
Configuration module for ML Pipeline.

This module contains all configuration classes and default parameters
for the stock movement prediction system.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from enum import Enum
import yaml
import os


class PredictionDirection(Enum):
    """Enum for prediction direction classes."""
    UP = 1
    FLAT = 0
    DOWN = -1


class WeightingMethod(Enum):
    """Enum for ensemble weighting methods."""
    EQUAL = "equal"
    PERFORMANCE_BASED = "performance_based"
    OPTIMIZED = "optimized"


@dataclass
class DataConfig:
    """Configuration for data collection and preprocessing."""
    start_date: str = "2020-01-01"
    end_date: str = "2024-12-31"
    train_test_split: float = 0.8
    stocks_universe: str = "fno"  # 'fno', 'nifty50', 'custom'
    data_source: str = "yfinance"
    custom_stocks: List[str] = field(default_factory=list)
    

@dataclass
class TechnicalFeatureConfig:
    """Configuration for technical indicator features."""
    enabled: bool = True
    rsi_periods: List[int] = field(default_factory=lambda: [5, 10, 14])
    ema_periods: List[int] = field(default_factory=lambda: [9, 21, 50])
    sma_periods: List[int] = field(default_factory=lambda: [20, 50])
    macd_params: List[int] = field(default_factory=lambda: [12, 26, 9])
    bollinger_params: List[int] = field(default_factory=lambda: [20, 2])
    atr_period: int = 14
    stochastic_params: List[int] = field(default_factory=lambda: [5, 5])
    adx_period: int = 14
    williams_r_period: int = 14
    supertrend_params: List[float] = field(default_factory=lambda: [14, 2.5])


@dataclass
class PriceFeatureConfig:
    """Configuration for price-based features."""
    enabled: bool = True
    return_periods: List[int] = field(default_factory=lambda: [1, 5, 10, 20])
    volatility_periods: List[int] = field(default_factory=lambda: [5, 10, 20])
    include_gap_features: bool = True
    include_swing_detection: bool = True
    include_price_position: bool = True


@dataclass
class VolumeFeatureConfig:
    """Configuration for volume-based features."""
    enabled: bool = True
    volume_ratio_periods: List[int] = field(default_factory=lambda: [5, 10, 20])
    include_obv: bool = True
    include_accumulation_distribution: bool = True
    include_chaikin_money_flow: bool = True
    include_volume_spikes: bool = True


@dataclass
class MarketFeatureConfig:
    """Configuration for market-wide features."""
    enabled: bool = True
    index_symbols: List[str] = field(default_factory=lambda: ["^NSEI", "^NSEBANK"])
    include_beta: bool = True
    include_correlation: bool = True
    include_relative_strength: bool = True


@dataclass
class FeatureConfig:
    """Master configuration for all feature categories."""
    technical: TechnicalFeatureConfig = field(default_factory=TechnicalFeatureConfig)
    price: PriceFeatureConfig = field(default_factory=PriceFeatureConfig)
    volume: VolumeFeatureConfig = field(default_factory=VolumeFeatureConfig)
    market: MarketFeatureConfig = field(default_factory=MarketFeatureConfig)


@dataclass
class LabelConfig:
    """Configuration for label generation."""
    up_threshold: float = 1.0  # > 1% = UP
    down_threshold: float = -1.0  # < -1% = DOWN
    # Between -1% and +1% = FLAT


@dataclass
class XGBoostParams:
    """XGBoost model hyperparameters."""
    n_estimators: int = 500
    max_depth: int = 6
    learning_rate: float = 0.05
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    min_child_weight: int = 3
    gamma: float = 0.1
    reg_alpha: float = 0.1
    reg_lambda: float = 1.0
    random_state: int = 42
    n_jobs: int = -1
    use_label_encoder: bool = False
    eval_metric: str = "mlogloss"


@dataclass
class RandomForestParams:
    """Random Forest model hyperparameters."""
    n_estimators: int = 500
    max_depth: int = 10
    min_samples_split: int = 5
    min_samples_leaf: int = 2
    max_features: str = "sqrt"
    bootstrap: bool = True
    random_state: int = 42
    n_jobs: int = -1
    class_weight: str = "balanced"


@dataclass
class LightGBMParams:
    """LightGBM model hyperparameters."""
    n_estimators: int = 500
    max_depth: int = 8
    learning_rate: float = 0.05
    num_leaves: int = 31
    min_child_samples: int = 20
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    reg_alpha: float = 0.1
    reg_lambda: float = 1.0
    random_state: int = 42
    n_jobs: int = -1
    verbose: int = -1


@dataclass
class EnsembleConfig:
    """Configuration for ensemble model."""
    voting: str = "soft"  # 'soft' or 'hard'
    weighting_method: WeightingMethod = WeightingMethod.EQUAL
    custom_weights: Dict[str, float] = field(default_factory=lambda: {
        "xgboost": 0.33,
        "random_forest": 0.33,
        "lightgbm": 0.34
    })
    min_agreement_threshold: float = 0.6  # Minimum agreement to make prediction


@dataclass
class ModelConfig:
    """Master configuration for all models."""
    xgboost: XGBoostParams = field(default_factory=XGBoostParams)
    random_forest: RandomForestParams = field(default_factory=RandomForestParams)
    lightgbm: LightGBMParams = field(default_factory=LightGBMParams)
    ensemble: EnsembleConfig = field(default_factory=EnsembleConfig)


@dataclass
class TrainingConfig:
    """Configuration for model training."""
    n_splits: int = 5
    early_stopping_rounds: int = 50
    purge_days: int = 5  # Days to purge between train and validation
    hyperparameter_tuning_enabled: bool = True
    n_trials: int = 100
    timeout: int = 3600  # 1 hour max for tuning


@dataclass
class EvaluationConfig:
    """Configuration for model evaluation."""
    confidence_threshold: float = 0.6
    metrics: List[str] = field(default_factory=lambda: [
        "accuracy", "precision", "recall", "f1", "log_loss", "roc_auc"
    ])


@dataclass
class BacktestConfig:
    """Configuration for backtesting."""
    initial_capital: float = 100000.0
    position_size: float = 0.05  # 5% per trade
    transaction_cost: float = 0.001  # 0.1% per trade
    slippage: float = 0.0005  # 0.05% slippage


@dataclass
class OutputConfig:
    """Configuration for output and notifications."""
    save_predictions: bool = True
    save_feature_importance: bool = True
    save_model_artifacts: bool = True
    notification_enabled: bool = True
    telegram_enabled: bool = True


@dataclass
class MLPipelineConfig:
    """
    Master configuration class for the entire ML pipeline.
    
    This class aggregates all configuration sections and provides
    methods for loading from YAML and saving to YAML.
    """
    data: DataConfig = field(default_factory=DataConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    labels: LabelConfig = field(default_factory=LabelConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    training: TrainingConfig = field(default_factory=TrainingConfig)
    evaluation: EvaluationConfig = field(default_factory=EvaluationConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    
    @classmethod
    def from_yaml(cls, path: str) -> "MLPipelineConfig":
        """
        Load configuration from a YAML file.
        
        Args:
            path: Path to the YAML configuration file.
            
        Returns:
            MLPipelineConfig instance with loaded values.
        """
        with open(path, 'r') as f:
            config_dict = yaml.safe_load(f)
        
        return cls.from_dict(config_dict)
    
    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "MLPipelineConfig":
        """
        Create configuration from a dictionary.
        
        Args:
            config_dict: Dictionary with configuration values.
            
        Returns:
            MLPipelineConfig instance with loaded values.
        """
        # Parse nested configurations
        data_config = DataConfig(**config_dict.get('data', {}))
        
        features_dict = config_dict.get('features', {})
        feature_config = FeatureConfig(
            technical=TechnicalFeatureConfig(**features_dict.get('technical', {})),
            price=PriceFeatureConfig(**features_dict.get('price', {})),
            volume=VolumeFeatureConfig(**features_dict.get('volume', {})),
            market=MarketFeatureConfig(**features_dict.get('market', {})),
        )
        
        labels_config = LabelConfig(**config_dict.get('labels', {}))
        
        models_dict = config_dict.get('models', {})
        model_config = ModelConfig(
            xgboost=XGBoostParams(**models_dict.get('xgboost', {})),
            random_forest=RandomForestParams(**models_dict.get('random_forest', {})),
            lightgbm=LightGBMParams(**models_dict.get('lightgbm', {})),
            ensemble=EnsembleConfig(**models_dict.get('ensemble', {})),
        )
        
        training_config = TrainingConfig(**config_dict.get('training', {}))
        evaluation_config = EvaluationConfig(**config_dict.get('evaluation', {}))
        backtest_config = BacktestConfig(**config_dict.get('backtest', {}))
        output_config = OutputConfig(**config_dict.get('output', {}))
        
        return cls(
            data=data_config,
            features=feature_config,
            labels=labels_config,
            models=model_config,
            training=training_config,
            evaluation=evaluation_config,
            backtest=backtest_config,
            output=output_config,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert configuration to a dictionary.
        
        Returns:
            Dictionary representation of the configuration.
        """
        from dataclasses import asdict
        return asdict(self)
    
    def to_yaml(self, path: str) -> None:
        """
        Save configuration to a YAML file.
        
        Args:
            path: Path to save the YAML file.
        """
        config_dict = self.to_dict()
        with open(path, 'w') as f:
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False)
    
    def __str__(self) -> str:
        """String representation of the configuration."""
        return yaml.dump(self.to_dict(), default_flow_style=False, sort_keys=False)


# Default configuration instance
DEFAULT_CONFIG = MLPipelineConfig()


def load_config(config_path: Optional[str] = None) -> MLPipelineConfig:
    """
    Load configuration from file or return default.
    
    Args:
        config_path: Optional path to configuration file.
        
    Returns:
        MLPipelineConfig instance.
    """
    if config_path and os.path.exists(config_path):
        return MLPipelineConfig.from_yaml(config_path)
    return DEFAULT_CONFIG