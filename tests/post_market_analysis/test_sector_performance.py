"""Tests for post_market_analysis.sector_performance — SectorPerformanceSource."""
import pytest
import pandas as pd
from unittest.mock import patch
from post_market_analysis.sector_performance import SectorPerformanceSource
from tests.post_market_analysis.conftest import mock_response


class TestSectorPerformanceSource:
    def test_fetch_raw_success_returns_json(self, sample_sector_raw):
        src = SectorPerformanceSource()
        with patch("requests.get", return_value=mock_response(sample_sector_raw, 200)):
            result = src.fetch_raw()
        assert result == sample_sector_raw

    def test_fetch_raw_retries_on_500_then_succeeds(self, sample_sector_raw):
        src = SectorPerformanceSource()
        fail = mock_response(None, 500)
        ok   = mock_response(sample_sector_raw, 200)
        with patch("requests.get", side_effect=[fail, ok]):
            with patch("time.sleep"):
                result = src.fetch_raw()
        assert result == sample_sector_raw

    def test_fetch_raw_raises_runtime_error_after_all_retries(self):
        src = SectorPerformanceSource()
        fail = mock_response(None, 500)
        with patch("requests.get", return_value=fail):
            with patch("time.sleep"):
                with pytest.raises(RuntimeError, match="Failed to fetch sector"):
                    src.fetch_raw()

    def test_fetch_raw_retries_on_connection_error(self, sample_sector_raw):
        src = SectorPerformanceSource()
        ok = mock_response(sample_sector_raw, 200)
        with patch("requests.get", side_effect=[ConnectionError("network down"), ok]):
            with patch("time.sleep"):
                result = src.fetch_raw()
        assert result == sample_sector_raw

    def test_fetch_raw_sleeps_between_retries(self):
        src = SectorPerformanceSource()
        fail = mock_response(None, 500)
        with patch("requests.get", return_value=fail):
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(RuntimeError):
                    src.fetch_raw()
        assert mock_sleep.call_count == src.RETRIES

    # ── normalize ─────────────────────────────────────────────────────────────

    def test_normalize_returns_dataframe(self, sample_sector_raw):
        src = SectorPerformanceSource()
        df = src.normalize(sample_sector_raw)
        assert isinstance(df, pd.DataFrame)

    def test_normalize_columns_present(self, sample_sector_raw):
        src = SectorPerformanceSource()
        df = src.normalize(sample_sector_raw)
        for col in ["ID", "Name", "McapZG", "Mcap", "StocksCount"]:
            assert col in df.columns

    def test_normalize_row_count_matches_input(self, sample_sector_raw):
        src = SectorPerformanceSource()
        df = src.normalize(sample_sector_raw)
        assert len(df) == len(sample_sector_raw)

    def test_normalize_empty_raw_returns_empty_dataframe(self):
        src = SectorPerformanceSource()
        df = src.normalize([])
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_run_adds_source_column(self, sample_sector_raw):
        src = SectorPerformanceSource()
        with patch("requests.get", return_value=mock_response(sample_sector_raw, 200)):
            df = src.run()
        assert "source" in df.columns
        assert (df["source"] == "sector_performance").all()
