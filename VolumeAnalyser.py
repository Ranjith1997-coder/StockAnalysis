from Analyser import Analyser


class VolumeAnalyser(Analyser):
    def __init__(self, percentage_change = 100, avgLookbackDays = 30, percent_above_avg = 100):
        super().__init__()
        self.analyserName = "Volume Analyser"
        self.percentage_change = percentage_change
        self.avgLookbackDays = avgLookbackDays
        self.percent_above_avg = percent_above_avg
        self.analysisMethods = (
        {"method": self.volumeSpike, "imputData": self.InputData.PRICE_DATA},
        {"method": self.volumeAboveAvgVolume, "imputData": self.InputData.PRICE_DATA})


    def volumeSpike(self, priceData):
        ivChange = (((priceData['Volume'].iloc[-1] - priceData['Volume'].iloc[-2]))/ priceData['Volume'].iloc[-2]) * 100
        if (ivChange > self.percentage_change):
            return (True, self.Trend.Neutral, "Volume increased by {:.2f}%".format(ivChange))
        return (False,None,None)

    def volumeAboveAvgVolume(self, priceData):
        avgVolume =priceData.tail(self.avgLookbackDays)['Volume'].mean()
        avgIncreasePercentage = ((priceData['Volume'].iloc[-1] / avgVolume) - 1) *100
        # print(avgVolume, priceData['Volume'].iloc[-1] , avgIncreasePercentage)
        if (avgIncreasePercentage >= self.percent_above_avg):
            return (True, self.Trend.Neutral, "Volume is {:.2f}% more than Average volume".format(avgIncreasePercentage))
        return (False,None,None)

