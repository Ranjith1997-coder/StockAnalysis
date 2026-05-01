"""Tests for analyser/VolumeAnalyser.py."""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

import common.shared as shared
from analyser.VolumeAnalyser import VolumeAnalyser
from tests.analyser.conftest import make_stock


def _positional_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    return mock


def _intraday_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    return mock


def _stock_with_df(df):
    s = make_stock()
    s.priceData = df
    return s


def _make_volume_df(n=60, base_close=100.0, close_change_pct=3.0,
                    vol_spike=True, base_volume=100_000):
    """
    Build an OHLCV DataFrame where the last row has a volume spike
    and a large price change.
    """
    closes = [base_close * (1.0 + 0.001 * i) for i in range(n)]
    vols   = [base_volume] * n

    if vol_spike:
        # Last 3 rows escalating volume (trend=rising) and big price move
        closes[-3] = closes[-4] * 1.001
        closes[-2] = closes[-3] * 1.002
        closes[-1] = closes[-2] * (1.0 + close_change_pct / 100.0)
        vols[-3]   = base_volume * 1.5
        vols[-2]   = base_volume * 1.8
        vols[-1]   = base_volume * 4.0   # 4x spike

    opens  = [c * 0.999 for c in closes]
    highs  = [c * 1.001 for c in closes]
    lows   = [c * 0.999 for c in closes]
    idx    = pd.date_range("2023-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


class TestVolumeBreakout:
    def test_bullish_breakout_detected(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            # 4x volume spike + +3% price move
            s = _stock_with_df(_make_volume_df(n=60, close_change_pct=3.0, vol_spike=True))
            result = a.analyse_volume_breakout(s)
            assert result is True
            assert "VOLUME_BREAKOUT" in s.analysis.get("BULLISH", {})

    def test_bearish_breakout_detected(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            df = _make_volume_df(n=60, close_change_pct=-3.0, vol_spike=True)
            s = _stock_with_df(df)
            result = a.analyse_volume_breakout(s)
            if result:
                assert "VOLUME_BREAKOUT" in s.analysis.get("BEARISH", {})

    def test_no_breakout_on_normal_volume(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            # Flat volume, no spike
            s = _stock_with_df(_make_volume_df(n=60, vol_spike=False))
            assert a.analyse_volume_breakout(s) is False

    def test_insufficient_data_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            s = _stock_with_df(_make_volume_df(n=10))
            assert a.analyse_volume_breakout(s) is False


class TestOBVDivergence:
    def _make_divergence_df(self, direction="bullish", n=50):
        """
        Bullish divergence: price makes lower low, OBV makes higher low.
        Create by having falling prices with alternating high/low volumes.
        """
        closes = list(reversed([100.0 * (1.0 + 0.003 * i) for i in range(n)]))  # downtrend
        if direction == "bullish":
            # Last few candles: price still falling but volume on up-days increasing
            for i in range(n - 5, n):
                closes[i] = closes[i] * 0.99  # still falling
        vols = [100_000 + (i % 3) * 10_000 for i in range(n)]
        opens  = [c * 1.001 for c in closes]
        highs  = [c * 1.002 for c in closes]
        lows   = [c * 0.998 for c in closes]
        idx    = pd.date_range("2023-01-01", periods=n, freq="D")
        return pd.DataFrame(
            {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
            index=idx,
        )

    def test_returns_bool(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_divergence_df())
            result = a.analyse_obv_divergence(s)
            assert isinstance(result, bool)

    def test_short_df_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            s = _stock_with_df(_make_volume_df(n=5))
            assert a.analyse_obv_divergence(s) is False


class TestVolumeClimax:
    def _make_climax_df(self, direction="up", n=50):
        closes = [100.0 + i * 0.5 for i in range(n)]
        if direction == "down":
            closes = list(reversed(closes))
        vols   = [100_000] * n
        # Last candle: massive volume spike (> 3x) with price move
        if direction == "up":
            closes[-1] = closes[-2] * 1.03
        else:
            closes[-1] = closes[-2] * 0.97
        vols[-1]   = 400_000   # 4x the average
        opens  = [c * 0.999 for c in closes]
        highs  = [c * 1.001 for c in closes]
        lows   = [c * 0.999 for c in closes]
        idx    = pd.date_range("2023-01-01", periods=n, freq="D")
        return pd.DataFrame(
            {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
            index=idx,
        )

    def test_climax_returns_bool(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_climax_df("up"))
            result = a.analyse_volume_climax(s)
            assert isinstance(result, bool)

    def test_short_df_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            s = _stock_with_df(_make_volume_df(n=5))
            assert a.analyse_volume_climax(s) is False


class TestResetConstants:
    def test_intraday_sets_1_5x_volume(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            assert VolumeAnalyser.TIMES_VOLUME == 1.5
            assert VolumeAnalyser.VOLUME_PRICE_THRESHOLD == 0.5
            assert VolumeAnalyser.CLIMAX_VOLUME_MULT == 2.5

    def test_positional_sets_2x_volume(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            assert VolumeAnalyser.TIMES_VOLUME == 2.0
            assert VolumeAnalyser.VOLUME_PRICE_THRESHOLD == 2.0
            assert VolumeAnalyser.CLIMAX_VOLUME_MULT == 3.0
