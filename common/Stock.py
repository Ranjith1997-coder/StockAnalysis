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

class Stock:
    DERIVATIVE_DATA_LENGTH = 30 # total number of rows to store in the derivatives dataframe 
    def __init__(self, stockName : str , stockSymbol : str):
        self.stockName = stockName
        self.stock_symbol = stockSymbol
        self.stockSymbolYFinance = stockSymbol+".NS"
        self.last_price_update = None
        self.stockSymbolOpestra = stockSymbol
        self.priceData = pd.DataFrame()
        self.ivData = None
        self.last_trend_timestamp = None
        self.zd_data_mutux = Lock()
        self.derivativesData = { 
                        "futuresData": {"currExpiry" : None, "nextExpiry" : None} , 
                        "optionsData": {"currExpiry" : None, "nextExpiry" : None} 
        }
        self.analysis = {"Timestamp" : None,
                        "BULLISH":{},
                        "BEARISH":{},
                        "NEUTRAL":{}
                        }

    def get_stock_price_data(self, period:str , interval:str):
        try :
            self.priceData = yf.download(self.stockSymbolYFinance, period=period, interval=interval)
            self.last_price_update = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            logger.error("Error while getting the stock price data")
            raise Exception()
        return  self.priceData
    
    def get_futures_and_options_data_from_nse_intraday(self):
        currexpiry = shared.stockExpires[0]
        nextexpiry = shared.stockExpires[1]
        try :
            data = NSE_DATA_CLASS.get_live_futures_and_options_data_intraday(self.stock_symbol, currexpiry, nextexpiry)
        except Exception:
            logger.error("Error while getting the futures and options data")
            raise Exception()
        
        if self.derivativesData["futuresData"]["currExpiry"] is None:
            self.derivativesData["futuresData"]["currExpiry"] = data["futuresData"]["currExpiry"]
        else:
            self.derivativesData["futuresData"]["currExpiry"]  = pd.concat([self.derivativesData["futuresData"]["currExpiry"], data["futuresData"]["currExpiry"]], ignore_index=True)
            if len(self.derivativesData["futuresData"]["currExpiry"]) > Stock.DERIVATIVE_DATA_LENGTH:
                self.derivativesData["futuresData"]["currExpiry"] = self.derivativesData["futuresData"]["currExpiry"].tail(Stock.DERIVATIVE_DATA_LENGTH) 
        
        if self.derivativesData["futuresData"]["nextExpiry"] is None:
            self.derivativesData["futuresData"]["nextExpiry"] = data["futuresData"]["nextExpiry"]
        else:
            self.derivativesData["futuresData"]["nextExpiry"]  = pd.concat([self.derivativesData["futuresData"]["nextExpiry"], data["futuresData"]["nextExpiry"]], ignore_index=True)
            if len(self.derivativesData["futuresData"]["nextExpiry"]) > Stock.DERIVATIVE_DATA_LENGTH:
                self.derivativesData["futuresData"]["nextExpiry"] = self.derivativesData["futuresData"]["nextExpiry"].tail(Stock.DERIVATIVE_DATA_LENGTH) 
        
        return self.derivativesData
    
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


    def get_stock_IV_data(self):
        try :
            self.ivData = getIVChartData(self.stockSymbolOpestra)[1]
        except Exception:
            raise Exception()
    
    def filter_data_from_bulk_download(self, bulk_data):
        self.priceData["Adj Close"] = bulk_data.loc[:,('Adj Close',self.stockSymbolYFinance)]
        self.priceData["Volume"] = bulk_data.loc[:,('Volume',self.stockSymbolYFinance)]
        self.priceData["Open"] = bulk_data.loc[:,('Open',self.stockSymbolYFinance)]
        self.priceData["Close"] = bulk_data.loc[:,('Close',self.stockSymbolYFinance)]
        self.priceData["High"] = bulk_data.loc[:,('High',self.stockSymbolYFinance)]
        self.priceData["Low"] = bulk_data.loc[:,('Low',self.stockSymbolYFinance)]
    
    def reset_price_data(self):
        self.priceData = self.priceData[0:0]
    
    def reset_analysis(self):
        self.analysis = {"Timestamp" : None,
                            "BULLISH":{},
                            "BEARISH":{},
                            "NEUTRAL":{}
                        }

    def compute_rsi(self, rsi_lookback = 14):
        change = self.priceData['Close'].diff()
        up_series = change.mask(change < 0, 0.0)
        down_series = -change.mask(change > 0, -0.0)

        #@numba.jit
        def rma(x, n):
            """Running moving average"""
            a = np.full_like(x, np.nan)
            # pdb.set_trace()
            a[n] = x[1:n+1].mean()
            for i in range(n+1, len(x)):
                a[i] = (a[i-1] * (n - 1) + x[i]) / n
            return a

        avg_gain = rma(up_series.to_numpy(), 14)
        avg_loss = rma(down_series.to_numpy(), 14)

        rs = avg_gain / avg_loss
        self.priceData['rsi'] = 100 - (100 / (1 + rs))
        return self.priceData['rsi']

    def compute_bollinger_band(self, window = 20):
        sma = self.priceData['Close'].rolling(window=window).mean()
        std = self.priceData['Close'].rolling(window=window).std()
        upper_bb = sma + (std * 2)
        lower_bb = sma - (std * 2)
        self.priceData["BB_UPPER_BAND"] = upper_bb
        self.priceData["BB_LOWER_BAND"] = lower_bb
        self.priceData["BB_SMA_20"] = sma
        return (sma, upper_bb, lower_bb)
    
    def compute_sma_of_volume(self, window):
        if not self.priceData.empty :
            self.priceData['Vol_SMA_'+str(window)] = self.priceData['Volume'].rolling(window).mean()
            return self.priceData['Vol_SMA_'+str(window)]
    
    def compute_atr_rank(self):
        try:
            self.priceData['atr_rank'] = self.priceData.ta.atr().rank(pct = True)
        # self.priceData['atr_rank'] = self.priceData.ta.atr()
        except Exception as e:
            print(self.stockName)
    
    def compute_candle_stick_pattern(self):
        if constant.mode.name == constant.Mode.INTRADAY.name:
            self.compute_triple_increase_decrease()
            self.compute_marubasu_candle_stick()
            self.compute_double_increase_decrease()
        else:
            self.compute_triple_increase_decrease()
            self.compute_marubasu_candle_stick()
            self.compute_double_increase_decrease()

    def compute_triple_increase_decrease(self):
        length = self.priceData.shape[0]

        series = pd.Series(index = self.priceData.index)
        for index in range(2,length):
            curr_price = self.priceData.iloc[index]
            prev_price = self.priceData.iloc[index-1]
            prev_minus_one_price = self.priceData.iloc[index-2]

            if prev_minus_one_price["Open"].item() < prev_minus_one_price["Close"].item()\
                and prev_price["Open"].item() < prev_price["Close"].item()\
                    and curr_price["Open"].item() < curr_price["Close"].item()\
                    and curr_price["Close"].item() > prev_price["Close"].item() \
                            and prev_price["Close"].item() > prev_minus_one_price["Close"].item():
                series.iloc[index] = ((curr_price["Close"].item() - prev_minus_one_price["Open"].item())/prev_minus_one_price["Open"].item()) * 100
            elif prev_minus_one_price["Open"].item() > prev_minus_one_price["Close"].item()\
                and prev_price["Open"].item() > prev_price["Close"].item()\
                    and curr_price["Open"].item() > curr_price["Close"].item()\
                        and curr_price["Close"].item() < prev_price["Close"].item() \
                        and prev_price["Close"].item() < prev_minus_one_price["Close"].item():
                series.iloc[index] =  ((curr_price["Close"].item() - prev_minus_one_price["Open"].item())/prev_minus_one_price["Open"].item()) * 100
            else:
                series.iloc[index] = 0.0
        self.priceData["3_CONT_INC_OR_DEC"] = series
    
    def compute_double_increase_decrease(self):
        length = self.priceData.shape[0]

        series = pd.Series(index = self.priceData.index)
        for index in range(2,length):
            curr_price = self.priceData.iloc[index]
            prev_price = self.priceData.iloc[index-1]

            if prev_price["Open"].item() < prev_price["Close"].item()\
                    and curr_price["Open"].item() < curr_price["Close"].item()\
                    and curr_price["Close"].item() > prev_price["Close"].item():
                series.iloc[index] = ((curr_price["Close"].item() - prev_price["Open"].item())/prev_price["Open"].item()) * 100
            elif prev_price["Open"].item() > prev_price["Close"].item()\
                    and curr_price["Open"].item() > curr_price["Close"].item()\
                        and curr_price["Close"].item() < prev_price["Close"].item() :
                series.iloc[index] =  ((curr_price["Close"].item() - prev_price["Open"].item())/prev_price["Open"].item()) * 100
            else:
                series.iloc[index] = 0.0
        self.priceData["2_CONT_INC_OR_DEC"] = series
    
    def compute_marubasu_candle_stick(self, candleWickPercentage = 0.2):
        length = self.priceData.shape[0]

        series = pd.Series(index = self.priceData.index)
        for index in range(0,length):
            closePrice = self.priceData.iloc[index]['Close'].item()
            openPrice = self.priceData.iloc[index]['Open'].item()
            highPrice = self.priceData.iloc[index]['High'].item()
            lowPrice = self.priceData.iloc[index]['Low'].item()

            if (((openPrice == lowPrice) or (percentageChange(openPrice, lowPrice) <= candleWickPercentage)) \
                and ((highPrice == closePrice) or (percentageChange(highPrice, closePrice) <= candleWickPercentage))):
                series.iloc[index] = percentageChange(closePrice, openPrice)
            elif (((openPrice == highPrice) or (percentageChange(highPrice,openPrice) <= candleWickPercentage)) \
            and ((lowPrice == closePrice) or (percentageChange(closePrice,lowPrice) <= candleWickPercentage))):
                series.iloc[index] = percentageChange(closePrice, openPrice)
            else:
                series.iloc[index] = 0.0
        self.priceData["MARUBASU"] = series
    
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
    


    def removeStockData(self):
        self.priceData = pd.DataFrame()
        self.ivData = None
    
    def is_price_data_empty(self):
        return self.priceData.empty

    def __repr__(self):
        return """Stock Name : {}
                Analysis Results : {} """.format(self.stockName,self.analysisResult)