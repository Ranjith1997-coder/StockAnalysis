"""
XGBoost model implementation for stock movement prediction.

This module provides an XGBoost classifier wrapper that follows the
BaseModel interface for consistent usage across the ML pipeline.
"""

from typing import Any, Dict, Optional
import pandas as pd
import numpy as np

from ml_pipeline.models.base_model import BaseModel
from ml_pipeline.config import XGBoostParams


class XGBoostModel(BaseModel):
    """
    XGBoost classifier for stock movement prediction.
    
    This model uses gradient boosting for classification, which is
    particularly effective for tabular data with mixed feature types.
    
    Attributes:
        model: XGBClassifier instance.
        model_name: "XGBoost"
        model_params: XGBoost hyperparameters.
    """
    
    def __init__(self, model_params: Optional[Dict] = None):
        """
        Initialize the XGBoost model.
        
        Args:
            model_params: Dictionary of XGBoost hyperparameters.
                         If None, uses default XGBoostParams.
        """
        # Use default params if not provided
        if model_params is None:
            default_params = XGBoostParams()
            model_params = {
                "n_estimators": default_params.n_estimators,
                "max_depth": default_params.max_depth,
                "learning_rate": default_params.learning_rate,
                "subsample": default_params.subsample,
                "colsample_bytree": default_params.colsample_bytree,
                "min_child_weight": default_params.min_child_weight,
                "gamma": default_params.gamma,
                "reg_alpha": default_params.reg_alpha,
                "reg_lambda": default_params.reg_lambda,
                "random_state": default_params.random_state,
                "n_jobs": default_params.n_jobs,
                "use_label_encoder": default_params.use_label_encoder,
                "eval_metric": default_params.eval_metric,
            }
        
        super().__init__(model_name="XGBoost", model_params=model_params)
        self.model = self._create_model()
    
    def _create_model(self) -> Any:
        """
        Create the XGBClassifier instance.
        
        Returns:
            XGBClassifier with configured hyperparameters.
        """
        from xgboost import XGBClassifier
        
        return XGBClassifier(**self.model_params)
    
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
        Train the XGBoost model.
        
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
        
        # Convert labels from [-1, 0, 1] to [0, 1, 2] for XGBoost
        y_train_encoded = y_train + 1  # -1->0, 0->1, 1->2
        
        # Prepare eval set if validation data provided
        eval_set = None
        if X_val is not None and y_val is not None:
            y_val_encoded = y_val + 1
            eval_set = [(X_val, y_val_encoded)]
        
        # Train the model
        train_params = {
            "X": X_train,
            "y": y_train_encoded,
            "eval_set": eval_set,
            "verbose": verbose,
        }
        
        if eval_set is not None:
            train_params["early_stopping_rounds"] = early_stopping_rounds
        
        # Add any additional kwargs
        train_params.update(kwargs)
        
        self.model.fit(**train_params)
        self.is_trained = True
        
        # Calculate training metrics
        train_predictions = self.predict(X_train)
        train_accuracy = np.mean(train_predictions == y_train.values)
        
        metrics = {
            "train_accuracy": train_accuracy,
            "best_iteration": getattr(self.model, "best_iteration", None),
            "best_score": getattr(self.model, "best_score", None),
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
        
        # XGBoost predicts 0, 1, 2; convert back to -1, 0, 1
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
    
    def get_feature_importance(self) -> pd.DataFrame:
        """
        Get feature importance scores.
        
        Returns:
            DataFrame with columns 'feature' and 'importance', sorted by importance.
        """
        if not self.is_trained:
            raise ValueError("Model must be trained before getting feature importance.")
        
        importance_dict = self.model.get_booster().get_score(importance_type="gain")
        
        # Create DataFrame with all features (some might have 0 importance)
        importance_data = []
        for feature in self._feature_names:
            importance_data.append({
                "feature": feature,
                "importance": importance_dict.get(feature, 0.0),
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
            SHAP values array of shape (n_samples, n_features, n_classes).
        """
        try:
            import shap
            explainer = shap.TreeExplainer(self.model)
            shap_values = explainer.shap_values(X)
            return shap_values
        except ImportError:
            raise ImportError("SHAP is required for SHAP values. Install with: pip install shap")


# Convenience function for quick model creation
def create_xgboost_model(
    n_estimators: int = 500,
    max_depth: int = 6,
    learning_rate: float = 0.05,
    **kwargs
) -> XGBoostModel:
    """
    Create an XGBoost model with specified parameters.
    
    Args:
        n_estimators: Number of boosting rounds.
        max_depth: Maximum tree depth.
        learning_rate: Boosting learning rate.
        **kwargs: Additional XGBoost parameters.
        
    Returns:
        Configured XGBoostModel instance.
    """
    params = {
        "n_estimators": n_estimators,
        "max_depth": max_depth,
        "learning_rate": learning_rate,
        **kwargs
    }
    return XGBoostModel(model_params=params)
