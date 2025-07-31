import sys
import os
sys.path.append(os.getcwd())

from datetime import datetime

import yfinance as yf
from fno.OptionOpstraCollection import getIVChartData
import pandas as pd
import common.constants as constant
from common.helperFunctions import percentageChange
import pandas as pd
from threading import Lock
import numpy as np
from nse.nse_derivative_data import NSE_DATA_CLASS
import  common.shared as shared
from common.logging_util import logger

pd.options.mode.chained_assignment = None

def get_futures_and_options_data_from_nse_intraday(stock):
        currexpiry = shared.stockExpires[0]
        nextexpiry = shared.stockExpires[1]
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
    currexpiry = shared.stockExpires[0]
    nextexpiry = shared.stockExpires[1]
    try :
        data = NSE_DATA_CLASS.get_future_price_volume_data_positional(self.stock_symbol,"FUTSTK", None, None, '1W', currexpiry, nextexpiry)
    except Exception:
        logger.error("Error while getting the futures and options data")
        raise Exception()
    
    self.derivativesData["futuresData"]["currExpiry"] = data["futuresData"]["currExpiry"]
    self.derivativesData["futuresData"]["nextExpiry"] = data["futuresData"]["nextExpiry"]

    return self.derivativesData

class Stock:
    def __init__(self, stockName : str , stockSymbol : str, yfinanceSymbol = None):
        self.stockName = stockName
        self.stock_symbol = stockSymbol
        if yfinanceSymbol is not None:
            self.stockSymbolYFinance = yfinanceSymbol
        else :
            self.stockSymbolYFinance = stockSymbol+".NS"
        self.prevDayOHLCV = None
        self.last_price_update = None
        self._priceData = pd.DataFrame()
        self.last_trend_timestamp = None
        self.derivativesData = { 
                        "futuresData": {"currExpiry" : None, "nextExpiry" : None} , 
                        "optionsData": {"currExpiry" : None, "nextExpiry" : None} 
        }
        self.analysis = {"Timestamp" : None,
                        "BULLISH":{},
                        "BEARISH":{},
                        "NEUTRAL":{},
                        "NoOfTrends": 0,
                        }

    def set_prev_day_ohlcv(self, open, close, high, low, volume):
        self.prevDayOHLCV = {"OPEN":open, "HIGH":high, "LOW":low, "CLOSE":close, "VOLUME":volume}
    
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
        if constant.mode.name == constant.Mode.INTRADAY.name:
            curr_data = self.priceData.iloc[-2]
        else:
            curr_data = self.priceData.iloc[-1]
        return curr_data
    
    @property
    def previous_equity_data(self):
        if constant.mode.name == constant.Mode.INTRADAY.name:
            prev_data = self.priceData.iloc[-3]
        else:
            prev_data = self.priceData.iloc[-2]
        return prev_data
    
    @property
    def previous_previous_equity_data(self):
        if constant.mode.name == constant.Mode.INTRADAY.name:
            prev_data = self.priceData.iloc[-4]
        else:
            prev_data = self.priceData.iloc[-3]
        return prev_data
    

    def removeStockData(self):
        self.priceData = pd.DataFrame()
        self.ivData = None
    
    def is_price_data_empty(self):
        return self.priceData.empty
