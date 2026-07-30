"""
Ensemble model implementation for stock movement prediction.

This module provides an ensemble model that combines predictions from
XGBoost, Random Forest, and LightGBM using soft voting (weighted probability averaging).
"""

from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
import pandas as pd
import numpy as np
from pathlib import Path
import json

from ml_pipeline.models.base_model import BaseModel, PredictionResult
from ml_pipeline.models.xgboost_model import XGBoostModel
from ml_pipeline.models.random_forest_model import RandomForestModel
from ml_pipeline.models.lightgbm_model import LightGBMModel
from ml_pipeline.config import EnsembleConfig, WeightingMethod


@dataclass
class EnsemblePredictionResult:
    """Container for ensemble prediction results with additional metadata."""
    predictions: np.ndarray
    probabilities: np.ndarray
    confidence: np.ndarray
    predicted_direction: List[str]
    model_predictions: Dict[str, np.ndarray]  # Individual model predictions
    model_probabilities: Dict[str, np.ndarray]  # Individual model probabilities
    agreement_score: np.ndarray  # How many models agree (0-1)
    
    def to_dataframe(self, index: pd.Index = None) -> pd.DataFrame:
        """Convert predictions to DataFrame with ensemble details."""
        df = pd.DataFrame({
            "prediction": self.predictions,
            "confidence": self.confidence,
            "direction": self.predicted_direction,
            "agreement": self.agreement_score,
        })
        
        # Add probability columns
        for i, prob_col in enumerate(["prob_down", "prob_flat", "prob_up"]):
            df[prob_col] = self.probabilities[:, i]
        
        # Add individual model predictions
        for model_name, preds in self.model_predictions.items():
            df[f"{model_name}_pred"] = preds
        
        if index is not None:
            df.index = index
        
        return df


class EnsembleModel:
    """
    Ensemble model combining XGBoost, Random Forest, and LightGBM.
    
    This model uses soft voting (weighted probability averaging) to combine
    predictions from multiple base models. The ensemble can use:
    - Equal weights for all models
    - Performance-based weights (proportional to validation accuracy)
    - Optimized weights (found via grid search)
    
    Attributes:
        models: Dictionary of base models.
        weights: Dictionary of model weights.
        voting: 'soft' (probability averaging) or 'hard' (majority voting).
        is_trained: Whether all models have been trained.
    """
    
    # Class-level constants for label mapping
    LABEL_MAP = {-1: "DOWN", 0: "FLAT", 1: "UP"}
    LABEL_TO_INT = {"DOWN": -1, "FLAT": 0, "UP": 1}
    
    def __init__(
        self,
        config: Optional[EnsembleConfig] = None,
        xgboost_params: Optional[Dict] = None,
        random_forest_params: Optional[Dict] = None,
        lightgbm_params: Optional[Dict] = None,
    ):
        """
        Initialize the ensemble model.
        
        Args:
            config: Ensemble configuration. If None, uses default.
            xgboost_params: XGBoost hyperparameters.
            random_forest_params: Random Forest hyperparameters.
            lightgbm_params: LightGBM hyperparameters.
        """
        self.config = config or EnsembleConfig()
        
        # Initialize base models
        self.models: Dict[str, BaseModel] = {
            "xgboost": XGBoostModel(model_params=xgboost_params),
            "random_forest": RandomForestModel(model_params=random_forest_params),
            "lightgbm": LightGBMModel(model_params=lightgbm_params),
        }
        
        # Set initial weights
        self.weights = self.config.custom_weights.copy()
        
        # Validate weights
        if not self._validate_weights():
            raise ValueError("Invalid weights: must sum to 1.0 and be non-negative")
        
        self.voting = self.config.voting
        self.is_trained = False
        self._feature_names: List[str] = []
    
    def _validate_weights(self) -> bool:
        """Validate that weights are proper probabilities."""
        total = sum(self.weights.values())
        return all(w >= 0 for w in self.weights.values()) and abs(total - 1.0) < 1e-6
    
    def set_weights(self, weights: Dict[str, float]) -> None:
        """
        Set custom weights for the ensemble.
        
        Args:
            weights: Dictionary mapping model names to weights.
        """
        self.weights = weights
        if not self._validate_weights():
            raise ValueError("Invalid weights: must sum to 1.0 and be non-negative")
    
    def train_all(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        X_val: Optional[pd.DataFrame] = None,
        y_val: Optional[pd.Series] = None,
        **kwargs
    ) -> Dict[str, Dict[str, Any]]:
        """
        Train all base models in the ensemble.
        
        Args:
            X_train: Training features.
            y_train: Training labels.
            X_val: Optional validation features.
            y_val: Optional validation labels.
            **kwargs: Additional arguments passed to each model's train method.
            
        Returns:
            Dictionary containing training metrics for each model.
        """
        self._feature_names = list(X_train.columns)
        training_results = {}
        
        for model_name, model in self.models.items():
            print(f"Training {model_name}...")
            results = model.train(X_train, y_train, X_val, y_val, **kwargs)
            training_results[model_name] = results
        
        # Update weights based on validation performance if configured
        if self.config.weighting_method == WeightingMethod.PERFORMANCE_BASED:
            if X_val is not None and y_val is not None:
                self._update_weights_performance(X_val, y_val)
        
        self.is_trained = True
        
        return training_results
    
    def _update_weights_performance(self, X_val: pd.DataFrame, y_val: pd.Series) -> None:
        """Update weights based on validation performance."""
        accuracies = {}
        
        for model_name, model in self.models.items():
            predictions = model.predict(X_val)
            accuracies[model_name] = np.mean(predictions == y_val.values)
        
        # Normalize accuracies to weights
        total_accuracy = sum(accuracies.values())
        if total_accuracy > 0:
            self.weights = {
                name: acc / total_accuracy 
                for name, acc in accuracies.items()
            }
        
        print(f"Updated weights based on performance: {self.weights}")
    
    def optimize_weights(
        self,
        X_val: pd.DataFrame,
        y_val: pd.Series,
        n_trials: int = 100,
    ) -> Dict[str, float]:
        """
        Optimize ensemble weights using grid search on validation data.
        
        Args:
            X_val: Validation features.
            y_val: Validation labels.
            n_trials: Number of optimization trials.
            
        Returns:
            Optimized weights dictionary.
        """
        try:
            import optuna
        except ImportError:
            raise ImportError("Optuna is required for weight optimization. Install with: pip install optuna")
        
        def objective(trial):
            # Suggest weights
            w_xgb = trial.suggest_float("w_xgb", 0.1, 0.6)
            w_rf = trial.suggest_float("w_rf", 0.1, 0.6)
            w_lgbm = 1.0 - w_xgb - w_rf  # Ensure sum is 1
            
            if w_lgbm < 0.1 or w_lgbm > 0.6:
                return float('-inf')  # Invalid weights
            
            weights = {
                "xgboost": w_xgb,
                "random_forest": w_rf,
                "lightgbm": w_lgbm,
            }
            
            # Get predictions
            probabilities = self._get_weighted_probabilities(X_val, weights)
            predictions = np.argmax(probabilities, axis=1) - 1  # Convert back to -1, 0, 1
            
            return np.mean(predictions == y_val.values)
        
        study = optuna.create_study(direction="maximize")
        study.optimize(objective, n_trials=n_trials)
        
        # Update weights with best values
        best_params = study.best_params
        self.weights = {
            "xgboost": best_params["w_xgb"],
            "random_forest": best_params["w_rf"],
            "lightgbm": 1.0 - best_params["w_xgb"] - best_params["w_rf"],
        }
        
        print(f"Optimized weights: {self.weights}")
        return self.weights
    
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """
        Make ensemble predictions.
        
        Args:
            X: Input features.
            
        Returns:
            Array of predicted labels (-1, 0, 1 for DOWN, FLAT, UP).
        """
        if not self.is_trained:
            raise ValueError("All models must be trained before making predictions.")
        
        if self.voting == "soft":
            probabilities = self.predict_proba(X)
            return np.argmax(probabilities, axis=1) - 1  # Convert 0,1,2 to -1,0,1
        else:
            # Hard voting
            predictions = np.column_stack([
                model.predict(X) for model in self.models.values()
            ])
            
            # Majority voting
            from scipy.stats import mode
            final_predictions, _ = mode(predictions, axis=1)
            return final_predictions.flatten()
    
    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """
        Get weighted probability predictions.
        
        Args:
            X: Input features.
            
        Returns:
            Array of shape (n_samples, 3) with probabilities for [DOWN, FLAT, UP].
        """
        if not self.is_trained:
            raise ValueError("All models must be trained before making predictions.")
        
        return self._get_weighted_probabilities(X, self.weights)
    
    def _get_weighted_probabilities(
        self, 
        X: pd.DataFrame, 
        weights: Dict[str, float]
    ) -> np.ndarray:
        """Calculate weighted average of probabilities."""
        weighted_proba = None
        
        for model_name, model in self.models.items():
            proba = model.predict_proba(X)
            weight = weights.get(model_name, 0.0)
            
            if weighted_proba is None:
                weighted_proba = weight * proba
            else:
                weighted_proba += weight * proba
        
        return weighted_proba
    
    def predict_with_details(self, X: pd.DataFrame) -> EnsemblePredictionResult:
        """
        Make predictions with full details including individual model predictions.
        
        Args:
            X: Input features.
            
        Returns:
            EnsemblePredictionResult with all prediction details.
        """
        if not self.is_trained:
            raise ValueError("All models must be trained before making predictions.")
        
        # Get individual model predictions
        model_predictions = {}
        model_probabilities = {}
        
        for model_name, model in self.models.items():
            model_predictions[model_name] = model.predict(X)
            model_probabilities[model_name] = model.predict_proba(X)
        
        # Get ensemble predictions
        probabilities = self._get_weighted_probabilities(X, self.weights)
        predictions = np.argmax(probabilities, axis=1) - 1
        confidence = np.max(probabilities, axis=1)
        
        # Calculate agreement score
        pred_matrix = np.column_stack(list(model_predictions.values()))
        agreement_score = np.apply_along_axis(
            lambda row: len(set(row)) == 1,  # All models agree
            axis=1,
            arr=pred_matrix
        ).astype(float)
        
        # Alternative: count how many models agree with ensemble prediction
        agreement_count = np.sum(pred_matrix == predictions.reshape(-1, 1), axis=1)
        agreement_score = agreement_count / len(self.models)
        
        predicted_direction = [self.LABEL_MAP.get(p, "UNKNOWN") for p in predictions]
        
        return EnsemblePredictionResult(
            predictions=predictions,
            probabilities=probabilities,
            confidence=confidence,
            predicted_direction=predicted_direction,
            model_predictions=model_predictions,
            model_probabilities=model_probabilities,
            agreement_score=agreement_score,
        )
    
    def get_model_agreement(self, X: pd.DataFrame) -> np.ndarray:
        """
        Calculate how many models agree on each prediction.
        
        Args:
            X: Input features.
            
        Returns:
            Array of agreement scores (0-1, where 1 means all models agree).
        """
        predictions = np.column_stack([
            model.predict(X) for model in self.models.values()
        ])
        
        # Count unique predictions per row
        n_unique = np.apply_along_axis(lambda row: len(set(row)), axis=1, arr=predictions)
        
        # Agreement score: 1 if all agree, 0.67 if 2/3 agree, 0.33 if all different
        agreement = (3 - n_unique + 1) / 3
        
        return agreement
    
    def get_feature_importance(self) -> pd.DataFrame:
        """
        Get aggregated feature importance from all models.
        
        Returns:
            DataFrame with feature importance from each model and average.
        """
        if not self.is_trained:
            raise ValueError("All models must be trained before getting feature importance.")
        
        importance_dfs = []
        
        for model_name, model in self.models.items():
            df = model.get_feature_importance()
            df = df.rename(columns={"importance": f"{model_name}_importance"})
            importance_dfs.append(df)
        
        # Merge all importance DataFrames
        merged = importance_dfs[0]
        for df in importance_dfs[1:]:
            merged = merged.merge(df, on="feature", how="outer")
        
        # Fill NaN with 0
        merged = merged.fillna(0)
        
        # Calculate weighted average importance
        importance_cols = [f"{name}_importance" for name in self.models.keys()]
        weights_list = [self.weights.get(name, 0) for name in self.models.keys()]
        
        merged["avg_importance"] = sum(
            merged[col] * w for col, w in zip(importance_cols, weights_list)
        )
        
        merged = merged.sort_values("avg_importance", ascending=False).reset_index(drop=True)
        
        return merged
    
    def save(self, path: str) -> None:
        """
        Save all models and ensemble configuration.
        
        Args:
            path: Base path for saving (models will be saved with suffixes).
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        # Save each model
        for model_name, model in self.models.items():
            model_path = f"{path}_{model_name}"
            model.save(model_path)
        
        # Save ensemble configuration
        config_path = f"{path}_ensemble_config.json"
        config_data = {
            "weights": self.weights,
            "voting": self.voting,
            "feature_names": self._feature_names,
        }
        with open(config_path, 'w') as f:
            json.dump(config_data, f, indent=2)
    
    def load(self, path: str) -> None:
        """
        Load all models and ensemble configuration.
        
        Args:
            path: Base path for loading.
        """
        path = Path(path)
        
        # Load each model
        for model_name, model in self.models.items():
            model_path = f"{path}_{model_name}"
            model.load(model_path)
        
        # Load ensemble configuration
        config_path = f"{path}_ensemble_config.json"
        if Path(config_path).exists():
            with open(config_path, 'r') as f:
                config_data = json.load(f)
            self.weights = config_data.get("weights", self.weights)
            self.voting = config_data.get("voting", self.voting)
            self._feature_names = config_data.get("feature_names", [])
        
        self.is_trained = True
    
    def __repr__(self) -> str:
        """String representation of the ensemble."""
        status = "trained" if self.is_trained else "not trained"
        return f"EnsembleModel(models={list(self.models.keys())}, weights={self.weights}, status='{status}')"


# Convenience function for quick ensemble creation
def create_ensemble_model(
    voting: str = "soft",
    weights: Optional[Dict[str, float]] = None,
    **kwargs
) -> EnsembleModel:
    """
    Create an ensemble model with specified configuration.
    
    Args:
        voting: 'soft' or 'hard' voting.
        weights: Custom model weights.
        **kwargs: Additional parameters for individual models.
        
    Returns:
        Configured EnsembleModel instance.
    """
    config = EnsembleConfig(
        voting=voting,
        custom_weights=weights or {"xgboost": 0.33, "random_forest": 0.33, "lightgbm": 0.34},
    )
    return EnsembleModel(config=config)
