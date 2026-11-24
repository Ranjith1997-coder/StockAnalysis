"""Volatility context: IV rank/VIX regime, term structure, expected move & POP.

Block 2 indicators (roadmap I1, I3, I6). They emit into NEUTRAL and are meant
for measurement first — the strike-placement POP gate in paper trading ships
disabled (`PAPER_MIN_POP=0`).
"""
from __future__ import annotations

from collections import namedtuple

import common.shared as shared
from lib.fno.option_math import (
    daily_expected_move,
    expected_move_band,
    expected_move_sigma,
    iv_percentile,
    pop_condor,
    pop_short_call,
    pop_short_put,
    suggest_condor_strikes,
)

from .Analyser import BaseAnalyzer

IVContext = namedtuple("IVContext", [
    "expiry", "iv", "ivp", "ivp_source", "zone",
    "vix", "vix_regime", "iv_minus_vix",
])
VixRegime = namedtuple("VixRegime", ["regime", "vix", "iv_profile"])
TermStructure = namedtuple("TermStructure", [
    "regime", "near_expiry", "next_expiry", "near_iv", "next_iv",
    "slope", "slope_pct", "front_dte", "back_dte", "event_week", "signal",
])
ExpectedMove = namedtuple("ExpectedMove", [
    "straddle", "daily_move", "sigma_1d", "dte", "lower_1s", "upper_1s",
])
PopLadder = namedtuple("PopLadder", [
    "widths", "strangle_pop", "condor_pop",
    "suggested_width", "suggested_put", "suggested_call", "target_pop",
])

IV_ZONES = ((10.0, "VERY_LOW"), (20.0, "LOW"), (70.0, "NORMAL"),
            (85.0, "HIGH"), (101.0, "VERY_HIGH"))
TARGET_POPS = (0.70, 0.80)
MIN_LOCAL_SAMPLES = 20
POP_WIDTHS = (1, 2, 3, 4)


def _f(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def iv_zone(ivp: float) -> str:
    for threshold, label in IV_ZONES:
        if ivp < threshold:
            return label
    return "VERY_HIGH"


def vix_regime(vix: float | None) -> str:
    if vix is None:
        return "UNKNOWN"
    if vix < 13:
        return "CALM"
    if vix < 18:
        return "NORMAL"
    if vix < 25:
        return "ELEVATED"
    return "PANIC"


class VolatilityContextAnalyser(BaseAnalyzer):
    """Emits IV_CONTEXT, VIX_REGIME, TERM_STRUCTURE, EXPECTED_MOVE, POP_LADDER."""

    def reset_constants(self, *args, **kwargs):
        """Mode-independent formulas; nothing to reset (orchestrator contract)."""
        return None

    # ── shared inputs ────────────────────────────────────────────────────

    def _nearest(self, stock):
        per_expiry = (
            stock.sensibull_ctx.get("current", {})
            .get("stats", {})
            .get("per_expiry_map", {})
        )
        if not per_expiry:
            return None, None, per_expiry
        expiry = sorted(per_expiry.keys())[0]
        return expiry, per_expiry[expiry], per_expiry

    def _local_iv_percentile(self, stock, iv: float) -> float | None:
        history = stock.sensibull_ctx.get("iv_chart_history")
        if history is None or getattr(history, "empty", True):
            return None
        if "iv_close" not in history.columns:
            return None
        return iv_percentile(history["iv_close"].tolist(), iv,
                             min_samples=MIN_LOCAL_SAMPLES)

    def _strike_gap(self, stock) -> float | None:
        try:
            store = getattr(stock, "_tick_store", None)
            strikes = sorted(float(k) for k in (store.options_live or {}))
        except (AttributeError, TypeError, ValueError):
            return None
        gaps = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
        return min(gaps) if gaps else None

    # ── I1: IV rank + VIX regime ─────────────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_iv_context(self, stock):
        expiry, expiry_data, per_expiry = self._nearest(stock)
        if expiry is None:
            return False
        iv_raw = _f(expiry_data.get("atm_iv"))
        if iv_raw is None:
            return False
        iv = iv_raw if iv_raw > 1 else iv_raw * 100

        vendor = _f(expiry_data.get("atm_iv_percentile"))
        if vendor is not None and vendor <= 1:
            vendor *= 100
        local = self._local_iv_percentile(stock, iv)
        if local is not None:
            ivp, source = local, "local"
        elif vendor is not None:
            ivp, source = vendor, "vendor"
        else:
            ivp, source = None, "insufficient"

        vix = _f(getattr(shared.app_ctx, "india_vix_ltp", None))
        regime = vix_regime(vix)
        premium = (iv - vix) if vix is not None else None

        if ivp is None:
            stock.set_analysis("NEUTRAL", "VIX_REGIME", VixRegime(
                regime=regime, vix=vix, iv_profile="UNKNOWN"))
            return True

        stock.set_analysis("NEUTRAL", "IV_CONTEXT", IVContext(
            expiry=expiry, iv=iv, ivp=ivp, ivp_source=source,
            zone=iv_zone(ivp), vix=vix, vix_regime=regime, iv_minus_vix=premium,
        ))
        stock.set_analysis("NEUTRAL", "VIX_REGIME", VixRegime(
            regime=regime, vix=vix,
            iv_profile="RICH" if (premium or 0) > 3 else
                       "CHEAP" if (premium or 0) < -3 else "FAIR",
        ))
        return True

    # ── I3: term structure ───────────────────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_term_structure(self, stock):
        _, _, per_expiry = self._nearest(stock)
        expiries = sorted(per_expiry.keys())
        if len(expiries) < 2:
            return False
        near_key, next_key = expiries[0], expiries[1]
        near_raw = _f((per_expiry[near_key] or {}).get("atm_iv"))
        next_raw = _f((per_expiry[next_key] or {}).get("atm_iv"))
        if near_raw is None or next_raw is None:
            return False
        near_iv = near_raw if near_raw > 1 else near_raw * 100
        next_iv = next_raw if next_raw > 1 else next_raw * 100
        slope = next_iv - near_iv
        slope_pct = (slope / near_iv * 100) if near_iv else 0.0

        if slope >= 0.25:
            regime, signal = "CONTANGO", "Calm carry: front premium decays fastest"
        elif slope <= -0.25:
            regime, signal = "BACKWARDATION", "Event/stress in the front expiry"
        else:
            regime, signal = "FLAT", "No carry edge between expiries"

        front_dte = (per_expiry[near_key] or {}).get("days_to_expiry")
        back_dte = (per_expiry[next_key] or {}).get("days_to_expiry")
        stock.set_analysis("NEUTRAL", "TERM_STRUCTURE", TermStructure(
            regime=regime, near_expiry=near_key, next_expiry=next_key,
            near_iv=near_iv, next_iv=next_iv, slope=slope, slope_pct=slope_pct,
            front_dte=front_dte, back_dte=back_dte,
            event_week=bool(front_dte is not None and int(front_dte) <= 2),
            signal=signal,
        ))
        return True

    # ── I6: expected move + POP ladder ───────────────────────────────────

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_placement(self, stock):
        expiry, expiry_data, _ = self._nearest(stock)
        if expiry is None:
            return False
        straddle = _f(stock.options_aggregate.get("atm_straddle_premium"))
        spot = _f(stock.ltp)
        iv_raw = _f(expiry_data.get("atm_iv"))
        dte = expiry_data.get("days_to_expiry")
        if not straddle or not spot or iv_raw is None:
            return False
        iv = (iv_raw if iv_raw > 1 else iv_raw * 100) / 100.0
        dte = int(dte) if dte is not None else 1

        lower, upper = expected_move_band(spot, straddle)
        stock.set_analysis("NEUTRAL", "EXPECTED_MOVE", ExpectedMove(
            straddle=straddle, daily_move=daily_expected_move(straddle, dte),
            sigma_1d=expected_move_sigma(straddle), dte=dte,
            lower_1s=lower, upper_1s=upper,
        ))

        gap = self._strike_gap(stock)
        if not gap:
            return True
        tau = max(dte, 1) / 365.0
        strangle_pop, condor_pop = {}, {}
        for width in POP_WIDTHS:
            put_k, call_k = spot - width * gap, spot + width * gap
            strangle_pop[width] = min(
                pop_short_put(put_k, spot, iv, tau),
                pop_short_call(call_k, spot, iv, tau),
            )
            condor_pop[width] = pop_condor(put_k, call_k, spot, iv, tau)

        target = TARGET_POPS[0]
        suggestion = suggest_condor_strikes(spot, gap, iv, tau, target)
        suggested_width = None
        suggested_put = suggested_call = None
        if suggestion:
            suggested_put, suggested_call, _ = suggestion
            suggested_width = int(round((suggested_call - spot) / gap))
        stock.set_analysis("NEUTRAL", "POP_LADDER", PopLadder(
            widths=POP_WIDTHS, strangle_pop=strangle_pop, condor_pop=condor_pop,
            suggested_width=suggested_width, suggested_put=suggested_put,
            suggested_call=suggested_call, target_pop=target,
        ))
        return True
