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
            self.analysis[trend][analysis_type] = data
            self.analysis['NoOfTrends'] += 1
    
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
