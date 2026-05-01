"""Tests for analyser/PCRAnalyser.py."""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

import common.shared as shared
from analyser.PCRAnalyser import PCRAnalyser
from tests.analyser.conftest import make_stock, make_sensibull_ctx


def _positional_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    return mock


def _intraday_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    return mock


def _stock_with_pcr(pcr, expiry="2024-01-25"):
    s = make_stock()
    s.sensibull_ctx = make_sensibull_ctx(pcr=pcr, expiry=expiry)
    return s


def _stock_with_pcr_history(pcr_values, expiry="2024-01-25"):
    """Stock with a DataFrame of historical PCR values."""
    s = _stock_with_pcr(pcr_values[-1], expiry=expiry)
    suffix = expiry.replace("-", "")
    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=len(pcr_values), freq="D"),
        "total_pcr": pcr_values,
        f"pcr_{suffix}": pcr_values,
    })
    s.sensibull_ctx["historical_data"] = df
    return s


class TestAnalysePcrExtremeZones:
    def test_extreme_low_pcr_bullish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(0.25)  # < PCR_EXTREME_BEARISH (0.3)
            result = a.analyse_pcr_extreme_zones(s)
            assert result is True
            assert "PCR_EXTREME" in s.analysis.get("BULLISH", {})

    def test_extreme_high_pcr_bearish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(1.6)  # > PCR_EXTREME_BULLISH (1.5)
            result = a.analyse_pcr_extreme_zones(s)
            assert result is True
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


class TestAnalysePcrDirectionalBias:
    def test_low_pcr_bearish_bias(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(0.4)  # < PCR_BEARISH_THRESHOLD (0.5)
            result = a.analyse_pcr_directional_bias(s)
            assert result is True
            assert "PCR_BIAS" in s.analysis.get("BEARISH", {})

    def test_high_pcr_bullish_bias(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(1.3)  # > PCR_BULLISH_THRESHOLD (1.2)
            result = a.analyse_pcr_directional_bias(s)
            assert result is True
            assert "PCR_BIAS" in s.analysis.get("BULLISH", {})

    def test_neutral_pcr_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            assert a.analyse_pcr_directional_bias(_stock_with_pcr(0.8)) is False


class TestAnalysePcrTrend:
    def test_rising_trend_bullish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr_history([0.9, 1.05, 1.25])
            result = a.analyse_pcr_trend(s)
            assert result is True
            assert "PCR_TREND" in s.analysis.get("BULLISH", {})

    def test_falling_trend_bearish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr_history([1.3, 1.1, 0.8])
            result = a.analyse_pcr_trend(s)
            assert result is True
            assert "PCR_TREND" in s.analysis.get("BEARISH", {})

    def test_insufficient_history_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr_history([1.0, 1.1])  # Only 2 rows
            # analyse_pcr_trend is @positional, must have >= 3 rows
            assert a.analyse_pcr_trend(s) is False

    def test_flat_trend_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr_history([1.0, 1.01, 1.0])  # minimal change
            assert a.analyse_pcr_trend(s) is False


class TestAnalysePcrDivergence:
    def _stock_with_two_expiries(self, near_pcr, far_pcr):
        s = make_stock()
        s.sensibull_ctx = make_sensibull_ctx(pcr=near_pcr, expiry="2024-01-25")
        # Set per_expiry_pcr in base_stats (what analyse_pcr_divergence reads)
        s.sensibull_ctx["current"]["stats"]["underlying_base_stats"]["per_expiry_pcr"] = {
            "2024-01-25": near_pcr,
            "2024-02-29": far_pcr,
        }
        return s

    def test_large_divergence_returns_true(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = self._stock_with_two_expiries(near_pcr=0.5, far_pcr=1.8)  # diff=1.3 > 1.2
            result = a.analyse_pcr_divergence(s)
            assert result is True

    def test_small_divergence_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = self._stock_with_two_expiries(near_pcr=1.0, far_pcr=1.1)  # diff=0.1
            assert a.analyse_pcr_divergence(s) is False

    def test_single_expiry_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PCRAnalyser()
            a.reset_constants()
            s = _stock_with_pcr(1.0)  # per_expiry_pcr not set → returns False
            assert a.analyse_pcr_divergence(s) is False


class TestResetConstants:
    def test_thresholds_same_for_both_modes(self):
        mock_i = MagicMock()
        mock_i.mode = shared.Mode.INTRADAY
        mock_p = MagicMock()
        mock_p.mode = shared.Mode.POSITIONAL
        with patch("common.shared.app_ctx", mock_i):
            a = PCRAnalyser()
            a.reset_constants()
            intra = PCRAnalyser.PCR_BULLISH_THRESHOLD
        with patch("common.shared.app_ctx", mock_p):
            a.reset_constants()
            pos = PCRAnalyser.PCR_BULLISH_THRESHOLD
        assert intra == pos == 1.2
