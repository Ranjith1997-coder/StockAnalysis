"""
LightGBM model implementation for stock movement prediction.

This module provides a LightGBM classifier wrapper that follows the
BaseModel interface for consistent usage across the ML pipeline.
"""

from typing import Any, Dict, Optional
import pandas as pd
import numpy as np

from ml_pipeline.models.base_model import BaseModel
from ml_pipeline.config import LightGBMParams


class LightGBMModel(BaseModel):
    """
    LightGBM classifier for stock movement prediction.
    
    LightGBM is a gradient boosting framework that uses tree-based learning
    algorithms. It's designed for efficiency and can handle large datasets.
    
    Attributes:
        model: LGBMClassifier instance.
        model_name: "LightGBM"
        model_params: LightGBM hyperparameters.
    """
    
    def __init__(self, model_params: Optional[Dict] = None):
        """
        Initialize the LightGBM model.
        
        Args:
            model_params: Dictionary of LightGBM hyperparameters.
                         If None, uses default LightGBMParams.
        """
        # Use default params if not provided
        if model_params is None:
            default_params = LightGBMParams()
            model_params = {
                "n_estimators": default_params.n_estimators,
                "max_depth": default_params.max_depth,
                "learning_rate": default_params.learning_rate,
                "num_leaves": default_params.num_leaves,
                "min_child_samples": default_params.min_child_samples,
                "subsample": default_params.subsample,
                "colsample_bytree": default_params.colsample_bytree,
                "reg_alpha": default_params.reg_alpha,
                "reg_lambda": default_params.reg_lambda,
                "random_state": default_params.random_state,
                "n_jobs": default_params.n_jobs,
                "verbose": default_params.verbose,
            }
        
        super().__init__(model_name="LightGBM", model_params=model_params)
        self.model = self._create_model()
    
    def _create_model(self) -> Any:
        """
        Create the LGBMClassifier instance.
        
        Returns:
            LGBMClassifier with configured hyperparameters.
        """
        from lightgbm import LGBMClassifier
        
        return LGBMClassifier(**self.model_params)
    
    def train(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        X_val: Optional[pd.DataFrame] = None,
        y_val: Optional[pd.Series] = None,
        early_stopping_rounds: int = 50,
        verbose: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Train the LightGBM model.
        
        Args:
            X_train: Training features.
            y_train: Training labels (should be -1, 0, 1 for DOWN, FLAT, UP).
            X_val: Optional validation features for early stopping.
            y_val: Optional validation labels for early stopping.
            early_stopping_rounds: Number of rounds for early stopping.
            verbose: Whether to print training progress.
            **kwargs: Additional arguments passed to fit().
            
        Returns:
            Dictionary containing training metrics and history.
        """
        # Store feature names
        self._feature_names = list(X_train.columns)
        
        # Convert labels from [-1, 0, 1] to [0, 1, 2] for LightGBM
        y_train_encoded = y_train + 1  # -1->0, 0->1, 1->2
        
        # Prepare eval set if validation data provided
        eval_set = None
        eval_metric = "multi_logloss"
        
        if X_val is not None and y_val is not None:
            y_val_encoded = y_val + 1
            eval_set = [(X_val, y_val_encoded)]
        
        # Train the model
        fit_params = {
            "eval_metric": eval_metric,
        }
        
        if eval_set is not None:
            fit_params["eval_set"] = eval_set
            # Use callbacks for early stopping in newer LightGBM versions
            try:
                from lightgbm import early_stopping
                callbacks = [early_stopping(stopping_rounds=early_stopping_rounds, verbose=verbose)]
                fit_params["callbacks"] = callbacks
            except ImportError:
                # Fallback for older versions
                fit_params["early_stopping_rounds"] = early_stopping_rounds
        
        # Add any additional kwargs, but filter out unsupported arguments
        supported_fit_params = {'eval_metric', 'eval_set', 'callbacks', 'early_stopping_rounds'}
        for key, value in kwargs.items():
            if key in supported_fit_params:
                fit_params[key] = value
        
        self.model.fit(X_train, y_train_encoded, **fit_params)
        self.is_trained = True
        
        # Calculate training metrics
        train_predictions = self.predict(X_train)
        train_accuracy = np.mean(train_predictions == y_train.values)
        
        metrics = {
            "train_accuracy": train_accuracy,
            "best_iteration": getattr(self.model, "best_iteration_", None),
            "best_score": getattr(self.model, "best_score_", None),
        }
        
        # Calculate validation metrics if available
        if X_val is not None and y_val is not None:
            val_predictions = self.predict(X_val)
            val_accuracy = np.mean(val_predictions == y_val.values)
            metrics["val_accuracy"] = val_accuracy
        
        # Create metadata
        self.metadata = self._create_metadata(
            X_train=X_train,
            y_train=y_train,
            validation_metrics=metrics,
        )
        
        return metrics
    
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """
        Make class predictions.
        
        Args:
            X: Input features.
            
        Returns:
            Array of predicted labels (-1, 0, 1 for DOWN, FLAT, UP).
        """
        if not self.is_trained:
            raise ValueError("Model must be trained before making predictions.")
        
        self._validate_features(X)
        
        # LightGBM predicts 0, 1, 2; convert back to -1, 0, 1
        predictions = self.model.predict(X)
        return predictions - 1
    
    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """
        Get probability predictions for each class.
        
        Args:
            X: Input features.
            
        Returns:
            Array of shape (n_samples, 3) with probabilities for [DOWN, FLAT, UP].
        """
        if not self.is_trained:
            raise ValueError("Model must be trained before making predictions.")
        
        self._validate_features(X)
        
        return self.model.predict_proba(X)
    
    def get_feature_importance(self, importance_type: str = "gain") -> pd.DataFrame:
        """
        Get feature importance scores.
        
        Args:
            importance_type: Type of importance ('split', 'gain').
            
        Returns:
            DataFrame with columns 'feature' and 'importance', sorted by importance.
        """
        if not self.is_trained:
            raise ValueError("Model must be trained before getting feature importance.")
        
        importance_values = self.model.booster_.feature_importance(importance_type=importance_type)
        
        importance_data = []
        for feature, importance in zip(self._feature_names, importance_values):
            importance_data.append({
                "feature": feature,
                "importance": importance,
            })
        
        df = pd.DataFrame(importance_data)
        df = df.sort_values("importance", ascending=False).reset_index(drop=True)
        
        return df
    
    def get_shap_values(self, X: pd.DataFrame) -> np.ndarray:
        """
        Calculate SHAP values for interpretability.
        
        Args:
            X: Input features.
            
        Returns:
            SHAP values array.
        """
        try:
            import shap
            explainer = shap.TreeExplainer(self.model)
            shap_values = explainer.shap_values(X)
            return shap_values
        except ImportError:
            raise ImportError("SHAP is required for SHAP values. Install with: pip install shap")


# Convenience function for quick model creation
def create_lightgbm_model(
    n_estimators: int = 500,
    max_depth: int = 8,
    learning_rate: float = 0.05,
    num_leaves: int = 31,
    **kwargs
) -> LightGBMModel:
    """
    Create a LightGBM model with specified parameters.
    
    Args:
        n_estimators: Number of boosting rounds.
        max_depth: Maximum tree depth.
        learning_rate: Boosting learning rate.
        num_leaves: Maximum number of leaves in one tree.
        **kwargs: Additional LightGBM parameters.
        
    Returns:
        Configured LightGBMModel instance.
    """
    params = {
        "n_estimators": n_estimators,
        "max_depth": max_depth,
        "learning_rate": learning_rate,
        "num_leaves": num_leaves,
        **kwargs
    }
    return LightGBMModel(model_params=params)
