"""
PanicModeAnalyser v2 — Enhanced composite panic detector.

Reads stock.analysis populated by all earlier analysers.
MUST be registered LAST in AnalyserOrchestrator.

Changes from v1:
  - Fixed PCR_EXTREME misuse in C6 (was confirming direction; it is contrarian)
  - Added OBV_DIVERGENCE to C5 (fixes intraday VOLUME_CLIMAX 10-day-lookback gap)
  - Added IV_RANK / IV_RANK_EXTREME / IV_PREMIUM to C2 (IV fear depth)
  - Added OI_WALL, OI_SUPPORT_RESISTANCE breach, OI_SR_SHIFT to C3
  - Added FUTURE_BREAKOUT_CONFIRMED / FUTURE_BREAKOUT_MTF_ALIGNED to C4
  - Added PCR_REVERSAL to C6 (was only in exhaustion)
  - Added E5 — candlestick reversal pattern in contrarian direction
  - Added futures short-covering / long-unwinding to E4 exhaustion
  - VIX-adaptive price threshold (degrades gracefully when VIX unavailable)
  - Confidence tiers: MODERATE / HIGH / EXTREME on both signals
  - namedtuples defined at module level (not recreated each call)
  - Live signal hooks: IV_EXPANDING, CE/PE_WALL_BREACH, PCR_SUSTAINED / PCR_CROSSOVER
"""

import traceback
from collections import namedtuple

from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
import common.shared as shared


# ── Output namedtuples ────────────────────────────────────────────────────────

PANIC_MODE_NT = namedtuple("PANIC_MODE", [
    "direction", "price_change_pct", "conditions_met",
    "conditions_count", "confidence", "mode", "signal",
])

PANIC_EXHAUSTION_NT = namedtuple("PANIC_EXHAUSTION", [
    "panic_direction", "conditions_met", "conditions_count",
    "iv_percentile", "confidence", "mode", "signal",
])


class PanicModeAnalyser(BaseAnalyzer):
    """
    Composite analyser that detects panic conditions by cross-reading
    stock.analysis already populated by all earlier analysers.

    PANIC_MODE (active panic) — fires when >= PANIC_MIN_CONDITIONS (4/6) align:
        C1  Price momentum    ltp_change_perc >= VIX-adaptive threshold
        C2  IV expanding      IV_SPIKE / IV_TREND(UPWARD) / IV_RANK HIGH+ /
                              IV_RANK_EXTREME VERY_HIGH / IV_PREMIUM EXPENSIVE+ /
                              live IV_EXPANDING / live IV_TREND_RISING
        C3  OI confirming     OI_INTRADAY_TREND / OI_BUILDUP / OI_SHIFT /
                              OI_SUPPORT_RESISTANCE breach / OI_WALL / OI_SR_SHIFT /
                              live CE_WALL_BREACH (bearish) or PE_WALL_BREACH (bullish)
        C4  Futures confirm   FUTURE_ACTION_SHORT/LONG_BUILDUP / FUTURE_ACTION /
                              FUTURE_SIGNAL_SCORE_HIGH/MEDIUM /
                              FUTURE_BREAKOUT_CONFIRMED / FUTURE_BREAKOUT_MTF_ALIGNED
        C5  Volume surge      VOLUME_BREAKOUT / VOLUME_CLIMAX / OBV_DIVERGENCE
        C6  PCR directional   PCR_BIAS / PCR_TREND / PCR_REVERSAL /
                              live PCR_CROSSOVER / live PCR_SUSTAINED
                              [PCR_EXTREME deliberately excluded — it is contrarian]

    Confidence tiers: 4/6 → MODERATE, 5/6 → HIGH, 6/6 → EXTREME

    PANIC_EXHAUSTION (burning out) — fires when >= EXHAUSTION_MIN_CONDITIONS (3/5):
        E1  IV extreme        IV_RANK_EXTREME VERY_HIGH / IV_RANK IVP > 80 /
                              IV_PREMIUM EXTREME
        E2  Contrarian PCR    PCR_EXTREME or PCR_REVERSAL in OPPOSITE direction
        E3  Volume climax     VOLUME_CLIMAX in panic direction /
                              VOLUME_BREAKOUT as exhaustion proxy
        E4  Structural hold   OI_WALL contrarian / OI_SUPPORT_RESISTANCE NEUTRAL /
                              FUTURE_ACTION_SHORT_COVERING (bullish during BEARISH panic) /
                              FUTURE_ACTION_LONG_UNWINDING (bearish during BULLISH panic)
        E5  Candle reversal   Double/Triple/Single reversal pattern in contrarian direction

    Confidence tiers: 3/5 → MODERATE, 4/5 → HIGH, 5/5 → EXTREME

    VIX-adaptive threshold:
        VIX > 20  → threshold × 0.7 (market already volatile, lower bar)
        VIX < 13  → threshold × 1.3 (calm market, need larger move to confirm panic)
        Otherwise → base threshold unchanged
        Requires app_ctx.india_vix_ltp (float). Degrades gracefully if absent.
    """

    INTRADAY_PRICE_THRESHOLD   = 1.5
    POSITIONAL_PRICE_THRESHOLD = 3.0
    PANIC_MIN_CONDITIONS       = 4
    EXHAUSTION_MIN_CONDITIONS  = 3

    def __init__(self) -> None:
        self.analyserName = "Panic Mode Analyser"
        super().__init__()

    def reset_constants(self):
        pass

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _get(stock, sentiment, signal_type):
        return stock.analysis.get(sentiment, {}).get(signal_type)

    @staticmethod
    def _any_match(data, attr, value):
        if data is None:
            return False
        items = data if isinstance(data, list) else [data]
        return any(getattr(item, attr, None) == value for item in items)

    def _check_intraday_mode(self):
        return shared.app_ctx.mode.name == shared.Mode.INTRADAY.name

    def _price_threshold(self):
        base = (self.INTRADAY_PRICE_THRESHOLD if self._check_intraday_mode()
                else self.POSITIONAL_PRICE_THRESHOLD)
        try:
            vix = getattr(shared.app_ctx, "india_vix_ltp", None)
            if vix is not None:
                if vix > 20:
                    return max(base * 0.7, 1.0)
                if vix < 13:
                    return base * 1.3
        except Exception:
            pass
        return base

    # ── PANIC_MODE ────────────────────────────────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_panic_mode(self, stock: Stock):
        """
        Detects an active panic move confirmed across IV, OI, futures, volume, and PCR.
        Fires when >= PANIC_MIN_CONDITIONS (4/6) align in the same direction.
        """
        try:
            logger.debug(f"Inside analyse_panic_mode for {stock.stock_symbol}")

            price_change = stock.ltp_change_perc
            if price_change is None:
                return False

            threshold = self._price_threshold()
            if price_change <= -threshold:
                direction = "BEARISH"
            elif price_change >= threshold:
                direction = "BULLISH"
            else:
                return False

            conditions_met = [f"price({price_change:+.1f}%)"]  # C1 always present

            # ── C2: IV Fear / Options Premium Expanding ───────────────────────
            iv_ok = False
            iv_label = "IV_EXPANDING"

            if self._get(stock, "NEUTRAL", "IV_SPIKE"):
                iv_ok = True
            elif self._any_match(self._get(stock, "NEUTRAL", "IV_TREND"), "trend", "UPWARD"):
                iv_ok = True
            else:
                # IV_RANK / IV_RANK_EXTREME
                for key in ("IV_RANK_EXTREME", "IV_RANK"):
                    item = self._get(stock, "NEUTRAL", key)
                    if item:
                        items = item if isinstance(item, list) else [item]
                        for i in items:
                            cat = getattr(i, "category", "")
                            ivp = getattr(i, "iv_percentile", None)
                            if cat in ("HIGH", "VERY_HIGH") or (ivp is not None and ivp > 70):
                                iv_ok = True
                                iv_label = (f"IV_EXPANDING(IVP={ivp:.0f}%)"
                                            if ivp is not None else "IV_EXPANDING(HIGH)")
                                break
                    if iv_ok:
                        break

                # IV_PREMIUM (options overpriced vs HV — seller territory)
                if not iv_ok:
                    iv_prem = self._get(stock, "NEUTRAL", "IV_PREMIUM")
                    if iv_prem:
                        items = iv_prem if isinstance(iv_prem, list) else [iv_prem]
                        for i in items:
                            if getattr(i, "zone", "") in ("EXPENSIVE", "EXTREME"):
                                iv_ok = True
                                iv_label = f"IV_EXPANDING(HV_zone={getattr(i, 'zone', '')})"
                                break

                # Live straddle signals (stored by options engine into stock.analysis)
                if not iv_ok and self._get(stock, "NEUTRAL", "IV_EXPANDING"):
                    iv_ok = True
                    iv_label = "IV_EXPANDING(live)"
                if not iv_ok and self._get(stock, "NEUTRAL", "IV_TREND_RISING"):
                    iv_ok = True
                    iv_label = "IV_TREND_RISING(live)"

            if iv_ok:
                conditions_met.append(iv_label)

            # ── C3: OI Smart Money Confirming Direction ───────────────────────
            oi_ok = False
            if self._check_intraday_mode():
                oi_ok = bool(self._get(stock, direction, "OI_INTRADAY_TREND"))
            if not oi_ok:
                oi_ok = bool(self._get(stock, direction, "OI_BUILDUP"))
            if not oi_ok:
                oi_ok = bool(self._get(stock, direction, "OI_SHIFT"))
            if not oi_ok:
                # OI_SUPPORT_RESISTANCE breach stored directionally
                oi_ok = bool(self._get(stock, direction, "OI_SUPPORT_RESISTANCE"))
            if not oi_ok:
                oi_ok = bool(self._get(stock, direction, "OI_WALL"))
            if not oi_ok and self._check_intraday_mode():
                oi_ok = bool(self._get(stock, direction, "OI_SR_SHIFT"))
            # Live wall breach signals (stored by options engine)
            if not oi_ok:
                wall_key = "CE_WALL_BREACH" if direction == "BEARISH" else "PE_WALL_BREACH"
                oi_ok = bool(self._get(stock, direction, wall_key))
            if oi_ok:
                conditions_met.append("OI_CONFIRM")

            # ── C4: Futures Smart Money ────────────────────────────────────────
            if direction == "BEARISH":
                fut = (
                    self._get(stock, "BEARISH", "FUTURE_ACTION_SHORT_BUILDUP")
                    or self._get(stock, "BEARISH", "FUTURE_ACTION_LONG_UNWINDING")
                    or self._get(stock, "BEARISH", "FUTURE_ACTION")
                    or self._get(stock, "BEARISH", "FUTURE_SIGNAL_SCORE_HIGH")
                    or self._get(stock, "BEARISH", "FUTURE_SIGNAL_SCORE_MEDIUM")
                    or self._get(stock, "BEARISH", "FUTURE_BREAKOUT_CONFIRMED")
                    or self._get(stock, "BEARISH", "FUTURE_BREAKOUT_MTF_ALIGNED")
                )
            else:
                fut = (
                    self._get(stock, "BULLISH", "FUTURE_ACTION_LONG_BUILDUP")
                    or self._get(stock, "BULLISH", "FUTURE_ACTION_SHORT_COVERING")
                    or self._get(stock, "BULLISH", "FUTURE_ACTION")
                    or self._get(stock, "BULLISH", "FUTURE_SIGNAL_SCORE_HIGH")
                    or self._get(stock, "BULLISH", "FUTURE_SIGNAL_SCORE_MEDIUM")
                    or self._get(stock, "BULLISH", "FUTURE_BREAKOUT_CONFIRMED")
                    or self._get(stock, "BULLISH", "FUTURE_BREAKOUT_MTF_ALIGNED")
                )
            if fut:
                conditions_met.append("FUTURES_CONFIRM")

            # ── C5: Volume / Participation ────────────────────────────────────
            # OBV_DIVERGENCE added here: fixes intraday VOLUME_CLIMAX gap
            # (VolumeAnalyser.analyse_volume_climax uses a 10-day lookback that
            # never fires intraday; OBV_DIVERGENCE covers that gap)
            if (self._get(stock, direction, "VOLUME_BREAKOUT")
                    or self._get(stock, direction, "VOLUME_CLIMAX")
                    or self._get(stock, direction, "OBV_DIVERGENCE")):
                conditions_met.append("VOLUME_SURGE")

            # ── C6: PCR Directional Bias ──────────────────────────────────────
            # PCR_EXTREME deliberately excluded — it is a contrarian signal
            # (high PCR_EXTREME means excessive put buying = potential bounce,
            #  NOT a directional confirmation of the panic direction)
            pcr_ok = (
                self._get(stock, direction, "PCR_BIAS")
                or self._get(stock, direction, "PCR_TREND")
                or self._get(stock, direction, "PCR_REVERSAL")
            )
            if not pcr_ok:
                # Live PCR signals (stored by options engine)
                pcr_ok = (
                    self._get(stock, direction, f"PCR_CROSSOVER_{direction}")
                    or self._get(stock, direction, f"PCR_SUSTAINED_{direction}")
                )
                if pcr_ok:
                    conditions_met.append("PCR_CONFIRM(live)")
            else:
                conditions_met.append("PCR_CONFIRM")

            # ── Gate ──────────────────────────────────────────────────────────
            count = len(conditions_met)
            if count < self.PANIC_MIN_CONDITIONS:
                logger.debug(
                    f"PANIC_MODE not triggered for {stock.stock_symbol} ({direction}): "
                    f"{count}/{self.PANIC_MIN_CONDITIONS} — {conditions_met}"
                )
                return False

            confidence = {4: "MODERATE", 5: "HIGH"}.get(count, "EXTREME")
            mode_label = "intraday" if self._check_intraday_mode() else "positional"
            signal = (
                f"{direction} panic [{confidence}] — {count}/6 confirmed: "
                f"{', '.join(conditions_met)}"
            )

            stock.set_analysis(direction, "PANIC_MODE", PANIC_MODE_NT(
                direction=direction,
                price_change_pct=price_change,
                conditions_met=conditions_met,
                conditions_count=count,
                confidence=confidence,
                mode=mode_label,
                signal=signal,
            ))
            logger.info(f"PANIC MODE [{confidence}] for {stock.stock_symbol}: {signal}")
            return True

        except Exception as e:
            logger.error(f"Error in analyse_panic_mode for {stock.stock_symbol}: {e}")
            logger.error(traceback.format_exc())
            return False

    # ── PANIC_EXHAUSTION ──────────────────────────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_panic_exhaustion(self, stock: Stock):
        """
        Detects panic exhaustion — the panic move is burning out.
        Fires in the CONTRARIAN direction when >= EXHAUSTION_MIN_CONDITIONS (3/5).
        """
        try:
            logger.debug(f"Inside analyse_panic_exhaustion for {stock.stock_symbol}")

            price_change = stock.ltp_change_perc
            if price_change is None:
                return False

            threshold = self._price_threshold()
            if price_change <= -threshold:
                panic_direction = "BEARISH"
                contrarian      = "BULLISH"
            elif price_change >= threshold:
                panic_direction = "BULLISH"
                contrarian      = "BEARISH"
            else:
                return False

            conditions_met = []
            iv_percentile  = None

            # ── E1: IV at extreme peak (fear has peaked) ──────────────────────
            for key in ("IV_RANK_EXTREME", "IV_RANK"):
                item = self._get(stock, "NEUTRAL", key)
                if item:
                    items = item if isinstance(item, list) else [item]
                    for i in items:
                        ivp      = getattr(i, "iv_percentile", None)
                        ivp_type = getattr(i, "ivp_type", None)
                        cat      = getattr(i, "category", "")
                        if (ivp_type == "VERY_HIGH" or cat == "VERY_HIGH"
                                or (ivp is not None and ivp > 80)):
                            label = (f"IV_EXTREME(IVP={ivp:.0f}%)"
                                     if ivp is not None else "IV_EXTREME")
                            conditions_met.append(label)
                            iv_percentile = ivp
                            break
                if iv_percentile is not None:
                    break

            # IV_PREMIUM EXTREME also qualifies
            if iv_percentile is None:
                iv_prem = self._get(stock, "NEUTRAL", "IV_PREMIUM")
                if iv_prem:
                    items = iv_prem if isinstance(iv_prem, list) else [iv_prem]
                    for i in items:
                        if getattr(i, "zone", "") == "EXTREME":
                            conditions_met.append("IV_EXTREME(vs_HV)")
                            break

            # ── E2: Contrarian PCR (excessive options buying = smart money fading) ──
            if (self._get(stock, contrarian, "PCR_EXTREME")
                    or self._get(stock, contrarian, "PCR_REVERSAL")):
                conditions_met.append("PCR_CONTRARIAN")

            # ── E3: Volume climax in panic direction ──────────────────────────
            if self._get(stock, panic_direction, "VOLUME_CLIMAX"):
                conditions_met.append("VOLUME_CLIMAX")
            elif self._get(stock, panic_direction, "VOLUME_BREAKOUT"):
                # High-volume breakout at price extreme acts as exhaustion proxy
                conditions_met.append("VOLUME_CLIMAX(breakout)")

            # ── E4: Structural support holding OR futures turning ─────────────
            structural_ok = False
            oi_wall = self._get(stock, contrarian, "OI_WALL")
            if oi_wall is None:
                oi_wall = self._get(stock, "NEUTRAL", "OI_SUPPORT_RESISTANCE")
            if oi_wall:
                conditions_met.append("STRUCTURAL_HOLD")
                structural_ok = True

            # Futures participants turning contrarian = exhaustion confirmation
            if panic_direction == "BEARISH":
                fut_turn = self._get(stock, "BULLISH", "FUTURE_ACTION_SHORT_COVERING")
            else:
                fut_turn = self._get(stock, "BEARISH", "FUTURE_ACTION_LONG_UNWINDING")
            if fut_turn and not structural_ok:
                conditions_met.append("FUTURES_TURNING")

            # ── E5: Candlestick reversal pattern in contrarian direction ──────
            for candle_key in (
                "Double_candle_stick_pattern",
                "Triple_candle_stick_pattern",
                "Triple_candle_reversal_pattern",
                "Single_candle_reversal_pattern",
            ):
                if self._get(stock, contrarian, candle_key):
                    conditions_met.append("CANDLE_REVERSAL")
                    break

            # ── Gate ──────────────────────────────────────────────────────────
            count = len(conditions_met)
            if count < self.EXHAUSTION_MIN_CONDITIONS:
                logger.debug(
                    f"PANIC_EXHAUSTION not triggered for {stock.stock_symbol} "
                    f"(panic={panic_direction}): "
                    f"{count}/{self.EXHAUSTION_MIN_CONDITIONS} — {conditions_met}"
                )
                return False

            confidence = {3: "MODERATE", 4: "HIGH"}.get(count, "EXTREME")
            mode_label = "intraday" if self._check_intraday_mode() else "positional"
            signal = (
                f"{panic_direction} panic exhaustion [{confidence}] — {count}/5 confirmed: "
                f"{', '.join(conditions_met)}"
            )

            stock.set_analysis(contrarian, "PANIC_EXHAUSTION", PANIC_EXHAUSTION_NT(
                panic_direction=panic_direction,
                conditions_met=conditions_met,
                conditions_count=count,
                iv_percentile=iv_percentile,
                confidence=confidence,
                mode=mode_label,
                signal=signal,
            ))
            logger.info(f"PANIC EXHAUSTION [{confidence}] for {stock.stock_symbol}: {signal}")
            return True

        except Exception as e:
            logger.error(f"Error in analyse_panic_exhaustion for {stock.stock_symbol}: {e}")
            logger.error(traceback.format_exc())
            return False
