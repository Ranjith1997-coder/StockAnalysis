"""
Script to Fetch Data for All F&O Stocks

This script fetches historical OHLCV data for all F&O stocks
and the Nifty 50 index for market features.

Usage:
    python -m ml_pipeline.examples.fetch_all_stocks
"""

import sys
import os
from pathlib import Path
from datetime import datetime
import time

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from ml_pipeline.data.data_collector import DataCollector, fetch_index_data


def main():
    """Fetch data for all F&O stocks."""
    
    # Configuration
    START_DATE = '2020-01-01'
    END_DATE = datetime.now().strftime('%Y-%m-%d')
    # Use absolute path to ensure consistency
    DATA_DIR = str(Path(__file__).parent.parent.parent / 'data')
    
    print("=" * 60)
    print(" F&O Stock Data Collection")
    print("=" * 60)
    print(f"\nStart Date: {START_DATE}")
    print(f"End Date: {END_DATE}")
    print(f"Data Directory: {DATA_DIR}")
    
    # Initialize collector
    collector = DataCollector(data_dir=DATA_DIR)
    
    # Get list of F&O stocks
    fno_stocks = collector.fno_stocks
    print(f"\nTotal F&O stocks to fetch: {len(fno_stocks)}")
    
    # First, fetch index data (Nifty 50)
    print("\n" + "-" * 60)
    print("Fetching Index Data...")
    print("-" * 60)
    
    index_data = fetch_index_data(
        symbols=['^NSEI'],  # Nifty 50
        start_date=START_DATE,
        end_date=END_DATE,
        data_dir=DATA_DIR
    )
    
    if '^NSEI' in index_data:
        print(f"✓ Nifty 50: {len(index_data['^NSEI'])} rows")
    else:
        print("✗ Failed to fetch Nifty 50 data")
    
    # Fetch all F&O stocks
    print("\n" + "-" * 60)
    print("Fetching F&O Stock Data...")
    print("-" * 60)
    
    results = collector.fetch_all_stocks(
        start_date=START_DATE,
        end_date=END_DATE,
        validate=True,
        save=True
    )
    
    # Summary
    print("\n" + "=" * 60)
    print(" Summary")
    print("=" * 60)
    
    successful = sum(1 for df, _ in results.values() if df is not None)
    valid = sum(1 for _, report in results.values() if report and report.is_valid)
    failed = len(results) - successful
    
    print(f"\nTotal Stocks: {len(results)}")
    print(f"Successfully Fetched: {successful}")
    print(f"Valid Data: {valid}")
    print(f"Failed: {failed}")
    
    # List failed stocks
    if failed > 0:
        print("\nFailed Stocks:")
        for symbol, (df, report) in results.items():
            if df is None:
                print(f"  - {symbol}")
    
    # List stocks with quality issues
    quality_issues = []
    for symbol, (df, report) in results.items():
        if df is not None and report and not report.is_valid:
            quality_issues.append((symbol, report.issues))
    
    if quality_issues:
        print("\nStocks with Quality Issues:")
        for symbol, issues in quality_issues[:10]:  # Show first 10
            print(f"  - {symbol}: {issues[0] if issues else 'Unknown'}")
        if len(quality_issues) > 10:
            print(f"  ... and {len(quality_issues) - 10} more")
    
    print("\n" + "=" * 60)
    print(" Data collection complete!")
    print("=" * 60)
    print(f"\nData saved to: {DATA_DIR}/stocks/")
    print("\nNext steps:")
    print("  1. Generate features for all stocks")
    print("  2. Train the ML model")
    print("  3. Evaluate and backtest")


if __name__ == '__main__':
    main()
