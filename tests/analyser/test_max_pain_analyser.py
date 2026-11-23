"""Tests for analyser/MaxPainAnalyser.py."""
import pytest
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import common.shared as shared
from services.analysis_engine.analyser.MaxPainAnalyser import MaxPainAnalyser
from tests.analyser.conftest import make_stock, make_sensibull_ctx

# Dynamically computed future expiry dates so the tests are not date-sensitive
_FAR_EXPIRY  = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")  # > 12 days
_NEAR_EXPIRY = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")   # ≤ 12 days


def _positional_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    return mock


def _intraday_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    return mock


def _stock_with_max_pain(ltp, max_pain_strike, expiry=None):
    if expiry is None:
        expiry = _NEAR_EXPIRY
    s = make_stock()
    s.ltp = ltp
    s.sensibull_ctx = make_sensibull_ctx(expiry=expiry)
    s.sensibull_ctx["current"]["stats"]["per_expiry_map"][expiry]["max_pain_strike"] = max_pain_strike
    return s


class TestAnalyseMaxPainDeviation:
    def test_no_sensibull_data_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            s = make_stock()
            assert a.analyse_max_pain_deviation(s) is False

    def test_far_expiry_returns_false(self):
        """More than 12 days to expiry — signal gated out."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            s = _stock_with_max_pain(ltp=20600, max_pain_strike=20000, expiry=_FAR_EXPIRY)
            # deviation = 3% > threshold, but days > 12
            assert a.analyse_max_pain_deviation(s) is False

    def test_bullish_deviation_above_max_pain(self):
        """Price > max_pain → price is 'above', expect move toward max_pain (bearish)."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            # threshold positional = 3%. deviation: (21000-20000)/20000=5% → strong
            s = _stock_with_max_pain(ltp=21000, max_pain_strike=20000)
            result = a.analyse_max_pain_deviation(s)
            assert result is True

    def test_below_threshold_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            # deviation = 1% < positional threshold (3%)
            s = _stock_with_max_pain(ltp=20200, max_pain_strike=20000)
            assert a.analyse_max_pain_deviation(s) is False

    def test_intraday_lower_threshold(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            # intraday threshold = 2%, deviation = 2.5%
            s = _stock_with_max_pain(ltp=20500, max_pain_strike=20000)
            result = a.analyse_max_pain_deviation(s)
            assert result is True


class TestAnalyseMaxPainTrend:
    def _stock_with_pain_history(self, pain_values, ltp=20000, expiry=None):
        if expiry is None:
            expiry = _NEAR_EXPIRY
        s = make_stock()
        s.ltp = ltp
        s.sensibull_ctx = make_sensibull_ctx(expiry=expiry, days_to_expiry=5)
        suffix = expiry.replace("-", "")
        df = pd.DataFrame({
            "timestamp": pd.date_range("2024-01-01", periods=len(pain_values), freq="D"),
            f"max_pain_{suffix}": pain_values,
        })
        s.sensibull_ctx["historical_data"] = df
        s.sensibull_ctx["current"]["stats"]["per_expiry_map"][expiry]["max_pain_strike"] = pain_values[-1]
        s.sensibull_ctx["current"]["stats"]["per_expiry_map"][expiry]["days_to_expiry"] = 5
        return s

    def test_insufficient_history_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            s = self._stock_with_pain_history([20000])  # only 1 snapshot
            assert a.analyse_max_pain_trend(s) is False

    def test_positional_convergence_toward_max_pain_is_bearish(self):
        """Spot 4.6 % above max pain narrowing to 2.5 % → BEARISH CONVERGING."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            s = make_stock()
            s.ltp = 20500.0
            s.sensibull_ctx = make_sensibull_ctx(expiry=_NEAR_EXPIRY, days_to_expiry=5)
            s.sensibull_ctx["oi_history"] = pd.DataFrame({
                "date": pd.date_range("2024-01-01", periods=5, freq="D"),
                "max_pain": [19600, 19500, 19500, 19700, 20000],
                "spot": [20200, 20400, 20550, 20600, 20500],
            })
            assert a.analyse_max_pain_trend(s) is True
            trend = s.analysis["BEARISH"]["MAX_PAIN_TREND"]
            assert trend.trend == "CONVERGING"
            assert trend.curr_deviation == pytest.approx(2.5)

    def test_intraday_convergence_uses_future_price_columns(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            s = make_stock()
            s.ltp = 20520.0
            s.sensibull_ctx = make_sensibull_ctx(expiry=_NEAR_EXPIRY, days_to_expiry=5)
            suffix = _NEAR_EXPIRY.replace("-", "")
            s.sensibull_ctx["historical_data"] = pd.DataFrame({
                "timestamp": pd.date_range("2024-01-01", periods=4, freq="5min"),
                f"max_pain_{suffix}": [19500] * 4,
                f"future_price_{suffix}": [20500, 20600, 20550, 20520],
            })
            assert a.analyse_max_pain_trend(s) is True
            assert s.analysis["BEARISH"]["MAX_PAIN_TREND"].trend == "CONVERGING"

    def test_positional_without_oi_history_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx(expiry=_NEAR_EXPIRY, days_to_expiry=5)
            assert a.analyse_max_pain_trend(s) is False


class TestResetConstants:
    def test_intraday_lower_deviation_threshold(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            assert MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD == 2.0
            assert MaxPainAnalyser.MAX_PAIN_STRONG_DEVIATION == 4.0

    def test_positional_higher_deviation_threshold(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            assert MaxPainAnalyser.MAX_PAIN_DEVIATION_THRESHOLD == 3.0
            assert MaxPainAnalyser.MAX_PAIN_STRONG_DEVIATION == 5.0


# ══════════════════════════════════════════════════════════════════════════════
# Gap-coverage tests (audit 2026-11-23): analyse_max_pain_alignment had 58 %
# line coverage and was never named by a test — only reached via run_all_*().
# ══════════════════════════════════════════════════════════════════════════════

class TestAnalyseMaxPainAlignment:
    def _stock(self, max_pain_type, pcr_type):
        s = make_stock()
        ctx = make_sensibull_ctx(expiry=_NEAR_EXPIRY)
        expiry_data = ctx["current"]["stats"]["per_expiry_map"][_NEAR_EXPIRY]
        expiry_data["max_pain_type"] = max_pain_type
        expiry_data["pcr_type"] = pcr_type
        expiry_data["pcr"] = 1.4
        expiry_data["max_pain_strike"] = 20000
        s.sensibull_ctx = ctx
        return s

    def test_matching_directional_types_align(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            s = self._stock("Bullish", "Bullish")
            assert a.analyse_max_pain_alignment(s) is True
            result = s.analysis["BULLISH"]["MAX_PAIN_ALIGNMENT"]
            assert result.alignment == "ALIGNED"
            assert result.expiry == _NEAR_EXPIRY
            assert result.pcr == 1.4

    def test_conflicting_directional_types_diverge(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            s = self._stock("Bullish", "Bearish")
            assert a.analyse_max_pain_alignment(s) is False
            result = s.analysis["NEUTRAL"]["MAX_PAIN_ALIGNMENT"]
            assert result.alignment == "DIVERGENT"
            assert "conflicts" in result.signal

    def test_neutral_pcr_is_undetermined_and_emits_nothing(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            s = self._stock("Bullish", "Neutral")
            assert a.analyse_max_pain_alignment(s) is False
            assert "MAX_PAIN_ALIGNMENT" not in s.analysis["NEUTRAL"]
            assert "MAX_PAIN_ALIGNMENT" not in s.analysis["BULLISH"]

    def test_missing_max_pain_type_is_skipped(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            s = self._stock(None, "Bullish")
            assert a.analyse_max_pain_alignment(s) is False
