import sys
import os
sys.path.append(os.getcwd())

import pandas as pd
import common.constants as constant
from common.helperFunctions import percentageChange
import pandas as pd
import threading
from nse.nse_derivative_data import NSE_DATA_CLASS
import  common.shared as shared
from common.logging_util import logger
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
import datetime 
import time


pd.options.mode.chained_assignment = None

def get_futures_and_options_data_from_nse_intraday(stock):
        currexpiry = shared.app_ctx.stockExpires[0]
        nextexpiry = shared.app_ctx.stockExpires[1]
        try :
            data = NSE_DATA_CLASS.get_live_futures_and_options_data_intraday(stock.stock_symbol, currexpiry, nextexpiry)
        except Exception:
            logger.error("Error while getting the futures and options data")
            raise Exception()
        
        if stock.derivativesData["futuresData"]["currExpiry"] is None:
            stock.derivativesData["futuresData"]["currExpiry"] = data["futuresData"]["currExpiry"]
        else:
            stock.derivativesData["futuresData"]["currExpiry"]  = pd.concat([stock.derivativesData["futuresData"]["currExpiry"], data["futuresData"]["currExpiry"]], ignore_index=True)
            if len(stock.derivativesData["futuresData"]["currExpiry"]) > Stock.DERIVATIVE_DATA_LENGTH:
                stock.derivativesData["futuresData"]["currExpiry"] = stock.derivativesData["futuresData"]["currExpiry"].tail(Stock.DERIVATIVE_DATA_LENGTH) 
        
        if stock.derivativesData["futuresData"]["nextExpiry"] is None:
            stock.derivativesData["futuresData"]["nextExpiry"] = data["futuresData"]["nextExpiry"]
        else:
            stock.derivativesData["futuresData"]["nextExpiry"]  = pd.concat([stock.derivativesData["futuresData"]["nextExpiry"], data["futuresData"]["nextExpiry"]], ignore_index=True)
            if len(stock.derivativesData["futuresData"]["nextExpiry"]) > Stock.DERIVATIVE_DATA_LENGTH:
                stock.derivativesData["futuresData"]["nextExpiry"] = stock.derivativesData["futuresData"]["nextExpiry"].tail(Stock.DERIVATIVE_DATA_LENGTH) 
        
        return stock.derivativesData

def get_futures_and_options_data_from_nse_positional(self):
    currexpiry = shared.app_ctx.stockExpires[0]
    nextexpiry = shared.app_ctx.stockExpires[1]
    try :
        data = NSE_DATA_CLASS.get_future_price_volume_data_positional(self.stock_symbol,"FUTSTK", None, None, '1W', currexpiry, nextexpiry)
    except Exception:
        logger.error("Error while getting the futures and options data")
        raise Exception()
    
    self.derivativesData["futuresData"]["currExpiry"] = data["futuresData"]["currExpiry"]
    self.derivativesData["futuresData"]["nextExpiry"] = data["futuresData"]["nextExpiry"]

    return self.derivativesData

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
            "atm_data": {"current": {}, "next": {}},
            "option_chain_data" : {"current":pd.DataFrame(), "next":pd.DataFrame()},
        }

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
        current_close = self.priceData['Close'].iloc[-1]
        # previous_close = stock.priceData['Close'].iloc[-2]
        previous_close = self.prevDayOHLCV['CLOSE']
        change_percent = percentageChange(current_close, previous_close)
        self.ltp = current_close
        self.ltp_change_perc = change_percent


    @staticmethod
    def black_scholes_price(S, K, T, r, sigma, option_type):
        """Calculate Black-Scholes price for a European option."""
        d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        if option_type == "CE":  # Call
            price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
        else:  # Put
            price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
        return price

    @staticmethod
    def implied_volatility(option_price, S, K, T, r, option_type):
        """Numerically solve for implied volatility."""
        try:
            return brentq(
                lambda sigma: Stock.black_scholes_price(S, K, T, r, sigma, option_type) - option_price,
                1e-6, 5.0, maxiter=500
            )
        except Exception:
            return np.nan
        
    @staticmethod
    def compute_iv_for_option_chain(option_price, option_strike, option_expiry, instrument_type, underlying_price, date, risk_free_rate=0.065):
        """
        Adds an 'iv' column to the option_chain_df DataFrame.
        Assumes 'close', 'strike', 'expiry', 'instrument_type' columns exist.
        """
        today_date = date
        option_price = option_price
        K = option_strike
        expiry = option_expiry
        option_type = instrument_type  # 'CE' or 'PE'
        S = underlying_price
        r = risk_free_rate
        # Calculate time to expiry in years
        if isinstance(expiry, str):
            expiry_date = pd.to_datetime(expiry)
        elif isinstance(expiry, datetime.date) and not isinstance(expiry, datetime.datetime):
            expiry_date = datetime.datetime.combine(expiry, datetime.datetime.min.time())
        else:
            expiry_date = expiry
        # Ensure today_date matches expiry_date's timezone
        if expiry_date.tzinfo is not None and expiry_date.tzinfo.utcoffset(expiry_date) is not None:
            if today_date.tzinfo is None or today_date.tzinfo.utcoffset(today_date) is None:
                today_date = today_date.replace(tzinfo=expiry_date.tzinfo)
        else:
            today_date = today_date.replace(tzinfo=None)
        T = max((expiry_date - today_date).days / 365.0, 1/365)  # Avoid zero division
        iv = Stock.implied_volatility(option_price, S, K, T, r, option_type)
        return iv

    def set_atm_data_for_stock(self, mode="positional"):
        """
        Finds the ATM option for the given stock.
        """
        # Find ATM strike for current expiry
        zerodha_ctx = self.zerodha_ctx
        if not zerodha_ctx:
            logger.warning(f"Zerodha context not found for {self.stock_symbol}")
            return None, None
        current_option_chain = zerodha_ctx['option_chain_data']["current"]
        next_options_chain = zerodha_ctx["option_chain_data"]["next"]

        unique_dates = current_option_chain.index.unique().tolist()

        for date in unique_dates:
            if date in zerodha_ctx["atm_data"]["current"].keys():
                continue
            option_chain_curr = current_option_chain.loc[date]
            dt = pd.Timestamp(date).tz_convert('Asia/Kolkata')
            if mode == "positional":
                dt = dt.replace(hour=5, minute=30, second=0)
            underlying_price = self.priceData.loc[dt, "Close"]
            if not option_chain_curr.empty :
                # Find the strike closest to the current price
                atm_row = option_chain_curr.iloc[(option_chain_curr['strike'] - underlying_price).abs().argmin()]
                zerodha_ctx["atm_data"]["current"][date] = atm_row
                logger.info(f"Current ATM strike for {self.stock_symbol} on {date}: {atm_row['strike']}")
            else:
                zerodha_ctx["atm_data"]["current"][date] = None
                logger.info(f"Current ATM strike not found for {self.stock_symbol} on {date}")
        
        unique_dates = next_options_chain.index.unique().tolist()
        for date in unique_dates:
            if date in zerodha_ctx["atm_data"]["next"].keys():
                continue
            option_chain_next = next_options_chain.loc[date]
            dt = pd.Timestamp(date).tz_convert('Asia/Kolkata')
            if mode == "positional":
                dt = dt.replace(hour=5, minute=30, second=0)
            underlying_price = self.priceData.loc[dt, "Close"]
            if not option_chain_next.empty :
                # Find the strike closest to the current price
                atm_row = option_chain_next.iloc[(option_chain_next['strike'] - underlying_price).abs().argmin()]
                zerodha_ctx["atm_data"]["next"][date] = atm_row
                logger.info(f"Next ATM strike for {self.stock_symbol} on {date}: {atm_row['strike']}")
            else:
                zerodha_ctx["atm_data"]["next"][date] = None
                logger.info(f"Next ATM strike not found for {self.stock_symbol} on {date}")
        
        return zerodha_ctx["atm_data"]

    def get_complete_option_chain(self, mode="positional"):
        """
        Fetch option chain data for positional (daily) or intraday (5min) mode.
        On first run, adds all data for the period.
        On subsequent runs (intraday), appends/updates only the latest data.
        """
        def helper(option_df, price_df, old_option_chain, interval, start_date, end_date):
            option_chain_data = []
            kite_connect = shared.app_ctx.zd_kc

            for _, row in option_df.iterrows():
                token = row['instrument_token']
                try:
                    for attempt in range(3):  # Retry up to 3 times
                        try:
                            hist_data = kite_connect.historical_data(
                                instrument_token=token,
                                from_date=start_date.strftime("%Y-%m-%d"),
                                to_date=end_date.strftime("%Y-%m-%d"),
                                interval=interval,
                                oi=True
                            )
                            break  # Success, exit retry loop
                        except Exception as e:
                            if "Too many requests" in str(e):
                                logger.warning("Rate limit hit, sleeping for 2 seconds...")
                                time.sleep(2)
                            else:
                                raise
                    
                    for candle in hist_data:
                        dt = pd.Timestamp(candle['date']).tz_convert('Asia/Kolkata')
                        if interval == "day":
                            dt = dt.replace(hour=5, minute=30, second=0)
                        # For intraday, only add if not already present in old_option_chain
                        if interval == "5minute" and old_option_chain is not None and dt in old_option_chain.index:
                            continue
                        underlying_price = price_df.loc[dt, "Close"] if dt in price_df.index else np.nan
                        iv = Stock.compute_iv_for_option_chain(
                            candle['close'],
                            row['strike'],
                            row['expiry'],
                            row['instrument_type'],
                            underlying_price,
                            dt,
                            risk_free_rate=0.06
                        )
                        option_chain_data.append({
                            "instrument_token": token,
                            "tradingsymbol": row['tradingsymbol'],
                            "expiry": row['expiry'],
                            "strike": row['strike'],
                            "instrument_type": row['instrument_type'],
                            "date": dt,
                            "open": candle['open'],
                            "high": candle['high'],
                            "low": candle['low'],
                            "close": candle['close'],
                            "volume": candle['volume'],
                            "oi": candle.get('oi', None), 
                            "underlying_price": underlying_price,
                            "iv": iv,
                        })
                except Exception as e:
                    logger.error(f"Error fetching data for {row['tradingsymbol']}: {e}")
                    raise Exception(f"Error fetching data for {row['tradingsymbol']}: {e}")
            df = pd.DataFrame(option_chain_data)
            if not df.empty:
                df.set_index("date", inplace=True)
                # For intraday, append new data to old_option_chain
                if interval == "5minute" and old_option_chain is not None and not old_option_chain.empty:
                    df = pd.concat([old_option_chain, df])
                    df = df[~df.index.duplicated(keep='last')]
                    df = df.sort_index()
            elif old_option_chain is not None:
                df = old_option_chain
            return df

        current_option_df = self.zerodha_ctx['option_chain']["current"]
        next_option_df = self.zerodha_ctx['option_chain']["next"]
        try:
            if mode == "positional":
                interval = "day"
                end_date = datetime.datetime.now()
                business_days = pd.bdate_range(end=end_date, periods=5).to_pydatetime()
                start_date = business_days[0]
                old_current = None
                old_next = None
            elif mode == "intraday":
                interval = "5minute"
                today = datetime.datetime.now()
                start_date = today
                end_date = today
                old_current = self.zerodha_ctx['option_chain_data']["current"]
                old_next = self.zerodha_ctx['option_chain_data']["next"]
            else:
                raise ValueError("mode must be 'positional' or 'intraday'")

            if current_option_df is not None:
                option_df = helper(current_option_df, self.priceData, old_current, interval, start_date, end_date)
                self.zerodha_ctx['option_chain_data']["current"] = option_df

            if next_option_df is not None:
                option_df = helper(next_option_df, self.priceData, old_next, interval, start_date, end_date)
                self.zerodha_ctx['option_chain_data']["next"] = option_df

            self.set_atm_data_for_stock(mode=mode)
            return self.zerodha_ctx['option_chain_data']
        except Exception as e:
            logger.error(f"get_option_chain ({mode}) failed: {e}")
            raise Exception(f"get_option_chain ({mode}) failed: {e}")

    def get_atm_data_for_stock(self, mode):
        """
        Retrieves the ATM data for the stock based on the specified mode.
        Raises exception on error and retries historical data fetch up to 3 times if 'Too many requests' error occurs.
        """
        zerodha_ctx = self.zerodha_ctx
        kite_connect = shared.app_ctx.zd_kc
        current_option_df = zerodha_ctx['option_chain']["current"]
        next_option_df = zerodha_ctx['option_chain']["next"]
        atm_data_current = {}
        atm_data_next = {}

        try:
            if mode == "Positional":
                business_days = pd.bdate_range(end=datetime.datetime.now(), periods=5).to_pydatetime()
                for date in business_days:
                    dt = pd.Timestamp(date).tz_localize('Asia/Kolkata').replace(hour=5, minute=30, second=0)
                    if dt not in self.priceData.index:
                        logger.info(f"No price data for {self.stock_symbol} on {dt}")
                        continue
                    underlying_price = self.priceData.loc[dt, "Close"]

                    # Current expiry ATM
                    if current_option_df is not None and not current_option_df.empty:
                        atm_row = current_option_df.iloc[(current_option_df['strike'] - underlying_price).abs().argmin()]
                        for attempt in range(3):
                            try:
                                hist_data = kite_connect.historical_data(
                                    instrument_token=atm_row['instrument_token'],
                                    from_date=dt.strftime("%Y-%m-%d"),
                                    to_date=dt.strftime("%Y-%m-%d"),
                                    interval="day",
                                    oi=True
                                )
                                break
                            except Exception as e:
                                if "Too many requests" in str(e):
                                    logger.warning("Rate limit hit, sleeping for 2 seconds...")
                                    time.sleep(2)
                                else:
                                    logger.error(f"Error fetching historical data for {self.stock_symbol} ATM (current expiry) on {dt}: {e}")
                                    raise
                        else:
                            logger.error(f"Failed to fetch historical data for {self.stock_symbol} ATM (current expiry) on {dt} after 3 attempts")
                            raise Exception(f"Too many requests for {self.stock_symbol} ATM (current expiry) on {dt}")
                        if hist_data:
                            candle = hist_data[0]
                            atm_row = atm_row.copy()
                            atm_row['open'] = candle['open']
                            atm_row['high'] = candle['high']
                            atm_row['low'] = candle['low']
                            atm_row['close'] = candle['close']
                            atm_row['volume'] = candle['volume']
                            atm_row['oi'] = candle.get('oi', None)
                            atm_row['iv'] = Stock.compute_iv_for_option_chain(
                                candle['close'],
                                atm_row['strike'],
                                atm_row['expiry'],
                                atm_row['instrument_type'],
                                underlying_price,
                                dt,
                                risk_free_rate=0.06
                            )
                            atm_row['underlying_price'] = underlying_price
                            atm_data_current[dt] = atm_row
                            logger.info(f"ATM data for {self.stock_symbol} (current expiry) on {dt}: {atm_row['strike']}, IV: {atm_row['iv']}")
                        else:
                            logger.info(f"No historical data for ATM {self.stock_symbol} {atm_row['strike']} (current expiry) on {dt}")

                    # Next expiry ATM
                    if next_option_df is not None and not next_option_df.empty:
                        atm_row_next = next_option_df.iloc[(next_option_df['strike'] - underlying_price).abs().argmin()]
                        for attempt in range(3):
                            try:
                                hist_data_next = kite_connect.historical_data(
                                    instrument_token=atm_row_next['instrument_token'],
                                    from_date=dt.strftime("%Y-%m-%d"),
                                    to_date=dt.strftime("%Y-%m-%d"),
                                    interval="day",
                                    oi=True
                                )
                                break
                            except Exception as e:
                                if "Too many requests" in str(e):
                                    logger.warning("Rate limit hit, sleeping for 2 seconds...")
                                    time.sleep(2)
                                else:
                                    logger.error(f"Error fetching historical data for {self.stock_symbol} ATM (next expiry) on {dt}: {e}")
                                    raise
                        else:
                            logger.error(f"Failed to fetch historical data for {self.stock_symbol} ATM (next expiry) on {dt} after 3 attempts")
                            raise Exception(f"Too many requests for {self.stock_symbol} ATM (next expiry) on {dt}")
                        if hist_data_next:
                            candle_next = hist_data_next[0]
                            atm_row_next = atm_row_next.copy()
                            atm_row_next['open'] = candle_next['open']
                            atm_row_next['high'] = candle_next['high']
                            atm_row_next['low'] = candle_next['low']
                            atm_row_next['close'] = candle_next['close']
                            atm_row_next['volume'] = candle_next['volume']
                            atm_row_next['oi'] = candle_next.get('oi', None)
                            atm_row_next['iv'] = Stock.compute_iv_for_option_chain(
                                candle_next['close'],
                                atm_row_next['strike'],
                                atm_row_next['expiry'],
                                atm_row_next['instrument_type'],
                                underlying_price,
                                dt,
                                risk_free_rate=0.06
                            )
                            atm_row_next['underlying_price'] = underlying_price
                            atm_data_next[dt] = atm_row_next
                            logger.info(f"ATM data for {self.stock_symbol} (next expiry) on {dt}: {atm_row_next['strike']}, IV: {atm_row_next['iv']}")
                        else:
                            logger.info(f"No historical data for ATM {self.stock_symbol} {atm_row_next['strike']} (next expiry) on {dt}")

                zerodha_ctx["atm_data"]["current"] = atm_data_current
                zerodha_ctx["atm_data"]["next"] = atm_data_next
                return atm_data_current, atm_data_next

            elif mode == "Intraday":
                atm_data_current = zerodha_ctx["atm_data"]["current"]
                atm_data_next = zerodha_ctx["atm_data"]["next"]

                today_str = datetime.datetime.now().strftime("%Y-%m-%d")
                intraday_dates = [dt for dt in self.priceData.index if dt.strftime("%Y-%m-%d") == today_str]
                if len(intraday_dates) < 2:
                    logger.info(f"Not enough intraday data for {self.stock_symbol}")
                    raise Exception(f"Not enough intraday data for {self.stock_symbol}")

                dt = intraday_dates[-2]
                underlying_price = self.priceData.loc[dt, "Close"]

                # Current expiry ATM
                if current_option_df is not None and not current_option_df.empty:
                    atm_row = current_option_df.iloc[(current_option_df['strike'] - underlying_price).abs().argmin()]
                    for attempt in range(3):
                        try:
                            hist_data = kite_connect.historical_data(
                                instrument_token=atm_row['instrument_token'],
                                from_date=dt.strftime("%Y-%m-%d"),
                                to_date=dt.strftime("%Y-%m-%d"),
                                interval="5minute",
                                oi=True
                            )
                            break
                        except Exception as e:
                            if "Too many requests" in str(e):
                                logger.warning("Rate limit hit, sleeping for 2 seconds...")
                                time.sleep(2)
                            else:
                                logger.error(f"Error fetching intraday data for {self.stock_symbol} ATM (current expiry) at {dt}: {e}")
                                raise
                    else:
                        logger.error(f"Failed to fetch intraday data for {self.stock_symbol} ATM (current expiry) at {dt} after 3 attempts")
                        raise Exception(f"Too many requests for {self.stock_symbol} ATM (current expiry) at {dt}")
                    if hist_data:
                        candle = next((c for c in hist_data if pd.Timestamp(c['date']).tz_convert('Asia/Kolkata') == dt), None)
                        if candle:
                            atm_row = atm_row.copy()
                            atm_row['open'] = candle['open']
                            atm_row['high'] = candle['high']
                            atm_row['low'] = candle['low']
                            atm_row['close'] = candle['close']
                            atm_row['volume'] = candle['volume']
                            atm_row['oi'] = candle.get('oi', None)
                            atm_row['iv'] = Stock.compute_iv_for_option_chain(
                                candle['close'],
                                atm_row['strike'],
                                atm_row['expiry'],
                                atm_row['instrument_type'],
                                underlying_price,
                                dt,
                                risk_free_rate=0.06
                            )
                            atm_row['underlying_price'] = underlying_price
                            atm_data_current[dt] = atm_row
                            logger.info(f"ATM data for {self.stock_symbol} (current expiry) at {dt}: {atm_row['strike']}, IV: {atm_row['iv']}")
                    else:
                        logger.info(f"No intraday data for ATM {self.stock_symbol} (current expiry) at {dt}")

                # Next expiry ATM
                if next_option_df is not None and not next_option_df.empty:
                    atm_row_next = next_option_df.iloc[(next_option_df['strike'] - underlying_price).abs().argmin()]
                    for attempt in range(3):
                        try:
                            hist_data_next = kite_connect.historical_data(
                                instrument_token=atm_row_next['instrument_token'],
                                from_date=dt.strftime("%Y-%m-%d"),
                                to_date=dt.strftime("%Y-%m-%d"),
                                interval="5minute",
                                oi=True
                            )
                            break
                        except Exception as e:
                            if "Too many requests" in str(e):
                                logger.warning("Rate limit hit, sleeping for 2 seconds...")
                                time.sleep(2)
                            else:
                                logger.error(f"Error fetching intraday data for {self.stock_symbol} ATM (next expiry) at {dt}: {e}")
                                raise
                    else:
                        logger.error(f"Failed to fetch intraday data for {self.stock_symbol} ATM (next expiry) at {dt} after 3 attempts")
                        raise Exception(f"Too many requests for {self.stock_symbol} ATM (next expiry) at {dt}")
                    if hist_data_next:
                        candle_next = next((c for c in hist_data_next if pd.Timestamp(c['date']).tz_convert('Asia/Kolkata') == dt), None)
                        if candle_next:
                            atm_row_next = atm_row_next.copy()
                            atm_row_next['open'] = candle_next['open']
                            atm_row_next['high'] = candle_next['high']
                            atm_row_next['low'] = candle_next['low']
                            atm_row_next['close'] = candle_next['close']
                            atm_row_next['volume'] = candle_next['volume']
                            atm_row_next['oi'] = candle_next.get('oi', None)
                            atm_row_next['iv'] = Stock.compute_iv_for_option_chain(
                                candle_next['close'],
                                atm_row_next['strike'],
                                atm_row_next['expiry'],
                                atm_row_next['instrument_type'],
                                underlying_price,
                                dt,
                                risk_free_rate=0.06
                            )
                            atm_row_next['underlying_price'] = underlying_price
                            atm_data_next[dt] = atm_row_next
                            logger.info(f"ATM data for {self.stock_symbol} (next expiry) at {dt}: {atm_row_next['strike']}, IV: {atm_row_next['iv']}")
                    else:
                        logger.info(f"No intraday data for ATM {self.stock_symbol} (next expiry) at {dt}")

                zerodha_ctx["atm_data"]["current"] = atm_data_current
                zerodha_ctx["atm_data"]["next"] = atm_data_next
                return atm_data_current, atm_data_next

            else:
                raise ValueError("mode must be 'positional' or 'intraday'")

        except Exception as e:
            logger.error(f"get_atm_data_for_stock failed for {self.stock_symbol}: {e}")
            raise
        

    @property
    def zerodha_data(self):
        """Thread-safe getter for zerodha_data"""
        with self._zerodha_lock:
            return self._zerodha_data.copy()  # Return a copy to prevent external modifications
    
    def update_zerodha_data(self, ticker_data):
        """
        Thread-safe update of the Zerodha data for total buy and sell quantities.

        Args:
            buy_quantity (int): The buy quantity to be added.
            sell_quantity (int): The sell quantity to be added.
        """
        with self._zerodha_lock:
            self._zerodha_data["volume_traded"] = ticker_data["volume_traded"]
            self._zerodha_data["last_price"] = ticker_data["last_price"]
            self._zerodha_data["open"] = ticker_data["ohlc"]["open"]
            self._zerodha_data["high"] = ticker_data["ohlc"]["high"]
            self._zerodha_data["close"] = ticker_data["ohlc"]["close"]
            self._zerodha_data["low"] = ticker_data["ohlc"]["low"] 
            self._zerodha_data["change"] = ticker_data["change"]
            self._zerodha_data["average_traded_price"] = ticker_data["average_traded_price"]  
            self._zerodha_data["total_buy_quantity"] = ticker_data["total_buy_quantity"]
            self._zerodha_data["total_sell_quantity"] = ticker_data["total_sell_quantity"]

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
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            curr_data = self.priceData.iloc[-2]
        else:
            curr_data = self.priceData.iloc[-1]
        return curr_data
    
    @property
    def previous_equity_data(self):
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            prev_data = self.priceData.iloc[-3]
        else:
            prev_data = self.priceData.iloc[-2]
        return prev_data
    
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
