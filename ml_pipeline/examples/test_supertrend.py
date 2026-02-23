#!/usr/bin/env python3
"""
Test script to verify the supertrend feature calculation.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd
import numpy as np
from ml_pipeline.features.technical_features import TechnicalFeatureGenerator
from ml_pipeline.config import TechnicalFeatureConfig

def test_supertrend():
    """Test supertrend calculation with sample data."""
    
    # Create sample OHLCV data
    np.random.seed(42)
    n = 100
    
    # Generate realistic price data
    base_price = 100
    returns = np.random.randn(n) * 0.02  # 2% daily volatility
    prices = base_price * np.cumprod(1 + returns)
    
    # Create OHLCV DataFrame
    dates = pd.date_range(start='2024-01-01', periods=n, freq='D')
    df = pd.DataFrame({
        'Open': prices * (1 + np.random.randn(n) * 0.005),
        'High': prices * (1 + np.abs(np.random.randn(n) * 0.01)),
        'Low': prices * (1 - np.abs(np.random.randn(n) * 0.01)),
        'Close': prices,
        'Volume': np.random.randint(1000000, 10000000, n)
    }, index=dates)
    
    # Ensure High >= max(Open, Close) and Low <= min(Open, Close)
    df['High'] = df[['High', 'Open', 'Close']].max(axis=1)
    df['Low'] = df[['Low', 'Open', 'Close']].min(axis=1)
    
    print("=" * 60)
    print("TESTING SUPERTREND CALCULATION")
    print("=" * 60)
    
    # Generate technical features
    config = TechnicalFeatureConfig()
    generator = TechnicalFeatureGenerator(config)
    features = generator.generate(df)
    
    # Check supertrend
    supertrend = features['supertrend']
    
    print(f"\nSample data shape: {df.shape}")
    print(f"Features shape: {features.shape}")
    print(f"\nSupertrend statistics:")
    print(f"  - Total values: {len(supertrend)}")
    print(f"  - NaN values: {supertrend.isna().sum()}")
    print(f"  - Unique values: {supertrend.dropna().unique()}")
    print(f"  - Value counts:")
    print(supertrend.value_counts().to_string())
    
    # Check if we have both uptrend and downtrend signals
    unique_vals = supertrend.dropna().unique()
    
    if len(unique_vals) > 0 and not (len(unique_vals) == 1 and unique_vals[0] == 0):
        print("\n✅ SUPERTREND IS WORKING!")
        print(f"   Found trend signals: {unique_vals}")
    else:
        print("\n❌ SUPERTREND STILL HAS ISSUES")
        print(f"   All values are: {unique_vals}")
    
    # Show last 10 values
    print(f"\nLast 10 supertrend values:")
    print(supertrend.tail(10).to_string())
    
    return supertrend

if __name__ == '__main__':
    test_supertrend()
