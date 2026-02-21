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
    
    # Double candlestick pattern thresholds
    ENGULFING_MIN_BODY_RATIO = 1.0    # curr body must be >= this * prev body for engulfing
    PIERCING_MIN_PENETRATION = 0.5    # min penetration above midpoint for piercing line (0.5 = 50%)
    DARK_CLOUD_MIN_PENETRATION = 0.5  # min penetration below midpoint for dark cloud cover
    
    # Trend detection thresholds for reversal patterns
    TREND_LOOKBACK_PERIOD = 5          # number of candles to look back for trend detection
    DOWNTREND_MIN_DECLINE = 1.0        # minimum % decline to confirm downtrend (for bullish reversal)
    UPTREND_MIN_INCREASE = 1.0         # minimum % increase to confirm uptrend (for bearish reversal)
    TREND_CONSISTENCY_RATIO = 0.6      # minimum ratio of bearish/bullish candles in trend

    def __init__(self):
        super().__init__()
        self.analyserName = "Candle Stick Pattern Analyser"

    def reset_constants(self, is_index = False):
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            if is_index:
                CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD = 1  
                CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD = 0.75    
                CandleStickAnalyser.MARUBASU_THRESHOLD = 0.5
                # Keep original intraday wick/hammer parameters
                CandleStickAnalyser.WICK_PERCENTAGE = 0.2
                CandleStickAnalyser.HAMMER_BODY_RATIO = 0.35
                CandleStickAnalyser.HAMMER_WICK_MULTIPLIER = 2.0
                # Double candlestick thresholds for intraday index
                CandleStickAnalyser.ENGULFING_MIN_BODY_RATIO = 1.0
                CandleStickAnalyser.PIERCING_MIN_PENETRATION = 0.5
                CandleStickAnalyser.DARK_CLOUD_MIN_PENETRATION = 0.5
                # Trend detection thresholds for intraday index (smaller moves expected)
                CandleStickAnalyser.TREND_LOOKBACK_PERIOD = 5
                CandleStickAnalyser.DOWNTREND_MIN_DECLINE = 0.5
                CandleStickAnalyser.UPTREND_MIN_INCREASE = 0.5
                CandleStickAnalyser.TREND_CONSISTENCY_RATIO = 0.6
            else:
                CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD = 1.5  
                CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD = 1    
                CandleStickAnalyser.MARUBASU_THRESHOLD = 1.5
                # Keep original intraday wick/hammer parameters
                CandleStickAnalyser.WICK_PERCENTAGE = 0.2
                CandleStickAnalyser.HAMMER_BODY_RATIO = 0.35
                CandleStickAnalyser.HAMMER_WICK_MULTIPLIER = 2.0
                # Double candlestick thresholds for intraday stocks
                CandleStickAnalyser.ENGULFING_MIN_BODY_RATIO = 1.0
                CandleStickAnalyser.PIERCING_MIN_PENETRATION = 0.5
                CandleStickAnalyser.DARK_CLOUD_MIN_PENETRATION = 0.5
                # Trend detection thresholds for intraday stocks
                CandleStickAnalyser.TREND_LOOKBACK_PERIOD = 5
                CandleStickAnalyser.DOWNTREND_MIN_DECLINE = 0.75
                CandleStickAnalyser.UPTREND_MIN_INCREASE = 0.75
                CandleStickAnalyser.TREND_CONSISTENCY_RATIO = 0.6
        else:
            if is_index:
                CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD = 2.5  
                CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD = 2   
                # Positional indices — use optimised single-candle parameters
                CandleStickAnalyser.MARUBASU_THRESHOLD = 1.5
                CandleStickAnalyser.WICK_PERCENTAGE = 0.3
                CandleStickAnalyser.HAMMER_BODY_RATIO = 0.25
                CandleStickAnalyser.HAMMER_WICK_MULTIPLIER = 3.0
                # Double candlestick thresholds for positional index
                CandleStickAnalyser.ENGULFING_MIN_BODY_RATIO = 1.0
                CandleStickAnalyser.PIERCING_MIN_PENETRATION = 0.5
                CandleStickAnalyser.DARK_CLOUD_MIN_PENETRATION = 0.5
                # Trend detection thresholds for positional index
                CandleStickAnalyser.TREND_LOOKBACK_PERIOD = 5
                CandleStickAnalyser.DOWNTREND_MIN_DECLINE = 1.0
                CandleStickAnalyser.UPTREND_MIN_INCREASE = 1.0
                CandleStickAnalyser.TREND_CONSISTENCY_RATIO = 0.6
            else:  
                CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD = 5  
                CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD = 4   
                # Positional stocks — use optimised single-candle parameters
                CandleStickAnalyser.MARUBASU_THRESHOLD = 1.5
                CandleStickAnalyser.WICK_PERCENTAGE = 0.3
                CandleStickAnalyser.HAMMER_BODY_RATIO = 0.25
                CandleStickAnalyser.HAMMER_WICK_MULTIPLIER = 3.0
                # === RELIABLE STRATEGIES - OPTIMIZED PARAMETERS ===
                # Double Candle REVERSAL (test PF 1.06, +₹29.57 expectancy)
                CandleStickAnalyser.ENGULFING_MIN_BODY_RATIO = 1.5
                CandleStickAnalyser.PIERCING_MIN_PENETRATION = 0.3
                CandleStickAnalyser.DARK_CLOUD_MIN_PENETRATION = 0.8
                # Triple Candle REVERSAL (test PF 1.09, +₹42.81 expectancy)
                CandleStickAnalyser.STAR_MAX_BODY_RATIO = 0.15
                # Trend detection for REVERSAL patterns (optimized)
                CandleStickAnalyser.TREND_LOOKBACK_PERIOD = 3
                CandleStickAnalyser.DOWNTREND_MIN_DECLINE = 1.75
                CandleStickAnalyser.UPTREND_MIN_INCREASE = 3.0
                CandleStickAnalyser.TREND_CONSISTENCY_RATIO = 0.6
        logger.debug(f"CandleStickAnalyser constants reset for mode {shared.app_ctx.mode.name}")
        logger.debug(f"THREE_CONT_INC_OR_DEC_THRESHOLD = {CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD} , TWO_CONT_INC_OR_DEC_THRESHOLD = {CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD} " )
        logger.debug(f"MARUBASU_THRESHOLD = {CandleStickAnalyser.MARUBASU_THRESHOLD} , WICK_PERCENTAGE = {CandleStickAnalyser.WICK_PERCENTAGE}")
        logger.debug(f"Trend detection: LOOKBACK={CandleStickAnalyser.TREND_LOOKBACK_PERIOD}, DOWN_THRESHOLD={CandleStickAnalyser.DOWNTREND_MIN_DECLINE}, UP_THRESHOLD={CandleStickAnalyser.UPTREND_MIN_INCREASE}")
    
    def _get_trend_context(self, stock: Stock, lookback: int = None) -> str:
        """
        Determine the trend direction using historical price data.
        
        Returns:
            'UPTREND': Price has been rising - suitable for bearish reversal patterns
            'DOWNTREND': Price has been falling - suitable for bullish reversal patterns
            'SIDEWAYS': No clear trend - reversal patterns less reliable
        """
        try:
            if lookback is None:
                lookback = self.TREND_LOOKBACK_PERIOD
            
            # Get the offset based on mode (intraday uses -2 for current, positional uses -1)
            if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
                end_idx = -3  # Start from before current candle
            else:
                end_idx = -2  # Start from before current candle
            
            start_idx = end_idx - lookback
            
            # Check if we have enough data
            if abs(start_idx) > len(stock.priceData):
                logger.debug(f"Insufficient price data for trend analysis in {stock.stock_symbol}")
                return 'SIDEWAYS'
            
            # Extract closes for trend analysis
            closes = stock.priceData['Close'].iloc[start_idx:end_idx]
            
            if len(closes) < lookback:
                return 'SIDEWAYS'
            
            # Calculate price change over the lookback period
            price_change_pct = percentageChange(closes.iloc[-1], closes.iloc[0])
            
            # Count bearish and bullish candles
            opens = stock.priceData['Open'].iloc[start_idx:end_idx]
            bearish_count = sum(opens.iloc[i] > closes.iloc[i] for i in range(len(closes)))
            bullish_count = sum(opens.iloc[i] < closes.iloc[i] for i in range(len(closes)))
            total_candles = len(closes)
            
            # Determine trend based on price change and candle consistency
            if price_change_pct <= -self.DOWNTREND_MIN_DECLINE:
                # Check consistency - more bearish candles confirms downtrend
                if bearish_count / total_candles >= self.TREND_CONSISTENCY_RATIO:
                    return 'DOWNTREND'
                # Still downtrend if price dropped significantly even with mixed candles
                elif price_change_pct <= -self.DOWNTREND_MIN_DECLINE * 2:
                    return 'DOWNTREND'
            
            elif price_change_pct >= self.UPTREND_MIN_INCREASE:
                # Check consistency - more bullish candles confirms uptrend
                if bullish_count / total_candles >= self.TREND_CONSISTENCY_RATIO:
                    return 'UPTREND'
                # Still uptrend if price rose significantly even with mixed candles
                elif price_change_pct >= self.UPTREND_MIN_INCREASE * 2:
                    return 'UPTREND'
            
            return 'SIDEWAYS'
            
        except Exception as e:
            logger.error(f"Error in _get_trend_context for stock {stock.stock_symbol}: {e}")
            return 'SIDEWAYS'
    
    def _is_suitable_for_bullish_reversal(self, stock: Stock) -> bool:
        """
        Check if the context is suitable for a bullish reversal pattern.
        Bullish reversals work best after a downtrend.
        """
        trend = self._get_trend_context(stock)
        return trend == 'DOWNTREND'
    
    def _is_suitable_for_bearish_reversal(self, stock: Stock) -> bool:
        """
        Check if the context is suitable for a bearish reversal pattern.
        Bearish reversals work best after an uptrend.
        """
        trend = self._get_trend_context(stock)
        return trend == 'UPTREND'
    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def singleCandleStickPattern(self, stock: Stock):
        """
        Single candlestick MOMENTUM patterns (Marubozu).
        
        Patterns detected:
        - Bullish Marubozu: Open=Low, Close=High, large bullish body
        - Bearish Marubozu: Open=High, Close=Low, large bearish body
        
        These are MOMENTUM patterns showing strong buying/selling pressure.
        Works well in most market contexts.
        """
        try:
            logger.debug(f'Inside singleCandleStickPattern for stock {stock.stock_symbol}')
            currData = stock.current_equity_data
            closePrice = currData['Close']
            openPrice = currData['Open']
            highPrice = currData['High']
            lowPrice = currData['Low']

            # Bullish Marubozu: No wicks, large bullish body
            if (((openPrice == lowPrice) or (percentageChange(openPrice, lowPrice) <= CandleStickAnalyser.WICK_PERCENTAGE)) \
                    and ((highPrice == closePrice) or (percentageChange(highPrice, closePrice) <= CandleStickAnalyser.WICK_PERCENTAGE)) \
                    and (percentageChange(closePrice, openPrice) >= CandleStickAnalyser.MARUBASU_THRESHOLD)):
                stock.set_analysis("BULLISH", "Single_candle_stick_pattern", 
                                   "Bullish Marubozu, rate: {:.2f}%".format(percentageChange(closePrice, openPrice)))
                return True

            # Bearish Marubozu: No wicks, large bearish body
            elif (((openPrice == highPrice) or (percentageChange(highPrice,openPrice) <= CandleStickAnalyser.WICK_PERCENTAGE)) \
                and ((lowPrice == closePrice) or (percentageChange(closePrice,lowPrice) <= CandleStickAnalyser.WICK_PERCENTAGE)) \
                and (abs(percentageChange(closePrice,openPrice)) >= CandleStickAnalyser.MARUBASU_THRESHOLD)):
                stock.set_analysis("BEARISH", "Single_candle_stick_pattern", 
                                   "Bearish Marubozu, rate: {:.2f}%".format(percentageChange(closePrice, openPrice)))
                return True

            return False
        except Exception as e:
            logger.error(f"Error in singleCandleStickPattern for stock {stock.stock_symbol}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def singleCandleReversalPattern(self, stock: Stock):
        """
        Single candlestick REVERSAL patterns (Hammer, Shooting Star).
        
        Patterns detected:
        - Hammer: Small body near top, long lower wick - Bullish reversal after decline
        - Shooting Star: Small body near bottom, long upper wick - Bearish reversal after rally
        
        TREND CONTEXT REQUIRED:
        - Hammer: Only signals BULLISH after a confirmed downtrend (price decline)
        - Shooting Star: Only signals BEARISH after a confirmed uptrend (price rally)
        
        This significantly reduces false signals by ensuring reversal patterns
        only trigger when they appear at the end of an appropriate trend.
        """
        try:
            logger.debug(f'Inside singleCandleReversalPattern for stock {stock.stock_symbol}')
            currData = stock.current_equity_data
            closePrice = currData['Close']
            openPrice = currData['Open']
            highPrice = currData['High']
            lowPrice = currData['Low']

            total_range = highPrice - lowPrice
            if total_range > 0:
                body = abs(closePrice - openPrice)
                body_ratio = body / total_range
                lower_wick = min(openPrice, closePrice) - lowPrice
                upper_wick = highPrice - max(openPrice, closePrice)

                # Hammer (Bullish reversal): small body near top, long lower shadow, tiny upper shadow
                # REQUIRE: Must appear after a downtrend for valid bullish reversal signal
                if (body > 0 and body_ratio <= self.HAMMER_BODY_RATIO and
                        lower_wick >= self.HAMMER_WICK_MULTIPLIER * body and
                        upper_wick <= total_range * 0.1):
                    
                    # Check trend context - only signal if after downtrend
                    if self._is_suitable_for_bullish_reversal(stock):
                        trend = self._get_trend_context(stock)
                        stock.set_analysis("BULLISH", "Single_candle_reversal_pattern",
                                           f"Hammer (after {trend}), range: {percentageChange(highPrice, lowPrice):.2f}%")
                        return True
                    else:
                        logger.debug(f"Hammer detected for {stock.stock_symbol} but no downtrend context - skipping")

                # Shooting Star (Bearish reversal): small body near bottom, long upper shadow, tiny lower shadow
                # REQUIRE: Must appear after an uptrend for valid bearish reversal signal
                elif (body > 0 and body_ratio <= self.HAMMER_BODY_RATIO and
                      upper_wick >= self.HAMMER_WICK_MULTIPLIER * body and
                      lower_wick <= total_range * 0.1):
                    
                    # Check trend context - only signal if after uptrend
                    if self._is_suitable_for_bearish_reversal(stock):
                        trend = self._get_trend_context(stock)
                        stock.set_analysis("BEARISH", "Single_candle_reversal_pattern",
                                           f"Shooting Star (after {trend}), range: {percentageChange(highPrice, lowPrice):.2f}%")
                        return True
                    else:
                        logger.debug(f"Shooting Star detected for {stock.stock_symbol} but no uptrend context - skipping")

            return False
        except Exception as e:
            logger.error(f"Error in singleCandleReversalPattern for stock {stock.stock_symbol}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False


    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def doubleCandleStickPattern(self, stock: Stock):
        """
        Double candlestick REVERSAL patterns only.
        
        Patterns detected:
        - Bullish Engulfing: bearish prev → larger bullish curr that engulfs prev body
        - Bearish Engulfing: bullish prev → larger bearish curr that engulfs prev body
        - Piercing Line: bearish prev → bullish curr closes above prev midpoint
        - Dark Cloud Cover: bullish prev → bearish curr closes below prev midpoint
        
        TREND CONTEXT REQUIRED:
        - Bullish patterns (Engulfing, Piercing): Only signal after downtrend
        - Bearish patterns (Engulfing, Dark Cloud): Only signal after uptrend
        
        This significantly improves pattern reliability by ensuring reversals
        occur at appropriate trend turning points.
        """
        try:
            logger.debug(f'Inside doubleCandleStickPattern for stock {stock.stock_symbol}')
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
            prev_body = abs(prevClosePrice - prevOpenPrice)
            curr_body = abs(closePrice - openPrice)

            # Bullish Engulfing: bearish prev → larger bullish curr that fully engulfs prev body
            # REQUIRE: Must appear after a downtrend for valid bullish reversal signal
            if (prevClosePrice < prevOpenPrice and closePrice > openPrice and
                    openPrice <= prevClosePrice and closePrice >= prevOpenPrice and
                    curr_body >= CandleStickAnalyser.ENGULFING_MIN_BODY_RATIO * prev_body):
                
                if self._is_suitable_for_bullish_reversal(stock):
                    trend = self._get_trend_context(stock)
                    stock.set_analysis("BULLISH", "Double_candle_stick_pattern",
                                       f"Bullish Engulfing (after {trend}), rate: {percentageChange(closePrice, openPrice):.2f}%")
                    return True
                else:
                    logger.debug(f"Bullish Engulfing detected for {stock.stock_symbol} but no downtrend context - skipping")

            # Bearish Engulfing: bullish prev → larger bearish curr that fully engulfs prev body
            # REQUIRE: Must appear after an uptrend for valid bearish reversal signal
            elif (prevClosePrice > prevOpenPrice and closePrice < openPrice and
                  openPrice >= prevClosePrice and closePrice <= prevOpenPrice and
                  curr_body >= CandleStickAnalyser.ENGULFING_MIN_BODY_RATIO * prev_body):
                
                if self._is_suitable_for_bearish_reversal(stock):
                    trend = self._get_trend_context(stock)
                    stock.set_analysis("BEARISH", "Double_candle_stick_pattern",
                                       f"Bearish Engulfing (after {trend}), rate: {percentageChange(closePrice, openPrice):.2f}%")
                    return True
                else:
                    logger.debug(f"Bearish Engulfing detected for {stock.stock_symbol} but no uptrend context - skipping")

            # Piercing Line: bearish prev → bullish curr opens ≤ prev close, closes above prev body midpoint
            # REQUIRE: Must appear after a downtrend for valid bullish reversal signal
            elif (prevClosePrice < prevOpenPrice and closePrice > openPrice and
                  openPrice <= prevClosePrice and 
                  closePrice > prev_mid and
                  closePrice < prevOpenPrice and
                  (closePrice - prev_mid) / prev_body >= CandleStickAnalyser.PIERCING_MIN_PENETRATION):
                
                if self._is_suitable_for_bullish_reversal(stock):
                    trend = self._get_trend_context(stock)
                    stock.set_analysis("BULLISH", "Double_candle_stick_pattern",
                                       f"Piercing Line (after {trend}), close: {closePrice:.2f} > mid: {prev_mid:.2f}")
                    return True
                else:
                    logger.debug(f"Piercing Line detected for {stock.stock_symbol} but no downtrend context - skipping")

            # Dark Cloud Cover: bullish prev → bearish curr opens ≥ prev close, closes below prev body midpoint
            # REQUIRE: Must appear after an uptrend for valid bearish reversal signal
            elif (prevClosePrice > prevOpenPrice and closePrice < openPrice and
                  openPrice >= prevClosePrice and 
                  closePrice < prev_mid and
                  closePrice > prevOpenPrice and
                  (prev_mid - closePrice) / prev_body >= CandleStickAnalyser.DARK_CLOUD_MIN_PENETRATION):
                
                if self._is_suitable_for_bearish_reversal(stock):
                    trend = self._get_trend_context(stock)
                    stock.set_analysis("BEARISH", "Double_candle_stick_pattern",
                                       f"Dark Cloud Cover (after {trend}), close: {closePrice:.2f} < mid: {prev_mid:.2f}")
                    return True
                else:
                    logger.debug(f"Dark Cloud Cover detected for {stock.stock_symbol} but no uptrend context - skipping")

            return False
        except Exception as e:
            logger.error(f"Error in doubleCandleStickPattern for stock {stock.stock_symbol}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def doubleCandleStickContinuationPattern(self, stock: Stock):
        """
        Double candlestick CONTINUATION patterns (trend-following).
        
        Patterns detected:
        - 2 Continuous Increase: Two consecutive bullish candles with rising prices
        - 2 Continuous Decrease: Two consecutive bearish candles with falling prices
        
        WARNING: These are trend-following patterns that buy after price has already moved.
        They tend to have NEGATIVE EXPECTANCY because they enter at local tops/bottoms.
        Use with caution and consider adding mean-reversion filters.
        """
        try:
            logger.debug(f'Inside doubleCandleStickContinuationPattern for stock {stock.stock_symbol}')
            currData = stock.current_equity_data
            prevData = stock.previous_equity_data
            
            closePrice = currData['Close']
            openPrice = currData['Open']

            prevClosePrice = prevData['Close']
            prevOpenPrice = prevData['Open']

            # 2 Continuous Increase (Trend-following: buying into strength)
            if (prevOpenPrice < prevClosePrice) and (openPrice < closePrice) and \
               (closePrice > prevClosePrice) and \
               (percentageChange(closePrice, prevOpenPrice) >= CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD):
                stock.set_analysis("BULLISH", "Double_candle_continuation_pattern", 
                                   "2_cont_inc, rate:{:.2f}%".format(percentageChange(closePrice, prevOpenPrice)))
                return True

            # 2 Continuous Decrease (Trend-following: selling into weakness)
            elif (prevOpenPrice > prevClosePrice) and (openPrice > closePrice) and \
                 (closePrice < prevClosePrice) and \
                 (abs(percentageChange(closePrice, prevOpenPrice)) >= CandleStickAnalyser.TWO_CONT_INC_OR_DEC_THRESHOLD):
                stock.set_analysis("BEARISH", "Double_candle_continuation_pattern", 
                                   "2_cont_dec, rate:{:.2f}%".format(percentageChange(closePrice, prevOpenPrice)))
                return True

            return False
        except Exception as e:
            logger.error(f"Error in doubleCandleStickContinuationPattern for stock {stock.stock_symbol}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def tripleCandleStickReversalPattern(self, stock: Stock):
        """
        Triple candlestick REVERSAL patterns only.
        
        Patterns detected:
        - Morning Star: Bullish reversal after downtrend
        - Evening Star: Bearish reversal after uptrend
        
        TREND CONTEXT REQUIRED:
        - Morning Star: Only signals BULLISH after a confirmed downtrend
        - Evening Star: Only signals BEARISH after a confirmed uptrend
        """
        try:
            logger.debug(f'Inside tripleCandleStickReversalPattern for stock {stock.stock_symbol}')
            currData = stock.current_equity_data
            prevData = stock.previous_equity_data
            prevPrevData = stock.previous_previous_equity_data
            
            closePrice = currData['Close']
            openPrice = currData['Open']

            prevClosePrice = prevData['Close']
            prevOpenPrice = prevData['Open']

            prevPrevClosePrice = prevPrevData['Close']
            prevPrevOpenPrice = prevPrevData['Open']

            first_body = abs(prevPrevClosePrice - prevPrevOpenPrice)
            star_body = abs(prevClosePrice - prevOpenPrice)
            pp_mid = (prevPrevOpenPrice + prevPrevClosePrice) / 2

            # Morning Star (Bullish reversal): large bearish → small star → bullish closing above 1st midpoint
            # REQUIRE: Must appear after a downtrend for valid bullish reversal signal
            if (prevPrevClosePrice < prevPrevOpenPrice and
                    first_body > 0 and star_body <= self.STAR_MAX_BODY_RATIO * first_body and
                    closePrice > openPrice and closePrice > pp_mid):
                
                if self._is_suitable_for_bullish_reversal(stock):
                    trend = self._get_trend_context(stock)
                    stock.set_analysis("BULLISH", "Triple_candle_reversal_pattern",
                                       f"Morning Star (after {trend}), close: {closePrice:.2f} > mid: {pp_mid:.2f}")
                    return True
                else:
                    logger.debug(f"Morning Star detected for {stock.stock_symbol} but no downtrend context - skipping")

            # Evening Star (Bearish reversal): large bullish → small star → bearish closing below 1st midpoint
            # REQUIRE: Must appear after an uptrend for valid bearish reversal signal
            elif (prevPrevClosePrice > prevPrevOpenPrice and
                  first_body > 0 and star_body <= self.STAR_MAX_BODY_RATIO * first_body and
                  closePrice < openPrice and closePrice < pp_mid):
                
                if self._is_suitable_for_bearish_reversal(stock):
                    trend = self._get_trend_context(stock)
                    stock.set_analysis("BEARISH", "Triple_candle_reversal_pattern",
                                       f"Evening Star (after {trend}), close: {closePrice:.2f} < mid: {pp_mid:.2f}")
                    return True
                else:
                    logger.debug(f"Evening Star detected for {stock.stock_symbol} but no uptrend context - skipping")

            return False
        except Exception as e:
            logger.error(f"Error in tripleCandleStickReversalPattern for stock {stock.stock_symbol}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def tripleCandleStickContinuationPattern(self, stock: Stock):
        """
        Triple candlestick CONTINUATION patterns only.
        
        Patterns detected:
        - 3 Continuous Increase: Three consecutive bullish candles with rising prices
        - 3 Continuous Decrease: Three consecutive bearish candles with falling prices
        
        WARNING: These are trend-following patterns that buy after price has already moved.
        They tend to have NEGATIVE EXPECTANCY because they enter at local tops/bottoms.
        Use with caution.
        """
        try:
            logger.debug(f'Inside tripleCandleStickContinuationPattern for stock {stock.stock_symbol}')
            currData = stock.current_equity_data
            prevData = stock.previous_equity_data
            prevPrevData = stock.previous_previous_equity_data
            
            closePrice = currData['Close']
            openPrice = currData['Open']

            prevClosePrice = prevData['Close']
            prevOpenPrice = prevData['Open']

            prevPrevClosePrice = prevPrevData['Close']
            prevPrevOpenPrice = prevPrevData['Open']

            # 3 Continuous Increase (Trend-following: buying into strength)
            if (prevPrevOpenPrice < prevPrevClosePrice) and (prevOpenPrice < prevClosePrice) and (openPrice < closePrice) \
                and (closePrice > prevClosePrice) and (prevClosePrice > prevPrevClosePrice) and \
                (percentageChange(closePrice, prevPrevOpenPrice) >= CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD):
                stock.set_analysis("BULLISH", "Triple_candle_continuation_pattern", 
                                   "3_cont_inc, rate:{:.2f}%".format(percentageChange(closePrice, prevPrevOpenPrice)))
                return True

            # 3 Continuous Decrease (Trend-following: selling into weakness)
            elif (prevPrevOpenPrice > prevPrevClosePrice) and (prevOpenPrice > prevClosePrice) and (openPrice > closePrice) \
                and (closePrice < prevClosePrice) and (prevClosePrice < prevPrevClosePrice) and \
                (abs(percentageChange(closePrice, prevPrevOpenPrice)) >= CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD):
                stock.set_analysis("BEARISH", "Triple_candle_continuation_pattern", 
                                   "3_cont_dec, rate:{:.2f}%".format(percentageChange(closePrice, prevPrevOpenPrice)))
                return True

            return False
        except Exception as e:
            logger.error(f"Error in tripleCandleStickContinuationPattern for stock {stock.stock_symbol}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
        


