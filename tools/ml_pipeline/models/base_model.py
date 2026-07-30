"""
Base model class for all ML models in the pipeline.

This module defines the abstract base class that all model implementations
must inherit from, ensuring a consistent interface across different models.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple, List
from dataclasses import dataclass
import pandas as pd
import numpy as np
from pathlib import Path
import joblib
import json
from datetime import datetime


@dataclass
class ModelMetadata:
    """Metadata for a trained model."""
    model_name: str
    version: str
    training_date: str
    feature_names: List[str]
    num_features: int
    num_samples_trained: int
    validation_metrics: Dict[str, float]
    hyperparameters: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert metadata to dictionary."""
        return {
            "model_name": self.model_name,
            "version": self.version,
            "training_date": self.training_date,
            "feature_names": self.feature_names,
            "num_features": self.num_features,
            "num_samples_trained": self.num_samples_trained,
            "validation_metrics": self.validation_metrics,
            "hyperparameters": self.hyperparameters,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelMetadata":
        """Create metadata from dictionary."""
        return cls(**data)


@dataclass
class PredictionResult:
    """Container for prediction results."""
    predictions: np.ndarray  # Predicted class labels
    probabilities: np.ndarray  # Probability for each class
    confidence: np.ndarray  # Max probability for each prediction
    predicted_direction: List[str]  # Human-readable direction
    
    def to_dataframe(self, index: pd.Index = None) -> pd.DataFrame:
        """Convert predictions to DataFrame."""
        df = pd.DataFrame({
            "prediction": self.predictions,
            "confidence": self.confidence,
            "direction": self.predicted_direction,
        })
        
        # Add probability columns
        for i, prob_col in enumerate(["prob_down", "prob_flat", "prob_up"]):
            df[prob_col] = self.probabilities[:, i]
        
        if index is not None:
            df.index = index
        
        return df


class BaseModel(ABC):
    """
    Abstract base class for all ML models.
    
    This class defines the interface that all model implementations must follow.
    It provides common functionality for saving/loading models and tracking metadata.
    
    Attributes:
        model: The underlying model instance.
        model_name: Human-readable name for the model.
        is_trained: Whether the model has been trained.
        metadata: Model metadata after training.
    """
    
    # Class-level constants for label mapping
    LABEL_MAP = {-1: "DOWN", 0: "FLAT", 1: "UP"}
    LABEL_TO_INT = {"DOWN": -1, "FLAT": 0, "UP": 1}
    
    def __init__(self, model_name: str, model_params: Optional[Dict] = None):
        """
        Initialize the base model.
        
        Args:
            model_name: Name identifier for the model.
            model_params: Dictionary of model hyperparameters.
        """
        self.model_name = model_name
        self.model_params = model_params or {}
        self.model: Any = None
        self.is_trained = False
        self.metadata: Optional[ModelMetadata] = None
        self._feature_names: List[str] = []
    
    @abstractmethod
    def _create_model(self) -> Any:
        """
        Create the underlying model instance.
        
        Returns:
            The model instance (e.g., XGBClassifier, RandomForestClassifier).
        """
        pass
    
    @abstractmethod
    def train(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        X_val: Optional[pd.DataFrame] = None,
        y_val: Optional[pd.Series] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Train the model on the provided data.
        
        Args:
            X_train: Training features.
            y_train: Training labels.
            X_val: Optional validation features.
            y_val: Optional validation labels.
            **kwargs: Additional training arguments.
            
        Returns:
            Dictionary containing training metrics and history.
        """
        pass
    
    @abstractmethod
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """
        Make class predictions for the input data.
        
        Args:
            X: Input features.
            
        Returns:
            Array of predicted class labels.
        """
        pass
    
    @abstractmethod
    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """
        Get probability predictions for each class.
        
        Args:
            X: Input features.
            
        Returns:
            Array of shape (n_samples, n_classes) with probabilities.
        """
        pass
    
    def predict_with_confidence(self, X: pd.DataFrame) -> PredictionResult:
        """
        Make predictions with confidence scores.
        
        Args:
            X: Input features.
            
        Returns:
            PredictionResult containing predictions, probabilities, and confidence.
        """
        predictions = self.predict(X)
        probabilities = self.predict_proba(X)
        confidence = np.max(probabilities, axis=1)
        
        # Convert numeric predictions to direction strings
        predicted_direction = [self.LABEL_MAP.get(p, "UNKNOWN") for p in predictions]
        
        return PredictionResult(
            predictions=predictions,
            probabilities=probabilities,
            confidence=confidence,
            predicted_direction=predicted_direction,
        )
    
    @abstractmethod
    def get_feature_importance(self) -> pd.DataFrame:
        """
        Get feature importance scores from the trained model.
        
        Returns:
            DataFrame with feature names and importance scores, sorted by importance.
        """
        pass
    
    def save(self, path: str, save_metadata: bool = True) -> None:
        """
        Save the model to disk.
        
        Args:
            path: Path to save the model (without extension).
            save_metadata: Whether to save metadata alongside the model.
        """
        if not self.is_trained:
            raise ValueError("Cannot save an untrained model.")
        
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        # Save model using joblib
        model_path = f"{path}.joblib"
        joblib.dump(self.model, model_path)
        
        # Save metadata
        if save_metadata and self.metadata:
            metadata_path = f"{path}_metadata.json"
            with open(metadata_path, 'w') as f:
                json.dump(self.metadata.to_dict(), f, indent=2)
        
        # Save feature names
        features_path = f"{path}_features.json"
        with open(features_path, 'w') as f:
            json.dump(self._feature_names, f)
    
    def load(self, path: str, load_metadata: bool = True) -> None:
        """
        Load a trained model from disk.
        
        Args:
            path: Path to the saved model (without extension).
            load_metadata: Whether to load metadata alongside the model.
        """
        path = Path(path)
        
        # Load model
        model_path = f"{path}.joblib"
        if not Path(model_path).exists():
            raise FileNotFoundError(f"Model file not found: {model_path}")
        
        self.model = joblib.load(model_path)
        self.is_trained = True
        
        # Load metadata
        if load_metadata:
            metadata_path = f"{path}_metadata.json"
            if Path(metadata_path).exists():
                with open(metadata_path, 'r') as f:
                    metadata_dict = json.load(f)
                self.metadata = ModelMetadata.from_dict(metadata_dict)
        
        # Load feature names
        features_path = f"{path}_features.json"
        if Path(features_path).exists():
            with open(features_path, 'r') as f:
                self._feature_names = json.load(f)
    
    def _validate_features(self, X: pd.DataFrame) -> None:
        """
        Validate that input features match the training features.
        
        Args:
            X: Input features to validate.
            
        Raises:
            ValueError: If features don't match.
        """
        if self._feature_names:
            missing_features = set(self._feature_names) - set(X.columns)
            extra_features = set(X.columns) - set(self._feature_names)
            
            if missing_features:
                raise ValueError(f"Missing features: {missing_features}")
            if extra_features:
                raise ValueError(f"Extra features not seen during training: {extra_features}")
    
    def _create_metadata(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        validation_metrics: Dict[str, float],
        version: str = "1.0.0",
    ) -> ModelMetadata:
        """
        Create metadata for the trained model.
        
        Args:
            X_train: Training features.
            y_train: Training labels.
            validation_metrics: Dictionary of validation metrics.
            version: Model version string.
            
        Returns:
            ModelMetadata instance.
        """
        return ModelMetadata(
            model_name=self.model_name,
            version=version,
            training_date=datetime.now().isoformat(),
            feature_names=list(X_train.columns),
            num_features=X_train.shape[1],
            num_samples_trained=len(X_train),
            validation_metrics=validation_metrics,
            hyperparameters=self.model_params,
        )
    
    def __repr__(self) -> str:
        """String representation of the model."""
        status = "trained" if self.is_trained else "not trained"
        return f"{self.__class__.__name__}(name='{self.model_name}', status='{status}')"
    
    def __str__(self) -> str:
        """Human-readable string representation."""
        return self.__repr__()
