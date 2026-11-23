"""Tests for lib/zerodha/futures_fetcher.py (audit 2026-11-23: 0 % covered).

The fetcher was production-reachable through ``Stock.get_futures_data`` but had
no test at all. These tests pin the parsing (spot map / basis), the retry path
and the intraday matching logic with a fake Kite client — no network.
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from lib.zerodha.futures_fetcher import FuturesFetcher

IST = "Asia/Kolkata"


def _candle(day: str, close: float, oi: int = 1_000, hour: int = 0) -> dict:
    stamp = pd.Timestamp(f"{day} {hour:02d}:00:00").tz_localize(IST)
    return {
        "date": stamp,
        "open": close - 5,
        "high": close + 10,
        "low": close - 10,
        "close": close,
        "volume": 10_000,
        "oi": oi,
    }


def _stock(price_rows=None, current_token=101, next_token=202, tz=IST):
    index = pd.DatetimeIndex(
        [pd.Timestamp(ts).tz_localize(tz) if tz else pd.Timestamp(ts) for ts, _ in (price_rows or [])]
    )
    price_data = pd.DataFrame(
        {"Close": [close for _, close in (price_rows or [])]}, index=index
    )
    stock = MagicMock()
    stock.stock_symbol = "NIFTY"
    stock.priceData = price_data
    stock.zerodha_ctx = {
        "futures_mdata": {
            "current": pd.DataFrame({"instrument_token": [current_token]}),
            "next": pd.DataFrame({"instrument_token": [next_token]}),
        },
        "futures_data": {"current": pd.DataFrame(), "next": pd.DataFrame()},
    }
    return stock


class TestCandlesToDf:
    def test_maps_fields_and_uses_spot_map_for_basis(self):
        candles = [_candle("2024-01-02", 20_100), _candle("2024-01-03", 20_200)]
        frame = FuturesFetcher._candles_to_df(
            candles, spot_map={"2024-01-02": 20_000.0, "2024-01-03": 20_050.0}
        )

        assert list(frame.columns) == [
            "open", "high", "low", "close", "volume", "oi", "underlying_price"
        ]
        assert frame["underlying_price"].tolist() == [20_000.0, 20_050.0]
        assert frame.index[0].hour == 5 and frame.index[0].minute == 30
        assert frame.index.tz is not None
        assert frame["oi"].tolist() == [1_000, 1_000]

    def test_falls_back_to_close_when_spot_missing(self):
        frame = FuturesFetcher._candles_to_df([_candle("2024-01-02", 20_100)], spot_map={})
        assert frame["underlying_price"].iloc[0] == 20_100

    def test_no_spot_map_still_works(self):
        frame = FuturesFetcher._candles_to_df([_candle("2024-01-02", 20_100)])
        assert frame["underlying_price"].iloc[0] == 20_100
        assert frame["close"].iloc[0] == 20_100

    def test_empty_history_returns_empty_frame(self):
        frame = FuturesFetcher._candles_to_df([])
        assert frame.empty


class TestCandleToRow:
    def test_row_carries_underlying_price(self):
        stamp = pd.Timestamp("2024-01-02 10:15", tz=IST)
        row = FuturesFetcher._candle_to_row(_candle("2024-01-02", 20_100, hour=10), stamp, 20_050.0)
        assert row["date"] is stamp
        assert row["underlying_price"] == 20_050.0
        assert row["close"] == 20_100
        assert row["oi"] == 1_000


class TestFetch:
    def test_unknown_mode_raises(self):
        fetcher = FuturesFetcher(MagicMock())
        with pytest.raises(ValueError, match="mode must be"):
            fetcher.fetch(_stock(), mode="weekly")

    def test_positional_fetches_current_and_next_with_basis(self):
        stock = _stock(price_rows=[("2024-01-02 15:30", 20_000.0), ("2024-01-03 15:30", 20_050.0)])
        kite = MagicMock()
        kite.historical_data.side_effect = [
            [_candle("2024-01-02", 20_100), _candle("2024-01-03", 20_200)],
            [_candle("2024-01-02", 20_500)],
        ]

        current, nxt = FuturesFetcher(kite).fetch(stock, mode="positional")

        assert len(current) == 2 and len(nxt) == 1
        assert current["underlying_price"].tolist() == [20_000.0, 20_050.0]
        assert stock.zerodha_ctx["futures_data"]["current"] is current
        assert stock.zerodha_ctx["futures_data"]["next"] is nxt

        first_call = kite.historical_data.call_args_list[0].kwargs
        assert first_call["instrument_token"] == 101
        assert first_call["interval"] == "day"
        assert first_call["oi"] is True
        assert first_call["continuous"] is False

    def test_positional_always_fetches_next_for_rollover(self):
        stock = _stock()
        kite = MagicMock()
        kite.historical_data.return_value = [_candle("2024-01-02", 20_100)]

        FuturesFetcher(kite).fetch(stock, mode="positional", is_next_expiry_required=False)

        assert kite.historical_data.call_count == 2

    def test_positional_retries_on_rate_limit(self):
        stock = _stock()
        kite = MagicMock()
        kite.historical_data.side_effect = [
            Exception("Too many requests"),
            [_candle("2024-01-02", 20_100)],
            [_candle("2024-01-02", 20_500)],
        ]
        with patch("lib.zerodha.futures_fetcher.time.sleep") as sleep:
            current, nxt = FuturesFetcher(kite).fetch(stock, mode="positional")

        assert len(current) == 1 and len(nxt) == 1
        sleep.assert_called_once_with(1)

    def test_positional_gives_up_after_three_rate_limits(self):
        stock = _stock()
        kite = MagicMock()
        kite.historical_data.side_effect = Exception("Too many requests")
        with patch("lib.zerodha.futures_fetcher.time.sleep"):
            with pytest.raises(Exception, match="Too many requests for futures token"):
                FuturesFetcher(kite).fetch(stock, mode="positional")

    def test_positional_other_error_is_reraised(self):
        stock = _stock()
        kite = MagicMock()
        kite.historical_data.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError, match="boom"):
            FuturesFetcher(kite).fetch(stock, mode="positional")

    def test_positional_without_current_metadata_leaves_empty_frame(self):
        stock = _stock()
        stock.zerodha_ctx["futures_mdata"]["current"] = pd.DataFrame()
        kite = MagicMock()
        kite.historical_data.return_value = [_candle("2024-01-02", 20_500)]

        current, nxt = FuturesFetcher(kite).fetch(stock, mode="positional")
        assert current.empty
        assert len(nxt) == 1

    def test_positional_empty_api_response_is_logged_and_empty(self):
        stock = _stock()
        kite = MagicMock()
        kite.historical_data.return_value = []
        current, _ = FuturesFetcher(kite).fetch(stock, mode="positional")
        assert current.empty


class TestIntradayFetch:
    def _today_stock(self, n_rows=3):
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        rows = [(f"{today} 09:{15 + 5 * i}", 20_000.0 + i) for i in range(n_rows)]
        return _stock(price_rows=rows)

    def test_appends_candle_matching_the_penultimate_bar(self):
        stock = self._today_stock()
        target = stock.priceData.index[-2]
        kite = MagicMock()
        kite.historical_data.return_value = [{
            "date": target,
            "open": 20_010.0, "high": 20_030.0, "low": 20_000.0,
            "close": 20_020.0, "volume": 500, "oi": 1_500,
        }]

        current, _ = FuturesFetcher(kite).fetch(stock, mode="intraday")

        assert len(current) == 1
        row = current.iloc[0]
        assert row["close"] == 20_020.0
        assert row["underlying_price"] == pytest.approx(stock.priceData.loc[target, "Close"])
        assert row["oi"] == 1_500

    def test_unmatched_candle_is_skipped(self):
        stock = self._today_stock()
        kite = MagicMock()
        kite.historical_data.return_value = [{
            "date": pd.Timestamp("2024-01-02 10:00", tz=IST),
            "open": 1, "high": 2, "low": 1, "close": 1, "volume": 1, "oi": 1,
        }]
        current, _ = FuturesFetcher(kite).fetch(stock, mode="intraday")
        assert current.empty

    def test_insufficient_intraday_rows_raises(self):
        stock = self._today_stock(n_rows=1)
        with pytest.raises(Exception, match="Not enough intraday data"):
            FuturesFetcher(MagicMock()).fetch(stock, mode="intraday")
