import sys
import os
sys.path.append(os.getcwd())

from datetime import datetime

import yfinance as yf
from fno.OptionOpstraCollection import getIVChartData
import pandas as pd
from fno.OptionOpstraCollection import get_FII_DII_Data
import pandas_ta as ta

pd.options.mode.chained_assignment = None

class Stock:
    def __init__(self, stockName, stockSymbolYFinance, stockSymbolOpestra):
        self.stockName = stockName
        self.stockSymbolYFinance = stockSymbolYFinance
        self.last_price_update = None
        self.stockSymbolOpestra = stockSymbolOpestra
        self.priceData = pd.DataFrame()
        self.ivData = None
        self.last_trend_timestamp = None
        self.analysis = {"Timestamp" : None,
                        "BULLISH":{},
                        "BEARISH":{},
                        "NEUTRAL":{}}

    def get_stock_price_data(self, period , interval):
        try :
            self.priceData = yf.download(self.stockSymbolYFinance, period=period, interval=interval)
            self.last_price_update = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            # self.priceData = self.priceData.dropna()
            # self.priceData['rsi'] = self.compute_rsi(self.priceData['Close'])
            # self.priceData['upper_bb'], self.priceData['lower_bb'] = self.bollinger_band_data(self.priceData['Close'])
        except Exception:
            raise Exception()
        return  self.priceData
    
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
                        "NEUTRAL":{}}

    def compute_rsi(self, rsi_lookback = 14):
        closeSeries = self.priceData["Close"]
        ret = closeSeries.diff()
        up = []
        down = []
        for i in range(len(ret)):
            if ret[i] < 0:
                up.append(0)
                down.append(ret[i])
            else:
                up.append(ret[i])
                down.append(0)
        up_series = pd.Series(up)
        down_series = pd.Series(down).abs()
        up_ewm = up_series.ewm(com=rsi_lookback - 1, adjust=False).mean()
        down_ewm = down_series.ewm(com=rsi_lookback - 1, adjust=False).mean()
        rs = up_ewm / down_ewm
        rsi = 100 - (100 / (1 + rs))
        rsi_df = pd.DataFrame(rsi).rename(columns = {0:'rsi'}).set_index(closeSeries.index)
        rsi_df = rsi_df.dropna()
        self.priceData["rsi"] = rsi_df[3:]
        return rsi_df[3:]

    def bollinger_band_data(self, data, window = 20):
        sma = data.rolling(window=window).mean()
        std = data.rolling(window=window).std()
        upper_bb = sma + std * 2
        lower_bb = sma - std * 2
        return upper_bb, lower_bb
    
    def compute_sma_of_volume(self, window):
        if not self.priceData.empty :
            self.priceData['Vol_SMA_'+str(window)] = self.priceData['Volume'].rolling(window).mean()
            return self.priceData['Vol_SMA_'+str(window)]
    
    def compute_atr_rank(self):
        self.priceData['atr_rank'] = self.priceData.ta.atr().rank(pct = True)
        # self.priceData['atr_rank'] = self.priceData.ta.atr()

    def removeStockData(self):
        self.priceData = None
        self.ivData = None

    def __repr__(self):
        return """Stock Name : {}
                Analysis Results : {} """.format(self.stockName,self.analysisResult)