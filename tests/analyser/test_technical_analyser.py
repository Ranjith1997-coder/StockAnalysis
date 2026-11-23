"""Tests for analyser/TechnicalAnalyser.py."""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

import common.shared as shared
from services.analysis_engine.analyser.TechnicalAnalyser import TechnicalAnalyser
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
            # RSI after 120 candles of -0.8% each is deep oversold → BULLISH
            assert result is True
            assert "RSI" in s.analysis["BULLISH"]

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

    def test_one_close_outside_the_band_is_not_confirmed(self):
        """The filter needs 2 consecutive closes; the old fixture had one."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_bb_df())
            assert a.analyse_Bolinger_band(s) is False

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

    def test_steady_uptrend_is_not_a_cross(self):
        """A steady trend has no *fresh* cross on the last bar → no signal."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_crossover_df("golden"))
            assert a.analyse_ema_crossover(s) is False

    def test_steady_downtrend_is_not_a_cross(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_crossover_df("death"))
            assert a.analyse_ema_crossover(s) is False

    def test_insufficient_data_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            s = _stock_with_df(_price_df(30))
            assert a.analyse_ema_crossover(s) is False


class TestAnalyseSupertrend:
    def test_steady_uptrend_does_not_flip(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(_price_df(60, trend="up"))
            assert a.analyse_supertrend(s) is False

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

    def test_oversold_without_a_cross_is_false(self):
        """Oversold alone is not a signal: %K must cross above %D."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._make_stoch_oversold_df())
            assert a.analyse_stochastic(s) is False

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


def test_vwap_skips_cleanly_when_no_intraday_rows():
    """Empty today_data must return False without hitting vwap.iloc[-1]."""
    stock = _stock_with_df(_price_df(n=60))   # all rows in 2023
    with patch("services.analysis_engine.analyser.TechnicalAnalyser.logger") as log:
        assert TechnicalAnalyser().analyse_vwap(stock) is False
        log.error.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Gap-coverage tests (audit 2026-11-23).
#
# The tests above assert `isinstance(result, bool)` or wrap the real assertion
# in `if result:`, so they pass whether or not a signal fires. Each fixture
# below was verified to produce the stated signal under the positional-stock
# constants set by reset_constants().
# ══════════════════════════════════════════════════════════════════════════════

def _ohlcv(closes, spread=0.004):
    """OHLCV frame with a symmetric intrabar range (drives ATR/stochastic)."""
    n = len(closes)
    return pd.DataFrame(
        {
            "Open": [c * (1 - spread / 2) for c in closes],
            "High": [c * (1 + spread) for c in closes],
            "Low": [c * (1 - spread) for c in closes],
            "Close": closes,
            "Volume": [100_000] * n,
        },
        index=pd.date_range("2023-01-01", periods=n, freq="D"),
    )


def _geo_series(start=100.0, rate=0.985, n=70):
    return [start * (rate ** i) for i in range(n)]


class TestBollingerBreakout(TestAnalyseBollingerBand):
    """The two-candle confirmation filter is what the old fixture missed."""

    def _breakout_df(self):
        closes = [100.0] * 100 + [105.0, 107.0]   # two closes outside the band
        return _ohlcv(closes)

    def test_two_consecutive_closes_above_upper_band_is_bullish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._breakout_df())
            assert a.analyse_Bolinger_band(s) is True
            assert "BollingerBand" in s.analysis["BULLISH"]

    def test_single_candle_outside_band_does_not_fire(self):
        """One close outside the band is not confirmed (the old fixture)."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(_ohlcv([100.0] * 99 + [115.0]))
            assert a.analyse_Bolinger_band(s) is False


class TestEmaCrossoverSignal:
    """A fresh cross on the last bar with ADX above threshold fires."""

    def _cross_df(self):
        closes = [100.0] * 60
        for _ in range(2):
            closes.append(closes[-1] * 0.988)     # fast dips below slow
        for _ in range(2):
            closes.append(closes[-1] * 1.05)      # sharp recovery → cross at last bar
        return _ohlcv(closes)

    def _run(self, adx_value):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._cross_df())
            with patch.object(
                TechnicalAnalyser, "_compute_adx",
                lambda self, frame: pd.Series(
                    [adx_value] * len(frame), index=frame.index
                ),
            ):
                result = a.analyse_ema_crossover(s)
            return result, s

    def test_bullish_cross_above_adx_threshold_fires(self):
        result, s = self._run(30.0)
        assert result is True
        assert s.analysis["BULLISH"]["EMA_CROSSOVER"].direction == "bullish_crossover"

    def test_adx_below_threshold_blocks_the_signal(self):
        result, s = self._run(5.0)
        assert result is False
        assert "EMA_CROSSOVER" not in s.analysis["BULLISH"]


class TestStochasticSignal:
    def test_k_crosses_d_in_oversold_zone_is_bullish(self):
        closes = _geo_series() + [_geo_series()[-1] * 1.03]   # one bounce bar
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(_ohlcv(closes))
            assert a.analyse_stochastic(s) is True
            assert s.analysis["BULLISH"]["STOCHASTIC"].signal == "bullish_crossover_oversold"

    def test_monotonic_downtrend_without_a_cross_is_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(_ohlcv(_geo_series()))
            assert a.analyse_stochastic(s) is False


class TestSupertrendSignal:
    def test_reversal_from_down_to_up_is_bullish(self):
        closes = _geo_series(rate=0.996, n=40)
        for _ in range(3):
            closes.append(closes[-1] * 1.01)
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(_ohlcv(closes))
            assert a.analyse_supertrend(s) is True
            assert "SUPERTREND" in s.analysis["BULLISH"]


class TestAnalyse52Week:
    """`analyse_is_52_week` was 8 % covered: no test ever reached the branches."""

    def _flat_then(self, last_close, n=253):
        closes = [100.0] * (n - 1) + [last_close]
        return _ohlcv(closes)

    def _high_list(self):
        import common.shared as shared
        return list(shared.ticker_52_week_high_list), list(shared.ticker_52_week_low_list)

    def _restore(self, saved):
        import common.shared as shared
        shared.ticker_52_week_high_list[:] = saved[0]
        shared.ticker_52_week_low_list[:] = saved[1]

    def test_new_high_emits_neutral_52_week_high(self):
        saved = self._high_list()
        try:
            with patch("common.shared.app_ctx", _positional_ctx()):
                s = _stock_with_df(self._flat_then(120.0))
                assert TechnicalAnalyser().analyse_is_52_week(s) is False
                assert "52-week-high" in s.analysis["NEUTRAL"]
        finally:
            self._restore(saved)

    def test_new_low_emits_neutral_52_week_low(self):
        saved = self._high_list()
        try:
            with patch("common.shared.app_ctx", _positional_ctx()):
                s = _stock_with_df(self._flat_then(80.0))
                assert TechnicalAnalyser().analyse_is_52_week(s) is False
                assert "52-week-low" in s.analysis["NEUTRAL"]
        finally:
            self._restore(saved)

    def test_inside_the_range_emits_nothing(self):
        saved = self._high_list()
        try:
            with patch("common.shared.app_ctx", _positional_ctx()):
                s = _stock_with_df(self._flat_then(100.0))
                assert TechnicalAnalyser().analyse_is_52_week(s) is False
                assert "52-week-high" not in s.analysis["NEUTRAL"]
                assert "52-week-low" not in s.analysis["NEUTRAL"]
        finally:
            self._restore(saved)


# ══════════════════════════════════════════════════════════════════════════════
# Gap-coverage tests (audit 2026-11-23, second pass): VWAP (52 %), ATR (62 %),
# buy/sell quantity (60 %) and RSI divergence (64 %).
# ══════════════════════════════════════════════════════════════════════════════

class TestVwapDeviation:
    def _rows(self, direction, n=12):
        rows, price = [], 100.0
        for _ in range(n):
            price *= 1.006 if direction == "up" else 0.994
            rows.append({"Open": price * 0.999, "High": price * 1.001,
                         "Low": price * 0.998, "Close": price, "Volume": 10_000})
        return rows

    def _run(self, direction):
        from datetime import datetime

        rows = self._rows(direction)
        frame = pd.DataFrame(rows)
        frame.index = pd.date_range(end=datetime.now(), periods=len(rows), freq="5min")
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(frame)
            return a.analyse_vwap(s), s

    def test_price_extended_above_vwap_is_bearish(self):
        result, s = self._run("up")
        assert result is True
        analysis = s.analysis["BEARISH"]["vwap_deviation"]
        assert analysis.deviation > TechnicalAnalyser.VWAP_DEVIATION_PERCENTAGE
        assert analysis.vwap_days > TechnicalAnalyser.VWAP_DAYS

    def test_price_extended_below_vwap_is_bullish(self):
        result, s = self._run("down")
        assert result is True
        analysis = s.analysis["BULLISH"]["vwap_deviation"]
        assert analysis.deviation < -TechnicalAnalyser.VWAP_DEVIATION_PERCENTAGE


class TestAtrVolatility:
    def _run(self, expanding):
        rows, base = [], 100.0
        for i in range(30):
            span = 0.5 + 0.25 * i if expanding else 0.5
            rows.append({"Open": base, "High": base + span, "Low": base - span,
                         "Close": base + span * 0.5, "Volume": 10_000})
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(pd.DataFrame(
                rows, index=pd.date_range("2023-01-01", periods=len(rows), freq="D")
            ))
            return a.analyse_atr(s), s

    def test_expanding_ranges_with_high_atr_is_flagged(self):
        result, s = self._run(expanding=True)
        assert result is True
        analysis = s.analysis["NEUTRAL"]["ATR"]
        assert analysis.volatility == "High"
        assert analysis.atr_percentage > TechnicalAnalyser.ATR_THRESHOLD

    def test_constant_ranges_are_not_a_breakout(self):
        result, _ = self._run(expanding=False)
        assert result is False


class TestBuySellQuantity:
    def _run(self, buy, sell):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = make_stock()
            s.update_zerodha_data({"total_buy_quantity": buy, "total_sell_quantity": sell})
            return a.analyze_buy_sell_quantity(s), s

    def test_buy_dominance_is_bullish(self):
        result, s = self._run(5_000, 1_000)
        assert result is True
        assert s.analysis["BULLISH"]["BUY_SELL"].buy_quantity == 5_000

    def test_sell_dominance_is_bearish(self):
        result, s = self._run(1_000, 5_000)
        assert result is True
        assert s.analysis["BEARISH"]["BUY_SELL"].sell_quantity == 5_000

    def test_balanced_quantities_emit_nothing(self):
        result, _ = self._run(1_000, 1_000)
        assert result is False


class TestRsiDivergence:
    def _frame(self, direction, peak_a=0.04, peak_b=0.08, dip=0.01, fade=0.006):
        rows, price = [], 100.0
        for _ in range(80):
            price *= 1.002 if direction == "bearish" else 0.998
            rows.append({"Open": price * 0.999, "High": price * 1.002,
                         "Low": price * 0.998, "Close": price, "Volume": 100_000})
        base = rows[-1]["Close"]
        if direction == "bearish":
            # peak A, dip, higher peak B (weaker momentum), then fade
            seg = []
            for i in range(40):
                if i < 15:
                    p = base * (1 + peak_a * i / 15)
                elif i < 21:
                    p = base * (1 + peak_a - dip * (i - 14))
                elif i < 30:
                    p = base * (1 + peak_a - dip * 6
                                + (peak_b - peak_a + dip * 6) * (i - 20) / 10)
                else:
                    p = base * (1 + peak_b - fade * (i - 30))
                seg.append({"Open": p * 0.999, "High": p * 1.002,
                            "Low": p * 0.998, "Close": p, "Volume": 100_000})
        else:
            # mirror: trough A, bounce, lower trough B, then recovery
            seg = []
            for i in range(40):
                if i < 15:
                    p = base * (1 - peak_a * i / 15)
                elif i < 21:
                    p = base * (1 - peak_a + dip * (i - 14))
                elif i < 30:
                    p = base * (1 - peak_a + dip * 6
                                - (peak_b - peak_a + dip * 6) * (i - 20) / 10)
                else:
                    p = base * (1 - peak_b + fade * (i - 30))
                seg.append({"Open": p * 1.001, "High": p * 1.002,
                            "Low": p * 0.998, "Close": p, "Volume": 100_000})
        frame = pd.DataFrame(rows[:-40] + seg)
        frame.index = pd.date_range("2023-01-01", periods=len(frame), freq="D")
        return frame

    def _run(self, direction):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = TechnicalAnalyser()
            a.reset_constants()
            s = _stock_with_df(self._frame(direction))
            return a.analyse_rsi_divergence(s), s

    def test_higher_high_with_weaker_rsi_is_bearish(self):
        result, s = self._run("bearish")
        assert result is True
        analysis = s.analysis["BEARISH"]["RSI_DIVERGENCE"]
        assert analysis.divergence_type == "bearish"
        assert analysis.price_current > analysis.price_previous

    def test_lower_low_with_stronger_rsi_is_bullish(self):
        result, s = self._run("bullish")
        assert result is True
        analysis = s.analysis["BULLISH"]["RSI_DIVERGENCE"]
        assert analysis.divergence_type == "bullish"
        assert analysis.price_current < analysis.price_previous
