"""Tests for common/Stock.py."""
import threading
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch

from common.Stock import Stock
from common.shared import Mode


# ── Factory helpers ───────────────────────────────────────────────────────────

def make_stock(symbol="RELIANCE", name="Reliance", index=False):
    s = Stock(name, symbol, is_index=index)
    s.set_prev_day_ohlcv(open=2800.0, close=2800.0, high=2850.0, low=2750.0, volume=1_000_000)
    return s


def _price_df(closes):
    return pd.DataFrame({"Close": closes, "Open": closes, "High": closes, "Low": closes, "Volume": [100_000] * len(closes)})


# ── __init__ ──────────────────────────────────────────────────────────────────

class TestStockInit:
    def test_stock_name_set(self):
        s = Stock("Reliance", "RELIANCE")
        assert s.stockName == "Reliance"

    def test_stock_symbol_set(self):
        s = Stock("Reliance", "RELIANCE")
        assert s.stock_symbol == "RELIANCE"

    def test_yfinance_symbol_default_ns(self):
        s = Stock("Reliance", "RELIANCE")
        assert s.stockSymbolYFinance == "RELIANCE.NS"

    def test_yfinance_symbol_custom(self):
        s = Stock("Nifty", "NIFTY", yfinanceSymbol="^NSEI")
        assert s.stockSymbolYFinance == "^NSEI"

    def test_is_index_false_default(self):
        s = Stock("Reliance", "RELIANCE")
        assert s.is_index is False

    def test_is_index_true_when_passed(self):
        s = Stock("Nifty", "NIFTY", is_index=True)
        assert s.is_index is True

    def test_price_data_empty_on_init(self):
        s = Stock("Reliance", "RELIANCE")
        assert s.priceData.empty is True

    def test_analysis_initial_state(self):
        s = Stock("Reliance", "RELIANCE")
        assert s.analysis["BULLISH"] == {}
        assert s.analysis["BEARISH"] == {}
        assert s.analysis["NEUTRAL"] == {}
        assert s.analysis["NoOfTrends"] == 0

    def test_options_aggregate_initialized(self):
        s = Stock("Reliance", "RELIANCE")
        assert s.options_aggregate["total_ce_oi"] == 0
        assert s.options_aggregate["total_pe_oi"] == 0
        assert s.options_aggregate["live_pcr"] == 0.0


# ── set_prev_day_ohlcv ────────────────────────────────────────────────────────

class TestSetPrevDayOhlcv:
    def test_stores_ohlcv(self):
        s = make_stock()
        assert s.prevDayOHLCV["OPEN"] == 2800.0
        assert s.prevDayOHLCV["CLOSE"] == 2800.0
        assert s.prevDayOHLCV["HIGH"] == 2850.0
        assert s.prevDayOHLCV["LOW"] == 2750.0
        assert s.prevDayOHLCV["VOLUME"] == 1_000_000


# ── priceData setter ──────────────────────────────────────────────────────────

class TestPriceDataSetter:
    def test_valid_dataframe_accepted(self):
        s = make_stock()
        df = _price_df([100.0, 101.0])
        s.priceData = df
        assert len(s.priceData) == 2

    def test_non_dataframe_raises_value_error(self):
        s = make_stock()
        with pytest.raises(ValueError):
            s.priceData = [1, 2, 3]

    def test_none_raises_value_error(self):
        s = make_stock()
        with pytest.raises(ValueError):
            s.priceData = None


# ── update_latest_data ────────────────────────────────────────────────────────

class TestUpdateLatestData:
    def test_empty_price_data_returns_early(self):
        s = make_stock()
        # DataFrame with Close column but zero rows — should return early without error
        s.priceData = pd.DataFrame({"Close": pd.Series([], dtype=float)})
        s.update_latest_data()  # must not raise

    def test_updates_ltp(self):
        s = make_stock()
        s.priceData = _price_df([2800.0, 2900.0])
        s.update_latest_data()
        assert s.ltp == pytest.approx(2900.0)

    def test_ltp_change_percentage_computed(self):
        s = make_stock()
        s.set_prev_day_ohlcv(open=2800.0, close=2800.0, high=2850.0, low=2750.0, volume=1_000_000)
        s.priceData = _price_df([2800.0, 2940.0])  # +5%
        s.update_latest_data()
        assert s.ltp_change_perc == pytest.approx(5.0)


# ── is_price_data_empty ───────────────────────────────────────────────────────

class TestIsPriceDataEmpty:
    def test_returns_true_when_empty(self):
        s = make_stock()
        assert s.is_price_data_empty() is True

    def test_returns_false_when_populated(self):
        s = make_stock()
        s.priceData = _price_df([100.0])
        assert s.is_price_data_empty() is False


# ── set_analysis ──────────────────────────────────────────────────────────────

class TestSetAnalysis:
    def test_adds_bullish_signal(self):
        s = make_stock()
        s.set_analysis("BULLISH", "RSI", {"value": 70})
        assert "RSI" in s.analysis["BULLISH"]

    def test_adds_bearish_signal(self):
        s = make_stock()
        s.set_analysis("BEARISH", "MACD", {"value": -5})
        assert "MACD" in s.analysis["BEARISH"]

    def test_adds_neutral_signal(self):
        s = make_stock()
        s.set_analysis("NEUTRAL", "VOLUME", {"value": 100})
        assert "VOLUME" in s.analysis["NEUTRAL"]

    def test_no_of_trends_increments_on_first_occurrence(self):
        s = make_stock()
        s.set_analysis("BULLISH", "RSI", {"value": 70})
        assert s.analysis["NoOfTrends"] == 1

    def test_no_of_trends_not_incremented_on_duplicate(self):
        s = make_stock()
        s.set_analysis("BULLISH", "RSI", {"first": True})
        s.set_analysis("BULLISH", "RSI", {"second": True})
        assert s.analysis["NoOfTrends"] == 1

    def test_duplicate_key_converts_to_list(self):
        s = make_stock()
        s.set_analysis("BULLISH", "RSI", {"first": True})
        s.set_analysis("BULLISH", "RSI", {"second": True})
        assert isinstance(s.analysis["BULLISH"]["RSI"], list)
        assert len(s.analysis["BULLISH"]["RSI"]) == 2

    def test_third_occurrence_appends_to_list(self):
        s = make_stock()
        s.set_analysis("BULLISH", "RSI", {"a": 1})
        s.set_analysis("BULLISH", "RSI", {"b": 2})
        s.set_analysis("BULLISH", "RSI", {"c": 3})
        assert len(s.analysis["BULLISH"]["RSI"]) == 3

    def test_52_week_high_appended_to_shared_list(self):
        import common.shared as shared
        s = make_stock()
        original = list(shared.ticker_52_week_high_list)
        s.set_analysis("BULLISH", "52-week-high", {"value": True})
        assert s in shared.ticker_52_week_high_list
        # Cleanup
        shared.ticker_52_week_high_list[:] = original

    def test_52_week_low_appended_to_shared_list(self):
        import common.shared as shared
        s = make_stock()
        original = list(shared.ticker_52_week_low_list)
        s.set_analysis("BEARISH", "52-week-low", {"value": True})
        assert s in shared.ticker_52_week_low_list
        shared.ticker_52_week_low_list[:] = original


# ── reset_analysis ────────────────────────────────────────────────────────────

class TestResetAnalysis:
    def test_clears_all_buckets(self):
        s = make_stock()
        s.set_analysis("BULLISH", "RSI", {})
        s.reset_analysis()
        assert s.analysis["BULLISH"] == {}
        assert s.analysis["BEARISH"] == {}
        assert s.analysis["NEUTRAL"] == {}

    def test_resets_no_of_trends(self):
        s = make_stock()
        s.set_analysis("BULLISH", "RSI", {})
        s.reset_analysis()
        assert s.analysis["NoOfTrends"] == 0

    def test_timestamp_reset_to_none(self):
        s = make_stock()
        s.set_analysis("BULLISH", "RSI", {})
        s.reset_analysis()
        assert s.analysis["Timestamp"] is None


# ── update_zerodha_data ───────────────────────────────────────────────────────

class TestUpdateZerodhaData:
    def _tick(self, ltp=3000.0):
        return {
            "last_price": ltp,
            "volume_traded": 50000,
            "average_traded_price": 2990.0,
            "last_traded_quantity": 10,
            "buy_quantity": 1000,
            "sell_quantity": 900,
            "ohlc": {"open": 2980.0, "high": 3010.0, "low": 2970.0, "close": 2995.0},
            "depth": {"buy": [], "sell": []},
        }

    def test_last_price_updated(self):
        s = make_stock()
        s.update_zerodha_data(self._tick(ltp=3050.0))
        assert s.zerodha_data["last_price"] == 3050.0

    def test_ohlc_mapped_correctly(self):
        s = make_stock()
        s.update_zerodha_data(self._tick())
        assert s.zerodha_data["open"] == 2980.0
        assert s.zerodha_data["high"] == 3010.0

    def test_thread_safe_concurrent_updates(self):
        s = make_stock()
        errors = []

        def update():
            try:
                for i in range(50):
                    s.update_zerodha_data(self._tick(ltp=float(3000 + i)))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=update) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert errors == []


# ── update_option_tick ────────────────────────────────────────────────────────

class TestUpdateOptionTick:
    def _tick(self, ltp=50.0, oi=1000):
        return {"last_price": ltp, "oi": oi, "volume": 500}

    def test_option_stored_correctly(self):
        s = make_stock()
        s.update_option_tick(21000, "CE", self._tick())
        assert 21000 in s.options_live
        assert "CE" in s.options_live[21000]

    def test_ltp_stored(self):
        s = make_stock()
        s.update_option_tick(21000, "PE", self._tick(ltp=80.0))
        assert s.options_live[21000]["PE"]["ltp"] == 80.0

    def test_prev_oi_tracked_on_second_call(self):
        s = make_stock()
        s.update_option_tick(21000, "CE", self._tick(oi=1000))
        s.update_option_tick(21000, "CE", self._tick(oi=1200))
        assert s.options_live[21000]["CE"]["prev_oi"] == 1000


# ── update_futures_tick ───────────────────────────────────────────────────────

class TestUpdateFuturesTick:
    def _tick(self, ltp=21050.0, oi=5000):
        return {"last_price": ltp, "oi": oi, "volume": 200}

    def test_future_stored_correctly(self):
        s = make_stock()
        s.update_futures_tick("2025-07", self._tick())
        assert "2025-07" in s.futures_live

    def test_prev_oi_on_second_call(self):
        s = make_stock()
        s.update_futures_tick("2025-07", self._tick(oi=5000))
        s.update_futures_tick("2025-07", self._tick(oi=5500))
        assert s.futures_live["2025-07"]["prev_oi"] == 5000


# ── recompute_options_aggregate ───────────────────────────────────────────────

class TestRecomputeOptionsAggregate:
    def _add_options(self, stock, ce_oi=1000, pe_oi=1500, ce_ltp=50.0, pe_ltp=60.0,
                     ce_strike=21000, pe_strike=21000):
        stock.options_live.setdefault(ce_strike, {})
        stock.options_live[ce_strike]["CE"] = {"oi": ce_oi, "ltp": ce_ltp, "prev_oi": 0}
        stock.options_live.setdefault(pe_strike, {})
        stock.options_live[pe_strike]["PE"] = {"oi": pe_oi, "ltp": pe_ltp, "prev_oi": 0}

    def test_total_ce_oi_computed(self):
        s = make_stock()
        self._add_options(s, ce_oi=2000, pe_oi=1000)
        s.recompute_options_aggregate(spot_price=21000.0)
        assert s.options_aggregate["total_ce_oi"] == 2000

    def test_total_pe_oi_computed(self):
        s = make_stock()
        self._add_options(s, ce_oi=2000, pe_oi=3000)
        s.recompute_options_aggregate(spot_price=21000.0)
        assert s.options_aggregate["total_pe_oi"] == 3000

    def test_live_pcr_computed(self):
        s = make_stock()
        self._add_options(s, ce_oi=2000, pe_oi=3000)
        s.recompute_options_aggregate(spot_price=21000.0)
        assert s.options_aggregate["live_pcr"] == pytest.approx(1.5)

    def test_pcr_zero_when_ce_oi_zero(self):
        s = make_stock()
        self._add_options(s, ce_oi=0, pe_oi=1000)
        s.recompute_options_aggregate(spot_price=21000.0)
        assert s.options_aggregate["live_pcr"] == 0.0

    def test_atm_strike_nearest_to_spot(self):
        s = make_stock()
        s.options_live[20900] = {"CE": {"oi": 500, "ltp": 200, "prev_oi": 0}}
        s.options_live[21000] = {"CE": {"oi": 1000, "ltp": 100, "prev_oi": 0}}
        s.options_live[21100] = {"CE": {"oi": 300, "ltp": 20, "prev_oi": 0}}
        s.recompute_options_aggregate(spot_price=21001.0)  # unambiguously closer to 21000
        assert s.options_aggregate["atm_strike"] == 21000

    def test_atm_straddle_premium(self):
        s = make_stock()
        self._add_options(s, ce_ltp=100.0, pe_ltp=80.0)
        s.recompute_options_aggregate(spot_price=21000.0)
        assert s.options_aggregate["atm_straddle_premium"] == pytest.approx(180.0)

    def test_empty_options_live_returns_early(self):
        s = make_stock()
        s.recompute_options_aggregate(spot_price=21000.0)
        assert s.options_aggregate["total_ce_oi"] == 0


# ── check_52_week_status ──────────────────────────────────────────────────────

class TestCheck52WeekStatus:
    def test_new_high_returns_1(self):
        s = make_stock()
        # Need 253 rows: rolling(252) on shift(1) requires 253 rows to have non-NaN at last pos
        closes = [float(i) for i in range(1, 254)]  # 1..253, last=253 is new high
        s.priceData = _price_df(closes)
        result = s.check_52_week_status()
        assert result == 1

    def test_new_low_returns_minus_1(self):
        s = make_stock()
        closes = [float(i) for i in range(253, 0, -1)]  # 253 values, last=1 is new low
        s.priceData = _price_df(closes)
        result = s.check_52_week_status()
        assert result == -1

    def test_neither_high_nor_low_returns_0(self):
        s = make_stock()
        # 253 rows: flat at 100 with a spike in the middle
        closes = [100.0] * 126 + [200.0] + [100.0] * 126
        s.priceData = _price_df(closes)
        result = s.check_52_week_status()
        assert result == 0

    def test_insufficient_data_returns_0(self):
        s = make_stock()
        s.priceData = _price_df([100.0, 101.0])
        result = s.check_52_week_status()
        assert result == 0


# ── current_equity_data ───────────────────────────────────────────────────────

class TestCurrentEquityData:
    def test_intraday_returns_second_last_row(self):
        s = make_stock()
        s.priceData = _price_df([100.0, 200.0, 300.0])
        with patch("common.Stock.shared") as m:
            m.app_ctx.mode.name = "INTRADAY"
            m.Mode.INTRADAY.name = "INTRADAY"
            row = s.current_equity_data
        assert row["Close"] == pytest.approx(200.0)

    def test_positional_returns_last_row(self):
        s = make_stock()
        s.priceData = _price_df([100.0, 200.0, 300.0])
        with patch("common.Stock.shared") as m:
            m.app_ctx.mode.name = "POSITIONAL"
            m.Mode.INTRADAY.name = "INTRADAY"  # names differ → falls to else
            row = s.current_equity_data
        assert row["Close"] == pytest.approx(300.0)

    def test_returns_none_when_not_enough_rows_intraday(self):
        s = make_stock()
        s.priceData = _price_df([100.0])  # Only 1 row — can't return iloc[-2]
        with patch("common.Stock.shared") as m:
            m.app_ctx.mode.name = "INTRADAY"
            m.Mode.INTRADAY.name = "INTRADAY"
            row = s.current_equity_data
        assert row is None
