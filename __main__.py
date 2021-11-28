from datetime import date

from Stock import Stock
from IVAnalyser import IVAnalyser
import constants
from json import dumps



def analyse(stock):
    iv_analyser = IVAnalyser()

    iv_analyser.runAnalysis(stock)

def storeResultsInJson(tickerList):

    jsonDict = {"No_of_stocks" : 0,
                "stockResults" : []}

    count = 0
    for ticker in tickerList:
        jsonDict["stockResults"].append({
            "StockName" : ticker.stockName,
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
        print(ticker)
        print("********************************************************")


if __name__ == '__main__':

    tickerList = []

    for index in constants.indexSymbolForNSE:
        ticker = Stock(index, constants.indexSymbolForYfinance[index], constants.indexSymbolForNSE[index])
        ticker.getStockData()
        analyse(ticker)
        ticker.removeStockData()
        tickerList.append(ticker)

    for stock in constants.stocks:
        ticker = Stock(stock,constants.stocks[stock]+".NS",constants.stocks[stock])
        ticker.getStockData()
        analyse(ticker)
        ticker.removeStockData()
        tickerList.append(ticker)

    storeResultsInJson(tickerList)

