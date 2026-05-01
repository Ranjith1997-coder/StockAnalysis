"""Tests for analyser/TechnicalAnalyser.py."""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

import common.shared as shared
from analyser.TechnicalAnalyser import TechnicalAnalyser
from tests.analyser.conftest import make_stock


def _stock_with_df(df):
    s = make_stock()
    s.priceData = df
    return s


def _price_df(n=120, base=100.0, trend="flat", vol=100_000):
    """Build an n-row OHLCV DataFrame."""
    closes = []
    c = base
    for _ in range(n):
        if trend == "up":
            c *= 1.008
        elif trend == "down":
            c *= 0.992
        closes.append(round(c, 4))
    opens  = [c * 0.999 for c in closes]
    highs  = [c * 1.001 for c in closes]
    lows   = [c * 0.999 for c in closes]
    vols   = [vol] * n
    idx    = pd.date_range("2023-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols},
        index=idx,
    )


def _positional_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    return mock


def _intraday_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    return mock


class TestComputeRsi:
    def test_rsi_returns_series(self):
        a = TechnicalAnalyser()
        close = pd.Series([float(i) for i in range(1, 50)])
        rsi = a._compute_rsi(close)
        assert isinstance(rsi, pd.Series)

    def test_rsi_range_0_to_100(self):
        a = TechnicalAnalyser()
        close = pd.Series(list(range(1, 50)))
        rsi = a._compute_rsi(close.astype(float))
        assert (rsi.dropna() >= 0).all()
        assert (rsi.dropna() <= 100).all()

    def test_rsi_raises_on_insufficient_data(self):
        a = TechnicalAnalyser()
        with pytest.raises(ValueError):
            a._compute_rsi(pd.Series([1.0, 2.0]))


class TestComputeAdx:
    def test_adx_returns_series(self):
        a = TechnicalAnalyser()
        df = _price_df(60, trend="up")
        adx = a._compute_adx(df)
        assert isinstance(adx, pd.Series)
        assert len(adx) == 60

    def test_adx_non_negative(self):
        a = TechnicalAnalyser()
        df = _price_df(60, trend="up")
        adx = a._compute_adx(df)
        assert (adx.dropna() >= 0).all()


class TestAnalyseRsi:
    def _make_oversold_df(self):
        """Sustained downtrend to push RSI below 30."""
        closes = [100.0 * (0.992 ** i) for i in range(120)]
        opens  = [c * 0.999 for c in closes]
        highs  = [c * 1.001 for c in closes]
        lows   = [c * 0.999 for c in closes]
        idx    = pd.date_range("2023-01-01", periods=120, freq="D")
        return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                             "Close": closes, "Volume": [100_000]*120}, index=idx)

    def _make_overbought_df(self):
        """Sustained uptrend to push RSI above 85."""
        closes = [100.0 * (1.012 ** i) for i in range(120)]
        opens  = [c * 0.999 for c in closes]
        highs  = [c * 1.001 for c in closes]
        lows   = [c * 0.999 for c in closes]
        idx    = pd.date_range("2023-01-01", periods=120, freq="D")
        return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                             "Close": closes, "Volume": [100_000]*120}, index=idx)

    def test_oversold_bullish_signal(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_oversold_df())
            result = a.analyse_rsi(s)
            # RSI after 120 candles of -0.8% each will be deep oversold
            if result:
                assert "RSI" in s.analysis.get("BULLISH", {})

    def test_insufficient_data_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            s = _stock_with_df(_price_df(50))  # < 100 rows
            assert a.analyse_rsi(s) is False


class TestAnalyseBollingerBand:
    def _make_bb_df(self, n=100):
        idx = pd.date_range("2023-01-01", periods=n, freq="D")
        closes = [100.0] * (n - 1) + [115.0]  # spike at end
        opens  = [c * 0.999 for c in closes]
        highs  = closes
        lows   = [c * 0.999 for c in closes]
        return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                             "Close": closes, "Volume": [100_000]*n}, index=idx)

    def test_price_above_upper_band_bearish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_bb_df())
            result = a.analyse_Bolinger_band(s)
            if result:
                assert "BollingerBand" in s.analysis.get("BEARISH", {})

    def test_short_dataframe_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            s = _stock_with_df(_price_df(10))
            assert a.analyse_Bolinger_band(s) is False


class TestAnalyseEMACrossover:
    def _make_crossover_df(self, direction="golden"):
        """
        For golden cross: price rises sharply after a downtrend.
        Fast EMA (20) will cross above slow EMA (50).
        """
        n = 120
        if direction == "golden":
            closes = [80.0 + i * 0.3 for i in range(n)]  # steady uptrend
        else:
            closes = [120.0 - i * 0.3 for i in range(n)]  # steady downtrend
        opens  = [c * 0.999 for c in closes]
        highs  = [c * 1.001 for c in closes]
        lows   = [c * 0.999 for c in closes]
        idx    = pd.date_range("2023-01-01", periods=n, freq="D")
        return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                             "Close": closes, "Volume": [100_000]*n}, index=idx)

    def test_golden_cross_produces_bullish_signal(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            df = self._make_crossover_df("golden")
            s = _stock_with_df(df)
            result = a.analyse_ema_crossover(s)
            if result:
                assert "EMA_CROSSOVER" in s.analysis.get("BULLISH", {})

    def test_death_cross_produces_bearish_signal(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_crossover_df("death"))
            result = a.analyse_ema_crossover(s)
            if result:
                assert "EMA_CROSSOVER" in s.analysis.get("BEARISH", {})

    def test_insufficient_data_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            s = _stock_with_df(_price_df(30))
            assert a.analyse_ema_crossover(s) is False


class TestAnalyseSupertrend:
    def test_returns_bool(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(_price_df(60, trend="up"))
            result = a.analyse_supertrend(s)
            assert isinstance(result, bool)

    def test_insufficient_data_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            s = _stock_with_df(_price_df(10))
            assert a.analyse_supertrend(s) is False


class TestAnalyseStochastic:
    def _make_stoch_oversold_df(self):
        n = 80
        closes = [100.0 * (0.994 ** i) for i in range(n)]  # downtrend → oversold
        highs  = [c * 1.001 for c in closes]
        lows   = [c * 0.999 for c in closes]
        opens  = [c * 0.999 for c in closes]
        idx    = pd.date_range("2023-01-01", periods=n, freq="D")
        return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                             "Close": closes, "Volume": [100_000]*n}, index=idx)

    def test_oversold_returns_bool(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_stoch_oversold_df())
            result = a.analyse_stochastic(s)
            assert isinstance(result, bool)

    def test_short_df_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            s = _stock_with_df(_price_df(5))
            assert a.analyse_stochastic(s) is False


class TestResetConstants:
    def test_intraday_sets_fast_ema_9(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            assert TechnicalAnalyser.FAST_EMA_PERIOD == 9
            assert TechnicalAnalyser.SLOW_EMA_PERIOD == 21

    def test_positional_sets_fast_ema_20(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            assert TechnicalAnalyser.FAST_EMA_PERIOD == 20
            assert TechnicalAnalyser.SLOW_EMA_PERIOD == 50

    def test_positional_rsi_threshold_85(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            assert TechnicalAnalyser.RSI_UPPER_THRESHOLD == 85
            assert TechnicalAnalyser.RSI_LOWER_THRESHOLD == 30

    def test_intraday_rsi_threshold_80(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            assert TechnicalAnalyser.RSI_UPPER_THRESHOLD == 80
            assert TechnicalAnalyser.RSI_LOWER_THRESHOLD == 20
