"""Tests for prevDayOHLCV Zerodha fallback when yfinance returns NaN."""
import json
import datetime
from unittest.mock import MagicMock, patch, patch as mock_patch
import pandas as pd
import numpy as np
import pytest


class TestRefreshGroupPrevDayReturnsNan:
    """Test that _refresh_group_prev_day collects NaN symbols."""

    def test_returns_nan_symbols(self):
        from services.data_gateway.yfinance_fetcher import _refresh_group_prev_day

        redis = MagicMock()
        obj_list = [
            {"tradingsymbol": "GOOD", "yfinancetradingsymbol": "GOOD.NS"},
            {"tradingsymbol": "BAD", "yfinancetradingsymbol": "BAD.NS"},
        ]

        # Build mock yfinance DataFrames matching real-world scenario:
        # Last bar = today (partial), second-to-last = prev day.
        # GOOD: prev day has valid Close. BAD: prev day has NaN Close.
        today = datetime.date.today()
        dates = pd.date_range("2026-07-01", periods=5, freq="B")
        # Make last date = today so expected_idx = -2
        dates = dates[:-1].append(pd.DatetimeIndex([pd.Timestamp(today)]))

        good_df = pd.DataFrame(
            {"Open": [1, 2, 3, 4, 5], "High": [1, 2, 3, 4, 5],
             "Low": [1, 2, 3, 4, 5], "Close": [1, 2, 3, 4, 5],
             "Volume": [100, 200, 300, 400, 500]},
            index=dates,
        )
        # BAD: index -2 (the prev day position) has NaN Close
        bad_close = [1, 2, 3, np.nan, 5]
        bad_df = pd.DataFrame(
            {"Open": [1, 2, 3, 7, 5], "High": [1, 2, 3, 7, 5],
             "Low": [1, 2, 3, 7, 5], "Close": bad_close,
             "Volume": [100, 200, 300, 400, 500]},
            index=dates,
        )

        mock_data = {"GOOD.NS": good_df, "BAD.NS": bad_df}

        with mock_patch("services.data_gateway.yfinance_fetcher.yf") as mock_yf:
            mock_yf.download.return_value = mock_data
            result = _refresh_group_prev_day(redis, obj_list, "stock")

        assert "BAD" in result
        assert "GOOD" not in result

    def test_returns_empty_when_all_valid(self):
        from services.data_gateway.yfinance_fetcher import _refresh_group_prev_day

        redis = MagicMock()
        obj_list = [
            {"tradingsymbol": "STOCK1", "yfinancetradingsymbol": "STOCK1.NS"},
        ]

        today = datetime.date.today()
        dates = pd.date_range("2026-07-01", periods=4, freq="B")
        dates = dates.append(pd.DatetimeIndex([pd.Timestamp(today)]))

        good_df = pd.DataFrame(
            {"Open": [1, 2, 3, 4, 5], "High": [1, 2, 3, 4, 5],
             "Low": [1, 2, 3, 4, 5], "Close": [1, 2, 3, 4, 5],
             "Volume": [100, 200, 300, 400, 500]},
            index=dates,
        )

        with mock_patch("services.data_gateway.yfinance_fetcher.yf") as mock_yf:
            mock_yf.download.return_value = {"STOCK1.NS": good_df}
            result = _refresh_group_prev_day(redis, obj_list, "stock")

        assert result == []

    def test_returns_empty_when_no_data(self):
        from services.data_gateway.yfinance_fetcher import _refresh_group_prev_day

        redis = MagicMock()
        with mock_patch("services.data_gateway.yfinance_fetcher.yf") as mock_yf:
            mock_yf.download.return_value = {}
            result = _refresh_group_prev_day(redis, [], "stock")

        assert result == []


class TestRefreshPrevDayOhlcvReturnsNan:
    """Test that refresh_prev_day_ohlcv returns aggregated NaN symbols."""

    def test_returns_nan_list(self):
        from services.data_gateway.yfinance_fetcher import refresh_prev_day_ohlcv

        redis = MagicMock()
        with mock_patch("services.data_gateway.yfinance_fetcher._refresh_group_prev_day") as mock_refresh:
            mock_refresh.side_effect = [["NAUKRI", "ABB"], [], [], []]
            with mock_patch("services.data_gateway.yfinance_fetcher.get_stock_objects_from_json") as mock_objs:
                mock_objs.return_value = ([], [], [], [])
                result = refresh_prev_day_ohlcv(redis)

        assert result == ["NAUKRI", "ABB"]


class TestZerodhaFallback:
    """Test ZerodhaFuturesManager.fetch_prev_day_ohlcv."""

    def _make_manager_with_enctoken(self):
        """Create a ZerodhaFuturesManager with enctoken set."""
        from services.data_gateway.zerodha_fetcher import ZerodhaFuturesManager

        redis = MagicMock()
        redis.hget.return_value = "fake_enctoken"
        redis.pubsub.return_value = MagicMock()

        with mock_patch("services.data_gateway.zerodha_fetcher.KiteConnect") as mock_kc:
            mgr = ZerodhaFuturesManager(redis, {})
            mgr._has_enctoken = True
            mgr._kc = MagicMock()
        return mgr, redis

    def test_fetches_and_writes_prevDayOHLCV(self):
        mgr, redis = self._make_manager_with_enctoken()

        today = datetime.date.today()
        candles = [
            {"date": datetime.datetime(2026, 7, 1), "open": 100, "high": 105,
             "low": 98, "close": 103, "volume": 1000, "oi": 0},
            {"date": datetime.datetime(2026, 7, 3), "open": 103, "high": 108,
             "low": 101, "close": 106, "volume": 1200, "oi": 0},
        ]
        mgr._kc.historical_data.return_value = candles

        token_map = {"NAUKRI": 12345}
        ok, fail = mgr.fetch_prev_day_ohlcv(redis, ["NAUKRI"], token_map)

        assert ok == 1
        assert fail == 0
        mgr._kc.historical_data.assert_called_once()
        call_kwargs = mgr._kc.historical_data.call_args[1]
        assert call_kwargs["instrument_token"] == 12345
        assert call_kwargs["interval"] == "day"
        assert call_kwargs["oi"] is False

        # Verify Redis write
        hset_call = redis.hset.call_args
        assert hset_call[0][0] == "data:price:NAUKRI"
        prev_day = json.loads(hset_call[1]["mapping"]["prevDayOHLCV_json"])
        assert prev_day["CLOSE"] == 106.0
        assert prev_day["OPEN"] == 103.0

    def test_skips_today_partial_bar(self):
        mgr, redis = self._make_manager_with_enctoken()

        today = datetime.date.today()
        candles = [
            {"date": datetime.datetime(2026, 7, 1), "open": 100, "high": 105,
             "low": 98, "close": 103, "volume": 1000, "oi": 0},
            {"date": datetime.datetime(2026, 7, 3), "open": 103, "high": 108,
             "low": 101, "close": 106, "volume": 1200, "oi": 0},
            {"date": datetime.datetime(today.year, today.month, today.day, 10, 0),
             "open": 106, "high": 110, "low": 105, "close": 108, "volume": 500, "oi": 0},
        ]
        mgr._kc.historical_data.return_value = candles

        token_map = {"NAUKRI": 12345}
        ok, fail = mgr.fetch_prev_day_ohlcv(redis, ["NAUKRI"], token_map)

        assert ok == 1
        hset_call = redis.hset.call_args
        prev_day = json.loads(hset_call[1]["mapping"]["prevDayOHLCV_json"])
        # Should use candles[-2] (Jul 3), not candles[-1] (today's partial)
        assert prev_day["CLOSE"] == 106.0

    def test_no_enctoken_returns_all_fail(self):
        from services.data_gateway.zerodha_fetcher import ZerodhaFuturesManager

        redis = MagicMock()
        redis.hget.return_value = None
        redis.pubsub.return_value = MagicMock()

        with mock_patch("services.data_gateway.zerodha_fetcher.KiteConnect"):
            mgr = ZerodhaFuturesManager(redis, {})
            mgr._has_enctoken = False
            mgr._kc = None

        ok, fail = mgr.fetch_prev_day_ohlcv(redis, ["NAUKRI", "ABB"], {})
        assert ok == 0
        assert fail == 2

    def test_403_triggers_refresh(self):
        mgr, redis = self._make_manager_with_enctoken()

        mgr._kc.historical_data.side_effect = Exception("403 Forbidden: Token expired")

        token_map = {"NAUKRI": 12345}
        with mock_patch.object(mgr, "request_refresh") as mock_refresh:
            ok, fail = mgr.fetch_prev_day_ohlcv(redis, ["NAUKRI"], token_map)

        assert ok == 0
        assert fail == 1
        mock_refresh.assert_called_once()

    def test_missing_token_counts_as_fail(self):
        mgr, redis = self._make_manager_with_enctoken()

        token_map = {}  # NAUKRI not in map
        ok, fail = mgr.fetch_prev_day_ohlcv(redis, ["NAUKRI"], token_map)

        assert ok == 0
        assert fail == 1
        mgr._kc.historical_data.assert_not_called()

    def test_empty_candles_counts_as_fail(self):
        mgr, redis = self._make_manager_with_enctoken()

        mgr._kc.historical_data.return_value = []

        token_map = {"NAUKRI": 12345}
        ok, fail = mgr.fetch_prev_day_ohlcv(redis, ["NAUKRI"], token_map)

        assert ok == 0
        assert fail == 1
        redis.hset.assert_not_called()

    def test_multiple_symbols(self):
        mgr, redis = self._make_manager_with_enctoken()

        candles = [
            {"date": datetime.datetime(2026, 7, 3), "open": 100, "high": 105,
             "low": 98, "close": 103, "volume": 1000, "oi": 0},
        ]
        mgr._kc.historical_data.return_value = candles

        token_map = {"NAUKRI": 12345, "ABB": 67890}
        ok, fail = mgr.fetch_prev_day_ohlcv(redis, ["NAUKRI", "ABB"], token_map)

        assert ok == 2
        assert fail == 0
        assert mgr._kc.historical_data.call_count == 2

    def test_generic_error_counts_as_fail_no_refresh(self):
        mgr, redis = self._make_manager_with_enctoken()

        mgr._kc.historical_data.side_effect = Exception("Network timeout")

        token_map = {"NAUKRI": 12345}
        with mock_patch.object(mgr, "request_refresh") as mock_refresh:
            ok, fail = mgr.fetch_prev_day_ohlcv(redis, ["NAUKRI"], token_map)

        assert ok == 0
        assert fail == 1
        mock_refresh.assert_not_called()
