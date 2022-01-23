from Analyser import Analyser
from math import log



class TechnicalAnalyser(Analyser):
    def __init__(self , rsi_upper_limit = 75, rsi_lower_limit = 25):
        super().__init__()
        self.analyserName = "Technical Analyser"
        self.rsi_upper_limit = rsi_upper_limit
        self.rsi_lower_limit = rsi_lower_limit
        self.rsi_crossover_lookback = 5
        self.analysisMethods = (
        {"method": self.rsiIndicator, "imputData": self.InputData.PRICE_DATA},
        {"method": self.rsiCrossoverIndicator, "imputData": self.InputData.PRICE_DATA},
        {"method": self.MACDIndicator, "imputData": self.InputData.PRICE_DATA},
        {"method": self.BollingerBandIndicator, "imputData": self.InputData.PRICE_DATA},
        {"method": self.price_52_week, "imputData": self.InputData.PRICE_DATA},
        {"method": self.daily_return, "imputData": self.InputData.PRICE_DATA})

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
                elif (previousRSIData.iloc[i]) <= self.rsi_lower_limit:
                    return (True, self.Trend.Bullish, "Rsi made crossover from oversold , RSI is {}".format(currentRSI))
        return (False, None, None)

    def MACDIndicator(self,priceData):

        k = priceData['Close'].ewm(span=12, adjust=False, min_periods=12).mean()
        d = priceData['Close'].ewm(span=26, adjust=False, min_periods=26).mean()

        macd = k - d
        macd_s = macd.ewm(span=9, adjust=False, min_periods=9).mean()

        if (macd[-2] < macd_s[-2]) and (macd[-1] > macd_s[-1]):
            return (True, self.Trend.Bullish, "MACD Bullish crossover ")
        elif (macd[-2] > macd_s[-2]) and (macd[-1] < macd_s[-1]):
            return (True, self.Trend.Bearish, "MACD Bearish crossover ")
        return (False, None, None)

    def BollingerBandIndicator(self, priceData):

        if priceData['Close'].iloc[-2] > priceData['lower_bb'].iloc[-2] and priceData['Close'].iloc[-1] < priceData['lower_bb'].iloc[-1]:
            return (True, self.Trend.Bullish, "Bollinger Band Bullish indicator")
        elif priceData['Close'].iloc[-2] < priceData['upper_bb'].iloc[-2] and priceData['Close'].iloc[-1] > priceData['upper_bb'].iloc[-1]:
            return (True, self.Trend.Bearish, "Bollinger Band Bearish indicator")
        return (False, None, None)

    def price_52_week(self,priceData, percent = 2):

        trailing_year_data = priceData['Close'].tail(252)
        currentValue = trailing_year_data[-1]
        min_value = trailing_year_data.min()
        max_value = trailing_year_data.max()
        max_percent = ((max_value - currentValue) / currentValue) * 100
        min_percent = ((currentValue - min_value) / currentValue) * 100

        if (currentValue == max_value):
            return (True, self.Trend.Neutral, "Price is 52 week high")
        elif (max_percent <= percent):
            return (True, self.Trend.Neutral, "Price is {}% close to 52 week high".format(max_percent))
        elif (currentValue == min_value):
            return (True, self.Trend.Neutral, "Price is 52 week low")
        elif (min_percent <= percent):
            return (True, self.Trend.Neutral, "Price is {}% close to 52 week low".format(min_percent))
        return (False, None, None)

    def daily_return(self,priceData, max_daily_return = 5 ):

        currentPrice = priceData['Close'].iloc[-1]
        previousPrice = priceData['Close'].iloc[-2]

        daily_return = log(currentPrice/previousPrice)*100
        if (daily_return <= -max_daily_return) or (daily_return >= max_daily_return):
            return (True, self.Trend.Neutral, "Daily return is {}%".format(daily_return))
        return (False, None, None)
