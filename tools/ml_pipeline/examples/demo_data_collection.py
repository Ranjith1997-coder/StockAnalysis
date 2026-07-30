"""
Demo Script: Data Collection and Feature Generation for a Single Stock

This script demonstrates the complete data pipeline:
1. Fetch historical OHLCV data from Yahoo Finance
2. Generate technical, price, volume, and market features
3. Generate labels for next-day prediction
4. Display the combined dataset

Usage:
    python -m ml_pipeline.examples.demo_data_collection
"""

import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Import ML pipeline components
from ml_pipeline.data.data_collector import DataCollector, fetch_index_data
from ml_pipeline.data.feature_engineer import FeatureEngineer
from ml_pipeline.data.label_generator import LabelGenerator


def display_header(title: str):
    """Print a formatted header."""
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)


def display_dataframe_info(df: pd.DataFrame, name: str):
    """Display DataFrame shape and columns."""
    print(f"\n{name}:")
    print(f"  Shape: {df.shape[0]} rows × {df.shape[1]} columns")
    print(f"  Date Range: {df.index.min().date()} to {df.index.max().date()}")
    print(f"  Columns: {list(df.columns)[:5]}..." if len(df.columns) > 5 else f"  Columns: {list(df.columns)}")


def main():
    """Main demo function."""
    
    # Configuration
    SYMBOL = 'RELIANCE.NS'  # Stock to analyze
    START_DATE = '2022-01-01'
    END_DATE = datetime.now().strftime('%Y-%m-%d')
    DATA_DIR = './data'
    
    display_header("ML Pipeline Demo: Data Collection & Feature Generation")
    print(f"\nStock: {SYMBOL}")
    print(f"Period: {START_DATE} to {END_DATE}")
    
    # =========================================================================
    # Step 1: Fetch Stock Data
    # =========================================================================
    display_header("Step 1: Fetching Stock Data from Yahoo Finance")
    
    collector = DataCollector(data_dir=DATA_DIR)
    
    print(f"\nFetching data for {SYMBOL}...")
    stock_df, quality_report = collector.fetch_stock(
        symbol=SYMBOL,
        start_date=START_DATE,
        end_date=END_DATE,
        validate=True,
        save=True
    )
    
    if stock_df is None:
        print(f"ERROR: Failed to fetch data for {SYMBOL}")
        return
    
    print(f"\nData Quality Report:")
    print(quality_report)
    
    display_dataframe_info(stock_df, "Raw OHLCV Data")
    
    print(f"\nSample Data (last 5 rows):")
    print(stock_df.tail().to_string())
    
    # =========================================================================
    # Step 2: Fetch Index Data (for market features)
    # =========================================================================
    display_header("Step 2: Fetching Index Data (Nifty 50)")
    
    print("\nFetching Nifty 50 data for market features...")
    index_data = fetch_index_data(
        symbols=['^NSEI'],  # Nifty 50
        start_date=START_DATE,
        end_date=END_DATE,
        data_dir=DATA_DIR
    )
    
    if '^NSEI' in index_data:
        nifty_df = index_data['^NSEI']
        print(f"Nifty 50 data: {len(nifty_df)} rows")
        print(f"Date Range: {nifty_df.index.min().date()} to {nifty_df.index.max().date()}")
    else:
        print("Warning: Could not fetch Nifty data. Market features will be limited.")
        nifty_df = None
    
    # =========================================================================
    # Step 3: Generate Features
    # =========================================================================
    display_header("Step 3: Generating Features")
    
    print("\nInitializing Feature Engineer...")
    engineer = FeatureEngineer(verbose=True)
    
    print("\nGenerating all features...")
    features_df = engineer.generate_features(
        df=stock_df,
        index_data=index_data if index_data else None
    )
    
    display_dataframe_info(features_df, "Generated Features")
    
    print(f"\nFeature Categories:")
    feature_groups = {
        'Technical': [c for c in features_df.columns if any(x in c for x in ['RSI', 'MACD', 'EMA', 'Bollinger', 'ATR', 'Stoch', 'ADX', 'Supertrend', 'Williams'])],
        'Price': [c for c in features_df.columns if any(x in c for x in ['Return', 'Volatility', 'Gap', 'Price_', 'Momentum', 'Distance', 'Consecutive'])],
        'Volume': [c for c in features_df.columns if any(x in c for x in ['Volume', 'OBV', 'Accumulation', 'CMF', 'Chaikin'])],
        'Market': [c for c in features_df.columns if any(x in c for x in ['Nifty', 'Beta', 'Correlation', 'Sector'])]
    }
    
    for group, cols in feature_groups.items():
        print(f"  {group}: {len(cols)} features")
    
    print(f"\nAll Features ({len(features_df.columns)} total):")
    for i, col in enumerate(features_df.columns, 1):
        print(f"  {i:2d}. {col}")
    
    print(f"\nSample Features (last 5 rows, first 10 columns):")
    print(features_df.iloc[:, :10].tail().to_string())
    
    # =========================================================================
    # Step 4: Generate Labels
    # =========================================================================
    display_header("Step 4: Generating Labels for Next-Day Prediction")
    
    print("\nLabel Configuration:")
    print("  UP threshold: > +1%")
    print("  DOWN threshold: < -1%")
    print("  FLAT: between -1% and +1%")
    
    label_generator = LabelGenerator(
        up_threshold=0.01,
        down_threshold=0.01,
        forward_days=1
    )
    
    labels_df = label_generator.generate_labels(stock_df)
    
    # Get statistics
    stats = label_generator.get_statistics(labels_df)
    print(f"\n{stats}")
    
    # Check class imbalance
    is_imbalanced, message = label_generator.check_class_imbalance(labels_df)
    print(f"\nClass Imbalance Check: {'⚠️ IMBALANCED' if is_imbalanced else '✓ BALANCED'}")
    print(f"  {message}")
    
    # Get class weights
    class_weights = label_generator.get_class_weights(labels_df)
    print(f"\nClass Weights (for imbalanced training):")
    for label, weight in class_weights.items():
        label_name = {1: 'UP', 0: 'FLAT', -1: 'DOWN'}[label]
        print(f"  {label_name} ({label:+d}): {weight:.3f}")
    
    # =========================================================================
    # Step 5: Combine Features and Labels
    # =========================================================================
    display_header("Step 5: Combining Features and Labels")
    
    # Align features and labels
    aligned_features, aligned_labels = label_generator.generate_labels_with_features(
        features_df=features_df,
        price_df=stock_df
    )
    
    print(f"\nAligned Dataset:")
    print(f"  Features: {aligned_features.shape}")
    print(f"  Labels: {aligned_labels.shape}")
    
    # Combine into single DataFrame for inspection
    combined_df = pd.concat([aligned_features, aligned_labels], axis=1)
    
    display_dataframe_info(combined_df, "Combined Features + Labels")
    
    # =========================================================================
    # Step 6: Display Final Dataset Sample
    # =========================================================================
    display_header("Step 6: Final Dataset Sample")
    
    # Select key columns for display
    display_cols = [
        # Price info
        'Return_1d', 'Return_5d', 'Volatility_10d',
        # Technical indicators
        'RSI_14', 'MACD', 'EMA_9', 'EMA_21',
        # Volume
        'Volume_Ratio_10d', 'OBV',
        # Market
        'Nifty_Returns_1d', 'Beta_20d',
        # Labels
        'forward_return', 'label', 'label_name'
    ]
    
    # Filter to available columns
    available_cols = [c for c in display_cols if c in combined_df.columns]
    
    print(f"\nSample Data (last 10 rows, key columns):")
    print(combined_df[available_cols].tail(10).to_string())
    
    # =========================================================================
    # Step 7: Summary Statistics
    # =========================================================================
    display_header("Step 7: Summary Statistics")
    
    print("\nFeature Statistics (numeric columns):")
    numeric_cols = combined_df.select_dtypes(include=[np.number]).columns
    summary = combined_df[numeric_cols].describe().T
    summary['missing'] = combined_df[numeric_cols].isna().sum()
    summary['missing_pct'] = (summary['missing'] / len(combined_df) * 100).round(2)
    
    print(summary[['count', 'mean', 'std', 'min', 'max', 'missing_pct']].head(20).to_string())
    
    # =========================================================================
    # Step 8: Save Combined Dataset
    # =========================================================================
    display_header("Step 8: Saving Dataset")
    
    output_dir = Path(DATA_DIR) / 'processed'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_file = output_dir / f"{SYMBOL.replace('.', '_')}_features_labels.parquet"
    combined_df.to_parquet(output_file)
    
    print(f"\nDataset saved to: {output_file}")
    print(f"  Size: {output_file.stat().st_size / 1024:.1f} KB")
    
    # =========================================================================
    # Final Summary
    # =========================================================================
    display_header("Summary")
    
    print(f"""
✓ Successfully processed {SYMBOL}

Data Summary:
  - Raw OHLCV rows: {len(stock_df)}
  - Feature rows: {len(features_df)}
  - Final aligned rows: {len(combined_df)}
  - Features generated: {len(features_df.columns)}
  - Date range: {combined_df.index.min().date()} to {combined_df.index.max().date()}

Label Distribution:
  - UP (+1): {stats.up_count} ({stats.up_pct:.1%})
  - FLAT (0): {stats.flat_count} ({stats.flat_pct:.1%})
  - DOWN (-1): {stats.down_count} ({stats.down_pct:.1%})

Files Created:
  - Raw data: {DATA_DIR}/stocks/{SYMBOL.replace('.', '_')}.parquet
  - Processed: {output_file}

Next Steps:
  1. Run this for all F&O stocks using collector.fetch_all_stocks()
  2. Train models using the training pipeline
  3. Evaluate and backtest predictions
""")


if __name__ == '__main__':
    main()
