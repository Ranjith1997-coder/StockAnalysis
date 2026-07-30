#!/usr/bin/env python3
"""
Demo script to train a stock movement prediction model.

This script demonstrates:
1. Loading preprocessed feature data
2. Preprocessing with scaling and class weights
3. Training an ensemble model with cross-validation
4. Evaluating on test set
5. Saving the trained model

Run:
    python -m ml_pipeline.examples.train_model
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

from ml_pipeline.data.preprocessor import DataPreprocessor, PreprocessingConfig
from ml_pipeline.training.trainer import Trainer, TrainingConfig


def load_all_data(data_dir: str, max_stocks: int = None) -> pd.DataFrame:
    """Load and combine feature data from all stocks."""
    processed_dir = Path(data_dir)
    
    all_data = []
    files = list(processed_dir.glob('*_features_labels.parquet'))
    
    if max_stocks:
        files = files[:max_stocks]
    
    for file in files:
        try:
            df = pd.read_parquet(file)
            symbol = file.stem.replace('_features_labels', '')
            df['symbol'] = symbol
            all_data.append(df)
        except Exception as e:
            print(f"Error loading {file}: {e}")
    
    if not all_data:
        raise ValueError(f"No data files found in {processed_dir}")
    
    combined = pd.concat(all_data, ignore_index=True)
    return combined


def main():
    """Main training function."""
    
    print("=" * 70)
    print(" STOCK MOVEMENT PREDICTION - MODEL TRAINING")
    print("=" * 70)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Find data directory
    script_dir = Path(__file__).parent
    data_dir = script_dir.parent.parent / 'data' / 'processed'
    
    if not data_dir.exists():
        print(f"\n❌ Data directory not found: {data_dir}")
        print("Please run feature generation first:")
        print("  python -m ml_pipeline.examples.generate_all_features")
        return
    
    # Step 1: Load data
    print(f"\n1. LOADING DATA")
    print("-" * 70)
    print(f"Data directory: {data_dir}")
    
    # Load all stocks (or limit for demo)
    df = load_all_data(str(data_dir), max_stocks=50)  # Use 50 stocks for demo
    
    print(f"✓ Loaded data: {df.shape[0]} rows, {df.shape[1]} columns")
    print(f"  Stocks: {df['symbol'].nunique()}")
    
    # Step 2: Preprocess data
    print(f"\n2. PREPROCESSING DATA")
    print("-" * 70)
    
    # Configure preprocessing
    preproc_config = PreprocessingConfig(
        min_correlation_threshold=0.0,  # Keep all features
        max_missing_pct=0.10,
        remove_zero_variance=True,
        scaler_type='robust',
        clip_outliers=True,
        outlier_method='iqr',
        test_size=0.2,
        use_time_series_split=True,
        purge_days=5,
        compute_class_weights=True,
        exclude_features=['symbol', 'label_name']
    )
    
    preprocessor = DataPreprocessor(preproc_config)
    
    # Get feature columns (numeric only)
    feature_cols = [col for col in df.columns 
                   if col not in ['label', 'symbol', 'forward_return', 'label_name']
                   and not col.startswith('_')
                   and pd.api.types.is_numeric_dtype(df[col])]
    
    print(f"Feature columns: {len(feature_cols)}")
    
    # Prepare train/test split
    X_train, X_test, y_train, y_test = preprocessor.prepare_data(
        df, 
        target_col='label',
        feature_cols=feature_cols
    )
    
    print(f"✓ Training set: {X_train.shape[0]} samples")
    print(f"✓ Test set: {X_test.shape[0]} samples")
    print(f"✓ Class weights: {preprocessor.class_weights}")
    
    # Step 3: Configure training
    print(f"\n3. CONFIGURING TRAINING")
    print("-" * 70)
    
    training_config = TrainingConfig(
        n_splits=5,
        purge_days=5,
        early_stopping_rounds=50,
        verbose=True,
        use_xgboost=True,
        use_random_forest=True,
        use_lightgbm=True,
        use_class_weights=True,
        save_best_model=True,
        model_dir=str(script_dir.parent.parent / 'data' / 'models')
    )
    
    print(f"Cross-validation folds: {training_config.n_splits}")
    print(f"Models: XGBoost={training_config.use_xgboost}, "
          f"RF={training_config.use_random_forest}, "
          f"LGBM={training_config.use_lightgbm}")
    
    # Step 4: Train model
    print(f"\n4. TRAINING MODEL")
    print("-" * 70)
    
    trainer = Trainer(training_config)
    
    result = trainer.train(
        X_train, 
        y_train, 
        class_weights=preprocessor.class_weights,
        X_test=X_test,
        y_test=y_test
    )
    
    # Step 5: Save model
    print(f"\n5. SAVING MODEL")
    print("-" * 70)
    
    model_dir = Path(training_config.model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    
    model_path = model_dir / f'ensemble_model_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pkl'
    trainer.save_model(str(model_path))
    
    # Also save preprocessor for inference
    preprocessor_path = model_dir / f'preprocessor_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pkl'
    import joblib
    joblib.dump(preprocessor, preprocessor_path)
    print(f"Preprocessor saved to: {preprocessor_path}")
    
    # Step 6: Summary
    print(f"\n" + "=" * 70)
    print(" TRAINING COMPLETE")
    print("=" * 70)
    
    print(f"""
Summary:
  - Model type: {result.model_type}
  - Training time: {result.training_time:.2f} seconds
  - CV Accuracy: {result.mean_cv_score['accuracy']:.4f} ± {result.std_cv_score['accuracy']:.4f}
  - CV F1 Macro: {result.mean_cv_score['f1_macro']:.4f} ± {result.std_cv_score['f1_macro']:.4f}
  
Test Set Performance:
  - Accuracy: {result.test_metrics['accuracy']:.4f}
  - F1 Macro: {result.test_metrics['f1_macro']:.4f}
  - Precision UP: {result.test_metrics.get('precision_up', 0):.4f}
  - Precision DOWN: {result.test_metrics.get('precision_down', 0):.4f}

Files saved:
  - Model: {model_path}
  - Preprocessor: {preprocessor_path}

Next steps:
  1. Use the model for predictions:
     from ml_pipeline.training.trainer import Trainer
     trainer = Trainer()
     trainer.load_model('{model_path}')
     predictions = trainer.predict(new_data)
  
  2. Run backtesting to evaluate trading performance
  
  3. Monitor model performance over time
""")
    
    return trainer, result


if __name__ == '__main__':
    main()
