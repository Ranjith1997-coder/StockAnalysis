"""
Training module for stock movement prediction.

This module provides:
- Trainer: Main training pipeline with cross-validation
- TrainingConfig: Configuration for training
- TrainingResult: Container for training results
"""

from ml_pipeline.training.trainer import (
    Trainer,
    TrainingConfig,
    TrainingResult,
    train_stock_model
)

__all__ = [
    'Trainer',
    'TrainingConfig',
    'TrainingResult',
    'train_stock_model'
]
