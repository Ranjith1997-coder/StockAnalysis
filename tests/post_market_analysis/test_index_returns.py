"""Tests for post_market_analysis.index_returns — IndexReturnsSource."""
import pytest
import pandas as pd
from unittest.mock import patch, call
from post_market_analysis.index_returns import IndexReturnsSource
from tests.post_market_analysis.conftest import mock_response


class TestIndexReturnsSource:
    def _split_raw(self, raw, per_page=10):
        """Split 20-item fixture into two pages."""
        return raw[:per_page], raw[per_page:]

    def test_fetch_raw_calls_requests_twice(self, sample_index_returns_raw):
        p1, p2 = self._split_raw(sample_index_returns_raw)
        src = IndexReturnsSource()
        with patch("requests.get", side_effect=[
            mock_response(p1, 200),
            mock_response(p2, 200),
        ]) as mock_get:
            with patch("time.sleep"):
                src.fetch_raw()
        assert mock_get.call_count == 2

    def test_fetch_raw_aggregates_both_pages(self, sample_index_returns_raw):
        p1, p2 = self._split_raw(sample_index_returns_raw)
        src = IndexReturnsSource()
        with patch("requests.get", side_effect=[
            mock_response(p1, 200),
            mock_response(p2, 200),
        ]):
            with patch("time.sleep"):
                result = src.fetch_raw()
        assert len(result) == len(sample_index_returns_raw)

    def test_fetch_raw_retries_failed_page(self, sample_index_returns_raw):
        """First call to page 1 fails, second succeeds."""
        p1, p2 = self._split_raw(sample_index_returns_raw)
        src = IndexReturnsSource()
        with patch("requests.get", side_effect=[
            mock_response(None, 500),   # page 1 first attempt fails
            mock_response(p1, 200),     # page 1 retry succeeds
            mock_response(p2, 200),     # page 2 succeeds
        ]):
            with patch("time.sleep"):
                result = src.fetch_raw()
        assert len(result) == len(sample_index_returns_raw)

    def test_fetch_raw_raises_after_all_retries_exhausted(self):
        src = IndexReturnsSource()
        with patch("requests.get", return_value=mock_response(None, 500)):
            with patch("time.sleep"):
                with pytest.raises(RuntimeError, match="Failed to fetch index data page"):
                    src.fetch_raw()

    def test_fetch_raw_sleeps_between_pages(self, sample_index_returns_raw):
        """time.sleep(0.5) called between page 1 and page 2."""
        p1, p2 = self._split_raw(sample_index_returns_raw)
        src = IndexReturnsSource()
        with patch("requests.get", side_effect=[
            mock_response(p1, 200),
            mock_response(p2, 200),
        ]):
            with patch("time.sleep") as mock_sleep:
                src.fetch_raw()
        # 0.5s sleep after each page (2 calls); retry sleeps may also occur
        sleep_args = [c.args[0] for c in mock_sleep.call_args_list]
        assert 0.5 in sleep_args

    # ── normalize ─────────────────────────────────────────────────────────────

    def test_normalize_returns_dataframe(self, sample_index_returns_raw):
        src = IndexReturnsSource()
        df = src.normalize(sample_index_returns_raw)
        assert isinstance(df, pd.DataFrame)

    def test_normalize_numeric_columns_are_float(self, sample_index_returns_raw):
        src = IndexReturnsSource()
        df = src.normalize(sample_index_returns_raw)
        for col in ["Open", "High", "Low", "Close", "PreviousClose", "Change", "ChangePercentage"]:
            assert pd.api.types.is_numeric_dtype(df[col]), f"{col} is not numeric"

    def test_normalize_handles_malformed_numeric_as_nan(self):
        """Unparseable numeric strings become NaN, not crash."""
        raw = [{"SecurityName": "IDX", "ChangePercentage": "bad_val", "Change": "N/A",
                "Close": None, "Open": None, "High": None, "Low": None, "PreviousClose": None}]
        src = IndexReturnsSource()
        df = src.normalize(raw)
        import math
        assert df["ChangePercentage"].isna().all() or math.isnan(df["ChangePercentage"].iloc[0])

    def test_normalize_empty_raw_returns_empty_dataframe(self):
        src = IndexReturnsSource()
        df = src.normalize([])
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_normalize_row_count_matches_input(self, sample_index_returns_raw):
        src = IndexReturnsSource()
        df = src.normalize(sample_index_returns_raw)
        assert len(df) == len(sample_index_returns_raw)

    def test_run_adds_source_column(self, sample_index_returns_raw):
        p1, p2 = self._split_raw(sample_index_returns_raw)
        src = IndexReturnsSource()
        with patch("requests.get", side_effect=[
            mock_response(p1, 200),
            mock_response(p2, 200),
        ]):
            with patch("time.sleep"):
                df = src.run()
        assert "source" in df.columns
        assert (df["source"] == "index_returns").all()
