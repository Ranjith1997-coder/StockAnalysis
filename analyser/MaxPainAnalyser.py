import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
import common.shared as shared
from collections import namedtuple
import pandas as pd
import numpy as np


class MaxPainAnalyser(BaseAnalyzer):
    """
    Analyzes Max Pain levels from Sensibull data.
    Max Pain is the strike price where option buyers lose maximum money at expiry.
    
    Theory: Price tends to gravitate toward max pain as expiry approaches due to
    options writers (market makers) hedging their positions.
    
    Data Source: Uses pre-calculated max pain from Sensibull API instead of
    calculating from raw option chain data.
    """
    
    # Threshold values
    MAX_PAIN_DEVIATION_THRESHOLD = 2.0  # % deviation to generate signal
    MAX_PAIN_STRONG_DEVIATION = 5.0     # % deviation for strong signal
    
    def __init__(self) -> None:
        self.analyserName = "Max Pain Analyser"
        super().__init__()
    
    def reset_constants(self):
        """Reset constants based on mode"""
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD = 1.5
            MaxPainAnalyser.MAX_PAIN_STRONG_DEVIATION = 3.0
        else:
            MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD = 2.0
            MaxPainAnalyser.MAX_PAIN_STRONG_DEVIATION = 5.0
        
        logger.debug(f"MaxPainAnalyser constants reset for mode {shared.app_ctx.mode.name}")
        logger.debug(f"MAX_PAIN_DEVIATION_THRESHOLD = {MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD}, "
                    f"MAX_PAIN_STRONG_DEVIATION = {MaxPainAnalyser.MAX_PAIN_STRONG_DEVIATION}")

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_max_pain_deviation(self, stock: Stock):
        """
        Analyze max pain for current and next expiry using Sensibull data.
        Generates signals based on price deviation from max pain level.
        """
        try:
            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx or not sensibull_ctx.get("current"):
                logger.debug(f"No Sensibull data for {stock.stock_symbol}")
                return False
            
            stats = sensibull_ctx["current"].get("stats")
            if not stats:
                logger.debug(f"No stats in Sensibull data for {stock.stock_symbol}")
                return False
            
            per_expiry_map = stats.get("per_expiry_map", {})
            if not per_expiry_map:
                logger.debug(f"No per-expiry data in Sensibull for {stock.stock_symbol}")
                return False
            
            # Get current underlying price
            current_price = stock.ltp if stock.ltp else stock.priceData['Close'].iloc[-1]
            
            # Analyze only the nearest/current expiry (first key in sorted expiries)
            nearest_expiry = sorted(per_expiry_map.keys())[0]
            expiry_data = per_expiry_map[nearest_expiry]
            
            max_pain_strike = expiry_data.get("max_pain_strike")
            max_pain_value = expiry_data.get("max_pain_value")
            max_pain_type = expiry_data.get("max_pain_type")
            future_price = expiry_data.get("future_price")
            pcr = expiry_data.get("pcr")
            
            if max_pain_strike is None:
                logger.debug(f"No max pain strike for {stock.stock_symbol} current expiry {nearest_expiry}")
                return False
            
            # Calculate deviation from max pain
            deviation = ((current_price - max_pain_strike) / max_pain_strike) * 100
            
            # Determine signal strength
            if abs(deviation) >= MaxPainAnalyser.MAX_PAIN_STRONG_DEVIATION:
                signal_strength = "STRONG"
            elif abs(deviation) >= MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD:
                signal_strength = "MODERATE"
            else:
                signal_strength = "WEAK"
                return False  # Skip weak signals
            
            MaxPainAnalysis = namedtuple("MaxPainAnalysis", [
                "expiry",
                "max_pain_strike", 
                "current_price", 
                "deviation_pct",
                "max_pain_value",
                "max_pain_type",
                "future_price",
                "pcr",
                "signal_strength"
            ])
            
            analysis = MaxPainAnalysis(
                expiry=nearest_expiry,
                max_pain_strike=max_pain_strike,
                current_price=current_price,
                deviation_pct=deviation,
                max_pain_value=max_pain_value,
                max_pain_type=max_pain_type,
                future_price=future_price,
                pcr=pcr,
                signal_strength=signal_strength
            )
            
            # Generate trading signals based on max pain deviation
            if deviation >= MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD:
                # Price above max pain - potential downward pull
                stock.set_analysis("BEARISH", "MAX_PAIN", analysis)
                logger.info(f"Max Pain Signal for {stock.stock_symbol} ({nearest_expiry}): "
                          f"Price ({current_price:.2f}) above Max Pain ({max_pain_strike:.2f}) "
                          f"by {deviation:.2f}% - Bearish pressure expected ({signal_strength})")
                return True
                
            elif deviation <= -MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD:
                # Price below max pain - potential upward pull
                stock.set_analysis("BULLISH", "MAX_PAIN", analysis)
                logger.info(f"Max Pain Signal for {stock.stock_symbol} ({nearest_expiry}): "
                          f"Price ({current_price:.2f}) below Max Pain ({max_pain_strike:.2f}) "
                          f"by {deviation:.2f}% - Bullish pressure expected ({signal_strength})")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_max_pain_deviation for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_max_pain_trend(self, stock: Stock):
        """
        Analyze if price is converging toward or diverging from max pain
        using historical Sensibull data.
        """
        try:
            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx:
                return False
            
            historical_df = sensibull_ctx.get("historical_data")
            
            if historical_df is None or historical_df.empty or len(historical_df) < 2:
                logger.debug(f"Insufficient historical Sensibull data for {stock.stock_symbol}")
                return False
            
            # Get current stats for comparison
            current_stats = sensibull_ctx.get("current", {}).get("stats", {})
            per_expiry_map = current_stats.get("per_expiry_map", {})
            
            if not per_expiry_map:
                return False
            
            # Find the nearest expiry (first key in sorted expiries)
            nearest_expiry = sorted(per_expiry_map.keys())[0]
            expiry_suffix = nearest_expiry.replace("-", "")
            
            # Check if we have historical max pain data for this expiry
            max_pain_col = f"max_pain_{expiry_suffix}"
            
            if max_pain_col not in historical_df.columns:
                logger.debug(f"No historical max pain data for expiry {nearest_expiry}")
                return False
            
            # Get last 2 max pain values
            recent_data = historical_df[[max_pain_col, 'timestamp']].tail(2)
            
            if len(recent_data) < 2:
                return False
            
            prev_max_pain = recent_data[max_pain_col].iloc[0]
            curr_max_pain = recent_data[max_pain_col].iloc[1]
            
            if pd.isna(prev_max_pain) or pd.isna(curr_max_pain):
                return False
            
            # Get current price
            current_price = stock.ltp if stock.ltp else stock.priceData['Close'].iloc[-1]
            
            # Calculate deviations
            prev_deviation = ((current_price - prev_max_pain) / prev_max_pain) * 100
            curr_deviation = ((current_price - curr_max_pain) / curr_max_pain) * 100
            
            is_converging = abs(curr_deviation) < abs(prev_deviation)
            is_diverging = abs(curr_deviation) > abs(prev_deviation)
            
            MaxPainTrend = namedtuple("MaxPainTrend", [
                "expiry", "prev_max_pain", "curr_max_pain", 
                "prev_deviation", "curr_deviation",
                "trend", "max_pain_shift"
            ])
            
            max_pain_shift = ((curr_max_pain - prev_max_pain) / prev_max_pain) * 100 if prev_max_pain else 0
            
            if is_converging and abs(curr_deviation) > 1.0:
                # Price moving toward max pain - confirming max pain theory
                trend = "CONVERGING"
                sentiment = "BEARISH" if curr_deviation > 0 else "BULLISH"
                
                stock.set_analysis(sentiment, "MAX_PAIN_TREND", MaxPainTrend(
                    expiry=nearest_expiry,
                    prev_max_pain=prev_max_pain,
                    curr_max_pain=curr_max_pain,
                    prev_deviation=prev_deviation,
                    curr_deviation=curr_deviation,
                    trend=trend,
                    max_pain_shift=max_pain_shift
                ))
                
                logger.info(f"Max Pain Convergence for {stock.stock_symbol} ({nearest_expiry}): "
                          f"Price moving toward max pain ({sentiment})")
                return True
            
            elif is_diverging and abs(curr_deviation) > 3.0:
                # Price moving away from max pain - strong directional move
                trend = "DIVERGING"
                stock.set_analysis("NEUTRAL", "MAX_PAIN_TREND", MaxPainTrend(
                    expiry=nearest_expiry,
                    prev_max_pain=prev_max_pain,
                    curr_max_pain=curr_max_pain,
                    prev_deviation=prev_deviation,
                    curr_deviation=curr_deviation,
                    trend=trend,
                    max_pain_shift=max_pain_shift
                ))
                
                logger.info(f"Max Pain Divergence for {stock.stock_symbol} ({nearest_expiry}): "
                          f"Strong directional move away from max pain")
                return False
            
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_max_pain_trend for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_max_pain_alignment(self, stock: Stock):
        """
        Analyze if Sensibull's max pain type aligns with PCR and other indicators.
        Strong alignment across multiple metrics increases signal confidence.
        """
        try:
            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx or not sensibull_ctx.get("current"):
                return False
            
            stats = sensibull_ctx["current"].get("stats")
            if not stats:
                return False
            
            per_expiry_map = stats.get("per_expiry_map", {})
            if not per_expiry_map:
                return False
            
            # Analyze alignment for nearest expiry
            nearest_expiry = sorted(per_expiry_map.keys())[0]
            expiry_data = per_expiry_map[nearest_expiry]
            
            max_pain_type = expiry_data.get("max_pain_type")
            pcr_type = expiry_data.get("pcr_type")
            max_pain_strike = expiry_data.get("max_pain_strike")
            pcr = expiry_data.get("pcr")
            
            if not max_pain_type or not max_pain_strike:
                return False
            
            # Check for alignment or divergence
            MaxPainAlignment = namedtuple("MaxPainAlignment", [
                "expiry", "max_pain_type", "pcr_type", "pcr",
                "alignment", "signal"
            ])
            
            # Determine alignment
            if max_pain_type == pcr_type and max_pain_type in ["Bearish", "Bullish"]:
                alignment = "ALIGNED"
                signal = f"{max_pain_type} signal confirmed by both Max Pain and PCR"
                sentiment = max_pain_type.upper()
                
                stock.set_analysis(sentiment, "MAX_PAIN_ALIGNMENT", MaxPainAlignment(
                    expiry=nearest_expiry,
                    max_pain_type=max_pain_type,
                    pcr_type=pcr_type,
                    pcr=pcr,
                    alignment=alignment,
                    signal=signal
                ))
                
                logger.info(f"Max Pain Alignment for {stock.stock_symbol} ({nearest_expiry}): "
                          f"{signal}")
                return True
                
            elif max_pain_type and pcr_type and max_pain_type != pcr_type:
                alignment = "DIVERGENT"
                signal = f"Max Pain ({max_pain_type}) conflicts with PCR ({pcr_type}) - mixed signals"
                
                stock.set_analysis("NEUTRAL", "MAX_PAIN_ALIGNMENT", MaxPainAlignment(
                    expiry=nearest_expiry,
                    max_pain_type=max_pain_type,
                    pcr_type=pcr_type,
                    pcr=pcr,
                    alignment=alignment,
                    signal=signal
                ))
                
                logger.info(f"Max Pain Divergence for {stock.stock_symbol} ({nearest_expiry}): "
                          f"{signal}")
                return False
            
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_max_pain_alignment for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

