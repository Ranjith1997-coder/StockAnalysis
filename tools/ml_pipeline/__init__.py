"""
ML Pipeline for Stock Movement Prediction

This module provides a complete machine learning pipeline for predicting
stock price direction using ensemble methods (XGBoost, Random Forest, LightGBM).

Key Components:
- data: Data collection and preprocessing
- features: Feature engineering modules
- models: ML model implementations
- training: Training and hyperparameter tuning
- evaluation: Model evaluation and backtesting
- deployment: Model deployment and prediction services
"""

__version__ = "0.1.0"
__author__ = "StockAnalysis Team"

from ml_pipeline.config import MLPipelineConfig
from ml_pipeline.models.ensemble_model import EnsembleModel
from ml_pipeline.models.xgboost_model import XGBoostModel
from ml_pipeline.models.random_forest_model import RandomForestModel
from ml_pipeline.models.lightgbm_model import LightGBMModel

__all__ = [
    "MLPipelineConfig",
    "EnsembleModel",
    "XGBoostModel",
    "RandomForestModel",
    "LightGBMModel",
]
