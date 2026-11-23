"""Tests for analyser/IVAnalyser.py."""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

import common.shared as shared
from services.analysis_engine.analyser.IVAnalyser import IVAnalyser
from tests.analyser.conftest import make_stock, make_sensibull_ctx


def _positional_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    return mock


def _intraday_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    return mock


def _stock_with_iv(atm_iv=20.0, atm_iv_change=5.0, expiry="2024-01-25", days=5):
    s = make_stock()
    s.sensibull_ctx = make_sensibull_ctx(
        atm_iv=atm_iv,
        atm_iv_change=atm_iv_change,
        expiry=expiry,
        days_to_expiry=days,
    )
    return s


class TestResetConstants:
    def test_intraday_iv_pct_change(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            assert IVAnalyser.IV_PERCENTAGE_CHANGE == 5

    def test_positional_iv_pct_change(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            assert IVAnalyser.IV_PERCENTAGE_CHANGE == 20


class TestAnalyseSpikeInATMIV:
    def test_no_sensibull_data_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = make_stock()
            # Empty per_expiry_map
            s.sensibull_ctx = make_sensibull_ctx()
            s.sensibull_ctx["current"]["stats"]["per_expiry_map"] = {}
            assert a.analyse_spike_in_ATM_IV(s) is False

    def test_positional_spike_above_threshold_detected(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            # atm_iv=20, atm_iv_change=6 → prev_iv=14, change=6/14*100≈42.8% ≥ 20%
            s = _stock_with_iv(atm_iv=20.0, atm_iv_change=6.0)
            result = a.analyse_spike_in_ATM_IV(s)
            assert result is True
            assert "IV_SPIKE" in s.analysis.get("NEUTRAL", {})

    def test_positional_small_change_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            # atm_iv=20, atm_iv_change=0.5 → ~2.6% change < 20%
            s = _stock_with_iv(atm_iv=20.0, atm_iv_change=0.5)
            assert a.analyse_spike_in_ATM_IV(s) is False

    def test_intraday_uses_historical_data(self):
        """Intraday path reads from historical_data, not atm_iv_change."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx(atm_iv=20.0, expiry="2024-01-25")
            # Add historical data with IV column
            suffix = "20240125"
            iv_col = f"atm_iv_{suffix}"
            df = pd.DataFrame({
                iv_col: [15.0, 22.0],  # +46.7% change — above intraday threshold of 5%
                "timestamp": pd.date_range("2024-01-01", periods=2, freq="5min"),
            })
            s.sensibull_ctx["historical_data"] = df
            result = a.analyse_spike_in_ATM_IV(s)
            assert result is True

    def test_intraday_insufficient_history_returns_false(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx(atm_iv=20.0, expiry="2024-01-25")
            # Only 1 row — insufficient
            suffix = "20240125"
            df = pd.DataFrame({f"atm_iv_{suffix}": [20.0]})
            s.sensibull_ctx["historical_data"] = df
            assert a.analyse_spike_in_ATM_IV(s) is False


class TestAnalyseTrendInATMIV:
    def _stock_with_iv_history(self, iv_values, expiry="2024-01-25"):
        s = make_stock()
        s.sensibull_ctx = make_sensibull_ctx(expiry=expiry)
        suffix = "20240125"
        df = pd.DataFrame({
            f"atm_iv_{suffix}": iv_values,
            f"atm_iv_percentile_{suffix}": [50.0] * len(iv_values),
            "timestamp": pd.date_range("2024-01-01", periods=len(iv_values), freq="5min"),
        })
        s.sensibull_ctx["historical_data"] = df
        return s

    def test_insufficient_history_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = self._stock_with_iv_history([20.0, 21.0])  # only 2 rows
            assert a.analyse_trend_in_ATM_IV(s) is False

    def test_rising_trend_detected(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            # Rising IV: 20 → 22 → 26 → +30% over 3 snapshots ≥ IV_TREND_PERCENTAGE_CHANGE=20
            s = self._stock_with_iv_history([20.0, 22.0, 26.0])
            result = a.analyse_trend_in_ATM_IV(s)
            assert result is True
            assert "IV_TREND" in s.analysis.get("NEUTRAL", {})

    def test_falling_trend_detected(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = self._stock_with_iv_history([30.0, 25.0, 22.0])  # falling -26.7%
            result = a.analyse_trend_in_ATM_IV(s)
            assert result is True
            trend = s.analysis["NEUTRAL"]["IV_TREND"]
            assert trend.trend == "DOWNWARD"
            assert trend.atm_iv == pytest.approx(22.0)


class TestAnalyseIVRank:
    def _stock_with_percentile(self, percentile, expiry="2024-01-25"):
        s = make_stock()
        s.sensibull_ctx = make_sensibull_ctx(expiry=expiry)
        suffix = expiry.replace("-", "")
        df = pd.DataFrame({
            f"atm_iv_{suffix}": [20.0, 20.0, 20.0],
            f"atm_iv_percentile_{suffix}": [percentile, percentile, percentile],
            "timestamp": pd.date_range("2024-01-01", periods=3, freq="5min"),
        })
        s.sensibull_ctx["historical_data"] = df
        # Also set the per_expiry_map
        s.sensibull_ctx["current"]["stats"]["per_expiry_map"][expiry]["atm_iv_percentile"] = percentile
        s.sensibull_ctx["current"]["stats"]["per_expiry_map"][expiry]["atm_ivp_type"] = (
            "VERY_LOW" if percentile < 10 else
            "LOW" if percentile < 20 else
            "HIGH" if percentile > 70 else
            "VERY_HIGH" if percentile > 85 else "NORMAL"
        )
        return s

    def test_extreme_low_iv_rank(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = self._stock_with_percentile(5.0)
            result = a.analyse_iv_rank(s)
            assert result is True

    def test_extreme_high_iv_rank(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = self._stock_with_percentile(90.0)
            result = a.analyse_iv_rank(s)
            assert result is True

    def test_mid_range_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = self._stock_with_percentile(50.0)
            assert a.analyse_iv_rank(s) is False


class TestIvSpikeDirectionSplit:
    def test_iv_drop_emits_crush_not_spike(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            # atm_iv=14, change=-6 → prev 20 → -30%
            s = _stock_with_iv(atm_iv=14.0, atm_iv_change=-6.0)
            assert a.analyse_spike_in_ATM_IV(s) is True
            neutral = s.analysis["NEUTRAL"]
            assert "IV_SPIKE" not in neutral
            assert neutral["IV_CRUSH"].direction == "DOWN"
            assert neutral["IV_CRUSH"].iv_change < 0

    def test_iv_rise_is_spike_with_up_direction(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = _stock_with_iv(atm_iv=20.0, atm_iv_change=6.0)
            a.analyse_spike_in_ATM_IV(s)
            assert s.analysis["NEUTRAL"]["IV_SPIKE"].direction == "UP"

    def test_index_scores_only_nearest_expiry(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = make_stock(index=True)
            s.sensibull_ctx = make_sensibull_ctx(atm_iv=20.0, atm_iv_change=6.0, expiry="2024-01-25")
            pem = s.sensibull_ctx["current"]["stats"]["per_expiry_map"]
            base = next(iter(pem.values()))
            pem["2024-02-01"] = dict(base)
            pem["2024-02-29"] = dict(base)
            assert a.analyse_spike_in_ATM_IV(s) is True
            spike = s.analysis["NEUTRAL"]["IV_SPIKE"]
            assert not isinstance(spike, list)   # one entry, not one per expiry
            assert spike.expiry == "2024-01-25"
            assert s.analysis["NoOfTrends"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# Gap-coverage tests (audit 2026-11-23): analyse_iv_vs_hv was 61 % covered and
# no test exercised the zone thresholds or the signal/no-signal split.
# ══════════════════════════════════════════════════════════════════════════════

def _alternating_closes(n=120):
    """Deterministic ±1 % daily returns → HV ≈ 15.8 % in positional mode."""
    closes = [100.0]
    for i in range(1, n):
        closes.append(closes[-1] * (1.01 if i % 2 else 0.99))
    return closes


def _price_frame(closes):
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes,
         "Volume": [1_000] * len(closes)},
        index=pd.date_range("2023-01-01", periods=len(closes), freq="D"),
    )


class TestAnalyseIvVsHv:
    def _stock(self, atm_iv):
        s = make_stock()
        s.priceData = _price_frame(_alternating_closes())
        s.sensibull_ctx = make_sensibull_ctx(atm_iv=atm_iv)
        return s

    def _run(self, atm_iv):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = self._stock(atm_iv)
            result = a.analyse_iv_vs_hv(s)
        return result, s

    def test_extreme_premium_emits_neutral_signal(self):
        result, s = self._run(200.0)
        assert result is True
        premium = s.analysis["NEUTRAL"]["IV_PREMIUM"]
        assert premium.zone == "EXTREME"
        assert premium.iv_hv_ratio >= IVAnalyser.IV_HV_EXTREME_RATIO
        assert premium.hv > 0

    def test_expensive_premium_emits_neutral_signal(self):
        result, s = self._run(28.0)
        assert result is True
        premium = s.analysis["NEUTRAL"]["IV_PREMIUM"]
        assert premium.zone == "EXPENSIVE"
        assert premium.iv_hv_ratio == pytest.approx(1.72, abs=0.1)

    def test_fair_premium_emits_nothing(self):
        result, s = self._run(15.0)
        assert result is False
        assert "IV_PREMIUM" not in s.analysis["NEUTRAL"]

    def test_cheap_premium_emits_nothing(self):
        result, s = self._run(2.0)
        assert result is False
        assert "IV_PREMIUM" not in s.analysis["NEUTRAL"]

    def test_zero_atm_iv_is_skipped(self):
        result, _ = self._run(0.0)
        assert result is False

    def test_insufficient_price_history_is_skipped(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = IVAnalyser()
            a.reset_constants()
            s = make_stock()
            s.priceData = _price_frame(_alternating_closes(10))
            s.sensibull_ctx = make_sensibull_ctx(atm_iv=200.0)
            assert a.analyse_iv_vs_hv(s) is False
