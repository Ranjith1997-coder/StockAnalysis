import yfinance as yf
from optionOpstraCollection import getIVChartData
import pandas as pd


class Stock:
    def __init__(self, stockName, stockSymbolYFinance, stockSymbolOpestra):
        self.stockName = stockName
        self.stockSymbolYFinance = stockSymbolYFinance
        self.stockSymbolOpestra = stockSymbolOpestra
        self.analysisResult = {"Bullish" :[],
                               "Bearish" :[],
                               "Neutral" :[],
                               "NoResult":[]}
        self.priceData = None
        self.ivData = None
        self.rsi_Df = None

    def getStockData(self):
        try :
            self.priceData = yf.download(self.stockSymbolYFinance, period="3y")
            self.ivData = getIVChartData(self.stockSymbolOpestra)[1]
            self.priceData['rsi'] = self.compute_rsi(self.priceData['Close'])
            self.priceData = self.priceData.dropna()
        except Exception:
            raise Exception()

    def compute_rsi(self, closeSeries, rsi_lookback = 14):
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
        return rsi_df[3:]

    def removeStockData(self):
        self.priceData = None
        self.ivData = None

    def __repr__(self):
        return """Stock Name : {}
Analysis Results : {} """.format(self.stockName,self.analysisResult)