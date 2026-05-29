"""
GEXAnalyser — Gamma Exposure analysis for index option chains.

GEX (Gamma Exposure) measures how much delta-hedging dealers must do when
spot moves.  Dealers are short whatever retail bought:

    per_strike_gex = (CE_gamma × CE_OI − PE_gamma × PE_OI)
                     × lot_size × spot² / 1e7   [₹ crores]

    Net GEX = Σ per_strike_gex

    Positive GEX → dealers long gamma → they sell rallies / buy dips → market PINS
    Negative GEX → dealers short gamma → they amplify moves → market TRENDS

GEX flip level = strike where cumulative GEX (scanning outward from ATM) crosses
zero.  Above this level the market is in a damping regime; below it the market
is in an amplifying regime.

Data requirements:
  • gamma must be present in stock.options_live[strike][CE/PE]
  • Only populated by Sensibull WS (OPTIONS_SOURCE=sensibull or OPTIONS_SOURCE=both)
  • In Zerodha-only mode all gamma values are 0 → all methods return False silently

Applies to: LIVE_OPTIONS_INDICES only (NIFTY, BANKNIFTY, SENSEX).
"""

from __future__ import annotations

import traceback
from collections import namedtuple
from statistics import mean, stdev

from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
import common.constants as constant
import common.shared as shared

# ── Namedtuples ────────────────────────────────────────────────────────────────

GEX_REGIME_NT = namedtuple("GexRegime", [
    "regime",          # "POSITIVE" | "NEGATIVE"
    "gex_total",       # float, net GEX in ₹ crores
    "gex_ce",          # float, CE-side contribution
    "gex_pe",          # float, PE-side contribution
    "flip_level",      # float | None, zero-crossing strike
    "magnitude",       # "MILD" | "MODERATE" | "STRONG"
    "regime_flipped",  # bool, True when sign changed since last cycle
    "prev_regime",     # "POSITIVE" | "NEGATIVE" | None
])

GEX_FLIP_PROXIMITY_NT = namedtuple("GexFlipProximity", [
    "flip_level",        # float
    "spot",              # float
    "distance_pct",      # float, abs % distance from flip level
    "approaching_from",  # "ABOVE" | "BELOW"
    "gex_total",         # float, current net GEX
])

GEX_WALL_NT = namedtuple("GexWall", [
    "call_walls",         # list[float], sorted by GEX magnitude desc
    "put_walls",          # list[float]
    "nearest_call_wall",  # float | None
    "nearest_put_wall",   # float | None
    "call_wall_gex",      # float, GEX at nearest call wall
    "put_wall_gex",       # float, GEX at nearest put wall
])

GEX_WALL_BREACH_NT = namedtuple("GexWallBreach", [
    "breach_side",       # "CALL" | "PUT"
    "breached_strike",   # float
    "gex_at_strike",     # float, current GEX at that strike
    "gex_prev_cycle",    # float, GEX at that strike last cycle
    "gex_drop_pct",      # float, % drop in GEX at this strike
    "spot",              # float
    "spot_beyond_pct",   # float, how far spot is past the wall
])

GEX_IMBALANCE_NT = namedtuple("GexImbalance", [
    "dominant_side",   # "CE" | "PE"
    "gex_ce",          # float
    "gex_pe",          # float
    "imbalance_ratio", # float
    "magnitude",       # "MODERATE" | "STRONG" | "EXTREME"
])


class GEXAnalyser(BaseAnalyzer):
    """Gamma Exposure analyser for index option chains."""

    # ── Thresholds (recalibrated by reset_constants per mode) ──────────────────
    FLIP_PROXIMITY_THRESHOLD_PCT = 0.4   # % distance from flip level to fire
    WALL_SIGMA                   = 1.5   # σ multiplier for wall detection
    WALL_MIN_GEX_CR              = 100   # Min ₹ crores at wall (noise filter)
    IMBALANCE_RATIO_THRESHOLD    = 2.5   # CE/PE ratio to fire GEX_IMBALANCE
    IMBALANCE_MIN_SIDE_CR        = 100   # Both sides must exceed this (noise filter)
    GEX_WALL_BREACH_SPOT_PCT     = 0.3   # Spot must be ≥ this % beyond wall
    GEX_WALL_BREACH_DROP_PCT     = 30.0  # GEX at strike must drop ≥ this % to confirm
    GEX_NOISE_FLOOR_CR           = 200   # Below this net GEX is noise; skip flip proximity

    def __init__(self) -> None:
        self.analyserName = "GEX Analyser"
        super().__init__()

    def reset_constants(self) -> None:
        """Calibrate thresholds per mode (intraday = more sensitive)."""
        is_intraday = shared.app_ctx.mode.name == shared.Mode.INTRADAY.name
        if is_intraday:
            GEXAnalyser.FLIP_PROXIMITY_THRESHOLD_PCT = 0.4
            GEXAnalyser.WALL_SIGMA                   = 1.5
            GEXAnalyser.WALL_MIN_GEX_CR              = 100
            GEXAnalyser.GEX_NOISE_FLOOR_CR           = 200
        else:
            GEXAnalyser.FLIP_PROXIMITY_THRESHOLD_PCT = 0.6   # wider for EOD gap risk
            GEXAnalyser.WALL_SIGMA                   = 1.8   # stricter for positional
            GEXAnalyser.WALL_MIN_GEX_CR              = 200
            GEXAnalyser.GEX_NOISE_FLOOR_CR           = 300
        logger.debug(
            f"[GEXAnalyser] constants reset | mode={'intraday' if is_intraday else 'positional'} "
            f"flip_prox={GEXAnalyser.FLIP_PROXIMITY_THRESHOLD_PCT}% "
            f"wall_sigma={GEXAnalyser.WALL_SIGMA} "
            f"wall_min={GEXAnalyser.WALL_MIN_GEX_CR}Cr "
            f"noise_floor={GEXAnalyser.GEX_NOISE_FLOOR_CR}Cr"
        )

    # ── Gate helpers ───────────────────────────────────────────────────────────

    def _is_applicable(self, stock: Stock) -> bool:
        """Return True only for LIVE_OPTIONS_INDICES with gamma data."""
        if stock.stock_symbol not in constant.LIVE_OPTIONS_INDICES:
            return False
        if not stock.options_live:
            return False
        has_gamma = any(
            data.get("CE", {}).get("gamma", 0.0) or data.get("PE", {}).get("gamma", 0.0)
            for data in stock.options_live.values()
        )
        if not has_gamma:
            n_strikes = len(stock.options_live)
            logger.debug(
                f"[GEXAnalyser] {stock.stock_symbol} — no gamma in options_live "
                f"(strikes={n_strikes}, OPTIONS_SOURCE may be zerodha-only or "
                f"Sensibull enrichment not yet received), skip"
            )
            return False
        return True

    # ── Core GEX computation ───────────────────────────────────────────────────

    def _compute_gex(self, stock: Stock) -> tuple[float, float, float, dict, float | None]:
        """
        Compute per-strike and aggregate GEX.

        Returns:
            gex_total, gex_ce, gex_pe, gex_by_strike, flip_level
        """
        spot = stock.ltp or 0.0
        if spot <= 0:
            return 0.0, 0.0, 0.0, {}, None

        lot_size = constant.INDEX_LOT_SIZES.get(stock.stock_symbol, 1)
        normaliser = 1e7  # express in ₹ crores

        total_gex_ce = 0.0
        total_gex_pe = 0.0
        gex_by_strike: dict[float, float] = {}

        for strike, data in stock.options_live.items():
            ce = data.get("CE", {})
            pe = data.get("PE", {})

            ce_gamma = ce.get("gamma", 0.0) or 0.0
            pe_gamma = pe.get("gamma", 0.0) or 0.0
            ce_oi    = ce.get("oi", 0) or 0
            pe_oi    = pe.get("oi", 0) or 0

            gex_ce = ce_gamma * ce_oi * lot_size * (spot ** 2) / normaliser
            gex_pe = pe_gamma * pe_oi * lot_size * (spot ** 2) / normaliser

            strike_gex = gex_ce - gex_pe
            gex_by_strike[float(strike)] = strike_gex
            total_gex_ce += gex_ce
            total_gex_pe += gex_pe

        gex_total = total_gex_ce - total_gex_pe
        flip_level = self._find_flip_level(spot, gex_by_strike, gex_total)

        return gex_total, total_gex_ce, total_gex_pe, gex_by_strike, flip_level

    def _find_flip_level(
        self,
        spot: float,
        gex_by_strike: dict[float, float],
        gex_total: float,
    ) -> float | None:
        """
        Find the strike where cumulative GEX crosses zero.
        Scans from ATM outward, alternating above/below spot.
        Returns None if no zero-crossing found within subscribed strikes.
        """
        if not gex_by_strike or gex_total == 0.0:
            return None

        strikes_sorted = sorted(gex_by_strike.keys())
        if not strikes_sorted:
            return None

        atm = min(strikes_sorted, key=lambda s: abs(s - spot))
        atm_idx = strikes_sorted.index(atm)

        cumulative = 0.0
        prev_cumulative = 0.0
        for i in range(atm_idx, -1, -1):
            s = strikes_sorted[i]
            prev_cumulative = cumulative
            cumulative += gex_by_strike[s]
            if prev_cumulative * cumulative < 0:
                return s

        cumulative = 0.0
        prev_cumulative = 0.0
        for i in range(atm_idx, len(strikes_sorted)):
            s = strikes_sorted[i]
            prev_cumulative = cumulative
            cumulative += gex_by_strike[s]
            if prev_cumulative * cumulative < 0:
                return s

        return None

    @staticmethod
    def _magnitude(gex_total: float) -> str:
        abs_gex = abs(gex_total)
        if abs_gex >= 2000:
            return "STRONG"
        if abs_gex >= 500:
            return "MODERATE"
        return "MILD"

    # ── Signal 1: GEX_REGIME (both modes) ─────────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_gex_regime(self, stock: Stock) -> bool:
        """
        Compute net GEX and emit regime signal. Always fires when gamma is available.
        Detects regime flips (positive→negative or vice versa) via options_aggregate.

        SOURCE DATA (DEBUG):    options_live[strike][CE/PE] gamma + oi, spot, lot_size
        ANALYSER INPUT (DEBUG): gex_total, gex_ce, gex_pe, flip_level, prev_regime
        CONDITION (DEBUG):      gamma present + index in LIVE_OPTIONS_INDICES
        """
        try:
            logger.debug(f"[GEX_REGIME] {stock.stock_symbol} — start")

            if not self._is_applicable(stock):
                return False

            spot = stock.ltp or 0.0
            lot_size = constant.INDEX_LOT_SIZES.get(stock.stock_symbol, 1)
            n_strikes = len(stock.options_live)

            logger.debug(
                f"[GEX_REGIME] {stock.stock_symbol} | "
                f"SOURCE strikes={n_strikes}, spot={spot}, lot_size={lot_size}"
            )

            gex_total, gex_ce, gex_pe, gex_by_strike, flip_level = self._compute_gex(stock)

            new_regime  = "POSITIVE" if gex_total >= 0 else "NEGATIVE"
            prev_regime = stock.options_aggregate.get("gex_regime")
            flipped     = prev_regime is not None and prev_regime != new_regime
            magnitude   = self._magnitude(gex_total)

            logger.debug(
                f"[GEX_REGIME] {stock.stock_symbol} | "
                f"INPUT gex_total={gex_total:+.1f}Cr gex_ce={gex_ce:.1f}Cr gex_pe={gex_pe:.1f}Cr | "
                f"CONDITION regime={new_regime} prev={prev_regime} flipped={flipped} "
                f"flip_level={flip_level} magnitude={magnitude}"
            )

            # Persist to options_aggregate for cross-cycle comparisons
            stock.options_aggregate["gex_total"]      = gex_total
            stock.options_aggregate["gex_ce"]         = gex_ce
            stock.options_aggregate["gex_pe"]         = gex_pe
            stock.options_aggregate["gex_regime"]     = new_regime
            stock.options_aggregate["gex_flip_level"] = flip_level
            stock.options_aggregate["gex_by_strike"]  = gex_by_strike

            stock.set_analysis("NEUTRAL", "GEX_REGIME", GEX_REGIME_NT(
                regime=new_regime,
                gex_total=round(gex_total, 1),
                gex_ce=round(gex_ce, 1),
                gex_pe=round(gex_pe, 1),
                flip_level=flip_level,
                magnitude=magnitude,
                regime_flipped=flipped,
                prev_regime=prev_regime,
            ))

            if flipped:
                logger.info(
                    f"[GEX_REGIME] {stock.stock_symbol} — REGIME FLIP: "
                    f"{prev_regime} → {new_regime} | gex={gex_total:+.1f}Cr flip_level={flip_level}"
                )
            else:
                logger.info(
                    f"[GEX_REGIME] {stock.stock_symbol} — EMITTED | "
                    f"regime={new_regime} gex={gex_total:+.1f}Cr magnitude={magnitude} flip_level={flip_level}"
                )
            return True

        except Exception as e:
            logger.error(f"[GEX_REGIME] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ── Signal 2: GEX_FLIP_PROXIMITY (both modes, wider threshold positional) ─

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_gex_flip_proximity(self, stock: Stock) -> bool:
        """
        Fire when spot is within FLIP_PROXIMITY_THRESHOLD_PCT of the GEX flip level.
        Reads flip_level already written by analyse_gex_regime in the same cycle.

        SOURCE DATA (DEBUG):    options_aggregate["gex_flip_level"], stock.ltp
        ANALYSER INPUT (DEBUG): % distance between spot and flip level
        CONDITION (DEBUG):      distance < threshold AND |gex_total| > noise floor
        """
        try:
            logger.debug(f"[GEX_FLIP_PROXIMITY] {stock.stock_symbol} — start")

            if not self._is_applicable(stock):
                return False

            flip_level = stock.options_aggregate.get("gex_flip_level")
            gex_total  = stock.options_aggregate.get("gex_total", 0.0)
            spot       = stock.ltp or 0.0

            logger.debug(
                f"[GEX_FLIP_PROXIMITY] {stock.stock_symbol} | "
                f"SOURCE flip_level={flip_level}, spot={spot}, gex_total={gex_total:+.1f}Cr"
            )

            if flip_level is None or spot <= 0:
                logger.debug(f"[GEX_FLIP_PROXIMITY] {stock.stock_symbol} — no flip level computed, skip")
                return False

            if abs(gex_total) < GEXAnalyser.GEX_NOISE_FLOOR_CR:
                logger.debug(
                    f"[GEX_FLIP_PROXIMITY] {stock.stock_symbol} — "
                    f"INPUT |gex|={abs(gex_total):.1f}Cr | "
                    f"CONDITION below noise floor {GEXAnalyser.GEX_NOISE_FLOOR_CR}Cr, skip"
                )
                return False

            distance_pct     = abs(spot - flip_level) / flip_level * 100
            approaching_from = "ABOVE" if spot > flip_level else "BELOW"

            logger.debug(
                f"[GEX_FLIP_PROXIMITY] {stock.stock_symbol} | "
                f"INPUT distance={distance_pct:.3f}%, approaching_from={approaching_from} | "
                f"CONDITION distance <= threshold {GEXAnalyser.FLIP_PROXIMITY_THRESHOLD_PCT}%"
            )

            if distance_pct <= GEXAnalyser.FLIP_PROXIMITY_THRESHOLD_PCT:
                stock.set_analysis("NEUTRAL", "GEX_FLIP_PROXIMITY", GEX_FLIP_PROXIMITY_NT(
                    flip_level=flip_level,
                    spot=round(spot, 2),
                    distance_pct=round(distance_pct, 3),
                    approaching_from=approaching_from,
                    gex_total=round(gex_total, 1),
                ))
                logger.info(
                    f"[GEX_FLIP_PROXIMITY] {stock.stock_symbol} — EMITTED | "
                    f"spot={spot} flip={flip_level} dist={distance_pct:.3f}% "
                    f"approaching_from={approaching_from} gex={gex_total:+.1f}Cr"
                )
                return True

            logger.debug(
                f"[GEX_FLIP_PROXIMITY] {stock.stock_symbol} — no signal | "
                f"distance={distance_pct:.3f}% > threshold {GEXAnalyser.FLIP_PROXIMITY_THRESHOLD_PCT}%"
            )
            return False

        except Exception as e:
            logger.error(f"[GEX_FLIP_PROXIMITY] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ── Signal 3: GEX_WALL (both modes) ───────────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_gex_wall(self, stock: Stock) -> bool:
        """
        Detect gamma concentration walls (mean + WALL_SIGMA σ of per-strike GEX).
        Call walls → BEARISH bucket (resistance), Put walls → BULLISH bucket (support).

        SOURCE DATA (DEBUG):    options_aggregate["gex_by_strike"]
        ANALYSER INPUT (DEBUG): distribution stats (mean, sigma, threshold)
        CONDITION (DEBUG):      strike GEX > threshold, within ±5% of spot, abs GEX > min
        """
        try:
            logger.debug(f"[GEX_WALL] {stock.stock_symbol} — start")

            if not self._is_applicable(stock):
                return False

            gex_by_strike = stock.options_aggregate.get("gex_by_strike", {})
            spot = stock.ltp or 0.0

            logger.debug(
                f"[GEX_WALL] {stock.stock_symbol} | "
                f"SOURCE strikes={len(gex_by_strike)}, spot={spot}"
            )

            if len(gex_by_strike) < 5:
                logger.debug(
                    f"[GEX_WALL] {stock.stock_symbol} — "
                    f"too few strikes ({len(gex_by_strike)} < 5), skip"
                )
                return False

            if spot <= 0:
                return False

            values    = list(gex_by_strike.values())
            mu        = mean(values)
            sigma     = stdev(values) if len(values) > 1 else 0.0
            threshold = mu + GEXAnalyser.WALL_SIGMA * sigma
            range_pct = 0.05

            logger.debug(
                f"[GEX_WALL] {stock.stock_symbol} | "
                f"INPUT mean={mu:.1f}Cr sigma={sigma:.1f}Cr | "
                f"CONDITION threshold={threshold:.1f}Cr (mean + {GEXAnalyser.WALL_SIGMA}σ), "
                f"range=±{range_pct*100:.0f}% of spot, min={GEXAnalyser.WALL_MIN_GEX_CR}Cr"
            )

            call_walls: list[tuple[float, float]] = []
            put_walls:  list[tuple[float, float]] = []

            for strike, sgex in gex_by_strike.items():
                if abs(strike - spot) / spot > range_pct:
                    continue
                abs_gex = abs(sgex)
                if abs_gex < GEXAnalyser.WALL_MIN_GEX_CR:
                    continue
                if abs_gex <= abs(threshold):
                    continue
                if sgex > 0:
                    call_walls.append((strike, sgex))
                else:
                    put_walls.append((strike, abs(sgex)))

            call_walls.sort(key=lambda x: x[1], reverse=True)
            put_walls.sort(key=lambda x: x[1], reverse=True)

            if not call_walls and not put_walls:
                logger.debug(
                    f"[GEX_WALL] {stock.stock_symbol} — no signal | "
                    f"no strike exceeds threshold={threshold:.1f}Cr within ±{range_pct*100:.0f}% of spot"
                )
                return False

            nearest_call     = min(call_walls, key=lambda x: abs(x[0] - spot))[0] if call_walls else None
            nearest_put      = min(put_walls,  key=lambda x: abs(x[0] - spot))[0] if put_walls  else None
            nearest_call_gex = next((g for s, g in call_walls if s == nearest_call), 0.0)
            nearest_put_gex  = next((g for s, g in put_walls  if s == nearest_put),  0.0)

            wall_data = GEX_WALL_NT(
                call_walls=[s for s, _ in call_walls],
                put_walls=[s for s, _ in put_walls],
                nearest_call_wall=nearest_call,
                nearest_put_wall=nearest_put,
                call_wall_gex=round(nearest_call_gex, 1),
                put_wall_gex=round(nearest_put_gex, 1),
            )

            if call_walls:
                stock.set_analysis("BEARISH", "GEX_WALL", wall_data)
            if put_walls:
                stock.set_analysis("BULLISH", "GEX_WALL", wall_data)

            logger.info(
                f"[GEX_WALL] {stock.stock_symbol} — EMITTED | "
                f"call_walls={[f'{s:.0f}({g:.0f}Cr)' for s, g in call_walls]} "
                f"put_walls={[f'{s:.0f}({g:.0f}Cr)' for s, g in put_walls]}"
            )
            return True

        except Exception as e:
            logger.error(f"[GEX_WALL] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ── Signal 4: GEX_WALL_BREACH (intraday only) ──────────────────────────────

    @BaseAnalyzer.intraday
    @BaseAnalyzer.index_intraday
    def analyse_gex_wall_breach(self, stock: Stock) -> bool:
        """
        Detect when spot has broken through a GEX wall AND dealer GEX at that
        strike dropped ≥30% (dealers unwinding confirms the break is real).
        Needs a previous cycle's gex_by_strike — not applicable positional.

        SOURCE DATA (DEBUG):    current + previous cycle gex_by_strike, spot
        ANALYSER INPUT (DEBUG): prev_walls identified, per-strike GEX drop %
        CONDITION (DEBUG):      spot ≥ SPOT_PCT beyond wall + GEX drop ≥ DROP_PCT
        """
        try:
            logger.debug(f"[GEX_WALL_BREACH] {stock.stock_symbol} — start")

            if not self._is_applicable(stock):
                return False

            curr_gex_by_strike = stock.options_aggregate.get("gex_by_strike", {})
            prev_gex_by_strike: dict[float, float] = getattr(self, "_prev_gex_by_strike", {}).get(
                stock.stock_symbol, {}
            )

            # Persist current for next cycle before any early returns
            if not hasattr(self, "_prev_gex_by_strike"):
                self._prev_gex_by_strike = {}
            self._prev_gex_by_strike[stock.stock_symbol] = dict(curr_gex_by_strike)

            logger.debug(
                f"[GEX_WALL_BREACH] {stock.stock_symbol} | "
                f"SOURCE curr_strikes={len(curr_gex_by_strike)}, "
                f"prev_strikes={len(prev_gex_by_strike)}"
            )

            if not prev_gex_by_strike:
                logger.debug(
                    f"[GEX_WALL_BREACH] {stock.stock_symbol} — no previous cycle data (first run), skip"
                )
                return False

            spot = stock.ltp or 0.0
            if spot <= 0:
                return False

            prev_values = list(prev_gex_by_strike.values())
            if len(prev_values) < 5:
                return False
            prev_mu     = mean(prev_values)
            prev_sigma  = stdev(prev_values) if len(prev_values) > 1 else 0.0
            prev_thresh = prev_mu + GEXAnalyser.WALL_SIGMA * prev_sigma
            range_pct   = 0.05

            # Find previous cycle walls for logging
            prev_walls = [
                (s, g) for s, g in prev_gex_by_strike.items()
                if abs(s - spot) / spot <= range_pct * 1.5
                and abs(g) >= GEXAnalyser.WALL_MIN_GEX_CR
                and abs(g) > abs(prev_thresh)
            ]

            logger.debug(
                f"[GEX_WALL_BREACH] {stock.stock_symbol} | "
                f"INPUT prev_walls={[f'{s:.0f}({g:+.0f}Cr)' for s, g in prev_walls]}, spot={spot} | "
                f"CONDITION spot_beyond>={GEXAnalyser.GEX_WALL_BREACH_SPOT_PCT}%, "
                f"gex_drop>={GEXAnalyser.GEX_WALL_BREACH_DROP_PCT}%"
            )

            found = False
            for strike, prev_sgex in prev_gex_by_strike.items():
                if abs(strike - spot) / spot > range_pct * 1.5:
                    continue
                if abs(prev_sgex) < GEXAnalyser.WALL_MIN_GEX_CR:
                    continue
                if abs(prev_sgex) <= abs(prev_thresh):
                    continue

                curr_sgex = curr_gex_by_strike.get(strike, 0.0)

                if abs(prev_sgex) > 0:
                    gex_drop_pct = (abs(prev_sgex) - abs(curr_sgex)) / abs(prev_sgex) * 100
                else:
                    continue
                if gex_drop_pct < GEXAnalyser.GEX_WALL_BREACH_DROP_PCT:
                    logger.debug(
                        f"[GEX_WALL_BREACH] {stock.stock_symbol} | "
                        f"strike={strike:.0f} gex_drop={gex_drop_pct:.1f}% < "
                        f"threshold {GEXAnalyser.GEX_WALL_BREACH_DROP_PCT}% — dealers still defending"
                    )
                    continue

                if prev_sgex > 0 and spot > strike:
                    spot_beyond_pct = (spot - strike) / strike * 100
                    if spot_beyond_pct >= GEXAnalyser.GEX_WALL_BREACH_SPOT_PCT:
                        stock.set_analysis("BULLISH", "GEX_WALL_BREACH", GEX_WALL_BREACH_NT(
                            breach_side="CALL",
                            breached_strike=strike,
                            gex_at_strike=round(curr_sgex, 1),
                            gex_prev_cycle=round(prev_sgex, 1),
                            gex_drop_pct=round(gex_drop_pct, 1),
                            spot=round(spot, 2),
                            spot_beyond_pct=round(spot_beyond_pct, 2),
                        ))
                        logger.info(
                            f"[GEX_WALL_BREACH] {stock.stock_symbol} — CALL WALL BREACH EMITTED | "
                            f"strike={strike:.0f} spot={spot:.2f} beyond={spot_beyond_pct:.2f}% "
                            f"gex: {prev_sgex:+.0f}→{curr_sgex:+.0f}Cr (drop={gex_drop_pct:.1f}%)"
                        )
                        found = True

                elif prev_sgex < 0 and spot < strike:
                    spot_beyond_pct = (strike - spot) / strike * 100
                    if spot_beyond_pct >= GEXAnalyser.GEX_WALL_BREACH_SPOT_PCT:
                        stock.set_analysis("BEARISH", "GEX_WALL_BREACH", GEX_WALL_BREACH_NT(
                            breach_side="PUT",
                            breached_strike=strike,
                            gex_at_strike=round(curr_sgex, 1),
                            gex_prev_cycle=round(prev_sgex, 1),
                            gex_drop_pct=round(gex_drop_pct, 1),
                            spot=round(spot, 2),
                            spot_beyond_pct=round(spot_beyond_pct, 2),
                        ))
                        logger.info(
                            f"[GEX_WALL_BREACH] {stock.stock_symbol} — PUT WALL BREACH EMITTED | "
                            f"strike={strike:.0f} spot={spot:.2f} beyond={spot_beyond_pct:.2f}% "
                            f"gex: {prev_sgex:+.0f}→{curr_sgex:+.0f}Cr (drop={gex_drop_pct:.1f}%)"
                        )
                        found = True

            if not found:
                logger.debug(
                    f"[GEX_WALL_BREACH] {stock.stock_symbol} — no signal | "
                    f"no wall broken with spot_beyond>={GEXAnalyser.GEX_WALL_BREACH_SPOT_PCT}% "
                    f"and gex_drop>={GEXAnalyser.GEX_WALL_BREACH_DROP_PCT}%"
                )
            return found

        except Exception as e:
            logger.error(f"[GEX_WALL_BREACH] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False

    # ── Signal 5: GEX_IMBALANCE (both modes) ──────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_gex_imbalance(self, stock: Stock) -> bool:
        """
        Fire when CE or PE gamma dominates by ≥2.5x — dealers must delta-hedge
        aggressively on one side, creating a persistent directional headwind.

        SOURCE DATA (DEBUG):    options_aggregate["gex_ce"] / ["gex_pe"]
        ANALYSER INPUT (DEBUG): CE/PE GEX ratio
        CONDITION (DEBUG):      ratio > IMBALANCE_RATIO_THRESHOLD, both sides > min floor
        """
        try:
            logger.debug(f"[GEX_IMBALANCE] {stock.stock_symbol} — start")

            if not self._is_applicable(stock):
                return False

            gex_ce = stock.options_aggregate.get("gex_ce", 0.0)
            gex_pe = stock.options_aggregate.get("gex_pe", 0.0)

            logger.debug(
                f"[GEX_IMBALANCE] {stock.stock_symbol} | "
                f"SOURCE gex_ce={gex_ce:.1f}Cr gex_pe={gex_pe:.1f}Cr"
            )

            if gex_ce < GEXAnalyser.IMBALANCE_MIN_SIDE_CR or gex_pe < GEXAnalyser.IMBALANCE_MIN_SIDE_CR:
                logger.debug(
                    f"[GEX_IMBALANCE] {stock.stock_symbol} — no signal | "
                    f"one side below noise floor {GEXAnalyser.IMBALANCE_MIN_SIDE_CR}Cr "
                    f"(ce={gex_ce:.1f} pe={gex_pe:.1f})"
                )
                return False

            if gex_ce >= gex_pe * GEXAnalyser.IMBALANCE_RATIO_THRESHOLD:
                ratio    = gex_ce / gex_pe
                dominant = "CE"
                bucket   = "BEARISH"
            elif gex_pe >= gex_ce * GEXAnalyser.IMBALANCE_RATIO_THRESHOLD:
                ratio    = gex_pe / gex_ce
                dominant = "PE"
                bucket   = "BULLISH"
            else:
                logger.debug(
                    f"[GEX_IMBALANCE] {stock.stock_symbol} — no signal | "
                    f"INPUT ratio={max(gex_ce, gex_pe) / min(gex_ce, gex_pe):.2f}x | "
                    f"CONDITION below threshold {GEXAnalyser.IMBALANCE_RATIO_THRESHOLD}x"
                )
                return False

            magnitude = "EXTREME" if ratio > 6.0 else "STRONG" if ratio > 4.0 else "MODERATE"

            logger.debug(
                f"[GEX_IMBALANCE] {stock.stock_symbol} | "
                f"INPUT ratio={ratio:.2f}x dominant={dominant} | "
                f"CONDITION ratio >= threshold {GEXAnalyser.IMBALANCE_RATIO_THRESHOLD}x → {magnitude}"
            )

            stock.set_analysis(bucket, "GEX_IMBALANCE", GEX_IMBALANCE_NT(
                dominant_side=dominant,
                gex_ce=round(gex_ce, 1),
                gex_pe=round(gex_pe, 1),
                imbalance_ratio=round(ratio, 2),
                magnitude=magnitude,
            ))
            logger.info(
                f"[GEX_IMBALANCE] {stock.stock_symbol} — EMITTED [{bucket}] | "
                f"dominant={dominant} ratio={ratio:.2f}x magnitude={magnitude} "
                f"(ce={gex_ce:.1f}Cr pe={gex_pe:.1f}Cr)"
            )
            return True

        except Exception as e:
            logger.error(f"[GEX_IMBALANCE] {stock.stock_symbol} — exception: {e}")
            logger.error(traceback.format_exc())
            return False
