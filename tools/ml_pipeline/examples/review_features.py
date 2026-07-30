"""
Script to Review and Analyze Generated Features

This script provides comprehensive analysis of the generated features including:
- Feature statistics and distributions
- Missing value analysis
- Correlation analysis
- Label distribution across stocks
- Feature importance preview

Usage:
    python -m ml_pipeline.examples.review_features
"""

import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import numpy as np

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


def load_all_processed_data(processed_dir: Path) -> dict:
    """Load all processed feature files."""
    data = {}
    parquet_files = list(processed_dir.glob('*_features_labels.parquet'))
    
    for file in parquet_files:
        symbol = file.stem.replace('_features_labels', '').replace('_', '.')
        try:
            df = pd.read_parquet(file)
            data[symbol] = df
        except Exception as e:
            print(f"Error loading {file}: {e}")
    
    return data


def analyze_feature_statistics(data: dict) -> pd.DataFrame:
    """Analyze statistics for all features across all stocks."""
    # Combine all data
    all_dfs = []
    for symbol, df in data.items():
        df_copy = df.copy()
        df_copy['symbol'] = symbol
        all_dfs.append(df_copy)
    
    combined = pd.concat(all_dfs, axis=0)
    
    # Get feature columns (exclude labels and symbol)
    feature_cols = [c for c in combined.columns if c not in ['symbol', 'forward_return', 'label', 'label_name']]
    
    # Calculate statistics
    stats = []
    for col in feature_cols:
        col_stats = {
            'feature': col,
            'count': combined[col].count(),
            'mean': combined[col].mean(),
            'std': combined[col].std(),
            'min': combined[col].min(),
            '25%': combined[col].quantile(0.25),
            '50%': combined[col].quantile(0.50),
            '75%': combined[col].quantile(0.75),
            'max': combined[col].max(),
            'missing': combined[col].isna().sum(),
            'missing_pct': combined[col].isna().sum() / len(combined) * 100,
            'zeros': (combined[col] == 0).sum(),
            'zeros_pct': (combined[col] == 0).sum() / len(combined) * 100
        }
        stats.append(col_stats)
    
    return pd.DataFrame(stats)


def analyze_label_distribution(data: dict) -> pd.DataFrame:
    """Analyze label distribution across all stocks."""
    results = []
    
    for symbol, df in data.items():
        label_counts = df['label'].value_counts()
        total = len(df)
        
        results.append({
            'symbol': symbol,
            'total_samples': total,
            'up_count': label_counts.get(1, 0),
            'flat_count': label_counts.get(0, 0),
            'down_count': label_counts.get(-1, 0),
            'up_pct': label_counts.get(1, 0) / total * 100,
            'flat_pct': label_counts.get(0, 0) / total * 100,
            'down_pct': label_counts.get(-1, 0) / total * 100,
            'date_start': df.index.min().strftime('%Y-%m-%d'),
            'date_end': df.index.max().strftime('%Y-%m-%d')
        })
    
    return pd.DataFrame(results)


def analyze_correlations(data: dict, sample_size: int = 5) -> pd.DataFrame:
    """Analyze feature correlations with labels for a sample of stocks."""
    # Sample a few stocks
    symbols = list(data.keys())[:sample_size]
    
    all_corrs = []
    for symbol in symbols:
        df = data[symbol]
        feature_cols = [c for c in df.columns if c not in ['forward_return', 'label', 'label_name']]
        
        # Calculate correlation with label
        for col in feature_cols:
            corr = df[col].corr(df['label'])
            all_corrs.append({
                'symbol': symbol,
                'feature': col,
                'correlation': corr
            })
    
    corr_df = pd.DataFrame(all_corrs)
    
    # Average correlation across stocks
    avg_corr = corr_df.groupby('feature')['correlation'].mean().sort_values(ascending=False)
    
    return avg_corr


def analyze_feature_importance_preview(data: dict) -> pd.DataFrame:
    """Quick preview of feature importance using simple correlation with forward return."""
    # Combine all data
    all_dfs = []
    for symbol, df in data.items():
        all_dfs.append(df)
    
    combined = pd.concat(all_dfs, axis=0)
    
    feature_cols = [c for c in combined.columns if c not in ['forward_return', 'label', 'label_name']]
    
    # Calculate correlation with forward return
    correlations = {}
    for col in feature_cols:
        corr = combined[col].corr(combined['forward_return'])
        correlations[col] = corr
    
    # Sort by absolute correlation
    sorted_corrs = sorted(correlations.items(), key=lambda x: abs(x[1]), reverse=True)
    
    return pd.DataFrame(sorted_corrs, columns=['feature', 'correlation_with_return'])


def main():
    """Main function to review generated features."""
    
    # Configuration
    PROCESSED_DIR = Path(__file__).parent.parent.parent / 'data' / 'processed'
    
    print("=" * 70)
    print(" Feature Review and Analysis")
    print("=" * 70)
    print(f"\nProcessed Data Directory: {PROCESSED_DIR}")
    
    # Check if processed data exists
    if not PROCESSED_DIR.exists():
        print("\n✗ No processed data found. Please run feature generation first:")
        print("  python -m ml_pipeline.examples.generate_all_features")
        return
    
    # Load all processed data
    print("\n" + "-" * 70)
    print("Loading processed data...")
    print("-" * 70)
    
    data = load_all_processed_data(PROCESSED_DIR)
    
    if not data:
        print("\n✗ No processed data files found.")
        return
    
    print(f"✓ Loaded {len(data)} stocks")
    
    # =========================================================================
    # 1. OVERALL STATISTICS
    # =========================================================================
    print("\n" + "=" * 70)
    print(" 1. OVERALL STATISTICS")
    print("=" * 70)
    
    total_samples = sum(len(df) for df in data.values())
    total_features = len([c for c in list(data.values())[0].columns if c not in ['forward_return', 'label', 'label_name']])
    
    print(f"\nTotal Stocks: {len(data)}")
    print(f"Total Samples: {total_samples:,}")
    print(f"Total Features: {total_features}")
    
    # =========================================================================
    # 2. LABEL DISTRIBUTION
    # =========================================================================
    print("\n" + "=" * 70)
    print(" 2. LABEL DISTRIBUTION")
    print("=" * 70)
    
    label_dist = analyze_label_distribution(data)
    
    # Overall distribution
    total_up = label_dist['up_count'].sum()
    total_flat = label_dist['flat_count'].sum()
    total_down = label_dist['down_count'].sum()
    total_all = total_up + total_flat + total_down
    
    print(f"\nOverall Label Distribution:")
    print(f"  UP (+1):   {total_up:6d} ({total_up/total_all*100:.1f}%)")
    print(f"  FLAT (0):  {total_flat:6d} ({total_flat/total_all*100:.1f}%)")
    print(f"  DOWN (-1): {total_down:6d} ({total_down/total_all*100:.1f}%)")
    
    print(f"\nPer-Stock Statistics:")
    print(f"  Average samples per stock: {label_dist['total_samples'].mean():.0f}")
    print(f"  Min samples: {label_dist['total_samples'].min()} ({label_dist.loc[label_dist['total_samples'].idxmin(), 'symbol']})")
    print(f"  Max samples: {label_dist['total_samples'].max()} ({label_dist.loc[label_dist['total_samples'].idxmax(), 'symbol']})")
    
    # Show sample of stocks
    print(f"\nSample Stocks (first 10):")
    print(label_dist[['symbol', 'total_samples', 'up_pct', 'flat_pct', 'down_pct']].head(10).to_string(index=False))
    
    # =========================================================================
    # 3. FEATURE STATISTICS
    # =========================================================================
    print("\n" + "=" * 70)
    print(" 3. FEATURE STATISTICS")
    print("=" * 70)
    
    feature_stats = analyze_feature_statistics(data)
    
    print(f"\nFeature Statistics Summary:")
    print(f"  Features with missing values: {(feature_stats['missing'] > 0).sum()}")
    print(f"  Features with >5% missing: {(feature_stats['missing_pct'] > 5).sum()}")
    print(f"  Features with >10% zeros: {(feature_stats['zeros_pct'] > 10).sum()}")
    
    print(f"\nTop 20 Features by Variance (std):")
    top_variance = feature_stats.nlargest(20, 'std')[['feature', 'mean', 'std', 'min', 'max']]
    print(top_variance.to_string(index=False))
    
    print(f"\nFeatures with Missing Values:")
    missing_features = feature_stats[feature_stats['missing'] > 0][['feature', 'missing', 'missing_pct']]
    if len(missing_features) > 0:
        print(missing_features.to_string(index=False))
    else:
        print("  No missing values in any feature!")
    
    # =========================================================================
    # 4. FEATURE CORRELATIONS WITH LABELS
    # =========================================================================
    print("\n" + "=" * 70)
    print(" 4. FEATURE CORRELATIONS WITH LABELS")
    print("=" * 70)
    
    avg_correlations = analyze_correlations(data, sample_size=10)
    
    print(f"\nTop 15 Features - Positive Correlation with UP label:")
    print(avg_correlations.head(15).to_string())
    
    print(f"\nTop 15 Features - Negative Correlation (predicts DOWN):")
    print(avg_correlations.tail(15).to_string())
    
    # =========================================================================
    # 5. FEATURE IMPORTANCE PREVIEW
    # =========================================================================
    print("\n" + "=" * 70)
    print(" 5. FEATURE IMPORTANCE PREVIEW (Correlation with Forward Return)")
    print("=" * 70)
    
    importance = analyze_feature_importance_preview(data)
    
    print(f"\nTop 20 Features by Correlation with Forward Return:")
    print(importance.head(20).to_string(index=False))
    
    # =========================================================================
    # 6. SAVE REPORTS
    # =========================================================================
    print("\n" + "=" * 70)
    print(" 6. SAVING REPORTS")
    print("=" * 70)
    
    reports_dir = PROCESSED_DIR / 'reports'
    reports_dir.mkdir(exist_ok=True)
    
    # Save label distribution
    label_file = reports_dir / 'label_distribution.csv'
    label_dist.to_csv(label_file, index=False)
    print(f"✓ Label distribution saved to: {label_file}")
    
    # Save feature statistics
    stats_file = reports_dir / 'feature_statistics.csv'
    feature_stats.to_csv(stats_file, index=False)
    print(f"✓ Feature statistics saved to: {stats_file}")
    
    # Save correlations
    corr_file = reports_dir / 'feature_correlations.csv'
    importance.to_csv(corr_file, index=False)
    print(f"✓ Feature correlations saved to: {corr_file}")
    
    # =========================================================================
    # 7. RECOMMENDATIONS
    # =========================================================================
    print("\n" + "=" * 70)
    print(" 7. RECOMMENDATIONS")
    print("=" * 70)
    
    print("""
Based on the feature analysis:

1. DATA QUALITY:
   - Check features with high missing percentages
   - Consider removing features with >10% missing values
   - Review features with high zero percentages

2. FEATURE SELECTION:
   - Focus on features with high correlation to labels
   - Consider removing highly correlated features (multicollinearity)
   - Use feature importance from trained models for final selection

3. LABEL BALANCE:
   - The dataset appears to have class imbalance (more FLAT than UP/DOWN)
   - Consider using class weights during training
   - Or use oversampling/undersampling techniques

4. NEXT STEPS:
   - Train the ML model using the training pipeline
   - Use cross-validation to evaluate feature importance
   - Consider feature engineering based on model feedback
""")
    
    print("\n" + "=" * 70)
    print(" Feature review complete!")
    print("=" * 70)


if __name__ == '__main__':
    main()
