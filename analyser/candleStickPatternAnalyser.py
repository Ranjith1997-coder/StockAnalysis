import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
import common.constants as constant
from common.logging_util import logger
from common.helperFunctions import percentageChange
from collections import namedtuple

class CandleStickAnalyser(BaseAnalyzer):
    THREE_CONT_INC_OR_DEC_THRESHOLD = 0
    TWO_CONT_INC_OR_DEC_THRESHOLD = 0
    MARUBASU_THRESHOLD = 0
    WICK_PERCENTAGE = 0.2

    def __init__(self):
        super().__init__()
        self.analyserName = "Candle Stick Pattern Analyser"

    def reset_constants(self, is_index = False):
        if constant.mode.name == constant.Mode.INTRADAY.name:
            if is_index:
                CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD = 1  
                CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD = 0.75    
                CandleStickAnalyser.MARUBASU_THRESHOLD = 0.5
            else:
                CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD = 1.5  
                CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD = 1    
                CandleStickAnalyser.MARUBASU_THRESHOLD = 1.5
        else:
            if is_index:
                CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD = 2.5  
                CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD = 2   
                CandleStickAnalyser.MARUBASU_THRESHOLD = 1.5
            else:  
                CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD = 5  
                CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD = 4   
                CandleStickAnalyser.MARUBASU_THRESHOLD = 3 
             #add something later on this
        logger.debug(f"CandleStickAnalyser constants reset for mode {constant.mode.name}")
        logger.debug(f"THREE_CONT_INC_OR_DEC_THRESHOLD = {CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD} , TWO_CONT_INC_OR_DEC_THRESHOLD = {CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD} " )
        logger.debug(f"MARUBASU_THRESHOLD = {CandleStickAnalyser.MARUBASU_THRESHOLD} , WICK_PERCENTAGE = {CandleStickAnalyser.WICK_PERCENTAGE}")
    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def singleCandleStickPattern(self, stock: Stock):   
        try:
            logger.debug(f'Inside singleCandleStickPattern for stock {stock.stock_symbol}')
            currData = stock.current_equity_data
            closePrice = currData['Close']
            openPrice = currData['Open']
            highPrice = currData['High']
            lowPrice = currData['Low']

            if (((openPrice == lowPrice) or (percentageChange(openPrice, lowPrice) <= CandleStickAnalyser.WICK_PERCENTAGE)) \
                    and ((highPrice == closePrice) or (percentageChange(highPrice, closePrice) <= CandleStickAnalyser.WICK_PERCENTAGE)) \
                    and (percentageChange(closePrice, openPrice) >= CandleStickAnalyser.MARUBASU_THRESHOLD)):
                stock.set_analysis("BULLISH", "Single_candle_stick_pattern", "Marubasu, rate: {:.2f}%".format(percentageChange(closePrice, openPrice)))
                return True
            elif (((openPrice == highPrice) or (percentageChange(highPrice,openPrice) <= CandleStickAnalyser.WICK_PERCENTAGE)) \
                and ((lowPrice == closePrice) or (percentageChange(closePrice,lowPrice) <= CandleStickAnalyser.WICK_PERCENTAGE)) \
                and (abs(percentageChange(closePrice,openPrice)) >= CandleStickAnalyser.MARUBASU_THRESHOLD)):
                stock.set_analysis("BEARISH", "Single_candle_stick_pattern", "Marubasu, rate: {:.2f}%".format(percentageChange(closePrice, openPrice)))
                return True
            # elif ((openPrice < closePrice) and ((closePrice == highPrice) or (percentageChange(highPrice, closePrice) <= CandleStickAnalyser.WICK_PERCENTAGE)) and \
            #         (openPrice > lowPrice) and (abs(percentageChange(lowPrice,openPrice)) >= 2 * percentageChange(closePrice, openPrice))):
            #     stock.set_analysis("BULLISH", "Single_candle_stick_pattern", "Hammer")
            #     return True
            # elif ((openPrice > closePrice) and ((closePrice == lowPrice) or (percentageChange(closePrice, lowPrice) <= CandleStickAnalyser.WICK_PERCENTAGE)) and \
            #         (openPrice < highPrice) and (percentageChange(highPrice,openPrice)) >= 2 * abs(percentageChange(closePrice, openPrice))):
            #     stock.set_analysis("BEARISH", "Single_candle_stick_pattern", "shooting star")
            return False
        except Exception as e:
            logger.error(f"Error in singleCandleStickPattern for stock {stock.stock_symbol}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False


    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def doubleCandleStickPattern(self, stock: Stock):
        try:
            logger.debug(f'Inside singleCandleStickPattern for stock {stock.stock_symbol}')
            currData = stock.current_equity_data
            prevData = stock.previous_equity_data
            
            closePrice = currData['Close']
            openPrice = currData['Open']
            highPrice = currData['High']
            lowPrice = currData['Low']

            prevClosePrice = prevData['Close']
            prevOpenPrice = prevData['Open']
            prevHighPrice = prevData['High']
            prevLowPrice = prevData['Low']

            # if (prevClosePrice < prevOpenPrice) and (closePrice > openPrice) and (openPrice > prevClosePrice) and (closePrice < prevOpenPrice):
            #     stock.set_analysis("BULLISH", "Double_candle_stick_pattern", "Harami")
            #     return True
            # elif (prevClosePrice > prevOpenPrice) and (closePrice < openPrice) and (openPrice < prevClosePrice) and (closePrice > prevOpenPrice):
            #     stock.set_analysis("BEARISH", "Double_candle_stick_pattern", "Harami")
            #     return True
            if (prevOpenPrice < prevClosePrice) and (openPrice < closePrice) and (closePrice > prevClosePrice ) and (percentageChange(closePrice, prevOpenPrice) >= CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD):
                stock.set_analysis("BULLISH", "Double_candle_stick_pattern", "2_cont_inc, rate:{:.2f}%".format(percentageChange(closePrice, prevOpenPrice)))
                return True
            elif (prevOpenPrice > prevClosePrice) and (openPrice > closePrice) and (closePrice < prevClosePrice ) and (abs(percentageChange(closePrice, prevOpenPrice)) >= CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD):
                stock.set_analysis("BEARISH", "Double_candle_stick_pattern", "2_cont_dec, rate:{:.2f}%".format(percentageChange(closePrice, prevOpenPrice)))
                return True
            return False
        except Exception as e:
            logger.error(f"Error in doubleCandleStickPattern for stock {stock.stock_symbol}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def tripleCandleStickPattern(self, stock: Stock):
        try:
            logger.debug(f'Inside tripleCandleStickPattern for stock {stock.stock_symbol}')
            currData = stock.current_equity_data
            prevData = stock.previous_equity_data
            prevPrevData = stock.previous_previous_equity_data
            
            closePrice = currData['Close']
            openPrice = currData['Open']
            highPrice = currData['High']
            lowPrice = currData['Low']

            prevClosePrice = prevData['Close']
            prevOpenPrice = prevData['Open']
            prevHighPrice = prevData['High']
            prevLowPrice = prevData['Low']

            prevPrevClosePrice = prevPrevData['Close']
            prevPrevOpenPrice = prevPrevData['Open']
            prevPrevHighPrice = prevPrevData['High']
            prevPrevLowPrice = prevPrevData['Low']

            if (prevPrevOpenPrice < prevPrevClosePrice) and (prevOpenPrice < prevClosePrice) and (openPrice < closePrice ) \
                and (closePrice > prevClosePrice) and (prevClosePrice > prevPrevClosePrice) and \
                (percentageChange(closePrice, prevPrevOpenPrice) >= CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD):
                stock.set_analysis("BULLISH", "Triple_candle_stick_pattern", "3_cont_inc, rate:{:.2f}%".format(percentageChange(closePrice, prevPrevOpenPrice)))
                return True
            elif (prevPrevOpenPrice > prevPrevClosePrice) and (prevOpenPrice > prevClosePrice) and (openPrice > closePrice ) \
                and (closePrice < prevClosePrice) and (prevClosePrice < prevPrevClosePrice) and \
                (abs(percentageChange(closePrice, prevPrevOpenPrice)) >= CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD):
                stock.set_analysis("BEARISH", "Triple_candle_stick_pattern", "3_cont_dec, rate:{:.2f}%".format(percentageChange(closePrice, prevPrevOpenPrice)))
                return True
            return False
        except Exception as e:
            logger.error(f"Error in tripleCandleStickPattern for stock {stock.stock_symbol}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
        


