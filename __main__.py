from datetime import date
from optionOpstraCollection import get_FII_DII_Data
from Stock import Stock
from IVAnalyser import IVAnalyser
from VolumeAnalyser import VolumeAnalyser
from TechnicalAnalyser import TechnicalAnalyser
from candleStickPatternAnalyser import CandleStickAnalyser
import constants
from json import dumps



analyserList = [IVAnalyser(),VolumeAnalyser(),TechnicalAnalyser(), CandleStickAnalyser()]
# analyserList = [CandleStickAnalyser(),]

def analyse(stock):

    for analyser in analyserList:
        analyser.runAnalysis(stock)

def storeResultsInJson(tickerList):

    jsonDict = {"FII_DII_DATA": get_FII_DII_Data()[-1],"No_of_stocks": 0, "stockResults": []}

    count = 0
    for ticker in tickerList:
        # if ((not ticker.analysisResult['Bullish']) and (not ticker.analysisResult['Bearish']) and (not ticker.analysisResult['Neutral'])):
        if ticker.indicator_count != 0 :
            jsonDict["stockResults"].append({
                "StockName" : ticker.stockName,
                "StockSymbol":ticker.stockSymbolOpestra,
                "No_of_Indicators": ticker.indicator_count,
                "AnalysisResults" : ticker.analysisResult
            })
            count+=1

    jsonDict["No_of_stocks"] = count

    json_object = dumps(jsonDict, indent=4)

    fileName = "stockAnalysis_{}.json".format(str(date.today()))
    with open(fileName, "w") as outfile:
        outfile.write(json_object)




def printResult(tickerList):

    for ticker in tickerList:
        if ticker.indicator_count != 0:
            print(ticker)
            print("********************************************************")


if __name__ == '__main__':

    tickerList = []

    for index in constants.indexSymbolForNSE:
        ticker = Stock(index, constants.indexSymbolForYfinance[index], constants.indexSymbolForNSE[index])
        try:
            ticker.getStockData()
        except Exception:
            print("Cannot Retrive data for {}".format(ticker.stockName))
            continue
        analyse(ticker)
        ticker.removeStockData()
        tickerList.append(ticker)

    for stock in constants.stocks:
        ticker = Stock(stock,constants.stocks[stock]+".NS",constants.stocks[stock])
        try:
            ticker.getStockData()
        except Exception:
            print("Cannot Retrive data for {}".format(ticker.stockName))
            continue
        analyse(ticker)
        ticker.removeStockData()
        tickerList.append(ticker)

    # printResult(tickerList)
    storeResultsInJson(tickerList)

