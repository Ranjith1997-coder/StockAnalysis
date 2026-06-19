import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
import common.shared as shared
from collections import namedtuple
import pandas as pd
import numpy as np


class OIChainAnalyser(BaseAnalyzer):
    """
    Analyzes per-strike Open Interest (OI) chain data from Sensibull.
    
    Uses per_strike_data (call_oi, put_oi, prev_call_oi, prev_put_oi) to generate:
    1. OI-based Support/Resistance identification
    2. OI Buildup detection (fresh writing / unwinding)
    3. OI Wall detection (massive concentrated OI barriers)
    4. Max Pain calculation from raw OI data
    5. OI Shift / Position migration analysis
    
    Data Source: Sensibull OI chain API endpoint providing per-strike OI snapshots
    with previous day comparison.
    """
    
    # ── Thresholds (tuned for abnormal-only signalling) ────────────────────
    # OI Support/Resistance
    SUPPORT_RESISTANCE_PROXIMITY_PCT = 1.5  # Only breach/very tight proximity signals
    SR_MIN_OI_DOMINANCE = 1.5               # S/R strike OI must be >= 1.5x avg OI to count
    
    # OI Buildup
    OI_BUILDUP_MIN_CHANGE_PCT = 100         # 100% change at a strike to be significant
    OI_BUILDUP_HEAVY_RATIO = 3.0            # Call/Put OI ratio for "heavy writing"
    OI_BUILDUP_DOMINANT_RATIO = 5.0         # Ratio for "dominant writing" (moderate signal)
    OI_BUILDUP_MIN_STRIKES = 3              # Min significant strikes to trigger
    OI_BUILDUP_MIN_TOTAL_CHANGE_PCT = 5.0   # Total OI change must be > 5% of total OI
    
    # OI Wall
    OI_WALL_STD_MULTIPLIER = 2.0            # Wall = mean + N*std (statistical outlier)
    OI_WALL_MAX_DISTANCE_PCT = 5.0          # Only walls within 5% of price
    OI_WALL_MIN_DISTANCE_PCT = 0.5          # Exclude ATM strikes (< 0.5% from price is just ATM)
    OI_WALL_MIN_ASYMMETRY_RATIO = 2.0       # Distance ratio for asymmetry signal
    
    # OI Shift
    OI_SHIFT_MIN_WRITING_RATIO = 5.0        # 5x imbalance required for shift signal
    OI_SHIFT_CENTER_THRESHOLD_PCT = 3.0     # OI center must be >3% from price for directional
    
    # Intraday OI Trend
    OI_TREND_MIN_SNAPSHOTS = 5              # Need 5+ snapshots for meaningful trend
    OI_TREND_MIN_PCR_CHANGE_PCT = 8.0       # PCR must move 8%+ for trend signal
    OI_TREND_MIN_OI_CHANGE_PCT = 5.0        # Single-side OI must change 5%+ 
    
    # Intraday S/R Shift
    OI_SR_SHIFT_MIN_SNAPSHOTS = 5           # Need 5+ snapshots
    OI_SR_SHIFT_MIN_STRIKE_WIDTHS = 2       # Must shift by at least 2 strike widths

    # OI Capitulation (positional-only)
    OI_CAPITULATION_MIN_CHANGE_PCT    = 30      # Strike OI must have dropped >= 30% vs prev day
    OI_CAPITULATION_MIN_ABS_REDUCTION = 50_000  # Absolute contracts removed (filters retail noise)
    OI_CAPITULATION_MIN_STRIKES       = 2       # At least 2 qualifying strikes required
    OI_CAPITULATION_MIN_TOTAL_PCT     = 8.0     # Unwound OI must be >= 8% of that side's total OI
    OI_CAPITULATION_DISTANCE_PCT      = 8.0     # Only evaluate strikes within ±8% of spot

    # OI Wall Migration (positional-only)
    OI_WALL_MIGRATION_MIN_POINTS      = 1       # Wall must shift by >= 1 strike width to signal

    # Shared expiry guard (used by both positional methods)
    OI_EXPIRY_TOTAL_DROP_GUARD_PCT    = 80.0    # Skip analysis if >80% total OI vanished (expiry day)

    # OI Positional Trend — analyse_positional_oi_trend (positional-only)
    OI_POSITIONAL_TREND_DAYS          = 5       # Look-back window in trading days
    OI_POSITIONAL_TREND_MIN_PCT       = 15.0    # One side must grow >= 15% over N days to signal
    OI_POSITIONAL_TREND_DIFF_PCT      = 10.0    # Leading side must exceed lagging side by >= 10%

    # OI Acceleration — analyse_oi_acceleration (positional-only)
    OI_ACCEL_MIN_RATIO                = 2.0     # Recent 3-day velocity must be >= 2x prior 3-day
    OI_ACCEL_MIN_VELOCITY             = 2_000_000  # Recent mean daily change must exceed this (absolute)
    OI_ACCEL_MIN_BASE                 = 500_000    # Prior mean daily change must exceed this to avoid noise
    
    def __init__(self) -> None:
        self.analyserName = "OI Chain Analyser"
        super().__init__()
    
    def reset_constants(self):
        """Reset constants based on mode — intraday is slightly more sensitive
        since we have real-time snapshots, but still requires abnormal moves."""
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            OIChainAnalyser.SUPPORT_RESISTANCE_PROXIMITY_PCT = 1.0
            OIChainAnalyser.SR_MIN_OI_DOMINANCE = 1.5
            OIChainAnalyser.OI_BUILDUP_MIN_CHANGE_PCT = 75
            OIChainAnalyser.OI_BUILDUP_HEAVY_RATIO = 2.5
            OIChainAnalyser.OI_BUILDUP_DOMINANT_RATIO = 4.0
            OIChainAnalyser.OI_BUILDUP_MIN_STRIKES = 3
            OIChainAnalyser.OI_BUILDUP_MIN_TOTAL_CHANGE_PCT = 3.0
            OIChainAnalyser.OI_WALL_STD_MULTIPLIER = 1.8
            OIChainAnalyser.OI_WALL_MAX_DISTANCE_PCT = 3.0
            OIChainAnalyser.OI_WALL_MIN_DISTANCE_PCT = 0.5
            OIChainAnalyser.OI_WALL_MIN_ASYMMETRY_RATIO = 1.8
            OIChainAnalyser.OI_SHIFT_MIN_WRITING_RATIO = 4.0
            OIChainAnalyser.OI_SHIFT_CENTER_THRESHOLD_PCT = 2.0
            OIChainAnalyser.OI_TREND_MIN_SNAPSHOTS = 5
            OIChainAnalyser.OI_TREND_MIN_PCR_CHANGE_PCT = 8.0
            OIChainAnalyser.OI_TREND_MIN_OI_CHANGE_PCT = 5.0
            OIChainAnalyser.OI_SR_SHIFT_MIN_SNAPSHOTS = 5
            OIChainAnalyser.OI_SR_SHIFT_MIN_STRIKE_WIDTHS = 2
            # Positional-only — same values in both modes
            OIChainAnalyser.OI_CAPITULATION_MIN_CHANGE_PCT    = 30
            OIChainAnalyser.OI_CAPITULATION_MIN_ABS_REDUCTION = 50_000
            OIChainAnalyser.OI_CAPITULATION_MIN_STRIKES       = 2
            OIChainAnalyser.OI_CAPITULATION_MIN_TOTAL_PCT     = 8.0
            OIChainAnalyser.OI_CAPITULATION_DISTANCE_PCT      = 8.0
            OIChainAnalyser.OI_WALL_MIGRATION_MIN_POINTS      = 1
            OIChainAnalyser.OI_EXPIRY_TOTAL_DROP_GUARD_PCT    = 80.0
            OIChainAnalyser.OI_POSITIONAL_TREND_DAYS          = 5
            OIChainAnalyser.OI_POSITIONAL_TREND_MIN_PCT       = 15.0
            OIChainAnalyser.OI_POSITIONAL_TREND_DIFF_PCT      = 10.0
            OIChainAnalyser.OI_ACCEL_MIN_RATIO                = 2.0
            OIChainAnalyser.OI_ACCEL_MIN_VELOCITY             = 2_000_000
            OIChainAnalyser.OI_ACCEL_MIN_BASE                 = 500_000
        else:
            OIChainAnalyser.SUPPORT_RESISTANCE_PROXIMITY_PCT = 1.5
            OIChainAnalyser.SR_MIN_OI_DOMINANCE = 1.5
            OIChainAnalyser.OI_BUILDUP_MIN_CHANGE_PCT = 100
            OIChainAnalyser.OI_BUILDUP_HEAVY_RATIO = 3.0
            OIChainAnalyser.OI_BUILDUP_DOMINANT_RATIO = 5.0
            OIChainAnalyser.OI_BUILDUP_MIN_STRIKES = 3
            OIChainAnalyser.OI_BUILDUP_MIN_TOTAL_CHANGE_PCT = 5.0
            OIChainAnalyser.OI_WALL_STD_MULTIPLIER = 2.0
            OIChainAnalyser.OI_WALL_MAX_DISTANCE_PCT = 5.0
            OIChainAnalyser.OI_WALL_MIN_DISTANCE_PCT = 0.5
            OIChainAnalyser.OI_WALL_MIN_ASYMMETRY_RATIO = 2.0
            OIChainAnalyser.OI_SHIFT_MIN_WRITING_RATIO = 5.0
            OIChainAnalyser.OI_SHIFT_CENTER_THRESHOLD_PCT = 3.0
            OIChainAnalyser.OI_TREND_MIN_SNAPSHOTS = 5
            OIChainAnalyser.OI_TREND_MIN_PCR_CHANGE_PCT = 8.0
            OIChainAnalyser.OI_TREND_MIN_OI_CHANGE_PCT = 5.0
            OIChainAnalyser.OI_SR_SHIFT_MIN_SNAPSHOTS = 5
            OIChainAnalyser.OI_SR_SHIFT_MIN_STRIKE_WIDTHS = 2
            # Positional-only — same values in both modes
            OIChainAnalyser.OI_CAPITULATION_MIN_CHANGE_PCT    = 30
            OIChainAnalyser.OI_CAPITULATION_MIN_ABS_REDUCTION = 50_000
            OIChainAnalyser.OI_CAPITULATION_MIN_STRIKES       = 2
            OIChainAnalyser.OI_CAPITULATION_MIN_TOTAL_PCT     = 8.0
            OIChainAnalyser.OI_CAPITULATION_DISTANCE_PCT      = 8.0
            OIChainAnalyser.OI_WALL_MIGRATION_MIN_POINTS      = 1
            OIChainAnalyser.OI_EXPIRY_TOTAL_DROP_GUARD_PCT    = 80.0
            OIChainAnalyser.OI_POSITIONAL_TREND_DAYS          = 5
            OIChainAnalyser.OI_POSITIONAL_TREND_MIN_PCT       = 15.0
            OIChainAnalyser.OI_POSITIONAL_TREND_DIFF_PCT      = 10.0
            OIChainAnalyser.OI_ACCEL_MIN_RATIO                = 2.0
            OIChainAnalyser.OI_ACCEL_MIN_VELOCITY             = 2_000_000
            OIChainAnalyser.OI_ACCEL_MIN_BASE                 = 500_000

        logger.debug(
            f"[OIChainAnalyser] constants reset | mode={shared.app_ctx.mode.name} | "
            f"SR: proximity={OIChainAnalyser.SUPPORT_RESISTANCE_PROXIMITY_PCT}% "
            f"dominance={OIChainAnalyser.SR_MIN_OI_DOMINANCE}x | "
            f"Buildup: min_change={OIChainAnalyser.OI_BUILDUP_MIN_CHANGE_PCT}% "
            f"heavy={OIChainAnalyser.OI_BUILDUP_HEAVY_RATIO}x "
            f"dominant={OIChainAnalyser.OI_BUILDUP_DOMINANT_RATIO}x "
            f"min_strikes={OIChainAnalyser.OI_BUILDUP_MIN_STRIKES} "
            f"min_total_chg={OIChainAnalyser.OI_BUILDUP_MIN_TOTAL_CHANGE_PCT}% | "
            f"Wall: std_mult={OIChainAnalyser.OI_WALL_STD_MULTIPLIER} "
            f"min_dist={OIChainAnalyser.OI_WALL_MIN_DISTANCE_PCT}% "
            f"max_dist={OIChainAnalyser.OI_WALL_MAX_DISTANCE_PCT}% "
            f"asymmetry={OIChainAnalyser.OI_WALL_MIN_ASYMMETRY_RATIO}x | "
            f"Shift: min_writing={OIChainAnalyser.OI_SHIFT_MIN_WRITING_RATIO}x "
            f"center_thresh={OIChainAnalyser.OI_SHIFT_CENTER_THRESHOLD_PCT}% | "
            f"Trend: min_snaps={OIChainAnalyser.OI_TREND_MIN_SNAPSHOTS} "
            f"min_pcr_chg={OIChainAnalyser.OI_TREND_MIN_PCR_CHANGE_PCT}% "
            f"min_oi_chg={OIChainAnalyser.OI_TREND_MIN_OI_CHANGE_PCT}% | "
            f"SRShift: min_snaps={OIChainAnalyser.OI_SR_SHIFT_MIN_SNAPSHOTS} "
            f"min_widths={OIChainAnalyser.OI_SR_SHIFT_MIN_STRIKE_WIDTHS} | "
            f"Capitulation: min_chg={OIChainAnalyser.OI_CAPITULATION_MIN_CHANGE_PCT}% "
            f"min_abs={OIChainAnalyser.OI_CAPITULATION_MIN_ABS_REDUCTION:,} "
            f"min_strikes={OIChainAnalyser.OI_CAPITULATION_MIN_STRIKES} "
            f"min_total={OIChainAnalyser.OI_CAPITULATION_MIN_TOTAL_PCT}% "
            f"dist={OIChainAnalyser.OI_CAPITULATION_DISTANCE_PCT}% | "
            f"WallMigration: min_points={OIChainAnalyser.OI_WALL_MIGRATION_MIN_POINTS} | "
            f"ExpiryGuard: drop>{OIChainAnalyser.OI_EXPIRY_TOTAL_DROP_GUARD_PCT}%=skip | "
            f"PositionalTrend: days={OIChainAnalyser.OI_POSITIONAL_TREND_DAYS} "
            f"min_pct={OIChainAnalyser.OI_POSITIONAL_TREND_MIN_PCT}% "
            f"diff_pct={OIChainAnalyser.OI_POSITIONAL_TREND_DIFF_PCT}% | "
            f"Acceleration: min_ratio={OIChainAnalyser.OI_ACCEL_MIN_RATIO}x "
            f"min_velocity={OIChainAnalyser.OI_ACCEL_MIN_VELOCITY:,} "
            f"min_base={OIChainAnalyser.OI_ACCEL_MIN_BASE:,}"
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _get_oi_chain_data(stock: Stock):
        """
        Extract latest OI chain snapshot from stock's sensibull_ctx.
        Returns (per_strike_data, meta) or (None, None) if unavailable.
        """
        oi_chain = stock.sensibull_ctx.get("oi_chain")
        if not oi_chain:
            return None, None
        
        per_strike_data = oi_chain.get("per_strike_data")
        if not per_strike_data or len(per_strike_data) == 0:
            return None, None
        
        meta = {
            "current_ltp": oi_chain.get("current_ltp"),
            "prev_ltp": oi_chain.get("prev_ltp"),
            "atm_strike": oi_chain.get("atm_strike"),
            "pcr": oi_chain.get("pcr"),
            "total_call_oi": oi_chain.get("total_call_oi", 0),
            "total_put_oi": oi_chain.get("total_put_oi", 0),
            "total_call_oi_change": oi_chain.get("total_call_oi_change", 0),
            "total_put_oi_change": oi_chain.get("total_put_oi_change", 0),
            "expiry": oi_chain.get("expiry"),
        }
        
        return per_strike_data, meta

    @staticmethod
    def _get_oi_chain_history(stock: Stock, min_snapshots=3):
        """
        Extract OI chain history from stock's sensibull_ctx.
        Returns list of snapshots or None if insufficient data.
        Each snapshot has: timestamp, total_call_oi, total_put_oi, pcr,
                          current_ltp, per_strike_data, etc.
        """
        history = stock.sensibull_ctx.get("oi_chain_history", [])
        if len(history) < min_snapshots:
            return None
        return history

    @staticmethod
    def _find_max_oi_strike(per_strike_data, oi_key):
        """
        Find the strike with maximum OI for a given key (call_oi or put_oi).
        Returns (strike, oi_value) or (None, 0).
        """
        max_oi = 0
        max_strike = None
        for strike_str, data in per_strike_data.items():
            oi = data.get(oi_key, 0)
            if oi > max_oi:
                max_oi = oi
                max_strike = float(strike_str)
        return max_strike, max_oi

    @staticmethod
    def _expiry_guard(per_strike_data: dict) -> bool:
        """
        Returns True if the OI chain looks like an expiry-day reset and analysis
        should be skipped.

        On expiry day all contracts settle to zero, so every strike will show
        near-100% OI reduction. Without this guard, capitulation and wall-migration
        would fire massive false signals.

        Fires when total OI across all strikes dropped > OI_EXPIRY_TOTAL_DROP_GUARD_PCT
        compared to yesterday's total (prev_call_oi + prev_put_oi).
        """
        prev_total = sum(
            d.get("prev_call_oi", 0) + d.get("prev_put_oi", 0)
            for d in per_strike_data.values()
        )
        if prev_total == 0:
            return False
        curr_total = sum(
            d.get("call_oi", 0) + d.get("put_oi", 0)
            for d in per_strike_data.values()
        )
        drop_pct = (prev_total - curr_total) / prev_total * 100
        if drop_pct > OIChainAnalyser.OI_EXPIRY_TOTAL_DROP_GUARD_PCT:
            logger.debug(
                f"[ExpiryGuard] total OI dropped {drop_pct:.1f}% "
                f"(prev={prev_total:,.0f} curr={curr_total:,.0f}) "
                f"> threshold={OIChainAnalyser.OI_EXPIRY_TOTAL_DROP_GUARD_PCT}% — expiry day, skip"
            )
            return True
        return False

    @staticmethod
    def _find_dominant_wall(
        per_strike_data: dict,
        oi_key: str,
        spot: float,
        std_multiplier: float,
        min_dist_pct: float,
        max_dist_pct: float,
    ):
        """
        Find the statistically dominant OI wall strike for one side of the chain.

        A wall must:
        - Have OI > mean + std_multiplier × std (statistical outlier)
        - Be between min_dist_pct and max_dist_pct from spot
        - Call walls: strike must be ABOVE spot
        - Put walls:  strike must be BELOW spot

        Returns (strike: float, oi: int) or (None, 0) if no qualifying wall found.
        """
        oi_values = [d.get(oi_key, 0) for d in per_strike_data.values() if d.get(oi_key, 0) > 0]
        if len(oi_values) < 5:
            return None, 0

        mean_oi = np.mean(oi_values)
        std_oi  = np.std(oi_values)
        threshold = mean_oi + std_multiplier * std_oi

        is_call = oi_key in ("call_oi", "prev_call_oi")
        best_strike = None
        best_oi     = 0

        for strike_str, data in per_strike_data.items():
            strike  = float(strike_str)
            oi      = data.get(oi_key, 0)
            dist    = abs(strike - spot) / spot * 100

            if dist < min_dist_pct or dist > max_dist_pct:
                continue
            if is_call and strike < spot:
                continue
            if not is_call and strike > spot:
                continue
            if oi > threshold and oi > best_oi:
                best_strike = strike
                best_oi     = oi

        return best_strike, best_oi

    # ──────────────────────────────────────────────────────────────────────────
    # 1. OI-Based Support & Resistance
    # ──────────────────────────────────────────────────────────────────────────
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_oi_support_resistance(self, stock: Stock):
        """
        Identify key support and resistance levels from OI data.
        STRINGENT: Only signals when price BREACHES a dominant OI level.
        
        - Highest Put OI strike = Support (put writers defend this level)
        - Highest Call OI strike = Resistance (call writers defend this level)
        
        Signal conditions (BREACH ONLY):
        - Price below max-put-OI strike → Support breached (BEARISH)
        - Price above max-call-OI strike → Resistance breached (BULLISH)
        - S/R strike must have OI >= SR_MIN_OI_DOMINANCE * average OI (no weak levels)
        """
        try:
            logger.debug(f"[OI_SR] {stock.stock_symbol} — start")

            per_strike_data, meta = self._get_oi_chain_data(stock)
            if per_strike_data is None:
                logger.debug(f"[OI_SR] {stock.stock_symbol} — no OI chain data, skip")
                return False

            current_ltp = meta["current_ltp"]
            if not current_ltp:
                logger.debug(f"[OI_SR] {stock.stock_symbol} — current_ltp missing, skip")
                return False

            logger.debug(
                f"[OI_SR] {stock.stock_symbol} | "
                f"SOURCE current_ltp={current_ltp} atm_strike={meta.get('atm_strike')} "
                f"pcr={meta.get('pcr')} expiry={meta.get('expiry')} "
                f"num_strikes={len(per_strike_data)}"
            )

            # Collect OI data and find max strikes
            max_call_oi = 0
            max_call_oi_strike = None
            max_put_oi = 0
            max_put_oi_strike = None
            call_oi_list = []
            put_oi_list = []
            all_call_ois = []
            all_put_ois = []
            
            for strike_str, data in per_strike_data.items():
                strike = float(strike_str)
                call_oi = data.get("call_oi", 0)
                put_oi = data.get("put_oi", 0)
                
                if call_oi > 0:
                    call_oi_list.append((strike, call_oi))
                    all_call_ois.append(call_oi)
                if put_oi > 0:
                    put_oi_list.append((strike, put_oi))
                    all_put_ois.append(put_oi)
                
                if call_oi > max_call_oi:
                    max_call_oi = call_oi
                    max_call_oi_strike = strike
                if put_oi > max_put_oi:
                    max_put_oi = put_oi
                    max_put_oi_strike = strike
            
            if max_call_oi_strike is None or max_put_oi_strike is None:
                logger.debug(f"[OI_SR] {stock.stock_symbol} — no call or put OI found, skip")
                return False

            logger.debug(
                f"[OI_SR] {stock.stock_symbol} | "
                f"max_call_strike={max_call_oi_strike:.0f} call_oi={max_call_oi:,.0f} | "
                f"max_put_strike={max_put_oi_strike:.0f} put_oi={max_put_oi:,.0f} | "
                f"avg_call_oi={np.mean(all_call_ois) if all_call_ois else 0:,.0f} "
                f"avg_put_oi={np.mean(all_put_ois) if all_put_ois else 0:,.0f}"
            )

            # ── Guard: If support and resistance land on the same strike, use next-best ──
            if max_call_oi_strike == max_put_oi_strike:
                call_oi_list_sorted = sorted(call_oi_list, key=lambda x: x[1], reverse=True)
                put_oi_list_sorted = sorted(put_oi_list, key=lambda x: x[1], reverse=True)

                # Demote the side with lower OI to its second-best strike
                if max_call_oi >= max_put_oi:
                    # Call OI is stronger at this strike — find next-best put strike
                    for strike, oi in put_oi_list_sorted:
                        if strike != max_call_oi_strike:
                            max_put_oi_strike = strike
                            max_put_oi = oi
                            break
                    else:
                        return False  # No alternative put strike
                else:
                    # Put OI is stronger at this strike — find next-best call strike
                    for strike, oi in call_oi_list_sorted:
                        if strike != max_put_oi_strike:
                            max_call_oi_strike = strike
                            max_call_oi = oi
                            break
                    else:
                        return False  # No alternative call strike

                logger.debug(f"OI S/R same-strike resolved for {stock.stock_symbol}: "
                           f"S={max_put_oi_strike:.0f} R={max_call_oi_strike:.0f}")

            # ── Gate: S/R strikes must be dominant (OI >= dominance * avg) ──
            avg_call_oi = np.mean(all_call_ois) if all_call_ois else 0
            avg_put_oi = np.mean(all_put_ois) if all_put_ois else 0
            
            call_is_dominant = max_call_oi >= OIChainAnalyser.SR_MIN_OI_DOMINANCE * avg_call_oi
            put_is_dominant = max_put_oi >= OIChainAnalyser.SR_MIN_OI_DOMINANCE * avg_put_oi
            
            logger.debug(
                f"[OI_SR] {stock.stock_symbol} | "
                f"CONDITION dominance: "
                f"call_oi={max_call_oi:,.0f} vs avg={avg_call_oi:,.0f} "
                f"(need {OIChainAnalyser.SR_MIN_OI_DOMINANCE}x={avg_call_oi * OIChainAnalyser.SR_MIN_OI_DOMINANCE:,.0f}) → call_dominant={call_is_dominant} | "
                f"put_oi={max_put_oi:,.0f} vs avg={avg_put_oi:,.0f} "
                f"(need {OIChainAnalyser.SR_MIN_OI_DOMINANCE}x={avg_put_oi * OIChainAnalyser.SR_MIN_OI_DOMINANCE:,.0f}) → put_dominant={put_is_dominant}"
            )

            if not call_is_dominant and not put_is_dominant:
                logger.debug(f"[OI_SR] {stock.stock_symbol} — no dominant OI levels, skip")
                return False
            
            # Top 3 for context
            call_oi_list.sort(key=lambda x: x[1], reverse=True)
            put_oi_list.sort(key=lambda x: x[1], reverse=True)
            top_resistances = call_oi_list[:3]
            top_supports = put_oi_list[:3]
            
            resistance_distance_pct = ((max_call_oi_strike - current_ltp) / current_ltp) * 100
            support_distance_pct = ((current_ltp - max_put_oi_strike) / current_ltp) * 100

            logger.debug(
                f"[OI_SR] {stock.stock_symbol} | "
                f"CONDITION breach: support_distance={support_distance_pct:.2f}% "
                f"(< 0 = breached) resistance_distance={resistance_distance_pct:.2f}% "
                f"(< 0 = breached) | put_dominant={put_is_dominant} call_dominant={call_is_dominant}"
            )
            
            OISupportResistance = namedtuple("OISupportResistance", [
                "resistance_strike", "resistance_oi", "support_strike", "support_oi",
                "current_price", "resistance_distance_pct", "support_distance_pct",
                "top_resistances", "top_supports", "oi_range",
                "signal", "expiry"
            ])
            
            lo = min(max_put_oi_strike, max_call_oi_strike)
            hi = max(max_put_oi_strike, max_call_oi_strike)
            oi_range = f"{lo:.0f} - {hi:.0f}"
            signal_generated = False
            
            # ── BREACH ONLY: Price BELOW dominant support → BEARISH ──
            if put_is_dominant and support_distance_pct < 0:
                signal = (f"Price BELOW dominant OI support {max_put_oi_strike:.0f} "
                         f"(OI: {max_put_oi:,.0f}, {abs(support_distance_pct):.1f}% below) - SUPPORT BREACHED")
                analysis = OISupportResistance(
                    resistance_strike=max_call_oi_strike, resistance_oi=max_call_oi,
                    support_strike=max_put_oi_strike, support_oi=max_put_oi,
                    current_price=current_ltp,
                    resistance_distance_pct=resistance_distance_pct,
                    support_distance_pct=support_distance_pct,
                    top_resistances=top_resistances, top_supports=top_supports,
                    oi_range=oi_range, signal=signal, expiry=meta.get("expiry")
                )
                stock.set_analysis("BEARISH", "OI_SUPPORT_RESISTANCE", analysis)
                logger.info(
                    f"[OI_SR] {stock.stock_symbol} — SIGNAL BEARISH (SUPPORT BREACHED) | "
                    f"support={max_put_oi_strike:.0f} oi={max_put_oi:,.0f} "
                    f"price={current_ltp} ({abs(support_distance_pct):.1f}% below)"
                )
                signal_generated = True

            # ── BREACH ONLY: Price ABOVE dominant resistance → BULLISH ──
            elif call_is_dominant and resistance_distance_pct < 0:
                signal = (f"Price ABOVE dominant OI resistance {max_call_oi_strike:.0f} "
                         f"(OI: {max_call_oi:,.0f}, {abs(resistance_distance_pct):.1f}% above) - RESISTANCE BREACHED")
                analysis = OISupportResistance(
                    resistance_strike=max_call_oi_strike, resistance_oi=max_call_oi,
                    support_strike=max_put_oi_strike, support_oi=max_put_oi,
                    current_price=current_ltp,
                    resistance_distance_pct=resistance_distance_pct,
                    support_distance_pct=support_distance_pct,
                    top_resistances=top_resistances, top_supports=top_supports,
                    oi_range=oi_range, signal=signal, expiry=meta.get("expiry")
                )
                stock.set_analysis("BULLISH", "OI_SUPPORT_RESISTANCE", analysis)
                logger.info(
                    f"[OI_SR] {stock.stock_symbol} — SIGNAL BULLISH (RESISTANCE BREACHED) | "
                    f"resistance={max_call_oi_strike:.0f} oi={max_call_oi:,.0f} "
                    f"price={current_ltp} ({abs(resistance_distance_pct):.1f}% above)"
                )
                signal_generated = True

            # No breach → store NEUTRAL info (excluded from scoring via constants)
            if not signal_generated:
                signal = f"OI Range: {oi_range} | Support: {max_put_oi_strike:.0f} | Resistance: {max_call_oi_strike:.0f}"
                analysis = OISupportResistance(
                    resistance_strike=max_call_oi_strike, resistance_oi=max_call_oi,
                    support_strike=max_put_oi_strike, support_oi=max_put_oi,
                    current_price=current_ltp,
                    resistance_distance_pct=resistance_distance_pct,
                    support_distance_pct=support_distance_pct,
                    top_resistances=top_resistances, top_supports=top_supports,
                    oi_range=oi_range, signal=signal, expiry=meta.get("expiry")
                )
                stock.set_analysis("NEUTRAL", "OI_SUPPORT_RESISTANCE", analysis)
                logger.debug(
                    f"[OI_SR] {stock.stock_symbol} — no breach | "
                    f"support={max_put_oi_strike:.0f} resistance={max_call_oi_strike:.0f} "
                    f"range={oi_range} price={current_ltp}"
                )
            
            return signal_generated
            
        except Exception as e:
            logger.error(f"[OI_SR] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # 2. OI Buildup Detection
    # ──────────────────────────────────────────────────────────────────────────
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_oi_buildup(self, stock: Stock):
        """
        Detect significant OI buildup (fresh writing) or unwinding at key strikes.
        STRINGENT: Requires extreme imbalance + meaningful total OI change.
        
        Gates:
        1. Total OI change must be >= OI_BUILDUP_MIN_TOTAL_CHANGE_PCT of total OI
        2. Minimum OI_BUILDUP_MIN_STRIKES strikes with >= OI_BUILDUP_MIN_CHANGE_PCT change
        3. Call/Put ratio must exceed HEAVY_RATIO (3x) or DOMINANT_RATIO (5x)
        """
        try:
            logger.debug(f"[OI_BUILDUP] {stock.stock_symbol} — start")

            per_strike_data, meta = self._get_oi_chain_data(stock)
            if per_strike_data is None:
                logger.debug(f"[OI_BUILDUP] {stock.stock_symbol} — no OI chain data, skip")
                return False

            current_ltp = meta["current_ltp"]
            if not current_ltp:
                logger.debug(f"[OI_BUILDUP] {stock.stock_symbol} — current_ltp missing, skip")
                return False

            total_call_oi_change = meta.get("total_call_oi_change", 0)
            total_put_oi_change = meta.get("total_put_oi_change", 0)
            total_call_oi = meta.get("total_call_oi", 0)
            total_put_oi = meta.get("total_put_oi", 0)
            total_oi = total_call_oi + total_put_oi

            logger.debug(
                f"[OI_BUILDUP] {stock.stock_symbol} | "
                f"SOURCE total_call_oi={total_call_oi:,.0f} total_put_oi={total_put_oi:,.0f} "
                f"total_call_oi_change={total_call_oi_change:+,.0f} "
                f"total_put_oi_change={total_put_oi_change:+,.0f} "
                f"current_ltp={current_ltp} expiry={meta.get('expiry')}"
            )

            # ── Gate 1: Total OI change must be meaningful relative to total OI ──
            if total_oi == 0:
                logger.debug(f"[OI_BUILDUP] {stock.stock_symbol} — total_oi=0, skip")
                return False
            total_abs_change = abs(total_call_oi_change) + abs(total_put_oi_change)
            total_change_pct = (total_abs_change / total_oi) * 100

            logger.debug(
                f"[OI_BUILDUP] {stock.stock_symbol} | "
                f"CONDITION Gate1: total_change_pct={total_change_pct:.1f}% "
                f">= threshold={OIChainAnalyser.OI_BUILDUP_MIN_TOTAL_CHANGE_PCT}% → "
                f"{'PASS' if total_change_pct >= OIChainAnalyser.OI_BUILDUP_MIN_TOTAL_CHANGE_PCT else 'FAIL'}"
            )

            if total_change_pct < OIChainAnalyser.OI_BUILDUP_MIN_TOTAL_CHANGE_PCT:
                logger.debug(f"[OI_BUILDUP] {stock.stock_symbol} — Gate1 FAIL, skip")
                return False
            
            # Analyze per-strike OI changes
            significant_call_buildup = []
            significant_put_buildup = []
            
            for strike_str, data in per_strike_data.items():
                strike = float(strike_str)
                call_oi = data.get("call_oi", 0)
                put_oi = data.get("put_oi", 0)
                prev_call_oi = data.get("prev_call_oi", 0)
                prev_put_oi = data.get("prev_put_oi", 0)
                
                call_change = call_oi - prev_call_oi
                put_change = put_oi - prev_put_oi
                
                call_change_pct = (call_change / prev_call_oi * 100) if prev_call_oi > 0 else (100.0 if call_change > 0 else 0.0)
                put_change_pct = (put_change / prev_put_oi * 100) if prev_put_oi > 0 else (100.0 if put_change > 0 else 0.0)
                
                # Only consider strikes within 8% of price
                strike_distance_pct = abs(strike - current_ltp) / current_ltp * 100
                if strike_distance_pct > 8:
                    continue
                
                if call_change > 0 and call_change_pct >= OIChainAnalyser.OI_BUILDUP_MIN_CHANGE_PCT:
                    significant_call_buildup.append((strike, call_change, call_change_pct, call_oi))
                
                if put_change > 0 and put_change_pct >= OIChainAnalyser.OI_BUILDUP_MIN_CHANGE_PCT:
                    significant_put_buildup.append((strike, put_change, put_change_pct, put_oi))
            
            significant_call_buildup.sort(key=lambda x: abs(x[1]), reverse=True)
            significant_put_buildup.sort(key=lambda x: abs(x[1]), reverse=True)

            logger.debug(
                f"[OI_BUILDUP] {stock.stock_symbol} | "
                f"per-strike scan (within 8% of {current_ltp}): "
                f"significant_call_strikes={len(significant_call_buildup)} "
                f"significant_put_strikes={len(significant_put_buildup)} "
                f"(need >={OIChainAnalyser.OI_BUILDUP_MIN_STRIKES} with "
                f">={OIChainAnalyser.OI_BUILDUP_MIN_CHANGE_PCT}% change)"
            )
            _cp_ratio = total_call_oi_change / total_put_oi_change if total_put_oi_change > 0 else float('inf')
            _pc_ratio = total_put_oi_change / total_call_oi_change if total_call_oi_change > 0 else float('inf')
            logger.debug(
                f"[OI_BUILDUP] {stock.stock_symbol} | "
                f"CONDITION ratio: call_put_ratio={_cp_ratio:.2f} "
                f"put_call_ratio={_pc_ratio:.2f} | "
                f"thresholds: heavy={OIChainAnalyser.OI_BUILDUP_HEAVY_RATIO}x "
                f"dominant={OIChainAnalyser.OI_BUILDUP_DOMINANT_RATIO}x "
                f"min_strikes={OIChainAnalyser.OI_BUILDUP_MIN_STRIKES}"
            )

            OIBuildup = namedtuple("OIBuildup", [
                "buildup_type", "key_strikes", "total_call_oi_change",
                "total_put_oi_change", "call_put_oi_change_ratio",
                "signal", "expiry"
            ])
            
            # Ratio logic:
            # - Both positive: normal ratio comparison
            # - Call positive + Put negative/zero: extreme bearish (writing + unwinding) → inf
            # - Put positive + Call negative/zero: extreme bullish (writing + unwinding) → inf
            call_put_ratio = (total_call_oi_change / total_put_oi_change) if total_put_oi_change > 0 else float('inf')
            put_call_ratio = (total_put_oi_change / total_call_oi_change) if total_call_oi_change > 0 else float('inf')
            
            signal_generated = False
            min_strikes = OIChainAnalyser.OI_BUILDUP_MIN_STRIKES
            
            # ── Heavy call writing — BEARISH (ratio >= HEAVY_RATIO, min strikes) ──
            if (len(significant_call_buildup) >= min_strikes and 
                total_call_oi_change > 0 and 
                call_put_ratio >= OIChainAnalyser.OI_BUILDUP_HEAVY_RATIO):
                
                top_strikes = significant_call_buildup[:5]
                key_strikes_str = ", ".join([f"{s[0]:.0f}(+{s[1]:,.0f})" for s in top_strikes])
                if call_put_ratio == float('inf'):
                    if total_put_oi_change < 0:
                        ratio_str = f"call writing + put unwinding"
                    else:
                        ratio_str = f"pure call writing (no put activity)"
                else:
                    ratio_str = f"{call_put_ratio:.1f}x call writing"
                signal = (f"ABNORMAL {ratio_str}. "
                         f"Call OI {total_call_oi_change:+,.0f} vs Put OI {total_put_oi_change:+,.0f}. "
                         f"Key strikes: {key_strikes_str}")

                stock.set_analysis("BEARISH", "OI_BUILDUP", OIBuildup(
                    buildup_type="HEAVY_CALL_WRITING",
                    key_strikes=top_strikes,
                    total_call_oi_change=total_call_oi_change,
                    total_put_oi_change=total_put_oi_change,
                    call_put_oi_change_ratio=call_put_ratio,
                    signal=signal, expiry=meta.get("expiry")
                ))
                logger.info(
                    f"[OI_BUILDUP] {stock.stock_symbol} — SIGNAL BEARISH HEAVY_CALL_WRITING | "
                    f"ratio={call_put_ratio:.1f}x call_chg={total_call_oi_change:+,.0f} "
                    f"put_chg={total_put_oi_change:+,.0f} strikes={len(significant_call_buildup)}"
                )
                signal_generated = True

            # ── Heavy put writing — BULLISH (ratio >= HEAVY_RATIO, min strikes) ──
            elif (len(significant_put_buildup) >= min_strikes and
                  total_put_oi_change > 0 and
                  put_call_ratio >= OIChainAnalyser.OI_BUILDUP_HEAVY_RATIO):

                top_strikes = significant_put_buildup[:5]
                key_strikes_str = ", ".join([f"{s[0]:.0f}(+{s[1]:,.0f})" for s in top_strikes])
                if put_call_ratio == float('inf'):
                    if total_call_oi_change < 0:
                        ratio_str = f"put writing + call unwinding"
                    else:
                        ratio_str = f"pure put writing (no call activity)"
                else:
                    ratio_str = f"{put_call_ratio:.1f}x put writing"
                signal = (f"ABNORMAL {ratio_str}. "
                         f"Put OI {total_put_oi_change:+,.0f} vs Call OI {total_call_oi_change:+,.0f}. "
                         f"Key strikes: {key_strikes_str}")

                stock.set_analysis("BULLISH", "OI_BUILDUP", OIBuildup(
                    buildup_type="HEAVY_PUT_WRITING",
                    key_strikes=top_strikes,
                    total_call_oi_change=total_call_oi_change,
                    total_put_oi_change=total_put_oi_change,
                    call_put_oi_change_ratio=call_put_ratio,
                    signal=signal, expiry=meta.get("expiry")
                ))
                logger.info(
                    f"[OI_BUILDUP] {stock.stock_symbol} — SIGNAL BULLISH HEAVY_PUT_WRITING | "
                    f"ratio={put_call_ratio:.1f}x put_chg={total_put_oi_change:+,.0f} "
                    f"call_chg={total_call_oi_change:+,.0f} strikes={len(significant_put_buildup)}"
                )
                signal_generated = True

            # ── Extreme dominant call writing — BEARISH (ratio >= DOMINANT_RATIO) ──
            elif (total_call_oi_change > 0 and total_put_oi_change > 0 and
                  call_put_ratio >= OIChainAnalyser.OI_BUILDUP_DOMINANT_RATIO):

                top_call_strikes = significant_call_buildup[:3]
                key_strikes_str = ", ".join([f"{s[0]:.0f}(+{s[1]:,.0f})" for s in top_call_strikes])
                signal = (f"Extreme call dominance ({call_put_ratio:.1f}x). "
                         f"Call OI {total_call_oi_change:+,.0f} vs Put OI {total_put_oi_change:+,.0f}. "
                         f"Top strikes: {key_strikes_str}")

                stock.set_analysis("BEARISH", "OI_BUILDUP", OIBuildup(
                    buildup_type="CALL_DOMINANT_WRITING",
                    key_strikes=top_call_strikes,
                    total_call_oi_change=total_call_oi_change,
                    total_put_oi_change=total_put_oi_change,
                    call_put_oi_change_ratio=call_put_ratio,
                    signal=signal, expiry=meta.get("expiry")
                ))
                logger.info(
                    f"[OI_BUILDUP] {stock.stock_symbol} — SIGNAL BEARISH CALL_DOMINANT_WRITING | "
                    f"ratio={call_put_ratio:.1f}x call_chg={total_call_oi_change:+,.0f} "
                    f"put_chg={total_put_oi_change:+,.0f}"
                )
                signal_generated = True

            # ── Extreme dominant put writing — BULLISH (ratio >= DOMINANT_RATIO) ──
            elif (total_put_oi_change > 0 and total_call_oi_change > 0 and
                  put_call_ratio >= OIChainAnalyser.OI_BUILDUP_DOMINANT_RATIO):

                top_put_strikes = significant_put_buildup[:3]
                key_strikes_str = ", ".join([f"{s[0]:.0f}(+{s[1]:,.0f})" for s in top_put_strikes])
                signal = (f"Extreme put dominance ({put_call_ratio:.1f}x). "
                         f"Put OI {total_put_oi_change:+,.0f} vs Call OI {total_call_oi_change:+,.0f}. "
                         f"Top strikes: {key_strikes_str}")

                stock.set_analysis("BULLISH", "OI_BUILDUP", OIBuildup(
                    buildup_type="PUT_DOMINANT_WRITING",
                    key_strikes=top_put_strikes,
                    total_call_oi_change=total_call_oi_change,
                    total_put_oi_change=total_put_oi_change,
                    call_put_oi_change_ratio=call_put_ratio,
                    signal=signal, expiry=meta.get("expiry")
                ))
                logger.info(
                    f"[OI_BUILDUP] {stock.stock_symbol} — SIGNAL BULLISH PUT_DOMINANT_WRITING | "
                    f"ratio={put_call_ratio:.1f}x put_chg={total_put_oi_change:+,.0f} "
                    f"call_chg={total_call_oi_change:+,.0f}"
                )
                signal_generated = True
            
            if not signal_generated:
                logger.debug(
                    f"[OI_BUILDUP] {stock.stock_symbol} — no abnormal signal | "
                    f"call_chg={total_call_oi_change:+,.0f} put_chg={total_put_oi_change:+,.0f} "
                    f"total_change_pct={total_change_pct:.1f}%"
                )
            return signal_generated

        except Exception as e:
            logger.error(f"[OI_BUILDUP] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # 3. OI Wall Detection
    # ──────────────────────────────────────────────────────────────────────────
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_oi_wall(self, stock: Stock):
        """
        Detect OI walls — statistical outliers in OI concentration.
        STRINGENT: Uses mean + N*std (not simple multiplier). Requires:
        1. Wall OI must be a statistical outlier (> mean + OI_WALL_STD_MULTIPLIER * std)
        2. Wall must be within OI_WALL_MAX_DISTANCE_PCT of price
        3. When both walls exist, distance asymmetry must exceed OI_WALL_MIN_ASYMMETRY_RATIO
           (one wall must be clearly closer than the other to generate directional signal)
        """
        try:
            logger.debug(f"[OI_WALL] {stock.stock_symbol} — start")

            per_strike_data, meta = self._get_oi_chain_data(stock)
            if per_strike_data is None:
                logger.debug(f"[OI_WALL] {stock.stock_symbol} — no OI chain data, skip")
                return False

            current_ltp = meta["current_ltp"]
            if not current_ltp:
                logger.debug(f"[OI_WALL] {stock.stock_symbol} — current_ltp missing, skip")
                return False

            logger.debug(
                f"[OI_WALL] {stock.stock_symbol} | "
                f"SOURCE current_ltp={current_ltp} num_strikes={len(per_strike_data)} "
                f"expiry={meta.get('expiry')}"
            )

            call_ois = []
            put_ois = []
            strike_data_list = []
            
            for strike_str, data in per_strike_data.items():
                strike = float(strike_str)
                call_oi = data.get("call_oi", 0)
                put_oi = data.get("put_oi", 0)
                
                if call_oi > 0:
                    call_ois.append(call_oi)
                if put_oi > 0:
                    put_ois.append(put_oi)
                
                strike_data_list.append({
                    "strike": strike, "call_oi": call_oi, "put_oi": put_oi,
                    "distance_pct": ((strike - current_ltp) / current_ltp) * 100
                })
            
            if len(call_ois) < 5 or len(put_ois) < 5:
                logger.debug(
                    f"[OI_WALL] {stock.stock_symbol} — insufficient OI data "
                    f"(call_strikes={len(call_ois)} put_strikes={len(put_ois)}, need 5 each), skip"
                )
                return False

            avg_call_oi = np.mean(call_ois)
            avg_put_oi = np.mean(put_ois)
            std_call_oi = np.std(call_ois)
            std_put_oi = np.std(put_ois)

            # ── Statistical outlier threshold: mean + N * std ──
            call_wall_threshold = avg_call_oi + OIChainAnalyser.OI_WALL_STD_MULTIPLIER * std_call_oi
            put_wall_threshold = avg_put_oi + OIChainAnalyser.OI_WALL_STD_MULTIPLIER * std_put_oi

            logger.debug(
                f"[OI_WALL] {stock.stock_symbol} | "
                f"call: mean={avg_call_oi:,.0f} std={std_call_oi:,.0f} "
                f"wall_threshold={call_wall_threshold:,.0f} "
                f"(mean + {OIChainAnalyser.OI_WALL_STD_MULTIPLIER}σ) | "
                f"put: mean={avg_put_oi:,.0f} std={std_put_oi:,.0f} "
                f"wall_threshold={put_wall_threshold:,.0f}"
            )

            max_dist = OIChainAnalyser.OI_WALL_MAX_DISTANCE_PCT
            
            call_walls = []
            put_walls = []
            
            min_dist = OIChainAnalyser.OI_WALL_MIN_DISTANCE_PCT

            for sd in strike_data_list:
                dist_abs = abs(sd["distance_pct"])
                # ── Gate: Only within max distance and beyond min distance (exclude ATM) ──
                if dist_abs > max_dist or dist_abs < min_dist:
                    continue

                if sd["call_oi"] > call_wall_threshold:
                    call_walls.append((sd["strike"], sd["call_oi"], sd["distance_pct"]))

                if sd["put_oi"] > put_wall_threshold:
                    put_walls.append((sd["strike"], sd["put_oi"], sd["distance_pct"]))
            
            logger.debug(
                f"[OI_WALL] {stock.stock_symbol} | "
                f"walls {min_dist}%–{max_dist}% from price (ATM excluded): "
                f"call_walls={len(call_walls)} put_walls={len(put_walls)}"
            )

            if not call_walls and not put_walls:
                logger.debug(f"[OI_WALL] {stock.stock_symbol} — no walls within distance, skip")
                return False

            call_walls.sort(key=lambda x: x[1], reverse=True)
            put_walls.sort(key=lambda x: x[1], reverse=True)
            
            OIWall = namedtuple("OIWall", [
                "call_walls", "put_walls", "nearest_call_wall", "nearest_put_wall",
                "current_price", "avg_call_oi", "avg_put_oi",
                "wall_type", "signal", "expiry"
            ])
            
            # Nearest walls above (call) and below (put) price
            nearest_call_wall = None
            for cw in call_walls:
                if cw[0] > current_ltp:
                    nearest_call_wall = cw
                    break
            
            nearest_put_wall = None
            for pw in put_walls:
                if pw[0] < current_ltp:
                    if nearest_put_wall is None or pw[0] > nearest_put_wall[0]:
                        nearest_put_wall = pw
            
            # ── Determine signal with asymmetry requirement ──
            if nearest_call_wall and nearest_put_wall:
                call_dist = ((nearest_call_wall[0] - current_ltp) / current_ltp) * 100
                put_dist = ((current_ltp - nearest_put_wall[0]) / current_ltp) * 100
                
                # Check asymmetry: one wall must be significantly closer
                if call_dist > 0 and put_dist > 0:
                    asymmetry_ratio = max(call_dist, put_dist) / min(call_dist, put_dist)
                else:
                    asymmetry_ratio = float('inf')
                
                logger.debug(
                    f"[OI_WALL] {stock.stock_symbol} | "
                    f"CONDITION asymmetry: call_dist={call_dist:.1f}% put_dist={put_dist:.1f}% "
                    f"ratio={asymmetry_ratio:.1f}x >= min={OIChainAnalyser.OI_WALL_MIN_ASYMMETRY_RATIO}x → "
                    f"{'PASS' if asymmetry_ratio >= OIChainAnalyser.OI_WALL_MIN_ASYMMETRY_RATIO else 'FAIL'}"
                )

                if asymmetry_ratio < OIChainAnalyser.OI_WALL_MIN_ASYMMETRY_RATIO:
                    logger.debug(f"[OI_WALL] {stock.stock_symbol} — walls equidistant, skip")
                    return False
                
                wall_type = "BOTH_WALLS"
                signal = (f"Call wall at {nearest_call_wall[0]:.0f} (OI: {nearest_call_wall[1]:,.0f}, {call_dist:.1f}% above) | "
                         f"Put wall at {nearest_put_wall[0]:.0f} (OI: {nearest_put_wall[1]:,.0f}, {put_dist:.1f}% below) "
                         f"[asymmetry: {asymmetry_ratio:.1f}x]")
                
                sentiment = "BEARISH" if call_dist < put_dist else "BULLISH"
                
            elif nearest_call_wall:
                wall_type = "CALL_WALL_ONLY"
                call_dist = ((nearest_call_wall[0] - current_ltp) / current_ltp) * 100
                signal = (f"Strong call wall at {nearest_call_wall[0]:.0f} "
                         f"(OI: {nearest_call_wall[1]:,.0f}, {call_dist:.1f}% above, "
                         f">{OIChainAnalyser.OI_WALL_STD_MULTIPLIER:.0f}σ outlier) - Resistance")
                sentiment = "BEARISH"
            elif nearest_put_wall:
                wall_type = "PUT_WALL_ONLY"
                put_dist = ((current_ltp - nearest_put_wall[0]) / current_ltp) * 100
                signal = (f"Strong put wall at {nearest_put_wall[0]:.0f} "
                         f"(OI: {nearest_put_wall[1]:,.0f}, {put_dist:.1f}% below, "
                         f">{OIChainAnalyser.OI_WALL_STD_MULTIPLIER:.0f}σ outlier) - Support")
                sentiment = "BULLISH"
            else:
                return False
            
            analysis = OIWall(
                call_walls=call_walls[:5], put_walls=put_walls[:5],
                nearest_call_wall=nearest_call_wall, nearest_put_wall=nearest_put_wall,
                current_price=current_ltp, avg_call_oi=avg_call_oi, avg_put_oi=avg_put_oi,
                wall_type=wall_type, signal=signal, expiry=meta.get("expiry")
            )
            
            stock.set_analysis(sentiment, "OI_WALL", analysis)
            logger.info(
                f"[OI_WALL] {stock.stock_symbol} — SIGNAL {sentiment} {wall_type} | {signal}"
            )
            return True

        except Exception as e:
            logger.error(f"[OI_WALL] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # 4. OI Shift / Position Migration Analysis
    # ──────────────────────────────────────────────────────────────────────────
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_oi_shift(self, stock: Stock):
        """
        Analyze how OI positions are shifting compared to previous day.
        
        - Call OI shifting to lower strikes → Writers adjusting down → Bearish
        - Call OI shifting to higher strikes → Writers adjusting up → Bullish
        - Put OI shifting to higher strikes → Writers adjusting up → Bullish
        - Put OI shifting to lower strikes → Writers adjusting down → Bearish
        
        Uses weighted average strike of OI change to determine shift direction.
        """
        try:
            logger.debug(f"[OI_SHIFT] {stock.stock_symbol} — start")

            per_strike_data, meta = self._get_oi_chain_data(stock)
            if per_strike_data is None:
                logger.debug(f"[OI_SHIFT] {stock.stock_symbol} — no OI chain data, skip")
                return False

            current_ltp = meta["current_ltp"]
            prev_ltp = meta.get("prev_ltp")
            if not current_ltp:
                logger.debug(f"[OI_SHIFT] {stock.stock_symbol} — current_ltp missing, skip")
                return False

            logger.debug(
                f"[OI_SHIFT] {stock.stock_symbol} | "
                f"SOURCE current_ltp={current_ltp} prev_ltp={prev_ltp} "
                f"expiry={meta.get('expiry')} num_strikes={len(per_strike_data)}"
            )

            # Calculate weighted average strike of OI changes
            call_oi_additions = []  # (strike, absolute_change) for additions only
            put_oi_additions = []
            call_oi_removals = []
            put_oi_removals = []
            
            total_new_call_oi = 0
            total_new_put_oi = 0
            
            for strike_str, data in per_strike_data.items():
                strike = float(strike_str)
                call_oi = data.get("call_oi", 0)
                put_oi = data.get("put_oi", 0)
                prev_call_oi = data.get("prev_call_oi", 0)
                prev_put_oi = data.get("prev_put_oi", 0)
                
                call_change = call_oi - prev_call_oi
                put_change = put_oi - prev_put_oi
                
                if call_change > 0:
                    call_oi_additions.append((strike, call_change))
                    total_new_call_oi += call_change
                elif call_change < 0:
                    call_oi_removals.append((strike, abs(call_change)))
                
                if put_change > 0:
                    put_oi_additions.append((strike, put_change))
                    total_new_put_oi += put_change
                elif put_change < 0:
                    put_oi_removals.append((strike, abs(put_change)))
            
            if total_new_call_oi == 0 and total_new_put_oi == 0:
                logger.debug(f"[OI_SHIFT] {stock.stock_symbol} — no new OI additions, skip")
                return False

            logger.debug(
                f"[OI_SHIFT] {stock.stock_symbol} | "
                f"total_new_call_oi={total_new_call_oi:,.0f} total_new_put_oi={total_new_put_oi:,.0f}"
            )

            # Weighted average strike of new call OI
            call_weighted_avg = None
            if total_new_call_oi > 0:
                call_weighted_avg = sum(s * c for s, c in call_oi_additions) / total_new_call_oi
            
            # Weighted average strike of new put OI
            put_weighted_avg = None
            if total_new_put_oi > 0:
                put_weighted_avg = sum(s * c for s, c in put_oi_additions) / total_new_put_oi
            
            OIShift = namedtuple("OIShift", [
                "call_oi_center", "put_oi_center", "current_price", "prev_price",
                "call_shift_direction", "put_shift_direction",
                "total_new_call_oi", "total_new_put_oi",
                "signal", "expiry"
            ])
            
            # Determine call OI shift direction relative to current price
            # Use configurable threshold for "near price" detection
            center_thresh = OIChainAnalyser.OI_SHIFT_CENTER_THRESHOLD_PCT / 100.0
            
            call_shift = None
            if call_weighted_avg is not None:
                call_dist_pct = ((call_weighted_avg - current_ltp) / current_ltp) * 100
                if call_weighted_avg < current_ltp * (1 - center_thresh):
                    call_shift = "BELOW_PRICE"  # Calls written significantly below CMP → very bearish
                elif call_weighted_avg < current_ltp * (1 + center_thresh):
                    call_shift = "NEAR_PRICE"   # Calls near CMP → mildly bearish (ignored unless extreme)
                else:
                    call_shift = "ABOVE_PRICE"  # Calls above CMP → normal/neutral
            
            put_shift = None
            if put_weighted_avg is not None:
                put_dist_pct = ((put_weighted_avg - current_ltp) / current_ltp) * 100
                if put_weighted_avg > current_ltp * (1 + center_thresh):
                    put_shift = "ABOVE_PRICE"  # Puts written significantly above CMP → very bullish
                elif put_weighted_avg > current_ltp * (1 - center_thresh):
                    put_shift = "NEAR_PRICE"   # Puts near CMP → mildly bullish (ignored unless extreme)
                else:
                    put_shift = "BELOW_PRICE"  # Puts below CMP → normal/neutral
            
            call_center_str = f"{call_weighted_avg:.0f} ({call_shift})" if call_weighted_avg is not None else "N/A"
            put_center_str  = f"{put_weighted_avg:.0f} ({put_shift})"  if put_weighted_avg  is not None else "N/A"
            logger.debug(
                f"[OI_SHIFT] {stock.stock_symbol} | "
                f"call_center={call_center_str} "
                f"put_center={put_center_str} | "
                f"CONDITION center_thresh={OIChainAnalyser.OI_SHIFT_CENTER_THRESHOLD_PCT}% "
                f"writing_ratio_min={OIChainAnalyser.OI_SHIFT_MIN_WRITING_RATIO}x"
            )

            # ── Only signal for truly abnormal scenarios ──
            signal_parts = []
            sentiment = "NEUTRAL"
            
            if call_weighted_avg:
                signal_parts.append(f"New Call OI center: {call_weighted_avg:.0f} ({call_shift})")
            if put_weighted_avg:
                signal_parts.append(f"New Put OI center: {put_weighted_avg:.0f} ({put_shift})")
            
            # VERY bearish: Call writing below price (very unusual) AND no bullish put writing
            if call_shift == "BELOW_PRICE" and put_shift in ("BELOW_PRICE", None):
                sentiment = "BEARISH"
                signal_parts.append("→ ABNORMAL: Call writing BELOW CMP with no put support near price")
            
            # VERY bullish: Put writing above price (very unusual) AND no bearish call writing
            elif put_shift == "ABOVE_PRICE" and call_shift in ("ABOVE_PRICE", None):
                sentiment = "BULLISH"
                signal_parts.append("→ ABNORMAL: Put writing ABOVE CMP with no call resistance near price")
            
            # Extreme bearish imbalance: Writing ratio >= threshold
            elif (total_new_call_oi > 0 and total_new_put_oi > 0 and 
                  total_new_call_oi > total_new_put_oi * OIChainAnalyser.OI_SHIFT_MIN_WRITING_RATIO):
                sentiment = "BEARISH"
                ratio = total_new_call_oi / total_new_put_oi
                signal_parts.append(f"→ Extreme call writing imbalance: {ratio:.1f}x heavier than put writing")
            
            # Extreme bullish imbalance
            elif (total_new_put_oi > 0 and total_new_call_oi > 0 and 
                  total_new_put_oi > total_new_call_oi * OIChainAnalyser.OI_SHIFT_MIN_WRITING_RATIO):
                sentiment = "BULLISH"
                ratio = total_new_put_oi / total_new_call_oi
                signal_parts.append(f"→ Extreme put writing imbalance: {ratio:.1f}x heavier than call writing")
            else:
                ratio_actual = (total_new_call_oi / total_new_put_oi
                                if total_new_put_oi > 0 else float("inf"))
                logger.debug(
                    f"[OI_SHIFT] {stock.stock_symbol} — no abnormal signal | "
                    f"call_shift={call_shift} put_shift={put_shift} "
                    f"actual_ratio={ratio_actual:.2f}x (need {OIChainAnalyser.OI_SHIFT_MIN_WRITING_RATIO}x for imbalance) "
                    f"call_new={total_new_call_oi:,.0f} put_new={total_new_put_oi:,.0f}"
                )
                return False

            signal = " | ".join(signal_parts)
            
            stock.set_analysis(sentiment, "OI_SHIFT", OIShift(
                call_oi_center=call_weighted_avg,
                put_oi_center=put_weighted_avg,
                current_price=current_ltp,
                prev_price=prev_ltp,
                call_shift_direction=call_shift,
                put_shift_direction=put_shift,
                total_new_call_oi=total_new_call_oi,
                total_new_put_oi=total_new_put_oi,
                signal=signal,
                expiry=meta.get("expiry")
            ))
            logger.info(
                f"[OI_SHIFT] {stock.stock_symbol} — SIGNAL {sentiment} | "
                f"call_center={call_center_str} "
                f"put_center={put_center_str} | {signal}"
            )
            return True

        except Exception as e:
            logger.error(f"[OI_SHIFT] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ══════════════════════════════════════════════════════════════════════════
    # POSITIONAL-ONLY: EOD analyses using prev_call_oi / prev_put_oi per strike
    # ══════════════════════════════════════════════════════════════════════════

    # ──────────────────────────────────────────────────────────────────────────
    # 5. OI Capitulation Tracker
    # ──────────────────────────────────────────────────────────────────────────
    @BaseAnalyzer.positional
    @BaseAnalyzer.index_positional
    def analyse_oi_capitulation(self, stock: Stock):
        """
        Detect institutional capitulation: strikes where OI strictly decreased
        vs the previous day's close, indicating position unwinding near the money.

        Put unwinding near money → Put writers abandoning the floor → BEARISH
        Call unwinding near money → Call writers getting squeezed/covered → BULLISH

        Filters applied in order:
        1. Expiry guard: skip if total OI dropped >80% (natural expiry roll)
        2. Distance: only strikes within ±OI_CAPITULATION_DISTANCE_PCT of spot
        3. Significance per strike: reduction >= OI_CAPITULATION_MIN_CHANGE_PCT
           AND absolute reduction >= OI_CAPITULATION_MIN_ABS_REDUCTION
        4. Minimum qualifying strikes: >= OI_CAPITULATION_MIN_STRIKES
        5. Macro weight: unwound OI >= OI_CAPITULATION_MIN_TOTAL_PCT of that
           side's total OI (ensures signal is not from a single retail strike)

        SOURCE DATA (DEBUG):   per_strike prev/curr OI, total side OI
        ANALYSER INPUT (DEBUG): strikes passing distance + significance filters
        CONDITION (DEBUG):     unwound totals, pct of total OI, which side wins
        """
        OICapitulation = namedtuple("OICapitulation", [
            "side",                   # "CALL" or "PUT"
            "total_unwound",          # absolute OI contracts removed
            "unwound_pct",            # % of that side's total OI unwound
            "top_strikes",            # list of (strike, oi_removed, pct_removed) — top 5
            "num_significant_strikes",
            "signal",
            "expiry",
        ])

        try:
            logger.debug(f"[OI_CAPIT] {stock.stock_symbol} — start")

            per_strike_data, meta = self._get_oi_chain_data(stock)
            if per_strike_data is None:
                logger.debug(f"[OI_CAPIT] {stock.stock_symbol} — no OI chain data, skip")
                return False

            current_ltp = meta["current_ltp"]
            if not current_ltp:
                logger.debug(f"[OI_CAPIT] {stock.stock_symbol} — current_ltp missing, skip")
                return False

            # ── 1. Expiry guard ───────────────────────────────────────────────
            if self._expiry_guard(per_strike_data):
                logger.debug(f"[OI_CAPIT] {stock.stock_symbol} — expiry guard fired, skip")
                return False

            total_call_oi = meta.get("total_call_oi", 0)
            total_put_oi  = meta.get("total_put_oi",  0)
            dist_pct      = OIChainAnalyser.OI_CAPITULATION_DISTANCE_PCT

            logger.debug(
                f"[OI_CAPIT] {stock.stock_symbol} | "
                f"SOURCE current_ltp={current_ltp} expiry={meta.get('expiry')} "
                f"total_call_oi={total_call_oi:,.0f} total_put_oi={total_put_oi:,.0f} "
                f"num_strikes={len(per_strike_data)}"
            )

            # ── 2 & 3. Per-strike scan — distance + significance filter ───────
            sig_call_unwinds = []  # (strike, removed, pct_removed)
            sig_put_unwinds  = []

            for strike_str, data in per_strike_data.items():
                strike = float(strike_str)
                if abs(strike - current_ltp) / current_ltp * 100 > dist_pct:
                    continue

                prev_call = data.get("prev_call_oi", 0)
                prev_put  = data.get("prev_put_oi",  0)
                curr_call = data.get("call_oi", 0)
                curr_put  = data.get("put_oi",  0)

                call_removed = prev_call - curr_call
                put_removed  = prev_put  - curr_put

                if call_removed > 0 and prev_call > 0:
                    pct = call_removed / prev_call * 100
                    if (pct >= OIChainAnalyser.OI_CAPITULATION_MIN_CHANGE_PCT and
                            call_removed >= OIChainAnalyser.OI_CAPITULATION_MIN_ABS_REDUCTION):
                        sig_call_unwinds.append((strike, call_removed, pct))

                if put_removed > 0 and prev_put > 0:
                    pct = put_removed / prev_put * 100
                    if (pct >= OIChainAnalyser.OI_CAPITULATION_MIN_CHANGE_PCT and
                            put_removed >= OIChainAnalyser.OI_CAPITULATION_MIN_ABS_REDUCTION):
                        sig_put_unwinds.append((strike, put_removed, pct))

            sig_call_unwinds.sort(key=lambda x: x[1], reverse=True)
            sig_put_unwinds.sort(key=lambda x:  x[1], reverse=True)

            logger.debug(
                f"[OI_CAPIT] {stock.stock_symbol} | "
                f"ANALYSER INPUT within ±{dist_pct}% of {current_ltp}: "
                f"sig_call_strikes={len(sig_call_unwinds)} "
                f"sig_put_strikes={len(sig_put_unwinds)} "
                f"(need >={OIChainAnalyser.OI_CAPITULATION_MIN_STRIKES} with "
                f">={OIChainAnalyser.OI_CAPITULATION_MIN_CHANGE_PCT}% chg AND "
                f">={OIChainAnalyser.OI_CAPITULATION_MIN_ABS_REDUCTION:,} abs)"
            )

            # ── 4. Minimum strikes check ──────────────────────────────────────
            min_str = OIChainAnalyser.OI_CAPITULATION_MIN_STRIKES
            call_qualifies = len(sig_call_unwinds) >= min_str
            put_qualifies  = len(sig_put_unwinds)  >= min_str

            if not call_qualifies and not put_qualifies:
                logger.debug(
                    f"[OI_CAPIT] {stock.stock_symbol} — "
                    f"not enough significant strikes (call={len(sig_call_unwinds)} "
                    f"put={len(sig_put_unwinds)}, need {min_str}), skip"
                )
                return False

            # ── 5. Macro weight check ─────────────────────────────────────────
            total_call_unwound = sum(x[1] for x in sig_call_unwinds)
            total_put_unwound  = sum(x[1] for x in sig_put_unwinds)

            call_unwound_pct = (total_call_unwound / total_call_oi * 100
                                if total_call_oi > 0 else 0)
            put_unwound_pct  = (total_put_unwound  / total_put_oi  * 100
                                if total_put_oi  > 0 else 0)

            min_total_pct = OIChainAnalyser.OI_CAPITULATION_MIN_TOTAL_PCT
            call_significant = call_qualifies and call_unwound_pct >= min_total_pct
            put_significant  = put_qualifies  and put_unwound_pct  >= min_total_pct

            logger.debug(
                f"[OI_CAPIT] {stock.stock_symbol} | "
                f"CONDITION macro weight: "
                f"call_unwound={total_call_unwound:,.0f} ({call_unwound_pct:.1f}% of total) "
                f"put_unwound={total_put_unwound:,.0f} ({put_unwound_pct:.1f}% of total) "
                f"threshold={min_total_pct}% → "
                f"call_significant={call_significant} put_significant={put_significant}"
            )

            if not call_significant and not put_significant:
                logger.debug(
                    f"[OI_CAPIT] {stock.stock_symbol} — "
                    f"unwinding not macro-significant (need {min_total_pct}%), skip"
                )
                return False

            # ── Signal direction — fire the side with higher unwound_pct ─────
            res = False

            # Prefer the side with higher conviction; if equal, fire both
            fire_call = call_significant and (
                not put_significant or call_unwound_pct >= put_unwound_pct
            )
            fire_put = put_significant and (
                not call_significant or put_unwound_pct >= call_unwound_pct
            )

            if fire_call:
                top = sig_call_unwinds[:5]
                strikes_str = ", ".join(
                    f"{s:.0f}(-{r:,.0f}/{p:.0f}%)" for s, r, p in top
                )
                signal = (
                    f"CALL OI capitulation: {total_call_unwound:,.0f} contracts unwound "
                    f"({call_unwound_pct:.1f}% of total call OI) across "
                    f"{len(sig_call_unwinds)} strikes near money. "
                    f"Top strikes: {strikes_str} — Call writers covering, BULLISH"
                )
                stock.set_analysis("BULLISH", "OI_CAPITULATION", OICapitulation(
                    side="CALL",
                    total_unwound=total_call_unwound,
                    unwound_pct=round(call_unwound_pct, 2),
                    top_strikes=top,
                    num_significant_strikes=len(sig_call_unwinds),
                    signal=signal,
                    expiry=meta.get("expiry"),
                ))
                logger.info(
                    f"[OI_CAPIT] {stock.stock_symbol} — SIGNAL BULLISH CALL_CAPITULATION | "
                    f"unwound={total_call_unwound:,.0f} ({call_unwound_pct:.1f}%) "
                    f"strikes={len(sig_call_unwinds)}"
                )
                res = True

            if fire_put:
                top = sig_put_unwinds[:5]
                strikes_str = ", ".join(
                    f"{s:.0f}(-{r:,.0f}/{p:.0f}%)" for s, r, p in top
                )
                signal = (
                    f"PUT OI capitulation: {total_put_unwound:,.0f} contracts unwound "
                    f"({put_unwound_pct:.1f}% of total put OI) across "
                    f"{len(sig_put_unwinds)} strikes near money. "
                    f"Top strikes: {strikes_str} — Put writers abandoning floor, BEARISH"
                )
                stock.set_analysis("BEARISH", "OI_CAPITULATION", OICapitulation(
                    side="PUT",
                    total_unwound=total_put_unwound,
                    unwound_pct=round(put_unwound_pct, 2),
                    top_strikes=top,
                    num_significant_strikes=len(sig_put_unwinds),
                    signal=signal,
                    expiry=meta.get("expiry"),
                ))
                logger.info(
                    f"[OI_CAPIT] {stock.stock_symbol} — SIGNAL BEARISH PUT_CAPITULATION | "
                    f"unwound={total_put_unwound:,.0f} ({put_unwound_pct:.1f}%) "
                    f"strikes={len(sig_put_unwinds)}"
                )
                res = True

            return res

        except Exception as e:
            logger.error(f"[OI_CAPIT] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # 6. OI Wall Migration (Overnight Trench Move)
    # ──────────────────────────────────────────────────────────────────────────
    @BaseAnalyzer.positional
    @BaseAnalyzer.index_positional
    def analyse_oi_wall_migration(self, stock: Stock):
        """
        Detect overnight migration of dominant OI walls — where institutions
        rolled their defences up or down between yesterday's close and today.

        Put wall rising  (curr > prev) → floor raised overnight → BULLISH
        Call wall falling (curr < prev) → ceiling lowered overnight → BEARISH

        Wall definition (same statistical filter as analyse_oi_wall):
            OI > mean + OI_WALL_STD_MULTIPLIER × std for that side AND
            distance from that day's spot: OI_WALL_MIN_DISTANCE_PCT ≤ dist ≤ OI_WALL_MAX_DISTANCE_PCT

        Yesterday's walls are found using prev_call_oi / prev_put_oi per strike
        with prev_ltp as the reference spot price.

        Retreat edge case:
            Yesterday had a valid wall but today no wall passes filters → emit
            NEUTRAL OI_WALL_MIGRATION with migration_direction="RETREAT" as a
            warning that the institution removed its defence entirely.

        SOURCE DATA (DEBUG):   prev/curr wall strikes, statistical thresholds
        ANALYSER INPUT (DEBUG): migration delta, distance filters applied
        CONDITION (DEBUG):     direction decision and retreat detection per side
        """
        OIWallMigration = namedtuple("OIWallMigration", [
            "side",               # "CALL" or "PUT"
            "prev_wall_strike",   # yesterday's dominant wall (None if no baseline)
            "curr_wall_strike",   # today's dominant wall (None if retreat)
            "migration_direction",# "HIGHER", "LOWER", "UNCHANGED", "RETREAT"
            "migration_pts",      # curr - prev in absolute points (None if retreat)
            "migration_pct",      # migration_pts / prev_ltp * 100 (None if retreat)
            "signal",
            "expiry",
        ])

        try:
            logger.debug(f"[OI_WALL_MIG] {stock.stock_symbol} — start")

            per_strike_data, meta = self._get_oi_chain_data(stock)
            if per_strike_data is None:
                logger.debug(f"[OI_WALL_MIG] {stock.stock_symbol} — no OI chain data, skip")
                return False

            current_ltp = meta["current_ltp"]
            prev_ltp    = meta.get("prev_ltp")
            if not current_ltp:
                logger.debug(f"[OI_WALL_MIG] {stock.stock_symbol} — current_ltp missing, skip")
                return False
            if not prev_ltp or prev_ltp <= 0:
                logger.debug(
                    f"[OI_WALL_MIG] {stock.stock_symbol} — "
                    f"prev_ltp={prev_ltp} invalid, no yesterday baseline, skip"
                )
                return False

            # ── Expiry guard ──────────────────────────────────────────────────
            if self._expiry_guard(per_strike_data):
                logger.debug(f"[OI_WALL_MIG] {stock.stock_symbol} — expiry guard fired, skip")
                return False

            logger.debug(
                f"[OI_WALL_MIG] {stock.stock_symbol} | "
                f"SOURCE current_ltp={current_ltp} prev_ltp={prev_ltp} "
                f"expiry={meta.get('expiry')} num_strikes={len(per_strike_data)}"
            )

            # ── Compute strike width for minimum migration threshold ───────────
            sorted_strikes = sorted(float(s) for s in per_strike_data.keys())
            if len(sorted_strikes) >= 2:
                strike_width = min(
                    sorted_strikes[i + 1] - sorted_strikes[i]
                    for i in range(len(sorted_strikes) - 1)
                    if sorted_strikes[i + 1] - sorted_strikes[i] > 0
                )
            else:
                strike_width = 1.0
            min_migration = strike_width * OIChainAnalyser.OI_WALL_MIGRATION_MIN_POINTS

            std_mult  = OIChainAnalyser.OI_WALL_STD_MULTIPLIER
            min_dist  = OIChainAnalyser.OI_WALL_MIN_DISTANCE_PCT
            max_dist  = OIChainAnalyser.OI_WALL_MAX_DISTANCE_PCT

            # ── Find walls — TODAY ────────────────────────────────────────────
            today_call_wall, today_call_oi = self._find_dominant_wall(
                per_strike_data, "call_oi", current_ltp, std_mult, min_dist, max_dist
            )
            today_put_wall, today_put_oi = self._find_dominant_wall(
                per_strike_data, "put_oi", current_ltp, std_mult, min_dist, max_dist
            )

            # ── Find walls — YESTERDAY (prev_* OI keys, prev_ltp as spot) ────
            prev_call_wall, prev_call_oi_val = self._find_dominant_wall(
                per_strike_data, "prev_call_oi", prev_ltp, std_mult, min_dist, max_dist
            )
            prev_put_wall, prev_put_oi_val = self._find_dominant_wall(
                per_strike_data, "prev_put_oi", prev_ltp, std_mult, min_dist, max_dist
            )

            logger.debug(
                f"[OI_WALL_MIG] {stock.stock_symbol} | "
                f"CALL: prev_wall={prev_call_wall} (oi={prev_call_oi_val:,.0f}) "
                f"curr_wall={today_call_wall} (oi={today_call_oi:,.0f}) | "
                f"PUT: prev_wall={prev_put_wall} (oi={prev_put_oi_val:,.0f}) "
                f"curr_wall={today_put_wall} (oi={today_put_oi:,.0f}) | "
                f"strike_width={strike_width:.0f} min_migration={min_migration:.0f}"
            )

            # ── Evaluate each side independently ─────────────────────────────
            results = []

            for side, prev_wall, curr_wall in (
                ("CALL", prev_call_wall, today_call_wall),
                ("PUT",  prev_put_wall,  today_put_wall),
            ):
                if prev_wall is None:
                    logger.debug(
                        f"[OI_WALL_MIG] {stock.stock_symbol} {side} — "
                        f"no yesterday wall baseline, skip this side"
                    )
                    continue

                if curr_wall is None:
                    # Retreat — yesterday had a wall, today it vanished
                    signal = (
                        f"{side} wall RETREAT: yesterday's wall at {prev_wall:.0f} "
                        f"no longer passes statistical filter today — "
                        f"institution removed {'ceiling' if side == 'CALL' else 'floor'} defence"
                    )
                    logger.debug(
                        f"[OI_WALL_MIG] {stock.stock_symbol} {side} — RETREAT | "
                        f"prev_wall={prev_wall:.0f} has no valid wall today"
                    )
                    results.append(("NEUTRAL", OIWallMigration(
                        side=side,
                        prev_wall_strike=prev_wall,
                        curr_wall_strike=None,
                        migration_direction="RETREAT",
                        migration_pts=None,
                        migration_pct=None,
                        signal=signal,
                        expiry=meta.get("expiry"),
                    )))
                    continue

                migration_pts = curr_wall - prev_wall
                migration_pct = migration_pts / prev_ltp * 100

                logger.debug(
                    f"[OI_WALL_MIG] {stock.stock_symbol} {side} | "
                    f"CONDITION migration: {prev_wall:.0f} → {curr_wall:.0f} "
                    f"delta={migration_pts:+.0f} pts ({migration_pct:+.2f}%) "
                    f"min_required={min_migration:.0f} → "
                    f"{'PASS' if abs(migration_pts) >= min_migration else 'UNCHANGED/skip'}"
                )

                if abs(migration_pts) < min_migration:
                    logger.debug(
                        f"[OI_WALL_MIG] {stock.stock_symbol} {side} — "
                        f"migration {abs(migration_pts):.0f} < min {min_migration:.0f}, unchanged"
                    )
                    continue

                if side == "PUT":
                    if migration_pts > 0:
                        direction  = "HIGHER"
                        sentiment  = "BULLISH"
                        direction_desc = "Floor raised overnight — put writers defending higher level"
                    else:
                        direction  = "LOWER"
                        sentiment  = "BEARISH"
                        direction_desc = "Floor lowered overnight — put writers retreating"
                else:  # CALL
                    if migration_pts < 0:
                        direction  = "LOWER"
                        sentiment  = "BEARISH"
                        direction_desc = "Ceiling lowered overnight — call writers tightening resistance"
                    else:
                        direction  = "HIGHER"
                        sentiment  = "BULLISH"
                        direction_desc = "Ceiling raised overnight — call writers giving room to move up"

                signal = (
                    f"{side} wall migrated {prev_wall:.0f} → {curr_wall:.0f} "
                    f"({migration_pts:+.0f} pts, {migration_pct:+.2f}%) — {direction_desc}"
                )
                results.append((sentiment, OIWallMigration(
                    side=side,
                    prev_wall_strike=prev_wall,
                    curr_wall_strike=curr_wall,
                    migration_direction=direction,
                    migration_pts=migration_pts,
                    migration_pct=round(migration_pct, 2),
                    signal=signal,
                    expiry=meta.get("expiry"),
                )))

            if not results:
                logger.debug(f"[OI_WALL_MIG] {stock.stock_symbol} — no migration signal on either side")
                return False

            # ── Emit signals ──────────────────────────────────────────────────
            # If both sides signal opposite directions → conflicting, emit NEUTRAL
            sentiments = [r[0] for r in results if r[0] != "NEUTRAL"]
            if len(sentiments) == 2 and sentiments[0] != sentiments[1]:
                for sentiment, data in results:
                    stock.set_analysis("NEUTRAL", "OI_WALL_MIGRATION", data)
                    logger.info(
                        f"[OI_WALL_MIG] {stock.stock_symbol} — SIGNAL NEUTRAL (conflicting sides) | "
                        f"{data.side}: {data.prev_wall_strike}→{data.curr_wall_strike} ({data.migration_direction})"
                    )
            else:
                for sentiment, data in results:
                    stock.set_analysis(sentiment, "OI_WALL_MIGRATION", data)
                    logger.info(
                        f"[OI_WALL_MIG] {stock.stock_symbol} — SIGNAL {sentiment} {data.side} {data.migration_direction} | "
                        f"{data.prev_wall_strike}→{data.curr_wall_strike} "
                        f"delta={data.migration_pts:+.0f} pts"
                        if data.migration_pts is not None else
                        f"[OI_WALL_MIG] {stock.stock_symbol} — SIGNAL {sentiment} {data.side} RETREAT | "
                        f"prev_wall={data.prev_wall_strike:.0f} vanished today"
                    )

            return True

        except Exception as e:
            logger.error(f"[OI_WALL_MIG] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # 7. Positional OI Trend (oi_history daily bars)
    # ──────────────────────────────────────────────────────────────────────────
    @BaseAnalyzer.positional
    @BaseAnalyzer.index_positional
    def analyse_positional_oi_trend(self, stock: Stock):
        """
        Detect a sustained directional OI build-up over the last N trading days
        using daily call_oi, put_oi, futures_oi, and pcr from oi_history.

        This is the positional equivalent of analyse_intraday_oi_trend — same concept
        but over days (from compute_intraday 1D) rather than intraday snapshots.

        Signal conditions:
          BEARISH CALL_BUILDUP:        call_oi grew >= MIN_PCT over N days
                                       AND call growth exceeds put growth by >= DIFF_PCT
                                       AND call_oi monotonically rising (recent 3 of N)
          BEARISH CALL_BUILDUP_ALIGNED: above + futures_oi rising + pcr falling
          BULLISH PUT_BUILDUP:         symmetric for put side
          BULLISH PUT_BUILDUP_ALIGNED: above + futures_oi rising + pcr rising
          NEUTRAL BALANCED_ACCUMULATION: both sides growing >= MIN_PCT within DIFF_PCT of each other

        SOURCE DATA (DEBUG):   call_oi, put_oi, futures_oi, pcr first/last of window
        ANALYSER INPUT (DEBUG): change_pct per side, monotonic check
        CONDITION (DEBUG):     which branch matched with values vs thresholds
        """
        OIPositionalTrend = namedtuple("OIPositionalTrend", [
            "buildup_type",
            "call_oi_change_pct",
            "put_oi_change_pct",
            "futures_oi_change_pct",
            "pcr_change_pct",
            "current_pcr",
            "days_analysed",
            "signal",
            "expiry",
        ])

        try:
            logger.debug(f"[OI_POS_TREND] {stock.stock_symbol} — start")

            oi_history = stock.sensibull_ctx.get("oi_history")
            if oi_history is None or oi_history.empty:
                logger.debug(f"[OI_POS_TREND] {stock.stock_symbol} — no oi_history, skip")
                return False

            n = OIChainAnalyser.OI_POSITIONAL_TREND_DAYS
            if len(oi_history) < n:
                logger.debug(
                    f"[OI_POS_TREND] {stock.stock_symbol} — "
                    f"insufficient rows ({len(oi_history)}, need {n}), skip"
                )
                return False

            window = oi_history.tail(n).reset_index(drop=True)

            call_ois   = window["call_oi"].dropna().tolist()
            put_ois    = window["put_oi"].dropna().tolist()
            fut_ois    = window["futures_oi"].dropna().tolist()
            pcrs       = window["pcr"].dropna().tolist()

            if len(call_ois) < n or len(put_ois) < n:
                logger.debug(
                    f"[OI_POS_TREND] {stock.stock_symbol} — "
                    f"NaN values in window (call_rows={len(call_ois)} put_rows={len(put_ois)}), skip"
                )
                return False

            first_call = call_ois[0];  last_call = call_ois[-1]
            first_put  = put_ois[0];   last_put  = put_ois[-1]
            first_fut  = fut_ois[0]  if fut_ois  else None
            last_fut   = fut_ois[-1] if fut_ois  else None
            first_pcr  = pcrs[0]     if pcrs     else None
            last_pcr   = pcrs[-1]    if pcrs     else None

            call_chg_pct = (last_call - first_call) / first_call * 100 if first_call > 0 else 0
            put_chg_pct  = (last_put  - first_put)  / first_put  * 100 if first_put  > 0 else 0
            fut_chg_pct  = (
                (last_fut - first_fut) / first_fut * 100
                if first_fut and first_fut > 0 else 0
            )
            pcr_chg_pct  = (
                (last_pcr - first_pcr) / first_pcr * 100
                if first_pcr and first_pcr > 0 else 0
            )

            logger.debug(
                f"[OI_POS_TREND] {stock.stock_symbol} | "
                f"SOURCE window={n} days ({window['date'].iloc[0]}→{window['date'].iloc[-1]}) | "
                f"call_oi {first_call:,.0f}→{last_call:,.0f} | "
                f"put_oi {first_put:,.0f}→{last_put:,.0f} | "
                f"futures_oi {first_fut:,.0f}→{last_fut:,.0f} | "
                f"pcr {first_pcr}→{last_pcr}"
            )

            # ── Monotonic check on recent 3 of N bars (allows 1 deviation) ───
            def is_mostly_rising(values, tolerance=1):
                rises = sum(1 for i in range(len(values) - 1) if values[i + 1] > values[i])
                return rises >= len(values) - 1 - tolerance

            recent_3_call = call_ois[-3:]
            recent_3_put  = put_ois[-3:]
            call_mono_rising = is_mostly_rising(recent_3_call)
            put_mono_rising  = is_mostly_rising(recent_3_put)
            futures_rising   = fut_chg_pct > 0
            pcr_rising       = pcr_chg_pct > 0

            min_pct  = OIChainAnalyser.OI_POSITIONAL_TREND_MIN_PCT
            diff_pct = OIChainAnalyser.OI_POSITIONAL_TREND_DIFF_PCT

            logger.debug(
                f"[OI_POS_TREND] {stock.stock_symbol} | "
                f"INPUT call_chg={call_chg_pct:+.1f}% put_chg={put_chg_pct:+.1f}% "
                f"fut_chg={fut_chg_pct:+.1f}% pcr_chg={pcr_chg_pct:+.1f}% | "
                f"recent_3 call_mono_rising={call_mono_rising} put_mono_rising={put_mono_rising} | "
                f"futures_rising={futures_rising} pcr_rising={pcr_rising} | "
                f"thresholds: min_pct={min_pct}% diff_pct={diff_pct}%"
            )

            call_dominant = (
                call_chg_pct >= min_pct
                and (call_chg_pct - put_chg_pct) >= diff_pct
                and call_mono_rising
            )
            put_dominant = (
                put_chg_pct >= min_pct
                and (put_chg_pct - call_chg_pct) >= diff_pct
                and put_mono_rising
            )
            balanced = (
                call_chg_pct >= min_pct
                and put_chg_pct >= min_pct
                and abs(call_chg_pct - put_chg_pct) < diff_pct
            )

            expiry = oi_history["date"].iloc[-1] if "date" in oi_history.columns else None

            if call_dominant:
                aligned = futures_rising and not pcr_rising
                buildup_type = "CALL_BUILDUP_ALIGNED" if aligned else "CALL_BUILDUP"
                signal = (
                    f"Call OI grew {call_chg_pct:+.1f}% over {n} days "
                    f"(put: {put_chg_pct:+.1f}%, diff: {call_chg_pct - put_chg_pct:.1f}%)"
                    + (f" | Futures confirming (+{fut_chg_pct:.1f}%) PCR falling ({pcr_chg_pct:+.1f}%)"
                       if aligned else "")
                    + " — heavy call writing, BEARISH"
                )
                stock.set_analysis("BEARISH", "OI_POSITIONAL_TREND", OIPositionalTrend(
                    buildup_type=buildup_type,
                    call_oi_change_pct=round(call_chg_pct, 2),
                    put_oi_change_pct=round(put_chg_pct, 2),
                    futures_oi_change_pct=round(fut_chg_pct, 2),
                    pcr_change_pct=round(pcr_chg_pct, 2),
                    current_pcr=last_pcr,
                    days_analysed=n,
                    signal=signal,
                    expiry=expiry,
                ))
                logger.info(
                    f"[OI_POS_TREND] {stock.stock_symbol} — SIGNAL BEARISH {buildup_type} | "
                    f"call={call_chg_pct:+.1f}% put={put_chg_pct:+.1f}% "
                    f"fut={fut_chg_pct:+.1f}% pcr_chg={pcr_chg_pct:+.1f}% over {n}d"
                )
                return True

            if put_dominant:
                aligned = futures_rising and pcr_rising
                buildup_type = "PUT_BUILDUP_ALIGNED" if aligned else "PUT_BUILDUP"
                signal = (
                    f"Put OI grew {put_chg_pct:+.1f}% over {n} days "
                    f"(call: {call_chg_pct:+.1f}%, diff: {put_chg_pct - call_chg_pct:.1f}%)"
                    + (f" | Futures confirming (+{fut_chg_pct:.1f}%) PCR rising ({pcr_chg_pct:+.1f}%)"
                       if aligned else "")
                    + " — heavy put writing, BULLISH"
                )
                stock.set_analysis("BULLISH", "OI_POSITIONAL_TREND", OIPositionalTrend(
                    buildup_type=buildup_type,
                    call_oi_change_pct=round(call_chg_pct, 2),
                    put_oi_change_pct=round(put_chg_pct, 2),
                    futures_oi_change_pct=round(fut_chg_pct, 2),
                    pcr_change_pct=round(pcr_chg_pct, 2),
                    current_pcr=last_pcr,
                    days_analysed=n,
                    signal=signal,
                    expiry=expiry,
                ))
                logger.info(
                    f"[OI_POS_TREND] {stock.stock_symbol} — SIGNAL BULLISH {buildup_type} | "
                    f"put={put_chg_pct:+.1f}% call={call_chg_pct:+.1f}% "
                    f"fut={fut_chg_pct:+.1f}% pcr_chg={pcr_chg_pct:+.1f}% over {n}d"
                )
                return True

            if balanced:
                signal = (
                    f"Balanced OI accumulation over {n} days: "
                    f"call={call_chg_pct:+.1f}% put={put_chg_pct:+.1f}% "
                    f"(diff={abs(call_chg_pct - put_chg_pct):.1f}%) "
                    f"— premium collection phase, no directional bias"
                )
                stock.set_analysis("NEUTRAL", "OI_POSITIONAL_TREND", OIPositionalTrend(
                    buildup_type="BALANCED_ACCUMULATION",
                    call_oi_change_pct=round(call_chg_pct, 2),
                    put_oi_change_pct=round(put_chg_pct, 2),
                    futures_oi_change_pct=round(fut_chg_pct, 2),
                    pcr_change_pct=round(pcr_chg_pct, 2),
                    current_pcr=last_pcr,
                    days_analysed=n,
                    signal=signal,
                    expiry=expiry,
                ))
                logger.info(
                    f"[OI_POS_TREND] {stock.stock_symbol} — SIGNAL NEUTRAL BALANCED_ACCUMULATION | "
                    f"call={call_chg_pct:+.1f}% put={put_chg_pct:+.1f}% over {n}d"
                )
                return True

            logger.debug(
                f"[OI_POS_TREND] {stock.stock_symbol} — no signal | "
                f"call={call_chg_pct:+.1f}% put={put_chg_pct:+.1f}% "
                f"(need >={min_pct}% with >={diff_pct}% diff and monotonic recent-3)"
            )
            return False

        except Exception as e:
            logger.error(f"[OI_POS_TREND] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # 8. OI Acceleration Detector
    # ──────────────────────────────────────────────────────────────────────────
    @BaseAnalyzer.positional
    @BaseAnalyzer.index_positional
    def analyse_oi_acceleration(self, stock: Stock):
        """
        Detect a sudden intensification in daily OI writing velocity by comparing
        the mean daily call_oi_change (or put_oi_change) in the last 3 days vs
        the prior 3 days.

        A 2x+ jump in writing pace signals smart money rapidly building a position —
        more actionable than gradual accumulation (which OI_POSITIONAL_TREND covers).

        Gates:
        1. prev_3 mean velocity >= OI_ACCEL_MIN_BASE (avoids noise / near-zero divisors)
        2. recent_3 mean velocity >= OI_ACCEL_MIN_VELOCITY (ensures absolute significance)
        3. accel_ratio >= OI_ACCEL_MIN_RATIO (2x default)

        If both call and put sides accelerate, fires the side with higher ratio.

        SOURCE DATA (DEBUG):   call_oi_change and put_oi_change series for both windows
        ANALYSER INPUT (DEBUG): velocities and ratio per side
        CONDITION (DEBUG):     which gate failed or which side triggered
        """
        OIAcceleration = namedtuple("OIAcceleration", [
            "side",             # "CALL" or "PUT"
            "accel_ratio",      # recent_velocity / prev_velocity
            "recent_velocity",  # mean daily OI change in last 3 days
            "prev_velocity",    # mean daily OI change in prior 3 days
            "signal",
            "expiry",
        ])

        try:
            logger.debug(f"[OI_ACCEL] {stock.stock_symbol} — start")

            oi_history = stock.sensibull_ctx.get("oi_history")
            if oi_history is None or oi_history.empty:
                logger.debug(f"[OI_ACCEL] {stock.stock_symbol} — no oi_history, skip")
                return False

            if len(oi_history) < 6:
                logger.debug(
                    f"[OI_ACCEL] {stock.stock_symbol} — "
                    f"insufficient rows ({len(oi_history)}, need 6), skip"
                )
                return False

            recent_3 = oi_history.tail(3)
            prev_3   = oi_history.tail(6).head(3)

            # Use call_oi_change / put_oi_change columns (already daily deltas from API)
            recent_call_vals = recent_3["call_oi_change"].dropna().tolist()
            prev_call_vals   = prev_3["call_oi_change"].dropna().tolist()
            recent_put_vals  = recent_3["put_oi_change"].dropna().tolist()
            prev_put_vals    = prev_3["put_oi_change"].dropna().tolist()

            if len(recent_call_vals) < 2 or len(prev_call_vals) < 2:
                logger.debug(
                    f"[OI_ACCEL] {stock.stock_symbol} — "
                    f"not enough valid call_oi_change rows, skip"
                )
                return False

            recent_call_vel = sum(recent_call_vals) / len(recent_call_vals)
            prev_call_vel   = sum(prev_call_vals)   / len(prev_call_vals)
            recent_put_vel  = sum(recent_put_vals)  / len(recent_put_vals) if recent_put_vals else 0
            prev_put_vel    = sum(prev_put_vals)    / len(prev_put_vals)   if prev_put_vals   else 0

            expiry = oi_history["date"].iloc[-1] if "date" in oi_history.columns else None

            logger.debug(
                f"[OI_ACCEL] {stock.stock_symbol} | "
                f"SOURCE recent_3={recent_3['date'].tolist()} prev_3={prev_3['date'].tolist()} | "
                f"call recent_vals={[f'{v:,.0f}' for v in recent_call_vals]} "
                f"prev_vals={[f'{v:,.0f}' for v in prev_call_vals]} | "
                f"put recent_vals={[f'{v:,.0f}' for v in recent_put_vals]} "
                f"prev_vals={[f'{v:,.0f}' for v in prev_put_vals]}"
            )

            min_ratio    = OIChainAnalyser.OI_ACCEL_MIN_RATIO
            min_velocity = OIChainAnalyser.OI_ACCEL_MIN_VELOCITY
            min_base     = OIChainAnalyser.OI_ACCEL_MIN_BASE

            # Compute ratios — guard against near-zero prev velocity
            call_ratio = (
                recent_call_vel / prev_call_vel
                if prev_call_vel >= min_base else 0
            )
            put_ratio = (
                recent_put_vel / prev_put_vel
                if prev_put_vel >= min_base else 0
            )

            logger.debug(
                f"[OI_ACCEL] {stock.stock_symbol} | "
                f"INPUT call: recent_vel={recent_call_vel:,.0f} prev_vel={prev_call_vel:,.0f} "
                f"ratio={call_ratio:.2f}x | "
                f"put: recent_vel={recent_put_vel:,.0f} prev_vel={prev_put_vel:,.0f} "
                f"ratio={put_ratio:.2f}x | "
                f"CONDITION min_ratio={min_ratio}x min_velocity={min_velocity:,} "
                f"min_base={min_base:,}"
            )

            call_qualifies = (
                call_ratio >= min_ratio
                and recent_call_vel >= min_velocity
                and prev_call_vel   >= min_base
            )
            put_qualifies = (
                put_ratio >= min_ratio
                and recent_put_vel  >= min_velocity
                and prev_put_vel    >= min_base
            )

            logger.debug(
                f"[OI_ACCEL] {stock.stock_symbol} | "
                f"CONDITION call_qualifies={call_qualifies} "
                f"(ratio={call_ratio:.2f}>={min_ratio} vel={recent_call_vel:,.0f}>={min_velocity:,}) | "
                f"put_qualifies={put_qualifies} "
                f"(ratio={put_ratio:.2f}>={min_ratio} vel={recent_put_vel:,.0f}>={min_velocity:,})"
            )

            if not call_qualifies and not put_qualifies:
                logger.debug(f"[OI_ACCEL] {stock.stock_symbol} — no acceleration detected, skip")
                return False

            # If both qualify, fire the side with higher ratio
            if call_qualifies and put_qualifies:
                fire_call = call_ratio >= put_ratio
                fire_put  = not fire_call
            else:
                fire_call = call_qualifies
                fire_put  = put_qualifies

            res = False

            if fire_call:
                signal = (
                    f"Call writing accelerated {call_ratio:.1f}x in last 3 days "
                    f"(recent avg: {recent_call_vel:,.0f}/day vs prior: {prev_call_vel:,.0f}/day) "
                    f"— smart money rapidly building resistance, BEARISH"
                )
                stock.set_analysis("BEARISH", "OI_ACCELERATION", OIAcceleration(
                    side="CALL",
                    accel_ratio=round(call_ratio, 2),
                    recent_velocity=round(recent_call_vel),
                    prev_velocity=round(prev_call_vel),
                    signal=signal,
                    expiry=expiry,
                ))
                logger.info(
                    f"[OI_ACCEL] {stock.stock_symbol} — SIGNAL BEARISH CALL_ACCELERATION | "
                    f"ratio={call_ratio:.1f}x recent={recent_call_vel:,.0f} prev={prev_call_vel:,.0f}"
                )
                res = True

            if fire_put:
                signal = (
                    f"Put writing accelerated {put_ratio:.1f}x in last 3 days "
                    f"(recent avg: {recent_put_vel:,.0f}/day vs prior: {prev_put_vel:,.0f}/day) "
                    f"— smart money rapidly building support, BULLISH"
                )
                stock.set_analysis("BULLISH", "OI_ACCELERATION", OIAcceleration(
                    side="PUT",
                    accel_ratio=round(put_ratio, 2),
                    recent_velocity=round(recent_put_vel),
                    prev_velocity=round(prev_put_vel),
                    signal=signal,
                    expiry=expiry,
                ))
                logger.info(
                    f"[OI_ACCEL] {stock.stock_symbol} — SIGNAL BULLISH PUT_ACCELERATION | "
                    f"ratio={put_ratio:.1f}x recent={recent_put_vel:,.0f} prev={prev_put_vel:,.0f}"
                )
                res = True

            return res

        except Exception as e:
            logger.error(f"[OI_ACCEL] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ══════════════════════════════════════════════════════════════════════════
    # INTRADAY-ONLY: History-based trend analysis (requires oi_chain_history)
    # These methods use the last 15 periodic snapshots (every ~5 mins) to
    # detect intraday OI trends that single-snapshot analysis cannot.
    # ══════════════════════════════════════════════════════════════════════════

    # ──────────────────────────────────────────────────────────────────────────
    # 6. Intraday OI Trend (total OI + PCR direction over time)
    # ──────────────────────────────────────────────────────────────────────────
    @BaseAnalyzer.intraday
    @BaseAnalyzer.index_intraday
    def analyse_intraday_oi_trend(self, stock: Stock):
        """
        Track how total Call OI, Put OI, and PCR are trending intraday using
        the last N periodic snapshots (stored in oi_chain_history).
        
        Signals:
        - Call OI consistently rising + Put OI flat/falling → Bearish (call writing)
        - Put OI consistently rising + Call OI flat/falling → Bullish (put writing)
        - PCR rising trend → Bullish shift (more puts being added)
        - PCR falling trend → Bearish shift (more calls being added)
        - Both OI rising but PCR falling → Net bearish (call writing outpacing puts)
        
        Requires at least 3 snapshots for meaningful trend detection.
        """
        try:
            logger.debug(f"[OI_TREND] {stock.stock_symbol} — start")

            min_snaps = OIChainAnalyser.OI_TREND_MIN_SNAPSHOTS
            available = len(stock.sensibull_ctx.get("oi_chain_history", []))
            history = self._get_oi_chain_history(stock, min_snapshots=min_snaps)
            if history is None:
                logger.debug(
                    f"[OI_TREND] {stock.stock_symbol} — insufficient history "
                    f"({available} snapshots, need {min_snaps}) → FAIL, skip"
                )
                return False

            logger.debug(
                f"[OI_TREND] {stock.stock_symbol} — history OK: {available} snapshots available"
            )
            
            # Extract time series from history
            timestamps = [s["timestamp"] for s in history]
            call_ois = [s.get("total_call_oi", 0) for s in history]
            put_ois = [s.get("total_put_oi", 0) for s in history]
            pcrs = [s.get("pcr") for s in history]
            ltps = [s.get("current_ltp") for s in history]
            
            # Filter out None PCR values
            pcrs = [p for p in pcrs if p is not None]
            
            if len(call_ois) < 3 or len(pcrs) < 3:
                logger.debug(
                    f"[OI_TREND] {stock.stock_symbol} — too few valid OI/PCR rows "
                    f"(call_oi_rows={len(call_ois)} pcr_rows={len(pcrs)}, need 3 each), skip"
                )
                return False

            logger.debug(
                f"[OI_TREND] {stock.stock_symbol} | "
                f"SOURCE snapshots={len(history)} | "
                f"first_call_oi={call_ois[0]:,.0f} last_call_oi={call_ois[-1]:,.0f} | "
                f"first_put_oi={put_ois[0]:,.0f} last_put_oi={put_ois[-1]:,.0f} | "
                f"first_pcr={pcrs[0]:.3f} last_pcr={pcrs[-1]:.3f}"
            )

            # Calculate changes from first to last snapshot
            first_call_oi = call_ois[0]
            last_call_oi = call_ois[-1]
            first_put_oi = put_ois[0]
            last_put_oi = put_ois[-1]
            first_pcr = pcrs[0]
            last_pcr = pcrs[-1]
            
            call_oi_change = last_call_oi - first_call_oi
            put_oi_change = last_put_oi - first_put_oi
            call_oi_change_pct = (call_oi_change / first_call_oi * 100) if first_call_oi > 0 else 0
            put_oi_change_pct = (put_oi_change / first_put_oi * 100) if first_put_oi > 0 else 0
            pcr_change = last_pcr - first_pcr
            pcr_change_pct = (pcr_change / first_pcr * 100) if first_pcr > 0 else 0
            
            # Check for consistent trend using recent snapshots
            recent_n = min(5, len(call_ois))
            recent_call_ois = call_ois[-recent_n:]
            recent_put_ois = put_ois[-recent_n:]
            recent_pcrs = pcrs[-recent_n:]
            
            # Monotonic check (allowing 1 deviation)
            def is_mostly_rising(values, tolerance=1):
                rises = sum(1 for i in range(len(values)-1) if values[i+1] > values[i])
                return rises >= len(values) - 1 - tolerance
            
            def is_mostly_falling(values, tolerance=1):
                falls = sum(1 for i in range(len(values)-1) if values[i+1] < values[i])
                return falls >= len(values) - 1 - tolerance
            
            call_rising = is_mostly_rising(recent_call_ois)
            call_falling = is_mostly_falling(recent_call_ois)
            put_rising = is_mostly_rising(recent_put_ois)
            put_falling = is_mostly_falling(recent_put_ois)
            pcr_rising = is_mostly_rising(recent_pcrs)
            pcr_falling = is_mostly_falling(recent_pcrs)
            
            OITrend = namedtuple("OITrend", [
                "call_oi_trend", "put_oi_trend", "pcr_trend",
                "call_oi_change_pct", "put_oi_change_pct", "pcr_change_pct",
                "first_pcr", "last_pcr",
                "first_ltp", "last_ltp",
                "snapshots_used", "signal", "expiry"
            ])
            
            # Trend labels for internal signal logic (recent monotonic direction)
            call_trend_recent = "RISING" if call_rising else ("FALLING" if call_falling else "FLAT")
            put_trend_recent = "RISING" if put_rising else ("FALLING" if put_falling else "FLAT")

            first_ltp = ltps[0] if ltps[0] else 0
            last_ltp = ltps[-1] if ltps[-1] else 0
            expiry = history[-1].get("expiry")

            # Thresholds for trend classification and signal logic
            min_pcr_change = OIChainAnalyser.OI_TREND_MIN_PCR_CHANGE_PCT
            min_oi_change = OIChainAnalyser.OI_TREND_MIN_OI_CHANGE_PCT

            # Display trend labels — derived from overall change_pct so they match displayed numbers
            call_trend = "RISING" if call_oi_change_pct > min_oi_change else ("FALLING" if call_oi_change_pct < -min_oi_change else "FLAT")
            put_trend = "RISING" if put_oi_change_pct > min_oi_change else ("FALLING" if put_oi_change_pct < -min_oi_change else "FLAT")
            pcr_trend_dir = "RISING" if pcr_change_pct > min_pcr_change else ("FALLING" if pcr_change_pct < -min_pcr_change else "FLAT")

            logger.debug(
                f"[OI_TREND] {stock.stock_symbol} | "
                f"INPUT call_chg={call_oi_change_pct:+.1f}% ({call_trend}) "
                f"put_chg={put_oi_change_pct:+.1f}% ({put_trend}) "
                f"pcr_chg={pcr_change_pct:+.1f}% ({pcr_trend_dir}) | "
                f"recent_monotonic call={call_trend_recent} put={put_trend_recent} | "
                f"thresholds: min_oi={min_oi_change}% min_pcr={min_pcr_change}%"
            )

            # ── Determine signal using change_pct (first→last across all snapshots) ──

            signal_parts = []
            sentiment = None

            # Strong bearish: significant call OI build-up + PCR declining
            if call_oi_change_pct > min_oi_change and pcr_change_pct < -min_pcr_change:
                sentiment = "BEARISH"
                signal_parts.append(f"Call OI rising ({call_oi_change_pct:+.1f}%) + PCR falling ({pcr_change_pct:+.1f}%)")
                signal_parts.append("→ Aggressive call writing - Bearish")

            # Strong bullish: significant put OI build-up + PCR increasing
            elif put_oi_change_pct > min_oi_change and pcr_change_pct > min_pcr_change:
                sentiment = "BULLISH"
                signal_parts.append(f"Put OI rising ({put_oi_change_pct:+.1f}%) + PCR rising ({pcr_change_pct:+.1f}%)")
                signal_parts.append("→ Aggressive put writing - Bullish")

            # One-sided call writing: calls grew ≥2x more than puts
            elif (call_oi_change_pct > min_oi_change and
                  call_oi_change_pct >= put_oi_change_pct * 2 and
                  call_oi_change_pct - put_oi_change_pct > min_oi_change):
                sentiment = "BEARISH"
                signal_parts.append(f"Call OI surging ({call_oi_change_pct:+.1f}%) while Put OI {put_trend_recent.lower()} ({put_oi_change_pct:+.1f}%)")
                signal_parts.append("→ One-sided call writing - Bearish pressure")

            # One-sided put writing: puts grew ≥2x more than calls
            elif (put_oi_change_pct > min_oi_change and
                  put_oi_change_pct >= call_oi_change_pct * 2 and
                  put_oi_change_pct - call_oi_change_pct > min_oi_change):
                sentiment = "BULLISH"
                signal_parts.append(f"Put OI surging ({put_oi_change_pct:+.1f}%) while Call OI {call_trend_recent.lower()} ({call_oi_change_pct:+.1f}%)")
                signal_parts.append("→ One-sided put writing - Bullish support")

            # Both unwinding significantly
            elif (call_oi_change_pct < -min_oi_change and put_oi_change_pct < -min_oi_change):
                sentiment = "NEUTRAL"
                signal_parts.append(f"Both Call OI ({call_oi_change_pct:+.1f}%) and Put OI ({put_oi_change_pct:+.1f}%) declining sharply")
                signal_parts.append("→ Mass position unwinding - potential breakout ahead")

            else:
                return False  # No abnormal intraday trend
            
            signal = " | ".join(signal_parts)
            
            stock.set_analysis(sentiment, "OI_INTRADAY_TREND", OITrend(
                call_oi_trend=call_trend,
                put_oi_trend=put_trend,
                pcr_trend=pcr_trend_dir,
                call_oi_change_pct=call_oi_change_pct,
                put_oi_change_pct=put_oi_change_pct,
                pcr_change_pct=pcr_change_pct,
                first_pcr=first_pcr,
                last_pcr=last_pcr,
                first_ltp=first_ltp,
                last_ltp=last_ltp,
                snapshots_used=len(history),
                signal=signal,
                expiry=expiry
            ))
            logger.info(
                f"[OI_TREND] {stock.stock_symbol} — SIGNAL {sentiment} | "
                f"call={call_oi_change_pct:+.1f}% put={put_oi_change_pct:+.1f}% "
                f"pcr={first_pcr:.3f}→{last_pcr:.3f} ({pcr_change_pct:+.1f}%) "
                f"snapshots={len(history)} | {signal}"
            )
            return True

        except Exception as e:
            logger.error(f"[OI_TREND] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # 7. Intraday Support/Resistance Shift Detection
    # ──────────────────────────────────────────────────────────────────────────
    @BaseAnalyzer.intraday
    @BaseAnalyzer.index_intraday
    def analyse_intraday_oi_sr_shift(self, stock: Stock):
        """
        Track if OI-based support/resistance levels are shifting intraday.
        Uses oi_chain_history to compare max Call OI and max Put OI strikes
        across multiple snapshots.
        
        Signals:
        - Call wall (resistance) shifting LOWER → Bearish (writers tightening ceiling)
        - Call wall shifting HIGHER → Bullish (writers giving room)
        - Put wall (support) shifting HIGHER → Bullish (writers raising floor)
        - Put wall shifting LOWER → Bearish (writers lowering floor)
        
        Requires at least 3 snapshots.
        """
        try:
            logger.debug(f"[OI_SR_SHIFT] {stock.stock_symbol} — start")

            min_snaps = OIChainAnalyser.OI_SR_SHIFT_MIN_SNAPSHOTS
            available = len(stock.sensibull_ctx.get("oi_chain_history", []))
            history = self._get_oi_chain_history(stock, min_snapshots=min_snaps)
            if history is None:
                logger.debug(
                    f"[OI_SR_SHIFT] {stock.stock_symbol} — insufficient history "
                    f"({available} snapshots, need {min_snaps}), skip"
                )
                return False

            logger.debug(
                f"[OI_SR_SHIFT] {stock.stock_symbol} — history OK: {available} snapshots"
            )
            
            # Extract max call OI strike and max put OI strike from each snapshot
            call_resistance_strikes = []
            put_support_strikes = []
            
            for snapshot in history:
                per_strike_data = snapshot.get("per_strike_data", {})
                if not per_strike_data:
                    continue
                
                call_strike, _ = self._find_max_oi_strike(per_strike_data, "call_oi")
                put_strike, _ = self._find_max_oi_strike(per_strike_data, "put_oi")
                
                if call_strike is not None:
                    call_resistance_strikes.append(call_strike)
                if put_strike is not None:
                    put_support_strikes.append(put_strike)
            
            if len(call_resistance_strikes) < min_snaps or len(put_support_strikes) < min_snaps:
                logger.debug(
                    f"[OI_SR_SHIFT] {stock.stock_symbol} — not enough valid strike data "
                    f"(resistance={len(call_resistance_strikes)} support={len(put_support_strikes)}, "
                    f"need {min_snaps}), skip"
                )
                return False

            logger.debug(
                f"[OI_SR_SHIFT] {stock.stock_symbol} | "
                f"SOURCE resistance_series={call_resistance_strikes[:3]}...{call_resistance_strikes[-1]:.0f} | "
                f"support_series={put_support_strikes[:3]}...{put_support_strikes[-1]:.0f}"
            )
            
            # Check if resistance (max call OI strike) is shifting
            first_resistance = call_resistance_strikes[0]
            last_resistance = call_resistance_strikes[-1]
            resistance_shift = last_resistance - first_resistance
            
            # Check if support (max put OI strike) is shifting
            first_support = put_support_strikes[0]
            last_support = put_support_strikes[-1]
            support_shift = last_support - first_support
            
            current_ltp = history[-1].get("current_ltp", 0)
            expiry = history[-1].get("expiry")
            
            # ── Calculate strike width for minimum shift requirement ──
            # Use the actual strike gap from the latest snapshot
            latest_strikes = sorted([float(s) for s in history[-1].get("per_strike_data", {}).keys()])
            if len(latest_strikes) >= 2:
                strike_width = min(latest_strikes[i+1] - latest_strikes[i] 
                                  for i in range(len(latest_strikes) - 1) 
                                  if latest_strikes[i+1] - latest_strikes[i] > 0)
            else:
                strike_width = 1  # fallback
            
            min_shift = strike_width * OIChainAnalyser.OI_SR_SHIFT_MIN_STRIKE_WIDTHS

            logger.debug(
                f"[OI_SR_SHIFT] {stock.stock_symbol} | "
                f"strike_width={strike_width:.0f} min_shift={min_shift:.0f} "
                f"({OIChainAnalyser.OI_SR_SHIFT_MIN_STRIKE_WIDTHS} widths) | "
                f"resistance_shift={resistance_shift:.0f} support_shift={support_shift:.0f}"
            )

            # ── Gate: Shift must be >= minimum strike widths ──
            if abs(resistance_shift) < min_shift and abs(support_shift) < min_shift:
                logger.debug(
                    f"[OI_SR_SHIFT] {stock.stock_symbol} — shifts too small "
                    f"(R={resistance_shift:.0f} S={support_shift:.0f} min={min_shift:.0f}), skip"
                )
                return False
            
            # ── Gate: Shift must be consistent (not just first vs last) ──
            # Check that the majority of intermediate snapshots show progression
            def is_consistent_shift(strikes, min_required_direction_pct=60):
                """At least 60% of step-to-step changes must be in the same direction as overall"""
                if len(strikes) < 3:
                    return True
                overall = strikes[-1] - strikes[0]
                if overall == 0:
                    return False
                steps = [strikes[i+1] - strikes[i] for i in range(len(strikes) - 1)]
                nonzero_steps = [s for s in steps if s != 0]
                if not nonzero_steps:
                    return False
                consistent = sum(1 for s in nonzero_steps if (s > 0) == (overall > 0))
                return (consistent / len(nonzero_steps)) * 100 >= min_required_direction_pct
            
            resistance_consistent = is_consistent_shift(call_resistance_strikes) if abs(resistance_shift) >= min_shift else False
            support_consistent = is_consistent_shift(put_support_strikes) if abs(support_shift) >= min_shift else False

            logger.debug(
                f"[OI_SR_SHIFT] {stock.stock_symbol} | "
                f"CONDITION consistency: resistance_consistent={resistance_consistent} "
                f"support_consistent={support_consistent}"
            )

            # If neither shift is both large enough AND consistent, skip
            if not resistance_consistent and not support_consistent:
                logger.debug(f"[OI_SR_SHIFT] {stock.stock_symbol} — shifts not consistent, skip")
                return False
            
            # Only keep shifts that are both large and consistent
            if not resistance_consistent:
                resistance_shift = 0
            if not support_consistent:
                support_shift = 0
            
            # No meaningful shift after consistency check
            if resistance_shift == 0 and support_shift == 0:
                return False
            
            OISRShift = namedtuple("OISRShift", [
                "first_resistance", "last_resistance", "resistance_shift",
                "first_support", "last_support", "support_shift",
                "current_price", "range_narrowing",
                "snapshots_used", "signal", "expiry"
            ])
            
            # Detect range narrowing/widening
            first_range = first_resistance - first_support
            last_range = last_resistance - last_support
            range_narrowing = last_range < first_range
            
            signal_parts = []
            sentiment = None
            
            # Resistance shifting lower (bearish - ceiling coming down)
            if resistance_shift < 0:
                signal_parts.append(f"Resistance shifted {first_resistance:.0f}→{last_resistance:.0f} (↓{abs(resistance_shift):.0f})")
            elif resistance_shift > 0:
                signal_parts.append(f"Resistance shifted {first_resistance:.0f}→{last_resistance:.0f} (↑{resistance_shift:.0f})")
            
            # Support shifting
            if support_shift > 0:
                signal_parts.append(f"Support shifted {first_support:.0f}→{last_support:.0f} (↑{support_shift:.0f})")
            elif support_shift < 0:
                signal_parts.append(f"Support shifted {first_support:.0f}→{last_support:.0f} (↓{abs(support_shift):.0f})")
            
            # Determine overall sentiment
            # Both tightening downward → Bearish
            if resistance_shift < 0 and support_shift < 0:
                sentiment = "BEARISH"
                signal_parts.append("→ Both S/R shifting down - Bearish migration")
            # Both moving up → Bullish
            elif resistance_shift > 0 and support_shift > 0:
                sentiment = "BULLISH"
                signal_parts.append("→ Both S/R shifting up - Bullish migration")
            # Resistance down + Support up → Range squeeze → Big move expected
            elif resistance_shift < 0 and support_shift > 0:
                sentiment = "NEUTRAL"
                signal_parts.append(f"→ Range squeezing ({first_range:.0f}→{last_range:.0f}) - Breakout imminent")
            # Resistance up + Support down → Range expanding
            elif resistance_shift > 0 and support_shift < 0:
                sentiment = "NEUTRAL"
                signal_parts.append(f"→ Range widening ({first_range:.0f}→{last_range:.0f}) - Volatility expansion")
            # Only resistance shifting
            elif resistance_shift < 0:
                sentiment = "BEARISH"
                signal_parts.append("→ Resistance tightening - Bearish ceiling pressure")
            elif resistance_shift > 0:
                sentiment = "BULLISH"
                signal_parts.append("→ Resistance expanding - Room to move up")
            # Only support shifting
            elif support_shift > 0:
                sentiment = "BULLISH"
                signal_parts.append("→ Support rising - Bullish floor moving up")
            elif support_shift < 0:
                sentiment = "BEARISH"
                signal_parts.append("→ Support falling - Bearish floor collapsing")
            else:
                return False
            
            signal = " | ".join(signal_parts)
            
            stock.set_analysis(sentiment, "OI_SR_SHIFT", OISRShift(
                first_resistance=first_resistance,
                last_resistance=last_resistance,
                resistance_shift=resistance_shift,
                first_support=first_support,
                last_support=last_support,
                support_shift=support_shift,
                current_price=current_ltp,
                range_narrowing=range_narrowing,
                snapshots_used=len(history),
                signal=signal,
                expiry=expiry
            ))
            logger.info(
                f"[OI_SR_SHIFT] {stock.stock_symbol} — SIGNAL {sentiment} | "
                f"resistance {first_resistance:.0f}→{last_resistance:.0f} ({resistance_shift:+.0f}) "
                f"support {first_support:.0f}→{last_support:.0f} ({support_shift:+.0f}) "
                f"range_narrowing={range_narrowing} snapshots={len(history)} | {signal}"
            )
            return True

        except Exception as e:
            logger.error(f"[OI_SR_SHIFT] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False
