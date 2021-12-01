from enum import Enum

class Analyser:

    class InputData(Enum):
        PRICE_DATA = 0
        IV_DATA = 1

    class Trend(Enum):
        Bullish = "Bullish"
        Bearish = "Bearish"
        Neutral = "Neutral"

    def __init__(self):
        #{"method":methodName, "imputData":InputData, "trend" : Trend, "comments":Comments}
        self.analysisMethods = ()
        self.analyserName = ""
        self.singleMethod = False

    def runAnalysis(self, stockObj):
        result = False
        for method in self.analysisMethods:
            if method["imputData"] == self.InputData.IV_DATA:
                result, trend, comment = method["method"](stockObj.ivData)
            elif method["imputData"] == self.InputData.PRICE_DATA:
                result, trend, comment = method["method"](stockObj.priceData)

            if result:
                stockObj.analysisResult[trend.value].append({"AnalyserName":self.analyserName,
                                                           "comment" : comment})
                if self.singleMethod:
                    return result

        if self.singleMethod and not result:
            stockObj.analysisResult['NoResult'].append({"AnalyserName": self.analyserName,
                                                   "comment": "No pattern found"})
        return result

