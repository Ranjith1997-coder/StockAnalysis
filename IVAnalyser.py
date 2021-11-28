from Analyser import Analyser


class IVAnalyser(Analyser):
    def __init__(self, percentage_change = 5, highIVPercentile = 90, lowIVPercentile = 10):
        super().__init__()
        self.analyserName = "IV Analyser"
        self.percentage_change = percentage_change
        self.highIVPercentile = highIVPercentile
        self.lowIVPercentile = lowIVPercentile

        self.analysisMethods = (
        {"method": self.IVspike, "imputData": self.InputData.IV_DATA, "trend": self.Trend.Neutral,
         "comments": "IV increased by {}%".format(self.percentage_change)},
        {"method": self.highIVPercentileStocks, "imputData": self.InputData.IV_DATA, "trend": self.Trend.Neutral,
         "comments": "IV percentile greater than {}%".format(self.highIVPercentile)},
        {"method": self.lowIVPercentileStocks, "imputData": self.InputData.IV_DATA, "trend": self.Trend.Neutral,
         "comments": "IV percentile lesser than {}%".format(self.lowIVPercentile)})


    def IVspike(self, ivData):
        ivChange = (((ivData['ImpVol'].iloc[-1] - ivData['ImpVol'].iloc[-2]))/ ivData['ImpVol'].iloc[-2]) * 100
        if (ivChange > self.percentage_change):
            return True
        return False

    def highIVPercentileStocks(self,ivData):
        ivPercentile = ivData['IVP'].iloc[-1]

        if (ivPercentile != None) and (ivPercentile >= self.highIVPercentile):
            return True
        return False

    def lowIVPercentileStocks(self,ivData):
        ivPercentile = ivData['IVP'].iloc[-1]

        if (ivPercentile != None) and (ivPercentile <= self.lowIVPercentile):
            return True
        return False


