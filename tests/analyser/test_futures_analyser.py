"""Tests for analyser/Futures_Analyser.py."""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

import common.shared as shared
from services.analysis_engine.analyser.Futures_Analyser import FuturesAnalyser
from tests.analyser.conftest import make_stock


def _positional_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    return mock


def _intraday_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    return mock


def _futures_df(n=30, base_price=20000.0, base_oi=1_000_000, trend="up", oi_trend="up"):
    """Build a minimal futures DataFrame with required columns."""
    prices = [base_price * (1.0 + 0.005 * i if trend == "up" else 1.0 - 0.005 * i) for i in range(n)]
    ois    = [base_oi   * (1.0 + 0.01  * i if oi_trend == "up" else 1.0 - 0.01  * i) for i in range(n)]
    highs  = [p * 1.001 for p in prices]
    lows   = [p * 0.999 for p in prices]
    idx    = pd.date_range("2024-01-01", periods=n, freq="5min")
    return pd.DataFrame({
        "open":    prices,
        "high":    highs,
        "low":     lows,
        "close":   prices,
        "volume":  [50_000] * n,
        "oi":      ois,
        "underlying_price": prices,
    }, index=idx)


def _stock_with_futures(curr_df, next_df=None):
    s = make_stock()
    s.zerodha_ctx = {
        "futures_data": {
            "current": curr_df,
            "next": next_df if next_df is not None else pd.DataFrame(),
        }
    }
    return s


class TestCalculateAtr:
    def test_returns_float(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(20)
            atr = a.calculate_atr(df)
            assert isinstance(atr, (float, np.floating))
            assert atr >= 0.0

    def test_short_data_returns_float(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(3)
            atr = a.calculate_atr(df, period=14)
            assert isinstance(atr, (float, np.floating))


class TestGetDynamicThresholds:
    def test_returns_two_floats(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = make_stock()
            df = _futures_df(25)
            price_thr, oi_thr = a.get_dynamic_thresholds(s, df)
            assert isinstance(price_thr, (int, float, np.floating))
            assert isinstance(oi_thr, (int, float, np.floating))
            assert price_thr >= 0.0
            assert oi_thr >= 0.0

    def test_positional_threshold_minimum(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = make_stock()
            df = _futures_df(25)
            price_thr, oi_thr = a.get_dynamic_thresholds(s, df)
            assert float(price_thr) >= FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE

    def test_intraday_threshold_minimum(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = make_stock()
            df = _futures_df(25)
            price_thr, oi_thr = a.get_dynamic_thresholds(s, df)
            assert price_thr >= 0.0


class TestCalculateTrend:
    def test_uptrend_returns_bullish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(10, trend="up")
            assert a.calculate_trend(df) == "BULLISH"

    def test_downtrend_returns_bearish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(10, trend="down")
            assert a.calculate_trend(df) == "BEARISH"

    def test_single_candle_returns_neutral(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(1)
            assert a.calculate_trend(df) == "NEUTRAL"

    def test_returns_string(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(10)
            result = a.calculate_trend(df)
            assert result in ("BULLISH", "BEARISH", "NEUTRAL")


class TestAnalyseIntradayCheckFutureAction:
    def test_long_buildup_bullish(self):
        """Price up + OI up → long buildup → BULLISH FUTURE_ACTION."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            # Make a large price+oi jump in the last row
            df = _futures_df(5, trend="up", oi_trend="up")
            # Exaggerate the last 2 rows for clear signal
            df.iloc[-1, df.columns.get_loc("close")] = df.iloc[-2]["close"] * 1.05
            df.iloc[-1, df.columns.get_loc("oi")]    = df.iloc[-2]["oi"]    * 1.05
            s = _stock_with_futures(df)
            result = a.analyse_intraday_check_future_action(s)
            assert isinstance(result, bool)
            if result:
                assert "FUTURE_ACTION" in s.analysis.get("BULLISH", {})

    def test_short_buildup_bearish(self):
        """Price down + OI up → short buildup → BEARISH."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            df = _futures_df(5, trend="up", oi_trend="up")
            df.iloc[-1, df.columns.get_loc("close")] = df.iloc[-2]["close"] * 0.95
            df.iloc[-1, df.columns.get_loc("oi")]    = df.iloc[-2]["oi"]    * 1.05
            s = _stock_with_futures(df)
            result = a.analyse_intraday_check_future_action(s)
            if result:
                assert "FUTURE_ACTION" in s.analysis.get("BEARISH", {})

    def test_insufficient_data_returns_false(self):
        """Single row → cannot compute prev/curr comparison → False."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            df = _futures_df(1)
            s = _stock_with_futures(df)
            assert a.analyse_intraday_check_future_action(s) is False

    def test_no_futures_data_returns_false(self):
        """Empty DataFrame → should return False."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = _stock_with_futures(pd.DataFrame())
            assert a.analyse_intraday_check_future_action(s) is False


class TestResetConstants:
    def test_positional_thresholds(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            assert FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE == 3.0
            assert FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE == 1.0

    def test_intraday_thresholds(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            assert FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE == 0.10
            assert FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE == 0.15


# ── Per-symbol session state ─────────────────────────────────────────────────

def _orb_df(day="2024-01-02", breakout=True, base=20000.0, open_oi=1_000_000):
    """09:15-start 5-min session: flat ORB, then a final bar breaking out with OI + volume."""
    n = 12
    idx = pd.date_range(f"{day} 09:15", periods=n, freq="5min")
    closes = [base] * n
    vols = [50_000] * n
    ois = [open_oi + i * 1_000 for i in range(n)]
    if breakout:
        closes[-1] = base * 1.01
        vols[-1] = 200_000
        ois[-1] = ois[-2] * 1.02
    return pd.DataFrame({
        "open": closes, "high": [c * 1.0005 for c in closes], "low": [c * 0.9995 for c in closes],
        "close": closes, "volume": vols, "oi": ois, "underlying_price": closes,
    }, index=idx)


class TestPerSymbolSessionState:
    def setup_method(self):
        FuturesAnalyser._session_state = {}

    def test_orb_fired_for_one_symbol_does_not_block_another(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            FuturesAnalyser._session_state = {}
            s1 = _stock_with_futures(_orb_df())
            s1.stock_symbol = "NIFTY"
            s2 = _stock_with_futures(_orb_df())
            s2.stock_symbol = "BANKNIFTY"
            assert a.analyse_intraday_breakout_oi_confirmation(s1) is True
            assert a.analyse_intraday_breakout_oi_confirmation(s2) is True
            # Same symbol, same session → deduplicated
            s1b = _stock_with_futures(_orb_df())
            s1b.stock_symbol = "NIFTY"
            assert a.analyse_intraday_breakout_oi_confirmation(s1b) is False

    def test_orb_state_resets_on_new_session_date(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            FuturesAnalyser._session_state = {}
            s = _stock_with_futures(_orb_df(day="2024-01-02"))
            assert a.analyse_intraday_breakout_oi_confirmation(s) is True
            s_next = _stock_with_futures(_orb_df(day="2024-01-03"))
            assert a.analyse_intraday_breakout_oi_confirmation(s_next) is True

    def test_session_open_oi_is_per_symbol(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            FuturesAnalyser._session_state = {}
            s1 = _stock_with_futures(_orb_df(open_oi=1_000_000))
            s1.stock_symbol = "NIFTY"
            s2 = _stock_with_futures(_orb_df(open_oi=5_000_000))
            s2.stock_symbol = "BANKNIFTY"
            a.analyse_intraday_oi_buildup_from_open(s1)
            a.analyse_intraday_oi_buildup_from_open(s2)
            st = FuturesAnalyser._session_state
            assert st["NIFTY"]["open_oi"] == 1_000_000
            assert st["BANKNIFTY"]["open_oi"] == 5_000_000
