"""Tests for post_market_analysis.fii_dii — FiiDiiActivitySource."""
import pytest
import datetime
from unittest.mock import patch, MagicMock, call
from post_market_analysis.fii_dii import FiiDiiActivitySource
from tests.post_market_analysis.conftest import mock_response


class TestFiiDiiActivitySource:
    def test_fetch_raw_success_returns_json(self, sample_fii_dii_raw):
        src = FiiDiiActivitySource()
        with patch("requests.get", return_value=mock_response(sample_fii_dii_raw, 200)):
            result = src.fetch_raw()
        assert result == sample_fii_dii_raw

    def test_fetch_raw_retries_on_500_then_succeeds(self, sample_fii_dii_raw):
        src = FiiDiiActivitySource()
        fail = mock_response(None, 500)
        ok   = mock_response(sample_fii_dii_raw, 200)
        with patch("requests.get", side_effect=[fail, fail, ok]):
            with patch("time.sleep"):
                result = src.fetch_raw()
        assert result == sample_fii_dii_raw

    def test_fetch_raw_raises_runtime_error_after_all_retries(self):
        src = FiiDiiActivitySource()
        fail = mock_response(None, 500)
        with patch("requests.get", return_value=fail):
            with patch("time.sleep"):
                with pytest.raises(RuntimeError, match="Failed to fetch FII/DII"):
                    src.fetch_raw()

    def test_fetch_raw_retries_on_connection_error(self, sample_fii_dii_raw):
        src = FiiDiiActivitySource()
        ok = mock_response(sample_fii_dii_raw, 200)
        with patch("requests.get", side_effect=[ConnectionError("timeout"), ok]):
            with patch("time.sleep"):
                result = src.fetch_raw()
        assert result == sample_fii_dii_raw

    def test_fetch_raw_sleeps_between_retries(self):
        src = FiiDiiActivitySource()
        fail = mock_response(None, 500)
        with patch("requests.get", return_value=fail):
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(RuntimeError):
                    src.fetch_raw()
        # should have slept RETRIES times (one per iteration)
        assert mock_sleep.call_count == src.RETRIES

    # ── normalize ─────────────────────────────────────────────────────────────

    def test_normalize_returns_dataframe(self, sample_fii_dii_raw):
        import pandas as pd
        src = FiiDiiActivitySource()
        df = src.normalize(sample_fii_dii_raw)
        assert isinstance(df, pd.DataFrame)

    def test_normalize_columns_present(self, sample_fii_dii_raw):
        src = FiiDiiActivitySource()
        df = src.normalize(sample_fii_dii_raw)
        for col in ["date", "level", "category", "category_short", "instrument", "value"]:
            assert col in df.columns, f"Missing column: {col}"

    def test_normalize_child_rows_expanded(self, sample_fii_dii_raw):
        """Each ChildData entry becomes its own row with level='instrument'."""
        src = FiiDiiActivitySource()
        df = src.normalize(sample_fii_dii_raw)
        instrument_rows = df[df["level"] == "instrument"]
        assert len(instrument_rows) > 0

    def test_normalize_nifty_banknifty_child_rows_present(self, sample_fii_dii_raw):
        src = FiiDiiActivitySource()
        df = src.normalize(sample_fii_dii_raw)
        instr_shorts = df[df["level"] == "instrument"]["instrument_short"].tolist()
        assert "NIFTY" in instr_shorts
        assert "BANKNIFTY" in instr_shorts

    def test_normalize_date_parsed_to_date_type(self, sample_fii_dii_raw):
        src = FiiDiiActivitySource()
        df = src.normalize(sample_fii_dii_raw)
        for d in df["date"].dropna():
            assert isinstance(d, datetime.date)

    def test_normalize_empty_raw_returns_empty_dataframe(self):
        import pandas as pd
        src = FiiDiiActivitySource()
        df = src.normalize([])
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_normalize_close_price_rows_included(self, sample_fii_dii_raw):
        """ClosePrice entries appear as level='close_price' rows."""
        src = FiiDiiActivitySource()
        df = src.normalize(sample_fii_dii_raw)
        close_rows = df[df["level"] == "close_price"]
        assert len(close_rows) >= 1

    def test_run_adds_source_column(self, sample_fii_dii_raw):
        src = FiiDiiActivitySource()
        with patch("requests.get", return_value=mock_response(sample_fii_dii_raw, 200)):
            df = src.run()
        assert "source" in df.columns
        assert (df["source"] == "fii_dii_activity").all()
