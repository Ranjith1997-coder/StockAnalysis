"""Tests for analyser/PCRAnalyser.py."""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

import common.shared as shared
from analyser.PCRAnalyser import PCRAnalyser
from tests.analyser.conftest import make_stock, make_sensibull_ctx


# ── Context helpers ───────────────────────────────────────────────────────────

def _positional_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    return mock


def _intraday_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    return mock


# ── Stock factories ───────────────────────────────────────────────────────────

def _stock_with_pcr(pcr, expiry="2024-01-25"):
    s = make_stock()
    s.sensibull_ctx = make_sensibull_ctx(pcr=pcr, expiry=expiry)
    return s


def _stock_with_oi_history_pcr(pcr_values, expiry="2024-01-25"):
    """Stock with oi_history DataFrame — used by analyse_pcr_trend and analyse_pcr_positional_reversal."""
    s = _stock_with_pcr(pcr_values[-1], expiry=expiry)
    n = len(pcr_values)
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    s.sensibull_ctx["oi_history"] = pd.DataFrame({
        "date":         [d.isoformat() for d in dates],
        "pcr":          pcr_values,
        "call_oi":      [1_000_000] * n,
        "put_oi":       [1_000_000] * n,
        "futures_oi":   [500_000]   * n,
    })
    return s


def _stock_with_oi_chain_snapshots(pcr_values, current_pcr=None):
    """Stock with oi_chain_history snapshots — used by intraday reversal and intraday trend."""
    pcr_current = current_pcr if current_pcr is not None else pcr_values[-1]
    s = _stock_with_pcr(pcr_current)
    s.sensibull_ctx["oi_chain_history"] = [{"pcr": v} for v in pcr_values]
    return s


def _stock_with_per_expiry_pcr(near_pcr, far_pcr):
    """Stock with two-expiry per_expiry_pcr — used by analyse_pcr_divergence."""
    s = _stock_with_pcr(near_pcr)
    s.sensibull_ctx["current"]["stats"]["underlying_base_stats"]["per_expiry_pcr"] = {
        "2024-01-25": near_pcr,
        "2024-02-29": far_pcr,
    }
    return s


# ── TestAnalysePcrExtremeZones ────────────────────────────────────────────────

class TestAnalysePcrExtremeZones:
    def test_extreme_low_pcr_bullish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(0.25)
            assert a.analyse_pcr_extreme_zones(s) is True
            assert "PCR_EXTREME" in s.analysis.get("BULLISH", {})

    def test_extreme_high_pcr_bearish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(1.6)
            assert a.analyse_pcr_extreme_zones(s) is True
            assert "PCR_EXTREME" in s.analysis.get("BEARISH", {})

    def test_normal_pcr_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            assert a.analyse_pcr_extreme_zones(_stock_with_pcr(0.9)) is False

    def test_no_sensibull_data_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            s = make_stock()
            assert a.analyse_pcr_extreme_zones(s) is False

    def test_extreme_low_namedtuple_has_confirmed_field(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(0.25)
            a.analyse_pcr_extreme_zones(s)
            data = s.analysis["BULLISH"]["PCR_EXTREME"]
            assert hasattr(data, "confirmed")
            assert hasattr(data, "consecutive_prior")

    def test_no_prior_history_confirmed_false(self):
        # No oi_chain_history or oi_history → consecutive=0, confirmed=False
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(0.25)
            a.analyse_pcr_extreme_zones(s)
            data = s.analysis["BULLISH"]["PCR_EXTREME"]
            assert data.confirmed is False
            assert data.consecutive_prior == 0

    def test_prior_extreme_in_oi_history_confirmed_true(self):
        # oi_history has 2 prior extreme rows → consecutive=2, confirmed=True
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.25, 0.27, 0.28])
            s.sensibull_ctx["current"]["stats"]["underlying_base_stats"]["total_pcr"] = 0.28
            a.analyse_pcr_extreme_zones(s)
            data = s.analysis["BULLISH"]["PCR_EXTREME"]
            assert data.confirmed is True
            assert data.consecutive_prior >= 1


# ── TestAnalysePcrDirectionalBias ─────────────────────────────────────────────

class TestAnalysePcrDirectionalBias:
    def test_low_pcr_bearish_bias(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            assert a.analyse_pcr_directional_bias(_stock_with_pcr(0.4)) is True
            s = _stock_with_pcr(0.4)
            a.analyse_pcr_directional_bias(s)
            assert "PCR_BIAS" in s.analysis.get("BEARISH", {})

    def test_high_pcr_bullish_bias(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(1.3)
            a.analyse_pcr_directional_bias(s)
            assert "PCR_BIAS" in s.analysis.get("BULLISH", {})

    def test_neutral_pcr_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            assert a.analyse_pcr_directional_bias(_stock_with_pcr(0.8)) is False

    def test_strength_strong_bearish(self):
        # pcr=0.30 < PCR_BEARISH_STRONG(0.35) → STRONG
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(0.30)
            a.analyse_pcr_directional_bias(s)
            assert s.analysis["BEARISH"]["PCR_BIAS"].strength == "STRONG"

    def test_strength_moderate_bearish(self):
        # pcr=0.40 in (0.35, 0.45) → MODERATE
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(0.40)
            a.analyse_pcr_directional_bias(s)
            assert s.analysis["BEARISH"]["PCR_BIAS"].strength == "MODERATE"

    def test_strength_weak_bearish(self):
        # pcr=0.48 in (0.45, 0.5) → WEAK
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(0.48)
            a.analyse_pcr_directional_bias(s)
            assert s.analysis["BEARISH"]["PCR_BIAS"].strength == "WEAK"

    def test_trend_direction_stable_no_prev(self):
        # No prev_pcr → STABLE
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(0.40)
            a.analyse_pcr_directional_bias(s)
            assert s.analysis["BEARISH"]["PCR_BIAS"].trend_direction == "STABLE"

    def test_trend_direction_strengthening_bearish(self):
        # pcr falling for BEARISH = STRENGTHENING
        # prev from oi_history[-2], current from current snapshot
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            # oi_history: yesterday=0.46, today=0.40 → delta=-0.06 → STRENGTHENING
            s = _stock_with_oi_history_pcr([0.46, 0.40])
            s.sensibull_ctx["current"]["stats"]["underlying_base_stats"]["total_pcr"] = 0.40
            a.analyse_pcr_directional_bias(s)
            assert s.analysis["BEARISH"]["PCR_BIAS"].trend_direction == "STRENGTHENING"

    def test_trend_direction_weakening_bearish(self):
        # pcr rising for BEARISH = WEAKENING
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            # oi_history: yesterday=0.40, today=0.46 → delta=+0.06 → WEAKENING
            s = _stock_with_oi_history_pcr([0.40, 0.46])
            s.sensibull_ctx["current"]["stats"]["underlying_base_stats"]["total_pcr"] = 0.46
            a.analyse_pcr_directional_bias(s)
            assert s.analysis["BEARISH"]["PCR_BIAS"].trend_direction == "WEAKENING"

    def test_trend_direction_stable_small_delta(self):
        # delta=0.01 < sensitivity(0.02) → STABLE
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.44, 0.45])
            s.sensibull_ctx["current"]["stats"]["underlying_base_stats"]["total_pcr"] = 0.45
            a.analyse_pcr_directional_bias(s)
            assert s.analysis["BEARISH"]["PCR_BIAS"].trend_direction == "STABLE"


# ── TestAnalysePcrTrend ───────────────────────────────────────────────────────

class TestAnalysePcrTrend:
    def test_rising_trend_bullish(self):
        # 5 monotonic rising values, pct=66%, abs=0.4 — both thresholds met
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.6, 0.7, 0.8, 0.9, 1.0])
            assert a.analyse_pcr_trend(s) is True
            assert "PCR_TREND" in s.analysis.get("BULLISH", {})

    def test_falling_trend_bearish(self):
        # 5 monotonic falling values, pct=-40%, abs=-0.4 — both thresholds met
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([1.0, 0.9, 0.8, 0.7, 0.6])
            assert a.analyse_pcr_trend(s) is True
            assert "PCR_TREND" in s.analysis.get("BEARISH", {})

    def test_insufficient_rows_skip(self):
        # 4 rows < PCR_TREND_DAYS(5) → skip
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.6, 0.7, 0.8, 0.9])
            assert a.analyse_pcr_trend(s) is False

    def test_not_monotonic_returns_false(self):
        # Dips in middle → not monotonic → no signal
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.6, 0.8, 0.7, 0.9, 1.0])
            assert a.analyse_pcr_trend(s) is False

    def test_small_abs_change_returns_false(self):
        # pct=20% (meets 8%) but abs=0.04 < min_abs(0.08) → no signal
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.20, 0.21, 0.22, 0.23, 0.24])
            assert a.analyse_pcr_trend(s) is False

    def test_empty_oi_history_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(0.8)  # oi_history is empty DataFrame
            assert a.analyse_pcr_trend(s) is False

    def test_namedtuple_has_pcr_change_abs(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.6, 0.7, 0.8, 0.9, 1.0])
            a.analyse_pcr_trend(s)
            data = s.analysis["BULLISH"]["PCR_TREND"]
            assert hasattr(data, "pcr_change_abs")
            assert abs(data.pcr_change_abs - 0.4) < 0.001

    def test_namedtuple_pcr_current_is_last_value(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.6, 0.7, 0.8, 0.9, 1.0])
            a.analyse_pcr_trend(s)
            assert s.analysis["BULLISH"]["PCR_TREND"].pcr_current == pytest.approx(1.0)


# ── TestAnalysePcrPositionalReversal ──────────────────────────────────────────

class TestAnalysePcrPositionalReversal:
    def test_zone_crossover_bearish_to_bullish(self):
        # old 3-day avg BEARISH (0.35), new 3-day avg BULLISH (1.4) → BULLISH
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.3, 0.35, 0.4, 1.3, 1.4, 1.5])
            assert a.analyse_pcr_positional_reversal(s) is True
            assert "PCR_POS_REVERSAL" in s.analysis.get("BULLISH", {})

    def test_zone_crossover_bullish_to_bearish(self):
        # old 3-day avg BULLISH (1.4), new 3-day avg BEARISH (0.35) → BEARISH
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([1.3, 1.4, 1.5, 0.3, 0.35, 0.4])
            assert a.analyse_pcr_positional_reversal(s) is True
            assert "PCR_POS_REVERSAL" in s.analysis.get("BEARISH", {})

    def test_zone_crossover_type_is_zone_crossover(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.3, 0.35, 0.4, 1.3, 1.4, 1.5])
            a.analyse_pcr_positional_reversal(s)
            assert s.analysis["BULLISH"]["PCR_POS_REVERSAL"].reversal_type == "ZONE_CROSSOVER"

    def test_neutral_transition_bearish_to_neutral_rising_bullish(self):
        # old avg BEARISH (0.35), new avg NEUTRAL (0.6), rising → BULLISH
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.3, 0.35, 0.4, 0.5, 0.6, 0.7])
            assert a.analyse_pcr_positional_reversal(s) is True
            assert "PCR_POS_REVERSAL" in s.analysis.get("BULLISH", {})
            assert s.analysis["BULLISH"]["PCR_POS_REVERSAL"].reversal_type == "NEUTRAL_TRANSITION"

    def test_neutral_transition_bullish_to_neutral_falling_bearish(self):
        # old avg BULLISH (1.5), new avg NEUTRAL (0.9), falling → BEARISH
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([1.4, 1.5, 1.6, 0.8, 0.9, 1.0])
            assert a.analyse_pcr_positional_reversal(s) is True
            assert "PCR_POS_REVERSAL" in s.analysis.get("BEARISH", {})
            assert s.analysis["BEARISH"]["PCR_POS_REVERSAL"].reversal_type == "NEUTRAL_TRANSITION"

    def test_trend_reversal_falling_to_rising_bullish(self):
        # Last 4: [1.0, 0.9, 0.8, 0.9] → d1<0, d2<0, d3>0, magnitude=12.5% → BULLISH
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.5, 0.5, 1.0, 0.9, 0.8, 0.9])
            assert a.analyse_pcr_positional_reversal(s) is True
            assert "PCR_POS_REVERSAL" in s.analysis.get("BULLISH", {})
            assert s.analysis["BULLISH"]["PCR_POS_REVERSAL"].reversal_type == "TREND_REVERSAL"

    def test_trend_reversal_rising_to_falling_bearish(self):
        # Last 4: [0.8, 0.9, 1.0, 0.9] → d1>0, d2>0, d3<0, magnitude=10% → BEARISH
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.5, 0.5, 0.8, 0.9, 1.0, 0.9])
            assert a.analyse_pcr_positional_reversal(s) is True
            assert "PCR_POS_REVERSAL" in s.analysis.get("BEARISH", {})
            assert s.analysis["BEARISH"]["PCR_POS_REVERSAL"].reversal_type == "TREND_REVERSAL"

    def test_insufficient_rows_skip(self):
        # 5 rows < PCR_POS_REVERSAL_MIN_ROWS(6) → skip
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.3, 0.35, 0.4, 1.3, 1.4])
            assert a.analyse_pcr_positional_reversal(s) is False

    def test_empty_oi_history_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(0.4)
            assert a.analyse_pcr_positional_reversal(s) is False

    def test_small_trend_reversal_below_threshold_returns_false(self):
        # d3 = 0.02, magnitude = 0.02/0.8 * 100 = 2.5% < PCR_POS_REVERSAL_TREND_PCT(8%) → no signal
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.5, 0.5, 0.8, 0.9, 1.0, 1.02])
            # old avg=(0.5+0.5+0.8)/3=0.6 NEUTRAL, new avg=(0.9+1.0+1.02)/3=0.97 NEUTRAL
            # last4=[0.8,0.9,1.0,1.02]: d1>0, d2>0, d3>0 → not rising→falling
            assert a.analyse_pcr_positional_reversal(s) is False

    def test_uses_3day_averages_not_single_day(self):
        # Single spike on day 4 (1.4) would create BEARISH→BULLISH if using single day,
        # but 3-day avg of new=[0.5, 0.5, 1.4] = 0.8 → NEUTRAL, not BULLISH
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.3, 0.35, 0.4, 0.5, 0.5, 1.4])
            # old avg=0.35 BEARISH, new avg=(0.5+0.5+1.4)/3=0.8 NEUTRAL
            # → neutral_transition fires, not zone_crossover
            result = a.analyse_pcr_positional_reversal(s)
            if result:
                data = s.analysis.get("BULLISH", {}).get("PCR_POS_REVERSAL")
                if data:
                    # Should be NEUTRAL_TRANSITION, not ZONE_CROSSOVER
                    assert data.reversal_type == "NEUTRAL_TRANSITION"

    def test_namedtuple_fields(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_history_pcr([0.3, 0.35, 0.4, 1.3, 1.4, 1.5])
            a.analyse_pcr_positional_reversal(s)
            data = s.analysis["BULLISH"]["PCR_POS_REVERSAL"]
            assert hasattr(data, "reversal_type")
            assert hasattr(data, "previous_pcr")
            assert hasattr(data, "current_pcr")
            assert hasattr(data, "previous_zone")
            assert hasattr(data, "current_zone")
            assert hasattr(data, "signal")


# ── TestAnalysePcrIntradayTrend ───────────────────────────────────────────────

class TestAnalysePcrIntradayTrend:
    def test_rising_snapshots_bullish(self):
        # 5 monotonically rising snapshots, change=25% ≥ 5% → BULLISH
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.4, 0.45, 0.5, 0.55, 0.5])
            # Not monotonic — use proper rising
            s = _stock_with_oi_chain_snapshots([0.4, 0.45, 0.50, 0.55, 0.60])
            assert a.analyse_pcr_intraday_trend(s) is True
            assert "PCR_INTRADAY_TREND" in s.analysis.get("BULLISH", {})

    def test_falling_snapshots_bearish(self):
        # 5 monotonically falling snapshots → BEARISH
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.60, 0.55, 0.50, 0.45, 0.40])
            assert a.analyse_pcr_intraday_trend(s) is True
            assert "PCR_INTRADAY_TREND" in s.analysis.get("BEARISH", {})

    def test_insufficient_snapshots_skip(self):
        # 2 snapshots < PCR_INTRADAY_MIN_SNAPSHOTS(3) → skip
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.4, 0.5])
            assert a.analyse_pcr_intraday_trend(s) is False

    def test_not_monotonic_returns_false(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.4, 0.5, 0.45, 0.55, 0.6])
            assert a.analyse_pcr_intraday_trend(s) is False

    def test_change_below_threshold_returns_false(self):
        # Rising but only 2% change < PCR_INTRADAY_TREND_MIN_PCT(5%) → no signal
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.50, 0.505, 0.510, 0.515, 0.510])
            assert a.analyse_pcr_intraday_trend(s) is False

    def test_namedtuple_fields(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.4, 0.45, 0.50, 0.55, 0.60])
            a.analyse_pcr_intraday_trend(s)
            data = s.analysis["BULLISH"]["PCR_INTRADAY_TREND"]
            assert hasattr(data, "pcr_first")
            assert hasattr(data, "pcr_last")
            assert hasattr(data, "pcr_change_pct")
            assert hasattr(data, "snapshots")
            assert data.pcr_first == pytest.approx(0.4)
            assert data.pcr_last  == pytest.approx(0.60)

    def test_uses_last_5_snapshots_when_more_available(self):
        # 8 snapshots in history but window is 5 — uses latest 5
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            # First 3 are falling (would break monotonic), last 5 are rising
            s = _stock_with_oi_chain_snapshots(
                [0.9, 0.8, 0.7, 0.40, 0.45, 0.50, 0.55, 0.60]
            )
            assert a.analyse_pcr_intraday_trend(s) is True
            data = s.analysis["BULLISH"]["PCR_INTRADAY_TREND"]
            assert data.snapshots == 5
            assert data.pcr_first == pytest.approx(0.40)


# ── TestAnalysePcrDivergence ──────────────────────────────────────────────────

class TestAnalysePcrDivergence:
    def test_large_divergence_returns_true(self):
        # diff=0.6 > PCR_DIVERGENCE_THRESHOLD(0.35) → fires
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_per_expiry_pcr(near_pcr=0.3, far_pcr=0.9)
            assert a.analyse_pcr_divergence(s) is True

    def test_small_divergence_returns_false(self):
        # diff=0.05 < 0.35 → no signal
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_per_expiry_pcr(near_pcr=1.0, far_pcr=1.05)
            assert a.analyse_pcr_divergence(s) is False

    def test_single_expiry_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(1.0)
            assert a.analyse_pcr_divergence(s) is False

    def test_near_bearish_far_bullish_signals_bearish(self):
        # near<0.5 and far>0.8 → BEARISH
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_per_expiry_pcr(near_pcr=0.3, far_pcr=0.9)
            a.analyse_pcr_divergence(s)
            assert "PCR_DIVERGENCE" in s.analysis.get("BEARISH", {})

    def test_near_bullish_far_bearish_signals_bullish(self):
        # near>0.8 and far<0.5 → BULLISH
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_per_expiry_pcr(near_pcr=0.9, far_pcr=0.4)
            a.analyse_pcr_divergence(s)
            assert "PCR_DIVERGENCE" in s.analysis.get("BULLISH", {})

    def test_near_less_than_far_signals_bearish(self):
        # near < far (but not in extreme zones) → BEARISH (far-term put interest dominant)
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_per_expiry_pcr(near_pcr=0.5, far_pcr=0.9)
            a.analyse_pcr_divergence(s)
            assert "PCR_DIVERGENCE" in s.analysis.get("BEARISH", {})


# ── TestAnalysePcrReversal (intraday, uses oi_chain_history) ──────────────────

class TestAnalysePcrReversal:
    def test_zone_crossover_bearish_to_bullish(self):
        # old avg=0.325 BEARISH, new avg=1.35 BULLISH → BULLISH
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.30, 0.35, 1.30, 1.40])
            assert a.analyse_pcr_reversal(s) is True
            assert "PCR_REVERSAL" in s.analysis.get("BULLISH", {})
            assert s.analysis["BULLISH"]["PCR_REVERSAL"].reversal_type == "ZONE_CROSSOVER"

    def test_zone_crossover_bullish_to_bearish(self):
        # old avg=1.35 BULLISH, new avg=0.325 BEARISH → BEARISH
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([1.30, 1.40, 0.30, 0.35])
            assert a.analyse_pcr_reversal(s) is True
            assert "PCR_REVERSAL" in s.analysis.get("BEARISH", {})
            assert s.analysis["BEARISH"]["PCR_REVERSAL"].reversal_type == "ZONE_CROSSOVER"

    def test_neutral_transition_bearish_to_neutral_rising_bullish(self):
        # old avg=0.325 BEARISH, new avg=0.65 NEUTRAL, rising → BULLISH
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.30, 0.35, 0.60, 0.70])
            assert a.analyse_pcr_reversal(s) is True
            assert "PCR_REVERSAL" in s.analysis.get("BULLISH", {})
            assert s.analysis["BULLISH"]["PCR_REVERSAL"].reversal_type == "NEUTRAL_TRANSITION"

    def test_neutral_transition_bullish_to_neutral_falling_bearish(self):
        # old avg=1.45 BULLISH, new avg=0.85 NEUTRAL, falling → BEARISH
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([1.40, 1.50, 0.80, 0.90])
            assert a.analyse_pcr_reversal(s) is True
            assert "PCR_REVERSAL" in s.analysis.get("BEARISH", {})
            assert s.analysis["BEARISH"]["PCR_REVERSAL"].reversal_type == "NEUTRAL_TRANSITION"

    def test_trend_reversal_rising_to_falling_bearish(self):
        # [0.5, 0.6, 0.7, 0.63]: d1>0, d2>0, d3<0, magnitude=10% → BEARISH
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.50, 0.60, 0.70, 0.63])
            assert a.analyse_pcr_reversal(s) is True
            assert "PCR_REVERSAL" in s.analysis.get("BEARISH", {})
            assert s.analysis["BEARISH"]["PCR_REVERSAL"].reversal_type == "TREND_REVERSAL"

    def test_trend_reversal_falling_to_rising_bullish(self):
        # [0.7, 0.6, 0.5, 0.56]: d1<0, d2<0, d3>0, magnitude=12% → BULLISH
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.70, 0.60, 0.50, 0.56])
            assert a.analyse_pcr_reversal(s) is True
            assert "PCR_REVERSAL" in s.analysis.get("BULLISH", {})
            assert s.analysis["BULLISH"]["PCR_REVERSAL"].reversal_type == "TREND_REVERSAL"

    def test_insufficient_snapshots_skip(self):
        # 3 snapshots < PCR_REVERSAL_MIN_SNAPSHOTS(4) → skip
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.30, 0.35, 1.30])
            assert a.analyse_pcr_reversal(s) is False

    def test_no_reversal_returns_false(self):
        # Flat PCR across 4 snapshots
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.44, 0.44, 0.44, 0.44])
            assert a.analyse_pcr_reversal(s) is False

    def test_small_trend_reversal_below_pct_threshold(self):
        # d3 magnitude = 1% < PCR_REVERSAL_TREND_MIN_PCT(8%) → no signal
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain_snapshots([0.50, 0.60, 0.70, 0.693])
            assert a.analyse_pcr_reversal(s) is False


# ── TestResetConstants ────────────────────────────────────────────────────────

class TestResetConstants:
    def test_core_thresholds_same_for_both_modes(self):
        mock_i = MagicMock(); mock_i.mode = shared.Mode.INTRADAY
        mock_p = MagicMock(); mock_p.mode = shared.Mode.POSITIONAL
        with patch("common.shared.app_ctx", mock_i):
            a = PCRAnalyser(); a.reset_constants()
            intra = PCRAnalyser.PCR_BULLISH_THRESHOLD
        with patch("common.shared.app_ctx", mock_p):
            a.reset_constants()
            pos = PCRAnalyser.PCR_BULLISH_THRESHOLD
        assert intra == pos == 1.2

    def test_positional_reversal_constants_set_in_positional_mode(self):
        mock = MagicMock(); mock.mode = shared.Mode.POSITIONAL
        with patch("common.shared.app_ctx", mock):
            a = PCRAnalyser(); a.reset_constants()
            assert PCRAnalyser.PCR_POS_REVERSAL_MIN_ROWS  == 6
            assert PCRAnalyser.PCR_POS_REVERSAL_TREND_PCT == 8.0

    def test_intraday_reversal_constants_set_in_intraday_mode(self):
        mock = MagicMock(); mock.mode = shared.Mode.INTRADAY
        with patch("common.shared.app_ctx", mock):
            a = PCRAnalyser(); a.reset_constants()
            assert PCRAnalyser.PCR_REVERSAL_MIN_SNAPSHOTS == 4
            assert PCRAnalyser.PCR_REVERSAL_TREND_MIN_PCT == 8.0

    def test_intraday_trend_constants_set_in_intraday_mode(self):
        mock = MagicMock(); mock.mode = shared.Mode.INTRADAY
        with patch("common.shared.app_ctx", mock):
            a = PCRAnalyser(); a.reset_constants()
            assert PCRAnalyser.PCR_INTRADAY_MIN_SNAPSHOTS == 3
            assert PCRAnalyser.PCR_INTRADAY_TREND_MIN_PCT == 5.0

    def test_divergence_threshold_set(self):
        mock = MagicMock(); mock.mode = shared.Mode.POSITIONAL
        with patch("common.shared.app_ctx", mock):
            a = PCRAnalyser(); a.reset_constants()
            assert PCRAnalyser.PCR_DIVERGENCE_THRESHOLD == 0.35
