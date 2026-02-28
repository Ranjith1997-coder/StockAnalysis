import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
from common.helperFunctions import percentageChange
from collections import namedtuple
import pandas as pd
import numpy as np
import common.shared as shared


class VolumeAnalyser(BaseAnalyzer):
    """Enhanced Volume Analyser with multiple volume-based strategies."""
    
    # Volume Breakout parameters
    TIMES_VOLUME = 2.0  # Volume should be 2x average (reduced from 10x)
    VOLUME_PRICE_THRESHOLD = 2.0  # Price change threshold %
    VOLUME_MA_PERIOD = 20  # Volume moving average period
    
    # OBV Divergence parameters
    OBV_LOOKBACK = 30  # Lookback period for divergence detection
    OBV_SWING_ORDER = 3  # Swing detection sensitivity
    OBV_MIN_DIVERGENCE = 5  # Minimum OBV % difference for valid divergence
    
    # Volume Climax parameters
    CLIMAX_VOLUME_MULT = 3.0  # Volume should be 3x average for climax
    CLIMAX_LOOKBACK = 10  # Days to look back for sustained move
    
    def __init__(self) -> None:
        self.analyserName = "Volume Analyser"
        super().__init__()
    
    def reset_constants(self):
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            VolumeAnalyser.VOLUME_PRICE_THRESHOLD = 0.5   
            VolumeAnalyser.TIMES_VOLUME = 1.5  # 1.5x for intraday (more sensitive)
            VolumeAnalyser.VOLUME_MA_PERIOD = 20
            VolumeAnalyser.OBV_LOOKBACK = 20
            VolumeAnalyser.CLIMAX_VOLUME_MULT = 2.5
        else:
            VolumeAnalyser.VOLUME_PRICE_THRESHOLD = 2.0  
            VolumeAnalyser.TIMES_VOLUME = 2.0  # 2x for positional
            VolumeAnalyser.VOLUME_MA_PERIOD = 20
            VolumeAnalyser.OBV_LOOKBACK = 30
            VolumeAnalyser.CLIMAX_VOLUME_MULT = 3.0
        logger.debug(f"VolumeAnalyser constants reset for mode {shared.app_ctx.mode.name}")
        logger.debug(f"TIMES_VOLUME = {VolumeAnalyser.TIMES_VOLUME}, VOLUME_PRICE_THRESHOLD = {VolumeAnalyser.VOLUME_PRICE_THRESHOLD}")

    # ==================== IMPROVED VOLUME BREAKOUT ====================
    
    @BaseAnalyzer.both
    def analyse_volume_breakout(self, stock: Stock):
        """
        Enhanced Volume Breakout Strategy.
        
        Detects significant volume spikes with price confirmation.
        
        BULLISH Signal:
        - Volume > TIMES_VOLUME × 20-day average volume
        - Volume > previous day's volume
        - Price increased > VOLUME_PRICE_THRESHOLD
        - Volume trend is rising (confirmation)
        
        BEARISH Signal:
        - Volume > TIMES_VOLUME × 20-day average volume
        - Volume > previous day's volume
        - Price decreased > VOLUME_PRICE_THRESHOLD
        - Volume trend is rising (confirmation)
        """
        try:
            logger.debug(f'Inside analyse_volume_breakout for stock {stock.stock_symbol}')
            
            price_data = stock.priceData
            if len(price_data) < VolumeAnalyser.VOLUME_MA_PERIOD + 5:
                return False
            
            volume = price_data['Volume']
            close = price_data['Close']
            
            # Current and previous data
            curr_vol = volume.iloc[-1]
            prev_vol = volume.iloc[-2]
            curr_price = close.iloc[-1]
            prev_price = close.iloc[-2]
            
            # Calculate volume moving average
            vol_ma = volume.iloc[-VolumeAnalyser.VOLUME_MA_PERIOD:].mean()
            
            # Calculate volume trend (is volume increasing over last 3 days?)
            vol_trend_rising = (volume.iloc[-1] > volume.iloc[-2] and 
                               volume.iloc[-2] > volume.iloc[-3])
            
            # Volume conditions
            vol_above_ma = curr_vol > VolumeAnalyser.TIMES_VOLUME * vol_ma
            vol_above_prev = curr_vol > prev_vol
            
            # Price change
            price_change_pct = percentageChange(curr_price, prev_price)
            
            VolumeBreakoutAnalysis = namedtuple("VolumeBreakoutAnalysis", [
                "volume", "volume_ma", "volume_ratio",
                "price_change_pct", "volume_trend"
            ])
            
            # BULLISH: High volume + price breakout up
            if (vol_above_ma and vol_above_prev and 
                price_change_pct > VolumeAnalyser.VOLUME_PRICE_THRESHOLD and
                vol_trend_rising):
                
                vol_ratio = curr_vol / vol_ma
                stock.set_analysis("BULLISH", "VOLUME_BREAKOUT", 
                    VolumeBreakoutAnalysis(
                        volume=curr_vol,
                        volume_ma=vol_ma,
                        volume_ratio=vol_ratio,
                        price_change_pct=price_change_pct,
                        volume_trend="rising"
                    ))
                logger.info(f"Volume Breakout BULLISH for {stock.stock_symbol}: "
                           f"vol={curr_vol:.0f}, ma={vol_ma:.0f}, ratio={vol_ratio:.1f}x, "
                           f"price={price_change_pct:.2f}%")
                return True
            
            # BEARISH: High volume + price breakdown
            elif (vol_above_ma and vol_above_prev and 
                  price_change_pct < -VolumeAnalyser.VOLUME_PRICE_THRESHOLD and
                  vol_trend_rising):
                
                vol_ratio = curr_vol / vol_ma
                stock.set_analysis("BEARISH", "VOLUME_BREAKOUT", 
                    VolumeBreakoutAnalysis(
                        volume=curr_vol,
                        volume_ma=vol_ma,
                        volume_ratio=vol_ratio,
                        price_change_pct=price_change_pct,
                        volume_trend="rising"
                    ))
                logger.info(f"Volume Breakout BEARISH for {stock.stock_symbol}: "
                           f"vol={curr_vol:.0f}, ma={vol_ma:.0f}, ratio={vol_ratio:.1f}x, "
                           f"price={price_change_pct:.2f}%")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_volume_breakout for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    # ==================== OBV DIVERGENCE ====================
    
    def _calculate_obv(self, price_data: pd.DataFrame) -> pd.Series:
        """
        Calculate On-Balance Volume (OBV).
        
        OBV = Previous OBV + Volume (if close > prev close)
        OBV = Previous OBV - Volume (if close < prev close)
        OBV = Previous OBV (if close = prev close)
        """
        close = price_data['Close']
        volume = price_data['Volume']
        
        direction = np.sign(close.diff()).fillna(0)
        obv = (volume * direction).cumsum()

        return obv
    
    @BaseAnalyzer.both
    def analyse_obv_divergence(self, stock: Stock):
        """
        OBV Divergence Detection - High conviction reversal signal.
        
        BULLISH Divergence:
        - Price makes lower low
        - OBV makes higher low
        - Indicates accumulation (smart money buying)
        
        BEARISH Divergence:
        - Price makes higher high
        - OBV makes lower high
        - Indicates distribution (smart money selling)
        
        Enhanced with:
        - Trend filter (only signal if trend is weakening)
        - Minimum OBV difference requirement
        """
        try:
            logger.debug(f'Inside analyse_obv_divergence for stock {stock.stock_symbol}')
            
            price_data = stock.priceData
            lookback = VolumeAnalyser.OBV_LOOKBACK
            order = VolumeAnalyser.OBV_SWING_ORDER
            
            if len(price_data) < lookback + 20:
                return False
            
            close = price_data['Close']
            
            # Calculate OBV
            obv = self._calculate_obv(price_data)
            
            # Work on recent window
            recent_close = close.iloc[-lookback:].reset_index(drop=True)
            recent_obv = obv.iloc[-lookback:].reset_index(drop=True)
            
            # Calculate trend using EMA
            ema_20 = close.ewm(span=20, adjust=False).mean()
            ema_50 = close.ewm(span=50, adjust=False).mean()
            
            # Check if trend is weakening (EMAs converging)
            ema_diff_curr = abs(ema_20.iloc[-1] - ema_50.iloc[-1])
            ema_diff_prev = abs(ema_20.iloc[-5] - ema_50.iloc[-5])
            trend_weakening = ema_diff_curr < ema_diff_prev
            
            # Determine trend direction
            if ema_20.iloc[-1] > ema_50.iloc[-1]:
                trend = "BULLISH"
            else:
                trend = "BEARISH"
            
            def find_swing_highs(series, swing_order):
                highs = []
                for i in range(swing_order, len(series) - swing_order):
                    if (all(series.iloc[i] > series.iloc[i - j] for j in range(1, swing_order + 1)) and
                        all(series.iloc[i] > series.iloc[i + j] for j in range(1, swing_order + 1))):
                        highs.append(i)
                return highs
            
            def find_swing_lows(series, swing_order):
                lows = []
                for i in range(swing_order, len(series) - swing_order):
                    if (all(series.iloc[i] < series.iloc[i - j] for j in range(1, swing_order + 1)) and
                        all(series.iloc[i] < series.iloc[i + j] for j in range(1, swing_order + 1))):
                        lows.append(i)
                return lows
            
            OBVDivergenceAnalysis = namedtuple("OBVDivergenceAnalysis", [
                "divergence_type", "price_current", "price_previous",
                "obv_current", "obv_previous", "trend", "trend_weakening"
            ])
            
            # BEARISH Divergence: Price HH + OBV LH
            price_highs = find_swing_highs(recent_close, order)
            if len(price_highs) >= 2:
                h1, h2 = price_highs[-2], price_highs[-1]
                obv_curr = recent_obv.iloc[h2]
                obv_prev = recent_obv.iloc[h1]
                
                # Calculate OBV % difference
                obv_diff_pct = ((obv_prev - obv_curr) / abs(obv_prev)) * 100 if obv_prev != 0 else 0
                
                # Price higher high + OBV lower high + trend weakening
                if (recent_close.iloc[h2] > recent_close.iloc[h1] and
                    obv_curr < obv_prev and
                    obv_diff_pct >= VolumeAnalyser.OBV_MIN_DIVERGENCE and
                    (trend_weakening or trend != "BULLISH")):
                    
                    stock.set_analysis("BEARISH", "OBV_DIVERGENCE",
                        OBVDivergenceAnalysis(
                            divergence_type="bearish",
                            price_current=recent_close.iloc[h2],
                            price_previous=recent_close.iloc[h1],
                            obv_current=obv_curr,
                            obv_previous=obv_prev,
                            trend=trend,
                            trend_weakening=trend_weakening
                        ))
                    logger.info(f"OBV Divergence BEARISH for {stock.stock_symbol}: "
                               f"price HH, OBV LH ({obv_diff_pct:.1f}% diff)")
                    return True
            
            # BULLISH Divergence: Price LL + OBV HL
            price_lows = find_swing_lows(recent_close, order)
            if len(price_lows) >= 2:
                l1, l2 = price_lows[-2], price_lows[-1]
                obv_curr = recent_obv.iloc[l2]
                obv_prev = recent_obv.iloc[l1]
                
                # Calculate OBV % difference
                obv_diff_pct = ((obv_curr - obv_prev) / abs(obv_prev)) * 100 if obv_prev != 0 else 0
                
                # Price lower low + OBV higher low + trend weakening
                if (recent_close.iloc[l2] < recent_close.iloc[l1] and
                    obv_curr > obv_prev and
                    obv_diff_pct >= VolumeAnalyser.OBV_MIN_DIVERGENCE and
                    (trend_weakening or trend != "BEARISH")):
                    
                    stock.set_analysis("BULLISH", "OBV_DIVERGENCE",
                        OBVDivergenceAnalysis(
                            divergence_type="bullish",
                            price_current=recent_close.iloc[l2],
                            price_previous=recent_close.iloc[l1],
                            obv_current=obv_curr,
                            obv_previous=obv_prev,
                            trend=trend,
                            trend_weakening=trend_weakening
                        ))
                    logger.info(f"OBV Divergence BULLISH for {stock.stock_symbol}: "
                               f"price LL, OBV HL ({obv_diff_pct:.1f}% diff)")
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_obv_divergence for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    # ==================== VOLUME CLIMAX ====================
    
    @BaseAnalyzer.both
    def analyse_volume_climax(self, stock: Stock):
        """
        Volume Climax Detection - Exhaustion reversal signal.
        
        BUYING CLIMAX (BEARISH Signal):
        - Price has been rising for CLIMAX_LOOKBACK days
        - Volume spikes to CLIMAX_VOLUME_MULT × average
        - Price closes near the LOW of the day (intraday) or 
          shows reversal pattern (positional)
        - Indicates exhaustion - everyone who wanted to buy has bought
        
        SELLING CLIMAX (BULLISH Signal):
        - Price has been falling for CLIMAX_LOOKBACK days
        - Volume spikes to CLIMAX_VOLUME_MULT × average
        - Price closes near the HIGH of the day (intraday) or
          shows reversal pattern (positional)
        - Indicates exhaustion - everyone who wanted to sell has sold
        """
        try:
            logger.debug(f'Inside analyse_volume_climax for stock {stock.stock_symbol}')
            
            price_data = stock.priceData
            lookback = VolumeAnalyser.CLIMAX_LOOKBACK
            
            if len(price_data) < lookback + 20:
                return False
            
            close = price_data['Close']
            high = price_data['High']
            low = price_data['Low']
            volume = price_data['Volume']
            
            # Current values
            curr_vol = volume.iloc[-1]
            curr_close = close.iloc[-1]
            curr_high = high.iloc[-1]
            curr_low = low.iloc[-1]
            
            # Volume average
            vol_ma = volume.iloc[-20:].mean()
            vol_ratio = curr_vol / vol_ma
            
            # Check for volume spike
            if vol_ratio < VolumeAnalyser.CLIMAX_VOLUME_MULT:
                return False
            
            # Check price trend over lookback period
            price_change = (close.iloc[-1] - close.iloc[-lookback]) / close.iloc[-lookback] * 100
            
            # Calculate where price closed relative to day's range
            # 0 = closed at low, 1 = closed at high
            day_range = curr_high - curr_low
            if day_range > 0:
                close_position = (curr_close - curr_low) / day_range
            else:
                close_position = 0.5
            
            VolumeClimaxAnalysis = namedtuple("VolumeClimaxAnalysis", [
                "climax_type", "volume", "volume_ma", "volume_ratio",
                "price_trend_pct", "close_position", "lookback_days"
            ])
            
            # BUYING CLIMAX (BEARISH)
            # Price was rising + high volume + closed near low
            if (price_change > 5 and  # Price rose > 5% over lookback
                close_position < 0.3):  # Closed in bottom 30% of range
                
                stock.set_analysis("BEARISH", "VOLUME_CLIMAX",
                    VolumeClimaxAnalysis(
                        climax_type="buying_climax",
                        volume=curr_vol,
                        volume_ma=vol_ma,
                        volume_ratio=vol_ratio,
                        price_trend_pct=price_change,
                        close_position=close_position,
                        lookback_days=lookback
                    ))
                logger.info(f"Volume Climax BUYING (BEARISH) for {stock.stock_symbol}: "
                           f"vol_ratio={vol_ratio:.1f}x, price_trend=+{price_change:.1f}%, "
                           f"close_pos={close_position:.2f}")
                return True
            
            # SELLING CLIMAX (BULLISH)
            # Price was falling + high volume + closed near high
            elif (price_change < -5 and  # Price fell > 5% over lookback
                  close_position > 0.7):  # Closed in top 30% of range
                
                stock.set_analysis("BULLISH", "VOLUME_CLIMAX",
                    VolumeClimaxAnalysis(
                        climax_type="selling_climax",
                        volume=curr_vol,
                        volume_ma=vol_ma,
                        volume_ratio=vol_ratio,
                        price_trend_pct=price_change,
                        close_position=close_position,
                        lookback_days=lookback
                    ))
                logger.info(f"Volume Climax SELLING (BULLISH) for {stock.stock_symbol}: "
                           f"vol_ratio={vol_ratio:.1f}x, price_trend={price_change:.1f}%, "
                           f"close_pos={close_position:.2f}")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_volume_climax for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
