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
            OIChainAnalyser.OI_WALL_MIN_ASYMMETRY_RATIO = 1.8
            OIChainAnalyser.OI_SHIFT_MIN_WRITING_RATIO = 4.0
            OIChainAnalyser.OI_SHIFT_CENTER_THRESHOLD_PCT = 2.0
            OIChainAnalyser.OI_TREND_MIN_SNAPSHOTS = 5
            OIChainAnalyser.OI_TREND_MIN_PCR_CHANGE_PCT = 8.0
            OIChainAnalyser.OI_TREND_MIN_OI_CHANGE_PCT = 5.0
            OIChainAnalyser.OI_SR_SHIFT_MIN_SNAPSHOTS = 5
            OIChainAnalyser.OI_SR_SHIFT_MIN_STRIKE_WIDTHS = 2
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
            OIChainAnalyser.OI_WALL_MIN_ASYMMETRY_RATIO = 2.0
            OIChainAnalyser.OI_SHIFT_MIN_WRITING_RATIO = 5.0
            OIChainAnalyser.OI_SHIFT_CENTER_THRESHOLD_PCT = 3.0
            OIChainAnalyser.OI_TREND_MIN_SNAPSHOTS = 5
            OIChainAnalyser.OI_TREND_MIN_PCR_CHANGE_PCT = 8.0
            OIChainAnalyser.OI_TREND_MIN_OI_CHANGE_PCT = 5.0
            OIChainAnalyser.OI_SR_SHIFT_MIN_SNAPSHOTS = 5
            OIChainAnalyser.OI_SR_SHIFT_MIN_STRIKE_WIDTHS = 2
        
        logger.debug(f"OIChainAnalyser constants reset for mode {shared.app_ctx.mode.name}")

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
            per_strike_data, meta = self._get_oi_chain_data(stock)
            if per_strike_data is None:
                return False
            
            current_ltp = meta["current_ltp"]
            if not current_ltp:
                return False
            
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
                return False
            
            # ── Gate: S/R strikes must be dominant (OI >= dominance * avg) ──
            avg_call_oi = np.mean(all_call_ois) if all_call_ois else 0
            avg_put_oi = np.mean(all_put_ois) if all_put_ois else 0
            
            call_is_dominant = max_call_oi >= OIChainAnalyser.SR_MIN_OI_DOMINANCE * avg_call_oi
            put_is_dominant = max_put_oi >= OIChainAnalyser.SR_MIN_OI_DOMINANCE * avg_put_oi
            
            if not call_is_dominant and not put_is_dominant:
                logger.debug(f"OI S/R skipped for {stock.stock_symbol}: no dominant OI levels "
                           f"(call max {max_call_oi:,.0f} vs avg {avg_call_oi:,.0f}, "
                           f"put max {max_put_oi:,.0f} vs avg {avg_put_oi:,.0f})")
                return False
            
            # Top 3 for context
            call_oi_list.sort(key=lambda x: x[1], reverse=True)
            put_oi_list.sort(key=lambda x: x[1], reverse=True)
            top_resistances = call_oi_list[:3]
            top_supports = put_oi_list[:3]
            
            resistance_distance_pct = ((max_call_oi_strike - current_ltp) / current_ltp) * 100
            support_distance_pct = ((current_ltp - max_put_oi_strike) / current_ltp) * 100
            
            OISupportResistance = namedtuple("OISupportResistance", [
                "resistance_strike", "resistance_oi", "support_strike", "support_oi",
                "current_price", "resistance_distance_pct", "support_distance_pct",
                "top_resistances", "top_supports", "oi_range",
                "signal", "expiry"
            ])
            
            oi_range = f"{max_put_oi_strike:.0f} - {max_call_oi_strike:.0f}"
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
                logger.info(f"OI Support BREACHED for {stock.stock_symbol}: {signal}")
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
                logger.info(f"OI Resistance BREACHED for {stock.stock_symbol}: {signal}")
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
                logger.debug(f"OI S/R info (no breach) for {stock.stock_symbol}: {signal}")
            
            return signal_generated
            
        except Exception as e:
            logger.error(f"Error in analyse_oi_support_resistance for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
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
            per_strike_data, meta = self._get_oi_chain_data(stock)
            if per_strike_data is None:
                return False
            
            current_ltp = meta["current_ltp"]
            if not current_ltp:
                return False
            
            total_call_oi_change = meta.get("total_call_oi_change", 0)
            total_put_oi_change = meta.get("total_put_oi_change", 0)
            total_call_oi = meta.get("total_call_oi", 0)
            total_put_oi = meta.get("total_put_oi", 0)
            total_oi = total_call_oi + total_put_oi
            
            # ── Gate 1: Total OI change must be meaningful relative to total OI ──
            if total_oi == 0:
                return False
            total_abs_change = abs(total_call_oi_change) + abs(total_put_oi_change)
            total_change_pct = (total_abs_change / total_oi) * 100
            if total_change_pct < OIChainAnalyser.OI_BUILDUP_MIN_TOTAL_CHANGE_PCT:
                logger.debug(f"OI Buildup skipped for {stock.stock_symbol}: total change "
                           f"{total_change_pct:.1f}% < {OIChainAnalyser.OI_BUILDUP_MIN_TOTAL_CHANGE_PCT}% threshold")
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
                    ratio_str = f"{call_put_ratio:.1f}x put writing"
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
                logger.info(f"OI Buildup for {stock.stock_symbol}: {signal}")
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
                    ratio_str = f"{put_call_ratio:.1f}x call writing"
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
                logger.info(f"OI Buildup for {stock.stock_symbol}: {signal}")
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
                logger.info(f"OI Buildup for {stock.stock_symbol}: {signal}")
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
                logger.info(f"OI Buildup for {stock.stock_symbol}: {signal}")
                signal_generated = True
            
            return signal_generated
            
        except Exception as e:
            logger.error(f"Error in analyse_oi_buildup for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
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
            per_strike_data, meta = self._get_oi_chain_data(stock)
            if per_strike_data is None:
                return False
            
            current_ltp = meta["current_ltp"]
            if not current_ltp:
                return False
            
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
                return False
            
            avg_call_oi = np.mean(call_ois)
            avg_put_oi = np.mean(put_ois)
            std_call_oi = np.std(call_ois)
            std_put_oi = np.std(put_ois)
            
            # ── Statistical outlier threshold: mean + N * std ──
            call_wall_threshold = avg_call_oi + OIChainAnalyser.OI_WALL_STD_MULTIPLIER * std_call_oi
            put_wall_threshold = avg_put_oi + OIChainAnalyser.OI_WALL_STD_MULTIPLIER * std_put_oi
            
            max_dist = OIChainAnalyser.OI_WALL_MAX_DISTANCE_PCT
            
            call_walls = []
            put_walls = []
            
            for sd in strike_data_list:
                # ── Gate: Only within max distance ──
                if abs(sd["distance_pct"]) > max_dist:
                    continue
                
                if sd["call_oi"] > call_wall_threshold:
                    call_walls.append((sd["strike"], sd["call_oi"], sd["distance_pct"]))
                
                if sd["put_oi"] > put_wall_threshold:
                    put_walls.append((sd["strike"], sd["put_oi"], sd["distance_pct"]))
            
            if not call_walls and not put_walls:
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
                
                if asymmetry_ratio < OIChainAnalyser.OI_WALL_MIN_ASYMMETRY_RATIO:
                    # Walls are equidistant — normal market structure, no signal
                    logger.debug(f"OI Wall skipped for {stock.stock_symbol}: walls equidistant "
                               f"(call {call_dist:.1f}% vs put {put_dist:.1f}%, asymmetry {asymmetry_ratio:.1f}x)")
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
            logger.info(f"OI Wall for {stock.stock_symbol}: {signal}")
            return True
            
        except Exception as e:
            logger.error(f"Error in analyse_oi_wall for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
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
            per_strike_data, meta = self._get_oi_chain_data(stock)
            if per_strike_data is None:
                return False
            
            current_ltp = meta["current_ltp"]
            prev_ltp = meta.get("prev_ltp")
            if not current_ltp:
                return False
            
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
                return False
            
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
                return False  # No abnormal signal detected
            
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
            logger.info(f"OI Shift for {stock.stock_symbol}: {signal}")
            return True
            
        except Exception as e:
            logger.error(f"Error in analyse_oi_shift for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
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
            min_snaps = OIChainAnalyser.OI_TREND_MIN_SNAPSHOTS
            history = self._get_oi_chain_history(stock, min_snapshots=min_snaps)
            if history is None:
                logger.debug(f"Insufficient OI chain history for {stock.stock_symbol} "
                           f"({len(stock.sensibull_ctx.get('oi_chain_history', []))} snapshots, "
                           f"need {min_snaps})")
                return False
            
            # Extract time series from history
            timestamps = [s["timestamp"] for s in history]
            call_ois = [s.get("total_call_oi", 0) for s in history]
            put_ois = [s.get("total_put_oi", 0) for s in history]
            pcrs = [s.get("pcr") for s in history]
            ltps = [s.get("current_ltp") for s in history]
            
            # Filter out None PCR values
            pcrs = [p for p in pcrs if p is not None]
            
            if len(call_ois) < 3 or len(pcrs) < 3:
                return False
            
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
            
            call_trend = "RISING" if call_rising else ("FALLING" if call_falling else "FLAT")
            put_trend = "RISING" if put_rising else ("FALLING" if put_falling else "FLAT")
            pcr_trend_dir = "RISING" if pcr_rising else ("FALLING" if pcr_falling else "FLAT")
            
            first_ltp = ltps[0] if ltps[0] else 0
            last_ltp = ltps[-1] if ltps[-1] else 0
            expiry = history[-1].get("expiry")
            
            # ── Determine signal (stringent thresholds) ──
            min_pcr_change = OIChainAnalyser.OI_TREND_MIN_PCR_CHANGE_PCT
            min_oi_change = OIChainAnalyser.OI_TREND_MIN_OI_CHANGE_PCT
            
            signal_parts = []
            sentiment = None
            
            # Strong bearish: Call OI rising + PCR falling significantly
            if call_rising and pcr_falling and abs(pcr_change_pct) > min_pcr_change:
                sentiment = "BEARISH"
                signal_parts.append(f"Call OI rising ({call_oi_change_pct:+.1f}%) + PCR falling ({pcr_change_pct:+.1f}%)")
                signal_parts.append("→ Aggressive intraday call writing - Bearish")
            
            # Strong bullish: Put OI rising + PCR rising significantly
            elif put_rising and pcr_rising and abs(pcr_change_pct) > min_pcr_change:
                sentiment = "BULLISH"
                signal_parts.append(f"Put OI rising ({put_oi_change_pct:+.1f}%) + PCR rising ({pcr_change_pct:+.1f}%)")
                signal_parts.append("→ Aggressive intraday put writing - Bullish")
            
            # One-sided call writing: significant OI rise
            elif call_rising and not put_rising and call_oi_change_pct > min_oi_change:
                sentiment = "BEARISH"
                signal_parts.append(f"Call OI surging ({call_oi_change_pct:+.1f}%) while Put OI {put_trend.lower()}")
                signal_parts.append("→ One-sided call writing - Bearish pressure")
            
            # One-sided put writing: significant OI rise
            elif put_rising and not call_rising and put_oi_change_pct > min_oi_change:
                sentiment = "BULLISH"
                signal_parts.append(f"Put OI surging ({put_oi_change_pct:+.1f}%) while Call OI {call_trend.lower()}")
                signal_parts.append("→ One-sided put writing - Bullish support")
            
            # Both unwinding significantly
            elif (call_falling and put_falling and 
                  abs(call_oi_change_pct) > min_oi_change and abs(put_oi_change_pct) > min_oi_change):
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
            logger.info(f"Intraday OI Trend for {stock.stock_symbol}: {signal}")
            return True
            
        except Exception as e:
            logger.error(f"Error in analyse_intraday_oi_trend for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
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
            min_snaps = OIChainAnalyser.OI_SR_SHIFT_MIN_SNAPSHOTS
            history = self._get_oi_chain_history(stock, min_snapshots=min_snaps)
            if history is None:
                return False
            
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
                return False
            
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
            
            # ── Gate: Shift must be >= minimum strike widths ──
            if abs(resistance_shift) < min_shift and abs(support_shift) < min_shift:
                logger.debug(f"OI S/R Shift skipped for {stock.stock_symbol}: shifts too small "
                           f"(R: {resistance_shift:.0f}, S: {support_shift:.0f}, min: {min_shift:.0f})")
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
            
            # If neither shift is both large enough AND consistent, skip
            if not resistance_consistent and not support_consistent:
                logger.debug(f"OI S/R Shift skipped for {stock.stock_symbol}: shifts not consistent")
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
            logger.info(f"OI S/R Shift for {stock.stock_symbol}: {signal}")
            return True
            
        except Exception as e:
            logger.error(f"Error in analyse_intraday_oi_sr_shift for {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
