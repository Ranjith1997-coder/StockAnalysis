import yfinance as yf
from optionOpstraCollection import getIVChartData



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

    def getStockData(self):
        self.priceData = yf.download(self.stockSymbolYFinance, period="3y")
        self.ivData = getIVChartData(self.stockSymbolOpestra)[1]

    def removeStockData(self):
        self.priceData = None
        self.ivData = None

    def __repr__(self):
        return """Stock Name : {}
Analysis Results : {} """.format(self.stockName,self.analysisResult)