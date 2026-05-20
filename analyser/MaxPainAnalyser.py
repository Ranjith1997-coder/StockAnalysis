import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
import common.shared as shared
from collections import namedtuple
import pandas as pd


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
            MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD = 2.0
            MaxPainAnalyser.MAX_PAIN_STRONG_DEVIATION = 4.0
        else:
            MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD = 3.0
            MaxPainAnalyser.MAX_PAIN_STRONG_DEVIATION = 5.0
        
        logger.debug(
            f"[MaxPainAnalyser] constants reset for mode={shared.app_ctx.mode.name} "
            f"DEVIATION_THRESHOLD={MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD} "
            f"STRONG_DEVIATION={MaxPainAnalyser.MAX_PAIN_STRONG_DEVIATION}"
        )

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_max_pain_deviation(self, stock: Stock):
        """
        Analyze max pain for current and next expiry using Sensibull data.
        Generates signals based on price deviation from max pain level.
        """
        try:
            logger.debug(f"[MP_DEV] {stock.stock_symbol} — start")

            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx or not sensibull_ctx.get("current"):
                logger.debug(f"[MP_DEV] {stock.stock_symbol} — no sensibull_ctx, skip")
                return False

            stats = sensibull_ctx["current"].get("stats")
            if not stats:
                logger.debug(f"[MP_DEV] {stock.stock_symbol} — no stats in sensibull_ctx, skip")
                return False

            per_expiry_map = stats.get("per_expiry_map", {})
            if not per_expiry_map:
                logger.debug(f"[MP_DEV] {stock.stock_symbol} — no per_expiry_map, skip")
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
                logger.debug(f"[MP_DEV] {stock.stock_symbol} — no max_pain_strike for expiry={nearest_expiry}, skip")
                return False

            # ── Expiry proximity gate: Max Pain theory is only reliable near expiry ──
            # Intraday: 7d gate (weekly options — only relevant within the expiry week)
            # Positional: 12d gate (multi-day view, allow 2 weeks out)
            gate_days = 7 if shared.app_ctx.mode == shared.Mode.INTRADAY else 12
            days_to_expiry = None
            try:
                from datetime import datetime, date
                expiry_date = datetime.strptime(nearest_expiry, "%Y-%m-%d").date()
                days_to_expiry = (expiry_date - date.today()).days
                if days_to_expiry > gate_days:
                    logger.debug(
                        f"[MP_DEV] {stock.stock_symbol} — "
                        f"{days_to_expiry}d to expiry > {gate_days}d gate, skip"
                    )
                    return False
            except Exception:
                pass  # If date parsing fails, proceed without the gate

            logger.debug(
                f"[MP_DEV] {stock.stock_symbol} | SOURCE "
                f"nearest_expiry={nearest_expiry} days_to_expiry={days_to_expiry} "
                f"current_price={current_price} max_pain_strike={max_pain_strike} "
                f"max_pain_value={max_pain_value} max_pain_type={max_pain_type} "
                f"future_price={future_price} pcr={pcr}"
            )

            # Calculate deviation from max pain
            deviation = ((current_price - max_pain_strike) / max_pain_strike) * 100

            # Determine signal strength
            if abs(deviation) >= MaxPainAnalyser.MAX_PAIN_STRONG_DEVIATION:
                signal_strength = "STRONG"
            elif abs(deviation) >= MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD:
                signal_strength = "MODERATE"
            else:
                signal_strength = "WEAK"
                logger.debug(
                    f"[MP_DEV] {stock.stock_symbol} — "
                    f"deviation={deviation:.2f}% below threshold={MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD}% (WEAK), skip"
                )
                return False  # Skip weak signals

            logger.debug(
                f"[MP_DEV] {stock.stock_symbol} | CONDITION strength: "
                f"abs_deviation={abs(deviation):.2f}% "
                f"strong_threshold={MaxPainAnalyser.MAX_PAIN_STRONG_DEVIATION}% "
                f"moderate_threshold={MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD}% "
                f"→ signal_strength={signal_strength}"
            )
            logger.debug(
                f"[MP_DEV] {stock.stock_symbol} | CONDITION direction: "
                f"deviation={deviation:.2f}% threshold=±{MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD}% "
                f"→ {'BEARISH' if deviation >= MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD else 'BULLISH' if deviation <= -MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD else 'NEUTRAL'}"
            )
            
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
        Analyze if price is converging toward or diverging from max pain.

        Positional: uses oi_history["max_pain"] + oi_history["spot"] (53 daily rows).
        Intraday:   uses historical_data[max_pain_{expiry}] + historical_data[future_price_{expiry}]
                    across intraday cycles — max pain stays flat, futures price moves each 5 min.
        """
        try:
            logger.debug(f"[MP_TREND] {stock.stock_symbol} — start")

            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx:
                logger.debug(f"[MP_TREND] {stock.stock_symbol} — no sensibull_ctx, skip")
                return False

            current_stats = sensibull_ctx.get("current", {}).get("stats", {})
            per_expiry_map = current_stats.get("per_expiry_map", {})
            if not per_expiry_map:
                logger.debug(f"[MP_TREND] {stock.stock_symbol} — no per_expiry_map, skip")
                return False

            nearest_expiry = sorted(per_expiry_map.keys())[0]

            MaxPainTrend = namedtuple("MaxPainTrend", [
                "expiry", "prev_max_pain", "curr_max_pain",
                "prev_deviation", "curr_deviation",
                "trend", "max_pain_shift"
            ])

            is_positional = shared.app_ctx.mode == shared.Mode.POSITIONAL

            # ── Positional path: oi_history["max_pain"] + oi_history["spot"] ──
            if is_positional:
                oi_history = sensibull_ctx.get("oi_history")
                if oi_history is None or oi_history.empty:
                    logger.debug(f"[MP_TREND] {stock.stock_symbol} — oi_history empty, skip")
                    return False
                if "max_pain" not in oi_history.columns:
                    logger.debug(f"[MP_TREND] {stock.stock_symbol} — no max_pain column in oi_history (needs fetcher update), skip")
                    return False

                recent = (
                    oi_history[["date", "max_pain", "spot"]]
                    .dropna(subset=["max_pain", "spot"])
                    .tail(5)
                )
                if len(recent) < 2:
                    logger.debug(
                        f"[MP_TREND] {stock.stock_symbol} — "
                        f"insufficient oi_history rows after dropna (rows={len(recent)} < 2), skip"
                    )
                    return False

                prev_max_pain = float(recent["max_pain"].iloc[-2])
                curr_max_pain = float(recent["max_pain"].iloc[-1])
                prev_spot     = float(recent["spot"].iloc[-2])
                curr_spot     = float(recent["spot"].iloc[-1])

                logger.debug(
                    f"[MP_TREND] {stock.stock_symbol} | SOURCE (positional) "
                    f"expiry={nearest_expiry} oi_history_rows={len(oi_history)} recent_rows={len(recent)} "
                    f"prev_max_pain={prev_max_pain:.2f} curr_max_pain={curr_max_pain:.2f} "
                    f"prev_spot={prev_spot:.2f} curr_spot={curr_spot:.2f}"
                )

                prev_deviation = ((prev_spot - prev_max_pain) / prev_max_pain) * 100
                curr_deviation = ((curr_spot - curr_max_pain) / curr_max_pain) * 100

            # ── Intraday path: deviation series across 5-min cycles ──
            else:
                historical_df = sensibull_ctx.get("historical_data")
                if historical_df is None or historical_df.empty:
                    logger.debug(f"[MP_TREND] {stock.stock_symbol} — historical_data empty, skip")
                    return False

                expiry_suffix = nearest_expiry.replace("-", "")
                mp_col = f"max_pain_{expiry_suffix}"
                fp_col = f"future_price_{expiry_suffix}"

                if mp_col not in historical_df.columns or fp_col not in historical_df.columns:
                    logger.debug(
                        f"[MP_TREND] {stock.stock_symbol} — "
                        f"missing columns {mp_col} or {fp_col} in historical_data, skip"
                    )
                    return False

                recent = historical_df[[mp_col, fp_col]].dropna().tail(6)
                if len(recent) < 3:
                    logger.debug(
                        f"[MP_TREND] {stock.stock_symbol} — "
                        f"insufficient intraday cycles (rows={len(recent)} < 3), skip"
                    )
                    return False

                prev_max_pain = float(recent[mp_col].iloc[-2])
                curr_max_pain = float(recent[mp_col].iloc[-1])
                prev_spot     = float(recent[fp_col].iloc[-2])
                curr_spot     = float(recent[fp_col].iloc[-1])

                logger.debug(
                    f"[MP_TREND] {stock.stock_symbol} | SOURCE (intraday) "
                    f"expiry={nearest_expiry} mp_col={mp_col} fp_col={fp_col} "
                    f"cycles={len(recent)} "
                    f"prev_max_pain={prev_max_pain:.2f} curr_max_pain={curr_max_pain:.2f} "
                    f"prev_price={prev_spot:.2f} curr_price={curr_spot:.2f}"
                )

                prev_deviation = ((prev_spot - prev_max_pain) / prev_max_pain) * 100
                curr_deviation = ((curr_spot - curr_max_pain) / curr_max_pain) * 100

            is_converging = abs(curr_deviation) < abs(prev_deviation)
            is_diverging  = abs(curr_deviation) > abs(prev_deviation)
            max_pain_shift = ((curr_max_pain - prev_max_pain) / prev_max_pain) * 100 if prev_max_pain else 0

            logger.debug(
                f"[MP_TREND] {stock.stock_symbol} | CONDITION deviations: "
                f"prev_deviation={prev_deviation:.2f}% curr_deviation={curr_deviation:.2f}% "
                f"max_pain_shift={max_pain_shift:.2f}% "
                f"is_converging={is_converging} is_diverging={is_diverging}"
            )
            logger.debug(
                f"[MP_TREND] {stock.stock_symbol} | CONDITION gate: "
                f"converging_gate=abs({curr_deviation:.2f}%)>2.0={'PASS' if is_converging and abs(curr_deviation) > 2.0 else 'FAIL'} "
                f"diverging_gate=abs({curr_deviation:.2f}%)>3.0={'PASS' if is_diverging and abs(curr_deviation) > 3.0 else 'FAIL'}"
            )

            if is_converging and abs(curr_deviation) > 2.0:
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

            logger.debug(
                f"[MP_TREND] {stock.stock_symbol} — "
                f"no trend signal (converging={is_converging} curr_deviation={curr_deviation:.2f}% "
                f"diverging={is_diverging}), skip"
            )
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
            logger.debug(f"[MP_ALIGN] {stock.stock_symbol} — start")

            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx or not sensibull_ctx.get("current"):
                logger.debug(f"[MP_ALIGN] {stock.stock_symbol} — no sensibull_ctx, skip")
                return False

            stats = sensibull_ctx["current"].get("stats")
            if not stats:
                logger.debug(f"[MP_ALIGN] {stock.stock_symbol} — no stats in sensibull_ctx, skip")
                return False

            per_expiry_map = stats.get("per_expiry_map", {})
            if not per_expiry_map:
                logger.debug(f"[MP_ALIGN] {stock.stock_symbol} — no per_expiry_map, skip")
                return False

            # Analyze alignment for nearest expiry
            nearest_expiry = sorted(per_expiry_map.keys())[0]
            expiry_data = per_expiry_map[nearest_expiry]

            max_pain_type = expiry_data.get("max_pain_type")
            pcr_type = expiry_data.get("pcr_type")
            max_pain_strike = expiry_data.get("max_pain_strike")
            pcr = expiry_data.get("pcr")

            if not max_pain_type or not max_pain_strike:
                logger.debug(
                    f"[MP_ALIGN] {stock.stock_symbol} — "
                    f"missing max_pain_type={max_pain_type} or max_pain_strike={max_pain_strike}, skip"
                )
                return False

            logger.debug(
                f"[MP_ALIGN] {stock.stock_symbol} | SOURCE "
                f"nearest_expiry={nearest_expiry} max_pain_type={max_pain_type} "
                f"pcr_type={pcr_type} pcr={pcr} max_pain_strike={max_pain_strike}"
            )

            # Check for alignment or divergence
            MaxPainAlignment = namedtuple("MaxPainAlignment", [
                "expiry", "max_pain_type", "pcr_type", "pcr",
                "alignment", "signal"
            ])

            types_match = max_pain_type == pcr_type
            both_directional = max_pain_type in ["Bearish", "Bullish"] and pcr_type in ["Bearish", "Bullish"]
            logger.debug(
                f"[MP_ALIGN] {stock.stock_symbol} | CONDITION alignment: "
                f"max_pain_type={max_pain_type} pcr_type={pcr_type} "
                f"types_match={types_match} both_directional={both_directional} "
                f"→ alignment={'ALIGNED' if types_match and both_directional else 'DIVERGENT' if both_directional and not types_match else 'UNDETERMINED'}"
            )

            # Determine alignment
            # ALIGNED: both directional, same direction
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

            # DIVERGENT: both directional, conflicting — pcr_type must also be directional (not Neutral/None)
            elif (max_pain_type in ["Bearish", "Bullish"]
                  and pcr_type in ["Bearish", "Bullish"]
                  and max_pain_type != pcr_type):
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

            # pcr_type is None/"Neutral" — PCR has no directional bias, skip
            logger.debug(
                f"[MP_ALIGN] {stock.stock_symbol} — "
                f"undetermined (max_pain_type={max_pain_type}, pcr_type={pcr_type}), skip"
            )
            return False
            
        except Exception as e:
            logger.error(f"Error in analyse_max_pain_alignment for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

