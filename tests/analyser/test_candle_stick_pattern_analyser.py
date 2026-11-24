"""Tests for analyser/candleStickPatternAnalyser.py."""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

import common.shared as shared
from services.analysis_engine.analyser.candleStickPatternAnalyser import CandleStickAnalyser
from tests.analyser.conftest import make_stock


def _make_candle(open_, high, low, close, volume=100_000):
    return {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}


def _price_df_from_rows(rows):
    """rows: list of dicts with Open/High/Low/Close/Volume."""
    return pd.DataFrame(rows)


# ── positional-mode constants ─────────────────────────────────────────────────
# After reset for POSITIONAL (stock): MARUBASU=1.5, THREE=5, TWO=4

@pytest.fixture(autouse=True)
def positional_mode():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    with patch("common.shared.app_ctx", mock):
        a = CandleStickAnalyser()
        a.reset_constants(is_index=False)
        yield mock


class TestSingleCandlestickPattern:
    """Marubozu — momentum patterns. MARUBASU_THRESHOLD=1.5 in positional stock mode."""

    def _make_stock_with_positional_candles(self, rows):
        """positional mode: current = iloc[-1], prev = iloc[-2]"""
        s = make_stock()
        s.priceData = pd.DataFrame(rows)
        return s

    def test_bullish_marubozu_detected(self):
        analyser = CandleStickAnalyser()
        # Open=Low, Close=High, close/open change >= 1.5%
        row = {"Open": 100.0, "High": 102.0, "Low": 100.0, "Close": 102.0, "Volume": 100_000}
        rows = [row] * 3
        s = self._make_stock_with_positional_candles(rows)
        result = analyser.analyse_single_candle_momentum(s)
        assert result is True
        assert "BULLISH" in s.analysis
        assert "Single_candle_stick_pattern" in s.analysis["BULLISH"]

    def test_bearish_marubozu_detected(self):
        analyser = CandleStickAnalyser()
        # Open=High, Close=Low, decline >= 1.5%
        row = {"Open": 102.0, "High": 102.0, "Low": 100.0, "Close": 100.0, "Volume": 100_000}
        rows = [row] * 3
        s = self._make_stock_with_positional_candles(rows)
        result = analyser.analyse_single_candle_momentum(s)
        assert result is True
        assert "Single_candle_stick_pattern" in s.analysis["BEARISH"]

    def test_small_body_returns_false(self):
        analyser = CandleStickAnalyser()
        # Only 0.5% change — below MARUBASU_THRESHOLD
        row = {"Open": 100.0, "High": 100.5, "Low": 100.0, "Close": 100.5, "Volume": 100_000}
        rows = [row] * 3
        s = self._make_stock_with_positional_candles(rows)
        assert analyser.analyse_single_candle_momentum(s) is False

    def test_candle_with_wick_returns_false(self):
        analyser = CandleStickAnalyser()
        # Long upper wick — not a marubozu
        row = {"Open": 100.0, "High": 104.0, "Low": 99.5, "Close": 101.5, "Volume": 100_000}
        rows = [row] * 3
        s = self._make_stock_with_positional_candles(rows)
        assert analyser.analyse_single_candle_momentum(s) is False


class TestDoubleCandlestickContinuationPattern:
    """2 consecutive rising / falling closes — TWO_CONT threshold=4 in positional stock."""

    def _make_stock_3rows(self, prev_prev_close, prev_close, curr_close, trend="up"):
        rows = []
        closes = [prev_prev_close, prev_close, curr_close]
        for i, c in enumerate(closes):
            if trend == "up":
                # Bullish candles: Open < Close
                row_open = c * 0.998
            else:
                # Bearish candles: Open > Close
                row_open = c * 1.002
            rows.append({
                "Open": row_open,
                "High": c * 1.003,
                "Low": c * 0.998,
                "Close": c,
                "Volume": 100_000,
            })
        s = make_stock()
        s.priceData = pd.DataFrame(rows)
        return s

    def test_two_rising_bullish(self):
        analyser = CandleStickAnalyser()
        # prev: +0.5%, curr: +5% — total 2-day move >>4%
        s = self._make_stock_3rows(100.0, 100.5, 105.5)
        result = analyser.analyse_double_candle_continuation(s)
        assert result is True
        assert "Double_candle_continuation_pattern" in s.analysis.get("BULLISH", {})

    def test_two_falling_bearish(self):
        analyser = CandleStickAnalyser()
        s = self._make_stock_3rows(100.0, 99.5, 94.5, trend="down")
        result = analyser.analyse_double_candle_continuation(s)
        assert result is True
        assert "Double_candle_continuation_pattern" in s.analysis.get("BEARISH", {})

    def test_mixed_directions_returns_false(self):
        analyser = CandleStickAnalyser()
        s = self._make_stock_3rows(100.0, 103.0, 99.0)  # up then down
        assert analyser.analyse_double_candle_continuation(s) is False


class TestTripleCandlestickContinuationPattern:
    """3 consecutive rising / falling closes — THREE_CONT threshold=5 in positional stock."""

    def _make_stock_5rows(self, closes, trend="up"):
        rows = []
        for c in closes:
            if trend == "up":
                row = {"Open": c * 0.998, "High": c * 1.003, "Low": c * 0.997, "Close": c, "Volume": 100_000}
            else:
                row = {"Open": c * 1.002, "High": c * 1.003, "Low": c * 0.997, "Close": c, "Volume": 100_000}
            rows.append(row)
        s = make_stock()
        s.priceData = pd.DataFrame(rows)
        return s

    def test_three_rising_bullish(self):
        analyser = CandleStickAnalyser()
        # prevprev.Open = 100*0.998=99.8, curr.Close=108.0 → change=(108-99.8)/99.8≈8.2% ≥ 5%
        s = self._make_stock_5rows([98.0, 99.0, 100.0, 103.0, 108.0], trend="up")
        result = analyser.analyse_triple_candle_continuation(s)
        assert result is True
        assert "Triple_candle_continuation_pattern" in s.analysis.get("BULLISH", {})

    def test_three_falling_bearish(self):
        analyser = CandleStickAnalyser()
        # prevprev.Open = 100*1.002=100.2, curr.Close=93.0 → |change|=(100.2-93)/100.2≈7.2% ≥ 5%
        s = self._make_stock_5rows([102.0, 101.0, 100.0, 97.0, 93.0], trend="down")
        result = analyser.analyse_triple_candle_continuation(s)
        assert result is True
        assert "Triple_candle_continuation_pattern" in s.analysis.get("BEARISH", {})

    def test_two_up_one_down_returns_false(self):
        analyser = CandleStickAnalyser()
        s = self._make_stock_5rows([100.0, 102.0, 104.0, 106.0, 103.0], trend="up")
        assert analyser.analyse_triple_candle_continuation(s) is False


class TestResetConstants:
    def test_intraday_stock_marubasu_threshold(self):
        mock = MagicMock()
        mock.mode = shared.Mode.INTRADAY
        with patch("common.shared.app_ctx", mock):
            a = CandleStickAnalyser()
            a.reset_constants(is_index=False)
            assert CandleStickAnalyser.MARUBASU_THRESHOLD == 1.5
            assert CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD == 1.5

    def test_positional_stock_three_threshold(self):
        mock = MagicMock()
        mock.mode = shared.Mode.POSITIONAL
        with patch("common.shared.app_ctx", mock):
            a = CandleStickAnalyser()
            a.reset_constants(is_index=False)
            assert CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD == 5

    def test_positional_index_lower_threshold(self):
        mock = MagicMock()
        mock.mode = shared.Mode.POSITIONAL
        with patch("common.shared.app_ctx", mock):
            a = CandleStickAnalyser()
            a.reset_constants(is_index=True)
            assert CandleStickAnalyser.THREE_CONT_INC_OR_DEC_THRESHOLD == 2.5


# ══════════════════════════════════════════════════════════════════════════════
# Gap-coverage tests (audit 2026-11-23): the three REVERSAL analysers sat at
# 50-54 % line coverage — only reached via run_all_*(), with no test asserting
# that a reversal actually fires or that the trend-context gate blocks it.
#
# Positional-stock constants after reset_constants(): TREND_LOOKBACK_PERIOD=3,
# DOWNTREND_MIN_DECLINE=1.75, UPTREND_MIN_INCREASE=3.0,
# HAMMER_BODY_RATIO=0.25, HAMMER_WICK_MULTIPLIER=3.0, STAR_MAX_BODY_RATIO=0.15.
# ══════════════════════════════════════════════════════════════════════════════

def _trend_rows(n, start, step):
    """n consecutive candles moving by ``step`` each (negative = downtrend)."""
    rows, price = [], start
    for _ in range(n):
        open_, close = price, price + step
        rows.append({
            "Open": open_, "High": max(open_, close) + 0.2,
            "Low": min(open_, close) - 0.2, "Close": close, "Volume": 100_000,
        })
        price = close
    return rows


def _candle(open_, close, high, low):
    return {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": 100_000}


class TestSingleCandleReversal:
    def _run(self, rows):
        s = make_stock()
        s.priceData = pd.DataFrame(rows)
        return CandleStickAnalyser().analyse_single_candle_reversal(s), s

    def test_hammer_after_downtrend_is_bullish(self):
        rows = _trend_rows(7, 110.0, -1.5)
        rows.append(_candle(open_=99.2, close=99.3, high=99.4, low=96.8))
        result, s = self._run(rows)
        assert result is True
        assert s.analysis["BULLISH"]["Single_candle_reversal_pattern"].pattern == "Hammer"

    def test_shooting_star_after_uptrend_is_bearish(self):
        rows = _trend_rows(7, 90.0, 1.5)
        rows.append(_candle(open_=100.8, close=100.7, high=103.4, low=100.65))
        result, s = self._run(rows)
        assert result is True
        assert s.analysis["BEARISH"]["Single_candle_reversal_pattern"].pattern == "Shooting Star"

    def test_hammer_shape_in_uptrend_is_rejected_by_trend_gate(self):
        rows = _trend_rows(7, 90.0, 1.5)
        rows.append(_candle(open_=99.2, close=99.3, high=99.4, low=96.8))
        result, s = self._run(rows)
        assert result is False
        assert "Single_candle_reversal_pattern" not in s.analysis["BULLISH"]


class TestDoubleCandleReversal:
    def _run(self, rows):
        s = make_stock()
        s.priceData = pd.DataFrame(rows)
        return CandleStickAnalyser().analyse_double_candle_reversal(s), s

    def test_bullish_engulfing_after_downtrend(self):
        rows = _trend_rows(6, 110.0, -1.5)
        rows.append(_candle(open_=101.0, close=99.5, high=101.1, low=99.4))
        rows.append(_candle(open_=99.0, close=101.5, high=101.6, low=98.9))
        result, s = self._run(rows)
        assert result is True
        assert s.analysis["BULLISH"]["Double_candle_stick_pattern"].pattern == "Bullish Engulfing"

    def test_bearish_engulfing_after_uptrend(self):
        rows = _trend_rows(6, 90.0, 1.5)
        rows.append(_candle(open_=99.0, close=100.5, high=100.6, low=98.9))
        rows.append(_candle(open_=101.0, close=98.5, high=101.1, low=98.4))
        result, s = self._run(rows)
        assert result is True
        assert s.analysis["BEARISH"]["Double_candle_stick_pattern"].pattern == "Bearish Engulfing"

    def test_engulfing_shape_in_wrong_trend_is_rejected(self):
        rows = _trend_rows(6, 90.0, 1.5)
        rows.append(_candle(open_=101.0, close=99.5, high=101.1, low=99.4))
        rows.append(_candle(open_=99.0, close=101.5, high=101.6, low=98.9))
        result, _ = self._run(rows)
        assert result is False


class TestTripleCandleReversal:
    def _run(self, rows):
        s = make_stock()
        s.priceData = pd.DataFrame(rows)
        return CandleStickAnalyser().analyse_triple_candle_reversal(s), s

    def test_morning_star_after_downtrend(self):
        rows = _trend_rows(7, 110.0, -1.5)
        rows.append(_candle(open_=99.5, close=98.0, high=99.6, low=97.9))
        rows.append(_candle(open_=98.0, close=97.9, high=98.05, low=97.8))
        rows.append(_candle(open_=98.05, close=99.0, high=99.1, low=98.0))
        result, s = self._run(rows)
        assert result is True
        assert s.analysis["BULLISH"]["Triple_candle_reversal_pattern"].pattern == "Morning Star"

    def test_evening_star_after_uptrend(self):
        rows = _trend_rows(7, 90.0, 1.5)
        rows.append(_candle(open_=100.5, close=102.0, high=102.1, low=100.4))
        rows.append(_candle(open_=102.0, close=102.1, high=102.15, low=101.95))
        rows.append(_candle(open_=102.05, close=100.9, high=102.1, low=100.8))
        result, s = self._run(rows)
        assert result is True
        assert s.analysis["BEARISH"]["Triple_candle_reversal_pattern"].pattern == "Evening Star"


class TestShortPriceHistory:
    """A replay/backtest can start with 1-2 bars; the analysers must skip."""

    def test_two_rows_skip_triple_patterns_without_raising(self):
        import pandas as pd

        rows = [
            {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1},
            {"Open": 100.5, "High": 102.0, "Low": 100.0, "Close": 101.5, "Volume": 1},
        ]
        s = make_stock()
        s.priceData = pd.DataFrame(rows, index=pd.date_range("2024-01-01", periods=2))
        a = CandleStickAnalyser()

        assert a.analyse_triple_candle_reversal(s) is False
        assert a.analyse_triple_candle_continuation(s) is False

    def test_one_row_skips_two_candle_patterns_without_raising(self):
        import pandas as pd

        s = make_stock()
        s.priceData = pd.DataFrame(
            [{"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1}],
            index=pd.date_range("2024-01-01", periods=1),
        )
        a = CandleStickAnalyser()

        assert a.analyse_single_candle_reversal(s) is False
        assert a.analyse_double_candle_reversal(s) is False
