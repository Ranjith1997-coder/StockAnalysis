"""Tests for analyser/candleStickPatternAnalyser.py."""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

import common.shared as shared
from analyser.candleStickPatternAnalyser import CandleStickAnalyser
from tests.analyser.conftest import make_stock, make_ohlcv_df, patch_ctx


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
        result = analyser.singleCandleStickPattern(s)
        assert result is True
        assert "BULLISH" in s.analysis
        assert "Single_candle_stick_pattern" in s.analysis["BULLISH"]

    def test_bearish_marubozu_detected(self):
        analyser = CandleStickAnalyser()
        # Open=High, Close=Low, decline >= 1.5%
        row = {"Open": 102.0, "High": 102.0, "Low": 100.0, "Close": 100.0, "Volume": 100_000}
        rows = [row] * 3
        s = self._make_stock_with_positional_candles(rows)
        result = analyser.singleCandleStickPattern(s)
        assert result is True
        assert "Single_candle_stick_pattern" in s.analysis["BEARISH"]

    def test_small_body_returns_false(self):
        analyser = CandleStickAnalyser()
        # Only 0.5% change — below MARUBASU_THRESHOLD
        row = {"Open": 100.0, "High": 100.5, "Low": 100.0, "Close": 100.5, "Volume": 100_000}
        rows = [row] * 3
        s = self._make_stock_with_positional_candles(rows)
        assert analyser.singleCandleStickPattern(s) is False

    def test_candle_with_wick_returns_false(self):
        analyser = CandleStickAnalyser()
        # Long upper wick — not a marubozu
        row = {"Open": 100.0, "High": 104.0, "Low": 99.5, "Close": 101.5, "Volume": 100_000}
        rows = [row] * 3
        s = self._make_stock_with_positional_candles(rows)
        assert analyser.singleCandleStickPattern(s) is False


class TestDoubleCandlestickContinuationPattern:
    """2 consecutive rising / falling closes — TWO_CONT threshold=4 in positional stock."""

    def _make_stock_3rows(self, prev_prev_close, prev_close, curr_close, trend="up"):
        rows = []
        closes = [prev_prev_close, prev_close, curr_close]
        for i, c in enumerate(closes):
            if trend == "up":
                # Bullish candles: Open < Close
                row_open = c * 0.998
                row_close = c
            else:
                # Bearish candles: Open > Close
                row_open = c * 1.002
                row_close = c
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
        result = analyser.doubleCandleStickContinuationPattern(s)
        assert result is True
        assert "Double_candle_continuation_pattern" in s.analysis.get("BULLISH", {})

    def test_two_falling_bearish(self):
        analyser = CandleStickAnalyser()
        s = self._make_stock_3rows(100.0, 99.5, 94.5, trend="down")
        result = analyser.doubleCandleStickContinuationPattern(s)
        assert result is True
        assert "Double_candle_continuation_pattern" in s.analysis.get("BEARISH", {})

    def test_mixed_directions_returns_false(self):
        analyser = CandleStickAnalyser()
        s = self._make_stock_3rows(100.0, 103.0, 99.0)  # up then down
        assert analyser.doubleCandleStickContinuationPattern(s) is False


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
        result = analyser.tripleCandleStickContinuationPattern(s)
        assert result is True
        assert "Triple_candle_continuation_pattern" in s.analysis.get("BULLISH", {})

    def test_three_falling_bearish(self):
        analyser = CandleStickAnalyser()
        # prevprev.Open = 100*1.002=100.2, curr.Close=93.0 → |change|=(100.2-93)/100.2≈7.2% ≥ 5%
        s = self._make_stock_5rows([102.0, 101.0, 100.0, 97.0, 93.0], trend="down")
        result = analyser.tripleCandleStickContinuationPattern(s)
        assert result is True
        assert "Triple_candle_continuation_pattern" in s.analysis.get("BEARISH", {})

    def test_two_up_one_down_returns_false(self):
        analyser = CandleStickAnalyser()
        s = self._make_stock_5rows([100.0, 102.0, 104.0, 106.0, 103.0], trend="up")
        assert analyser.tripleCandleStickContinuationPattern(s) is False


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
