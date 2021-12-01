from Analyser import Analyser



class TechnicalAnalyser(Analyser):
    def __init__(self , rsi_upper_limit = 75, rsi_lower_limit = 25):
        super().__init__()
        self.analyserName = "Technical Analyser"
        self.rsi_upper_limit = rsi_upper_limit
        self.rsi_lower_limit = rsi_lower_limit
        self.rsi_crossover_lookback = 5
        self.analysisMethods = (
        {"method": self.rsiIndicator, "imputData": self.InputData.PRICE_DATA},
        {"method": self.rsiCrossoverIndicator, "imputData": self.InputData.PRICE_DATA})

    def rsiIndicator(self, priceData):
        currentRSI = priceData['rsi'].iloc[-1]
        if (currentRSI > self.rsi_upper_limit):
            return (True,self.Trend.Bearish, "Stock is overbought , RSI is {}".format(currentRSI))
        elif (currentRSI < self.rsi_lower_limit):
            return (True, self.Trend.Bullish, "Stock is oversold , RSI is {}".format(currentRSI))
        return (False,None,None)

    def rsiCrossoverIndicator(self, priceData):
        currentRSI = priceData['rsi'].iloc[-1]
        previousRSIData = priceData['rsi'].tail(self.rsi_crossover_lookback + 1)
        if (currentRSI < self.rsi_upper_limit) and (currentRSI > self.rsi_lower_limit):
            for i in range(len(previousRSIData)):
                if (previousRSIData.iloc[i]) >= self.rsi_upper_limit:
                    return (True, self.Trend.Bearish, "Rsi made crossover from overbought , RSI is {}".format(currentRSI))
                if (previousRSIData.iloc[i]) <= self.rsi_lower_limit:
                    return (True, self.Trend.Bullish, "Rsi made crossover from oversold , RSI is {}".format(currentRSI))
        return (False, None, None)