import sys
import os
sys.path.append(os.getcwd())

from datetime import datetime

import yfinance as yf
from fno.OptionOpstraCollection import getIVChartData
import pandas as pd
from fno.OptionOpstraCollection import get_FII_DII_Data
import pandas_ta as ta
from common.constants import mode,Mode
from common.helperFunctions import percentageChange
import pandas as pd

pd.options.mode.chained_assignment = None


class Stock:
    def __init__(self, stockName, stockSymbol):
        self.stockName = stockName
        self.stock_symbol = stockSymbol
        self.stockSymbolYFinance = stockSymbol+".NS"
        self.last_price_update = None
        self.stockSymbolOpestra = stockSymbol
        self.priceData = pd.DataFrame()
        self.ivData = None
        self.last_trend_timestamp = None
        # {
        #     ltp: last traded price
        #     volume: volume 
        #     buy_quantity: total buy quantity
        #     sell_quantity: total sell quantity
        # }
        self.zd_data = { "series_data" : [],
                         "open" : None,
                         "high" : None,
                         "low"  : None,
                         "data_count": 0,
                         "change" : None,
                         "last_updated_time_stamp" : []
                        }
        self.analysis = {"Timestamp" : None,
                        "BULLISH":{},
                        "BEARISH":{},
                        "NEUTRAL":{}}

    def get_stock_price_data(self, period , interval):
        try :
            self.priceData = yf.download(self.stockSymbolYFinance, period=period, interval=interval)
            self.last_price_update = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
                        "NEUTRAL":{}
                        }

    def compute_rsi(self, rsi_lookback = 14):
        closeSeries = self.priceData["Close"]
        ret = closeSeries.diff()
        up = []
        down = []
        for i in range(len(ret)):
            if ret.iloc[i] < 0:
                up.append(0)
                down.append(ret.iloc[i])
            else:
                up.append(ret.iloc[i])
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

    def bollinger_band_data(self, window = 20):
        sma = self.priceData['Close'].rolling(window=window).mean()
        std = self.priceData['Close'].rolling(window=window).std()
        upper_bb = sma + (std * 2)
        lower_bb = sma - (std * 2)
        self.priceData["BB_UPPER_BAND"] = upper_bb
        self.priceData["BB_LOWER_BAND"] = lower_bb
        return (upper_bb, lower_bb)
    
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
        if mode.name == Mode.INTRADAY.name:
            candle_stick = self.priceData.ta.cdl_pattern(name= ["3whitesoldiers","3blackcrows"])
            self.compute_triple_increase_decrease()
            self.compute_marubasu_candle_stick()
            self.compute_double_increase_decrease()
        else:
            self.compute_triple_increase_decrease()
            self.compute_marubasu_candle_stick()
            self.compute_double_increase_decrease()
            candle_stick = self.priceData.ta.cdl_pattern(name= ["3whitesoldiers","3blackcrows", "harami", "engulfing"])
            
        self.priceData = pd.concat([self.priceData , candle_stick], axis= 1)

    def compute_triple_increase_decrease(self):
        length = self.priceData.shape[0]

        series = pd.Series(index = self.priceData.index)
        for index in range(2,length):
            curr_price = self.priceData.iloc[index]
            prev_price = self.priceData.iloc[index-1]
            prev_minus_one_price = self.priceData.iloc[index-2]

            if prev_minus_one_price["Open"] < prev_minus_one_price["Close"]\
                and prev_price["Open"] < prev_price["Close"]\
                    and curr_price["Open"] < curr_price["Close"]\
                    and curr_price["Close"] > prev_price["Close"] \
                            and prev_price["Close"] > prev_minus_one_price["Close"]:
                series.iloc[index] = ((curr_price["Close"] - prev_minus_one_price["Open"])/prev_minus_one_price["Open"]) * 100
            elif prev_minus_one_price["Open"] > prev_minus_one_price["Close"]\
                and prev_price["Open"] > prev_price["Close"]\
                    and curr_price["Open"] > curr_price["Close"]\
                        and curr_price["Close"] < prev_price["Close"] \
                        and prev_price["Close"] < prev_minus_one_price["Close"]:
                series.iloc[index] =  ((curr_price["Close"] - prev_minus_one_price["Open"])/prev_minus_one_price["Open"]) * 100
            else:
                series.iloc[index] = 0.0
        self.priceData["3_CONT_INC_OR_DEC"] = series
    
    def compute_double_increase_decrease(self):
        length = self.priceData.shape[0]

        series = pd.Series(index = self.priceData.index)
        for index in range(2,length):
            curr_price = self.priceData.iloc[index]
            prev_price = self.priceData.iloc[index-1]

            if prev_price["Open"] < prev_price["Close"]\
                    and curr_price["Open"] < curr_price["Close"]\
                    and curr_price["Close"] > prev_price["Close"]:
                series.iloc[index] = ((curr_price["Close"] - prev_price["Open"])/prev_price["Open"]) * 100
            elif prev_price["Open"] > prev_price["Close"]\
                    and curr_price["Open"] > curr_price["Close"]\
                        and curr_price["Close"] < prev_price["Close"] :
                series.iloc[index] =  ((curr_price["Close"] - prev_price["Open"])/prev_price["Open"]) * 100
            else:
                series.iloc[index] = 0.0
        self.priceData["2_CONT_INC_OR_DEC"] = series
    
    def compute_marubasu_candle_stick(self, candleWickPercentage = 0.2):
        length = self.priceData.shape[0]

        series = pd.Series(index = self.priceData.index)
        for index in range(0,length):
            closePrice = self.priceData.iloc[index]['Close']
            openPrice = self.priceData.iloc[index]['Open']
            highPrice = self.priceData.iloc[index]['High']
            lowPrice = self.priceData.iloc[index]['Low']

            if (((openPrice == lowPrice) or (percentageChange(openPrice, lowPrice) <= candleWickPercentage)) \
                and ((highPrice == closePrice) or (percentageChange(highPrice, closePrice) <= candleWickPercentage))):
                series.iloc[index] = percentageChange(closePrice, openPrice)
            elif (((openPrice == highPrice) or (percentageChange(highPrice,openPrice) <= candleWickPercentage)) \
            and ((lowPrice == closePrice) or (percentageChange(closePrice,lowPrice) <= candleWickPercentage))):
                series.iloc[index] = percentageChange(closePrice, openPrice)
            else:
                series.iloc[index] = 0.0
        self.priceData["MARUBASU"] = series


    def removeStockData(self):
        self.priceData = None
        self.ivData = None
    
    def is_price_data_empty(self):
        return self.priceData.empty

    def __repr__(self):
        return """Stock Name : {}
                Analysis Results : {} """.format(self.stockName,self.analysisResult)