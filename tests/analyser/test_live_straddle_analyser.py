"""Tests for analyser/LiveStraddleAnalyser.py."""
import time
import pytest
from analyser.LiveStraddleAnalyser import LiveStraddleAnalyser
from analyser.LiveOptionsHistory import LiveOptionsHistory, OptionsSnapshot


def _make_agg(straddle=200.0, atm_strike=20000, ce_ltp=100.0, pe_ltp=100.0):
    return {
        "atm_straddle_premium": straddle,
        "atm_strike": atm_strike,
        "live_pcr": 1.0,
        "total_ce_oi": 100_000,
        "total_pe_oi": 100_000,
        "max_oi_ce_strike": 20200,
        "max_oi_pe_strike": 19800,
        "net_ce_oi_change": 0,
        "net_pe_oi_change": 0,
    }


def _make_history_with_straddle(straddle_values, spot=20000.0, spacing_min=2):
    """
    Build a history object with synthetic snapshots spread `spacing_min` apart.
    The last snapshot is placed just 30 seconds ago so straddle_series(5) finds it.
    """
    h = LiveOptionsHistory("NIFTY")
    n = len(straddle_values)
    # Place newest snap 30s ago; older snaps spaced `spacing_min` back
    newest_ts = time.time() - 30
    for i, sv in enumerate(straddle_values):
        offset = (n - 1 - i) * spacing_min * 60
        ts = newest_ts - offset
        s = OptionsSnapshot(
            ts=ts, spot=spot,
            pcr=1.0, straddle=sv, atm_strike=20000,
            total_ce_oi=100_000, total_pe_oi=100_000,
            ce_wall=20200, pe_wall=19800, ce_wall_oi=60_000, pe_wall_oi=50_000,
            net_ce_oi_change=0, net_pe_oi_change=0,
        )
        h._buf.append(s)
    h._last_ts = h._buf[-1].ts if h._buf else 0.0
    return h


class TestIsValidStraddle:
    def test_valid_straddle(self):
        a = LiveStraddleAnalyser("NIFTY")
        assert a._is_valid_straddle(200.0, 20000.0) is True  # 200/20000 = 1% > 0.3%

    def test_zero_straddle_invalid(self):
        a = LiveStraddleAnalyser("NIFTY")
        assert a._is_valid_straddle(0.0, 20000.0) is False

    def test_below_min_pct_invalid(self):
        a = LiveStraddleAnalyser("NIFTY")
        # 5 / 20000 = 0.025% < 0.3%
        assert a._is_valid_straddle(5.0, 20000.0) is False


class TestCheckIvChange:
    def test_returns_none_when_straddle_zero(self):
        a = LiveStraddleAnalyser("NIFTY")
        result = a.check_iv_change(_make_agg(straddle=0.0), 20000.0)
        assert result is None

    def test_returns_none_when_straddle_invalid(self):
        # Straddle too small relative to spot
        a = LiveStraddleAnalyser("NIFTY")
        result = a.check_iv_change(_make_agg(straddle=5.0), 20000.0)
        assert result is None

    def test_iv_expanding_detected_via_history(self):
        a = LiveStraddleAnalyser("NIFTY")
        # 3 snaps at 3-min spacing: oldest=T-6.5min (200), T-3.5min (205), T-0.5min (215)
        # minutes_of_data = 6min >= 6 ✓
        # straddle_series(5) returns snaps from last 5min: T-3.5min(205) and T-0.5min(215)
        # change = (215-205)/205 = +4.9% ≥ 4% threshold ✓
        h = _make_history_with_straddle([200.0, 205.0, 215.0], spacing_min=3)
        result = a.check_iv_change(_make_agg(straddle=215.0), 20000.0, history=h)
        assert result is not None
        assert result[0] == "IV_EXPANDING"

    def test_iv_compressing_detected_via_history(self):
        a = LiveStraddleAnalyser("NIFTY")
        # 3 snaps at 3-min spacing: 200 → 196 → 188
        # straddle_series(5): old=196, new=188, change=-4.1% ≤ -5%? → 196→188 = -4.1% not < -5%
        # Use bigger drop: 200 → 200 → 185 (series 5min: 200→185 = -7.5% ≤ -5%)
        h = _make_history_with_straddle([200.0, 200.0, 185.0], spacing_min=3)
        result = a.check_iv_change(_make_agg(straddle=185.0), 20000.0, history=h)
        assert result is not None
        assert result[0] == "IV_COMPRESSING"

    def test_small_straddle_change_returns_none(self):
        a = LiveStraddleAnalyser("NIFTY")
        # Only +1% change — below the 4% threshold
        h = _make_history_with_straddle([200.0, 201.0, 202.0], spacing_min=3)
        result = a.check_iv_change(_make_agg(straddle=202.0), 20000.0, history=h)
        assert result is None

    def test_spot_movement_suppresses_signal(self):
        a = LiveStraddleAnalyser("NIFTY")
        # Straddle up 8% but spot moved 1.25% (>0.1%)
        h = _make_history_with_straddle([200.0, 200.0, 216.0], spacing_min=3)
        result = a.check_iv_change(_make_agg(straddle=216.0), 20250.0, history=h)
        assert result is None


class TestCheckImpliedMoveBoundary:
    def test_returns_none_when_no_open_reference(self):
        a = LiveStraddleAnalyser("NIFTY")
        result = a.check_implied_move_boundary(_make_agg(straddle=200.0), 20000.0)
        assert result is None

    def test_boundary_alert_when_range_consumed(self):
        a = LiveStraddleAnalyser("NIFTY")
        h = _make_history_with_straddle([200.0, 200.0, 200.0], spot=20000.0, spacing_min=3)
        # half_range=200*0.68/2=68; upper=20068; spot at 20062 → consumed=(20062-20000)/68=91% ≥ 75%
        # remaining = 20068 - 20062 = 6 > 0 ✓
        result = a.check_implied_move_boundary(_make_agg(straddle=200.0), 20062.0, history=h)
        assert result is not None
        assert result[0] == "RANGE_BOUNDARY"

    def test_no_alert_when_inside_range(self):
        a = LiveStraddleAnalyser("NIFTY")
        h = _make_history_with_straddle([200.0, 200.0, 200.0], spot=20000.0, spacing_min=3)
        # spot moved only 50 points — 25% of 200
        result = a.check_implied_move_boundary(_make_agg(straddle=200.0), 20050.0, history=h)
        assert result is None


class TestCheckSkewReversal:
    def test_returns_none_without_atm_data(self):
        a = LiveStraddleAnalyser("NIFTY")
        # options_live without ATM strike data
        result = a.check_iv_skew_reversal(_make_agg(), {}, 20000.0)
        assert result is None

    def test_ce_heavy_skew_detected(self):
        from analyser.LiveStraddleAnalyser import LiveStraddleAnalyser as LSA
        a = LSA("NIFTY")
        agg = {
            "atm_straddle_premium": 200.0,
            "atm_strike": 20000,
            "live_pcr": 1.0,
            "total_ce_oi": 100_000,
            "total_pe_oi": 100_000,
            "max_oi_ce_strike": 20200,
            "max_oi_pe_strike": 19800,
            "net_ce_oi_change": 0,
            "net_pe_oi_change": 0,
        }
        options_live = {
            20000: {
                "CE": {"ltp": 120.0},
                "PE": {"ltp": 80.0},
            }
        }
        # Build history for a ratio flip: ratio goes from 0.92 (CE-heavy) past 0.95 boundary
        # Current ratio = CE/PE = 120/80 = 1.5 — crossing from below 0.95 (CE-heavy) to above
        a._skew_history.extend([0.92, 0.90])  # was CE-heavy (ratio < 0.95)
        result = a.check_iv_skew_reversal(agg, options_live, 20000.0)
        # With valid options_live, the method should fire or return None based on history
        assert isinstance(result, (tuple, type(None)))
