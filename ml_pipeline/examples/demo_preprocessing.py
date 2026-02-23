#!/usr/bin/env python3
"""
Demo script to demonstrate data preprocessing for ML training.

This script shows:
1. Loading processed feature data
2. Feature selection based on correlation
3. Feature scaling (RobustScaler)
4. Outlier handling
5. Class weight computation
6. Train/test split with time-series awareness

Run:
    python -m ml_pipeline.examples.demo_preprocessing
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import numpy as np
from pathlib import Path
from ml_pipeline.data.preprocessor import DataPreprocessor, PreprocessingConfig


def load_sample_data(data_dir: str, max_stocks: int = 5) -> pd.DataFrame:
    """Load and combine feature data from multiple stocks."""
    processed_dir = Path(data_dir)
    
    all_data = []
    files = list(processed_dir.glob('*_features_labels.parquet'))
    
    for i, file in enumerate(files[:max_stocks]):
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


def demo_preprocessing():
    """Demonstrate preprocessing steps."""
    
    print("=" * 70)
    print(" DATA PREPROCESSING DEMO")
    print("=" * 70)
    
    # Find data directory
    script_dir = Path(__file__).parent
    data_dir = script_dir.parent.parent / 'data' / 'processed'
    
    if not data_dir.exists():
        print(f"\n❌ Data directory not found: {data_dir}")
        print("Please run feature generation first:")
        print("  python -m ml_pipeline.examples.generate_all_features")
        return
    
    # Load sample data
    print(f"\n1. LOADING DATA")
    print("-" * 70)
    print(f"Data directory: {data_dir}")
    
    try:
        df = load_sample_data(str(data_dir), max_stocks=10)
        print(f"✓ Loaded data: {df.shape[0]} rows, {df.shape[1]} columns")
        print(f"  Symbols: {df['symbol'].nunique()} stocks")
        print(f"  Date range: {df.index.min()} to {df.index.max()}")
    except Exception as e:
        print(f"❌ Error loading data: {e}")
        return
    
    # Show label distribution
    print(f"\n2. LABEL DISTRIBUTION")
    print("-" * 70)
    label_counts = df['label'].value_counts()
    total = len(df)
    for label, count in label_counts.items():
        label_name = {-1: 'DOWN', 0: 'FLAT', 1: 'UP'}.get(int(label), str(label))
        print(f"  {label_name} ({int(label):+d}): {count} ({count/total*100:.1f}%)")
    
    # Configure preprocessor
    print(f"\n3. PREPROCESSING CONFIGURATION")
    print("-" * 70)
    
    config = PreprocessingConfig(
        min_correlation_threshold=0.0,    # Keep all features (tree models handle selection)
        max_missing_pct=0.10,             # Remove features with > 10% missing
        remove_zero_variance=True,        # Remove zero variance features
        scaler_type='robust',             # RobustScaler for outlier resistance
        clip_outliers=True,               # Clip outliers
        outlier_method='iqr',             # Use IQR method
        test_size=0.2,                    # 20% for test
        use_time_series_split=True,       # Time-series aware split
        purge_days=5,                     # 5-day purge gap
        compute_class_weights=True,       # Compute class weights
        exclude_features=['symbol', 'label_direction']  # Exclude non-feature columns
    )
    
    print(f"  Feature selection:")
    print(f"    - Min correlation threshold: {config.min_correlation_threshold}")
    print(f"    - Max missing %: {config.max_missing_pct}")
    print(f"    - Remove zero variance: {config.remove_zero_variance}")
    print(f"  Scaling:")
    print(f"    - Scaler type: {config.scaler_type}")
    print(f"    - Clip outliers: {config.clip_outliers}")
    print(f"    - Outlier method: {config.outlier_method}")
    print(f"  Train/test split:")
    print(f"    - Test size: {config.test_size}")
    print(f"    - Time series split: {config.use_time_series_split}")
    print(f"    - Purge days: {config.purge_days}")
    
    # Create and fit preprocessor
    print(f"\n4. FITTING PREPROCESSOR")
    print("-" * 70)
    
    preprocessor = DataPreprocessor(config)
    
    # Get feature columns - only numeric columns
    feature_cols = [col for col in df.columns 
                   if col not in ['label', 'symbol', 'forward_return', 'label_direction']
                   and not col.startswith('_')
                   and pd.api.types.is_numeric_dtype(df[col])]
    
    print(f"  Initial feature count: {len(feature_cols)}")
    print(f"  Non-numeric columns excluded: {[c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])]}")
    
    # Fit preprocessor
    preprocessor.fit(df, target_col='label', feature_cols=feature_cols)
    
    print(f"\n  Selected features: {len(preprocessor.selected_features)}")
    print(f"  Excluded features: {len(preprocessor.excluded_features)}")
    
    # Show selected features
    print(f"\n  Selected features (first 20):")
    for i, feat in enumerate(preprocessor.selected_features[:20]):
        stats = preprocessor.feature_stats.get(feat, {})
        print(f"    {i+1:2d}. {feat}")
    if len(preprocessor.selected_features) > 20:
        print(f"    ... and {len(preprocessor.selected_features) - 20} more")
    
    # Show class weights
    print(f"\n5. CLASS WEIGHTS (for imbalanced data)")
    print("-" * 70)
    for label, weight in preprocessor.class_weights.items():
        label_int = int(label)
        label_name = {-1: 'DOWN', 0: 'FLAT', 1: 'UP'}.get(label_int, str(label_int))
        print(f"  {label_name} ({label_int:+d}): {weight:.3f}")
    
    # Prepare train/test data
    print(f"\n6. TRAIN/TEST SPLIT")
    print("-" * 70)
    
    X_train, X_test, y_train, y_test = preprocessor.prepare_data(
        df, 
        target_col='label',
        feature_cols=feature_cols
    )
    
    print(f"  Training set:")
    print(f"    - Samples: {len(X_train)}")
    print(f"    - Features: {X_train.shape[1]}")
    print(f"    - Label distribution:")
    for label in [-1, 0, 1]:
        count = (y_train == label).sum()
        label_name = {-1: 'DOWN', 0: 'FLAT', 1: 'UP'}.get(label, str(label))
        print(f"      {label_name}: {count} ({count/len(y_train)*100:.1f}%)")
    
    print(f"\n  Test set:")
    print(f"    - Samples: {len(X_test)}")
    print(f"    - Features: {X_test.shape[1]}")
    print(f"    - Label distribution:")
    for label in [-1, 0, 1]:
        count = (y_test == label).sum()
        label_name = {-1: 'DOWN', 0: 'FLAT', 1: 'UP'}.get(label, str(label))
        print(f"      {label_name}: {count} ({count/len(y_test)*100:.1f}%)")
    
    # Show sample of preprocessed data
    print(f"\n7. SAMPLE PREPROCESSED DATA")
    print("-" * 70)
    print("  First 5 rows of training data (first 5 features):")
    sample = X_train.iloc[:5, :5]
    print(sample.to_string())
    
    # Summary
    print(f"\n" + "=" * 70)
    print(" PREPROCESSING COMPLETE")
    print("=" * 70)
    print(f"""
Data is ready for training!

Next steps:
1. Train the ensemble model:
   - XGBoost
   - Random Forest
   - LightGBM

2. Use class weights during training:
   class_weights = {preprocessor.class_weights}

3. Use time-series cross-validation for hyperparameter tuning

4. Evaluate on test set with:
   - Accuracy, Precision, Recall, F1
   - Confusion Matrix
   - Feature Importance
""")
    
    return X_train, X_test, y_train, y_test, preprocessor


if __name__ == '__main__':
    demo_preprocessing()
