"""Tests for tools/pinn_volatility/data/bhavcopy_fetcher.py — Step 2 of the
PINN plan.

Schema/URL used here was verified against a real, live NSE Bhavcopy file
downloaded on 2026-08-16 (see conversation) -- these tests use a small
synthetic CSV built from that real column schema, not the plan doc's
(stale/404ing) assumed schema.
"""

import io
import zipfile
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest
import requests

import tools.pinn_volatility.data.bhavcopy_fetcher as bhavcopy_fetcher
from tools.pinn_volatility.data.bhavcopy_fetcher import (
    bhavcopy_url, fetch_bhavcopy, fetch_recent_bhavcopies, extract_index_rows, SYMBOLS,
)


@pytest.fixture
def weekdays_only(monkeypatch):
    """Trading-day logic itself lives in common.market_calendar (already has
    its own test coverage in tests/common/test_market_calendar.py) -- tests
    here that only care about fetch_recent_bhavcopies' walk-back/network-call
    behavior mock is_trading_day() down to plain weekday-only, so they're not
    coupled to real NSE holiday data."""
    monkeypatch.setattr(bhavcopy_fetcher, "is_trading_day", lambda d: d.weekday() < 5)

# Minimal columns needed by extract_index_rows() + what dataset.py (Step 3)
# will need -- matches the real schema's names exactly.
_SAMPLE_ROWS = [
    # NIFTY index option (IDO) -- should be kept
    dict(TradDt="2026-08-14", FinInstrmTp="IDO", TckrSymb="NIFTY",
         XpryDt="2026-08-21", StrkPric=24000.0, OptnTp="CE",
         SttlmPric=150.5, TtlTradgVol=1000, OpnIntrst=5000, UndrlygPric=24100.0),
    # NIFTY index future (IDF) -- should be kept (gives forward price)
    dict(TradDt="2026-08-14", FinInstrmTp="IDF", TckrSymb="NIFTY",
         XpryDt="2026-08-27", StrkPric=0.0, OptnTp="",
         SttlmPric=24150.0, TtlTradgVol=2000, OpnIntrst=8000, UndrlygPric=24100.0),
    # BANKNIFTY index option -- should be kept
    dict(TradDt="2026-08-14", FinInstrmTp="IDO", TckrSymb="BANKNIFTY",
         XpryDt="2026-08-26", StrkPric=51000.0, OptnTp="PE",
         SttlmPric=300.0, TtlTradgVol=500, OpnIntrst=1200, UndrlygPric=51200.0),
    # A stock option (STO) -- should be filtered out
    dict(TradDt="2026-08-14", FinInstrmTp="STO", TckrSymb="ABCAPITAL",
         XpryDt="2026-08-25", StrkPric=330.0, OptnTp="PE",
         SttlmPric=0.10, TtlTradgVol=37200, OpnIntrst=100, UndrlygPric=401.0),
    # A stock future (STF) -- should be filtered out
    dict(TradDt="2026-08-14", FinInstrmTp="STF", TckrSymb="ABCAPITAL",
         XpryDt="2026-08-25", StrkPric=0.0, OptnTp="",
         SttlmPric=402.0, TtlTradgVol=1000, OpnIntrst=200, UndrlygPric=401.0),
]


def _sample_csv_bytes() -> bytes:
    df = pd.DataFrame(_SAMPLE_ROWS)
    return df.to_csv(index=False).encode("utf-8")


def _sample_zip_bytes(inner_filename="BhavCopy_NSE_FO_0_0_0_20260814_F_0000.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_filename, _sample_csv_bytes())
    return buf.getvalue()


class TestBhavcopyUrl:
    def test_url_format(self):
        url = bhavcopy_url(date(2026, 8, 14))
        assert url == "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20260814_F_0000.csv.zip"


class TestFetchBhavcopy:
    def test_cache_hit_skips_download(self, tmp_path):
        cache_dir = str(tmp_path)
        cache_file = tmp_path / "20260814.csv"
        pd.DataFrame(_SAMPLE_ROWS).to_csv(cache_file, index=False)

        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.requests.get") as mock_get:
            df = fetch_bhavcopy(date(2026, 8, 14), cache_dir=cache_dir)
            mock_get.assert_not_called()

        assert df is not None
        assert len(df) == len(_SAMPLE_ROWS)

    def test_successful_download_parses_and_caches(self, tmp_path):
        cache_dir = str(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = _sample_zip_bytes()
        mock_resp.raise_for_status = MagicMock()

        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.requests.get",
                   return_value=mock_resp) as mock_get:
            df = fetch_bhavcopy(date(2026, 8, 14), cache_dir=cache_dir)
            mock_get.assert_called_once()

        assert df is not None
        assert len(df) == len(_SAMPLE_ROWS)
        # Cache file should now exist for next time.
        assert (tmp_path / "20260814.csv").exists()

    def test_404_returns_none_without_retry(self, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.requests.get",
                   return_value=mock_resp) as mock_get:
            df = fetch_bhavcopy(date(2026, 8, 15), cache_dir=str(tmp_path))
            # 404 = not yet published -- should NOT retry (wastes time).
            assert mock_get.call_count == 1

        assert df is None

    def test_transient_error_retries_then_succeeds(self, tmp_path):
        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.content = _sample_zip_bytes()
        mock_resp_ok.raise_for_status = MagicMock()

        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.requests.get",
                   side_effect=[requests.exceptions.ConnectionError("boom"), mock_resp_ok]) as mock_get, \
             patch("tools.pinn_volatility.data.bhavcopy_fetcher.time.sleep"):
            df = fetch_bhavcopy(date(2026, 8, 14), cache_dir=str(tmp_path))
            assert mock_get.call_count == 2

        assert df is not None
        assert len(df) == len(_SAMPLE_ROWS)

    def test_exhausted_retries_returns_none(self, tmp_path):
        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.requests.get",
                   side_effect=requests.exceptions.ConnectionError("boom")) as mock_get, \
             patch("tools.pinn_volatility.data.bhavcopy_fetcher.time.sleep"):
            df = fetch_bhavcopy(date(2026, 8, 14), cache_dir=str(tmp_path))
            assert mock_get.call_count == 3  # MAX_RETRIES

        assert df is None

    def test_malformed_zip_returns_none(self, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"not a real zip file"
        mock_resp.raise_for_status = MagicMock()

        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.requests.get",
                   return_value=mock_resp), \
             patch("tools.pinn_volatility.data.bhavcopy_fetcher.time.sleep"):
            df = fetch_bhavcopy(date(2026, 8, 14), cache_dir=str(tmp_path))

        assert df is None


class TestExtractIndexRows:
    def test_keeps_only_index_options_and_futures_for_target_symbols(self):
        df = pd.DataFrame(_SAMPLE_ROWS)
        result = extract_index_rows(df, symbols=SYMBOLS)

        assert len(result) == 3  # NIFTY IDO, NIFTY IDF, BANKNIFTY IDO
        assert set(result["FinInstrmTp"]) <= {"IDO", "IDF"}
        assert set(result["TckrSymb"]) == {"NIFTY", "BANKNIFTY"}

    def test_excludes_stock_options_and_futures(self):
        df = pd.DataFrame(_SAMPLE_ROWS)
        result = extract_index_rows(df, symbols=SYMBOLS)
        assert "ABCAPITAL" not in set(result["TckrSymb"])

    def test_sensex_absent_from_default_symbols(self):
        """SENSEX is intentionally excluded (BSE product, not in NSE's file)
        -- confirm it's not in the default SYMBOLS list."""
        assert "SENSEX" not in SYMBOLS

    def test_custom_symbol_list(self):
        df = pd.DataFrame(_SAMPLE_ROWS)
        result = extract_index_rows(df, symbols=["NIFTY"])
        assert set(result["TckrSymb"]) == {"NIFTY"}

    def test_empty_result_when_no_match(self):
        df = pd.DataFrame(_SAMPLE_ROWS)
        result = extract_index_rows(df, symbols=["FINNIFTY"])
        assert len(result) == 0


class TestFetchRecentBhavcopies:
    """end_date=2026-08-17 is a Monday (Aug 14 = Friday, verified live
    earlier); Aug 15-16 are the weekend.

    All tests here use the `weekdays_only` fixture -- they test the
    weekend-skip + 404-tolerance walk-back logic specifically, independent
    of the NSE holiday-calendar optimization (covered separately below in
    TestNseHolidaySkipping)."""

    def test_skips_weekends_without_network_call(self, weekdays_only):
        called_dates = []

        def fake_fetch(d, cache_dir=None):
            called_dates.append(d)
            assert d.weekday() < 5  # must never be called on Sat/Sun
            return pd.DataFrame(_SAMPLE_ROWS)

        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.fetch_bhavcopy",
                   side_effect=fake_fetch):
            fetch_recent_bhavcopies(n_days=1, end_date=date(2026, 8, 17))

        assert called_dates == [date(2026, 8, 17)]

    def test_collects_requested_number_of_trading_days(self, weekdays_only):
        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.fetch_bhavcopy",
                   return_value=pd.DataFrame(_SAMPLE_ROWS)) as mock_fetch:
            result = fetch_recent_bhavcopies(n_days=3, end_date=date(2026, 8, 17))

        assert mock_fetch.call_count == 3  # Aug 17, 14, 13 (weekend skipped for free)
        assert len(result) == 3 * 3  # 3 index rows per sample day x 3 days

    def test_skips_missing_day_and_keeps_walking_back(self, weekdays_only):
        """Simulate an unscheduled closure the holiday calendar doesn't know
        about: one weekday returns None (same as a 404) -- the walk must
        keep going back until n_days successes are found."""
        holiday = date(2026, 8, 13)

        def fake_fetch(d, cache_dir=None):
            return None if d == holiday else pd.DataFrame(_SAMPLE_ROWS)

        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.fetch_bhavcopy",
                   side_effect=fake_fetch) as mock_fetch:
            result = fetch_recent_bhavcopies(n_days=3, end_date=date(2026, 8, 17))

        assert len(result) == 3 * 3  # still got 3 successful days
        attempted_dates = [c.args[0] for c in mock_fetch.call_args_list]
        assert holiday in attempted_dates  # it was tried, just didn't count

    def test_returns_empty_dataframe_when_nothing_found(self, weekdays_only):
        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.fetch_bhavcopy",
                   return_value=None):
            result = fetch_recent_bhavcopies(n_days=5, end_date=date(2026, 8, 17), max_days_back=10)
        assert result.empty

    def test_respects_max_days_back_safety_cap(self, weekdays_only):
        """An extended outage (fetch always returns None) must not walk back
        indefinitely -- bounded by max_days_back calendar days."""
        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.fetch_bhavcopy",
                   return_value=None) as mock_fetch:
            result = fetch_recent_bhavcopies(n_days=5, end_date=date(2026, 8, 17), max_days_back=10)

        # 10 calendar days back from Aug 17 includes 6 weekdays -> at most 6 calls.
        assert mock_fetch.call_count <= 6
        assert result.empty


class TestUsesSharedMarketCalendar:
    """fetch_recent_bhavcopies() must defer to common.market_calendar.is_trading_day()
    for BOTH weekend and holiday logic -- that module already has its own
    dedicated test coverage (tests/common/test_market_calendar.py) for the
    three-layer fallback (NSE API -> XNSE historical -> custom JSON overlay)
    and for real NSE holiday dates (e.g. 26-Jan-2026 Republic Day, verified
    live against the real API in an earlier version of this file -- see
    conversation). These tests only confirm fetch_recent_bhavcopies actually
    calls and respects is_trading_day(), not its internal correctness."""

    def test_known_holiday_skipped_without_network_call(self, monkeypatch):
        holiday = date(2026, 8, 13)  # a Thursday -- would otherwise be fetched

        def fake_is_trading_day(d):
            return d.weekday() < 5 and d != holiday

        monkeypatch.setattr(bhavcopy_fetcher, "is_trading_day", fake_is_trading_day)

        called_dates = []

        def fake_fetch(d, cache_dir=None):
            called_dates.append(d)
            return pd.DataFrame(_SAMPLE_ROWS)

        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.fetch_bhavcopy",
                   side_effect=fake_fetch):
            fetch_recent_bhavcopies(n_days=3, end_date=date(2026, 8, 17))

        assert holiday not in called_dates

    def test_is_trading_day_called_for_every_candidate_calendar_day(self, monkeypatch):
        checked_dates = []

        def fake_is_trading_day(d):
            checked_dates.append(d)
            return d.weekday() < 5

        monkeypatch.setattr(bhavcopy_fetcher, "is_trading_day", fake_is_trading_day)

        with patch("tools.pinn_volatility.data.bhavcopy_fetcher.fetch_bhavcopy",
                   return_value=pd.DataFrame(_SAMPLE_ROWS)):
            fetch_recent_bhavcopies(n_days=3, end_date=date(2026, 8, 17))

        # Aug 17 (Mon) down through Aug 13 (Thu) = 5 calendar days scanned
        # to collect 3 weekday trading days (Aug 15-16 weekend in between).
        assert checked_dates == [date(2026, 8, 17), date(2026, 8, 16), date(2026, 8, 15),
                                  date(2026, 8, 14), date(2026, 8, 13)]
