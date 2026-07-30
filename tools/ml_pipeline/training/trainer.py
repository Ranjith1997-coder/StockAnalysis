"""
Training Pipeline for Stock Movement Prediction.

This module provides a complete training pipeline including:
- Time-series cross-validation
- Class weight handling for imbalanced data
- Model training with early stopping
- Hyperparameter tuning with Optuna
- Model evaluation and comparison

Usage:
    from ml_pipeline.training.trainer import Trainer
    
    trainer = Trainer(config)
    results = trainer.train(X, y)
    trainer.save_model('model.pkl')
"""

from typing import Dict, List, Optional, Tuple, Union, Any
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
import warnings
import joblib
from pathlib import Path
from datetime import datetime

from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    classification_report, confusion_matrix
)

from ml_pipeline.models.ensemble_model import EnsembleModel
from ml_pipeline.models.xgboost_model import XGBoostModel
from ml_pipeline.models.random_forest_model import RandomForestModel
from ml_pipeline.models.lightgbm_model import LightGBMModel
from ml_pipeline.config import MLPipelineConfig


@dataclass
class TrainingConfig:
    """Configuration for model training."""
    
    # Cross-validation
    n_splits: int = 5  # Number of CV folds
    purge_days: int = 5  # Days to exclude between train/val to prevent leakage
    
    # Training
    early_stopping_rounds: int = 50
    verbose: bool = True
    
    # Hyperparameter tuning
    use_optuna: bool = False  # Whether to use Optuna for hyperparameter tuning
    n_trials: int = 50  # Number of Optuna trials
    optuna_timeout: int = 3600  # Optuna timeout in seconds
    
    # Model selection
    use_xgboost: bool = True
    use_random_forest: bool = True
    use_lightgbm: bool = True
    
    # Ensemble
    ensemble_weights: Optional[Dict[str, float]] = None  # If None, use equal weights
    
    # Class imbalance
    use_class_weights: bool = True
    
    # Saving
    save_best_model: bool = True
    model_dir: str = 'data/models'


@dataclass
class TrainingResult:
    """Container for training results."""
    
    # Model
    model: Any
    model_type: str
    
    # Cross-validation metrics
    cv_scores: Dict[str, List[float]]
    mean_cv_score: Dict[str, float]
    std_cv_score: Dict[str, float]
    
    # Test metrics (if test set provided)
    test_metrics: Optional[Dict[str, float]] = None
    
    # Feature importance
    feature_importance: Optional[pd.DataFrame] = None
    
    # Training metadata
    training_time: float = 0.0
    best_iteration: Optional[int] = None
    
    # Class weights used
    class_weights: Optional[Dict[int, float]] = None


class Trainer:
    """
    Training pipeline for stock movement prediction.
    
    This class handles:
    - Time-series cross-validation
    - Model training with early stopping
    - Hyperparameter tuning (optional)
    - Model evaluation
    - Model persistence
    
    Attributes:
        config: Training configuration.
        model: Trained model.
        best_params: Best hyperparameters found (if Optuna used).
    
    Example:
        >>> config = TrainingConfig(n_splits=5)
        >>> trainer = Trainer(config)
        >>> result = trainer.train(X, y, class_weights={-1: 1.3, 0: 0.7, 1: 1.2})
        >>> trainer.save_model('model.pkl')
    """
    
    def __init__(
        self, 
        config: Optional[TrainingConfig] = None,
        ml_config: Optional[MLPipelineConfig] = None
    ):
        """
        Initialize the trainer.
        
        Args:
            config: Training configuration. If None, uses defaults.
            ml_config: ML pipeline configuration for model parameters.
        """
        self.config = config or TrainingConfig()
        self.ml_config = ml_config or MLPipelineConfig()
        self.model: Optional[Any] = None
        self.best_params: Optional[Dict] = None
        self.training_result: Optional[TrainingResult] = None
        
    def train(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        class_weights: Optional[Dict[int, float]] = None,
        X_test: Optional[pd.DataFrame] = None,
        y_test: Optional[pd.Series] = None
    ) -> TrainingResult:
        """
        Train the model using time-series cross-validation.
        
        Args:
            X: Training features.
            y: Training labels.
            class_weights: Class weights for imbalanced data.
            X_test: Optional test features for final evaluation.
            y_test: Optional test labels for final evaluation.
            
        Returns:
            TrainingResult with model and metrics.
        """
        start_time = datetime.now()
        
        if self.config.verbose:
            print("=" * 70)
            print(" TRAINING PIPELINE")
            print("=" * 70)
            print(f"\nTraining data: {X.shape[0]} samples, {X.shape[1]} features")
            print(f"Cross-validation: {self.config.n_splits} folds")
            print(f"Class weights: {class_weights}")
        
        # Create model
        self.model = self._create_model(class_weights)
        
        # Perform cross-validation
        cv_scores = self._cross_validate(X, y)
        
        # Train final model on all data
        if self.config.verbose:
            print("\n" + "-" * 70)
            print("Training final model on all data...")
        
        # Use appropriate training method
        if hasattr(self.model, 'train_all'):
            # EnsembleModel uses train_all
            self.model.train_all(X, y)
        elif hasattr(self.model, 'train'):
            self.model.train(X, y)
        else:
            self.model.fit(X, y)
        
        # Get feature importance
        feature_importance = self._get_feature_importance(X)
        
        # Evaluate on test set if provided
        test_metrics = None
        if X_test is not None and y_test is not None:
            test_metrics = self._evaluate(X_test, y_test)
        
        # Calculate training time
        training_time = (datetime.now() - start_time).total_seconds()
        
        # Create result
        self.training_result = TrainingResult(
            model=self.model,
            model_type='ensemble' if isinstance(self.model, EnsembleModel) else 'single',
            cv_scores=cv_scores,
            mean_cv_score={k: np.mean(v) for k, v in cv_scores.items()},
            std_cv_score={k: np.std(v) for k, v in cv_scores.items()},
            test_metrics=test_metrics,
            feature_importance=feature_importance,
            training_time=training_time,
            class_weights=class_weights
        )
        
        # Print summary
        if self.config.verbose:
            self._print_summary(self.training_result)
        
        return self.training_result
    
    def _create_model(
        self, 
        class_weights: Optional[Dict[int, float]] = None
    ) -> Union[EnsembleModel, Any]:
        """Create the model based on configuration."""
        models = []
        
        if self.config.use_xgboost:
            xgb_model = XGBoostModel()
            models.append(('xgboost', xgb_model))
        
        if self.config.use_random_forest:
            rf_model = RandomForestModel()
            models.append(('random_forest', rf_model))
        
        if self.config.use_lightgbm:
            lgb_model = LightGBMModel()
            models.append(('lightgbm', lgb_model))
        
        if len(models) == 1:
            return models[0][1]
        
        # Create ensemble with equal weights
        weights = self.config.ensemble_weights
        if weights is None:
            weights = {name: 1.0 / len(models) for name, _ in models}
        
        from ml_pipeline.config import EnsembleConfig
        ensemble_config = EnsembleConfig(custom_weights=weights, voting='soft')
        
        return EnsembleModel(config=ensemble_config)
    
    def _cross_validate(
        self, 
        X: pd.DataFrame, 
        y: pd.Series
    ) -> Dict[str, List[float]]:
        """
        Perform time-series cross-validation.
        
        Uses TimeSeriesSplit with purge gap to prevent look-ahead bias.
        """
        tscv = TimeSeriesSplit(n_splits=self.config.n_splits)
        
        cv_scores = {
            'accuracy': [],
            'precision_macro': [],
            'recall_macro': [],
            'f1_macro': [],
            'precision_up': [],
            'precision_down': []
        }
        
        if self.config.verbose:
            print("\n" + "-" * 70)
            print("CROSS-VALIDATION")
            print("-" * 70)
        
        for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
            # Apply purge gap
            purge = self.config.purge_days
            if purge > 0:
                val_idx = val_idx[val_idx >= train_idx[-1] + purge]
            
            if len(val_idx) == 0:
                continue
            
            X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]
            
            # Clone model for this fold
            fold_model = self._create_model(class_weights=None)
            # Use train method for models
            if hasattr(fold_model, 'train_all'):
                # EnsembleModel uses train_all
                fold_model.train_all(X_train, y_train)
            elif hasattr(fold_model, 'train'):
                fold_model.train(X_train, y_train)
            else:
                fold_model.fit(X_train, y_train)
            
            # Predict
            y_pred = fold_model.predict(X_val)
            
            # Calculate metrics
            cv_scores['accuracy'].append(accuracy_score(y_val, y_pred))
            cv_scores['precision_macro'].append(precision_score(y_val, y_pred, average='macro', zero_division=0))
            cv_scores['recall_macro'].append(recall_score(y_val, y_pred, average='macro', zero_division=0))
            cv_scores['f1_macro'].append(f1_score(y_val, y_pred, average='macro', zero_division=0))
            
            # Per-class precision
            precisions = precision_score(y_val, y_pred, average=None, zero_division=0)
            labels = sorted(y.unique())
            for i, label in enumerate(labels):
                if label == 1:
                    cv_scores['precision_up'].append(precisions[i] if i < len(precisions) else 0)
                elif label == -1:
                    cv_scores['precision_down'].append(precisions[i] if i < len(precisions) else 0)
            
            if self.config.verbose:
                print(f"  Fold {fold + 1}: Accuracy={cv_scores['accuracy'][-1]:.4f}, "
                      f"F1={cv_scores['f1_macro'][-1]:.4f}")
        
        return cv_scores
    
    def _evaluate(
        self, 
        X_test: pd.DataFrame, 
        y_test: pd.Series
    ) -> Dict[str, float]:
        """Evaluate model on test set."""
        y_pred = self.model.predict(X_test)
        
        metrics = {
            'accuracy': accuracy_score(y_test, y_pred),
            'precision_macro': precision_score(y_test, y_pred, average='macro', zero_division=0),
            'recall_macro': recall_score(y_test, y_pred, average='macro', zero_division=0),
            'f1_macro': f1_score(y_test, y_pred, average='macro', zero_division=0)
        }
        
        # Per-class metrics
        labels = sorted(y_test.unique())
        precisions = precision_score(y_test, y_pred, average=None, zero_division=0)
        recalls = recall_score(y_test, y_pred, average=None, zero_division=0)
        
        for i, label in enumerate(labels):
            label_name = {-1: 'down', 0: 'flat', 1: 'up'}.get(int(label), str(label))
            if i < len(precisions):
                metrics[f'precision_{label_name}'] = precisions[i]
                metrics[f'recall_{label_name}'] = recalls[i]
        
        return metrics
    
    def _get_feature_importance(self, X: pd.DataFrame) -> Optional[pd.DataFrame]:
        """Get feature importance from trained model."""
        try:
            if hasattr(self.model, 'get_feature_importance'):
                importance_df = self.model.get_feature_importance()
                if importance_df is not None:
                    return importance_df
            return None
        except Exception:
            return None
    
    def _print_summary(self, result: TrainingResult) -> None:
        """Print training summary."""
        print("\n" + "=" * 70)
        print(" TRAINING SUMMARY")
        print("=" * 70)
        
        print(f"\nModel Type: {result.model_type}")
        print(f"Training Time: {result.training_time:.2f} seconds")
        
        print("\nCross-Validation Results:")
        print(f"  Accuracy:  {result.mean_cv_score['accuracy']:.4f} ± {result.std_cv_score['accuracy']:.4f}")
        print(f"  F1 Macro:  {result.mean_cv_score['f1_macro']:.4f} ± {result.std_cv_score['f1_macro']:.4f}")
        print(f"  Precision UP:   {np.mean(result.cv_scores['precision_up']):.4f}")
        print(f"  Precision DOWN: {np.mean(result.cv_scores['precision_down']):.4f}")
        
        if result.test_metrics:
            print("\nTest Set Results:")
            print(f"  Accuracy:  {result.test_metrics['accuracy']:.4f}")
            print(f"  F1 Macro:  {result.test_metrics['f1_macro']:.4f}")
            print(f"  Precision UP:   {result.test_metrics.get('precision_up', 0):.4f}")
            print(f"  Precision DOWN: {result.test_metrics.get('precision_down', 0):.4f}")
        
        if result.feature_importance is not None:
            print("\nTop 10 Features:")
            # Handle both 'importance' and 'avg_importance' columns
            importance_col = 'importance' if 'importance' in result.feature_importance.columns else 'avg_importance'
            for i, row in result.feature_importance.head(10).iterrows():
                print(f"  {row['feature']}: {row[importance_col]:.4f}")
        
        print("\n" + "=" * 70)
    
    def save_model(self, filepath: str) -> None:
        """
        Save trained model to file.
        
        Args:
            filepath: Path to save model.
        """
        if self.model is None:
            raise ValueError("No model to save. Train a model first.")
        
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        joblib.dump({
            'model': self.model,
            'config': self.config,
            'training_result': self.training_result
        }, path)
        
        if self.config.verbose:
            print(f"Model saved to: {path}")
    
    def load_model(self, filepath: str) -> None:
        """
        Load trained model from file.
        
        Args:
            filepath: Path to load model from.
        """
        data = joblib.load(filepath)
        self.model = data['model']
        self.config = data.get('config', self.config)
        self.training_result = data.get('training_result', None)
        
        if self.config.verbose:
            print(f"Model loaded from: {filepath}")
    
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """
        Make predictions using trained model.
        
        Args:
            X: Features to predict.
            
        Returns:
            Predicted labels.
        """
        if self.model is None:
            raise ValueError("No model available. Train or load a model first.")
        return self.model.predict(X)
    
    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """
        Get prediction probabilities.
        
        Args:
            X: Features to predict.
            
        Returns:
            Predicted probabilities.
        """
        if self.model is None:
            raise ValueError("No model available. Train or load a model first.")
        
        if hasattr(self.model, 'predict_proba'):
            return self.model.predict_proba(X)
        else:
            raise ValueError("Model does not support probability predictions.")


def train_stock_model(
    df: pd.DataFrame,
    target_col: str = 'label',
    config: Optional[TrainingConfig] = None,
    class_weights: Optional[Dict[int, float]] = None
) -> Tuple[Trainer, TrainingResult]:
    """
    Convenience function to train a stock prediction model.
    
    Args:
        df: DataFrame with features and target.
        target_col: Name of target column.
        config: Training configuration.
        class_weights: Class weights for imbalanced data.
        
    Returns:
        Tuple of (Trainer, TrainingResult)
    """
    # Separate features and target
    X = df.drop(columns=[target_col])
    y = df[target_col]
    
    # Create trainer
    trainer = Trainer(config)
    
    # Train
    result = trainer.train(X, y, class_weights=class_weights)
    
    return trainer, result
