"""
Script to Generate Features for All F&O Stocks

This script loads stored OHLCV data and generates features for all stocks.

Usage:
    python -m ml_pipeline.examples.generate_all_features
"""

import sys
import os
from pathlib import Path
from datetime import datetime
import time
import pandas as pd

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from ml_pipeline.data.data_collector import DataCollector
from ml_pipeline.data.feature_engineer import FeatureEngineer
from ml_pipeline.data.label_generator import LabelGenerator


def main():
    """Generate features for all F&O stocks."""
    
    # Configuration
    DATA_DIR = str(Path(__file__).parent.parent.parent / 'data')
    OUTPUT_DIR = Path(DATA_DIR) / 'processed'
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    print("=" * 60)
    print(" Feature Generation for All F&O Stocks")
    print("=" * 60)
    print(f"\nData Directory: {DATA_DIR}")
    print(f"Output Directory: {OUTPUT_DIR}")
    
    # Initialize components
    collector = DataCollector(data_dir=DATA_DIR)
    engineer = FeatureEngineer(verbose=False)
    label_generator = LabelGenerator(
        up_threshold=0.01,
        down_threshold=0.01,
        forward_days=1
    )
    
    # Load Nifty data for market features
    print("\n" + "-" * 60)
    print("Loading Nifty 50 data for market features...")
    print("-" * 60)
    
    nifty_df = collector.load_stock_data('^NSEI')
    if nifty_df is not None:
        print(f"✓ Nifty 50: {len(nifty_df)} rows")
        index_data = {'^NSEI': nifty_df}
    else:
        print("✗ Nifty 50 data not found. Market features will be limited.")
        index_data = None
    
    # Get list of available stocks
    available_stocks = collector.get_available_stocks()
    # Filter out index data
    stock_symbols = [s for s in available_stocks if not s.startswith('^')]
    
    print("\n" + "-" * 60)
    print(f"Generating features for {len(stock_symbols)} stocks...")
    print("-" * 60)
    
    successful = 0
    failed = 0
    results = []
    
    for i, symbol in enumerate(stock_symbols, 1):
        try:
            # Load stock data
            stock_df = collector.load_stock_data(symbol)
            
            if stock_df is None or stock_df.empty:
                print(f"  [{i}/{len(stock_symbols)}] {symbol}: No data available")
                failed += 1
                continue
            
            # Generate features
            features_df = engineer.generate_features(stock_df, index_data=index_data)
            
            if features_df.empty:
                print(f"  [{i}/{len(stock_symbols)}] {symbol}: Feature generation failed (empty result)")
                failed += 1
                continue
            
            # Generate labels
            labels_df = label_generator.generate_labels(stock_df)
            
            # Align features and labels
            aligned_features, aligned_labels = label_generator.generate_labels_with_features(
                features_df=features_df,
                price_df=stock_df
            )
            
            # Combine features and labels
            combined_df = pd.concat([aligned_features, aligned_labels], axis=1)
            
            # Save to parquet
            output_file = OUTPUT_DIR / f"{symbol.replace('.', '_')}_features_labels.parquet"
            combined_df.to_parquet(output_file)
            
            # Get label distribution
            stats = label_generator.get_statistics(aligned_labels)
            
            print(f"  [{i}/{len(stock_symbols)}] {symbol}: {len(combined_df)} samples, "
                  f"UP={stats.up_count}, FLAT={stats.flat_count}, DOWN={stats.down_count}")
            
            successful += 1
            results.append({
                'symbol': symbol,
                'samples': len(combined_df),
                'features': len(features_df.columns),
                'up': stats.up_count,
                'flat': stats.flat_count,
                'down': stats.down_count
            })
            
        except Exception as e:
            print(f"  [{i}/{len(stock_symbols)}] {symbol}: Error - {str(e)[:50]}")
            failed += 1
    
    # Summary
    print("\n" + "=" * 60)
    print(" Summary")
    print("=" * 60)
    
    print(f"\nTotal Stocks: {len(stock_symbols)}")
    print(f"Successful: {successful}")
    print(f"Failed: {failed}")
    
    if results:
        # Create summary DataFrame
        summary_df = pd.DataFrame(results)
        
        # Save summary
        summary_file = OUTPUT_DIR / 'feature_generation_summary.csv'
        summary_df.to_csv(summary_file, index=False)
        
        print(f"\nFeature Generation Statistics:")
        print(f"  Total samples across all stocks: {summary_df['samples'].sum()}")
        print(f"  Average samples per stock: {summary_df['samples'].mean():.0f}")
        print(f"  Total UP labels: {summary_df['up'].sum()}")
        print(f"  Total FLAT labels: {summary_df['flat'].sum()}")
        print(f"  Total DOWN labels: {summary_df['down'].sum()}")
        
        print(f"\nSummary saved to: {summary_file}")
    
    print(f"\nProcessed files saved to: {OUTPUT_DIR}")
    
    print("\n" + "=" * 60)
    print(" Feature generation complete!")
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Review the generated features")
    print("  2. Train the ML model using the training pipeline")
    print("  3. Evaluate and backtest predictions")


if __name__ == '__main__':
    main()
