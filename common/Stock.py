import sys
import os
sys.path.append(os.getcwd())

import pandas as pd
import common.constants as constant
from common.helperFunctions import percentageChange
import pandas as pd
import threading
import  common.shared as shared
from common.logging_util import logger
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
        self._priceData = pd.DataFrame()
        self.last_trend_timestamp = None
        self.derivativesData = { 
                        "futuresData": {"currExpiry" : None, "nextExpiry" : None} , 
                        "optionsData": {"currExpiry" : None, "nextExpiry" : None} 
        }
        self._zerodha_data = {
            "volume_traded": 0,
            "last_price": 0,
            "open": 0,
            "high" : 0,
            "close": 0,
            "low": 0,
            "change": 0,
            "average_traded_price": 0,
            "total_buy_quantity": 0,
            "total_sell_quantity": 0
        }
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
            "oi_chain_history": []     # List of periodic OI chain snapshots (max 15 for intraday)
        }

        # Live options tick data from WebSocket (keyed by strike -> CE/PE)
        # { 24000: { "CE": {ltp, oi, prev_oi, volume, buy_qty, sell_qty, ...}, "PE": {...} } }
        self.options_live: dict = {}

        # Aggregated options metrics (recomputed periodically from options_live)
        self.options_aggregate = {
            "total_ce_oi": 0,
            "total_pe_oi": 0,
            "live_pcr": 0.0,
            "atm_strike": None,
            "atm_straddle_premium": 0.0,
            "atm_iv_ce": 0.0,
            "atm_iv_pe": 0.0,
            "iv_skew": 0.0,
            "max_oi_ce_strike": None,
            "max_oi_pe_strike": None,
            "net_ce_oi_change": 0,
            "net_pe_oi_change": 0,
            "last_updated": 0.0,
        }

        # Live futures tick data from WebSocket
        # { "current": {ltp, oi, volume, buy_qty, sell_qty, ...}, "next": {...} }
        self.futures_live: dict = {}

        self._zerodha_lock = threading.Lock()
        self.analysis = {"Timestamp" : None,
                        "BULLISH":{},
                        "BEARISH":{},
                        "NEUTRAL":{},
                        "NoOfTrends": 0,
                        }

    def set_prev_day_ohlcv(self, open, close, high, low, volume):
        self.prevDayOHLCV = {"OPEN":open, "HIGH":high, "LOW":low, "CLOSE":close, "VOLUME":volume}
    
    def update_latest_data(self):
        valid_closes = self.priceData['Close'].dropna()
        if valid_closes.empty:
            return
        current_close = valid_closes.iloc[-1]
        # previous_close = stock.priceData['Close'].iloc[-2]
        previous_close = self.prevDayOHLCV['CLOSE']
        change_percent = percentageChange(current_close, previous_close)
        self.ltp = current_close
        self.ltp_change_perc = change_percent


    def get_futures_data_for_stock(self, mode="positional", is_next_expiry_required=False):
        """
        Fetch futures OHLC and OI data for current and next expiry.
        For positional: gets daily data for the last 5 business days.
        For intraday: appends new 5min data for today.
        Updates zerodha_ctx["futures_data"]["current"] and zerodha_ctx["futures_data"]["next"].
        """
        zerodha_ctx = self.zerodha_ctx
        kite_connect = shared.app_ctx.zd_kc
        futures_mdata_current = zerodha_ctx["futures_mdata"]["current"]
        futures_mdata_next = zerodha_ctx["futures_mdata"]["next"]

        try:
            if mode == "positional":
                interval = "day"
                end_date = datetime.datetime.now()
                business_days = pd.bdate_range(end=end_date, periods=5).to_pydatetime()
                start_date = business_days[0]
                # Fetch all data in one call
                def fetch_futures(token, start_date, end_date, interval):
                    for attempt in range(3):
                        try:
                            hist_data = kite_connect.historical_data(
                                instrument_token=token,
                                from_date=start_date.strftime("%Y-%m-%d"),
                                to_date=end_date.strftime("%Y-%m-%d"),
                                interval=interval,
                                oi=True
                            )
                            return hist_data
                        except Exception as e:
                            if "Too many requests" in str(e):
                                logger.warning(f"Rate limit hit for {self.stock_symbol} sleeping for 1 seconds...")
                                time.sleep(1)
                            else:
                                logger.error(f"Error fetching futures data for token {token}: {e}")
                                raise
                    logger.error(f"Failed to fetch futures data for token {token} after 3 attempts")
                    raise Exception(f"Too many requests for futures token {token}")

                # Current expiry
                futures_data_current = pd.DataFrame()
                if futures_mdata_current is not None:
                    token = futures_mdata_current['instrument_token'].values[0]
                    hist_data = fetch_futures(token, start_date, end_date, interval)
                    if hist_data:
                        rows = []
                        for candle in hist_data:
                            dt = pd.Timestamp(candle['date']).tz_convert('Asia/Kolkata').replace(hour=5, minute=30, second=0)
                            row = {
                                "date": dt,
                                "open": candle['open'],
                                "high": candle['high'],
                                "low": candle['low'],
                                "close": candle['close'],
                                "volume": candle['volume'],
                                "oi": candle.get('oi', None),
                                "underlying_price": candle['close']
                            }
                            rows.append(row)
                        futures_data_current = pd.DataFrame(rows).set_index("date")
                        logger.info(f"Futures data for {self.stock_symbol} (current expiry) fetched for {len(futures_data_current)} rows.")

                # Next expiry
                futures_data_next = pd.DataFrame()
                if is_next_expiry_required and futures_mdata_next is not None:
                    token_next = futures_mdata_next['instrument_token'].values[0]
                    hist_data_next = fetch_futures(token_next, start_date, end_date, interval)
                    if hist_data_next:
                        rows_next = []
                        for candle in hist_data_next:
                            dt = pd.Timestamp(candle['date']).tz_convert('Asia/Kolkata').replace(hour=5, minute=30, second=0)
                            row_next = {
                                "date": dt,
                                "open": candle['open'],
                                "high": candle['high'],
                                "low": candle['low'],
                                "close": candle['close'],
                                "volume": candle['volume'],
                                "oi": candle.get('oi', None),
                                "underlying_price": candle['close']
                            }
                            rows_next.append(row_next)
                        futures_data_next = pd.DataFrame(rows_next).set_index("date")
                        logger.info(f"Futures data for {self.stock_symbol} (next expiry) fetched for {len(futures_data_next)} rows.")

                zerodha_ctx["futures_data"]["current"] = futures_data_current
                zerodha_ctx["futures_data"]["next"] = futures_data_next
                return futures_data_current, futures_data_next

            elif mode == "intraday":
                interval = "5minute"
                today = datetime.datetime.now()
                start_date = today
                end_date = today
                today_str = datetime.datetime.now().strftime("%Y-%m-%d")
                intraday_dates = [dt for dt in self.priceData.index if dt.strftime("%Y-%m-%d") == today_str]
                if len(intraday_dates) < 2:
                    logger.info(f"Not enough intraday data for {self.stock_symbol}")
                    raise Exception(f"Not enough intraday data for {self.stock_symbol}")

                dt = intraday_dates[-2]
                underlying_price = self.priceData.loc[dt, "Close"]
                # Current expiry
                futures_data_current = zerodha_ctx["futures_data"]["current"]
                if futures_mdata_current is not None and not futures_mdata_current.empty:
                    token = futures_mdata_current['instrument_token'].values[0]
                    for attempt in range(3):
                        try:
                            hist_data = kite_connect.historical_data(
                                instrument_token=token,
                                from_date=dt.strftime("%Y-%m-%d"),
                                to_date=dt.strftime("%Y-%m-%d"),
                                interval=interval,
                                oi=True
                            )
                            break
                        except Exception as e:
                            if "Too many requests" in str(e):
                                logger.warning(f"Rate limit hit for {self.stock_symbol}, sleeping for 1 seconds...")
                                time.sleep(1)
                            else:
                                logger.error(f"Error fetching intraday futures data for token {token} at {dt}: {e}")
                                raise
                    else:
                        logger.error(f"Failed to fetch intraday futures data for token {token} at {dt} after 3 attempts")
                        raise Exception(f"Too many requests for futures token {token} at {dt}")
                    if hist_data:
                        candle = next((c for c in hist_data if pd.Timestamp(c['date']).tz_convert('Asia/Kolkata') == dt), None)
                        if candle:
                            row = {
                                "date": dt,
                                "open": candle['open'],
                                "high": candle['high'],
                                "low": candle['low'],
                                "close": candle['close'],
                                "volume": candle['volume'],
                                "oi": candle.get('oi', None),
                                "underlying_price": underlying_price
                            }
                            # Append new row - preserve datetime index properly
                            new_row_df = pd.DataFrame([row]).set_index("date")
                            if not futures_data_current.empty:
                                futures_data_current = pd.concat([futures_data_current, new_row_df])
                            else:
                                futures_data_current = new_row_df
                            logger.info(f"Futures data for {self.stock_symbol} (current expiry) at {dt}: {row}")

                # Next expiry
                futures_data_next = zerodha_ctx["futures_data"]["next"]
                if is_next_expiry_required and futures_mdata_next is not None:
                    token_next = futures_mdata_next['instrument_token'].values[0]
                    for dt in intraday_dates:
                        if dt in futures_data_next.index if not futures_data_next.empty else []:
                            continue  # Skip if already present
                        for attempt in range(3):
                            try:
                                hist_data_next = kite_connect.historical_data(
                                    instrument_token=token_next,
                                    from_date=dt.strftime("%Y-%m-%d %H:%M:%S"),
                                    to_date=dt.strftime("%Y-%m-%d %H:%M:%S"),
                                    interval=interval,
                                    oi=True
                                )
                                break
                            except Exception as e:
                                if "Too many requests" in str(e):
                                    logger.warning(f"Rate limit hit {self.stock_symbol} , sleeping for 1 seconds...")
                                    time.sleep(1)
                                else:
                                    logger.error(f"Error fetching intraday futures data for token {token_next} at {dt}: {e}")
                                    raise
                        else:
                            logger.error(f"Failed to fetch intraday futures data for token {token_next} at {dt} after 3 attempts")
                            raise Exception(f"Too many requests for futures token {token_next} at {dt}")
                        if hist_data_next:
                            candle_next = next((c for c in hist_data_next if pd.Timestamp(c['date']).tz_convert('Asia/Kolkata') == dt), None)
                            if candle_next:
                                row_next = {
                                    "date": dt,
                                    "open": candle_next['open'],
                                    "high": candle_next['high'],
                                    "low": candle_next['low'],
                                    "close": candle_next['close'],
                                    "volume": candle_next['volume'],
                                    "oi": candle_next.get('oi', None),
                                    "underlying_price": candle_next['close']
                                }
                                # Append new row - preserve datetime index properly
                                new_row_df = pd.DataFrame([row_next]).set_index("date")
                                if not futures_data_next.empty:
                                    futures_data_next = pd.concat([futures_data_next, new_row_df])
                                else:
                                    futures_data_next = new_row_df
                                logger.info(f"Futures data for {self.stock_symbol} (next expiry) at {dt}: {row_next}")

                zerodha_ctx["futures_data"]["current"] = futures_data_current
                zerodha_ctx["futures_data"]["next"] = futures_data_next
                
                return futures_data_current, futures_data_next

            else:
                raise ValueError("mode must be 'positional' or 'intraday'")

        except Exception as e:
            logger.error(f"get_complete_futures_data failed for {self.stock_symbol}: {e}")
            raise

    def fetch_sensibull_data(self, mode="positional"):
        """
        Fetches stock insights data from Sensibull API and stores it in sensibull_ctx.
        Stores periodic snapshots in a DataFrame for historical analysis.
        
        Args:
            mode (str): "positional" for daily data or "intraday" for periodic updates
        
        Returns:
            pd.DataFrame: The historical data DataFrame, or None if the request fails.
        """
        try:
            from urllib.parse import quote
            encoded_symbol = quote(self.stock_symbol, safe='')
            url = f"https://oxide.sensibull.com/v1/compute/cache/insights/stock_info?tradingsymbol={encoded_symbol}"
            logger.info(f"Fetching Sensibull data for {self.stock_symbol} from {url}")
            
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            
            if data.get("success") and "payload" in data:
                payload = data["payload"]
                timestamp = datetime.datetime.now()
                
                # Store current snapshot
                self.sensibull_ctx["last_fetch_time"] = timestamp
                self.sensibull_ctx["current"]["underlying_info"] = payload.get("underlying_info")
                self.sensibull_ctx["current"]["stats"] = payload.get("stats")
                self.sensibull_ctx["current"]["per_expiry_map"] = payload.get("stats", {}).get("per_expiry_map")
                self.sensibull_ctx["current"]["nse_stats"] = payload.get("nse_stats")
                
                # Extract key metrics for historical storage
                stats = payload.get("stats", {})
                base_stats = stats.get("underlying_base_stats", {})
                per_expiry_map = stats.get("per_expiry_map", {})
                
                # Build historical row with flattened per-expiry data
                historical_row = {
                    "timestamp": timestamp,
                    "volume_spike": base_stats.get("volume_spike"),
                    "volume_spike_type": base_stats.get("volume_spike_type"),
                    "future_oi_change": base_stats.get("future_oi_change"),
                    "oi_change_type": base_stats.get("oi_change_type"),
                    "total_pcr": base_stats.get("total_pcr"),
                }
                
                # Add per-expiry metrics (flatten for DataFrame)
                for expiry, expiry_data in per_expiry_map.items():
                    # Use expiry as suffix for column names
                    expiry_suffix = expiry.replace("-", "")
                    historical_row[f"future_price_{expiry_suffix}"] = expiry_data.get("future_price")
                    historical_row[f"future_change_pct_{expiry_suffix}"] = expiry_data.get("future_change_percent")
                    historical_row[f"atm_strike_{expiry_suffix}"] = expiry_data.get("atm_strike")
                    historical_row[f"atm_iv_{expiry_suffix}"] = expiry_data.get("atm_iv")
                    historical_row[f"atm_iv_change_{expiry_suffix}"] = expiry_data.get("atm_iv_change")
                    historical_row[f"atm_iv_percentile_{expiry_suffix}"] = expiry_data.get("atm_iv_percentile")
                    historical_row[f"atm_ivp_type_{expiry_suffix}"] = expiry_data.get("atm_ivp_type")
                    historical_row[f"max_pain_{expiry_suffix}"] = expiry_data.get("max_pain_strike")
                    historical_row[f"max_pain_type_{expiry_suffix}"] = expiry_data.get("max_pain_type")
                    historical_row[f"pcr_{expiry_suffix}"] = expiry_data.get("pcr")
                    historical_row[f"pcr_type_{expiry_suffix}"] = expiry_data.get("pcr_type")
                    historical_row[f"lot_size_{expiry_suffix}"] = expiry_data.get("lot_size")
                
                # Create new row DataFrame
                new_row_df = pd.DataFrame([historical_row])
                
                # Handle historical data storage based on mode
                existing_historical = self.sensibull_ctx["historical_data"]
                
                if mode == "positional":
                    # For positional, keep last 30 days
                    if not existing_historical.empty:
                        # Append new row
                        updated_df = pd.concat([existing_historical, new_row_df], ignore_index=True)
                        # Keep only last 30 rows
                        updated_df = updated_df.tail(30)
                    else:
                        updated_df = new_row_df
                elif mode == "intraday":
                    # For intraday, keep data for last 5 days
                    if not existing_historical.empty:
                        # Append new row
                        updated_df = pd.concat([existing_historical, new_row_df], ignore_index=True)
                        # Filter to keep only last 5 days
                        five_days_ago = timestamp - datetime.timedelta(days=5)
                        updated_df = updated_df[updated_df["timestamp"] >= five_days_ago]
                    else:
                        updated_df = new_row_df
                else:
                    raise ValueError("mode must be 'positional' or 'intraday'")
                
                self.sensibull_ctx["historical_data"] = updated_df
                
                logger.info(f"Sensibull data successfully fetched and stored for {self.stock_symbol}. Historical rows: {len(updated_df)}")
                return updated_df
            else:
                logger.warning(f"Sensibull API returned unsuccessful response for {self.stock_symbol}")
                return None
                
        except requests.exceptions.Timeout:
            logger.error(f"Timeout while fetching Sensibull data for {self.stock_symbol}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error fetching Sensibull data for {self.stock_symbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching Sensibull data for {self.stock_symbol}: {e}")
            return None

    def _build_oi_chain_expiry_body(self):
        """
        Build the expiries dict for the OI chain POST request body.
        Uses expiry data from the already-fetched Sensibull insights (per_expiry_map).
        Enables the nearest expiry and disables the rest.
        
        Returns:
            dict: Expiries dict for the POST body, or None if no expiry data available.
        """
        per_expiry_map = self.sensibull_ctx.get("current", {}).get("per_expiry_map")
        if not per_expiry_map:
            logger.debug(f"No per_expiry_map available for {self.stock_symbol}, cannot build OI chain request")
            return None
        
        sorted_expiries = sorted(per_expiry_map.keys())
        if not sorted_expiries:
            return None
        
        expiries_body = {}
        nearest_expiry = sorted_expiries[0]
        for exp in sorted_expiries:
            expiries_body[exp] = {
                "is_weekly": False,
                "is_enabled": exp == nearest_expiry
            }
        
        return expiries_body

    def fetch_sensibull_oi_chain(self, mode="positional"):
        """
        Fetches per-strike OI chain data from Sensibull OI chart endpoint (POST).
        
        For positional mode:
            - Fetches a single snapshot and stores it for analysis.
        
        For intraday mode:
            - Called every ~5 minutes by the monitor loop.
            - Stores the latest snapshot and appends to oi_chain_history (max 15 entries).
            - History is used by OIChainAnalyser for trend detection.
        
        API: POST https://oxide.sensibull.com/v1/compute/1/oi_graphs/oi_chart
        Body: {underlying, expiries, atm_strike_selection, auto_update, show_prev_oi, ...}
        
        Requires: fetch_sensibull_data() to be called first (for expiry info).
        
        Args:
            mode (str): "positional" for single snapshot or "intraday" for periodic updates.
        
        Returns:
            dict: The OI chain snapshot stored in sensibull_ctx["oi_chain"], or None on failure.
        """
        try:
            url = "https://oxide.sensibull.com/v1/compute/1/oi_graphs/oi_chart"
            
            # Build expiry body from insights data
            expiries_body = self._build_oi_chain_expiry_body()
            if not expiries_body:
                logger.warning(f"Cannot fetch OI chain for {self.stock_symbol}: no expiry data available. "
                              "Ensure fetch_sensibull_data() is called first.")
                return None
            
            # Build POST body
            request_body = {
                "underlying": self.stock_symbol,
                "expiries": expiries_body,
                "atm_strike_selection": "twenty",
                "input_min_strike": None,
                "input_max_strike": None,
                "auto_update": "full_day",
                "show_prev_oi": True
            }
            
            logger.info(f"Fetching Sensibull OI chain for {self.stock_symbol} ({mode}) from {url}")
            
            response = requests.post(url, json=request_body, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            
            if not data.get("success") or "payload" not in data:
                logger.warning(f"Sensibull OI chain API returned unsuccessful response for {self.stock_symbol}")
                return None
            
            payload = data["payload"]
            
            # Find the enabled expiry from response
            expiries_resp = payload.get("input", {}).get("expiries", {})
            enabled_expiry = None
            for exp_date, exp_info in expiries_resp.items():
                if exp_info.get("is_enabled", False):
                    enabled_expiry = exp_date
                    break
            
            # Build OI chain snapshot
            timestamp = datetime.datetime.now()
            oi_chain = {
                "timestamp": timestamp,
                "date": payload.get("input", {}).get("date", datetime.datetime.now().strftime("%Y-%m-%d")),
                "expiry": enabled_expiry,
                "underlying_symbol": self.stock_symbol,
                
                # Price data
                "prev_ltp": payload.get("prev_ltp"),
                "current_ltp": payload.get("current_ltp") or payload.get("date_ltp"),
                "date_ltp": payload.get("date_ltp"),
                "atm_strike": payload.get("atm_strike"),
                
                # Aggregated OI
                "total_call_oi": payload.get("total_call_oi", 0),
                "total_put_oi": payload.get("total_put_oi", 0),
                "total_call_oi_change": payload.get("total_call_oi_change", 0),
                "total_put_oi_change": payload.get("total_put_oi_change", 0),
                "pcr": payload.get("pcr"),
                
                # Per-strike data
                "per_strike_data": payload.get("per_strike_data", {}),
                "strike_list": payload.get("strike_list", []),
                "min_strike": payload.get("min_strike"),
                "max_strike": payload.get("max_strike"),
                
                # Metadata
                "underlying_token": payload.get("underlying_token"),
            }
            
            # Store latest snapshot
            self.sensibull_ctx["oi_chain"] = oi_chain
            
            # Handle history based on mode
            if mode == "intraday":
                # Append to history, keep last 15 snapshots
                history = self.sensibull_ctx["oi_chain_history"]
                history.append(oi_chain)
                if len(history) > 15:
                    self.sensibull_ctx["oi_chain_history"] = history[-15:]
                
                logger.info(f"Sensibull OI chain fetched for {self.stock_symbol} (intraday): "
                           f"PCR={oi_chain['pcr']}, LTP={oi_chain['current_ltp']}, "
                           f"Strikes={len(oi_chain['per_strike_data'])}, "
                           f"History={len(self.sensibull_ctx['oi_chain_history'])}/15, "
                           f"Call OI={oi_chain['total_call_oi']:,}, Put OI={oi_chain['total_put_oi']:,}")
            else:
                # Positional: single snapshot, clear history
                self.sensibull_ctx["oi_chain_history"] = [oi_chain]
                
                logger.info(f"Sensibull OI chain fetched for {self.stock_symbol} (positional): "
                           f"PCR={oi_chain['pcr']}, LTP={oi_chain['current_ltp']}, "
                           f"Strikes={len(oi_chain['per_strike_data'])}, "
                           f"Call OI={oi_chain['total_call_oi']:,}, Put OI={oi_chain['total_put_oi']:,}")
            
            return oi_chain
            
        except requests.exceptions.Timeout:
            logger.error(f"Timeout while fetching Sensibull OI chain for {self.stock_symbol}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error fetching Sensibull OI chain for {self.stock_symbol}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching Sensibull OI chain for {self.stock_symbol}: {e}")
            return None

    @property
    def zerodha_data(self):
        """Thread-safe getter for zerodha_data"""
        with self._zerodha_lock:
            return self._zerodha_data.copy()  # Return a copy to prevent external modifications
    
    def update_zerodha_data(self, ticker_data):
        """
        Thread-safe update of Zerodha tick data.
        Handles both equity ticks (184-byte full mode with volume/depth)
        and index ticks (28/32-byte quote/full mode with only OHLC).
        """
        with self._zerodha_lock:
            self._zerodha_data["last_price"] = ticker_data.get("last_price", self._zerodha_data["last_price"])
            self._zerodha_data["change"] = ticker_data.get("change", self._zerodha_data["change"])

            ohlc = ticker_data.get("ohlc")
            if ohlc:
                self._zerodha_data["open"] = ohlc.get("open", self._zerodha_data["open"])
                self._zerodha_data["high"] = ohlc.get("high", self._zerodha_data["high"])
                self._zerodha_data["close"] = ohlc.get("close", self._zerodha_data["close"])
                self._zerodha_data["low"] = ohlc.get("low", self._zerodha_data["low"])

            # These fields are only present in equity/option ticks (44 or 184 bytes), not index ticks (28/32 bytes)
            if "volume_traded" in ticker_data:
                self._zerodha_data["volume_traded"] = ticker_data["volume_traded"]
            if "average_traded_price" in ticker_data:
                self._zerodha_data["average_traded_price"] = ticker_data["average_traded_price"]
            if "total_buy_quantity" in ticker_data:
                self._zerodha_data["total_buy_quantity"] = ticker_data["total_buy_quantity"]
            if "total_sell_quantity" in ticker_data:
                self._zerodha_data["total_sell_quantity"] = ticker_data["total_sell_quantity"]

    def update_option_tick(self, strike: float, option_type: str, tick: dict):
        """Update live option data from a WebSocket tick."""
        with self._zerodha_lock:
            if strike not in self.options_live:
                self.options_live[strike] = {}

            entry = self.options_live[strike].get(option_type, {})
            entry["prev_oi"] = entry.get("oi", 0)
            entry["ltp"] = tick.get("last_price", 0)
            entry["oi"] = tick.get("oi", 0)
            entry["volume"] = tick.get("volume_traded", 0)
            entry["buy_qty"] = tick.get("total_buy_quantity", 0)
            entry["sell_qty"] = tick.get("total_sell_quantity", 0)
            entry["timestamp"] = tick.get("exchange_timestamp")

            if "ohlc" in tick:
                entry["open"] = tick["ohlc"].get("open", 0)
                entry["high"] = tick["ohlc"].get("high", 0)
                entry["low"] = tick["ohlc"].get("low", 0)
                entry["close"] = tick["ohlc"].get("close", 0)

            if "depth" in tick:
                entry["depth"] = tick["depth"]

            self.options_live[strike][option_type] = entry

    def update_futures_tick(self, expiry_key: str, tick: dict):
        """Update live futures data from a WebSocket tick."""
        with self._zerodha_lock:
            entry = self.futures_live.get(expiry_key, {})
            entry["prev_oi"] = entry.get("oi", 0)
            entry["ltp"] = tick.get("last_price", 0)
            entry["oi"] = tick.get("oi", 0)
            entry["volume"] = tick.get("volume_traded", 0)
            entry["buy_qty"] = tick.get("total_buy_quantity", 0)
            entry["sell_qty"] = tick.get("total_sell_quantity", 0)
            entry["change"] = tick.get("change", 0)
            entry["timestamp"] = tick.get("exchange_timestamp")

            if "ohlc" in tick:
                entry["open"] = tick["ohlc"].get("open", 0)
                entry["high"] = tick["ohlc"].get("high", 0)
                entry["low"] = tick["ohlc"].get("low", 0)
                entry["close"] = tick["ohlc"].get("close", 0)

            self.futures_live[expiry_key] = entry

    def recompute_options_aggregate(self, spot_price: float = None):
        """Recompute aggregate metrics from options_live data."""
        with self._zerodha_lock:
            if not self.options_live:
                return

            total_ce_oi = 0
            total_pe_oi = 0
            net_ce_oi_change = 0
            net_pe_oi_change = 0
            max_ce_oi = 0
            max_pe_oi = 0
            max_ce_strike = None
            max_pe_strike = None

            for strike, data in self.options_live.items():
                ce = data.get("CE", {})
                pe = data.get("PE", {})

                ce_oi = ce.get("oi", 0)
                pe_oi = pe.get("oi", 0)
                total_ce_oi += ce_oi
                total_pe_oi += pe_oi

                net_ce_oi_change += ce_oi - ce.get("prev_oi", 0)
                net_pe_oi_change += pe_oi - pe.get("prev_oi", 0)

                if ce_oi > max_ce_oi:
                    max_ce_oi = ce_oi
                    max_ce_strike = strike
                if pe_oi > max_pe_oi:
                    max_pe_oi = pe_oi
                    max_pe_strike = strike

            agg = self.options_aggregate
            agg["total_ce_oi"] = total_ce_oi
            agg["total_pe_oi"] = total_pe_oi
            agg["live_pcr"] = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 0.0
            agg["max_oi_ce_strike"] = max_ce_strike
            agg["max_oi_pe_strike"] = max_pe_strike
            agg["net_ce_oi_change"] = net_ce_oi_change
            agg["net_pe_oi_change"] = net_pe_oi_change

            # ATM straddle premium
            if spot_price and self.options_live:
                closest_strike = min(self.options_live.keys(), key=lambda s: abs(s - spot_price))
                agg["atm_strike"] = closest_strike
                atm_data = self.options_live.get(closest_strike, {})
                ce_ltp = atm_data.get("CE", {}).get("ltp", 0)
                pe_ltp = atm_data.get("PE", {}).get("ltp", 0)
                agg["atm_straddle_premium"] = ce_ltp + pe_ltp

            import time as _time
            agg["last_updated"] = _time.time()

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
        self.analysis = {"Timestamp" : None,
                            "BULLISH":{},
                            "BEARISH":{},
                            "NEUTRAL":{},
                            "NoOfTrends": 0,
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
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            prev_data = self.priceData.iloc[-4]
        else:
            prev_data = self.priceData.iloc[-3]
        return prev_data
    

    def removeStockData(self):
        self.priceData = pd.DataFrame()
        self.ivData = None
    
    def is_price_data_empty(self):
        return self.priceData.empty
