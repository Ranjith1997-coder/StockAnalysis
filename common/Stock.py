import sys
import os
sys.path.append(os.getcwd())

import pandas as pd
import common.constants as constant
from common.helperFunctions import percentageChange
import common.shared as shared
from common.logging_util import logger
from zerodha.tick_store import TickStore
import numpy as np
import datetime
import time
import requests


pd.options.mode.chained_assignment = None

class Stock:
    def __init__(self, stockName : str , stockSymbol : str, yfinanceSymbol = None, is_index = False):
        self.stockName = stockName
        self.stock_symbol = stockSymbol
        self.is_index = is_index
        if yfinanceSymbol is not None:
            self.stockSymbolYFinance = yfinanceSymbol
        else :
            self.stockSymbolYFinance = stockSymbol+".NS"
        self.prevDayOHLCV = None
        self.last_price_update = None
        self.ltp = None
        self.ltp_change_perc = None
        # Annualised HV (%) from daily closes, computed once at morning bias.
        # Persists through the intraday loop so IV vs HV is not contaminated
        # by intraday gap bars when priceData is overwritten with 5m bars.
        self.daily_hv: float | None = None
        self._priceData = pd.DataFrame()
        self.last_trend_timestamp = None
        self.derivativesData = { 
                        "futuresData": {"currExpiry" : None, "nextExpiry" : None} , 
                        "optionsData": {"currExpiry" : None, "nextExpiry" : None} 
        }
        self._tick_store = TickStore()
        self.zerodha_ctx = {
            "last_notification_time": None,

            "option_chain": {"current": None, "next": None},

            "futures_mdata": {"current": None, "next": None}, # contains the instrument token and expiry for futures
            "futures_data" : {"current":pd.DataFrame(), "next":pd.DataFrame()} # contains the futures OHLC and OI data for futures
        }
        
        self.sensibull_ctx = {
            "last_fetch_time": None,
            "current": {  # Latest snapshot
                "underlying_info": None,
                "stats": None,
                "per_expiry_map": None,
                "nse_stats": None
            },
            "historical_data": pd.DataFrame(),  # Time-series data
            "oi_chain": None,          # Latest per-strike OI chain snapshot
            "oi_chain_history": [],    # List of periodic OI chain snapshots (max 15 for intraday)
            "iv_chart_history": pd.DataFrame(),  # Daily IV closes from iv_chart API (fetched once)
            "oi_history": pd.DataFrame(),        # Daily OI history from compute_intraday 1D (fetched once)
        }

        self.analysis = {"Timestamp": None,
                        "BULLISH":  {},
                        "BEARISH":  {},
                        "NEUTRAL":  {},
                        "NoOfTrends": 0,
                        # Set by OptionSellerCompositeAnalyser to force-pass the score gate.
                        # None = use normal scoring; NotificationPriority value = bypass.
                        "PRIORITY_OVERRIDE": None,
                        }

    def set_prev_day_ohlcv(self, open, close, high, low, volume):
        self.prevDayOHLCV = {"OPEN":open, "HIGH":high, "LOW":low, "CLOSE":close, "VOLUME":volume}
    
    def update_latest_data(self):
        valid_closes = self.priceData['Close'].dropna()
        if valid_closes.empty:
            logger.warning(f"[Stock] No valid Close prices in priceData for {self.stock_symbol} — ltp not updated")
            return
        current_close = valid_closes.iloc[-1]
        if self.prevDayOHLCV is None:
            logger.warning(f"[Stock] prevDayOHLCV not set for {self.stock_symbol} — ltp_change_perc will be 0")
            self.ltp = current_close
            return
        previous_close = self.prevDayOHLCV['CLOSE']
        change_percent = percentageChange(current_close, previous_close)
        self.ltp = current_close
        self.ltp_change_perc = change_percent


    def get_futures_data_for_stock(self, mode="positional", is_next_expiry_required=False):
        """Deprecated shim — use zerodha.futures_fetcher.FuturesFetcher.fetch() instead."""
        import warnings
        warnings.warn(
            "Stock.get_futures_data_for_stock() is deprecated. "
            "Use zerodha.futures_fetcher.FuturesFetcher.fetch(stock, ...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        from zerodha.futures_fetcher import FuturesFetcher
        return FuturesFetcher(shared.app_ctx.zd_kc).fetch(
            self, mode=mode, is_next_expiry_required=is_next_expiry_required
        )

    def fetch_sensibull_data(self, mode="positional"):
        """Deprecated shim — use fno.sensibull_fetcher.SensibullFetcher.fetch_data() instead."""
        import warnings
        warnings.warn(
            "Stock.fetch_sensibull_data() is deprecated. "
            "Use fno.sensibull_fetcher.SensibullFetcher.fetch_data(stock, ...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        from fno.sensibull_fetcher import SensibullFetcher
        return SensibullFetcher().fetch_data(self, mode=mode)

    def fetch_sensibull_oi_chain(self, mode="positional"):
        """Deprecated shim — use fno.sensibull_fetcher.SensibullFetcher.fetch_oi_chain() instead."""
        import warnings
        warnings.warn(
            "Stock.fetch_sensibull_oi_chain() is deprecated. "
            "Use fno.sensibull_fetcher.SensibullFetcher.fetch_oi_chain(stock, ...) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        from fno.sensibull_fetcher import SensibullFetcher
        return SensibullFetcher().fetch_oi_chain(self, mode=mode)

    # ------------------------------------------------------------------
    # Live tick delegation — backed by TickStore
    # ------------------------------------------------------------------

    @property
    def zerodha_data(self) -> dict:
        """Thread-safe snapshot of the current tick data."""
        return self._tick_store.zerodha_data

    def update_zerodha_data(self, ticker_data: dict) -> None:
        """Thread-safe update of Zerodha tick data."""
        self._tick_store.update_zerodha_data(ticker_data)

    def update_option_tick(self, strike: float, option_type: str, tick: dict, merge: bool = False) -> None:
        """Update live option data from a WebSocket tick."""
        self._tick_store.update_option_tick(strike, option_type, tick, merge=merge)

    def update_futures_tick(self, expiry_key: str, tick: dict) -> None:
        """Update live futures data from a WebSocket tick."""
        self._tick_store.update_futures_tick(expiry_key, tick)

    def recompute_options_aggregate(self, spot_price: float = None) -> None:
        """Recompute aggregate metrics from options_live data."""
        self._tick_store.recompute_options_aggregate(spot_price)

    @property
    def options_live(self) -> dict:
        return self._tick_store.options_live

    @property
    def options_aggregate(self) -> dict:
        return self._tick_store.options_aggregate

    @property
    def futures_live(self) -> dict:
        return self._tick_store.futures_live

    @property
    def priceData(self):
        """Getter for priceData"""
        return self._priceData

    @priceData.setter
    def priceData(self, value):
        """Setter for priceData"""
        if not isinstance(value, pd.DataFrame):
            raise ValueError("priceData must be a pandas DataFrame")
        self._priceData = value

    def set_analysis(self, trend : str, analysis_type: str, data):
        if trend in ['BULLISH', 'BEARISH', 'NEUTRAL']:
            existing = self.analysis[trend].get(analysis_type)
            if existing is None:
                self.analysis[trend][analysis_type] = data
                self.analysis['NoOfTrends'] += 1
            else:
                if not isinstance(existing, list):
                    self.analysis[trend][analysis_type] = [existing]
                self.analysis[trend][analysis_type].append(data)
            if analysis_type == "52-week-high":
                shared.ticker_52_week_high_list.append(self)
            elif analysis_type == "52-week-low":
                shared.ticker_52_week_low_list.append(self)
            
    
    def reset_analysis(self):
        # Remove this stock from the 52-week tracking lists to prevent cross-cycle duplicates
        if self in shared.ticker_52_week_high_list:
            shared.ticker_52_week_high_list.remove(self)
        if self in shared.ticker_52_week_low_list:
            shared.ticker_52_week_low_list.remove(self)
        self.analysis = {"Timestamp": None,
                            "BULLISH":  {},
                            "BEARISH":  {},
                            "NEUTRAL":  {},
                            "NoOfTrends": 0,
                            "PRIORITY_OVERRIDE": None,
                        }
    
    def reset_price_data(self):
        self.priceData = self.priceData[0:0]

    def check_52_week_status(self):
        close_df = self.priceData[['Close']]
        close_df['rolling_max_prev'] = close_df['Close'].shift(1).rolling(window=252).max()
        close_df['rolling_min_prev'] = close_df['Close'].shift(1).rolling(window=252).min()

        is_52_week_high = (close_df['Close'].iloc[-1] > close_df['rolling_max_prev'].iloc[-1])
        is_52_week_low = (close_df['Close'].iloc[-1] < close_df['rolling_min_prev'].iloc[-1])

        if (is_52_week_high.item()):
            return 1
        elif (is_52_week_low.item()):
            return -1
        else:
            return 0
    @property
    def current_equity_data(self):
        if self.priceData is None:
            return None
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            if len(self.priceData) < 2:
                return None
            return self.priceData.iloc[-2]
        else:
            if len(self.priceData) < 1:
                return None
            return self.priceData.iloc[-1]

    @property
    def previous_equity_data(self):
        if self.priceData is None:
            return None
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            if len(self.priceData) < 3:
                return None
            return self.priceData.iloc[-3]
        else:
            if len(self.priceData) < 2:
                return None
            return self.priceData.iloc[-2]
    
    @property
    def previous_previous_equity_data(self):
        if self.priceData is None:
            return None
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            if len(self.priceData) < 4:
                return None
            return self.priceData.iloc[-4]
        else:
            if len(self.priceData) < 3:
                return None
            return self.priceData.iloc[-3]
    

    def removeStockData(self):
        self.priceData = pd.DataFrame()
    
    def is_price_data_empty(self):
        return self.priceData.empty
