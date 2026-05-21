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

    # Base thresholds — overridden by reset_constants per mode
    FUTURE_OI_INCREASE_PERCENTAGE = 0
    FUTURE_PRICE_CHANGE_PERCENTAGE = 0
    ORB_CANDLES = 3

    # Dynamic threshold parameters
    ATR_PERIOD = 14
    OI_VOLATILITY_PERIOD = 20
    # Intraday minimums (5-min candles): real OI std ~0.06%, price std ~0.09%
    MIN_PRICE_THRESHOLD = 0.10   # 0.10% — just above 5-min noise floor
    MIN_OI_THRESHOLD    = 0.05   # 0.05% — allows dynamic std*1.5 to dominate

    # Multi-timeframe parameters — overridden by reset_constants
    SHORT_TERM_CANDLES  = 5
    MEDIUM_TERM_CANDLES = 15

    # OI startup noise: skip rows where OI < this fraction of max contract OI
    OI_STARTUP_FRACTION = 0.05

    # ORB deduplication: set per-session in analyse_intraday_breakout_oi_confirmation
    _orb_fired_up:         bool = False
    _orb_fired_down:       bool = False
    _orb_open_time_warned: bool = False

    # OI buildup from open: session open OI, set on first call each session
    _session_open_oi: float = 0.0
    _session_date:    str   = ""

    # Tracks last mode reset to avoid repeated reset_constants calls in the same mode
    _last_reset_mode: str = ""

    # Signal scoring parameters
    SIGNAL_SCORE_COMPONENTS = {
        'oi_confirmation':      20,
        'volume_confirmation':  20,
        'trend_alignment':      15,
        'momentum_confirmation': 15,
        'time_filter':          10,
        'risk_reward_ratio':    20,
    }

    def __init__(self) -> None:
        self.analyserName = "Futures Analyser"
        super().__init__()
        self._dynamic_thresholds_cache = {}
    
    def reset_constants(self):
        """Reset constants based on trading mode (intraday vs positional).
        Idempotent — does nothing when called again in the same mode.
        """
        current_mode = shared.app_ctx.mode.name
        if FuturesAnalyser._last_reset_mode == current_mode:
            return
        FuturesAnalyser._last_reset_mode = current_mode

        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            # Intraday 5-min: real daily price std ~0.09%, OI std ~0.06%
            FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE = 0.15  # ~1.5x 5-min noise
            FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE  = 0.10  # ~1.5x 5-min OI noise
            FuturesAnalyser.ORB_CANDLES       = 3
            FuturesAnalyser.SHORT_TERM_CANDLES  = 6   # 30 min
            FuturesAnalyser.MEDIUM_TERM_CANDLES = 18  # 90 min
            FuturesAnalyser.MIN_PRICE_THRESHOLD = 0.10
            FuturesAnalyser.MIN_OI_THRESHOLD    = 0.05
        else:
            # Positional daily: real price std ~1.3%, OI std ~15% (mature contract)
            FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE = 1.0   # below 1σ to catch real moves
            FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE  = 3.0   # ~0.2σ — catches meaningful OI shifts
            FuturesAnalyser.ORB_CANDLES       = 3                  # unused in positional
            FuturesAnalyser.SHORT_TERM_CANDLES  = 5   # 1 week
            FuturesAnalyser.MEDIUM_TERM_CANDLES = 15  # 3 weeks
            FuturesAnalyser.MIN_PRICE_THRESHOLD = 1.0
            FuturesAnalyser.MIN_OI_THRESHOLD    = 3.0
        # Reset per-session state on each mode switch
        FuturesAnalyser._orb_fired_up        = False
        FuturesAnalyser._orb_fired_down      = False
        FuturesAnalyser._orb_open_time_warned = False
        FuturesAnalyser._session_open_oi     = 0.0
        FuturesAnalyser._session_date        = ""
        logger.debug(
            f"[FuturesAnalyser] constants reset | mode={shared.app_ctx.mode.name} | "
            f"price_thresh={FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE}% "
            f"oi_thresh={FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE}% "
            f"short_term={FuturesAnalyser.SHORT_TERM_CANDLES} "
            f"medium_term={FuturesAnalyser.MEDIUM_TERM_CANDLES} "
            f"orb_candles={FuturesAnalyser.ORB_CANDLES}"
        )
    
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
        Calculate OI change volatility (std dev of percentage changes).
        Skips early contract rows where OI is below OI_STARTUP_FRACTION of max OI
        to avoid the 400–500% spikes that poison the std on contract start days.
        """
        try:
            if 'oi' not in futures_data.columns or len(futures_data) < 3:
                return self.MIN_OI_THRESHOLD

            oi = futures_data['oi'].dropna()
            if len(oi) < 3:
                return self.MIN_OI_THRESHOLD

            # Skip startup rows: OI below 5% of contract max is pre-liquidity noise
            max_oi = oi.max()
            if max_oi > 0:
                oi = oi[oi >= max_oi * self.OI_STARTUP_FRACTION]

            if len(oi) < 3:
                return self.MIN_OI_THRESHOLD

            oi_changes = oi.pct_change().dropna() * 100
            if len(oi_changes) < 2:
                return self.MIN_OI_THRESHOLD

            period = min(period, len(oi_changes))
            oi_volatility = oi_changes.tail(period).std()
            return oi_volatility if not np.isnan(oi_volatility) else self.MIN_OI_THRESHOLD
        except Exception as e:
            logger.warning(f"Error calculating OI volatility: {e}")
            return self.MIN_OI_THRESHOLD
    
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
                # Intraday: floor at half the base threshold
                price_threshold = max(price_threshold, FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE * 0.5)
                oi_threshold    = max(oi_threshold,    FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE  * 0.5)
            else:
                # Positional: floor at base, cap at 3x base.
                # The full-contract vol std (~16%) is too loose as a threshold —
                # a real daily OI move of 10-15% (e.g. short covering) would be
                # suppressed. Cap ensures meaningful moves always trigger.
                price_threshold = max(price_threshold, FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE)
                oi_threshold    = max(oi_threshold,    FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE)
                oi_threshold    = min(oi_threshold,    FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE * 3)

            at_floor = oi_volatility <= self.MIN_OI_THRESHOLD
            logger.debug(
                f"Dynamic thresholds for {stock.stock_symbol}: price={price_threshold:.3f}%, oi={oi_threshold:.3f}%"
                + (" [oi at floor — insufficient rows for vol calc]" if at_floor else "")
            )
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
        """Apply time-based filter for signal quality.
        Positional runs at ~8 PM — time windows are irrelevant, always full score.
        """
        try:
            if shared.app_ctx.mode == shared.Mode.POSITIONAL:
                return self.SIGNAL_SCORE_COMPONENTS['time_filter']

            from datetime import datetime, time

            now = datetime.now()
            current_time = now.time()

            # Market open (9:15-9:45) — high volatility, less reliable
            if time(9, 15) <= current_time < time(9, 45):
                return 0

            # Lunch hour (12:00-13:00) — low liquidity
            if time(12, 0) <= current_time < time(13, 0):
                return self.SIGNAL_SCORE_COMPONENTS['time_filter'] * 0.5

            # Market close (14:45-15:30) — position squaring
            if time(14, 45) <= current_time <= time(15, 30):
                return self.SIGNAL_SCORE_COMPONENTS['time_filter'] * 0.75

            # Prime trading hours (9:45-12:00, 13:00-14:45)
            return self.SIGNAL_SCORE_COMPONENTS['time_filter']

        except Exception as e:
            logger.warning(f"Error applying time filter: {e}")
            return self.SIGNAL_SCORE_COMPONENTS['time_filter'] * 0.5
    
    def _calculate_risk_reward_score(self, pattern_data: namedtuple,
                                      futures_data: pd.DataFrame, pattern_type: str) -> float:
        """Calculate risk/reward score component, mode-aware."""
        try:
            is_positional = shared.app_ctx.mode == shared.Mode.POSITIONAL

            # ORB breakout: score based on ORB range vs distance past the level
            if 'breakout' in pattern_type.lower():
                if hasattr(pattern_data, 'orb_high') and hasattr(pattern_data, 'orb_low'):
                    orb_range     = pattern_data.orb_high - pattern_data.orb_low
                    last_close    = pattern_data.last_close
                    stop_distance = (last_close - pattern_data.orb_high
                                     if 'up' in pattern_type.lower()
                                     else pattern_data.orb_low - last_close)
                    if stop_distance > 0:
                        rr_ratio = orb_range / stop_distance
                        if rr_ratio >= 2:
                            return self.SIGNAL_SCORE_COMPONENTS['risk_reward_ratio']
                        elif rr_ratio >= 1.5:
                            return self.SIGNAL_SCORE_COMPONENTS['risk_reward_ratio'] * 0.75
                        elif rr_ratio >= 1:
                            return self.SIGNAL_SCORE_COMPONENTS['risk_reward_ratio'] * 0.5

            # Action/OI patterns: scale thresholds by mode
            elif hasattr(pattern_data, 'price_percentage'):
                price_mag = abs(pattern_data.price_percentage)
                oi_mag    = abs(pattern_data.oi_percentage) if hasattr(pattern_data, 'oi_percentage') else 0
                # Positional: 1σ price ~1.3%, OI std ~15% → strong move = 1% price + 5% OI
                # Intraday:   5-min price std ~0.09%, OI std ~0.06% → strong = 0.15% + 0.1%
                high_thresh  = (1.0, 5.0)  if is_positional else (0.15, 0.10)
                med_thresh   = (0.5, 2.0)  if is_positional else (0.10, 0.06)
                if price_mag >= high_thresh[0] and oi_mag >= high_thresh[1]:
                    return self.SIGNAL_SCORE_COMPONENTS['risk_reward_ratio']
                elif price_mag >= med_thresh[0] and oi_mag >= med_thresh[1]:
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
            logger.debug(f"[FUT_ACT] {stock.stock_symbol} — start")

            def get_future_action(futures_data, price_col="close", oi_col="oi", expiry='current'):
                """
                Determines futures action based on price and OI percentage change.
                Uses dynamic thresholds based on volatility.
                """
                FutureActionAnalysis = namedtuple('FutureActionAnalysis',
                    ['expiry', 'action', 'price_percentage', 'oi_percentage', 'score', 'confidence'])

                if futures_data is None or futures_data.empty:
                    logger.debug(f"[FUT_ACT] {stock.stock_symbol} — futures_data None/empty expiry={expiry}, skip")
                    return False

                if len(futures_data) < 2:
                    logger.debug(f"[FUT_ACT] {stock.stock_symbol} — insufficient rows (rows={len(futures_data)} < 2) expiry={expiry}, skip")
                    return False

                # Get dynamic thresholds
                price_threshold, oi_threshold = self.get_dynamic_thresholds(stock, futures_data)

                prev_oi = futures_data.iloc[-2][oi_col]
                curr_oi = futures_data.iloc[-1][oi_col]
                prev_price = futures_data.iloc[-2][price_col]
                curr_price = futures_data.iloc[-1][price_col]

                price_percentage = percentageChange(curr_price, prev_price)
                oi_percentage = percentageChange(curr_oi, prev_oi)

                logger.debug(
                    f"[FUT_ACT] {stock.stock_symbol} | SOURCE expiry={expiry} rows={len(futures_data)} "
                    f"prev_price={prev_price:.2f} curr_price={curr_price:.2f} "
                    f"prev_oi={prev_oi:,.0f} curr_oi={curr_oi:,.0f} "
                    f"price_threshold={price_threshold:.3f}% oi_threshold={oi_threshold:.3f}%"
                )

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

                logger.debug(
                    f"[FUT_ACT] {stock.stock_symbol} | CONDITION expiry={expiry} "
                    f"price_pct={price_percentage:+.2f}% (thresh={price_threshold:.3f}%) "
                    f"oi_pct={oi_percentage:+.2f}% (thresh={oi_threshold:.3f}%) "
                    f"→ action={action or 'none'}"
                )

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

                    logger.info(
                        f"[FUT_ACT] {stock.stock_symbol} — {action} {sentiment} | "
                        f"price={price_percentage:+.2f}% oi={oi_percentage:+.2f}% "
                        f"score={score_result['total_score']} confidence={score_result['confidence']} expiry={expiry}"
                    )
                    return True

                return False

            zerodha_ctx = stock.zerodha_ctx

            futures_data_curr = zerodha_ctx["futures_data"]["current"]
            futures_data_next = zerodha_ctx["futures_data"]["next"]
            res = False

            if get_future_action(futures_data_curr, expiry='current'):
                res = True

            # Enable next expiry analysis for roll detection
            if futures_data_next is not None and not futures_data_next.empty:
                if get_future_action(futures_data_next, expiry='next'):
                    res = True

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
            logger.debug(f"[FUT_PVO] {stock.stock_symbol} — start")

            zerodha_ctx = stock.zerodha_ctx
            fut_curr = zerodha_ctx["futures_data"]["current"]
            if fut_curr is None or fut_curr.empty:
                logger.debug(f"[FUT_PVO] {stock.stock_symbol} — futures_data None/empty, skip")
                return False
            if len(fut_curr) < 2:
                logger.debug(f"[FUT_PVO] {stock.stock_symbol} — insufficient rows (rows={len(fut_curr)} < 2), skip")
                return False

            prev = fut_curr.iloc[-2]
            curr = fut_curr.iloc[-1]

            prev_close = prev.get("close"); curr_close = curr.get("close")
            prev_vol = prev.get("volume", 0); curr_vol = curr.get("volume", 0)
            prev_oi = prev.get("oi", 0); curr_oi = curr.get("oi", 0)
            if None in (prev_close, curr_close):
                logger.debug(f"[FUT_PVO] {stock.stock_symbol} — prev/curr close missing, skip")
                return False

            price_pct = percentageChange(curr_close, prev_close)
            vol_pct = percentageChange(curr_vol, prev_vol) if prev_vol else 0
            oi_pct = percentageChange(curr_oi, prev_oi) if prev_oi else 0

            # Get dynamic thresholds
            price_threshold, oi_threshold = self.get_dynamic_thresholds(stock, fut_curr)

            FLAT_PRICE_THRESH = max(price_threshold * 0.3, 0.15)
            FLAT_VOL_OI_THRESH = max(oi_threshold * 0.3, 0.2)

            logger.debug(
                f"[FUT_PVO] {stock.stock_symbol} | SOURCE rows={len(fut_curr)} "
                f"prev_close={prev_close:.2f} curr_close={curr_close:.2f} "
                f"prev_vol={prev_vol:,.0f} curr_vol={curr_vol:,.0f} "
                f"prev_oi={prev_oi:,.0f} curr_oi={curr_oi:,.0f}"
            )

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

            # Directional confirmation patterns (price + volume + OI all agree)
            if price_up and vol_incr and oi_incr:
                # Price up, vol surge, OI building → fresh long buildup, strong BULLISH
                pattern = "price_up_vol_oi_incr"
                sentiment = "BULLISH"
            elif price_down and vol_incr and oi_incr:
                # Price down, vol surge, OI building → fresh short buildup, strong BEARISH
                pattern = "price_down_vol_oi_incr"
                sentiment = "BEARISH"
            elif price_up and vol_incr and oi_dec:
                # Price up, vol surge, OI falling → short covering, BULLISH
                pattern = "price_up_vol_incr_oi_dec"
                sentiment = "BULLISH"
            elif price_down and vol_incr and oi_dec:
                # Price down, vol surge, OI falling → long unwinding, BEARISH
                pattern = "price_down_vol_incr_oi_dec"
                sentiment = "BEARISH"
            # Momentum without OI (retail/momentum driven, no institutional confirmation)
            elif price_up and vol_incr and oi_flat:
                # Price up, volume surge, OI unchanged → retail momentum, no new positions
                pattern = "price_up_vol_incr_oi_flat"
            elif price_down and vol_incr and oi_flat:
                # Price down, volume surge, OI unchanged → sell-off without fresh shorts
                pattern = "price_down_vol_incr_oi_flat"
            # Divergence / weak-move patterns (price moves without participation)
            elif price_up and vol_flat and oi_flat:
                pattern = "price_up_vol_oi_flat"
            elif price_down and vol_flat and oi_flat:
                pattern = "price_down_vol_oi_flat"
            # Absorption / pre-breakout patterns (price flat, participation changes)
            elif price_flat and vol_incr and oi_incr:
                pattern = "price_flat_vol_oi_incr"
            elif price_flat and vol_dec and oi_dec:
                pattern = "price_flat_vol_oi_dec"

            logger.debug(
                f"[FUT_PVO] {stock.stock_symbol} | CONDITION "
                f"price_pct={price_pct:+.2f}% (up={price_up} down={price_down} flat={price_flat} thresh={price_threshold:.3f}%/{FLAT_PRICE_THRESH:.3f}%) "
                f"vol_pct={vol_pct:+.2f}% (flat={vol_flat} incr={vol_incr} dec={vol_dec}) "
                f"oi_pct={oi_pct:+.2f}% (flat={oi_flat} incr={oi_incr} dec={oi_dec} thresh={oi_threshold:.3f}%/{FLAT_VOL_OI_THRESH:.3f}%) "
                f"→ pattern={pattern or 'none'}"
            )

            if not pattern:
                return False

            # Calculate signal score — include vol_confirm for directional patterns
            pattern_data = namedtuple('PatternData', ['price_percentage', 'oi_percentage', 'oi_confirm', 'vol_confirm'])(
                price_percentage=price_pct,
                oi_percentage=oi_pct,
                oi_confirm=oi_incr or oi_dec,
                vol_confirm=vol_incr,
            )
            score_result = self.calculate_signal_score(
                stock, pattern_data, fut_curr, f"pvo_{pattern}"
            )

            result_tuple = PatternTuple(
                pattern, price_pct, vol_pct, oi_pct, "current",
                mtf_analysis['aligned'], score_result['total_score'], score_result['confidence']
            )

            stock.set_analysis(sentiment, "FUTURE_PVO_PATTERN", result_tuple)

            logger.info(
                f"[FUT_PVO] {stock.stock_symbol} — {pattern} {sentiment} | "
                f"price={price_pct:+.2f}% vol={vol_pct:+.2f}% oi={oi_pct:+.2f}% "
                f"score={score_result['total_score']} mtf_aligned={mtf_analysis['aligned']}"
            )
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
            logger.debug(f"[FUT_ORB] {stock.stock_symbol} — start")

            zerodha_ctx = stock.zerodha_ctx
            fut_curr = zerodha_ctx["futures_data"]["current"]
            if fut_curr is None or fut_curr.empty:
                logger.debug(f"[FUT_ORB] {stock.stock_symbol} — futures_data None/empty, skip")
                return False

            # Ensure sorted by index (time)
            fut_curr = fut_curr.sort_index()

            # Intraday subset (assumes only today present; if not, filter by date)
            last_dt = fut_curr.index[-1]
            today_str = last_dt.strftime("%Y-%m-%d")
            fut_today = fut_curr[[d.strftime("%Y-%m-%d") == today_str for d in fut_curr.index]]

            # Validate ORB starts at market open (9:15 IST) — log only once per session
            first_bar_time = fut_today.index[0]
            if hasattr(first_bar_time, 'hour'):
                if not (first_bar_time.hour == 9 and first_bar_time.minute == 15):
                    if not FuturesAnalyser._orb_open_time_warned:
                        logger.debug(
                            f"[FUT_ORB] {stock.stock_symbol} — first bar not at 09:15 "
                            f"({first_bar_time.strftime('%H:%M')}), ORB disabled for this session"
                        )
                        FuturesAnalyser._orb_open_time_warned = True
                    return False

            # Dynamic ORB period based on volatility
            atr_pct = self.calculate_atr_percentage(fut_curr, self.ATR_PERIOD)
            if atr_pct > 1.0:  # High volatility
                ORB_CANDLES = 5  # Longer range for volatile stocks
            else:
                ORB_CANDLES = self.ORB_CANDLES

            # Buffer = 0.3% of ORB high — ensures price has genuinely cleared the range
            BREAKOUT_BUFFER_PCT = 0.30
            price_threshold, oi_threshold = self.get_dynamic_thresholds(stock, fut_curr)
            OI_CONFIRM_PCT = max(oi_threshold, 0.5)
            VOL_ROLL_N = 5
            VOL_FACTOR = 1.2

            if len(fut_today) < ORB_CANDLES + 2:
                logger.debug(
                    f"[FUT_ORB] {stock.stock_symbol} — insufficient today candles "
                    f"(today={len(fut_today)} < orb_candles+2={ORB_CANDLES + 2}), skip"
                )
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
                logger.debug(f"[FUT_ORB] {stock.stock_symbol} — close/oi missing, skip")
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

            logger.debug(
                f"[FUT_ORB] {stock.stock_symbol} | SOURCE today_candles={len(fut_today)} orb_candles={ORB_CANDLES} "
                f"orbH={orb_high:.2f} orbL={orb_low:.2f} bufH={buffer_high_level:.2f} bufL={buffer_low_level:.2f} "
                f"atr_pct={atr_pct:.3f}% last_close={last_close:.2f} "
                f"last_oi={last_oi:,.0f} prev_oi={prev_oi:,.0f} "
                f"last_vol={last_vol:,.0f} vol_avg={vol_avg:.0f} "
                f"oi_thresh={OI_CONFIRM_PCT:.3f}%"
            )

            pattern = None
            sentiment = "NEUTRAL"

            if breakout_up and not FuturesAnalyser._orb_fired_up:
                if oi_confirm and vol_confirm:
                    pattern = "orb_breakout_up_oi_confirmed"
                    sentiment = "BULLISH"
            elif breakout_down and not FuturesAnalyser._orb_fired_down:
                if oi_confirm and vol_confirm:
                    pattern = "orb_breakout_down_oi_confirmed"
                    sentiment = "BEARISH"

            logger.debug(
                f"[FUT_ORB] {stock.stock_symbol} | CONDITION "
                f"breakout_up={breakout_up} breakout_down={breakout_down} "
                f"already_fired_up={FuturesAnalyser._orb_fired_up} already_fired_down={FuturesAnalyser._orb_fired_down} "
                f"oi_pct={oi_pct:+.2f}% (need>{OI_CONFIRM_PCT:.3f}%) oi_confirm={oi_confirm} "
                f"vol={last_vol:,.0f} vol_avg={vol_avg:.0f} (need>{VOL_FACTOR}x) vol_confirm={vol_confirm} "
                f"→ pattern={pattern or 'none'}"
            )

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

            # Mark as fired so this direction doesn't re-fire every cycle
            if sentiment == "BULLISH":
                FuturesAnalyser._orb_fired_up = True
            else:
                FuturesAnalyser._orb_fired_down = True

            logger.info(
                f"[FUT_ORB] {stock.stock_symbol} — {pattern} {sentiment} | "
                f"close={last_close:.2f} orbH={orb_high:.2f} orbL={orb_low:.2f} "
                f"OI%={oi_pct:+.2f} vol={last_vol:,.0f} avgVol={vol_avg:.0f} "
                f"oi_confirm={oi_confirm} vol_confirm={vol_confirm} "
                f"score={score_result['total_score']} mtf={mtf_analysis['aligned']}"
            )
            return True

        except Exception as e:
            logger.error(f"Error in analyse_intraday_breakout_oi_confirmation for {stock.stock_symbol}: {e}")
            logger.error(traceback.format_exc())
            return False

    # ==================== New Positional Methods ====================

    @BaseAnalyzer.positional
    @BaseAnalyzer.index_positional
    def analyse_positional_oi_trend(self, stock: Stock):
        """
        Detect multi-day OI buildup/unwinding trends using the 55-row positional dataset.

        Uses 10-day and 20-day OI slopes plus cumulative % change to classify:
        - LONG_BUILDUP_TREND:    OI rising + price rising  (sustained bullish)
        - SHORT_BUILDUP_TREND:   OI rising + price falling (sustained bearish)
        - SHORT_COVERING_TREND:  OI falling + price rising (shorts unwinding)
        - LONG_UNWINDING_TREND:  OI falling + price falling (longs exiting)

        Requires at least 10 clean rows after startup noise is removed.
        """
        try:
            logger.debug(f"[FUT_POS_OI] {stock.stock_symbol} — start")

            zerodha_ctx = stock.zerodha_ctx
            fut = zerodha_ctx["futures_data"]["current"]
            if fut is None or fut.empty:
                logger.debug(f"[FUT_POS_OI] {stock.stock_symbol} — futures_data empty, skip")
                return False

            # Skip startup rows — 5% filter for short window decision logic
            max_oi = fut["oi"].max()
            if max_oi <= 0:
                logger.debug(f"[FUT_POS_OI] {stock.stock_symbol} — oi all zero, skip")
                return False
            df = fut[fut["oi"] >= max_oi * self.OI_STARTUP_FRACTION].copy()

            if len(df) < 10:
                logger.debug(f"[FUT_POS_OI] {stock.stock_symbol} — insufficient mature rows ({len(df)} < 10), skip")
                return False

            # Short window (10 rows) — used for signal decision
            short = df.tail(10)
            oi_chg_10d    = (short["oi"].iloc[-1]    - short["oi"].iloc[0])    / short["oi"].iloc[0]    * 100
            price_chg_10d = (short["close"].iloc[-1] - short["close"].iloc[0]) / short["close"].iloc[0] * 100

            # Long window (20 rows) — stricter 20% OI floor to avoid startup slope
            # contaminating the 20d view. Informational only — not used for decision.
            df_long = fut[fut["oi"] >= max_oi * 0.20].copy()
            if len(df_long) >= 2:
                long          = df_long.tail(min(20, len(df_long)))
                oi_chg_20d    = (long["oi"].iloc[-1]    - long["oi"].iloc[0])    / long["oi"].iloc[0]    * 100
                price_chg_20d = (long["close"].iloc[-1] - long["close"].iloc[0]) / long["close"].iloc[0] * 100
            else:
                oi_chg_20d    = float("nan")
                price_chg_20d = float("nan")

            oi_20d_s    = f"{oi_chg_20d:+.2f}%"    if not (oi_chg_20d != oi_chg_20d)    else "N/A"
            price_20d_s = f"{price_chg_20d:+.2f}%" if not (price_chg_20d != price_chg_20d) else "N/A"
            logger.debug(
                f"[FUT_POS_OI] {stock.stock_symbol} | SOURCE rows={len(df)} "
                f"oi_10d={oi_chg_10d:+.2f}% oi_20d={oi_20d_s} "
                f"price_10d={price_chg_10d:+.2f}% price_20d={price_20d_s}"
            )

            # Threshold: OI must move at least 5% over the window to be meaningful
            OI_TREND_THRESHOLD    = 5.0
            PRICE_TREND_THRESHOLD = self.FUTURE_PRICE_CHANGE_PERCENTAGE

            oi_rising   = oi_chg_10d > OI_TREND_THRESHOLD
            oi_falling  = oi_chg_10d < -OI_TREND_THRESHOLD
            price_up    = price_chg_10d > PRICE_TREND_THRESHOLD
            price_down  = price_chg_10d < -PRICE_TREND_THRESHOLD

            action    = None
            sentiment = None

            if oi_rising and price_up:
                action = "LONG_BUILDUP_TREND"
                sentiment = "BULLISH"
            elif oi_rising and price_down:
                action = "SHORT_BUILDUP_TREND"
                sentiment = "BEARISH"
            elif oi_falling and price_up:
                action = "SHORT_COVERING_TREND"
                sentiment = "BULLISH"
            elif oi_falling and price_down:
                action = "LONG_UNWINDING_TREND"
                sentiment = "BEARISH"

            logger.debug(
                f"[FUT_POS_OI] {stock.stock_symbol} | CONDITION "
                f"oi_rising={oi_rising} oi_falling={oi_falling} "
                f"price_up={price_up} price_down={price_down} "
                f"oi_thresh={OI_TREND_THRESHOLD}% price_thresh={PRICE_TREND_THRESHOLD}% "
                f"→ action={action or 'none'}"
            )

            if not action:
                return False

            import math
            OITrendTuple = namedtuple("FuturesOITrend", [
                "action", "oi_chg_10d", "oi_chg_20d",
                "price_chg_10d", "price_chg_20d",
            ])
            result = OITrendTuple(
                action=action,
                oi_chg_10d=round(oi_chg_10d, 2),
                oi_chg_20d=round(oi_chg_20d, 2) if not math.isnan(oi_chg_20d) else None,
                price_chg_10d=round(price_chg_10d, 2),
                price_chg_20d=round(price_chg_20d, 2) if not math.isnan(price_chg_20d) else None,
            )
            stock.set_analysis(sentiment, "FUTURE_OI_TREND", result)
            logger.info(
                f"[FUT_POS_OI] {stock.stock_symbol} — {action} {sentiment} | "
                f"oi_10d={oi_chg_10d:+.2f}% oi_20d={oi_20d_s} "
                f"price_10d={price_chg_10d:+.2f}% price_20d={price_20d_s}"
            )
            return True

        except Exception as e:
            logger.error(f"Error in analyse_positional_oi_trend for {stock.stock_symbol}: {e}")
            logger.error(traceback.format_exc())
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_positional_cost_of_carry(self, stock: Stock):
        """
        Compute futures basis (cost of carry) from underlying_price vs futures close.

        Runs in BOTH modes:
        - Intraday: detects live backwardation from 5-min candles (1 row minimum).
          Only BACKWARDATION fires — trend signals need multiple days.
        - Positional: full signals — BACKWARDATION, HIGH_COST_OF_CARRY, BASIS_EXPANDING.
          Requires 3+ rows. Annualised CoC suppressed when days_to_expiry < 10 (misleading).

        Signals:
        - BACKWARDATION:      basis < -0.05%  → futures below spot, bearish institutional
        - HIGH_COST_OF_CARRY: ann CoC > 15%   → premium overheated (positional only)
        - BASIS_EXPANDING:    basis growing near expiry → conviction (positional only)
        """
        try:
            logger.debug(f"[FUT_COC] {stock.stock_symbol} — start")

            is_positional = shared.app_ctx.mode == shared.Mode.POSITIONAL
            zerodha_ctx = stock.zerodha_ctx
            fut = zerodha_ctx["futures_data"]["current"]
            if fut is None or fut.empty:
                logger.debug(f"[FUT_COC] {stock.stock_symbol} — futures_data empty, skip")
                return False

            if "underlying_price" not in fut.columns:
                logger.debug(f"[FUT_COC] {stock.stock_symbol} — no underlying_price column, skip")
                return False

            df = fut.dropna(subset=["close", "underlying_price"]).copy()
            # If underlying_price == close for all rows, spot data is not populated
            if (df["underlying_price"] == df["close"]).all():
                logger.debug(f"[FUT_COC] {stock.stock_symbol} — underlying_price equals close (spot not populated), skip")
                return False

            # Intraday: 1 row is enough to check backwardation
            # Positional: need 3 rows for trend/mean analysis
            min_rows = 3 if is_positional else 1
            if len(df) < min_rows:
                logger.debug(f"[FUT_COC] {stock.stock_symbol} — insufficient rows ({len(df)} < {min_rows}), skip")
                return False

            df["basis"]     = df["close"] - df["underlying_price"]
            df["basis_pct"] = df["basis"] / df["underlying_price"] * 100

            curr_basis_pct = float(df["basis_pct"].iloc[-1])
            prev_basis_pct = float(df["basis_pct"].iloc[-2]) if len(df) >= 2 else curr_basis_pct
            basis_5d_mean  = float(df["basis_pct"].tail(5).mean()) if is_positional else curr_basis_pct
            basis_trend    = curr_basis_pct - prev_basis_pct  # positive = expanding

            # Days to expiry — suppress annualised CoC when < 10 days (meaningless near expiry)
            days_to_expiry = None
            fmdata = zerodha_ctx.get("futures_mdata", {}).get("current")
            if fmdata is not None and not fmdata.empty and "expiry" in fmdata.columns:
                from datetime import date
                expiry_date = fmdata["expiry"].iloc[0]
                days_to_expiry = (expiry_date - date.today()).days

            ann_coc = None
            if is_positional and days_to_expiry and days_to_expiry >= 10:
                ann_coc = curr_basis_pct / days_to_expiry * 365

            ann_coc_s = f"{ann_coc:.1f}%" if ann_coc is not None else "N/A"
            logger.debug(
                f"[FUT_COC] {stock.stock_symbol} | SOURCE rows={len(df)} mode={'POS' if is_positional else 'INTRA'} "
                f"curr_basis_pct={curr_basis_pct:+.3f}% prev={prev_basis_pct:+.3f}% "
                f"5d_mean={basis_5d_mean:+.3f}% days_to_expiry={days_to_expiry} ann_coc={ann_coc_s}"
            )

            action    = None
            sentiment = None

            if curr_basis_pct < -0.05:
                # Futures below spot — backwardation. Valid in both modes.
                action = "BACKWARDATION"
                sentiment = "BEARISH"
            elif is_positional and ann_coc is not None and ann_coc > 15.0:
                # Premium overheated — positional only
                action = "HIGH_COST_OF_CARRY"
                sentiment = "BEARISH"
            elif (is_positional
                  and days_to_expiry is not None and days_to_expiry <= 10
                  and basis_trend > 0.05 and curr_basis_pct > 0.10):
                # Basis expanding approaching expiry = conviction — positional only
                action = "BASIS_EXPANDING"
                sentiment = "BULLISH"

            logger.debug(
                f"[FUT_COC] {stock.stock_symbol} | CONDITION "
                f"backwardation={curr_basis_pct < -0.05} "
                f"high_coc={ann_coc is not None and ann_coc > 15.0} "
                f"basis_expanding={is_positional and (days_to_expiry or 99) <= 10 and basis_trend > 0.05} "
                f"→ action={action or 'none'}"
            )

            if not action:
                return False

            COCTuple = namedtuple("FuturesCostOfCarry", [
                "action", "basis_pct", "basis_5d_mean",
                "basis_trend", "ann_coc", "days_to_expiry",
            ])
            result = COCTuple(
                action=action,
                basis_pct=round(curr_basis_pct, 3),
                basis_5d_mean=round(basis_5d_mean, 3),
                basis_trend=round(basis_trend, 3),
                ann_coc=round(ann_coc, 2) if ann_coc is not None else None,
                days_to_expiry=days_to_expiry,
            )
            stock.set_analysis(sentiment, "FUTURE_COST_OF_CARRY", result)
            logger.info(
                f"[FUT_COC] {stock.stock_symbol} — {action} {sentiment} | "
                f"basis={curr_basis_pct:+.3f}% ann_coc={ann_coc_s} "
                f"days_to_expiry={days_to_expiry}"
            )
            return True

        except Exception as e:
            logger.error(f"Error in analyse_positional_cost_of_carry for {stock.stock_symbol}: {e}")
            logger.error(traceback.format_exc())
            return False

    @BaseAnalyzer.positional
    @BaseAnalyzer.index_positional
    def analyse_positional_rollover_pressure(self, stock: Stock):
        """
        Detect expiry rollover using OI ratio between current and next contract.

        curr_oi / next_oi falling below 2x in the last 5 trading days before expiry
        indicates active rollover — suppresses FUT_ACT directional signals on current.

        Signals:
        - ROLLOVER_ACTIVE:   ratio < 2x  → current contract dying, next dominant
        - ROLLOVER_STARTING: ratio 2–4x  → rollover beginning, treat signals cautiously
        """
        try:
            logger.debug(f"[FUT_ROLL] {stock.stock_symbol} — start")

            zerodha_ctx = stock.zerodha_ctx
            fut_curr = zerodha_ctx["futures_data"]["current"]
            fut_next = zerodha_ctx["futures_data"]["next"]

            if fut_curr is None or fut_curr.empty:
                logger.debug(f"[FUT_ROLL] {stock.stock_symbol} — current futures_data empty, skip")
                return False
            if fut_next is None or fut_next.empty:
                logger.debug(f"[FUT_ROLL] {stock.stock_symbol} — next futures_data empty, skip")
                return False

            curr_oi = float(fut_curr["oi"].iloc[-1] or 0)
            next_oi = float(fut_next["oi"].iloc[-1] or 0)

            if next_oi <= 0:
                logger.debug(f"[FUT_ROLL] {stock.stock_symbol} — next OI is zero, skip")
                return False

            ratio = curr_oi / next_oi

            # Trend: is the ratio falling over last 5 rows of current contract?
            if len(fut_curr) >= 3 and len(fut_next) >= 3:
                n = min(5, len(fut_curr), len(fut_next))
                past_curr = float(fut_curr["oi"].iloc[-n] or 0)
                past_next = float(fut_next["oi"].iloc[-n] or 0)
                past_ratio = past_curr / past_next if past_next > 0 else ratio
                ratio_trend = ratio - past_ratio   # negative = ratio falling = rollover accelerating
            else:
                ratio_trend = 0.0
                past_ratio  = ratio

            logger.debug(
                f"[FUT_ROLL] {stock.stock_symbol} | SOURCE "
                f"curr_oi={curr_oi:,.0f} next_oi={next_oi:,.0f} "
                f"ratio={ratio:.2f}x past_ratio={past_ratio:.2f}x ratio_trend={ratio_trend:+.2f}"
            )

            action    = None
            sentiment = "NEUTRAL"

            if ratio < 2.0:
                action = "ROLLOVER_ACTIVE"
            elif ratio < 4.0 and ratio_trend < -0.5:
                action = "ROLLOVER_STARTING"

            logger.debug(
                f"[FUT_ROLL] {stock.stock_symbol} | CONDITION "
                f"ratio={ratio:.2f}x (active<2.0 starting<4.0) "
                f"ratio_trend={ratio_trend:+.2f} → action={action or 'none'}"
            )

            if not action:
                return False

            RolloverTuple = namedtuple("FuturesRollover", [
                "action", "curr_oi", "next_oi",
                "ratio", "ratio_trend",
            ])
            result = RolloverTuple(
                action=action,
                curr_oi=int(curr_oi),
                next_oi=int(next_oi),
                ratio=round(ratio, 2),
                ratio_trend=round(ratio_trend, 2),
            )
            stock.set_analysis(sentiment, "FUTURE_ROLLOVER", result)
            logger.info(
                f"[FUT_ROLL] {stock.stock_symbol} — {action} {sentiment} | "
                f"curr_oi={curr_oi:,.0f} next_oi={next_oi:,.0f} "
                f"ratio={ratio:.2f}x trend={ratio_trend:+.2f}"
            )
            return True

        except Exception as e:
            logger.error(f"Error in analyse_positional_rollover_pressure for {stock.stock_symbol}: {e}")
            logger.error(traceback.format_exc())
            return False

    # ==================== New Intraday Methods ====================

    @BaseAnalyzer.intraday
    @BaseAnalyzer.index_intraday
    def analyse_intraday_oi_buildup_from_open(self, stock: Stock):
        """
        Compare current OI against session-open OI rather than prev candle.

        Candle-to-candle OI noise (±0.06% std) rarely reaches threshold. But
        sustained OI accumulation from open (+1.5% over the session) is a
        reliable signal of institutional positioning.

        Signals:
        - OI_BUILDUP_FROM_OPEN:   OI rose ≥1.5% from open + price up   → BULLISH
        - OI_SHORT_BUILD_FROM_OPEN: OI rose ≥1.5% from open + price down → BEARISH
        - OI_UNWINDING_FROM_OPEN: OI fell ≥1.5% from open + price move  → opposite direction
        """
        try:
            logger.debug(f"[FUT_OI_OPEN] {stock.stock_symbol} — start")

            zerodha_ctx = stock.zerodha_ctx
            fut = zerodha_ctx["futures_data"]["current"]
            if fut is None or fut.empty:
                logger.debug(f"[FUT_OI_OPEN] {stock.stock_symbol} — futures_data empty, skip")
                return False
            if len(fut) < 3:
                logger.debug(f"[FUT_OI_OPEN] {stock.stock_symbol} — insufficient rows ({len(fut)} < 3), skip")
                return False

            from datetime import datetime
            today_str = datetime.now().strftime("%Y-%m-%d")

            # Cache session-open OI (first bar of today)
            if FuturesAnalyser._session_date != today_str or FuturesAnalyser._session_open_oi <= 0:
                open_oi = float(fut["oi"].iloc[0] or 0)
                if open_oi <= 0:
                    logger.debug(f"[FUT_OI_OPEN] {stock.stock_symbol} — open OI is zero, skip")
                    return False
                FuturesAnalyser._session_open_oi = open_oi
                FuturesAnalyser._session_date    = today_str

            open_oi    = FuturesAnalyser._session_open_oi
            curr_oi    = float(fut["oi"].iloc[-1] or 0)
            curr_close = float(fut["close"].iloc[-1])
            open_close = float(fut["close"].iloc[0])

            if open_oi <= 0 or curr_oi <= 0:
                logger.debug(f"[FUT_OI_OPEN] {stock.stock_symbol} — zero OI, skip")
                return False

            oi_from_open_pct    = (curr_oi - open_oi) / open_oi * 100
            price_from_open_pct = (curr_close - open_close) / open_close * 100

            logger.debug(
                f"[FUT_OI_OPEN] {stock.stock_symbol} | SOURCE rows={len(fut)} "
                f"open_oi={open_oi:,.0f} curr_oi={curr_oi:,.0f} "
                f"oi_from_open={oi_from_open_pct:+.2f}% "
                f"open_close={open_close:.2f} curr_close={curr_close:.2f} "
                f"price_from_open={price_from_open_pct:+.2f}%"
            )

            OI_OPEN_THRESHOLD    = 1.5   # 1.5% sustained buildup is meaningful
            PRICE_OPEN_THRESHOLD = self.FUTURE_PRICE_CHANGE_PERCENTAGE

            oi_building  = oi_from_open_pct >= OI_OPEN_THRESHOLD
            oi_unwinding = oi_from_open_pct <= -OI_OPEN_THRESHOLD
            price_up     = price_from_open_pct > PRICE_OPEN_THRESHOLD
            price_down   = price_from_open_pct < -PRICE_OPEN_THRESHOLD

            action    = None
            sentiment = None

            if oi_building and price_up:
                action = "OI_BUILDUP_FROM_OPEN"
                sentiment = "BULLISH"
            elif oi_building and price_down:
                action = "OI_SHORT_BUILD_FROM_OPEN"
                sentiment = "BEARISH"
            elif oi_unwinding and price_up:
                action = "OI_UNWINDING_FROM_OPEN_UP"
                sentiment = "BULLISH"
            elif oi_unwinding and price_down:
                action = "OI_UNWINDING_FROM_OPEN_DOWN"
                sentiment = "BEARISH"

            logger.debug(
                f"[FUT_OI_OPEN] {stock.stock_symbol} | CONDITION "
                f"oi_building={oi_building} oi_unwinding={oi_unwinding} "
                f"price_up={price_up} price_down={price_down} "
                f"oi_thresh={OI_OPEN_THRESHOLD}% price_thresh={PRICE_OPEN_THRESHOLD}% "
                f"→ action={action or 'none'}"
            )

            if not action:
                return False

            OIOpenTuple = namedtuple("FuturesOIFromOpen", [
                "action", "oi_from_open_pct", "price_from_open_pct",
                "open_oi", "curr_oi",
            ])
            result = OIOpenTuple(
                action=action,
                oi_from_open_pct=round(oi_from_open_pct, 2),
                price_from_open_pct=round(price_from_open_pct, 2),
                open_oi=int(open_oi),
                curr_oi=int(curr_oi),
            )
            stock.set_analysis(sentiment, "FUTURE_OI_FROM_OPEN", result)
            logger.info(
                f"[FUT_OI_OPEN] {stock.stock_symbol} — {action} {sentiment} | "
                f"oi_from_open={oi_from_open_pct:+.2f}% "
                f"price_from_open={price_from_open_pct:+.2f}%"
            )
            return True

        except Exception as e:
            logger.error(f"Error in analyse_intraday_oi_buildup_from_open for {stock.stock_symbol}: {e}")
            logger.error(traceback.format_exc())
            return False
