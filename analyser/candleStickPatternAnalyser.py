import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
from common.helperFunctions import percentageChange
import common.shared as shared

class CandleStickAnalyser(BaseAnalyzer):
    THREE_CONT_INC_OR_DEC_THRESHOLD = 0
    TWO_CONT_INC_OR_DEC_THRESHOLD = 0
    MARUBASU_THRESHOLD = 0
    WICK_PERCENTAGE = 0.2
    HAMMER_BODY_RATIO = 0.35      # max body / total range for hammer / shooting star
    HAMMER_WICK_MULTIPLIER = 2.0  # min long wick / body size
    STAR_MAX_BODY_RATIO = 0.3     # max star body / first candle body

    def __init__(self):
        super().__init__()
        self.analyserName = "Candle Stick Pattern Analyser"

    def reset_constants(self, is_index = False):
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
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
        logger.debug(f"CandleStickAnalyser constants reset for mode {shared.app_ctx.mode.name}")
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
            # Hammer / Shooting Star detection (ratio-based, works for both candle colours)
            total_range = highPrice - lowPrice
            if total_range > 0:
                body = abs(closePrice - openPrice)
                body_ratio = body / total_range
                lower_wick = min(openPrice, closePrice) - lowPrice
                upper_wick = highPrice - max(openPrice, closePrice)

                # Hammer (Bullish reversal): small body near top, long lower shadow, tiny upper shadow
                if (body > 0 and body_ratio <= self.HAMMER_BODY_RATIO and
                        lower_wick >= self.HAMMER_WICK_MULTIPLIER * body and
                        upper_wick <= total_range * 0.1):
                    stock.set_analysis("BULLISH", "Single_candle_stick_pattern",
                                       f"Hammer, range: {percentageChange(highPrice, lowPrice):.2f}%")
                    return True

                # Shooting Star (Bearish reversal): small body near bottom, long upper shadow, tiny lower shadow
                elif (body > 0 and body_ratio <= self.HAMMER_BODY_RATIO and
                      upper_wick >= self.HAMMER_WICK_MULTIPLIER * body and
                      lower_wick <= total_range * 0.1):
                    stock.set_analysis("BEARISH", "Single_candle_stick_pattern",
                                       f"Shooting Star, range: {percentageChange(highPrice, lowPrice):.2f}%")
                    return True

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

            prev_mid = (prevOpenPrice + prevClosePrice) / 2

            # Bullish Engulfing: bearish prev → larger bullish curr that fully engulfs prev body
            if (prevClosePrice < prevOpenPrice and closePrice > openPrice and
                    openPrice <= prevClosePrice and closePrice >= prevOpenPrice):
                stock.set_analysis("BULLISH", "Double_candle_stick_pattern",
                                   f"Bullish Engulfing, rate: {percentageChange(closePrice, openPrice):.2f}%")
                return True

            # Bearish Engulfing: bullish prev → larger bearish curr that fully engulfs prev body
            elif (prevClosePrice > prevOpenPrice and closePrice < openPrice and
                  openPrice >= prevClosePrice and closePrice <= prevOpenPrice):
                stock.set_analysis("BEARISH", "Double_candle_stick_pattern",
                                   f"Bearish Engulfing, rate: {percentageChange(closePrice, openPrice):.2f}%")
                return True

            # Piercing Line: bearish prev → bullish curr opens ≤ prev close, closes above prev body midpoint
            elif (prevClosePrice < prevOpenPrice and closePrice > openPrice and
                  openPrice <= prevClosePrice and closePrice > prev_mid and
                  closePrice < prevOpenPrice):
                stock.set_analysis("BULLISH", "Double_candle_stick_pattern",
                                   f"Piercing Line, close: {closePrice:.2f} > mid: {prev_mid:.2f}")
                return True

            # Dark Cloud Cover: bullish prev → bearish curr opens ≥ prev close, closes below prev body midpoint
            elif (prevClosePrice > prevOpenPrice and closePrice < openPrice and
                  openPrice >= prevClosePrice and closePrice < prev_mid and
                  closePrice > prevOpenPrice):
                stock.set_analysis("BEARISH", "Double_candle_stick_pattern",
                                   f"Dark Cloud Cover, close: {closePrice:.2f} < mid: {prev_mid:.2f}")
                return True

            # 2 Continuous Increase
            elif (prevOpenPrice < prevClosePrice) and (openPrice < closePrice) and (closePrice > prevClosePrice) and (percentageChange(closePrice, prevOpenPrice) >= CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD):
                stock.set_analysis("BULLISH", "Double_candle_stick_pattern", "2_cont_inc, rate:{:.2f}%".format(percentageChange(closePrice, prevOpenPrice)))
                return True

            # 2 Continuous Decrease
            elif (prevOpenPrice > prevClosePrice) and (openPrice > closePrice) and (closePrice < prevClosePrice) and (abs(percentageChange(closePrice, prevOpenPrice)) >= CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD):
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

            first_body = abs(prevPrevClosePrice - prevPrevOpenPrice)
            star_body = abs(prevClosePrice - prevOpenPrice)
            pp_mid = (prevPrevOpenPrice + prevPrevClosePrice) / 2

            # Morning Star (Bullish reversal): large bearish → small star → bullish closing above 1st midpoint
            if (prevPrevClosePrice < prevPrevOpenPrice and
                    first_body > 0 and star_body <= self.STAR_MAX_BODY_RATIO * first_body and
                    closePrice > openPrice and closePrice > pp_mid):
                stock.set_analysis("BULLISH", "Triple_candle_stick_pattern",
                                   f"Morning Star, close: {closePrice:.2f} > mid: {pp_mid:.2f}")
                return True

            # Evening Star (Bearish reversal): large bullish → small star → bearish closing below 1st midpoint
            elif (prevPrevClosePrice > prevPrevOpenPrice and
                  first_body > 0 and star_body <= self.STAR_MAX_BODY_RATIO * first_body and
                  closePrice < openPrice and closePrice < pp_mid):
                stock.set_analysis("BEARISH", "Triple_candle_stick_pattern",
                                   f"Evening Star, close: {closePrice:.2f} < mid: {pp_mid:.2f}")
                return True

            # 3 Continuous Increase
            elif (prevPrevOpenPrice < prevPrevClosePrice) and (prevOpenPrice < prevClosePrice) and (openPrice < closePrice) \
                and (closePrice > prevClosePrice) and (prevClosePrice > prevPrevClosePrice) and \
                (percentageChange(closePrice, prevPrevOpenPrice) >= CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD):
                stock.set_analysis("BULLISH", "Triple_candle_stick_pattern", "3_cont_inc, rate:{:.2f}%".format(percentageChange(closePrice, prevPrevOpenPrice)))
                return True

            # 3 Continuous Decrease
            elif (prevPrevOpenPrice > prevPrevClosePrice) and (prevOpenPrice > prevClosePrice) and (openPrice > closePrice) \
                and (closePrice < prevClosePrice) and (prevClosePrice < prevPrevClosePrice) and \
                (abs(percentageChange(closePrice, prevPrevOpenPrice)) >= CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD):
                stock.set_analysis("BEARISH", "Triple_candle_stick_pattern", "3_cont_dec, rate:{:.2f}%".format(percentageChange(closePrice, prevPrevOpenPrice)))
                return True

            return False
        except Exception as e:
            logger.error(f"Error in tripleCandleStickPattern for stock {stock.stock_symbol}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
        


