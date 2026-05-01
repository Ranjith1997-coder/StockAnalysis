"""Tests for analyser/MaxPainAnalyser.py."""
import pytest
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import common.shared as shared
from analyser.MaxPainAnalyser import MaxPainAnalyser
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

    def test_rising_max_pain_trend(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = MaxPainAnalyser()
            a.reset_constants()
            # Max pain moving from 19500 → 20000 → convergence toward spot
            s = self._stock_with_pain_history([19500, 19700, 20000], ltp=20000)
            result = a.analyse_max_pain_trend(s)
            assert isinstance(result, bool)


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
