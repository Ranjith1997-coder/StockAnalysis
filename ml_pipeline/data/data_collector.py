"""
Data Collector Module for ML Stock Prediction Pipeline.

This module handles fetching, validating, and storing historical stock data
from Yahoo Finance (yfinance) for F&O stocks.

Key Components:
- DataCollector: Main class for fetching and storing stock data
- DataValidator: Validates data quality and completeness
- DataStorage: Handles local storage in parquet format

Data Flow:
1. Load list of F&O stocks from NSE or local file
2. Fetch historical OHLCV data for each stock using yfinance
3. Validate data quality (no missing values, correct format)
4. Store data in parquet format for efficient access
5. Support incremental updates (fetch only new data)

Usage:
    from ml_pipeline.data.data_collector import DataCollector
    
    collector = DataCollector(data_dir='./data')
    
    # Fetch data for all F&O stocks
    collector.fetch_all_stocks(start_date='2020-01-01', end_date='2024-12-31')
    
    # Fetch data for specific stocks
    collector.fetch_stocks(['RELIANCE.NS', 'TCS.NS'], start_date='2020-01-01')
    
    # Load stored data
    data = collector.load_stock_data('RELIANCE.NS')
"""

import os
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass

import pandas as pd
import numpy as np
import yfinance as yf

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class DataQualityReport:
    """
    Report on data quality for a single stock.
    
    Attributes:
        symbol: Stock symbol
        total_rows: Total number of rows fetched
        missing_values: Count of missing values per column
        duplicate_dates: Number of duplicate date entries
        zero_volume_days: Days with zero volume
        price_anomalies: Days with suspicious price movements (>20%)
        is_valid: Whether data passes quality checks
        issues: List of identified issues
    """
    symbol: str
    total_rows: int
    missing_values: Dict[str, int]
    duplicate_dates: int
    zero_volume_days: int
    price_anomalies: int
    is_valid: bool
    issues: List[str]
    
    def __str__(self) -> str:
        """String representation of the quality report."""
        status = "✓ VALID" if self.is_valid else "✗ INVALID"
        lines = [
            f"Data Quality Report for {self.symbol}",
            f"Status: {status}",
            f"Total Rows: {self.total_rows}",
            f"Missing Values: {self.missing_values}",
            f"Duplicate Dates: {self.duplicate_dates}",
            f"Zero Volume Days: {self.zero_volume_days}",
            f"Price Anomalies: {self.price_anomalies}",
        ]
        if self.issues:
            lines.append("Issues:")
            for issue in self.issues:
                lines.append(f"  - {issue}")
        return "\n".join(lines)


class DataValidator:
    """
    Validates stock data quality and completeness.
    
    This class performs various checks on fetched data to ensure
    it meets quality standards before being used for training.
    
    Validation Checks:
    1. Missing Values: Checks for NaN values in OHLCV columns
    2. Duplicate Dates: Identifies duplicate date entries
    3. Zero Volume: Flags days with zero trading volume
    4. Price Anomalies: Detects suspicious price movements
    5. Data Continuity: Checks for large gaps in dates
    6. OHLC Consistency: Ensures High >= Low, Open/Close within range
    
    Example:
        validator = DataValidator()
        report = validator.validate(df, symbol='RELIANCE.NS')
        if not report.is_valid:
            print(f"Data quality issues: {report.issues}")
    """
    
    # Thresholds for validation
    MAX_MISSING_PCT = 0.05  # Max 5% missing values allowed
    MAX_ZERO_VOLUME_PCT = 0.10  # Max 10% zero volume days
    PRICE_ANOMALY_THRESHOLD = 0.25  # 25% daily move flagged as anomaly
    MAX_GAP_DAYS = 7  # Max allowed gap between consecutive trading days
    
    def validate(self, df: pd.DataFrame, symbol: str) -> DataQualityReport:
        """
        Validate stock data and return a quality report.
        
        Args:
            df: DataFrame with OHLCV data (columns: Open, High, Low, Close, Volume)
            symbol: Stock symbol for reporting
            
        Returns:
            DataQualityReport with validation results
        """
        issues = []
        
        if df is None or df.empty:
            return DataQualityReport(
                symbol=symbol,
                total_rows=0,
                missing_values={},
                duplicate_dates=0,
                zero_volume_days=0,
                price_anomalies=0,
                is_valid=False,
                issues=["No data available"]
            )
        
        # Check missing values
        missing = df.isnull().sum().to_dict()
        missing_pct = df.isnull().sum().sum() / (len(df) * len(df.columns))
        if missing_pct > self.MAX_MISSING_PCT:
            issues.append(f"High missing value percentage: {missing_pct:.2%}")
        
        # Check duplicate dates
        duplicate_dates = df.index.duplicated().sum()
        if duplicate_dates > 0:
            issues.append(f"Found {duplicate_dates} duplicate dates")
        
        # Check zero volume days
        zero_volume_days = (df['Volume'] == 0).sum() if 'Volume' in df.columns else 0
        zero_volume_pct = zero_volume_days / len(df)
        if zero_volume_pct > self.MAX_ZERO_VOLUME_PCT:
            issues.append(f"High zero volume percentage: {zero_volume_pct:.2%}")
        
        # Check price anomalies (daily returns > threshold)
        daily_returns = df['Close'].pct_change()
        price_anomalies = (daily_returns.abs() > self.PRICE_ANOMALY_THRESHOLD).sum()
        if price_anomalies > len(df) * 0.01:  # More than 1% anomalies
            issues.append(f"High number of price anomalies: {price_anomalies}")
        
        # Check OHLC consistency
        ohlc_issues = self._check_ohlc_consistency(df)
        if ohlc_issues:
            issues.extend(ohlc_issues)
        
        # Check data continuity
        continuity_issues = self._check_continuity(df)
        if continuity_issues:
            issues.extend(continuity_issues)
        
        # Determine if data is valid
        is_valid = len(issues) == 0
        
        return DataQualityReport(
            symbol=symbol,
            total_rows=len(df),
            missing_values=missing,
            duplicate_dates=duplicate_dates,
            zero_volume_days=zero_volume_days,
            price_anomalies=price_anomalies,
            is_valid=is_valid,
            issues=issues
        )
    
    def _check_ohlc_consistency(self, df: pd.DataFrame) -> List[str]:
        """
        Check if OHLC values are logically consistent.
        
        Rules:
        - High should be >= Low
        - High should be >= Open and Close
        - Low should be <= Open and Close
        
        Args:
            df: DataFrame with OHLC data
            
        Returns:
            List of inconsistency descriptions
        """
        issues = []
        
        # High < Low check
        high_low_violations = (df['High'] < df['Low']).sum()
        if high_low_violations > 0:
            issues.append(f"High < Low violations: {high_low_violations}")
        
        # High < Open or High < Close
        high_violations = ((df['High'] < df['Open']) | (df['High'] < df['Close'])).sum()
        if high_violations > 0:
            issues.append(f"High < Open/Close violations: {high_violations}")
        
        # Low > Open or Low > Close
        low_violations = ((df['Low'] > df['Open']) | (df['Low'] > df['Close'])).sum()
        if low_violations > 0:
            issues.append(f"Low > Open/Close violations: {low_violations}")
        
        return issues
    
    def _check_continuity(self, df: pd.DataFrame) -> List[str]:
        """
        Check for large gaps in the date sequence.
        
        Trading days should be consecutive (excluding weekends/holidays).
        Large gaps might indicate missing data.
        
        Args:
            df: DataFrame with datetime index
            
        Returns:
            List of continuity issues
        """
        issues = []
        
        if len(df) < 2:
            return issues
        
        # Calculate gaps between consecutive dates
        date_diffs = df.index.to_series().diff().dropna()
        
        # Find gaps larger than threshold
        large_gaps = date_diffs[date_diffs > timedelta(days=self.MAX_GAP_DAYS)]
        
        if len(large_gaps) > 0:
            for gap_date, gap in large_gaps.items():
                issues.append(f"Large gap of {gap.days} days after {gap_date.date()}")
        
        return issues


class DataStorage:
    """
    Handles local storage of stock data in parquet format.
    
    Parquet is chosen for:
    - Efficient compression (smaller file sizes)
    - Fast columnar access (quick feature extraction)
    - Native pandas support
    - Preservation of data types
    
    Directory Structure:
        data_dir/
        ├── stocks/
        │   ├── RELIANCE.NS.parquet
        │   ├── TCS.NS.parquet
        │   └── ...
        ├── indices/
        │   ├── NIFTY50.parquet
        │   └── BANKNIFTY.parquet
        └── metadata.json
    
    Example:
        storage = DataStorage(data_dir='./data')
        
        # Save stock data
        storage.save_stock_data('RELIANCE.NS', df)
        
        # Load stock data
        df = storage.load_stock_data('RELIANCE.NS')
        
        # Get list of available stocks
        stocks = storage.get_available_stocks()
    """
    
    def __init__(self, data_dir: str = './data'):
        """
        Initialize the data storage.
        
        Args:
            data_dir: Root directory for data storage
        """
        self.data_dir = Path(data_dir)
        self.stocks_dir = self.data_dir / 'stocks'
        self.indices_dir = self.data_dir / 'indices'
        self.metadata_file = self.data_dir / 'metadata.json'
        
        # Create directories if they don't exist
        self.stocks_dir.mkdir(parents=True, exist_ok=True)
        self.indices_dir.mkdir(parents=True, exist_ok=True)
    
    def save_stock_data(
        self, 
        symbol: str, 
        df: pd.DataFrame,
        mode: str = 'overwrite'
    ) -> bool:
        """
        Save stock data to parquet file.
        
        Args:
            symbol: Stock symbol (e.g., 'RELIANCE.NS')
            df: DataFrame with OHLCV data
            mode: 'overwrite' or 'append'
            
        Returns:
            True if save was successful
        """
        try:
            # Ensure date index is properly formatted
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            
            # Sort by date
            df = df.sort_index()
            
            # Remove duplicates
            df = df[~df.index.duplicated(keep='last')]
            
            file_path = self.stocks_dir / f"{symbol.replace('.', '_')}.parquet"
            
            if mode == 'append' and file_path.exists():
                existing_df = pd.read_parquet(file_path)
                df = pd.concat([existing_df, df])
                df = df[~df.index.duplicated(keep='last')]
                df = df.sort_index()
            
            df.to_parquet(file_path, index=True)
            logger.info(f"Saved {len(df)} rows for {symbol} to {file_path}")
            return True
            
        except Exception as e:
            logger.error(f"Error saving data for {symbol}: {e}")
            return False
    
    def load_stock_data(
        self, 
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Optional[pd.DataFrame]:
        """
        Load stock data from parquet file.
        
        Args:
            symbol: Stock symbol (e.g., 'RELIANCE.NS')
            start_date: Optional start date filter (YYYY-MM-DD)
            end_date: Optional end date filter (YYYY-MM-DD)
            
        Returns:
            DataFrame with OHLCV data, or None if not found
        """
        file_path = self.stocks_dir / f"{symbol.replace('.', '_')}.parquet"
        
        if not file_path.exists():
            logger.warning(f"No data file found for {symbol}")
            return None
        
        try:
            df = pd.read_parquet(file_path)
            
            # Ensure datetime index
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            
            # Apply date filters
            if start_date:
                df = df[df.index >= pd.to_datetime(start_date)]
            if end_date:
                df = df[df.index <= pd.to_datetime(end_date)]
            
            return df
            
        except Exception as e:
            logger.error(f"Error loading data for {symbol}: {e}")
            return None
    
    def get_available_stocks(self) -> List[str]:
        """
        Get list of stocks with stored data.
        
        Returns:
            List of stock symbols
        """
        parquet_files = list(self.stocks_dir.glob('*.parquet'))
        return [f.stem.replace('_', '.') for f in parquet_files]
    
    def get_data_info(self, symbol: str) -> Optional[Dict]:
        """
        Get information about stored data for a stock.
        
        Args:
            symbol: Stock symbol
            
        Returns:
            Dictionary with data info or None if not found
        """
        df = self.load_stock_data(symbol)
        if df is None:
            return None
        
        return {
            'symbol': symbol,
            'start_date': df.index.min().strftime('%Y-%m-%d'),
            'end_date': df.index.max().strftime('%Y-%m-%d'),
            'total_rows': len(df),
            'columns': list(df.columns),
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
    
    def delete_stock_data(self, symbol: str) -> bool:
        """
        Delete stored data for a stock.
        
        Args:
            symbol: Stock symbol
            
        Returns:
            True if deletion was successful
        """
        file_path = self.stocks_dir / f"{symbol.replace('.', '_')}.parquet"
        
        if file_path.exists():
            try:
                file_path.unlink()
                logger.info(f"Deleted data file for {symbol}")
                return True
            except Exception as e:
                logger.error(f"Error deleting data for {symbol}: {e}")
                return False
        
        return False


class DataCollector:
    """
    Main class for collecting stock data from Yahoo Finance.
    
    This class orchestrates the data collection process:
    1. Loads the list of F&O stocks
    2. Fetches historical data using yfinance
    3. Validates data quality
    4. Stores data locally in parquet format
    5. Supports incremental updates
    
    Features:
    - Rate limiting to avoid API throttling
    - Retry logic for failed requests
    - Progress tracking and logging
    - Batch processing for large stock lists
    - Incremental updates (only fetch new data)
    
    Example:
        collector = DataCollector(data_dir='./data')
        
        # Fetch all F&O stocks
        collector.fetch_all_stocks(
            start_date='2020-01-01',
            end_date='2024-12-31'
        )
        
        # Fetch specific stocks
        collector.fetch_stocks(
            symbols=['RELIANCE.NS', 'TCS.NS'],
            start_date='2020-01-01'
        )
        
        # Update existing data (incremental)
        collector.update_all_stocks()
    """
    
    # Rate limiting settings
    MIN_REQUEST_INTERVAL = 0.5  # Seconds between requests
    MAX_RETRIES = 3
    RETRY_DELAY = 2  # Seconds to wait before retry
    
    def __init__(
        self,
        data_dir: str = './data',
        fno_list_file: Optional[str] = None
    ):
        """
        Initialize the data collector.
        
        Args:
            data_dir: Directory for storing data
            fno_list_file: Path to JSON file with F&O stock list
        """
        self.storage = DataStorage(data_dir)
        self.validator = DataValidator()
        self.fno_list_file = fno_list_file
        self._last_request_time = 0
        
        # Load F&O stock list
        self.fno_stocks = self._load_fno_list()
    
    def _load_fno_list(self) -> List[str]:
        """
        Load the list of F&O stocks from file or use default.
        
        The F&O list can be loaded from:
        1. User-provided JSON file
        2. Project's final_derivatives_list.json file (preferred)
        3. Project's fnolist.json file
        4. Default hardcoded list
        
        Returns:
            List of stock symbols with .NS suffix
        """
        import json
        
        # Try user-provided file first
        if self.fno_list_file and os.path.exists(self.fno_list_file):
            try:
                with open(self.fno_list_file, 'r') as f:
                    data = json.load(f)
                    # Handle both list and dict formats
                    if isinstance(data, dict) and 'UnderlyingList' in data:
                        stocks = [item['tradingsymbol'] for item in data['UnderlyingList']]
                    elif isinstance(data, list):
                        stocks = data
                    else:
                        stocks = []
                    logger.info(f"Loaded {len(stocks)} F&O stocks from {self.fno_list_file}")
                    return [self._add_ns_suffix(s) for s in stocks]
            except Exception as e:
                logger.warning(f"Error loading F&O list from {self.fno_list_file}: {e}")
        
        # Try multiple possible locations for the derivatives list
        possible_paths = [
            # When running from project root
            Path.cwd() / 'final_derivatives_list.json',
            # When running from ml_pipeline/examples
            Path.cwd().parent / 'final_derivatives_list.json',
            # Relative to this file
            Path(__file__).parent.parent.parent / 'final_derivatives_list.json',
            # Absolute path
            Path('/Users/rkumark/Ranjith/StockAnalysis/final_derivatives_list.json'),
        ]
        
        logger.info(f"Searching for F&O stock list in paths:")
        for project_derivatives in possible_paths:
            logger.info(f"  Checking: {project_derivatives} (exists: {project_derivatives.exists()})")
            if project_derivatives.exists():
                try:
                    with open(project_derivatives, 'r') as f:
                        data = json.load(f)
                        logger.info(f"  JSON keys found: {list(data.keys())}")
                        # Handle nested structure: {"data": {"UnderlyingList": [...]}}
                        if 'data' in data and 'UnderlyingList' in data.get('data', {}):
                            stocks = [item['tradingsymbol'] for item in data['data']['UnderlyingList']]
                            logger.info(f"Loaded {len(stocks)} F&O stocks from {project_derivatives}")
                            return [self._add_ns_suffix(s) for s in stocks]
                        elif 'UnderlyingList' in data:
                            stocks = [item['tradingsymbol'] for item in data['UnderlyingList']]
                            logger.info(f"Loaded {len(stocks)} F&O stocks from {project_derivatives}")
                            return [self._add_ns_suffix(s) for s in stocks]
                        else:
                            logger.warning(f"  'UnderlyingList' not found in {project_derivatives}")
                except Exception as e:
                    logger.warning(f"Error loading F&O list from {project_derivatives}: {e}")
        
        # Try fnolist.json as fallback
        fnolist_paths = [
            Path.cwd() / 'fnolist.json',
            Path.cwd().parent / 'fnolist.json',
            Path(__file__).parent.parent.parent / 'fnolist.json',
            Path('/Users/rkumark/Ranjith/StockAnalysis/fnolist.json'),
        ]
        
        for project_fnolist in fnolist_paths:
            if project_fnolist.exists():
                try:
                    with open(project_fnolist, 'r') as f:
                        data = json.load(f)
                        if 'UnderlyingList' in data:
                            stocks = [item['tradingsymbol'] for item in data['UnderlyingList']]
                            logger.info(f"Loaded {len(stocks)} F&O stocks from {project_fnolist}")
                            return [self._add_ns_suffix(s) for s in stocks]
                except Exception as e:
                    logger.warning(f"Error loading F&O list from {project_fnolist}: {e}")
        
        # Default list of major F&O stocks
        logger.info("Using default F&O stock list")
        return self._get_default_fno_list()
    
    def _add_ns_suffix(self, symbol: str) -> str:
        """Add .NS suffix to symbol if not present."""
        symbol = symbol.upper().strip()
        if not symbol.endswith('.NS'):
            return f"{symbol}.NS"
        return symbol
    
    def _get_default_fno_list(self) -> List[str]:
        """
        Get a default list of major F&O stocks.
        
        This is a fallback when no F&O list file is available.
        Includes the most liquid F&O stocks.
        """
        return [
            # Nifty 50 stocks
            'RELIANCE.NS', 'TCS.NS', 'HDFCBANK.NS', 'INFY.NS', 'ICICIBANK.NS',
            'HINDUNILVR.NS', 'ITC.NS', 'SBIN.NS', 'BHARTIARTL.NS', 'KOTAKBANK.NS',
            'LT.NS', 'AXISBANK.NS', 'ASIANPAINT.NS', 'MARUTI.NS', 'SUNPHARMA.NS',
            'TITAN.NS', 'BAJFINANCE.NS', 'DMART.NS', 'WIPRO.NS', 'HCLTECH.NS',
            'ULTRACEMCO.NS', 'NTPC.NS', 'POWERGRID.NS', 'TATAMOTORS.NS', 'TATASTEEL.NS',
            'ONGC.NS', 'JSWSTEEL.NS', 'HDFC.NS', 'BAJAJFINSV.NS', 'ADANIENT.NS',
            # Bank Nifty stocks
            'HDFCBANK.NS', 'ICICIBANK.NS', 'SBIN.NS', 'KOTAKBANK.NS', 'AXISBANK.NS',
            'BAJFINANCE.NS', 'INDUSINDBK.NS', 'AUBANK.NS', 'BANDHANBNK.NS', 'FEDERALBNK.NS',
            # Other liquid F&O stocks
            'TATAMOTORS.NS', 'TATASTEEL.NS', 'ADANIENT.NS', 'ADANIPORTS.NS',
            'APOLLOHOSP.NS', 'BPCL.NS', 'BRITANNIA.NS', 'CIPLA.NS', 'COALINDIA.NS',
            'DIVISLAB.NS', 'DRREDDY.NS', 'EICHERMOT.NS', 'GRASIM.NS', 'HDFCLIFE.NS',
            'HEROMOTOCO.NS', 'HINDALCO.NS', 'IOC.NS', 'IRCTC.NS', 'JINDALSTEL.NS',
            'M&M.NS', 'MFSL.NS', 'NAUKRI.NS', 'NESTLEIND.NS', 'PIIND.NS',
            'PNB.NS', 'SBILIFE.NS', 'SHREECEM.NS', 'SIEMENS.NS', 'TECHM.NS',
            'UPL.NS', 'VEDL.NS', 'ZEEL.NS', 'GAIL.NS', 'IDEA.NS',
        ]
    
    def _rate_limit(self):
        """
        Implement rate limiting to avoid API throttling.
        
        Ensures minimum time between requests to yfinance.
        """
        current_time = time.time()
        time_since_last = current_time - self._last_request_time
        
        if time_since_last < self.MIN_REQUEST_INTERVAL:
            sleep_time = self.MIN_REQUEST_INTERVAL - time_since_last
            time.sleep(sleep_time)
        
        self._last_request_time = time.time()
    
    def _fetch_with_retry(
        self,
        symbol: str,
        start_date: str,
        end_date: Optional[str] = None
    ) -> Optional[pd.DataFrame]:
        """
        Fetch stock data with retry logic.
        
        Args:
            symbol: Stock symbol
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD), defaults to today
            
        Returns:
            DataFrame with OHLCV data or None if failed
        """
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')
        
        for attempt in range(self.MAX_RETRIES):
            try:
                self._rate_limit()
                
                # Create ticker and fetch data
                ticker = yf.Ticker(symbol)
                df = ticker.history(
                    start=start_date,
                    end=end_date,
                    interval='1d',
                    auto_adjust=True,  # Adjust for splits and dividends
                    prepost=False,
                    repair=True  # Attempt to repair corrupted data
                )
                
                if df.empty:
                    logger.warning(f"No data returned for {symbol}")
                    return None
                
                # Standardize column names
                df.columns = [col.capitalize() for col in df.columns]
                
                # Ensure we have required columns
                required_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
                for col in required_cols:
                    if col not in df.columns:
                        logger.warning(f"Missing column {col} for {symbol}")
                        return None
                
                # Keep only OHLCV columns
                df = df[required_cols].copy()
                
                # Remove timezone info from index for consistency
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                
                logger.info(f"Fetched {len(df)} rows for {symbol}")
                return df
                
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed for {symbol}: {e}")
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY * (attempt + 1))
        
        logger.error(f"All retries failed for {symbol}")
        return None
    
    def fetch_stock(
        self,
        symbol: str,
        start_date: str,
        end_date: Optional[str] = None,
        validate: bool = True,
        save: bool = True
    ) -> Tuple[Optional[pd.DataFrame], Optional[DataQualityReport]]:
        """
        Fetch data for a single stock.
        
        Args:
            symbol: Stock symbol (e.g., 'RELIANCE.NS')
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD), defaults to today
            validate: Whether to validate data quality
            save: Whether to save data to storage
            
        Returns:
            Tuple of (DataFrame, DataQualityReport) or (None, None) if failed
        """
        logger.info(f"Fetching data for {symbol}")
        
        # Fetch data
        df = self._fetch_with_retry(symbol, start_date, end_date)
        
        if df is None:
            return None, None
        
        # Validate data
        report = None
        if validate:
            report = self.validator.validate(df, symbol)
            if not report.is_valid:
                logger.warning(f"Data quality issues for {symbol}: {report.issues}")
        
        # Save data
        if save:
            self.storage.save_stock_data(symbol, df)
        
        return df, report
    
    def fetch_stocks(
        self,
        symbols: List[str],
        start_date: str,
        end_date: Optional[str] = None,
        validate: bool = True,
        save: bool = True,
        show_progress: bool = True
    ) -> Dict[str, Tuple[Optional[pd.DataFrame], Optional[DataQualityReport]]]:
        """
        Fetch data for multiple stocks.
        
        Args:
            symbols: List of stock symbols
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD), defaults to today
            validate: Whether to validate data quality
            save: Whether to save data to storage
            show_progress: Whether to show progress
            
        Returns:
            Dictionary mapping symbol to (DataFrame, Report) tuple
        """
        results = {}
        total = len(symbols)
        
        for i, symbol in enumerate(symbols, 1):
            if show_progress:
                logger.info(f"Processing {i}/{total}: {symbol}")
            
            df, report = self.fetch_stock(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                validate=validate,
                save=save
            )
            
            results[symbol] = (df, report)
        
        # Summary
        successful = sum(1 for df, _ in results.values() if df is not None)
        valid = sum(1 for _, report in results.values() if report and report.is_valid)
        
        logger.info(f"Completed: {successful}/{total} fetched, {valid}/{total} valid")
        
        return results
    
    def fetch_all_stocks(
        self,
        start_date: str,
        end_date: Optional[str] = None,
        validate: bool = True,
        save: bool = True
    ) -> Dict[str, Tuple[Optional[pd.DataFrame], Optional[DataQualityReport]]]:
        """
        Fetch data for all F&O stocks.
        
        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD), defaults to today
            validate: Whether to validate data quality
            save: Whether to save data to storage
            
        Returns:
            Dictionary mapping symbol to (DataFrame, Report) tuple
        """
        logger.info(f"Fetching data for {len(self.fno_stocks)} F&O stocks")
        
        return self.fetch_stocks(
            symbols=self.fno_stocks,
            start_date=start_date,
            end_date=end_date,
            validate=validate,
            save=save
        )
    
    def update_stock(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        Update data for a single stock (incremental update).
        
        Only fetches new data since the last available date.
        
        Args:
            symbol: Stock symbol
            
        Returns:
            Updated DataFrame or None if failed
        """
        # Get existing data info
        info = self.storage.get_data_info(symbol)
        
        if info is None:
            # No existing data, fetch from default start date
            logger.info(f"No existing data for {symbol}, fetching from 2020-01-01")
            df, _ = self.fetch_stock(
                symbol=symbol,
                start_date='2020-01-01',
                validate=True,
                save=True
            )
            return df
        
        # Fetch new data since last date
        last_date = info['end_date']
        next_date = (datetime.strptime(last_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        
        logger.info(f"Updating {symbol} from {next_date}")
        
        new_df, _ = self.fetch_stock(
            symbol=symbol,
            start_date=next_date,
            validate=True,
            save=False  # Don't save yet, we'll merge
        )
        
        if new_df is None or new_df.empty:
            logger.info(f"No new data for {symbol}")
            return self.storage.load_stock_data(symbol)
        
        # Load existing and merge
        existing_df = self.storage.load_stock_data(symbol)
        merged_df = pd.concat([existing_df, new_df])
        merged_df = merged_df[~merged_df.index.duplicated(keep='last')]
        merged_df = merged_df.sort_index()
        
        # Save merged data
        self.storage.save_stock_data(symbol, merged_df)
        
        logger.info(f"Updated {symbol}: {len(existing_df)} -> {len(merged_df)} rows")
        
        return merged_df
    
    def update_all_stocks(self) -> Dict[str, Optional[pd.DataFrame]]:
        """
        Update data for all F&O stocks (incremental update).
        
        Returns:
            Dictionary mapping symbol to updated DataFrame
        """
        logger.info(f"Updating data for {len(self.fno_stocks)} F&O stocks")
        
        results = {}
        for i, symbol in enumerate(self.fno_stocks, 1):
            logger.info(f"Updating {i}/{len(self.fno_stocks)}: {symbol}")
            results[symbol] = self.update_stock(symbol)
        
        return results
    
    def load_stock_data(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Optional[pd.DataFrame]:
        """
        Load stored data for a stock.
        
        Args:
            symbol: Stock symbol
            start_date: Optional start date filter
            end_date: Optional end date filter
            
        Returns:
            DataFrame with OHLCV data
        """
        return self.storage.load_stock_data(symbol, start_date, end_date)
    
    def get_available_stocks(self) -> List[str]:
        """Get list of stocks with stored data."""
        return self.storage.get_available_stocks()
    
    def get_collection_summary(self) -> pd.DataFrame:
        """
        Get summary of all collected data.
        
        Returns:
            DataFrame with summary info for each stock
        """
        stocks = self.get_available_stocks()
        summaries = []
        
        for symbol in stocks:
            info = self.storage.get_data_info(symbol)
            if info:
                summaries.append(info)
        
        if not summaries:
            return pd.DataFrame()
        
        return pd.DataFrame(summaries)


# Convenience functions
def fetch_single_stock(
    symbol: str,
    start_date: str,
    end_date: Optional[str] = None,
    data_dir: str = './data'
) -> Optional[pd.DataFrame]:
    """
    Convenience function to fetch data for a single stock.
    
    Args:
        symbol: Stock symbol (e.g., 'RELIANCE.NS')
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        data_dir: Directory for data storage
        
    Returns:
        DataFrame with OHLCV data
    """
    collector = DataCollector(data_dir=data_dir)
    df, _ = collector.fetch_stock(symbol, start_date, end_date)
    return df


def fetch_index_data(
    symbols: List[str] = None,
    start_date: str = '2020-01-01',
    end_date: Optional[str] = None,
    data_dir: str = './data'
) -> Dict[str, pd.DataFrame]:
    """
    Fetch index data (Nifty, Bank Nifty, etc.) for market features.
    
    Args:
        symbols: List of index symbols (default: Nifty 50, Bank Nifty)
        start_date: Start date
        end_date: End date
        data_dir: Directory for data storage
        
    Returns:
        Dictionary mapping index symbol to DataFrame
    """
    if symbols is None:
        symbols = ['^NSEI', '^NSEBANK']  # Nifty 50 and Bank Nifty
    
    collector = DataCollector(data_dir=data_dir)
    results = {}
    
    for symbol in symbols:
        df, _ = collector.fetch_stock(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            validate=True,
            save=True
        )
        if df is not None:
            results[symbol] = df
    
    return results
