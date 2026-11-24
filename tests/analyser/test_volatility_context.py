"""Tests for the Block 2 volatility-context analysers."""
from __future__ import annotations

import pandas as pd
import pytest

import common.shared as shared
from services.analysis_engine.analyser.VolatilityContextAnalyser import (
    VolatilityContextAnalyser,
    iv_zone,
    vix_regime,
)
from tests.analyser.conftest import make_stock

SPOT = 25_000.0
GAP = 50.0


def _stock(expiries: dict[str, tuple[float, int, float | None]],
           *, straddle: float = 120.0, iv_history=None, spot: float = SPOT):
    """expiries: {iso: (atm_iv, days_to_expiry, percentile)}."""
    s = make_stock()
    s.ltp = spot
    s.sensibull_ctx = {
        "current": {"stats": {"per_expiry_map": {
            key: {
                "atm_iv": iv,
                "atm_iv_percentile": pct,
                "days_to_expiry": dte,
                "atm_strike": spot,
            }
            for key, (iv, dte, pct) in expiries.items()
        }}},
        "iv_chart_history": iv_history if iv_history is not None else pd.DataFrame(),
        "historical_data": pd.DataFrame(),
        "oi_chain_history": [],
        "oi_history": pd.DataFrame(),
    }
    s._tick_store.options_aggregate["atm_straddle_premium"] = straddle
    for step in range(-4, 5):
        strike = spot + step * GAP
        for side in ("CE", "PE"):
            s._tick_store.update_option_tick(
                strike, side, {"last_price": 50.0, "oi": 10_000, "iv": 13.0}
            )
    return s


def _history(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"iv_close": values})


TWO = {"2026-11-26": (13.0, 2, 55.0), "2026-12-03": (14.0, 9, 60.0)}


class TestZones:
    @pytest.mark.parametrize("ivp,zone", [
        (5, "VERY_LOW"), (15, "LOW"), (50, "NORMAL"), (75, "HIGH"), (90, "VERY_HIGH"),
    ])
    def test_iv_zone_boundaries(self, ivp, zone):
        assert iv_zone(ivp) == zone

    @pytest.mark.parametrize("vix,regime", [
        (12.9, "CALM"), (13.0, "NORMAL"), (17.9, "NORMAL"),
        (18.0, "ELEVATED"), (25.0, "PANIC"), (None, "UNKNOWN"),
    ])
    def test_vix_regime_boundaries(self, vix, regime):
        assert vix_regime(vix) == regime


class TestIvContext:
    def test_local_percentile_is_preferred(self, monkeypatch):
        monkeypatch.setattr(shared.app_ctx, "india_vix_ltp", 14.0, raising=False)
        history = _history([10 + i * 0.1 for i in range(100)])   # 10.0 .. 19.9
        s = _stock({"2026-11-26": (15.0, 2, 90.0)}, iv_history=history)
        a = VolatilityContextAnalyser()

        assert a.analyse_iv_context(s) is True
        context = s.analysis["NEUTRAL"]["IV_CONTEXT"]
        assert context.ivp_source == "local"
        assert context.ivp == pytest.approx(51.0)
        assert context.zone == "NORMAL"
        assert context.vix == 14.0
        assert context.iv_minus_vix == pytest.approx(1.0)
        assert s.analysis["NEUTRAL"]["VIX_REGIME"].regime == "NORMAL"

    def test_vendor_percentile_is_the_fallback(self, monkeypatch):
        monkeypatch.setattr(shared.app_ctx, "india_vix_ltp", 20.0, raising=False)
        s = _stock({"2026-11-26": (24.0, 2, 82.0)})
        a = VolatilityContextAnalyser()

        assert a.analyse_iv_context(s) is True
        context = s.analysis["NEUTRAL"]["IV_CONTEXT"]
        assert context.ivp_source == "vendor"
        assert context.zone == "HIGH"
        assert s.analysis["NEUTRAL"]["VIX_REGIME"].iv_profile == "RICH"
        assert context.vix == 20.0

    def test_no_percentile_still_reports_the_vix_regime(self, monkeypatch):
        monkeypatch.setattr(shared.app_ctx, "india_vix_ltp", 30.0, raising=False)
        s = _stock({"2026-11-26": (22.0, 2, None)})
        a = VolatilityContextAnalyser()

        assert a.analyse_iv_context(s) is True
        assert "IV_CONTEXT" not in s.analysis["NEUTRAL"]
        assert s.analysis["NEUTRAL"]["VIX_REGIME"].regime == "PANIC"

    def test_missing_sensibull_is_skipped(self):
        s = make_stock()
        s.sensibull_ctx = {}
        assert VolatilityContextAnalyser().analyse_iv_context(s) is False


class TestTermStructure:
    def test_contango(self):
        s = _stock(TWO)
        assert VolatilityContextAnalyser().analyse_term_structure(s) is True
        term = s.analysis["NEUTRAL"]["TERM_STRUCTURE"]
        assert term.regime == "CONTANGO"
        assert term.slope == pytest.approx(1.0)
        assert term.slope_pct == pytest.approx(100 / 13, rel=1e-6)
        assert term.front_dte == 2

    def test_backwardation_and_event_week(self):
        s = _stock({"2026-11-26": (16.0, 2, 55.0), "2026-12-03": (13.5, 9, 55.0)})
        VolatilityContextAnalyser().analyse_term_structure(s)
        term = s.analysis["NEUTRAL"]["TERM_STRUCTURE"]
        assert term.regime == "BACKWARDATION"
        assert term.event_week is True

    def test_flat_inside_the_dead_band(self):
        s = _stock({"2026-11-26": (14.0, 3, 55.0), "2026-12-03": (14.1, 10, 55.0)})
        VolatilityContextAnalyser().analyse_term_structure(s)
        assert s.analysis["NEUTRAL"]["TERM_STRUCTURE"].regime == "FLAT"

    def test_single_expiry_is_skipped(self):
        s = _stock({"2026-11-26": (13.0, 2, 55.0)})
        assert VolatilityContextAnalyser().analyse_term_structure(s) is False


class TestPlacement:
    def test_expected_move_values(self):
        s = _stock(TWO, straddle=120.0)
        assert VolatilityContextAnalyser().analyse_placement(s) is True
        move = s.analysis["NEUTRAL"]["EXPECTED_MOVE"]
        assert move.daily_move == pytest.approx(120 / 2 ** 0.5, rel=1e-9)
        assert move.sigma_1d == pytest.approx(150.0)
        assert move.lower_1s == pytest.approx(SPOT - 150.0)
        assert move.upper_1s == pytest.approx(SPOT + 150.0)

    def test_pop_ladder_is_monotone_and_suggests_a_width(self):
        s = _stock(TWO, straddle=120.0)
        VolatilityContextAnalyser().analyse_placement(s)
        ladder = s.analysis["NEUTRAL"]["POP_LADDER"]

        pops = [ladder.condor_pop[w] for w in ladder.widths]
        assert pops == sorted(pops)
        assert all(0 <= p <= 1 for p in pops)
        assert ladder.suggested_width is not None and ladder.suggested_width >= 1
        assert ladder.suggested_put < SPOT < ladder.suggested_call
        # the suggested width must independently meet the target
        from lib.fno.option_math import pop_condor

        tau = 2 / 365.0
        achieved = pop_condor(ladder.suggested_put, ladder.suggested_call,
                              SPOT, 0.13, tau)
        assert achieved >= ladder.target_pop

    def test_missing_straddle_is_skipped(self):
        s = _stock(TWO, straddle=0.0)
        assert VolatilityContextAnalyser().analyse_placement(s) is False
