"""
Random Forest model implementation for stock movement prediction.

This module provides a Random Forest classifier wrapper that follows the
BaseModel interface for consistent usage across the ML pipeline.
"""

from typing import Any, Dict, Optional
import pandas as pd
import numpy as np

from ml_pipeline.models.base_model import BaseModel
from ml_pipeline.config import RandomForestParams


class RandomForestModel(BaseModel):
    """
    Random Forest classifier for stock movement prediction.
    
    This model uses an ensemble of decision trees with bagging,
    providing robust predictions with built-in feature importance.
    
    Attributes:
        model: RandomForestClassifier instance.
        model_name: "RandomForest"
        model_params: Random Forest hyperparameters.
    """
    
    def __init__(self, model_params: Optional[Dict] = None):
        """
        Initialize the Random Forest model.
        
        Args:
            model_params: Dictionary of Random Forest hyperparameters.
                         If None, uses default RandomForestParams.
        """
        # Use default params if not provided
        if model_params is None:
            default_params = RandomForestParams()
            model_params = {
                "n_estimators": default_params.n_estimators,
                "max_depth": default_params.max_depth,
                "min_samples_split": default_params.min_samples_split,
                "min_samples_leaf": default_params.min_samples_leaf,
                "max_features": default_params.max_features,
                "bootstrap": default_params.bootstrap,
                "random_state": default_params.random_state,
                "n_jobs": default_params.n_jobs,
                "class_weight": default_params.class_weight,
            }
        
        super().__init__(model_name="RandomForest", model_params=model_params)
        self.model = self._create_model()
    
    def _create_model(self) -> Any:
        """
        Create the RandomForestClassifier instance.
        
        Returns:
            RandomForestClassifier with configured hyperparameters.
        """
        from sklearn.ensemble import RandomForestClassifier
        
        return RandomForestClassifier(**self.model_params)
    
    def train(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        X_val: Optional[pd.DataFrame] = None,
        y_val: Optional[pd.Series] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Train the Random Forest model.
        
        Args:
            X_train: Training features.
            y_train: Training labels (should be -1, 0, 1 for DOWN, FLAT, UP).
            X_val: Optional validation features (not used for early stopping in RF).
            y_val: Optional validation labels.
            **kwargs: Additional arguments passed to fit().
            
        Returns:
            Dictionary containing training metrics and history.
        """
        # Store feature names
        self._feature_names = list(X_train.columns)
        
        # Convert labels from [-1, 0, 1] to [0, 1, 2] for sklearn
        y_train_encoded = y_train + 1  # -1->0, 0->1, 1->2
        
        # Train the model
        self.model.fit(X_train, y_train_encoded, **kwargs)
        self.is_trained = True
        
        # Calculate training metrics
        train_predictions = self.predict(X_train)
        train_accuracy = np.mean(train_predictions == y_train.values)
        
        metrics = {
            "train_accuracy": train_accuracy,
            "n_estimators": self.model.n_estimators,
            "n_features": self.model.n_features_in_,
            "n_classes": self.model.n_classes_,
        }
        
        # Calculate OOB score if bootstrap is enabled
        if self.model.bootstrap and hasattr(self.model, "oob_score_"):
            metrics["oob_score"] = self.model.oob_score_
        
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
        
        # sklearn predicts 0, 1, 2; convert back to -1, 0, 1
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
        Get feature importance scores based on mean decrease in impurity.
        
        Returns:
            DataFrame with columns 'feature' and 'importance', sorted by importance.
        """
        if not self.is_trained:
            raise ValueError("Model must be trained before getting feature importance.")
        
        importance_data = []
        for feature, importance in zip(self._feature_names, self.model.feature_importances_):
            importance_data.append({
                "feature": feature,
                "importance": importance,
            })
        
        df = pd.DataFrame(importance_data)
        df = df.sort_values("importance", ascending=False).reset_index(drop=True)
        
        return df
    
    def get_permutation_importance(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        n_repeats: int = 10,
        random_state: int = 42,
    ) -> pd.DataFrame:
        """
        Calculate permutation importance on validation data.
        
        This is more reliable than impurity-based importance but slower.
        
        Args:
            X: Validation features.
            y: Validation labels.
            n_repeats: Number of permutation repeats.
            random_state: Random seed for reproducibility.
            
        Returns:
            DataFrame with feature permutation importance.
        """
        from sklearn.inspection import permutation_importance
        
        if not self.is_trained:
            raise ValueError("Model must be trained before getting feature importance.")
        
        # Encode labels
        y_encoded = y + 1
        
        result = permutation_importance(
            self.model,
            X,
            y_encoded,
            n_repeats=n_repeats,
            random_state=random_state,
            n_jobs=-1,
        )
        
        importance_data = []
        for feature, mean_importance, std_importance in zip(
            self._feature_names, result.importances_mean, result.importances_std
        ):
            importance_data.append({
                "feature": feature,
                "importance_mean": mean_importance,
                "importance_std": std_importance,
            })
        
        df = pd.DataFrame(importance_data)
        df = df.sort_values("importance_mean", ascending=False).reset_index(drop=True)
        
        return df


# Convenience function for quick model creation
def create_random_forest_model(
    n_estimators: int = 500,
    max_depth: int = 10,
    min_samples_split: int = 5,
    **kwargs
) -> RandomForestModel:
    """
    Create a Random Forest model with specified parameters.
    
    Args:
        n_estimators: Number of trees in the forest.
        max_depth: Maximum tree depth.
        min_samples_split: Minimum samples to split a node.
        **kwargs: Additional Random Forest parameters.
        
    Returns:
        Configured RandomForestModel instance.
    """
    params = {
        "n_estimators": n_estimators,
        "max_depth": max_depth,
        "min_samples_split": min_samples_split,
        **kwargs
    }
    return RandomForestModel(model_params=params)
