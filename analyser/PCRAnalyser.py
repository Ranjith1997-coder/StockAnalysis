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

    # Core thresholds
    PCR_BEARISH_THRESHOLD = 0.5
    PCR_BULLISH_THRESHOLD = 1.2
    PCR_EXTREME_BEARISH = 0.3
    PCR_EXTREME_BULLISH = 1.5

    # Bias strength bands (WEAK < MODERATE < STRONG)
    PCR_BEARISH_STRONG   = 0.35   # pcr < this → STRONG bearish
    PCR_BEARISH_MODERATE = 0.45   # pcr < this → MODERATE bearish (else WEAK)
    PCR_BULLISH_MODERATE = 1.35   # pcr > this → MODERATE bullish
    PCR_BULLISH_STRONG   = 1.5    # pcr > this → STRONG bullish

    # Bias trend-direction sensitivity (abs PCR change to count as STRENGTHENING/WEAKENING)
    PCR_BIAS_DIRECTION_SENSITIVITY = 0.02

    # Divergence threshold (near vs far expiry PCR diff)
    PCR_DIVERGENCE_THRESHOLD = 0.35

    # Positional trend (uses oi_history daily rows)
    PCR_TREND_DAYS    = 5      # days window
    PCR_TREND_MIN_PCT = 8.0    # min % change over window
    PCR_TREND_MIN_ABS = 0.08   # min absolute PCR change over window

    # Intraday trend (uses oi_chain_history per-cycle snapshots)
    PCR_INTRADAY_MIN_SNAPSHOTS = 3    # snapshots needed
    PCR_INTRADAY_TREND_MIN_PCT = 5.0  # min % change over snapshots

    # Intraday reversal
    PCR_REVERSAL_MIN_SNAPSHOTS  = 4
    PCR_REVERSAL_TREND_MIN_PCT  = 8.0  # magnitude threshold for trend-reversal path

    # Positional reversal (uses oi_history daily rows)
    PCR_POS_REVERSAL_MIN_ROWS  = 6     # need 6 daily rows (3 old + 3 new)
    PCR_POS_REVERSAL_TREND_PCT = 8.0   # min single-day reversal % on trend-flip path

    def __init__(self) -> None:
        self.analyserName = "PCR Analyser"
        super().__init__()

    def reset_constants(self):
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            PCRAnalyser.PCR_BEARISH_THRESHOLD          = 0.5
            PCRAnalyser.PCR_BULLISH_THRESHOLD          = 1.2
            PCRAnalyser.PCR_EXTREME_BEARISH            = 0.3
            PCRAnalyser.PCR_EXTREME_BULLISH            = 1.5
            PCRAnalyser.PCR_DIVERGENCE_THRESHOLD       = 0.35
            PCRAnalyser.PCR_INTRADAY_MIN_SNAPSHOTS     = 3
            PCRAnalyser.PCR_INTRADAY_TREND_MIN_PCT     = 5.0
            PCRAnalyser.PCR_REVERSAL_MIN_SNAPSHOTS     = 4
            PCRAnalyser.PCR_REVERSAL_TREND_MIN_PCT     = 8.0
        else:
            PCRAnalyser.PCR_BEARISH_THRESHOLD          = 0.5
            PCRAnalyser.PCR_BULLISH_THRESHOLD          = 1.2
            PCRAnalyser.PCR_EXTREME_BEARISH            = 0.3
            PCRAnalyser.PCR_EXTREME_BULLISH            = 1.5
            PCRAnalyser.PCR_DIVERGENCE_THRESHOLD       = 0.35
            PCRAnalyser.PCR_TREND_DAYS                 = 5
            PCRAnalyser.PCR_TREND_MIN_PCT              = 8.0
            PCRAnalyser.PCR_TREND_MIN_ABS              = 0.08
            PCRAnalyser.PCR_POS_REVERSAL_MIN_ROWS      = 6
            PCRAnalyser.PCR_POS_REVERSAL_TREND_PCT     = 8.0

        logger.debug(f"[PCRAnalyser] constants reset for mode {shared.app_ctx.mode.name}")

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _bias_strength(pcr: float, direction: str) -> str:
        if direction == "BEARISH":
            if pcr < PCRAnalyser.PCR_BEARISH_STRONG:
                return "STRONG"
            elif pcr < PCRAnalyser.PCR_BEARISH_MODERATE:
                return "MODERATE"
            return "WEAK"
        else:  # BULLISH
            if pcr > PCRAnalyser.PCR_BULLISH_STRONG:
                return "STRONG"
            elif pcr > PCRAnalyser.PCR_BULLISH_MODERATE:
                return "MODERATE"
            return "WEAK"

    @staticmethod
    def _bias_trend_direction(current_pcr: float, prev_pcr: float | None, direction: str) -> str:
        if prev_pcr is None:
            return "STABLE"
        delta = current_pcr - prev_pcr
        sensitivity = PCRAnalyser.PCR_BIAS_DIRECTION_SENSITIVITY
        if abs(delta) < sensitivity:
            return "STABLE"
        # BEARISH bias strengthens when pcr falls further; BULLISH strengthens when pcr rises
        if direction == "BEARISH":
            return "STRENGTHENING" if delta < 0 else "WEAKENING"
        else:
            return "STRENGTHENING" if delta > 0 else "WEAKENING"

    @staticmethod
    def _get_prev_pcr(sensibull_ctx: dict) -> float | None:
        """Returns the previous PCR reading — intraday: from oi_chain_history[-2]; positional: from oi_history[-2]."""
        oi_chain_history = sensibull_ctx.get("oi_chain_history", [])
        if len(oi_chain_history) >= 2:
            return oi_chain_history[-2].get("pcr")
        oi_history = sensibull_ctx.get("oi_history", pd.DataFrame())
        if not oi_history.empty and len(oi_history) >= 2 and "pcr" in oi_history.columns:
            val = oi_history["pcr"].iloc[-2]
            return float(val) if pd.notna(val) else None
        return None

    @staticmethod
    def _count_consecutive_extreme(sensibull_ctx: dict, low_thresh: float, high_thresh: float,
                                   is_low: bool) -> int:
        """Count how many consecutive prior snapshots/days were also in the same extreme zone."""
        count = 0
        oi_chain_history = sensibull_ctx.get("oi_chain_history", [])
        if len(oi_chain_history) >= 2:
            for snap in reversed(oi_chain_history[:-1]):
                p = snap.get("pcr")
                if p is None:
                    break
                if (is_low and p <= low_thresh) or (not is_low and p >= high_thresh):
                    count += 1
                else:
                    break
            return count
        oi_history = sensibull_ctx.get("oi_history", pd.DataFrame())
        if not oi_history.empty and "pcr" in oi_history.columns:
            pcr_col = oi_history["pcr"].dropna()
            for val in reversed(pcr_col.iloc[:-1].tolist()):
                if (is_low and val <= low_thresh) or (not is_low and val >= high_thresh):
                    count += 1
                else:
                    break
        return count

    # ── methods ───────────────────────────────────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_pcr_extreme_zones(self, stock: Stock):
        """
        Detect extreme PCR zones that signal potential reversals.
        PCR <= 0.3: Extreme bearish (contrarian bullish signal)
        PCR >= 1.5: Extreme bullish (contrarian bearish signal)
        Adds consecutive-snapshot/day confirmation count.
        """
        try:
            logger.debug(f"[PCR_EXTREME] {stock.stock_symbol} — start")

            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx or not sensibull_ctx.get("current"):
                logger.debug(f"[PCR_EXTREME] {stock.stock_symbol} — no Sensibull current data, skip")
                return False

            stats = sensibull_ctx["current"].get("stats")
            if not stats:
                logger.debug(f"[PCR_EXTREME] {stock.stock_symbol} — no stats in current data, skip")
                return False

            base_stats = stats.get("underlying_base_stats", {})
            total_pcr = base_stats.get("total_pcr")

            if total_pcr is None:
                logger.debug(f"[PCR_EXTREME] {stock.stock_symbol} — total_pcr missing, skip")
                return False

            logger.debug(f"[PCR_EXTREME] {stock.stock_symbol} | SOURCE total_pcr={total_pcr:.3f}")

            extreme_low_pass  = total_pcr <= PCRAnalyser.PCR_EXTREME_BEARISH
            extreme_high_pass = total_pcr >= PCRAnalyser.PCR_EXTREME_BULLISH
            logger.debug(
                f"[PCR_EXTREME] {stock.stock_symbol} | "
                f"CONDITION pcr={total_pcr:.3f} "
                f"extreme_low<={PCRAnalyser.PCR_EXTREME_BEARISH} → {'PASS' if extreme_low_pass else 'FAIL'} | "
                f"extreme_high>={PCRAnalyser.PCR_EXTREME_BULLISH} → {'PASS' if extreme_high_pass else 'FAIL'}"
            )

            if not (extreme_low_pass or extreme_high_pass):
                logger.debug(
                    f"[PCR_EXTREME] {stock.stock_symbol} — no signal | "
                    f"pcr={total_pcr:.3f} within [{PCRAnalyser.PCR_EXTREME_BEARISH},{PCRAnalyser.PCR_EXTREME_BULLISH}]"
                )
                return False

            is_low = extreme_low_pass
            consecutive = PCRAnalyser._count_consecutive_extreme(
                sensibull_ctx, PCRAnalyser.PCR_EXTREME_BEARISH, PCRAnalyser.PCR_EXTREME_BULLISH, is_low
            )
            confirmed = consecutive >= 1

            logger.debug(
                f"[PCR_EXTREME] {stock.stock_symbol} | "
                f"CONFIRMATION consecutive_prior={consecutive} confirmed={confirmed}"
            )

            PCR_EXTREME = namedtuple("PCR_EXTREME", ["pcr_value", "zone", "signal", "confirmed", "consecutive_prior"])

            if extreme_low_pass:
                stock.set_analysis("BULLISH", "PCR_EXTREME", PCR_EXTREME(
                    pcr_value=total_pcr,
                    zone="EXTREME_LOW",
                    signal="Excessive call buying - potential reversal up",
                    confirmed=confirmed,
                    consecutive_prior=consecutive,
                ))
                logger.info(
                    f"[PCR_EXTREME] {stock.stock_symbol} — SIGNAL BULLISH | "
                    f"pcr={total_pcr:.3f} zone=EXTREME_LOW confirmed={confirmed} prior={consecutive}"
                )
                return True

            stock.set_analysis("BEARISH", "PCR_EXTREME", PCR_EXTREME(
                pcr_value=total_pcr,
                zone="EXTREME_HIGH",
                signal="Excessive put buying - potential reversal down",
                confirmed=confirmed,
                consecutive_prior=consecutive,
            ))
            logger.info(
                f"[PCR_EXTREME] {stock.stock_symbol} — SIGNAL BEARISH | "
                f"pcr={total_pcr:.3f} zone=EXTREME_HIGH confirmed={confirmed} prior={consecutive}"
            )
            return True

        except Exception as e:
            logger.error(f"Error in analyse_pcr_extreme_zones for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_pcr_directional_bias(self, stock: Stock):
        """
        Directional bias from total PCR with strength (STRONG/MODERATE/WEAK) and
        trend direction (STRENGTHENING/WEAKENING/STABLE) vs previous snapshot/day.
        PCR < 0.5: Bearish bias  |  PCR > 1.2: Bullish bias
        """
        try:
            logger.debug(f"[PCR_BIAS] {stock.stock_symbol} — start")

            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx or not sensibull_ctx.get("current"):
                logger.debug(f"[PCR_BIAS] {stock.stock_symbol} — no Sensibull current data, skip")
                return False

            stats = sensibull_ctx["current"].get("stats")
            if not stats:
                logger.debug(f"[PCR_BIAS] {stock.stock_symbol} — no stats in current data, skip")
                return False

            base_stats     = stats.get("underlying_base_stats", {})
            total_pcr      = base_stats.get("total_pcr")
            per_expiry_pcr = base_stats.get("per_expiry_pcr", {})

            if total_pcr is None:
                logger.debug(f"[PCR_BIAS] {stock.stock_symbol} — total_pcr missing, skip")
                return False

            prev_pcr = PCRAnalyser._get_prev_pcr(sensibull_ctx)

            logger.debug(
                f"[PCR_BIAS] {stock.stock_symbol} | SOURCE total_pcr={total_pcr:.3f} "
                f"prev_pcr={f'{prev_pcr:.3f}' if prev_pcr is not None else 'N/A'} "
                f"expiries={list(per_expiry_pcr.keys())}"
            )

            bearish_pass = total_pcr < PCRAnalyser.PCR_BEARISH_THRESHOLD
            bullish_pass = total_pcr > PCRAnalyser.PCR_BULLISH_THRESHOLD
            logger.debug(
                f"[PCR_BIAS] {stock.stock_symbol} | "
                f"CONDITION pcr={total_pcr:.3f} "
                f"bearish<{PCRAnalyser.PCR_BEARISH_THRESHOLD} → {'PASS' if bearish_pass else 'FAIL'} | "
                f"bullish>{PCRAnalyser.PCR_BULLISH_THRESHOLD} → {'PASS' if bullish_pass else 'FAIL'}"
            )

            if not (bearish_pass or bullish_pass):
                logger.debug(
                    f"[PCR_BIAS] {stock.stock_symbol} — no signal | "
                    f"pcr={total_pcr:.3f} in neutral range "
                    f"[{PCRAnalyser.PCR_BEARISH_THRESHOLD},{PCRAnalyser.PCR_BULLISH_THRESHOLD}]"
                )
                return False

            direction      = "BEARISH" if bearish_pass else "BULLISH"
            strength       = PCRAnalyser._bias_strength(total_pcr, direction)
            trend_dir      = PCRAnalyser._bias_trend_direction(total_pcr, prev_pcr, direction)

            logger.debug(
                f"[PCR_BIAS] {stock.stock_symbol} | "
                f"STRENGTH={strength} TREND_DIR={trend_dir}"
            )

            PCR_BIAS = namedtuple("PCR_BIAS", ["total_pcr", "bias", "per_expiry", "strength", "trend_direction"])

            if bearish_pass:
                stock.set_analysis("BEARISH", "PCR_BIAS", PCR_BIAS(
                    total_pcr=total_pcr,
                    bias="Bearish - More calls than puts",
                    per_expiry=per_expiry_pcr,
                    strength=strength,
                    trend_direction=trend_dir,
                ))
                logger.info(
                    f"[PCR_BIAS] {stock.stock_symbol} — SIGNAL BEARISH | "
                    f"pcr={total_pcr:.3f} < {PCRAnalyser.PCR_BEARISH_THRESHOLD} "
                    f"strength={strength} trend={trend_dir}"
                )
                return True

            stock.set_analysis("BULLISH", "PCR_BIAS", PCR_BIAS(
                total_pcr=total_pcr,
                bias="Bullish - More puts than calls",
                per_expiry=per_expiry_pcr,
                strength=strength,
                trend_direction=trend_dir,
            ))
            logger.info(
                f"[PCR_BIAS] {stock.stock_symbol} — SIGNAL BULLISH | "
                f"pcr={total_pcr:.3f} > {PCRAnalyser.PCR_BULLISH_THRESHOLD} "
                f"strength={strength} trend={trend_dir}"
            )
            return True

        except Exception as e:
            logger.error(f"Error in analyse_pcr_directional_bias for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.positional
    @BaseAnalyzer.index_positional
    def analyse_pcr_trend(self, stock: Stock):
        """
        Multi-day PCR trend using oi_history daily rows (positional only).
        Requires monotonic rise/fall over PCR_TREND_DAYS days, with both
        % change >= PCR_TREND_MIN_PCT and absolute change >= PCR_TREND_MIN_ABS.
        """
        try:
            logger.debug(f"[PCR_TREND] {stock.stock_symbol} — start")

            sensibull_ctx = stock.sensibull_ctx
            oi_history    = sensibull_ctx.get("oi_history", pd.DataFrame())

            if oi_history is None or oi_history.empty:
                logger.debug(f"[PCR_TREND] {stock.stock_symbol} — oi_history empty, skip")
                return False

            if "pcr" not in oi_history.columns:
                logger.debug(f"[PCR_TREND] {stock.stock_symbol} — no pcr column in oi_history, skip")
                return False

            if len(oi_history) < PCRAnalyser.PCR_TREND_DAYS:
                logger.debug(
                    f"[PCR_TREND] {stock.stock_symbol} — "
                    f"insufficient rows ({len(oi_history)}<{PCRAnalyser.PCR_TREND_DAYS}), skip"
                )
                return False

            pcr_series = oi_history["pcr"].dropna().tail(PCRAnalyser.PCR_TREND_DAYS)

            if len(pcr_series) < PCRAnalyser.PCR_TREND_DAYS:
                logger.debug(
                    f"[PCR_TREND] {stock.stock_symbol} — "
                    f"insufficient non-null PCR values ({len(pcr_series)}<{PCRAnalyser.PCR_TREND_DAYS}), skip"
                )
                return False

            pcr_list   = pcr_series.tolist()
            is_rising  = all(pcr_list[i] < pcr_list[i + 1] for i in range(len(pcr_list) - 1))
            is_falling = all(pcr_list[i] > pcr_list[i + 1] for i in range(len(pcr_list) - 1))
            pcr_change_pct = ((pcr_list[-1] - pcr_list[0]) / pcr_list[0]) * 100 if pcr_list[0] != 0 else 0
            pcr_change_abs = pcr_list[-1] - pcr_list[0]

            pct_pass = abs(pcr_change_pct) >= PCRAnalyser.PCR_TREND_MIN_PCT
            abs_pass = abs(pcr_change_abs) >= PCRAnalyser.PCR_TREND_MIN_ABS
            signal_pass = (is_rising or is_falling) and pct_pass and abs_pass

            logger.debug(
                f"[PCR_TREND] {stock.stock_symbol} | SOURCE "
                f"pcr_values={[f'{v:.3f}' for v in pcr_list]} rows_available={len(oi_history)}"
            )
            logger.debug(
                f"[PCR_TREND] {stock.stock_symbol} | "
                f"CONDITION is_rising={is_rising} is_falling={is_falling} "
                f"pcr_change={pcr_change_pct:.2f}%(min={PCRAnalyser.PCR_TREND_MIN_PCT}%) "
                f"abs={pcr_change_abs:+.3f}(min={PCRAnalyser.PCR_TREND_MIN_ABS}) → "
                f"{'PASS' if signal_pass else 'FAIL'}"
            )

            PCR_TREND = namedtuple("PCR_TREND", ["trend", "pcr_current", "pcr_change_pct", "pcr_change_abs", "values"])

            if is_rising and pct_pass and abs_pass:
                stock.set_analysis("BULLISH", "PCR_TREND", PCR_TREND(
                    trend="RISING",
                    pcr_current=pcr_list[-1],
                    pcr_change_pct=pcr_change_pct,
                    pcr_change_abs=pcr_change_abs,
                    values=pcr_list,
                ))
                logger.info(
                    f"[PCR_TREND] {stock.stock_symbol} — SIGNAL BULLISH | "
                    f"trend=RISING pcr_change={pcr_change_pct:.2f}% abs={pcr_change_abs:+.3f}"
                )
                return True

            if is_falling and pct_pass and abs_pass:
                stock.set_analysis("BEARISH", "PCR_TREND", PCR_TREND(
                    trend="FALLING",
                    pcr_current=pcr_list[-1],
                    pcr_change_pct=pcr_change_pct,
                    pcr_change_abs=pcr_change_abs,
                    values=pcr_list,
                ))
                logger.info(
                    f"[PCR_TREND] {stock.stock_symbol} — SIGNAL BEARISH | "
                    f"trend=FALLING pcr_change={pcr_change_pct:.2f}% abs={pcr_change_abs:+.3f}"
                )
                return True

            logger.debug(
                f"[PCR_TREND] {stock.stock_symbol} — no signal | "
                f"not monotonic or change too small (pct={pcr_change_pct:.1f}% abs={pcr_change_abs:+.3f})"
            )
            return False

        except Exception as e:
            logger.error(f"Error in analyse_pcr_trend for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.positional
    @BaseAnalyzer.index_positional
    def analyse_pcr_positional_reversal(self, stock: Stock):
        """
        Detect multi-day PCR reversal using oi_history daily rows.
        Uses 6 rows split as old=first 3, new=last 3 to smooth single-day noise.

        Three paths (same logic as intraday reversal but at daily granularity):
        1a. Zone crossover: old 3-day avg in BEARISH/BULLISH zone, new 3-day avg flipped
        1b. Neutral transition: was BEARISH/BULLISH, now entered NEUTRAL while moving toward flip
        2.  Trend reversal: 3 consecutive daily drops/rises followed by a reversal day > PCR_POS_REVERSAL_TREND_PCT
        """
        try:
            logger.debug(f"[PCR_POS_REV] {stock.stock_symbol} — start")

            sensibull_ctx = stock.sensibull_ctx
            oi_history    = sensibull_ctx.get("oi_history", pd.DataFrame())

            if oi_history is None or oi_history.empty:
                logger.debug(f"[PCR_POS_REV] {stock.stock_symbol} — oi_history empty, skip")
                return False

            if "pcr" not in oi_history.columns:
                logger.debug(f"[PCR_POS_REV] {stock.stock_symbol} — no pcr column in oi_history, skip")
                return False

            pcr_series = oi_history["pcr"].dropna()

            if len(pcr_series) < PCRAnalyser.PCR_POS_REVERSAL_MIN_ROWS:
                logger.debug(
                    f"[PCR_POS_REV] {stock.stock_symbol} — "
                    f"insufficient rows ({len(pcr_series)}<{PCRAnalyser.PCR_POS_REVERSAL_MIN_ROWS}), skip"
                )
                return False

            pcr_list    = pcr_series.tail(PCRAnalyser.PCR_POS_REVERSAL_MIN_ROWS).tolist()
            current_pcr = pcr_list[-1]

            PCR_POS_REVERSAL = namedtuple("PCR_POS_REVERSAL", [
                "reversal_type", "previous_pcr", "current_pcr",
                "previous_zone", "current_zone", "signal"
            ])

            def get_zone(pcr):
                if pcr < PCRAnalyser.PCR_BEARISH_THRESHOLD:
                    return "BEARISH"
                elif pcr > PCRAnalyser.PCR_BULLISH_THRESHOLD:
                    return "BULLISH"
                return "NEUTRAL"

            # 3-day averages — smooths single expiry-day spikes
            old_avg_pcr = sum(pcr_list[:3]) / 3
            new_avg_pcr = sum(pcr_list[3:]) / 3
            old_zone    = get_zone(old_avg_pcr)
            new_zone    = get_zone(new_avg_pcr)

            logger.debug(
                f"[PCR_POS_REV] {stock.stock_symbol} | SOURCE "
                f"pcr_values={[f'{v:.3f}' for v in pcr_list]} rows_available={len(pcr_series)} "
                f"old_avg={old_avg_pcr:.3f}({old_zone}) new_avg={new_avg_pcr:.3f}({new_zone})"
            )

            # ── Path 1a: full zone crossover ──────────────────────────────
            zone_cross_pass = old_zone != new_zone and old_zone != "NEUTRAL" and new_zone != "NEUTRAL"
            logger.debug(
                f"[PCR_POS_REV] {stock.stock_symbol} | "
                f"CONDITION zone_crossover old={old_zone}→new={new_zone} "
                f"different={old_zone != new_zone} both_non_neutral={old_zone != 'NEUTRAL' and new_zone != 'NEUTRAL'} → "
                f"{'PASS' if zone_cross_pass else 'FAIL'}"
            )

            if zone_cross_pass:
                if old_zone == "BEARISH" and new_zone == "BULLISH":
                    stock.set_analysis("BULLISH", "PCR_POS_REVERSAL", PCR_POS_REVERSAL(
                        reversal_type="ZONE_CROSSOVER",
                        previous_pcr=old_avg_pcr,
                        current_pcr=new_avg_pcr,
                        previous_zone=old_zone,
                        current_zone=new_zone,
                        signal="PCR 3-day avg crossed from bearish to bullish zone — multi-day sentiment flip",
                    ))
                    logger.info(
                        f"[PCR_POS_REV] {stock.stock_symbol} — SIGNAL BULLISH | "
                        f"type=ZONE_CROSSOVER {old_zone}→{new_zone} "
                        f"old_avg={old_avg_pcr:.3f} new_avg={new_avg_pcr:.3f}"
                    )
                    return True

                stock.set_analysis("BEARISH", "PCR_POS_REVERSAL", PCR_POS_REVERSAL(
                    reversal_type="ZONE_CROSSOVER",
                    previous_pcr=old_avg_pcr,
                    current_pcr=new_avg_pcr,
                    previous_zone=old_zone,
                    current_zone=new_zone,
                    signal="PCR 3-day avg crossed from bullish to bearish zone — multi-day sentiment flip",
                ))
                logger.info(
                    f"[PCR_POS_REV] {stock.stock_symbol} — SIGNAL BEARISH | "
                    f"type=ZONE_CROSSOVER {old_zone}→{new_zone} "
                    f"old_avg={old_avg_pcr:.3f} new_avg={new_avg_pcr:.3f}"
                )
                return True

            # ── Path 1b: neutral transition (early multi-day reversal) ────
            neutral_transition = old_zone != "NEUTRAL" and new_zone == "NEUTRAL"
            if neutral_transition:
                moving_up   = new_avg_pcr > old_avg_pcr
                moving_down = new_avg_pcr < old_avg_pcr
                logger.debug(
                    f"[PCR_POS_REV] {stock.stock_symbol} | "
                    f"CONDITION neutral_transition old={old_zone} "
                    f"moving_up={moving_up} moving_down={moving_down}"
                )
                if old_zone == "BEARISH" and moving_up:
                    stock.set_analysis("BULLISH", "PCR_POS_REVERSAL", PCR_POS_REVERSAL(
                        reversal_type="NEUTRAL_TRANSITION",
                        previous_pcr=old_avg_pcr,
                        current_pcr=new_avg_pcr,
                        previous_zone=old_zone,
                        current_zone=new_zone,
                        signal="PCR 3-day avg rising from bearish through neutral — early bullish shift",
                    ))
                    logger.info(
                        f"[PCR_POS_REV] {stock.stock_symbol} — SIGNAL BULLISH | "
                        f"type=NEUTRAL_TRANSITION BEARISH→NEUTRAL rising "
                        f"{old_avg_pcr:.3f}→{new_avg_pcr:.3f}"
                    )
                    return True

                if old_zone == "BULLISH" and moving_down:
                    stock.set_analysis("BEARISH", "PCR_POS_REVERSAL", PCR_POS_REVERSAL(
                        reversal_type="NEUTRAL_TRANSITION",
                        previous_pcr=old_avg_pcr,
                        current_pcr=new_avg_pcr,
                        previous_zone=old_zone,
                        current_zone=new_zone,
                        signal="PCR 3-day avg falling from bullish through neutral — early bearish shift",
                    ))
                    logger.info(
                        f"[PCR_POS_REV] {stock.stock_symbol} — SIGNAL BEARISH | "
                        f"type=NEUTRAL_TRANSITION BULLISH→NEUTRAL falling "
                        f"{old_avg_pcr:.3f}→{new_avg_pcr:.3f}"
                    )
                    return True

            # ── Path 2: trend reversal on last 4 daily deltas ─────────────
            # Use the last 4 daily values for delta checks (same logic as intraday)
            last4       = pcr_list[-4:]
            d1          = last4[1] - last4[0]
            d2          = last4[2] - last4[1]
            d3          = last4[3] - last4[2]

            logger.debug(
                f"[PCR_POS_REV] {stock.stock_symbol} | "
                f"CONDITION trend_reversal last4={[f'{v:.3f}' for v in last4]} "
                f"deltas=[{d1:+.3f},{d2:+.3f},{d3:+.3f}]"
            )

            if d1 > 0 and d2 > 0 and d3 < 0:
                magnitude = abs(d3 / last4[2]) * 100 if last4[2] != 0 else 0
                if magnitude >= PCRAnalyser.PCR_POS_REVERSAL_TREND_PCT:
                    stock.set_analysis("BEARISH", "PCR_POS_REVERSAL", PCR_POS_REVERSAL(
                        reversal_type="TREND_REVERSAL",
                        previous_pcr=last4[2],
                        current_pcr=current_pcr,
                        previous_zone=get_zone(last4[2]),
                        current_zone=get_zone(current_pcr),
                        signal=f"PCR trend reversed rising→falling over 3 days ({magnitude:.1f}% drop) — Bearish",
                    ))
                    logger.info(
                        f"[PCR_POS_REV] {stock.stock_symbol} — SIGNAL BEARISH | "
                        f"type=TREND_REVERSAL rising→falling magnitude={magnitude:.1f}%"
                    )
                    return True

            if d1 < 0 and d2 < 0 and d3 > 0:
                magnitude = abs(d3 / last4[2]) * 100 if last4[2] != 0 else 0
                if magnitude >= PCRAnalyser.PCR_POS_REVERSAL_TREND_PCT:
                    stock.set_analysis("BULLISH", "PCR_POS_REVERSAL", PCR_POS_REVERSAL(
                        reversal_type="TREND_REVERSAL",
                        previous_pcr=last4[2],
                        current_pcr=current_pcr,
                        previous_zone=get_zone(last4[2]),
                        current_zone=get_zone(current_pcr),
                        signal=f"PCR trend reversed falling→rising over 3 days ({magnitude:.1f}% rise) — Bullish",
                    ))
                    logger.info(
                        f"[PCR_POS_REV] {stock.stock_symbol} — SIGNAL BULLISH | "
                        f"type=TREND_REVERSAL falling→rising magnitude={magnitude:.1f}%"
                    )
                    return True

            logger.debug(
                f"[PCR_POS_REV] {stock.stock_symbol} — no signal | "
                f"no zone crossover, no neutral transition, no significant trend reversal"
            )
            return False

        except Exception as e:
            logger.error(f"Error in analyse_pcr_positional_reversal for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.intraday
    @BaseAnalyzer.index_intraday
    def analyse_pcr_intraday_trend(self, stock: Stock):
        """
        Intraday PCR trend using oi_chain_history per-cycle snapshots.
        Checks whether PCR has been consistently rising or falling across the session.
        Uses a window of up to the last 5 snapshots from oi_chain_history.
        """
        try:
            logger.debug(f"[PCR_INTRADAY_TREND] {stock.stock_symbol} — start")

            sensibull_ctx    = stock.sensibull_ctx
            oi_chain_history = sensibull_ctx.get("oi_chain_history", [])

            pcr_raw = [snap.get("pcr") for snap in oi_chain_history if snap.get("pcr") is not None]

            if len(pcr_raw) < PCRAnalyser.PCR_INTRADAY_MIN_SNAPSHOTS:
                logger.debug(
                    f"[PCR_INTRADAY_TREND] {stock.stock_symbol} — "
                    f"insufficient snapshots ({len(pcr_raw)}<{PCRAnalyser.PCR_INTRADAY_MIN_SNAPSHOTS}), skip"
                )
                return False

            # Use last 5 snapshots for trend to limit noise
            window   = pcr_raw[-5:] if len(pcr_raw) >= 5 else pcr_raw
            pcr_list = window

            logger.debug(
                f"[PCR_INTRADAY_TREND] {stock.stock_symbol} | SOURCE "
                f"pcr_snapshots={[f'{v:.3f}' for v in pcr_list]} "
                f"total_history={len(pcr_raw)}"
            )

            is_rising  = all(pcr_list[i] < pcr_list[i + 1] for i in range(len(pcr_list) - 1))
            is_falling = all(pcr_list[i] > pcr_list[i + 1] for i in range(len(pcr_list) - 1))
            pcr_change_pct = ((pcr_list[-1] - pcr_list[0]) / pcr_list[0]) * 100 if pcr_list[0] != 0 else 0

            pct_pass    = abs(pcr_change_pct) >= PCRAnalyser.PCR_INTRADAY_TREND_MIN_PCT
            signal_pass = (is_rising or is_falling) and pct_pass
            logger.debug(
                f"[PCR_INTRADAY_TREND] {stock.stock_symbol} | "
                f"CONDITION is_rising={is_rising} is_falling={is_falling} "
                f"pcr_change={pcr_change_pct:.2f}%(min={PCRAnalyser.PCR_INTRADAY_TREND_MIN_PCT}%) → "
                f"{'PASS' if signal_pass else 'FAIL'}"
            )

            PCR_INTRADAY_TREND = namedtuple(
                "PCR_INTRADAY_TREND",
                ["trend", "pcr_first", "pcr_last", "pcr_change_pct", "snapshots"]
            )

            if is_rising and pct_pass:
                stock.set_analysis("BULLISH", "PCR_INTRADAY_TREND", PCR_INTRADAY_TREND(
                    trend="RISING",
                    pcr_first=pcr_list[0],
                    pcr_last=pcr_list[-1],
                    pcr_change_pct=pcr_change_pct,
                    snapshots=len(pcr_list),
                ))
                logger.info(
                    f"[PCR_INTRADAY_TREND] {stock.stock_symbol} — SIGNAL BULLISH | "
                    f"trend=RISING {pcr_list[0]:.3f}→{pcr_list[-1]:.3f} "
                    f"change={pcr_change_pct:+.2f}% over {len(pcr_list)} snapshots"
                )
                return True

            if is_falling and pct_pass:
                stock.set_analysis("BEARISH", "PCR_INTRADAY_TREND", PCR_INTRADAY_TREND(
                    trend="FALLING",
                    pcr_first=pcr_list[0],
                    pcr_last=pcr_list[-1],
                    pcr_change_pct=pcr_change_pct,
                    snapshots=len(pcr_list),
                ))
                logger.info(
                    f"[PCR_INTRADAY_TREND] {stock.stock_symbol} — SIGNAL BEARISH | "
                    f"trend=FALLING {pcr_list[0]:.3f}→{pcr_list[-1]:.3f} "
                    f"change={pcr_change_pct:+.2f}% over {len(pcr_list)} snapshots"
                )
                return True

            logger.debug(
                f"[PCR_INTRADAY_TREND] {stock.stock_symbol} — no signal | "
                f"not monotonic or change too small ({pcr_change_pct:.1f}%)"
            )
            return False

        except Exception as e:
            logger.error(f"Error in analyse_pcr_intraday_trend for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_pcr_divergence(self, stock: Stock):
        """
        Detect meaningful PCR divergence between near and far expiry.
        Threshold lowered from 1.2 → 0.35 (realistic for normal market conditions).
        Direction: near < 0.5 and far > 0.8 → BEARISH (near-term call-heavy, far-term put-heavy)
                   near > 0.8 and far < 0.5 → BULLISH (near-term put-heavy, far-term call-heavy)
                   Otherwise → NEUTRAL with divergence noted.
        """
        try:
            logger.debug(f"[PCR_DIV] {stock.stock_symbol} — start")

            sensibull_ctx = stock.sensibull_ctx
            if not sensibull_ctx or not sensibull_ctx.get("current"):
                logger.debug(f"[PCR_DIV] {stock.stock_symbol} — no Sensibull current data, skip")
                return False

            stats = sensibull_ctx["current"].get("stats")
            if not stats:
                logger.debug(f"[PCR_DIV] {stock.stock_symbol} — no stats in current data, skip")
                return False

            base_stats     = stats.get("underlying_base_stats", {})
            per_expiry_pcr = base_stats.get("per_expiry_pcr", {})

            if not per_expiry_pcr or len(per_expiry_pcr) < 2:
                logger.debug(
                    f"[PCR_DIV] {stock.stock_symbol} — "
                    f"per_expiry_pcr missing or fewer than 2 expiries, skip"
                )
                return False

            sorted_expiries = sorted(per_expiry_pcr.items())
            near_month_expiry, near_month_pcr = sorted_expiries[0]
            far_month_expiry,  far_month_pcr  = sorted_expiries[1]

            if near_month_pcr is None or far_month_pcr is None:
                logger.debug(f"[PCR_DIV] {stock.stock_symbol} — null PCR value in expiries, skip")
                return False

            logger.debug(
                f"[PCR_DIV] {stock.stock_symbol} | SOURCE "
                f"near={near_month_expiry}:{near_month_pcr:.3f} "
                f"far={far_month_expiry}:{far_month_pcr:.3f}"
            )

            pcr_diff = abs(near_month_pcr - far_month_pcr)
            div_pass = pcr_diff > PCRAnalyser.PCR_DIVERGENCE_THRESHOLD
            logger.debug(
                f"[PCR_DIV] {stock.stock_symbol} | "
                f"CONDITION pcr_diff={pcr_diff:.3f} "
                f"min_diff={PCRAnalyser.PCR_DIVERGENCE_THRESHOLD} → {'PASS' if div_pass else 'FAIL'}"
            )

            if not div_pass:
                logger.debug(
                    f"[PCR_DIV] {stock.stock_symbol} — no signal | "
                    f"diff={pcr_diff:.3f} < {PCRAnalyser.PCR_DIVERGENCE_THRESHOLD}"
                )
                return False

            # Determine direction based on near vs far zone
            if near_month_pcr < 0.5 and far_month_pcr > 0.8:
                direction = "BEARISH"
                signal    = "Near-term call-heavy, far-term put-heavy — near-term bearish pressure"
            elif near_month_pcr > 0.8 and far_month_pcr < 0.5:
                direction = "BULLISH"
                signal    = "Near-term put-heavy, far-term call-heavy — near-term bullish protection"
            elif near_month_pcr < far_month_pcr:
                direction = "BEARISH"
                signal    = "Far expiry put interest dominant — longer-term hedging detected"
            else:
                direction = "BULLISH"
                signal    = "Near expiry put interest dominant — near-term protective buying"

            logger.debug(
                f"[PCR_DIV] {stock.stock_symbol} | "
                f"DIRECTION={direction} signal={signal}"
            )

            PCR_DIVERGENCE = namedtuple(
                "PCR_DIVERGENCE",
                ["near_month_pcr", "far_month_pcr", "divergence", "signal"]
            )

            stock.set_analysis(direction, "PCR_DIVERGENCE", PCR_DIVERGENCE(
                near_month_pcr=near_month_pcr,
                far_month_pcr=far_month_pcr,
                divergence=pcr_diff,
                signal=signal,
            ))
            logger.info(
                f"[PCR_DIV] {stock.stock_symbol} — SIGNAL {direction} | "
                f"near={near_month_pcr:.3f} far={far_month_pcr:.3f} "
                f"diff={pcr_diff:.3f} | {signal}"
            )
            return True

        except Exception as e:
            logger.error(f"Error in analyse_pcr_divergence for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.intraday
    @BaseAnalyzer.index_intraday
    def analyse_pcr_reversal(self, stock: Stock):
        """
        Detect PCR reversal using oi_chain_history per-cycle snapshots (not historical_data).
        Two paths:
        1. Zone crossover: avg of first 2 snapshots vs avg of last 2 snapshots crossed bias zones.
           Also catches BEARISH→NEUTRAL or BULLISH→NEUTRAL transitions (early reversal).
        2. Trend reversal: 3 monotonic steps followed by a reversal > PCR_REVERSAL_TREND_MIN_PCT.
        """
        try:
            logger.debug(f"[PCR_REV] {stock.stock_symbol} — start")

            sensibull_ctx    = stock.sensibull_ctx
            oi_chain_history = sensibull_ctx.get("oi_chain_history", [])

            pcr_raw = [snap.get("pcr") for snap in oi_chain_history if snap.get("pcr") is not None]

            if len(pcr_raw) < PCRAnalyser.PCR_REVERSAL_MIN_SNAPSHOTS:
                logger.debug(
                    f"[PCR_REV] {stock.stock_symbol} — "
                    f"insufficient snapshots ({len(pcr_raw)}<{PCRAnalyser.PCR_REVERSAL_MIN_SNAPSHOTS}), skip"
                )
                return False

            pcr_list    = pcr_raw[-4:]
            current_pcr = pcr_list[-1]

            PCR_REVERSAL = namedtuple("PCR_REVERSAL", [
                "reversal_type", "previous_pcr", "current_pcr",
                "previous_zone", "current_zone", "signal"
            ])

            def get_zone(pcr):
                if pcr < PCRAnalyser.PCR_BEARISH_THRESHOLD:
                    return "BEARISH"
                elif pcr > PCRAnalyser.PCR_BULLISH_THRESHOLD:
                    return "BULLISH"
                return "NEUTRAL"

            old_avg_pcr = (pcr_list[0] + pcr_list[1]) / 2
            new_avg_pcr = (pcr_list[2] + pcr_list[3]) / 2
            old_zone    = get_zone(old_avg_pcr)
            new_zone    = get_zone(new_avg_pcr)

            logger.debug(
                f"[PCR_REV] {stock.stock_symbol} | SOURCE "
                f"pcr_values={[f'{v:.3f}' for v in pcr_list]} "
                f"old_avg={old_avg_pcr:.3f}({old_zone}) new_avg={new_avg_pcr:.3f}({new_zone})"
            )

            # ── Path 1a: full zone crossover (BEARISH↔BULLISH) ─────────────
            zone_cross_pass = old_zone != new_zone and old_zone != "NEUTRAL" and new_zone != "NEUTRAL"
            logger.debug(
                f"[PCR_REV] {stock.stock_symbol} | "
                f"CONDITION zone_crossover old={old_zone}→new={new_zone} "
                f"different={old_zone != new_zone} both_non_neutral={old_zone != 'NEUTRAL' and new_zone != 'NEUTRAL'} → "
                f"{'PASS' if zone_cross_pass else 'FAIL'}"
            )

            if zone_cross_pass:
                if old_zone == "BEARISH" and new_zone == "BULLISH":
                    stock.set_analysis("BULLISH", "PCR_REVERSAL", PCR_REVERSAL(
                        reversal_type="ZONE_CROSSOVER",
                        previous_pcr=old_avg_pcr,
                        current_pcr=new_avg_pcr,
                        previous_zone=old_zone,
                        current_zone=new_zone,
                        signal="PCR reversed from bearish to bullish zone (Bullish)",
                    ))
                    logger.info(
                        f"[PCR_REV] {stock.stock_symbol} — SIGNAL BULLISH | "
                        f"type=ZONE_CROSSOVER {old_zone}→{new_zone} "
                        f"old_avg={old_avg_pcr:.3f} new_avg={new_avg_pcr:.3f}"
                    )
                    return True

                stock.set_analysis("BEARISH", "PCR_REVERSAL", PCR_REVERSAL(
                    reversal_type="ZONE_CROSSOVER",
                    previous_pcr=old_avg_pcr,
                    current_pcr=new_avg_pcr,
                    previous_zone=old_zone,
                    current_zone=new_zone,
                    signal="PCR reversed from bullish to bearish zone (Bearish)",
                ))
                logger.info(
                    f"[PCR_REV] {stock.stock_symbol} — SIGNAL BEARISH | "
                    f"type=ZONE_CROSSOVER {old_zone}→{new_zone} "
                    f"old_avg={old_avg_pcr:.3f} new_avg={new_avg_pcr:.3f}"
                )
                return True

            # ── Path 1b: transition through neutral (early reversal) ───────
            # BEARISH→NEUTRAL with PCR rising = moving toward bullish
            # BULLISH→NEUTRAL with PCR falling = moving toward bearish
            neutral_transition = old_zone != "NEUTRAL" and new_zone == "NEUTRAL"
            if neutral_transition:
                moving_up   = new_avg_pcr > old_avg_pcr
                moving_down = new_avg_pcr < old_avg_pcr
                logger.debug(
                    f"[PCR_REV] {stock.stock_symbol} | "
                    f"CONDITION neutral_transition old={old_zone} moving_up={moving_up} moving_down={moving_down}"
                )
                if old_zone == "BEARISH" and moving_up:
                    stock.set_analysis("BULLISH", "PCR_REVERSAL", PCR_REVERSAL(
                        reversal_type="NEUTRAL_TRANSITION",
                        previous_pcr=old_avg_pcr,
                        current_pcr=new_avg_pcr,
                        previous_zone=old_zone,
                        current_zone=new_zone,
                        signal="PCR rising from bearish zone through neutral — early bullish shift",
                    ))
                    logger.info(
                        f"[PCR_REV] {stock.stock_symbol} — SIGNAL BULLISH | "
                        f"type=NEUTRAL_TRANSITION BEARISH→NEUTRAL rising "
                        f"{old_avg_pcr:.3f}→{new_avg_pcr:.3f}"
                    )
                    return True

                if old_zone == "BULLISH" and moving_down:
                    stock.set_analysis("BEARISH", "PCR_REVERSAL", PCR_REVERSAL(
                        reversal_type="NEUTRAL_TRANSITION",
                        previous_pcr=old_avg_pcr,
                        current_pcr=new_avg_pcr,
                        previous_zone=old_zone,
                        current_zone=new_zone,
                        signal="PCR falling from bullish zone through neutral — early bearish shift",
                    ))
                    logger.info(
                        f"[PCR_REV] {stock.stock_symbol} — SIGNAL BEARISH | "
                        f"type=NEUTRAL_TRANSITION BULLISH→NEUTRAL falling "
                        f"{old_avg_pcr:.3f}→{new_avg_pcr:.3f}"
                    )
                    return True

            # ── Path 2: trend reversal (direction flip on 4th delta) ───────
            first_trend  = pcr_list[1] - pcr_list[0]
            second_trend = pcr_list[2] - pcr_list[1]
            third_trend  = pcr_list[3] - pcr_list[2]

            logger.debug(
                f"[PCR_REV] {stock.stock_symbol} | "
                f"CONDITION trend_reversal "
                f"deltas=[{first_trend:+.3f},{second_trend:+.3f},{third_trend:+.3f}]"
            )

            if first_trend > 0 and second_trend > 0 and third_trend < 0:
                magnitude = abs(third_trend / pcr_list[2]) * 100 if pcr_list[2] != 0 else 0
                if magnitude >= PCRAnalyser.PCR_REVERSAL_TREND_MIN_PCT:
                    stock.set_analysis("BEARISH", "PCR_REVERSAL", PCR_REVERSAL(
                        reversal_type="TREND_REVERSAL",
                        previous_pcr=pcr_list[2],
                        current_pcr=current_pcr,
                        previous_zone=get_zone(pcr_list[2]),
                        current_zone=get_zone(current_pcr),
                        signal=f"PCR trend reversed rising→falling ({magnitude:.1f}% drop) - Bearish",
                    ))
                    logger.info(
                        f"[PCR_REV] {stock.stock_symbol} — SIGNAL BEARISH | "
                        f"type=TREND_REVERSAL rising→falling magnitude={magnitude:.1f}%"
                    )
                    return True

            if first_trend < 0 and second_trend < 0 and third_trend > 0:
                magnitude = abs(third_trend / pcr_list[2]) * 100 if pcr_list[2] != 0 else 0
                if magnitude >= PCRAnalyser.PCR_REVERSAL_TREND_MIN_PCT:
                    stock.set_analysis("BULLISH", "PCR_REVERSAL", PCR_REVERSAL(
                        reversal_type="TREND_REVERSAL",
                        previous_pcr=pcr_list[2],
                        current_pcr=current_pcr,
                        previous_zone=get_zone(pcr_list[2]),
                        current_zone=get_zone(current_pcr),
                        signal=f"PCR trend reversed falling→rising ({magnitude:.1f}% rise) - Bullish",
                    ))
                    logger.info(
                        f"[PCR_REV] {stock.stock_symbol} — SIGNAL BULLISH | "
                        f"type=TREND_REVERSAL falling→rising magnitude={magnitude:.1f}%"
                    )
                    return True

            logger.debug(
                f"[PCR_REV] {stock.stock_symbol} — no signal | "
                f"no zone crossover, no neutral transition, no significant trend reversal"
            )
            return False

        except Exception as e:
            logger.error(f"Error in analyse_pcr_reversal for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
