import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
import common.shared as shared
from collections import namedtuple
import pandas as pd

class PCRAnalyser(BaseAnalyzer):
    """
    Analyzes Put-Call Ratio (PCR) signals from Sensibull data.
    PCR < 0.5: Bearish (excessive call buying)
    PCR > 1.0: Bullish (excessive put buying)
    """
    
    # Threshold values
    PCR_BEARISH_THRESHOLD = 0.5
    PCR_BULLISH_THRESHOLD = 1.2
    PCR_EXTREME_BEARISH = 0.3
    PCR_EXTREME_BULLISH = 1.5
    
    def __init__(self) -> None:
        self.analyserName = "PCR Analyser"
        super().__init__()
    
    def reset_constants(self):
        """Reset constants based on mode"""
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            PCRAnalyser.PCR_BEARISH_THRESHOLD = 0.5
            PCRAnalyser.PCR_BULLISH_THRESHOLD = 1.0
            PCRAnalyser.PCR_EXTREME_BEARISH = 0.3
            PCRAnalyser.PCR_EXTREME_BULLISH = 1.5
        else:
            PCRAnalyser.PCR_BEARISH_THRESHOLD = 0.5
            PCRAnalyser.PCR_BULLISH_THRESHOLD = 1.0
            PCRAnalyser.PCR_EXTREME_BEARISH = 0.3
            PCRAnalyser.PCR_EXTREME_BULLISH = 1.5
        
        logger.debug(f"PCRAnalyser constants reset for mode {shared.app_ctx.mode.name}")

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_pcr_extreme_zones(self, stock: Stock):
        """
        Detect extreme PCR zones that signal potential reversals.
        PCR < 0.3: Extreme bearish (contrarian bullish signal)
        PCR > 1.5: Extreme bullish (contrarian bearish signal)
        """
        try:
            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx or not sensibull_ctx.get("current"):
                logger.debug(f"No Sensibull data available for {stock.stock_symbol}")
                return False
            
            stats = sensibull_ctx["current"].get("stats")
            if not stats:
                return False
            
            base_stats = stats.get("underlying_base_stats", {})
            total_pcr = base_stats.get("total_pcr")
            
            if total_pcr is None:
                return False
            
            PCR_EXTREME = namedtuple("PCR_EXTREME", ["pcr_value", "zone", "signal"])
            
            # Check for extreme zones
            if total_pcr <= PCRAnalyser.PCR_EXTREME_BEARISH:
                # Extremely low PCR -> Too many calls -> Contrarian Bullish
                stock.set_analysis("BULLISH", "PCR_EXTREME", PCR_EXTREME(
                    pcr_value=total_pcr,
                    zone="EXTREME_LOW",
                    signal="Excessive call buying - potential reversal up"
                ))
                logger.info(f"Extreme low PCR detected for {stock.stock_symbol}: {total_pcr:.3f} (Contrarian Bullish)")
                return True
            
            elif total_pcr >= PCRAnalyser.PCR_EXTREME_BULLISH:
                # Extremely high PCR -> Too many puts -> Contrarian Bearish
                stock.set_analysis("BEARISH", "PCR_EXTREME", PCR_EXTREME(
                    pcr_value=total_pcr,
                    zone="EXTREME_HIGH",
                    signal="Excessive put buying - potential reversal down"
                ))
                logger.info(f"Extreme high PCR detected for {stock.stock_symbol}: {total_pcr:.3f} (Contrarian Bearish)")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_pcr_extreme_zones for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_pcr_directional_bias(self, stock: Stock):
        """
        Analyze PCR for directional bias.
        PCR < 0.5: Bearish bias (more calls than puts)
        PCR > 1.0: Bullish bias (more puts than calls)
        """
        try:
            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx or not sensibull_ctx.get("current"):
                return False
            
            stats = sensibull_ctx["current"].get("stats")
            if not stats:
                return False
            
            base_stats = stats.get("underlying_base_stats", {})
            total_pcr = base_stats.get("total_pcr")
            per_expiry_pcr = base_stats.get("per_expiry_pcr", {})
            
            if total_pcr is None:
                return False
            
            PCR_BIAS = namedtuple("PCR_BIAS", ["total_pcr", "bias", "per_expiry"])
            
            # Determine bias
            if total_pcr < PCRAnalyser.PCR_BEARISH_THRESHOLD:
                stock.set_analysis("BEARISH", "PCR_BIAS", PCR_BIAS(
                    total_pcr=total_pcr,
                    bias="Bearish - More calls than puts",
                    per_expiry=per_expiry_pcr
                ))
                logger.info(f"Bearish PCR bias for {stock.stock_symbol}: {total_pcr:.3f}")
                return True
            
            elif total_pcr > PCRAnalyser.PCR_BULLISH_THRESHOLD:
                stock.set_analysis("BULLISH", "PCR_BIAS", PCR_BIAS(
                    total_pcr=total_pcr,
                    bias="Bullish - More puts than calls",
                    per_expiry=per_expiry_pcr
                ))
                logger.info(f"Bullish PCR bias for {stock.stock_symbol}: {total_pcr:.3f}")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_pcr_directional_bias for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_pcr_trend(self, stock: Stock):
        """
        Analyze PCR trend from historical data.
        Rising PCR: Increasing put interest (Bullish)
        Falling PCR: Increasing call interest (Bearish)
        """
        try:
            sensibull_ctx = stock.sensibull_ctx
            historical_df = sensibull_ctx.get("historical_data")
            
            if historical_df is None or historical_df.empty:
                logger.debug(f"No historical Sensibull data for {stock.stock_symbol}")
                return False
            
            if len(historical_df) < 3:
                logger.debug(f"Insufficient historical data for PCR trend analysis for {stock.stock_symbol}")
                return False
            
            # Get last 3 PCR values
            recent_data = historical_df.tail(3)
            pcr_values = recent_data["total_pcr"].dropna()
            
            if len(pcr_values) < 3:
                return False
            
            pcr_list = pcr_values.tolist()
            
            # Check for consistent trend
            is_rising = all(pcr_list[i] < pcr_list[i+1] for i in range(len(pcr_list)-1))
            is_falling = all(pcr_list[i] > pcr_list[i+1] for i in range(len(pcr_list)-1))
            
            if not (is_rising or is_falling):
                return False
            
            pcr_change = ((pcr_list[-1] - pcr_list[0]) / pcr_list[0]) * 100 if pcr_list[0] != 0 else 0
            
            PCR_TREND = namedtuple("PCR_TREND", ["trend", "pcr_current", "pcr_change_pct", "values"])
            
            if is_rising and abs(pcr_change) > 10:  # Significant rise (>10%)
                stock.set_analysis("BULLISH", "PCR_TREND", PCR_TREND(
                    trend="RISING",
                    pcr_current=pcr_list[-1],
                    pcr_change_pct=pcr_change,
                    values=pcr_list
                ))
                logger.info(f"Rising PCR trend for {stock.stock_symbol}: {pcr_change:.2f}% (Bullish)")
                return True
            
            elif is_falling and abs(pcr_change) > 10:  # Significant fall (>10%)
                stock.set_analysis("BEARISH", "PCR_TREND", PCR_TREND(
                    trend="FALLING",
                    pcr_current=pcr_list[-1],
                    pcr_change_pct=pcr_change,
                    values=pcr_list
                ))
                logger.info(f"Falling PCR trend for {stock.stock_symbol}: {pcr_change:.2f}% (Bearish)")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_pcr_trend for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_pcr_divergence(self, stock: Stock):
        """
        Detect PCR divergence across expiries.
        If near-month and far-month PCR show different bias, it could signal uncertainty or hedging.
        """
        try:
            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx or not sensibull_ctx.get("current"):
                return False
            
            stats = sensibull_ctx["current"].get("stats")
            if not stats:
                return False
            
            base_stats = stats.get("underlying_base_stats", {})
            per_expiry_pcr = base_stats.get("per_expiry_pcr", {})
            
            if not per_expiry_pcr or len(per_expiry_pcr) < 2:
                return False
            
            # Get sorted expiries
            sorted_expiries = sorted(per_expiry_pcr.items())
            if len(sorted_expiries) < 2:
                return False
            
            near_month_expiry, near_month_pcr = sorted_expiries[0]
            far_month_expiry, far_month_pcr = sorted_expiries[1]
            
            # Check if PCR values are valid
            if near_month_pcr is None or far_month_pcr is None:
                return False
            
            # Check for significant divergence
            pcr_diff = abs(near_month_pcr - far_month_pcr)
            
            if pcr_diff > 0.9:  # Significant divergence
                PCR_DIVERGENCE = namedtuple("PCR_DIVERGENCE", [
                    "near_month_pcr", "far_month_pcr", "divergence", "signal"
                ])
                
                signal = ""
                if near_month_pcr < 0.5 and far_month_pcr > 1.0:
                    signal = "Near-month bearish, far-month bullish - mixed sentiment"
                elif near_month_pcr > 1.0 and far_month_pcr < 0.5:
                    signal = "Near-month bullish, far-month bearish - hedging activity"
                else:
                    signal = f"Divergence detected - uncertainty in market direction"
                
                stock.set_analysis("NEUTRAL", "PCR_DIVERGENCE", PCR_DIVERGENCE(
                    near_month_pcr=near_month_pcr,
                    far_month_pcr=far_month_pcr,
                    divergence=pcr_diff,
                    signal=signal
                ))
                logger.info(f"PCR divergence for {stock.stock_symbol}: Near={near_month_pcr:.3f}, Far={far_month_pcr:.3f}")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_pcr_divergence for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.intraday
    @BaseAnalyzer.index_intraday
    def analyse_pcr_reversal(self, stock: Stock):
        """
        Detect PCR reversal - when PCR crosses from one bias zone to another.
        
        Reversal types:
        1. Zone crossover: PCR moves from bearish zone (<0.5) to bullish zone (>1.0) or vice versa
        2. Trend reversal: PCR was consistently rising/falling and has now reversed direction
        
        A bullish reversal (PCR rising from bearish to bullish zone) suggests sentiment shift from call-heavy to put-heavy.
        A bearish reversal (PCR falling from bullish to bearish zone) suggests sentiment shift from put-heavy to call-heavy.
        """
        try:
            sensibull_ctx = stock.sensibull_ctx
            historical_df = sensibull_ctx.get("historical_data")
            
            if historical_df is None or historical_df.empty:
                logger.debug(f"No historical Sensibull data for {stock.stock_symbol}")
                return False
            
            if len(historical_df) < 4:
                logger.debug(f"Insufficient historical data for PCR reversal analysis for {stock.stock_symbol}")
                return False
            
            # Get last 4 PCR values for reversal detection
            recent_data = historical_df.tail(4)
            pcr_values = recent_data["total_pcr"].dropna()
            
            if len(pcr_values) < 4:
                return False
            
            pcr_list = pcr_values.tolist()
            current_pcr = pcr_list[-1]
            previous_pcr = pcr_list[-2]
            
            PCR_REVERSAL = namedtuple("PCR_REVERSAL", [
                "reversal_type", "previous_pcr", "current_pcr", 
                "previous_zone", "current_zone", "signal"
            ])
            
            # Determine zones
            def get_zone(pcr):
                if pcr < PCRAnalyser.PCR_BEARISH_THRESHOLD:
                    return "BEARISH"
                elif pcr > PCRAnalyser.PCR_BULLISH_THRESHOLD:
                    return "BULLISH"
                else:
                    return "NEUTRAL"
            
            # Check for zone crossover reversal
            # Look at the zone of the first 2 values vs the last 2 values
            old_avg_pcr = (pcr_list[0] + pcr_list[1]) / 2
            new_avg_pcr = (pcr_list[2] + pcr_list[3]) / 2
            
            old_zone = get_zone(old_avg_pcr)
            new_zone = get_zone(new_avg_pcr)
            
            # Detect significant zone crossover
            if old_zone != new_zone and old_zone != "NEUTRAL" and new_zone != "NEUTRAL":
                if old_zone == "BEARISH" and new_zone == "BULLISH":
                    # PCR moved from low (call-heavy) to high (put-heavy) -> Bullish reversal
                    stock.set_analysis("BULLISH", "PCR_REVERSAL", PCR_REVERSAL(
                        reversal_type="ZONE_CROSSOVER",
                        previous_pcr=old_avg_pcr,
                        current_pcr=new_avg_pcr,
                        previous_zone=old_zone,
                        current_zone=new_zone,
                        signal="PCR reversed from bearish to bullish zone - sentiment shifting to puts (Bullish)"
                    ))
                    logger.info(f"Bullish PCR reversal for {stock.stock_symbol}: {old_avg_pcr:.3f} -> {new_avg_pcr:.3f}")
                    return True
                
                elif old_zone == "BULLISH" and new_zone == "BEARISH":
                    # PCR moved from high (put-heavy) to low (call-heavy) -> Bearish reversal
                    stock.set_analysis("BEARISH", "PCR_REVERSAL", PCR_REVERSAL(
                        reversal_type="ZONE_CROSSOVER",
                        previous_pcr=old_avg_pcr,
                        current_pcr=new_avg_pcr,
                        previous_zone=old_zone,
                        current_zone=new_zone,
                        signal="PCR reversed from bullish to bearish zone - sentiment shifting to calls (Bearish)"
                    ))
                    logger.info(f"Bearish PCR reversal for {stock.stock_symbol}: {old_avg_pcr:.3f} -> {new_avg_pcr:.3f}")
                    return True
            
            # Check for trend reversal (direction change)
            # First 3 values were trending one way, but the 4th reverses
            first_trend = pcr_list[1] - pcr_list[0]
            second_trend = pcr_list[2] - pcr_list[1]
            third_trend = pcr_list[3] - pcr_list[2]
            
            # Was rising (positive trend), now falling
            if first_trend > 0 and second_trend > 0 and third_trend < 0:
                trend_magnitude = abs(third_trend / pcr_list[2]) * 100 if pcr_list[2] != 0 else 0
                if trend_magnitude > 10:  # Significant reversal (>10% change)
                    stock.set_analysis("BEARISH", "PCR_REVERSAL", PCR_REVERSAL(
                        reversal_type="TREND_REVERSAL",
                        previous_pcr=pcr_list[2],
                        current_pcr=current_pcr,
                        previous_zone=get_zone(pcr_list[2]),
                        current_zone=get_zone(current_pcr),
                        signal=f"PCR trend reversed from rising to falling ({trend_magnitude:.1f}% drop) - Bearish"
                    ))
                    logger.info(f"PCR trend reversal (rising->falling) for {stock.stock_symbol}: {trend_magnitude:.1f}% drop")
                    return True
            
            # Was falling (negative trend), now rising
            if first_trend < 0 and second_trend < 0 and third_trend > 0:
                trend_magnitude = abs(third_trend / pcr_list[2]) * 100 if pcr_list[2] != 0 else 0
                if trend_magnitude > 10:  # Significant reversal (>10% change)
                    stock.set_analysis("BULLISH", "PCR_REVERSAL", PCR_REVERSAL(
                        reversal_type="TREND_REVERSAL",
                        previous_pcr=pcr_list[2],
                        current_pcr=current_pcr,
                        previous_zone=get_zone(pcr_list[2]),
                        current_zone=get_zone(current_pcr),
                        signal=f"PCR trend reversed from falling to rising ({trend_magnitude:.1f}% rise) - Bullish"
                    ))
                    logger.info(f"PCR trend reversal (falling->rising) for {stock.stock_symbol}: {trend_magnitude:.1f}% rise")
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_pcr_reversal for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False