import traceback
import numpy as np
import pandas as pd
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.helperFunctions import percentageChange
from common.logging_util import logger
from collections import namedtuple
import common.shared as shared
import common.constants as constant


class FuturesAnalyser(BaseAnalyzer):
    """Enhanced Futures Analyser with dynamic thresholds, multi-timeframe analysis, and signal scoring."""
    
    # Base thresholds (will be dynamically adjusted)
    FUTURE_OI_INCREASE_PERCENTAGE = 0
    FUTURE_PRICE_CHANGE_PERCENTAGE = 0
    ORB_CANDLES = 3
    
    # Dynamic threshold parameters
    ATR_PERIOD = 14
    OI_VOLATILITY_PERIOD = 20
    MIN_PRICE_THRESHOLD = 0.15  # Minimum 0.15% price change
    MIN_OI_THRESHOLD = 0.3      # Minimum 0.3% OI change
    
    # Multi-timeframe parameters
    SHORT_TERM_CANDLES = 5      # Short-term trend (5 candles)
    MEDIUM_TERM_CANDLES = 15    # Medium-term trend (15 candles)
    
    # Signal scoring parameters
    SIGNAL_SCORE_COMPONENTS = {
        'oi_confirmation': 20,
        'volume_confirmation': 20,
        'trend_alignment': 15,
        'momentum_confirmation': 15,
        'time_filter': 10,
        'risk_reward_ratio': 20
    }

    def __init__(self) -> None:
        self.analyserName = "Futures Analyser"
        super().__init__()
        self._dynamic_thresholds_cache = {}
    
    def reset_constants(self):
        """Reset constants based on trading mode (intraday vs positional)."""
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE = 0.5
            FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE = 0.5
            FuturesAnalyser.ORB_CANDLES = 3
        else:
            FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE = 10
            FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE = 2
        logger.debug(f"FuturesAnalyser constants reset for mode {shared.app_ctx.mode.name}")
        logger.debug(f'FUTURE_OI_INCREASE_PERCENTAGE: {FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE}, FUTURE_PRICE_CHANGE_PERCENTAGE: {FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE}')
    
    # ==================== Dynamic Threshold Methods ====================
    
    def calculate_atr(self, futures_data: pd.DataFrame, period: int = 14) -> float:
        """
        Calculate Average True Range (ATR) for futures data.
        
        Args:
            futures_data: DataFrame with 'high', 'low', 'close' columns
            period: ATR calculation period
            
        Returns:
            ATR value
        """
        try:
            if len(futures_data) < period + 1:
                period = max(len(futures_data) - 1, 1)
            
            high = futures_data['high']
            low = futures_data['low']
            close = futures_data['close']
            
            tr1 = high - low
            tr2 = abs(high - close.shift(1))
            tr3 = abs(low - close.shift(1))
            
            true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = true_range.rolling(window=period).mean().iloc[-1]
            
            return atr if not np.isnan(atr) else 0.0
        except Exception as e:
            logger.warning(f"Error calculating ATR: {e}")
            return 0.0
    
    def calculate_atr_percentage(self, futures_data: pd.DataFrame, period: int = 14) -> float:
        """
        Calculate ATR as a percentage of price.
        
        Args:
            futures_data: DataFrame with OHLC data
            period: ATR calculation period
            
        Returns:
            ATR percentage
        """
        try:
            atr = self.calculate_atr(futures_data, period)
            if atr == 0:
                return 0.5  # Default fallback
            
            last_close = futures_data['close'].iloc[-1]
            atr_pct = (atr / last_close) * 100
            return atr_pct
        except Exception as e:
            logger.warning(f"Error calculating ATR percentage: {e}")
            return 0.5
    
    def calculate_oi_volatility(self, futures_data: pd.DataFrame, period: int = 20) -> float:
        """
        Calculate OI change volatility (standard deviation).
        
        Args:
            futures_data: DataFrame with 'oi' column
            period: Lookback period for volatility calculation
            
        Returns:
            OI volatility (std dev of percentage changes)
        """
        try:
            if 'oi' not in futures_data.columns or len(futures_data) < period + 1:
                period = max(len(futures_data) - 1, 2)
            
            oi_changes = futures_data['oi'].pct_change().dropna() * 100
            if len(oi_changes) < 2:
                return 0.5
            
            oi_volatility = oi_changes.tail(period).std()
            return oi_volatility if not np.isnan(oi_volatility) else 0.5
        except Exception as e:
            logger.warning(f"Error calculating OI volatility: {e}")
            return 0.5
    
    def get_dynamic_thresholds(self, stock: Stock, futures_data: pd.DataFrame) -> tuple:
        """
        Calculate dynamic thresholds based on stock volatility.
        
        Args:
            stock: Stock object
            futures_data: DataFrame with futures data
            
        Returns:
            Tuple of (price_threshold, oi_threshold)
        """
        try:
            cache_key = f"{stock.stock_symbol}_{len(futures_data)}"
            
            # Calculate ATR-based price threshold
            atr_pct = self.calculate_atr_percentage(futures_data, self.ATR_PERIOD)
            price_threshold = max(atr_pct * 0.5, self.MIN_PRICE_THRESHOLD)
            
            # Calculate OI volatility-based threshold
            oi_volatility = self.calculate_oi_volatility(futures_data, self.OI_VOLATILITY_PERIOD)
            oi_threshold = max(oi_volatility * 1.5, self.MIN_OI_THRESHOLD)
            
            # Apply mode-based adjustments
            if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
                # Intraday: tighter thresholds
                price_threshold = max(price_threshold, FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE * 0.5)
                oi_threshold = max(oi_threshold, FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE * 0.5)
            else:
                # Positional: use base thresholds as minimum
                price_threshold = max(price_threshold, FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE)
                oi_threshold = max(oi_threshold, FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE)
            
            logger.debug(f"Dynamic thresholds for {stock.stock_symbol}: price={price_threshold:.3f}%, oi={oi_threshold:.3f}%")
            return price_threshold, oi_threshold
            
        except Exception as e:
            logger.warning(f"Error calculating dynamic thresholds for {stock.stock_symbol}: {e}")
            return FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE, FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE
    
    # ==================== Multi-Timeframe Analysis Methods ====================
    
    def calculate_trend(self, futures_data: pd.DataFrame) -> str:
        """
        Determine trend direction from futures data.
        
        Args:
            futures_data: DataFrame with 'close' column
            
        Returns:
            Trend direction: 'BULLISH', 'BEARISH', or 'NEUTRAL'
        """
        try:
            if len(futures_data) < 2:
                return 'NEUTRAL'
            
            closes = futures_data['close']
            
            # Calculate price change percentage
            price_change = percentageChange(closes.iloc[-1], closes.iloc[0])
            
            # Calculate if making higher highs/lows or lower highs/lows
            if len(closes) >= 3:
                higher_highs = closes.iloc[-1] > closes.iloc[-2] > closes.iloc[-3]
                lower_lows = closes.iloc[-1] < closes.iloc[-2] < closes.iloc[-3]
            else:
                higher_highs = False
                lower_lows = False
            
            if price_change > 0.3 or higher_highs:
                return 'BULLISH'
            elif price_change < -0.3 or lower_lows:
                return 'BEARISH'
            else:
                return 'NEUTRAL'
                
        except Exception as e:
            logger.warning(f"Error calculating trend: {e}")
            return 'NEUTRAL'
    
    def analyze_multi_timeframe_trend(self, futures_data: pd.DataFrame) -> dict:
        """
        Analyze trend across multiple timeframes.
        
        Args:
            futures_data: DataFrame with futures data
            
        Returns:
            Dictionary with short_term, medium_term trends and alignment status
        """
        try:
            if len(futures_data) < self.SHORT_TERM_CANDLES:
                return {
                    'short_term': 'NEUTRAL',
                    'medium_term': 'NEUTRAL',
                    'aligned': False,
                    'alignment_strength': 0
                }
            
            # Short-term trend (last 5 candles)
            short_term_data = futures_data.tail(self.SHORT_TERM_CANDLES)
            short_term_trend = self.calculate_trend(short_term_data)
            
            # Medium-term trend (last 15 candles or available data)
            medium_term_len = min(self.MEDIUM_TERM_CANDLES, len(futures_data))
            medium_term_data = futures_data.tail(medium_term_len)
            medium_term_trend = self.calculate_trend(medium_term_data)
            
            # Check alignment
            aligned = (short_term_trend == medium_term_trend and 
                      short_term_trend != 'NEUTRAL')
            
            # Calculate alignment strength (0-100)
            alignment_strength = 0
            if aligned:
                alignment_strength = 100
            elif short_term_trend != 'NEUTRAL' and medium_term_trend != 'NEUTRAL':
                # Conflicting trends
                alignment_strength = 0
            elif short_term_trend != 'NEUTRAL' or medium_term_trend != 'NEUTRAL':
                # One timeframe has a signal
                alignment_strength = 50
            
            return {
                'short_term': short_term_trend,
                'medium_term': medium_term_trend,
                'aligned': aligned,
                'alignment_strength': alignment_strength
            }
            
        except Exception as e:
            logger.warning(f"Error in multi-timeframe analysis: {e}")
            return {
                'short_term': 'NEUTRAL',
                'medium_term': 'NEUTRAL',
                'aligned': False,
                'alignment_strength': 0
            }
    
    # ==================== Signal Scoring Methods ====================
    
    def calculate_signal_score(self, stock: Stock, pattern_data: namedtuple, 
                               futures_data: pd.DataFrame, pattern_type: str) -> dict:
        """
        Calculate confidence score for a futures signal.
        
        Args:
            stock: Stock object
            pattern_data: Named tuple with pattern details
            futures_data: DataFrame with futures data
            pattern_type: Type of pattern detected
            
        Returns:
            Dictionary with score breakdown and confidence level
        """
        try:
            score = 0
            score_breakdown = {}
            
            # 1. OI Confirmation (+20)
            if hasattr(pattern_data, 'oi_confirm') and pattern_data.oi_confirm:
                score += self.SIGNAL_SCORE_COMPONENTS['oi_confirmation']
                score_breakdown['oi_confirmation'] = self.SIGNAL_SCORE_COMPONENTS['oi_confirmation']
            elif hasattr(pattern_data, 'oi_percentage'):
                oi_pct = abs(pattern_data.oi_percentage)
                if oi_pct > 1.0:
                    score += self.SIGNAL_SCORE_COMPONENTS['oi_confirmation']
                    score_breakdown['oi_confirmation'] = self.SIGNAL_SCORE_COMPONENTS['oi_confirmation']
                elif oi_pct > 0.5:
                    score += self.SIGNAL_SCORE_COMPONENTS['oi_confirmation'] * 0.5
                    score_breakdown['oi_confirmation'] = self.SIGNAL_SCORE_COMPONENTS['oi_confirmation'] * 0.5
            
            # 2. Volume Confirmation (+20)
            if hasattr(pattern_data, 'vol_confirm') and pattern_data.vol_confirm:
                score += self.SIGNAL_SCORE_COMPONENTS['volume_confirmation']
                score_breakdown['volume_confirmation'] = self.SIGNAL_SCORE_COMPONENTS['volume_confirmation']
            
            # 3. Multi-timeframe Alignment (+15)
            mtf_analysis = self.analyze_multi_timeframe_trend(futures_data)
            if mtf_analysis['aligned']:
                score += self.SIGNAL_SCORE_COMPONENTS['trend_alignment']
                score_breakdown['trend_alignment'] = self.SIGNAL_SCORE_COMPONENTS['trend_alignment']
            elif mtf_analysis['alignment_strength'] >= 50:
                score += self.SIGNAL_SCORE_COMPONENTS['trend_alignment'] * 0.5
                score_breakdown['trend_alignment'] = self.SIGNAL_SCORE_COMPONENTS['trend_alignment'] * 0.5
            
            # 4. Momentum Confirmation (+15)
            momentum_score = self._check_momentum(futures_data)
            if momentum_score > 0:
                score += momentum_score
                score_breakdown['momentum_confirmation'] = momentum_score
            
            # 5. Time Filter (+10)
            time_score = self._apply_time_filter()
            if time_score > 0:
                score += time_score
                score_breakdown['time_filter'] = time_score
            
            # 6. Risk/Reward (+20)
            rr_score = self._calculate_risk_reward_score(pattern_data, futures_data, pattern_type)
            if rr_score > 0:
                score += rr_score
                score_breakdown['risk_reward_ratio'] = rr_score
            
            # Determine confidence level
            if score >= 70:
                confidence = 'HIGH'
            elif score >= 50:
                confidence = 'MEDIUM'
            elif score >= 30:
                confidence = 'LOW'
            else:
                confidence = 'VERY_LOW'
            
            return {
                'total_score': score,
                'confidence': confidence,
                'score_breakdown': score_breakdown,
                'mtf_analysis': mtf_analysis
            }
            
        except Exception as e:
            logger.error(f"Error calculating signal score for {stock.stock_symbol}: {e}")
            return {
                'total_score': 0,
                'confidence': 'VERY_LOW',
                'score_breakdown': {},
                'mtf_analysis': {}
            }
    
    def _check_momentum(self, futures_data: pd.DataFrame) -> float:
        """Check momentum indicators for confirmation."""
        try:
            if len(futures_data) < 14:
                return 0
            
            closes = futures_data['close']
            
            # Simple RSI-like momentum check
            gains = closes.diff().clip(lower=0)
            losses = (-closes.diff()).clip(lower=0)
            
            avg_gain = gains.rolling(window=14).mean().iloc[-1]
            avg_loss = losses.rolling(window=14).mean().iloc[-1]
            
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
            
            # Price rate of change
            roc = (closes.iloc[-1] - closes.iloc[-5]) / closes.iloc[-5] * 100 if len(closes) >= 5 else 0
            
            momentum_score = 0
            
            # RSI confirmation (not overbought/oversold)
            if 40 <= rsi <= 70:
                momentum_score += self.SIGNAL_SCORE_COMPONENTS['momentum_confirmation'] * 0.5
            elif 30 <= rsi <= 80:
                momentum_score += self.SIGNAL_SCORE_COMPONENTS['momentum_confirmation'] * 0.25
            
            # ROC confirmation
            if abs(roc) > 0.2:
                momentum_score += self.SIGNAL_SCORE_COMPONENTS['momentum_confirmation'] * 0.5
            
            return min(momentum_score, self.SIGNAL_SCORE_COMPONENTS['momentum_confirmation'])
            
        except Exception as e:
            logger.warning(f"Error checking momentum: {e}")
            return 0
    
    def _apply_time_filter(self) -> float:
        """Apply time-based filter for signal quality."""
        try:
            from datetime import datetime, time
            
            # Get current time in market timezone (assuming IST for Indian markets)
            now = datetime.now()
            current_time = now.time()
            
            # Market open (9:15-9:45) - High volatility, less reliable
            if time(9, 15) <= current_time < time(9, 45):
                return 0  # No bonus during opening volatility
            
            # Lunch hour (12:00-13:00) - Low liquidity
            if time(12, 0) <= current_time < time(13, 0):
                return self.SIGNAL_SCORE_COMPONENTS['time_filter'] * 0.5  # Reduced bonus
            
            # Market close (14:45-15:30) - Position squaring
            if time(14, 45) <= current_time <= time(15, 30):
                return self.SIGNAL_SCORE_COMPONENTS['time_filter'] * 0.75  # Slightly reduced
            
            # Prime trading hours (9:45-12:00, 13:00-14:45)
            return self.SIGNAL_SCORE_COMPONENTS['time_filter']
            
        except Exception as e:
            logger.warning(f"Error applying time filter: {e}")
            return self.SIGNAL_SCORE_COMPONENTS['time_filter'] * 0.5
    
    def _calculate_risk_reward_score(self, pattern_data: namedtuple, 
                                      futures_data: pd.DataFrame, pattern_type: str) -> float:
        """Calculate risk/reward score component."""
        try:
            # For breakout patterns, calculate based on ORB levels
            if 'breakout' in pattern_type.lower():
                if hasattr(pattern_data, 'orb_high') and hasattr(pattern_data, 'orb_low'):
                    orb_range = pattern_data.orb_high - pattern_data.orb_low
                    last_close = pattern_data.last_close
                    
                    if 'up' in pattern_type.lower():
                        stop_distance = last_close - pattern_data.orb_high
                    else:
                        stop_distance = pattern_data.orb_low - last_close
                    
                    if stop_distance > 0:
                        # Potential target is 1:1 or 1:2
                        rr_ratio = orb_range / abs(stop_distance) if stop_distance != 0 else 0
                        
                        if rr_ratio >= 2:
                            return self.SIGNAL_SCORE_COMPONENTS['risk_reward_ratio']
                        elif rr_ratio >= 1.5:
                            return self.SIGNAL_SCORE_COMPONENTS['risk_reward_ratio'] * 0.75
                        elif rr_ratio >= 1:
                            return self.SIGNAL_SCORE_COMPONENTS['risk_reward_ratio'] * 0.5
            
            # For action patterns, use OI and price magnitude as proxy
            elif hasattr(pattern_data, 'price_percentage'):
                price_mag = abs(pattern_data.price_percentage)
                oi_mag = abs(pattern_data.oi_percentage) if hasattr(pattern_data, 'oi_percentage') else 0
                
                if price_mag > 1.0 and oi_mag > 1.0:
                    return self.SIGNAL_SCORE_COMPONENTS['risk_reward_ratio']
                elif price_mag > 0.5 and oi_mag > 0.5:
                    return self.SIGNAL_SCORE_COMPONENTS['risk_reward_ratio'] * 0.5
            
            return 0
            
        except Exception as e:
            logger.warning(f"Error calculating risk/reward score: {e}")
            return 0

    # ==================== Main Analysis Methods ====================
    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_intraday_check_future_action(self, stock: Stock):
        """
        Analyze futures action with dynamic thresholds and signal scoring.
        
        Detects:
        - Long Buildup: Price up + OI up
        - Short Buildup: Price down + OI up
        - Short Covering: Price up + OI down
        - Long Unwinding: Price down + OI down
        """
        try:
            logger.debug("Inside analyse_intraday_check_future_action method for stock {}".format(stock.stock_symbol))

            def get_future_action(futures_data, price_col="close", oi_col="oi", expiry='current'):
                """
                Determines futures action based on price and OI percentage change.
                Uses dynamic thresholds based on volatility.
                """
                FutureActionAnalysis = namedtuple('FutureActionAnalysis', 
                    ['expiry', 'action', 'price_percentage', 'oi_percentage', 'score', 'confidence'])

                if len(futures_data) < 2:
                    logger.warning(f"Insufficient data for futures analysis for stock: {stock.stock_symbol} and expiry: {expiry}. Skipping action determination.")
                    return False

                # Get dynamic thresholds
                price_threshold, oi_threshold = self.get_dynamic_thresholds(stock, futures_data)

                prev_oi = futures_data.iloc[-2][oi_col]
                curr_oi = futures_data.iloc[-1][oi_col]
                prev_price = futures_data.iloc[-2][price_col]
                curr_price = futures_data.iloc[-1][price_col]

                price_percentage = percentageChange(curr_price, prev_price)
                oi_percentage = percentageChange(curr_oi, prev_oi)

                action = None
                sentiment = None

                if price_percentage > price_threshold and oi_percentage > oi_threshold:
                    action = "long_buildup"
                    sentiment = "BULLISH"
                elif price_percentage < (-1 * price_threshold) and oi_percentage > oi_threshold:
                    action = "short_buildup"
                    sentiment = "BEARISH"
                elif price_percentage > price_threshold and oi_percentage < (-1 * oi_threshold):
                    action = "short_covering"
                    sentiment = "BULLISH"
                elif price_percentage < (-1 * price_threshold) and oi_percentage < (-1 * oi_threshold):
                    action = "long_unwinding"
                    sentiment = "BEARISH"

                if action:
                    # Calculate signal score
                    pattern_tuple = namedtuple('PatternData', ['price_percentage', 'oi_percentage', 'oi_confirm'])(
                        price_percentage=price_percentage,
                        oi_percentage=oi_percentage,
                        oi_confirm=True
                    )
                    score_result = self.calculate_signal_score(
                        stock, pattern_tuple, futures_data, f"future_action_{action}"
                    )
                    
                    analysis_result = FutureActionAnalysis(
                        expiry, action, price_percentage, oi_percentage,
                        score_result['total_score'], score_result['confidence']
                    )
                    
                    stock.set_analysis(sentiment, "FUTURE_ACTION", analysis_result)
                    
                    logger.info(f"Futures {action} detected for {stock.stock_symbol}: "
                               f"price={price_percentage:.2f}%, oi={oi_percentage:.2f}%, "
                               f"score={score_result['total_score']}, confidence={score_result['confidence']}")
                    return True
                    
                return False

            zerodha_ctx = stock.zerodha_ctx

            futures_data_curr = zerodha_ctx["futures_data"]["current"]
            futures_data_next = zerodha_ctx["futures_data"]["next"]
            res = False
            
            if get_future_action(futures_data_curr, expiry='current'):
                logger.info(f"Futures action detected for {stock.stock_symbol} for current expiry")
                res = True
            
            # Enable next expiry analysis for roll detection
            if futures_data_next is not None and not futures_data_next.empty:
                if get_future_action(futures_data_next, expiry='next'):
                    logger.info(f"Futures action detected for {stock.stock_symbol} for next expiry")
                    res = True

            if res:
                logger.debug(f"Futures action detected for {stock.stock_symbol}")
            
            return res
            
        except Exception as e:
            logger.error(f"Error in analyse_intraday_check_future_action for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False    

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_intraday_price_volume_oi_pattern(self, stock: Stock):
        """
        Analyze Price-Volume-OI patterns with dynamic thresholds and multi-timeframe confirmation.
        
        Patterns:
          price_up_vol_oi_flat      -> price up while participation not building (weak move)
          price_down_vol_oi_flat    -> price down while participation not building (weak move)
          price_flat_vol_oi_incr    -> possible absorption / pre-breakout buildup
          price_flat_vol_oi_dec     -> position unwinding, potential breakout
        """
        try:
            zerodha_ctx = stock.zerodha_ctx
            fut_curr = zerodha_ctx["futures_data"]["current"]
            if fut_curr is None or fut_curr.empty or len(fut_curr) < 2:
                return False

            prev = fut_curr.iloc[-2]
            curr = fut_curr.iloc[-1]

            prev_close = prev.get("close"); curr_close = curr.get("close")
            prev_vol = prev.get("volume", 0); curr_vol = curr.get("volume", 0)
            prev_oi = prev.get("oi", 0); curr_oi = curr.get("oi", 0)
            if None in (prev_close, curr_close):
                return False

            price_pct = percentageChange(curr_close, prev_close)
            vol_pct = percentageChange(curr_vol, prev_vol) if prev_vol else 0
            oi_pct = percentageChange(curr_oi, prev_oi) if prev_oi else 0

            # Get dynamic thresholds
            price_threshold, oi_threshold = self.get_dynamic_thresholds(stock, fut_curr)
            
            FLAT_PRICE_THRESH = max(price_threshold * 0.3, 0.15)
            FLAT_VOL_OI_THRESH = max(oi_threshold * 0.3, 0.2)

            price_up   = price_pct > price_threshold
            price_down = price_pct < -price_threshold
            price_flat = abs(price_pct) <= FLAT_PRICE_THRESH

            vol_flat = abs(vol_pct) <= FLAT_VOL_OI_THRESH
            oi_flat  = abs(oi_pct) <= FLAT_VOL_OI_THRESH
            vol_incr = vol_pct > oi_threshold
            oi_incr  = oi_pct > oi_threshold
            vol_dec = vol_pct < -oi_threshold
            oi_dec  = oi_pct < -oi_threshold

            # Multi-timeframe analysis
            mtf_analysis = self.analyze_multi_timeframe_trend(fut_curr)

            PatternTuple = namedtuple("FuturesPVOPattern",
                ["pattern", "price_pct", "vol_pct", "oi_pct", "expiry", 
                 "mtf_aligned", "score", "confidence"])

            pattern = None
            sentiment = "NEUTRAL"

            if price_up and vol_flat and oi_flat:
                pattern = "price_up_vol_oi_flat"
                sentiment = "NEUTRAL"
            elif price_down and vol_flat and oi_flat:
                pattern = "price_down_vol_oi_flat"
                sentiment = "NEUTRAL"
            elif price_flat and vol_incr and oi_incr:
                pattern = "price_flat_vol_oi_incr"
                sentiment = "NEUTRAL"
            elif price_flat and vol_dec and oi_dec:
                pattern = "price_flat_vol_oi_dec"
                sentiment = "NEUTRAL"

            if not pattern:
                return False

            # Calculate signal score
            pattern_data = namedtuple('PatternData', ['price_percentage', 'oi_percentage', 'oi_confirm'])(
                price_percentage=price_pct,
                oi_percentage=oi_pct,
                oi_confirm=oi_incr or oi_dec
            )
            score_result = self.calculate_signal_score(
                stock, pattern_data, fut_curr, f"pvo_{pattern}"
            )

            result_tuple = PatternTuple(
                pattern, price_pct, vol_pct, oi_pct, "current",
                mtf_analysis['aligned'], score_result['total_score'], score_result['confidence']
            )

            stock.set_analysis(sentiment, "FUTURE_PVO_PATTERN", result_tuple)
            
            logger.info(f"{pattern} detected for {stock.stock_symbol}: "
                        f"price {price_pct:.2f}%, vol {vol_pct:.2f}%, oi {oi_pct:.2f}%, "
                        f"score={score_result['total_score']}, mtf_aligned={mtf_analysis['aligned']}")
            return True

        except Exception as e:
            logger.error(f"Error in analyse_intraday_price_volume_oi_pattern for {stock.stock_symbol}: {e}")
            logger.error(traceback.format_exc())
            return False
        
    @BaseAnalyzer.index_intraday
    @BaseAnalyzer.intraday
    def analyse_intraday_breakout_oi_confirmation(self, stock: Stock):
        """
        Detect opening range breakout (ORB) with OI + Volume confirmation.
        
        Enhanced with:
        - Dynamic ORB period based on volatility
        - Multi-timeframe trend confirmation
        - Signal scoring system
        
        Patterns:
          orb_breakout_up_oi_confirmed
          orb_breakout_down_oi_confirmed
        """
        try:
            zerodha_ctx = stock.zerodha_ctx
            fut_curr = zerodha_ctx["futures_data"]["current"]
            if fut_curr is None or fut_curr.empty:
                return False

            # Ensure sorted by index (time)
            fut_curr = fut_curr.sort_index()

            # Intraday subset (assumes only today present; if not, filter by date)
            last_dt = fut_curr.index[-1]
            today_str = last_dt.strftime("%Y-%m-%d")
            fut_today = fut_curr[[d.strftime("%Y-%m-%d") == today_str for d in fut_curr.index]]

            # Dynamic ORB period based on volatility
            atr_pct = self.calculate_atr_percentage(fut_curr, self.ATR_PERIOD)
            if atr_pct > 1.0:  # High volatility
                ORB_CANDLES = 5  # Longer range for volatile stocks
            else:
                ORB_CANDLES = self.ORB_CANDLES
            
            BREAKOUT_BUFFER_PCT = 0.05
            price_threshold, oi_threshold = self.get_dynamic_thresholds(stock, fut_curr)
            OI_CONFIRM_PCT = max(oi_threshold, 0.5)
            VOL_ROLL_N = 5
            VOL_FACTOR = 1.2

            if len(fut_today) < ORB_CANDLES + 2:
                return False

            orb_slice = fut_today.iloc[:ORB_CANDLES]
            orb_high = orb_slice['high'].max()
            orb_low = orb_slice['low'].min()

            last = fut_today.iloc[-1]
            prev = fut_today.iloc[-2]

            last_close = last['close']; prev_close = prev['close']
            last_oi = last.get('oi', 0); prev_oi = prev.get('oi', 0)
            last_vol = last.get('volume', 0)

            if any(v is None for v in [last_close, prev_close, last_oi, prev_oi]):
                return False

            # Percentage changes
            oi_pct = percentageChange(last_oi, prev_oi)

            # Rolling volume average (exclude last bar)
            vol_series = fut_today['volume']
            if len(vol_series) < VOL_ROLL_N + 1:
                vol_avg = vol_series.iloc[:-1].mean()
            else:
                vol_avg = vol_series.iloc[-(VOL_ROLL_N+1):-1].mean()
            vol_confirm = last_vol > VOL_FACTOR * vol_avg if vol_avg > 0 else False

            buffer_high_level = orb_high * (1 + BREAKOUT_BUFFER_PCT / 100)
            buffer_low_level = orb_low * (1 - BREAKOUT_BUFFER_PCT / 100)

            breakout_up = last_close > buffer_high_level
            breakout_down = last_close < buffer_low_level

            oi_confirm = oi_pct > OI_CONFIRM_PCT
            pattern = None
            sentiment = "NEUTRAL"

            if breakout_up:
                if oi_confirm and vol_confirm:
                    pattern = "orb_breakout_up_oi_confirmed"
                    sentiment = "BULLISH"
            elif breakout_down:
                if oi_confirm and vol_confirm:
                    pattern = "orb_breakout_down_oi_confirmed"
                    sentiment = "BEARISH"

            if not pattern:
                return False

            # Multi-timeframe analysis
            mtf_analysis = self.analyze_multi_timeframe_trend(fut_curr)

            BreakoutTuple = namedtuple("FuturesBreakoutPattern", [
                "pattern",
                "orb_high",
                "orb_low",
                "last_close",
                "oi_pct",
                "vol",
                "vol_avg",
                "oi_confirm",
                "vol_confirm",
                "expiry",
                "mtf_aligned",
                "score",
                "confidence"
            ])

            # Calculate signal score
            pattern_data = namedtuple('PatternData', [
                'price_percentage', 'oi_percentage', 'oi_confirm', 'vol_confirm',
                'orb_high', 'orb_low', 'last_close'
            ])(
                price_percentage=((last_close - orb_high) / orb_high * 100) if breakout_up else ((orb_low - last_close) / orb_low * 100),
                oi_percentage=oi_pct,
                oi_confirm=oi_confirm,
                vol_confirm=vol_confirm,
                orb_high=orb_high,
                orb_low=orb_low,
                last_close=last_close
            )
            score_result = self.calculate_signal_score(
                stock, pattern_data, fut_curr, pattern
            )

            result_tuple = BreakoutTuple(
                pattern=pattern,
                orb_high=orb_high,
                orb_low=orb_low,
                last_close=last_close,
                oi_pct=oi_pct,
                vol=last_vol,
                vol_avg=vol_avg,
                oi_confirm=oi_confirm,
                vol_confirm=vol_confirm,
                expiry="current",
                mtf_aligned=mtf_analysis['aligned'],
                score=score_result['total_score'],
                confidence=score_result['confidence']
            )

            stock.set_analysis(sentiment, "FUTURE_BREAKOUT_PATTERN", result_tuple)

            logger.info(
                f"{pattern} {stock.stock_symbol}: close={last_close:.2f} "
                f"orbH={orb_high:.2f} orbL={orb_low:.2f} "
                f"OI%={oi_pct:.2f} vol={last_vol} avgVol={vol_avg:.0f} "
                f"OIconf={oi_confirm} VOLconf={vol_confirm} "
                f"score={score_result['total_score']} mtf={mtf_analysis['aligned']}"
            )
            return True

        except Exception as e:
            logger.error(f"Error in analyse_intraday_breakout_oi_confirmation for {stock.stock_symbol}: {e}")
            logger.error(traceback.format_exc())
            return False
