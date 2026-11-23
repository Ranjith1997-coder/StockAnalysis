"""Tests for analyser/VolumeAnalyser.py."""
import pandas as pd
from unittest.mock import patch, MagicMock

import common.shared as shared
from services.analysis_engine.analyser.VolumeAnalyser import VolumeAnalyser
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
            assert result is True
            assert "VOLUME_BREAKOUT" in s.analysis["BEARISH"]

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

    def test_alternating_volume_without_divergence_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_divergence_df())
            assert a.analyse_obv_divergence(s) is False

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

    def test_slow_grind_up_is_not_a_climax(self):
        """0.5 %/day is below the 5 % lookback move gate → no climax."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_climax_df("up"))
            assert a.analyse_volume_climax(s) is False

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


# ══════════════════════════════════════════════════════════════════════════════
# Gap-coverage tests (audit 2026-11-23).
#
# `test_bearish_breakout_detected`, `TestOBVDivergence.test_returns_bool` and
# `TestVolumeClimax.test_climax_returns_bool` passed regardless of the outcome.
# Fixtures below were verified against the documented gates:
#   OBV: price HH + OBV LH (>= 5 %) + weakening trend.
#   CLIMAX: >= 3x volume, >5 % price move over CLIMAX_LOOKBACK, close in the
#           bottom (buying) or top (selling) 30 % of the day's range.
# ══════════════════════════════════════════════════════════════════════════════

def _row(open_, high, low, close, volume):
    return {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}


def _frame(rows):
    df = pd.DataFrame(rows)
    df.index = pd.date_range("2023-01-01", periods=len(df), freq="D")
    return df


def _divergence_rows():
    """Rising base, then a heavy-volume dip and a light-volume higher peak."""
    rows, price = [], 100.0
    for _ in range(70):
        price *= 1.002
        rows.append(_row(price * 0.999, price * 1.002, price * 0.998, price, 100_000))

    base = rows[-1]["Close"]
    segment = []
    for i in range(40):
        if i < 15:
            p = base * (1 + 0.004 * i)                      # peak A
        elif i < 21:
            p = base * (1 + 0.060 - 0.008 * (i - 14))       # heavy-volume dip
        elif i < 30:
            p = base * (1 + 0.004 + 0.008 * (i - 20))       # higher peak B
        else:
            p = base * (1 + 0.004 + 0.08 - 0.006 * (i - 30))  # fade → EMAs converge
        volume = 120_000 if 15 <= i < 21 else (20_000 if 21 <= i < 30 else 100_000)
        segment.append(_row(p * 0.999, p * 1.002, p * 0.998, p, volume))
    return rows[:-40] + segment


def _climax_rows(direction):
    rows, price = [], 100.0
    for _ in range(40):
        previous = price
        price = previous * (1.008 if direction == "up" else 0.992)
        rows.append(_row(previous, previous * 1.002, price * 0.998, price, 100_000))

    previous = rows[-1]["Close"]
    if direction == "up":
        # Gap up, close near the low of the day → buying climax
        rows.append(_row(previous * 1.03, previous * 1.04,
                         previous * 1.00, previous * 1.005, 400_000))
    else:
        # Gap down, close near the high of the day → selling climax
        rows.append(_row(previous * 0.97, previous * 1.00,
                         previous * 0.96, previous * 0.995, 400_000))
    return rows


class TestVerifiedVolumeSignals:
    def test_bearish_breakout_is_strict(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            s = _stock_with_df(_make_volume_df(n=60, close_change_pct=-3.0, vol_spike=True))
            assert a.analyse_volume_breakout(s) is True
            breakout = s.analysis["BEARISH"]["VOLUME_BREAKOUT"]
            assert breakout.volume_ratio >= 3.0
            assert breakout.price_change_pct < 0

    def test_buying_climax_is_bearish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            s = _stock_with_df(_frame(_climax_rows("up")))
            assert a.analyse_volume_climax(s) is True
            climax = s.analysis["BEARISH"]["VOLUME_CLIMAX"]
            assert climax.climax_type == "buying_climax"
            assert climax.close_position < 0.3

    def test_selling_climax_is_bullish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            s = _stock_with_df(_frame(_climax_rows("down")))
            assert a.analyse_volume_climax(s) is True
            climax = s.analysis["BULLISH"]["VOLUME_CLIMAX"]
            assert climax.climax_type == "selling_climax"
            assert climax.close_position > 0.7

    def test_bearish_obv_divergence_is_bearish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = VolumeAnalyser()
            a.reset_constants()
            s = _stock_with_df(_frame(_divergence_rows()))
            assert a.analyse_obv_divergence(s) is True
            divergence = s.analysis["BEARISH"]["OBV_DIVERGENCE"]
            assert divergence.divergence_type == "bearish"
            assert divergence.price_current > divergence.price_previous
            assert divergence.obv_current < divergence.obv_previous
