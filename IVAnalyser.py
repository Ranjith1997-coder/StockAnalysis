from Analyser import Analyser


class IVAnalyser(Analyser):
    def __init__(self, percentage_change = 30, highIVPercentile = 90, lowIVPercentile = 10):
        super().__init__()
        self.analyserName = "IV Analyser"
        self.percentage_change = percentage_change
        self.highIVPercentile = highIVPercentile
        self.lowIVPercentile = lowIVPercentile

        self.analysisMethods = (
        {"method": self.IVspike, "imputData": self.InputData.IV_DATA},
        {"method": self.highIVPercentileStocks, "imputData": self.InputData.IV_DATA},
        {"method": self.lowIVPercentileStocks, "imputData": self.InputData.IV_DATA})


    def IVspike(self, ivData):
        ivChange = (((ivData['ImpVol'].iloc[-1] - ivData['ImpVol'].iloc[-2]))/ ivData['ImpVol'].iloc[-2]) * 100
        if (ivChange > self.percentage_change):
            return (True, self.Trend.Neutral, "IV increased by {:.2f}%".format(ivChange))
        return (False,None,None)

    def highIVPercentileStocks(self,ivData):
        ivPercentile = ivData['IVP'].iloc[-1]

        if (ivPercentile != None) and (ivPercentile >= self.highIVPercentile):
            return (True, self.Trend.Neutral, "IV percentile {} lesser than {}%".format(ivPercentile,self.highIVPercentile))
        return (False,None,None)

    def lowIVPercentileStocks(self,ivData):
        ivPercentile = ivData['IVP'].iloc[-1]

        if (ivPercentile != None) and (ivPercentile <= self.lowIVPercentile):
            return (True, self.Trend.Neutral, "IV percentile {} lesser than {}%".format(ivPercentile,self.lowIVPercentile))
        return (False,None,None)


