"""
Example script to run backtesting on trained model.

This script:
1. Loads the trained model and preprocessor
2. Loads the processed data
3. Generates predictions on test data
4. Runs backtest simulation
5. Displays performance results
"""

import os
import sys
from pathlib import Path
from datetime import datetime
import pandas as pd
import numpy as np

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from ml_pipeline.training.trainer import Trainer
from ml_pipeline.data.preprocessor import DataPreprocessor
from ml_pipeline.evaluation.backtest import BacktestEngine, BacktestConfig


def find_latest_model(models_dir: str = "data/models") -> tuple:
    """Find the latest saved model and preprocessor files."""
    models_path = project_root / models_dir
    
    if not models_path.exists():
        raise FileNotFoundError(f"Models directory not found: {models_path}")
    
    # Find model files
    model_files = list(models_path.glob("ensemble_model_*.pkl"))
    if not model_files:
        raise FileNotFoundError("No trained model found. Run train_model.py first.")
    
    # Get latest model
    latest_model = max(model_files, key=lambda x: x.stat().st_mtime)
    
    # Find corresponding preprocessor
    model_timestamp = latest_model.stem.replace("ensemble_model_", "")
    preprocessor_file = models_path / f"preprocessor_{model_timestamp}.pkl"
    
    if not preprocessor_file.exists():
        raise FileNotFoundError(f"Preprocessor not found: {preprocessor_file}")
    
    return str(latest_model), str(preprocessor_file)


def load_processed_data(data_dir: str = "data/processed") -> pd.DataFrame:
    """Load the processed feature data from all stock files."""
    data_path = project_root / data_dir
    
    # Find all stock feature files
    feature_files = list(data_path.glob("*_features_labels.parquet"))
    
    if not feature_files:
        raise FileNotFoundError(f"No feature files found in {data_path}")
    
    print(f"Loading data from {len(feature_files)} stock files...")
    
    # Load and combine all files
    dfs = []
    for file in feature_files:
        try:
            stock_df = pd.read_parquet(file)
            # Extract symbol from filename
            symbol = file.stem.replace("_features_labels", "")
            
            # Reset index to get date as column
            if stock_df.index.name == 'date' or stock_df.index.name is not None:
                stock_df = stock_df.reset_index()
            
            # Ensure date column exists
            if 'date' not in stock_df.columns:
                # Try to find a date-like column
                date_cols = [c for c in stock_df.columns if 'date' in c.lower()]
                if date_cols:
                    stock_df['date'] = stock_df[date_cols[0]]
                else:
                    # Use index as date
                    stock_df = stock_df.reset_index()
                    if 'index' in stock_df.columns:
                        stock_df = stock_df.rename(columns={'index': 'date'})
            
            if 'symbol' not in stock_df.columns:
                stock_df['symbol'] = symbol
            dfs.append(stock_df)
        except Exception as e:
            print(f"  Warning: Could not load {file}: {e}")
    
    if not dfs:
        raise FileNotFoundError("No valid feature files could be loaded")
    
    # Combine all dataframes
    combined_df = pd.concat(dfs, ignore_index=True)
    print(f"✓ Combined data: {len(combined_df)} rows from {len(dfs)} stocks")
    
    return combined_df


def prepare_price_data(df: pd.DataFrame, data_collector=None) -> pd.DataFrame:
    """Prepare price data for backtesting by loading from data/stocks directory."""
    from pathlib import Path
    
    # Get unique symbols and dates from the feature data
    symbols = df['symbol'].unique()
    
    # Load raw price data for each symbol
    price_dfs = []
    data_dir = project_root / 'data' / 'stocks'
    
    print(f"  Loading price data from {data_dir}")
    
    for symbol in symbols:
        # Try to load stock data file
        stock_file = data_dir / f"{symbol}.parquet"
        
        if stock_file.exists():
            try:
                stock_df = pd.read_parquet(stock_file)
                # Reset index if date is in index
                if stock_df.index.name is not None:
                    stock_df = stock_df.reset_index()
                
                # Standardize column names to lowercase
                stock_df = stock_df.rename(columns={c: c.lower() for c in stock_df.columns})
                
                if 'date' in stock_df.columns:
                    stock_df['date'] = pd.to_datetime(stock_df['date'])
                
                stock_df['symbol'] = symbol
                price_dfs.append(stock_df[['date', 'symbol', 'open', 'high', 'low', 'close']])
            except Exception as e:
                print(f"  Warning: Could not load price data for {symbol}: {e}")
    
    if not price_dfs:
        raise ValueError("No price data available for backtesting")
    
    price_data = pd.concat(price_dfs, ignore_index=True)
    print(f"  ✓ Loaded price data: {len(price_data)} rows from {len(price_dfs)} stocks")
    return price_data


def generate_predictions(
    trainer: Trainer,
    df: pd.DataFrame,
) -> pd.DataFrame:
    """Generate predictions for backtesting."""
    # Get feature columns - exclude non-feature columns
    exclude_cols = [
        'date', 'symbol', 'label', 'open', 'high', 'low', 'close', 'volume', 'index',
        'forward_return', 'forward_close', 'target_label', 'target_return'
    ]
    feature_cols = [c for c in df.columns if c not in exclude_cols and not c.startswith('target')]
    
    # Filter numeric columns only
    feature_cols = [c for c in feature_cols if df[c].dtype in ['float64', 'int64', 'float32', 'int32']]
    
    # Get the feature names the model was trained on
    if hasattr(trainer.model, '_feature_names') and trainer.model._feature_names:
        trained_features = trainer.model._feature_names
        # Use only features that were used during training
        feature_cols = [c for c in feature_cols if c in trained_features]
        print(f"Using {len(feature_cols)} features (matched from {len(trained_features)} trained features)")
    else:
        print(f"Using {len(feature_cols)} features for prediction")
    
    # Prepare data
    X = df[feature_cols].copy()
    
    # Handle NaN values
    X = X.fillna(0)
    
    # Get predictions with probabilities
    predictions = trainer.predict(X)
    probabilities = trainer.predict_proba(X)
    
    # Create predictions DataFrame
    pred_df = pd.DataFrame({
        'date': df['date'].values if 'date' in df.columns else df.index,
        'symbol': df['symbol'].values if 'symbol' in df.columns else 'UNKNOWN',
        'prediction': predictions,
        'probability': np.max(probabilities, axis=1),
        'confidence': np.max(probabilities, axis=1) - np.mean(probabilities, axis=1),
    })
    
    # Add probability columns
    pred_df['prob_down'] = probabilities[:, 0]
    pred_df['prob_flat'] = probabilities[:, 1]
    pred_df['prob_up'] = probabilities[:, 2]
    
    return pred_df


def main():
    """Main function to run backtest."""
    print("=" * 70)
    print(" STOCK PREDICTION BACKTESTING")
    print("=" * 70)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Step 1: Find and load model
    print("\n1. LOADING MODEL")
    print("-" * 70)
    
    try:
        model_path, preprocessor_path = find_latest_model()
        print(f"Model: {model_path}")
        print(f"Preprocessor: {preprocessor_path}")
    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("\nPlease run training first:")
        print("  python -m ml_pipeline.examples.train_model")
        return
    
    # Load trainer
    trainer = Trainer()
    trainer.load_model(model_path)
    print("✓ Model loaded successfully")
    
    # Step 2: Load data
    print("\n2. LOADING DATA")
    print("-" * 70)
    
    df = load_processed_data()
    print(f"✓ Loaded data: {len(df)} rows, {len(df.columns)} columns")
    
    # Step 3: Prepare data for backtest
    print("\n3. PREPARING BACKTEST DATA")
    print("-" * 70)
    
    # Use a portion of data for backtest (last 6 months)
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])
        cutoff_date = df['date'].max() - pd.Timedelta(days=180)
        backtest_df = df[df['date'] >= cutoff_date].copy()
    else:
        # Use last 252 trading days (1 year)
        backtest_df = df.tail(252).copy()
    
    print(f"Backtest period: {backtest_df['date'].min()} to {backtest_df['date'].max()}")
    print(f"Total samples: {len(backtest_df)}")
    
    # Prepare price data
    price_data = prepare_price_data(backtest_df)
    print(f"✓ Price data prepared: {len(price_data)} rows")
    
    # Step 4: Generate predictions
    print("\n4. GENERATING PREDICTIONS")
    print("-" * 70)
    
    predictions = generate_predictions(trainer, backtest_df)
    print(f"✓ Generated predictions: {len(predictions)} rows")
    
    # Prediction distribution
    pred_counts = predictions['prediction'].value_counts()
    print(f"\nPrediction Distribution:")
    for pred, count in pred_counts.items():
        label = {-1: 'DOWN', 0: 'FLAT', 1: 'UP'}.get(pred, str(pred))
        print(f"  {label}: {count} ({count/len(predictions)*100:.1f}%)")
    
    # Step 5: Configure backtest
    print("\n5. CONFIGURING BACKTEST")
    print("-" * 70)
    
    config = BacktestConfig(
        initial_capital=1_000_000.0,  # ₹10 Lakh
        position_size_pct=0.10,  # 10% per position
        max_positions=10,
        min_probability=0.40,
        stop_loss_pct=0.05,  # 5% stop loss
        take_profit_pct=0.10,  # 10% take profit
        max_holding_days=10,
        trade_up=True,
        trade_down=True,
        trade_flat=False,
    )
    
    print(f"Initial Capital: ₹{config.initial_capital:,.0f}")
    print(f"Position Size: {config.position_size_pct*100:.0f}%")
    print(f"Max Positions: {config.max_positions}")
    print(f"Stop Loss: {config.stop_loss_pct*100:.0f}%")
    print(f"Take Profit: {config.take_profit_pct*100:.0f}%")
    print(f"Max Holding: {config.max_holding_days} days")
    
    # Step 6: Run backtest
    print("\n6. RUNNING BACKTEST")
    print("-" * 70)
    
    engine = BacktestEngine(config)
    result = engine.run(predictions, price_data, show_progress=True)
    
    # Step 7: Display results
    print("\n7. BACKTEST RESULTS")
    print("-" * 70)
    
    result.print_summary()
    
    # Additional analysis
    if len(result.trades) > 0:
        print("\nTRADE ANALYSIS")
        print("-" * 70)
        
        trades = result.trades
        
        # By direction
        print("\nBy Direction:")
        for direction in ['LONG', 'SHORT']:
            dir_trades = trades[trades['direction'] == direction]
            if len(dir_trades) > 0:
                win_rate = (dir_trades['pnl'] > 0).mean()
                avg_return = dir_trades['return'].mean()
                print(f"  {direction}: {len(dir_trades)} trades, Win Rate: {win_rate*100:.1f}%, Avg Return: {avg_return*100:.2f}%")
        
        # By exit reason
        print("\nBy Exit Reason:")
        for reason in trades['exit_reason'].unique():
            reason_trades = trades[trades['exit_reason'] == reason]
            win_rate = (reason_trades['pnl'] > 0).mean()
            print(f"  {reason}: {len(reason_trades)} trades, Win Rate: {win_rate*100:.1f}%")
        
        # Top winning trades
        print("\nTop 5 Winning Trades:")
        top_winners = trades.nlargest(5, 'pnl')[['symbol', 'direction', 'entry_date', 'pnl', 'return']]
        for _, trade in top_winners.iterrows():
            print(f"  {trade['symbol']} {trade['direction']}: ₹{trade['pnl']:,.0f} ({trade['return']*100:.1f}%)")
        
        # Top losing trades
        print("\nTop 5 Losing Trades:")
        top_losers = trades.nsmallest(5, 'pnl')[['symbol', 'direction', 'entry_date', 'pnl', 'return']]
        for _, trade in top_losers.iterrows():
            print(f"  {trade['symbol']} {trade['direction']}: ₹{trade['pnl']:,.0f} ({trade['return']*100:.1f}%)")
    
    # Step 8: Save results
    print("\n8. SAVING RESULTS")
    print("-" * 70)
    
    results_dir = project_root / "data" / "backtest_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Save equity curve
    equity_file = results_dir / f"equity_curve_{timestamp}.csv"
    result.equity_curve.to_csv(equity_file)
    print(f"✓ Equity curve saved: {equity_file}")
    
    # Save trades
    if len(result.trades) > 0:
        trades_file = results_dir / f"trades_{timestamp}.csv"
        result.trades.to_csv(trades_file, index=False)
        print(f"✓ Trades saved: {trades_file}")
    
    print("\n" + "=" * 70)
    print(" BACKTEST COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
