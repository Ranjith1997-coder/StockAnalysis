"""Tests for post_market_analysis.fo_participant_oi — FoParticipantOISource."""
import pytest
import datetime
import pandas as pd
from unittest.mock import patch
from post_market_analysis.fo_participant_oi import FoParticipantOISource
from tests.post_market_analysis.conftest import mock_response


class TestFoParticipantOISource:
    def test_fetch_raw_success_returns_json(self, sample_fo_participant_raw):
        src = FoParticipantOISource()
        with patch("requests.get", return_value=mock_response(sample_fo_participant_raw, 200)):
            result = src.fetch_raw()
        assert result == sample_fo_participant_raw

    def test_fetch_raw_raises_on_http_error(self):
        """No retry logic — HTTP error propagates immediately via raise_for_status."""
        src = FoParticipantOISource()
        err_resp = mock_response(None, 500, raise_for_status=True)
        with patch("requests.get", return_value=err_resp):
            with pytest.raises(Exception):
                src.fetch_raw()

    def test_fetch_raw_calls_raise_for_status(self, sample_fo_participant_raw):
        src = FoParticipantOISource()
        resp = mock_response(sample_fo_participant_raw, 200)
        with patch("requests.get", return_value=resp):
            src.fetch_raw()
        resp.raise_for_status.assert_called_once()

    # ── normalize ─────────────────────────────────────────────────────────────

    def test_normalize_returns_dataframe(self, sample_fo_participant_raw):
        src = FoParticipantOISource()
        df = src.normalize(sample_fo_participant_raw)
        assert isinstance(df, pd.DataFrame)

    def test_normalize_date_column_is_date_type(self, sample_fo_participant_raw):
        src = FoParticipantOISource()
        df = src.normalize(sample_fo_participant_raw)
        for d in df["Date"].dropna():
            assert isinstance(d, datetime.date)

    def test_normalize_preserves_participant_columns(self, sample_fo_participant_raw):
        src = FoParticipantOISource()
        df = src.normalize(sample_fo_participant_raw)
        assert "FoParticipantTypeName" in df.columns
        assert "Net" in df.columns
        assert "Long" in df.columns
        assert "Short" in df.columns

    def test_normalize_row_count_matches_input(self, sample_fo_participant_raw):
        src = FoParticipantOISource()
        df = src.normalize(sample_fo_participant_raw)
        assert len(df) == len(sample_fo_participant_raw)

    def test_run_adds_source_column(self, sample_fo_participant_raw):
        src = FoParticipantOISource()
        with patch("requests.get", return_value=mock_response(sample_fo_participant_raw, 200)):
            df = src.run()
        assert "source" in df.columns
        assert (df["source"] == "fo_participant_oi").all()
