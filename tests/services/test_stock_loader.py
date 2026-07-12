"""Tests for services/common/stock_loader.py — Redis → Stock reconstruction."""
import json
import pandas as pd
import pytest
from unittest.mock import MagicMock

from common.Stock import Stock


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_price_df(rows=3):
    return pd.DataFrame({
        "Open": [100 + i for i in range(rows)],
        "High": [105 + i for i in range(rows)],
        "Low": [95 + i for i in range(rows)],
        "Close": [102 + i for i in range(rows)],
        "Volume": [100000 * (i + 1) for i in range(rows)],
    })


def _mock_redis_hgetall(mapping_by_key: dict):
    """Create a MagicMock Redis where hgetall returns the dict for the matching key.

    Args:
        mapping_by_key: {"data:price:RELIANCE": {...}, "data:sensibull:RELIANCE": {...}, ...}
    """
    redis = MagicMock()

    def hgetall_side_effect(key):
        return mapping_by_key.get(key, {})

    redis.hgetall.side_effect = hgetall_side_effect
    return redis


# ═══════════════════════════════════════════════════════════════════════════
# load_stock_from_redis
# ═══════════════════════════════════════════════════════════════════════════

class TestLoadStockFromRedis:
    """Test load_stock_from_redis()."""

    def test_returns_none_when_no_data(self):
        from services.common.stock_loader import load_stock_from_redis
        redis = MagicMock()
        redis.hgetall.return_value = {}
        result = load_stock_from_redis(redis, "MISSING")
        assert result is None

    def test_loads_price_data_dataframe(self):
        from services.common.stock_loader import load_stock_from_redis
        df = _make_price_df(3)
        redis = _mock_redis_hgetall({
            "data:price:RELIANCE": {"priceData_json": df.to_json(orient="split")},
        })
        stock = load_stock_from_redis(redis, "RELIANCE")
        assert stock is not None
        assert not stock.priceData.empty
        assert len(stock.priceData) == 3
        assert "Close" in stock.priceData.columns

    def test_loads_ltp_and_change(self):
        from services.common.stock_loader import load_stock_from_redis
        redis = _mock_redis_hgetall({
            "data:price:RELIANCE": {"priceData_json": "{}", "ltp": "24500.5", "ltp_change_perc": "1.25"},
        })
        stock = load_stock_from_redis(redis, "RELIANCE")
        assert stock.ltp == 24500.5
        assert stock.ltp_change_perc == 1.25

    def test_loads_prev_day_ohlcv(self):
        from services.common.stock_loader import load_stock_from_redis
        prev = {"OPEN": 100, "HIGH": 105, "LOW": 95, "CLOSE": 102, "VOLUME": 500000}
        redis = _mock_redis_hgetall({
            "data:price:RELIANCE": {"priceData_json": "{}", "prevDayOHLCV_json": json.dumps(prev)},
        })
        stock = load_stock_from_redis(redis, "RELIANCE")
        assert stock.prevDayOHLCV is not None
        assert stock.prevDayOHLCV["CLOSE"] == 102

    def test_loads_daily_hv(self):
        from services.common.stock_loader import load_stock_from_redis
        redis = _mock_redis_hgetall({
            "data:price:RELIANCE": {"priceData_json": "{}", "daily_hv": "18.5"},
        })
        stock = load_stock_from_redis(redis, "RELIANCE")
        assert stock.daily_hv == 18.5

    def test_handles_missing_fields_gracefully(self):
        from services.common.stock_loader import load_stock_from_redis
        redis = _mock_redis_hgetall({
            "data:price:RELIANCE": {"priceData_json": "{}"},
        })
        stock = load_stock_from_redis(redis, "RELIANCE")
        assert stock is not None
        assert stock.ltp is None
        assert stock.ltp_change_perc is None
        assert stock.prevDayOHLCV is None
        assert stock.daily_hv is None

    def test_handles_invalid_float_values(self):
        from services.common.stock_loader import load_stock_from_redis
        redis = _mock_redis_hgetall({
            "data:price:RELIANCE": {"priceData_json": "{}", "ltp": "abc", "ltp_change_perc": "None", "daily_hv": ""},
        })
        stock = load_stock_from_redis(redis, "RELIANCE")
        assert stock.ltp is None
        assert stock.ltp_change_perc is None
        assert stock.daily_hv is None

    def test_is_index_flag_preserved(self):
        from services.common.stock_loader import load_stock_from_redis
        redis = _mock_redis_hgetall({
            "data:price:NIFTY": {"priceData_json": "{}"},
        })
        stock = load_stock_from_redis(redis, "NIFTY", is_index=True)
        assert stock.is_index is True


# ═══════════════════════════════════════════════════════════════════════════
# load_price_data_from_redis
# ═══════════════════════════════════════════════════════════════════════════

class TestLoadPriceDataFromRedis:
    """Test load_price_data_from_redis()."""

    def test_updates_existing_stock_objects(self):
        from services.common.stock_loader import load_price_data_from_redis
        df = _make_price_df(2)
        price_hash = {"priceData_json": df.to_json(orient="split"), "ltp": "100.0"}

        redis = _mock_redis_hgetall({
            "data:price:STOCK1": price_hash,
            "data:price:INDEX1": price_hash,
        })

        s1 = Stock("Stock1", "STOCK1")
        i1 = Stock("Index1", "INDEX1", is_index=True)

        updated = load_price_data_from_redis(redis, [s1], [i1])
        assert updated == 2
        assert not s1.priceData.empty
        assert s1.ltp == 100.0
        assert not i1.priceData.empty

    def test_returns_count_of_updated_symbols(self):
        from services.common.stock_loader import load_price_data_from_redis
        df = _make_price_df(1)
        price_hash = {"priceData_json": df.to_json(orient="split")}

        redis = _mock_redis_hgetall({
            "data:price:S1": price_hash,
            "data:price:S2": price_hash,
            "data:price:I1": price_hash,
            "data:price:C1": price_hash,
            "data:price:G1": price_hash,
        })

        stocks = [Stock("s1", "S1"), Stock("s2", "S2")]
        indices = [Stock("i1", "I1", is_index=True)]
        commodities = [Stock("c1", "C1", is_index=True)]
        globals_ = [Stock("g1", "G1", is_index=True)]

        updated = load_price_data_from_redis(redis, stocks, indices, commodities, globals_)
        assert updated == 5

    def test_skips_symbols_with_no_redis_data(self):
        from services.common.stock_loader import load_price_data_from_redis
        df = _make_price_df(1)
        redis = _mock_redis_hgetall({
            "data:price:S1": {"priceData_json": df.to_json(orient="split")},
            # S2 not in mapping → hgetall returns {}
        })

        s1 = Stock("s1", "S1")
        s2 = Stock("s2", "S2")

        updated = load_price_data_from_redis(redis, [s1, s2], [])
        assert updated == 1
        assert not s1.priceData.empty
        assert s2.priceData.empty

    def test_commodity_and_global_optional(self):
        from services.common.stock_loader import load_price_data_from_redis
        df = _make_price_df(1)
        redis = _mock_redis_hgetall({
            "data:price:S1": {"priceData_json": df.to_json(orient="split")},
        })

        s1 = Stock("s1", "S1")
        updated = load_price_data_from_redis(redis, [s1], [])
        assert updated == 1


# ═══════════════════════════════════════════════════════════════════════════
# load_sensibull_from_redis
# ═══════════════════════════════════════════════════════════════════════════

class TestLoadSensibullFromRedis:
    """Test load_sensibull_from_redis()."""

    def test_returns_false_when_no_data(self):
        from services.common.stock_loader import load_sensibull_from_redis
        redis = MagicMock()
        redis.hgetall.return_value = {}
        stock = Stock("Test", "TEST")
        assert load_sensibull_from_redis(redis, stock) is False

    def test_returns_true_and_populates_ctx(self):
        from services.common.stock_loader import load_sensibull_from_redis
        redis = _mock_redis_hgetall({
            "data:sensibull:RELIANCE": {
                "last_fetch_time": "2026-07-12T10:00:00",
                "current_json": json.dumps({"underlying_info": {"spot": 100}}),
                "historical_data_json": _make_price_df(2).to_json(orient="split"),
                "oi_chain_json": json.dumps({"strikes": [100, 105]}),
                "oi_chain_history_json": json.dumps([{"snapshot": 1}]),
                "iv_chart_history_json": "{}",
                "oi_history_json": "{}",
            },
        })
        stock = Stock("Reliance", "RELIANCE")
        result = load_sensibull_from_redis(redis, stock)
        assert result is True
        assert stock.sensibull_ctx["last_fetch_time"] == "2026-07-12T10:00:00"
        assert stock.sensibull_ctx["current"]["underlying_info"]["spot"] == 100
        assert not stock.sensibull_ctx["historical_data"].empty
        assert stock.sensibull_ctx["oi_chain"] is not None
        assert len(stock.sensibull_ctx["oi_chain_history"]) == 1

    def test_loads_current_dict(self):
        from services.common.stock_loader import load_sensibull_from_redis
        redis = _mock_redis_hgetall({
            "data:sensibull:X": {"current_json": json.dumps({"stats": {"pcr": 0.9}})},
        })
        stock = Stock("X", "X")
        load_sensibull_from_redis(redis, stock)
        assert stock.sensibull_ctx["current"]["stats"]["pcr"] == 0.9

    def test_loads_oi_chain(self):
        from services.common.stock_loader import load_sensibull_from_redis
        redis = _mock_redis_hgetall({
            "data:sensibull:X": {"oi_chain_json": json.dumps({"strikes": [100, 200]})},
        })
        stock = Stock("X", "X")
        load_sensibull_from_redis(redis, stock)
        assert stock.sensibull_ctx["oi_chain"]["strikes"] == [100, 200]

    def test_loads_oi_chain_history(self):
        from services.common.stock_loader import load_sensibull_from_redis
        redis = _mock_redis_hgetall({
            "data:sensibull:X": {"oi_chain_history_json": json.dumps([{"a": 1}, {"a": 2}])},
        })
        stock = Stock("X", "X")
        load_sensibull_from_redis(redis, stock)
        assert len(stock.sensibull_ctx["oi_chain_history"]) == 2

    def test_skips_empty_current_json(self):
        from services.common.stock_loader import load_sensibull_from_redis
        redis = _mock_redis_hgetall({
            "data:sensibull:X": {"current_json": "{}"},
        })
        stock = Stock("X", "X")
        load_sensibull_from_redis(redis, stock)
        # current stays at default Stock init (dict with None values)
        assert stock.sensibull_ctx["current"]["underlying_info"] is None

    def test_handles_malformed_json_gracefully(self):
        from services.common.stock_loader import load_sensibull_from_redis
        redis = _mock_redis_hgetall({
            "data:sensibull:X": {
                "current_json": "{bad json",
                "oi_chain_history_json": "[also bad",
            },
        })
        stock = Stock("X", "X")
        # Should not raise
        result = load_sensibull_from_redis(redis, stock)
        assert result is True

    def test_loads_dataframes(self):
        from services.common.stock_loader import load_sensibull_from_redis
        hist_df = _make_price_df(5)
        iv_df = pd.DataFrame({"date": ["2026-01-01"], "iv_close": [20.5], "price_close": [100]})
        oi_df = pd.DataFrame({"date": ["2026-01-01"], "pcr": [0.9], "max_pain": [100]})

        redis = _mock_redis_hgetall({
            "data:sensibull:X": {
                "historical_data_json": hist_df.to_json(orient="split"),
                "iv_chart_history_json": iv_df.to_json(orient="split"),
                "oi_history_json": oi_df.to_json(orient="split"),
            },
        })
        stock = Stock("X", "X")
        load_sensibull_from_redis(redis, stock)
        assert len(stock.sensibull_ctx["historical_data"]) == 5
        assert len(stock.sensibull_ctx["iv_chart_history"]) == 1
        assert len(stock.sensibull_ctx["oi_history"]) == 1


# ═══════════════════════════════════════════════════════════════════════════
# load_zerodha_from_redis
# ═══════════════════════════════════════════════════════════════════════════

class TestLoadZerodhaFromRedis:
    """Test load_zerodha_from_redis()."""

    def test_returns_false_when_no_data(self):
        from services.common.stock_loader import load_zerodha_from_redis
        redis = MagicMock()
        redis.hgetall.return_value = {}
        stock = Stock("Test", "TEST")
        assert load_zerodha_from_redis(redis, stock) is False

    def test_returns_true_and_populates_futures_current(self):
        from services.common.stock_loader import load_zerodha_from_redis
        fut_df = pd.DataFrame({"close": [100, 101], "oi": [50000, 51000], "volume": [1000, 1200]})
        redis = _mock_redis_hgetall({
            "data:zerodha:RELIANCE": {
                "futures_data_current_json": fut_df.to_json(orient="split"),
            },
        })
        stock = Stock("Reliance", "RELIANCE")
        result = load_zerodha_from_redis(redis, stock)
        assert result is True
        assert not stock.zerodha_ctx["futures_data"]["current"].empty
        assert len(stock.zerodha_ctx["futures_data"]["current"]) == 2

    def test_loads_next_expiry_futures(self):
        from services.common.stock_loader import load_zerodha_from_redis
        curr_df = pd.DataFrame({"close": [100], "oi": [50000]})
        next_df = pd.DataFrame({"close": [102], "oi": [48000]})
        redis = _mock_redis_hgetall({
            "data:zerodha:X": {
                "futures_data_current_json": curr_df.to_json(orient="split"),
                "futures_data_next_json": next_df.to_json(orient="split"),
            },
        })
        stock = Stock("X", "X")
        load_zerodha_from_redis(redis, stock)
        assert len(stock.zerodha_ctx["futures_data"]["current"]) == 1
        assert len(stock.zerodha_ctx["futures_data"]["next"]) == 1

    def test_loads_futures_mdata(self):
        from services.common.stock_loader import load_zerodha_from_redis
        mdata = {"current": {"instrument_token": 12345, "expiry": "2026-07-30"}, "next": None}
        redis = _mock_redis_hgetall({
            "data:zerodha:X": {"futures_mdata_json": json.dumps(mdata, default=str)},
        })
        stock = Stock("X", "X")
        load_zerodha_from_redis(redis, stock)
        assert stock.zerodha_ctx["futures_mdata"]["current"] is not None
        assert stock.zerodha_ctx["futures_mdata"]["next"] is None

    def test_handles_empty_futures_data(self):
        from services.common.stock_loader import load_zerodha_from_redis
        redis = _mock_redis_hgetall({
            "data:zerodha:X": {"futures_data_current_json": "{}"},
        })
        stock = Stock("X", "X")
        load_zerodha_from_redis(redis, stock)
        assert stock.zerodha_ctx["futures_data"]["current"].empty

    def test_malformed_mdata_json_skipped(self):
        from services.common.stock_loader import load_zerodha_from_redis
        redis = _mock_redis_hgetall({
            "data:zerodha:X": {"futures_mdata_json": "{bad json"},
        })
        stock = Stock("X", "X")
        # Should not raise
        load_zerodha_from_redis(redis, stock)
        assert stock.zerodha_ctx["futures_mdata"]["current"] is None


# ═══════════════════════════════════════════════════════════════════════════
# load_options_live_from_redis
# ═══════════════════════════════════════════════════════════════════════════

class TestLoadOptionsLiveFromRedis:
    """Test load_options_live_from_redis()."""

    def test_returns_false_when_no_data(self):
        from services.common.stock_loader import load_options_live_from_redis
        redis = MagicMock()
        redis.hgetall.return_value = {}
        stock = Stock("Test", "TEST")
        assert load_options_live_from_redis(redis, stock) is False

    def test_parses_strike_ce_pe_keys(self):
        from services.common.stock_loader import load_options_live_from_redis
        redis = _mock_redis_hgetall({
            "data:options_live:NIFTY": {
                "24000_CE": json.dumps({"ltp": 100, "oi": 50000}),
                "24000_PE": json.dumps({"ltp": 95, "oi": 55000}),
                "24500_CE": json.dumps({"ltp": 80, "oi": 40000}),
            },
        })
        stock = Stock("NIFTY", "NIFTY", is_index=True)
        result = load_options_live_from_redis(redis, stock)
        assert result is True
        ol = stock._tick_store.options_live
        assert 24000.0 in ol
        assert ol[24000.0]["CE"]["ltp"] == 100
        assert ol[24000.0]["PE"]["oi"] == 55000
        assert 24500.0 in ol
        assert ol[24500.0]["CE"]["ltp"] == 80

    def test_handles_malformed_key_skipped(self):
        from services.common.stock_loader import load_options_live_from_redis
        redis = _mock_redis_hgetall({
            "data:options_live:X": {
                "garbage": "x",
                "24000_CE": json.dumps({"ltp": 100}),
            },
        })
        stock = Stock("X", "X")
        load_options_live_from_redis(redis, stock)
        ol = stock._tick_store.options_live
        assert 24000.0 in ol
        # garbage key was skipped (no "garbage" strike)
        assert "garbage" not in ol

    def test_returns_false_when_all_keys_malformed(self):
        from services.common.stock_loader import load_options_live_from_redis
        redis = _mock_redis_hgetall({
            "data:options_live:X": {"garbage": "x", "also_bad": "y"},
        })
        stock = Stock("X", "X")
        result = load_options_live_from_redis(redis, stock)
        assert result is False
        assert stock._tick_store.options_live == {}

    def test_populates_tick_store(self):
        from services.common.stock_loader import load_options_live_from_redis
        redis = _mock_redis_hgetall({
            "data:options_live:X": {"100_CE": json.dumps({"ltp": 50})},
        })
        stock = Stock("X", "X")
        load_options_live_from_redis(redis, stock)
        assert hasattr(stock, "_tick_store")
        assert 100.0 in stock._tick_store.options_live


# ═══════════════════════════════════════════════════════════════════════════
# load_tick_from_redis
# ═══════════════════════════════════════════════════════════════════════════

class TestLoadTickFromRedis:
    """Test load_tick_from_redis()."""

    def test_returns_false_when_no_data(self):
        from services.common.stock_loader import load_tick_from_redis
        redis = MagicMock()
        redis.hgetall.return_value = {}
        stock = Stock("Test", "TEST")
        assert load_tick_from_redis(redis, stock) is False

    def test_loads_equity_tick_into_zerodha_data(self):
        from services.common.stock_loader import load_tick_from_redis
        redis = _mock_redis_hgetall({
            "data:tick:RELIANCE": {
                "last_price": "2450.5",
                "volume_traded": "1000000",
                "open": "2440",
                "high": "2460",
                "low": "2435",
                "close": "2450",
                "total_buy_quantity": "50000",
                "total_sell_quantity": "60000",
                "average_traded_price": "2451.2",
                "change": "0.5",
            },
        })
        stock = Stock("Reliance", "RELIANCE")
        result = load_tick_from_redis(redis, stock)
        assert result is True
        zd = stock._tick_store._zerodha_data
        assert zd["last_price"] == 2450.5
        assert zd["volume_traded"] == 1000000.0
        assert zd["high"] == 2460.0

    def test_loads_options_aggregate(self):
        from services.common.stock_loader import load_tick_from_redis
        redis = _mock_redis_hgetall({
            "data:tick:NIFTY": {},
            "data:options_agg:NIFTY": {
                "live_pcr": "0.85",
                "atm_strike": "24000",
                "total_ce_oi": "5000000",
            },
        })
        stock = Stock("NIFTY", "NIFTY", is_index=True)
        load_tick_from_redis(redis, stock)
        agg = stock._tick_store.options_aggregate
        assert agg["live_pcr"] == 0.85
        assert agg["atm_strike"] == 24000.0
        assert agg["total_ce_oi"] == 5000000.0

    def test_numeric_conversion_in_aggregate(self):
        from services.common.stock_loader import load_tick_from_redis
        redis = _mock_redis_hgetall({
            "data:tick:X": {},
            "data:options_agg:X": {
                "live_pcr": "0.85",
                "regime": "long_gamma",
            },
        })
        stock = Stock("X", "X")
        load_tick_from_redis(redis, stock)
        agg = stock._tick_store.options_aggregate
        assert isinstance(agg["live_pcr"], float)
        assert agg["regime"] == "long_gamma"  # non-numeric kept as string

    def test_skips_empty_values_in_aggregate(self):
        from services.common.stock_loader import load_tick_from_redis
        redis = _mock_redis_hgetall({
            "data:tick:X": {},
            "data:options_agg:X": {
                "live_pcr": "",
                "atm_strike": "24000",
            },
        })
        stock = Stock("X", "X")
        load_tick_from_redis(redis, stock)
        agg = stock._tick_store.options_aggregate
        assert "live_pcr" not in agg or agg.get("live_pcr") != ""
        assert agg["atm_strike"] == 24000.0

    def test_combined_tick_and_options_live(self):
        from services.common.stock_loader import load_tick_from_redis
        redis = _mock_redis_hgetall({
            "data:tick:NIFTY": {"last_price": "24500"},
            "data:options_agg:NIFTY": {"live_pcr": "0.9"},
            "data:options_live:NIFTY": {"24000_CE": json.dumps({"ltp": 100})},
        })
        stock = Stock("NIFTY", "NIFTY", is_index=True)
        result = load_tick_from_redis(redis, stock)
        assert result is True
        assert stock._tick_store._zerodha_data["last_price"] == 24500.0
        assert stock._tick_store.options_aggregate["live_pcr"] == 0.9
        assert 24000.0 in stock._tick_store.options_live


# ═══════════════════════════════════════════════════════════════════════════
# _dict_to_df
# ═══════════════════════════════════════════════════════════════════════════

class TestDictToDf:
    """Test the _dict_to_df helper."""

    def test_none_returns_none(self):
        from services.common.stock_loader import _dict_to_df
        assert _dict_to_df(None) is None

    def test_dict_returns_single_row_df(self):
        from services.common.stock_loader import _dict_to_df
        result = _dict_to_df({"a": 1, "b": 2})
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1
        assert result.iloc[0]["a"] == 1

    def test_list_returns_multi_row_df(self):
        from services.common.stock_loader import _dict_to_df
        result = _dict_to_df([{"a": 1}, {"a": 2}])
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 2

    def test_other_type_passthrough(self):
        from services.common.stock_loader import _dict_to_df
        assert _dict_to_df("hello") == "hello"
        assert _dict_to_df(42) == 42
