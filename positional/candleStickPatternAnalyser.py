from positional.Analyser import Analyser
from common.helperFunctions import percentageChange



class CandleStickAnalyser(Analyser):
    def __init__(self, percentage_change = 100, avgLookbackDays = 30, percent_above_avg = 100):
        super().__init__()
        self.analyserName = "Candle Stick Pattern Analyser"
        self.singleMethod = True
        self.analysisMethods = (
        {"method": self.singleCandleStickPattern, "imputData": self.InputData.PRICE_DATA},
        {"method": self.doubleCandleStickPattern, "imputData": self.InputData.PRICE_DATA})


    def singleCandleStickPattern(self, priceData, priceChangePercentage = 1.5 , candleWickPercentage = 0.2):
        closePrice = priceData['Close'].iloc[-1]
        openPrice = priceData['Open'].iloc[-1]
        highPrice = priceData['High'].iloc[-1]
        lowPrice = priceData['Low'].iloc[-1]

        if (((openPrice == lowPrice) or (percentageChange(openPrice, lowPrice) <= candleWickPercentage)) \
                and ((highPrice == closePrice) or (percentageChange(highPrice, closePrice) <= candleWickPercentage)) \
                and (percentageChange(closePrice, openPrice) >= priceChangePercentage)):
            return (True, self.Trend.Bullish, "Bullish Marubasu , with return {}".format((percentageChange(closePrice,openPrice))))
        elif (((openPrice == highPrice) or (percentageChange(highPrice,openPrice) <= candleWickPercentage)) \
            and ((lowPrice == closePrice) or (percentageChange(closePrice,lowPrice) <= candleWickPercentage)) \
            and (abs(percentageChange(closePrice,openPrice)) >= priceChangePercentage)):
            return (True, self.Trend.Bearish,
                    "Bearish Marubasu , with return {}".format((percentageChange(closePrice, openPrice))))
        elif ((openPrice < closePrice) and ((closePrice == highPrice) or (percentageChange(highPrice, closePrice) <= candleWickPercentage)) and \
                (openPrice > lowPrice) and (abs(percentageChange(lowPrice,openPrice)) >= 2 * percentageChange(closePrice, openPrice))):
            return (True, self.Trend.Bullish,
                    "Bullish Hammer , with return {}".format((percentageChange(closePrice, openPrice))))
        elif ((openPrice > closePrice) and ((closePrice == lowPrice) or (percentageChange(closePrice, lowPrice) <= candleWickPercentage)) and \
                (openPrice < highPrice) and (percentageChange(highPrice,openPrice)) >= 2 * abs(percentageChange(closePrice, openPrice))):
            return (True, self.Trend.Bearish,
                    "Bearish shooting star , with return {}".format((percentageChange(closePrice, openPrice))))
        return (False, None, None)

    def doubleCandleStickPattern(self, priceData):
        closePrice = priceData['Close'].iloc[-1]
        openPrice = priceData['Open'].iloc[-1]
        highPrice = priceData['High'].iloc[-1]
        lowPrice = priceData['Low'].iloc[-1]

        prevClosePrice = priceData['Close'].iloc[-2]
        prevOpenPrice = priceData['Open'].iloc[-2]
        prevHighPrice = priceData['High'].iloc[-2]
        prevLowPrice = priceData['Low'].iloc[-2]

        if (prevClosePrice < prevOpenPrice) and (closePrice > openPrice) and (openPrice > prevClosePrice) and (closePrice < prevOpenPrice):
            return (True, self.Trend.Bullish,
                    "Bullish harami , with return {}".format((percentageChange(closePrice, openPrice))))
        elif (prevClosePrice > prevOpenPrice) and (closePrice < openPrice) and (openPrice < prevClosePrice) and (closePrice > prevOpenPrice):
            return (True, self.Trend.Bullish,
                    "Bearish harami , with return {}".format((percentageChange(closePrice, openPrice))))
        return (False, None, None)

    def tripleCandleStickPattern(self, priceData, priceChangePercentage=1.5, candleWickPercentage=0.2):
        pass



