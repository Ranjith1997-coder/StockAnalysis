"""Tests for analyser/GEXAnalyser.py."""
import pytest
from unittest.mock import patch, MagicMock

import common.shared as shared
from analyser.GEXAnalyser import GEXAnalyser
from tests.analyser.conftest import make_stock, patch_ctx


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_options_live(spot=24000.0, ce_gamma=0.002, pe_gamma=0.002,
                       ce_oi=100_000, pe_oi=80_000, strikes=None):
    """
    Build a minimal options_live dict.
    Default: 5 strikes centred on spot, CE gamma slightly > PE gamma
    (positive GEX = dealers long gamma = market pins).
    """
    if strikes is not None:
        return strikes

    step = 100  # NIFTY-style 100pt strikes
    base = int(spot / step) * step
    result = {}
    for i in range(-2, 3):
        s = float(base + i * step)
        result[s] = {
            "CE": {"gamma": ce_gamma, "oi": ce_oi, "ltp": 100.0},
            "PE": {"gamma": pe_gamma, "oi": pe_oi, "ltp": 80.0},
        }
    return result


def _stock_with_gamma(symbol="NIFTY", spot=24000.0,
                      ce_gamma=0.002, pe_gamma=0.002,
                      ce_oi=100_000, pe_oi=80_000,
                      prev_regime=None, strikes=None):
    s = make_stock(symbol=symbol)
    s.ltp = spot
    s._tick_store.options_live = _make_options_live(spot, ce_gamma, pe_gamma, ce_oi, pe_oi, strikes)
    if prev_regime:
        s.options_aggregate["gex_regime"] = prev_regime
    return s


# ── Gate tests ─────────────────────────────────────────────────────────────────

class TestGates:
    def test_non_index_symbol_skipped(self, intraday_ctx):
        a = GEXAnalyser()
        a.reset_constants()
        s = make_stock(symbol="RELIANCE")
        s._tick_store.options_live = _make_options_live()
        assert a.analyse_gex_regime(s) is False

    def test_no_options_live_skipped(self, intraday_ctx):
        a = GEXAnalyser()
        a.reset_constants()
        s = make_stock(symbol="NIFTY")
        s._tick_store.options_live = {}
        assert a.analyse_gex_regime(s) is False

    def test_all_zero_gamma_skipped(self, intraday_ctx):
        a = GEXAnalyser()
        a.reset_constants()
        s = make_stock(symbol="NIFTY")
        s._tick_store.options_live = {
            24000.0: {"CE": {"gamma": 0.0, "oi": 50000}, "PE": {"gamma": 0.0, "oi": 50000}}
        }
        assert a.analyse_gex_regime(s) is False


# ── GEX_REGIME ─────────────────────────────────────────────────────────────────

class TestGexRegime:
    def test_positive_regime_when_ce_dominates(self, intraday_ctx):
        """CE gamma × OI > PE gamma × OI → positive GEX → dealers long gamma."""
        a = GEXAnalyser()
        a.reset_constants()
        # CE: gamma=0.003, OI=100k  →  CE contribution >> PE contribution
        s = _stock_with_gamma("NIFTY", spot=24000.0, ce_gamma=0.003, pe_gamma=0.001,
                               ce_oi=100_000, pe_oi=100_000)
        result = a.analyse_gex_regime(s)
        assert result is True
        regime_data = s.analysis["NEUTRAL"]["GEX_REGIME"]
        assert regime_data.regime == "POSITIVE"
        assert regime_data.gex_total > 0

    def test_negative_regime_when_pe_dominates(self, intraday_ctx):
        """PE gamma × OI > CE gamma × OI → negative GEX → dealers short gamma."""
        a = GEXAnalyser()
        a.reset_constants()
        s = _stock_with_gamma("NIFTY", spot=24000.0, ce_gamma=0.001, pe_gamma=0.003,
                               ce_oi=100_000, pe_oi=100_000)
        a.analyse_gex_regime(s)
        regime_data = s.analysis["NEUTRAL"]["GEX_REGIME"]
        assert regime_data.regime == "NEGATIVE"
        assert regime_data.gex_total < 0

    def test_regime_flip_detected(self, intraday_ctx):
        """When prev_regime=POSITIVE and new regime=NEGATIVE, regime_flipped=True."""
        a = GEXAnalyser()
        a.reset_constants()
        s = _stock_with_gamma("NIFTY", spot=24000.0, ce_gamma=0.001, pe_gamma=0.003,
                               ce_oi=100_000, pe_oi=100_000, prev_regime="POSITIVE")
        a.analyse_gex_regime(s)
        regime_data = s.analysis["NEUTRAL"]["GEX_REGIME"]
        assert regime_data.regime_flipped is True
        assert regime_data.prev_regime == "POSITIVE"

    def test_no_flip_when_regime_unchanged(self, intraday_ctx):
        a = GEXAnalyser()
        a.reset_constants()
        s = _stock_with_gamma("NIFTY", spot=24000.0, ce_gamma=0.003, pe_gamma=0.001,
                               ce_oi=100_000, pe_oi=100_000, prev_regime="POSITIVE")
        a.analyse_gex_regime(s)
        regime_data = s.analysis["NEUTRAL"]["GEX_REGIME"]
        assert regime_data.regime_flipped is False

    def test_options_aggregate_updated(self, intraday_ctx):
        """analyse_gex_regime must persist results to options_aggregate."""
        a = GEXAnalyser()
        a.reset_constants()
        s = _stock_with_gamma("NIFTY", spot=24000.0, ce_gamma=0.003, pe_gamma=0.001,
                               ce_oi=100_000, pe_oi=100_000)
        a.analyse_gex_regime(s)
        agg = s.options_aggregate
        assert agg["gex_total"] != 0.0
        assert agg["gex_regime"] in ("POSITIVE", "NEGATIVE")
        assert isinstance(agg["gex_by_strike"], dict)
        assert len(agg["gex_by_strike"]) > 0

    def test_magnitude_mild(self, intraday_ctx):
        """Very small gamma → MILD magnitude."""
        a = GEXAnalyser()
        a.reset_constants()
        # Tiny gamma → tiny GEX total < 500 Cr
        s = _stock_with_gamma("NIFTY", spot=24000.0, ce_gamma=0.00001, pe_gamma=0.000005,
                               ce_oi=1000, pe_oi=1000)
        a.analyse_gex_regime(s)
        regime_data = s.analysis["NEUTRAL"]["GEX_REGIME"]
        assert regime_data.magnitude == "MILD"

    def test_higher_oi_produces_higher_gex(self, intraday_ctx):
        """GEX scales with OI (absolute shares), not lot_size."""
        a = GEXAnalyser()
        a.reset_constants()

        def _run(symbol, ce_oi, pe_oi):
            s = _stock_with_gamma(symbol, spot=52000.0, ce_gamma=0.002, pe_gamma=0.001,
                                   ce_oi=ce_oi, pe_oi=pe_oi)
            a.analyse_gex_regime(s)
            return s.options_aggregate["gex_total"]

        low_oi_gex  = _run("NIFTY", 100_000, 100_000)
        high_oi_gex = _run("NIFTY", 500_000, 100_000)
        assert high_oi_gex > low_oi_gex


# ── GEX_FLIP_PROXIMITY ─────────────────────────────────────────────────────────

class TestGexFlipProximity:
    def _stock_with_flip_level(self, flip_level, spot, gex_total=1500.0):
        s = make_stock(symbol="NIFTY")
        s.ltp = spot
        s._tick_store.options_live = _make_options_live(spot=spot, ce_gamma=0.002, pe_gamma=0.001)
        s.options_aggregate["gex_flip_level"] = flip_level
        s.options_aggregate["gex_total"]      = gex_total
        return s

    def test_fires_when_spot_within_threshold(self, intraday_ctx):
        a = GEXAnalyser()
        a.reset_constants()
        # spot = 24000, flip = 24080 → distance = 0.33% < 0.4% threshold
        s = self._stock_with_flip_level(flip_level=24080.0, spot=24000.0)
        result = a.analyse_gex_flip_proximity(s)
        assert result is True
        data = s.analysis["NEUTRAL"]["GEX_FLIP_PROXIMITY"]
        assert data.approaching_from == "BELOW"
        assert data.distance_pct < 0.4

    def test_does_not_fire_when_far(self, intraday_ctx):
        a = GEXAnalyser()
        a.reset_constants()
        # spot = 24000, flip = 24500 → distance = 2.08% > 0.4%
        s = self._stock_with_flip_level(flip_level=24500.0, spot=24000.0)
        result = a.analyse_gex_flip_proximity(s)
        assert result is False

    def test_approaching_from_above(self, intraday_ctx):
        a = GEXAnalyser()
        a.reset_constants()
        # spot above flip level
        s = self._stock_with_flip_level(flip_level=24000.0, spot=24080.0)
        a.analyse_gex_flip_proximity(s)
        data = s.analysis["NEUTRAL"]["GEX_FLIP_PROXIMITY"]
        assert data.approaching_from == "ABOVE"

    def test_skipped_when_gex_below_noise_floor(self, intraday_ctx):
        a = GEXAnalyser()
        a.reset_constants()
        # Small GEX → below noise floor → skip
        s = self._stock_with_flip_level(flip_level=24050.0, spot=24000.0, gex_total=50.0)
        result = a.analyse_gex_flip_proximity(s)
        assert result is False

    def test_wider_threshold_in_positional_mode(self, positional_ctx):
        """Positional mode uses 0.6% threshold — should fire at 0.5% distance."""
        a = GEXAnalyser()
        a.reset_constants()
        # 0.5% distance: spot=24000, flip=24120 → 0.5% → fires at 0.6, not at 0.4
        s = self._stock_with_flip_level(flip_level=24120.0, spot=24000.0, gex_total=1500.0)
        result = a.analyse_gex_flip_proximity(s)
        assert result is True


# ── GEX_WALL ───────────────────────────────────────────────────────────────────

class TestGexWall:
    def _stock_with_gex_by_strike(self, spot=24000.0, gex_by_strike=None):
        s = make_stock(symbol="NIFTY")
        s.ltp = spot
        # Need options_live with gamma so _is_applicable passes
        s._tick_store.options_live = _make_options_live(spot=spot)
        if gex_by_strike:
            s.options_aggregate["gex_by_strike"] = gex_by_strike
        return s

    def test_call_wall_detected_in_bearish_bucket(self, intraday_ctx):
        """A spike in CE GEX at one strike → call wall → BEARISH bucket."""
        a = GEXAnalyser()
        a.reset_constants()
        # Normal GEX ~1.0 at most strikes, huge spike at 24200
        gex = {23800.0: 1.0, 23900.0: 1.2, 24000.0: 1.1,
               24100.0: 1.0, 24200.0: 600.0}  # spike
        s = self._stock_with_gex_by_strike(spot=24000.0, gex_by_strike=gex)
        result = a.analyse_gex_wall(s)
        assert result is True
        assert "GEX_WALL" in s.analysis.get("BEARISH", {})

    def test_put_wall_detected_in_bullish_bucket(self, intraday_ctx):
        """A negative GEX spike (PE dominates) → put wall → BULLISH bucket."""
        a = GEXAnalyser()
        a.reset_constants()
        gex = {23800.0: -600.0, 23900.0: -1.0, 24000.0: -1.0,
               24100.0: -1.0, 24200.0: -1.0}  # spike on downside
        s = self._stock_with_gex_by_strike(spot=24000.0, gex_by_strike=gex)
        result = a.analyse_gex_wall(s)
        assert result is True
        assert "GEX_WALL" in s.analysis.get("BULLISH", {})

    def test_no_wall_when_distribution_is_flat(self, intraday_ctx):
        """When all strikes have similar GEX, no wall should fire."""
        a = GEXAnalyser()
        a.reset_constants()
        gex = {float(s): 100.0 for s in range(23800, 24300, 100)}
        s = self._stock_with_gex_by_strike(spot=24000.0, gex_by_strike=gex)
        result = a.analyse_gex_wall(s)
        assert result is False

    def test_wall_outside_5pct_ignored(self, intraday_ctx):
        """Spikes beyond ±5% of spot are not counted as walls."""
        a = GEXAnalyser()
        a.reset_constants()
        # Spike at 20000 — far from spot 24000 (>16% away)
        gex = {23800.0: 1.0, 23900.0: 1.0, 24000.0: 1.0,
               24100.0: 1.0, 20000.0: 999.0}
        s = self._stock_with_gex_by_strike(spot=24000.0, gex_by_strike=gex)
        result = a.analyse_gex_wall(s)
        assert result is False


# ── GEX_WALL_BREACH ────────────────────────────────────────────────────────────

class TestGexWallBreach:
    def test_call_wall_breach_emits_bullish(self, intraday_ctx):
        """Spot crossed a call wall (CE dominated) AND GEX dropped ≥30%."""
        a = GEXAnalyser()
        a.reset_constants()
        s = make_stock(symbol="NIFTY")
        spot = 24300.0
        s.ltp = spot
        s._tick_store.options_live = _make_options_live(spot=spot)

        # Previous cycle: call wall at 24200 (CE dominated, high GEX)
        prev_gex = {23800.0: 1.0, 23900.0: 1.2, 24000.0: 1.1,
                    24100.0: 1.3, 24200.0: 500.0}
        # Current cycle: spot crossed 24200, GEX at 24200 dropped 60%
        curr_gex = {23800.0: 1.0, 23900.0: 1.2, 24000.0: 1.1,
                    24100.0: 1.3, 24200.0: 200.0}
        s.options_aggregate["gex_by_strike"] = curr_gex

        # Seed previous cycle state into analyser
        a._prev_gex_by_strike = {"NIFTY": prev_gex}

        result = a.analyse_gex_wall_breach(s)
        assert result is True
        assert "GEX_WALL_BREACH" in s.analysis.get("BULLISH", {})
        breach = s.analysis["BULLISH"]["GEX_WALL_BREACH"]
        assert breach.breach_side == "CALL"
        assert breach.gex_drop_pct >= 30.0

    def test_no_breach_when_gex_held(self, intraday_ctx):
        """If GEX at the wall strike didn't drop, dealers still defending — no breach."""
        a = GEXAnalyser()
        a.reset_constants()
        s = make_stock(symbol="NIFTY")
        s._ltp = 24250.0
        s._tick_store.options_live = _make_options_live(spot=24250.0)

        prev_gex = {24200.0: 500.0}
        curr_gex = {24200.0: 490.0}  # only 2% drop — not a breach
        s.options_aggregate["gex_by_strike"] = curr_gex
        a._prev_gex_by_strike = {"NIFTY": prev_gex}

        result = a.analyse_gex_wall_breach(s)
        assert result is False

    def test_no_breach_on_first_cycle(self, intraday_ctx):
        """First run has no previous cycle data — should return False silently."""
        a = GEXAnalyser()
        a.reset_constants()
        s = make_stock(symbol="NIFTY")
        s._ltp = 24000.0
        s._tick_store.options_live = _make_options_live()
        s.options_aggregate["gex_by_strike"] = {24000.0: 100.0}
        # No _prev_gex_by_strike set
        result = a.analyse_gex_wall_breach(s)
        assert result is False

    def test_wall_breach_not_available_in_positional(self, positional_ctx):
        """GEX_WALL_BREACH is intraday-only — positional run should not fire it."""
        a = GEXAnalyser()
        a.reset_constants()
        s = make_stock(symbol="NIFTY")
        s._ltp = 24250.0
        s._tick_store.options_live = _make_options_live(spot=24250.0)
        s.options_aggregate["gex_by_strike"] = {24200.0: 200.0}
        a._prev_gex_by_strike = {"NIFTY": {24200.0: 500.0}}
        # Positional methods don't include analyse_gex_wall_breach
        assert not any(m.__name__ == "analyse_gex_wall_breach"
                       for m in a._positional_methods)


# ── GEX_IMBALANCE ──────────────────────────────────────────────────────────────

class TestGexImbalance:
    def _stock_with_gex_sides(self, gex_ce, gex_pe, symbol="NIFTY"):
        s = make_stock(symbol=symbol)
        s._ltp = 24000.0
        s._tick_store.options_live = _make_options_live()
        s.options_aggregate["gex_ce"] = gex_ce
        s.options_aggregate["gex_pe"] = gex_pe
        return s

    def test_ce_dominance_emits_bearish(self, intraday_ctx):
        """CE GEX 3× PE GEX → CE dominant → BEARISH bucket."""
        a = GEXAnalyser()
        a.reset_constants()
        s = self._stock_with_gex_sides(gex_ce=600.0, gex_pe=200.0)
        result = a.analyse_gex_imbalance(s)
        assert result is True
        assert "GEX_IMBALANCE" in s.analysis.get("BEARISH", {})
        data = s.analysis["BEARISH"]["GEX_IMBALANCE"]
        assert data.dominant_side == "CE"
        assert data.imbalance_ratio == pytest.approx(3.0, rel=0.01)

    def test_pe_dominance_emits_bullish(self, intraday_ctx):
        """PE GEX 4× CE GEX → PE dominant → BULLISH bucket."""
        a = GEXAnalyser()
        a.reset_constants()
        s = self._stock_with_gex_sides(gex_ce=150.0, gex_pe=600.0)
        result = a.analyse_gex_imbalance(s)
        assert result is True
        assert "GEX_IMBALANCE" in s.analysis.get("BULLISH", {})
        data = s.analysis["BULLISH"]["GEX_IMBALANCE"]
        assert data.dominant_side == "PE"

    def test_balanced_gex_no_signal(self, intraday_ctx):
        """Equal CE and PE GEX → no imbalance → False."""
        a = GEXAnalyser()
        a.reset_constants()
        s = self._stock_with_gex_sides(gex_ce=300.0, gex_pe=300.0)
        result = a.analyse_gex_imbalance(s)
        assert result is False

    def test_magnitude_extreme(self, intraday_ctx):
        """CE/PE ratio > 6 → EXTREME magnitude."""
        a = GEXAnalyser()
        a.reset_constants()
        s = self._stock_with_gex_sides(gex_ce=700.0, gex_pe=100.0)
        a.analyse_gex_imbalance(s)
        data = s.analysis["BEARISH"]["GEX_IMBALANCE"]
        assert data.magnitude == "EXTREME"

    def test_magnitude_strong(self, intraday_ctx):
        """Ratio between 4–6 → STRONG."""
        a = GEXAnalyser()
        a.reset_constants()
        s = self._stock_with_gex_sides(gex_ce=500.0, gex_pe=100.0)
        a.analyse_gex_imbalance(s)
        data = s.analysis["BEARISH"]["GEX_IMBALANCE"]
        assert data.magnitude == "STRONG"

    def test_skipped_when_one_side_below_noise_floor(self, intraday_ctx):
        """If either side is below IMBALANCE_MIN_SIDE_CR, skip to avoid noise."""
        a = GEXAnalyser()
        a.reset_constants()
        s = self._stock_with_gex_sides(gex_ce=500.0, gex_pe=5.0)  # pe too small
        result = a.analyse_gex_imbalance(s)
        assert result is False


# ── reset_constants ────────────────────────────────────────────────────────────

class TestResetConstants:
    def test_intraday_thresholds(self, intraday_ctx):
        a = GEXAnalyser()
        a.reset_constants()
        assert GEXAnalyser.FLIP_PROXIMITY_THRESHOLD_PCT == 0.4
        assert GEXAnalyser.WALL_SIGMA == 1.5
        assert GEXAnalyser.WALL_MIN_GEX_CR == 100

    def test_positional_thresholds(self, positional_ctx):
        a = GEXAnalyser()
        a.reset_constants()
        assert GEXAnalyser.FLIP_PROXIMITY_THRESHOLD_PCT == 0.6
        assert GEXAnalyser.WALL_SIGMA == 1.8
        assert GEXAnalyser.WALL_MIN_GEX_CR == 200
