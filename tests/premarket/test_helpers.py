"""Tests for pure helper functions: _safe_float, _chg, _extract_ticker_data."""
import pytest
import math
import pandas as pd
from premarket.premarket_report import _safe_float, _chg, _extract_ticker_data


# ── _safe_float ───────────────────────────────────────────────────────────────

class TestSafeFloat:
    def test_int_returns_float(self):
        assert _safe_float(42) == 42.0

    def test_plain_float_returned(self):
        assert _safe_float(3.14) == pytest.approx(3.14)

    def test_nan_returns_none(self):
        assert _safe_float(float("nan")) is None

    def test_pd_na_returns_none(self):
        assert _safe_float(float("nan")) is None

    def test_string_non_numeric_returns_none(self):
        assert _safe_float("abc") is None

    def test_none_returns_none(self):
        # None triggers float(None) → TypeError
        assert _safe_float(None) is None

    def test_series_single_element_extracts_value(self):
        s = pd.Series([5.0])
        result = _safe_float(s)
        assert result == pytest.approx(5.0)

    def test_series_element_nan_returns_none(self):
        s = pd.Series([float("nan")])
        result = _safe_float(s)
        assert result is None

    def test_zero_returns_zero(self):
        assert _safe_float(0) == 0.0


# ── _chg ──────────────────────────────────────────────────────────────────────

class TestChg:
    def test_positive_has_green_dot(self):
        result = _chg(1.5)
        assert "🟢" in result

    def test_positive_has_plus_prefix(self):
        result = _chg(1.5)
        assert "+" in result

    def test_positive_formatted_to_two_decimals(self):
        result = _chg(1.5)
        assert "1.50%" in result

    def test_negative_has_red_dot(self):
        result = _chg(-2.3)
        assert "🔴" in result

    def test_negative_no_plus_prefix(self):
        result = _chg(-2.3)
        assert "+" not in result

    def test_negative_formatted_to_two_decimals(self):
        result = _chg(-2.3)
        assert "2.30%" in result

    def test_zero_has_white_dot(self):
        result = _chg(0.0)
        assert "⚪" in result

    def test_zero_shows_zero_value(self):
        result = _chg(0.0)
        assert "0.00%" in result


# ── _extract_ticker_data ──────────────────────────────────────────────────────

class TestExtractTickerData:
    def test_none_data_returns_none(self):
        assert _extract_ticker_data(None, "^GSPC") is None

    def test_empty_dataframe_returns_none(self):
        assert _extract_ticker_data(pd.DataFrame(), "^GSPC") is None

    def test_single_ticker_no_multiindex_returned_as_is(self):
        df = pd.DataFrame({"Close": [100.0, 101.0], "Open": [99.0, 100.0]})
        result = _extract_ticker_data(df, "^GSPC")
        assert result is df

    def test_multiindex_level0_ticker_returns_correct_slice(self):
        tickers = ["^GSPC", "^IXIC"]
        dates = pd.date_range("2026-04-28", periods=2)
        arrays = pd.MultiIndex.from_product([tickers, ["Close", "Open"]], names=["Ticker", "Field"])
        df = pd.DataFrame(
            [[100.0, 99.0, 200.0, 198.0], [101.0, 100.0, 202.0, 200.0]],
            index=dates,
            columns=arrays,
        )
        # Rearrange so level-0 is ticker (group_by='ticker' shape)
        df = df.swaplevel(axis=1).sort_index(axis=1)
        result = _extract_ticker_data(df, "^GSPC")
        assert result is not None
        assert "Close" in result.columns

    def test_ticker_absent_returns_none(self):
        tickers = ["^GSPC"]
        dates = pd.date_range("2026-04-28", periods=2)
        cols = pd.MultiIndex.from_product([tickers, ["Close"]])
        df = pd.DataFrame([[100.0], [101.0]], index=dates, columns=cols)
        assert _extract_ticker_data(df, "MISSING") is None
