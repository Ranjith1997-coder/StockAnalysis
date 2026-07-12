from __future__ import annotations

import json
import pandas as pd
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from common.Stock import Stock
    from services.common.redis_proxy import RedisProxy

from services.common.serialization import (
    dataframe_from_json,
    safe_json_loads,
)


def load_stock_from_redis(redis: RedisProxy, symbol: str, is_index: bool = False) -> Stock | None:
    from common.Stock import Stock

    price_raw = redis.hgetall(f"data:price:{symbol}")
    if not price_raw:
        return None

    stock = Stock(symbol, symbol, is_index=is_index)

    price_data = dataframe_from_json(price_raw.get("priceData_json", "{}"))
    if not price_data.empty:
        stock.priceData = price_data

    prev_day_raw = price_raw.get("prevDayOHLCV_json", "")
    if prev_day_raw:
        try:
            stock.prevDayOHLCV = json.loads(prev_day_raw)
        except (json.JSONDecodeError, TypeError):
            pass

    ltp_str = price_raw.get("ltp", "")
    if ltp_str:
        try:
            stock.ltp = float(ltp_str)
        except (ValueError, TypeError):
            pass

    change_str = price_raw.get("ltp_change_perc", "")
    if change_str:
        try:
            stock.ltp_change_perc = float(change_str)
        except (ValueError, TypeError):
            pass

    hv_str = price_raw.get("daily_hv", "")
    if hv_str:
        try:
            stock.daily_hv = float(hv_str)
        except (ValueError, TypeError):
            pass

    return stock


def load_price_data_from_redis(
    redis: RedisProxy, stock_objs: list[Stock], index_objs: list[Stock],
    commodity_objs: list[Stock] | None = None,
    global_indices_objs: list[Stock] | None = None,
) -> int:
    updated = 0
    for stock in stock_objs:
        price_raw = redis.hgetall(f"data:price:{stock.stock_symbol}")
        if not price_raw:
            continue
        _apply_price_raw(stock, price_raw)
        updated += 1

    for index in index_objs:
        price_raw = redis.hgetall(f"data:price:{index.stock_symbol}")
        if not price_raw:
            continue
        _apply_price_raw(index, price_raw)
        updated += 1

    if commodity_objs:
        for commodity in commodity_objs:
            price_raw = redis.hgetall(f"data:price:{commodity.stock_symbol}")
            if not price_raw:
                continue
            _apply_price_raw(commodity, price_raw)
            updated += 1

    if global_indices_objs:
        for gi in global_indices_objs:
            price_raw = redis.hgetall(f"data:price:{gi.stock_symbol}")
            if not price_raw:
                continue
            _apply_price_raw(gi, price_raw)
            updated += 1

    return updated


def _apply_price_raw(stock: Stock, price_raw: dict[str, str]):
    price_data = dataframe_from_json(price_raw.get("priceData_json", "{}"))
    if not price_data.empty:
        stock.priceData = price_data

    last_update = price_raw.get("last_price_update", "")
    if last_update:
        stock.last_price_update = last_update

    prev_day_raw = price_raw.get("prevDayOHLCV_json", "")
    if prev_day_raw:
        try:
            stock.prevDayOHLCV = json.loads(prev_day_raw)
        except (json.JSONDecodeError, TypeError):
            pass

    ltp_str = price_raw.get("ltp", "")
    if ltp_str:
        try:
            stock.ltp = float(ltp_str)
        except (ValueError, TypeError):
            pass

    change_str = price_raw.get("ltp_change_perc", "")
    if change_str:
        try:
            stock.ltp_change_perc = float(change_str)
        except (ValueError, TypeError):
            pass

    hv_str = price_raw.get("daily_hv", "")
    if hv_str:
        try:
            stock.daily_hv = float(hv_str)
        except (ValueError, TypeError):
            pass


def load_sensibull_from_redis(redis: RedisProxy, stock: Stock) -> bool:
    sensibull_raw = redis.hgetall(f"data:sensibull:{stock.stock_symbol}")
    if not sensibull_raw:
        return False

    ctx = stock.sensibull_ctx
    ctx["last_fetch_time"] = sensibull_raw.get("last_fetch_time")

    current_json = sensibull_raw.get("current_json", "{}")
    if current_json != "{}":
        try:
            ctx["current"] = json.loads(current_json)
        except (json.JSONDecodeError, TypeError):
            pass

    ctx["historical_data"] = dataframe_from_json(
        sensibull_raw.get("historical_data_json", "{}")
    )

    oi_chain_raw = sensibull_raw.get("oi_chain_json", "null")
    if oi_chain_raw != "null":
        ctx["oi_chain"] = safe_json_loads(oi_chain_raw)

    hist_list_raw = sensibull_raw.get("oi_chain_history_json", "[]")
    if hist_list_raw and hist_list_raw != "[]":
        try:
            ctx["oi_chain_history"] = json.loads(hist_list_raw)
        except (json.JSONDecodeError, TypeError):
            pass

    ctx["iv_chart_history"] = dataframe_from_json(
        sensibull_raw.get("iv_chart_history_json", "{}")
    )
    ctx["oi_history"] = dataframe_from_json(
        sensibull_raw.get("oi_history_json", "{}")
    )

    return True


def load_zerodha_from_redis(redis: RedisProxy, stock: Stock) -> bool:
    zerodha_raw = redis.hgetall(f"data:zerodha:{stock.stock_symbol}")
    if not zerodha_raw:
        return False

    ctx = stock.zerodha_ctx

    futures_current_raw = zerodha_raw.get("futures_data_current_json", "")
    if futures_current_raw:
        ctx["futures_data"]["current"] = dataframe_from_json(futures_current_raw)

    futures_next_raw = zerodha_raw.get("futures_data_next_json", "")
    if futures_next_raw:
        ctx["futures_data"]["next"] = dataframe_from_json(futures_next_raw)

    futures_mdata_raw = zerodha_raw.get("futures_mdata_json", "{}")
    if futures_mdata_raw and futures_mdata_raw != "{}":
        try:
            loaded = json.loads(futures_mdata_raw)
            if isinstance(loaded, dict):
                ctx["futures_mdata"]["current"] = _dict_to_df(loaded.get("current"))
                ctx["futures_mdata"]["next"] = _dict_to_df(loaded.get("next"))
        except (json.JSONDecodeError, TypeError):
            pass

    return True


def load_options_live_from_redis(redis: RedisProxy, stock: Stock) -> bool:
    """Load live options tick data from Redis into Stock's TickStore.

    Reads `data:options_live:{symbol}` hash (published by market-data service)
    and populates `stock._tick_store.options_live` with per-strike
    CE/PE tick dicts including gamma/oi from WS2 + Sensibull.
    """
    raw = redis.hgetall(f"data:options_live:{stock.stock_symbol}")
    if not raw:
        return False

    options_live: dict[float, dict] = {}
    for key, value in raw.items():
        parts = key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        try:
            strike = float(parts[0])
        except (ValueError, TypeError):
            continue
        opt_type = parts[1]
        if strike not in options_live:
            options_live[strike] = {}
        options_live[strike][opt_type] = safe_json_loads(value) or {}

    if options_live:
        stock._tick_store.options_live = options_live
        return True
    return False


def load_tick_from_redis(redis: RedisProxy, stock: Stock) -> bool:
    """Load live tick data (equity + options aggregate) from Redis into TickStore.

    Reads `data:tick:{symbol}` and `data:options_agg:{symbol}` hashes published
    by the market-data service's 1-second snapshot publisher. Populates:
      - stock._tick_store._zerodha_data (last_price, ohlc, volume, buy/sell qty)
      - stock._tick_store.options_aggregate (PCR, ATM, walls, gex_*, ...)
      - stock._tick_store.options_live (via load_options_live_from_redis)

    Returns True if any data was loaded.
    """
    loaded = False

    # Equity/index tick
    tick_raw = redis.hgetall(f"data:tick:{stock.stock_symbol}")
    if tick_raw:
        zd = stock._tick_store._zerodha_data
        for field in ("last_price", "open", "high", "low", "close",
                       "volume_traded", "total_buy_quantity", "total_sell_quantity",
                       "average_traded_price", "change"):
            val = tick_raw.get(field)
            if val is not None and val != "":
                try:
                    zd[field] = float(val)
                except (ValueError, TypeError):
                    pass
        loaded = True

    # Options aggregate
    agg_raw = redis.hgetall(f"data:options_agg:{stock.stock_symbol}")
    if agg_raw:
        agg = stock._tick_store.options_aggregate
        for k, v in agg_raw.items():
            if v is None or v == "":
                continue
            # Try numeric conversion, keep string if it fails
            try:
                agg[k] = float(v)
            except (ValueError, TypeError):
                agg[k] = v
        loaded = True

    # Options live (per-strike)
    if load_options_live_from_redis(redis, stock):
        loaded = True

    return loaded


def _dict_to_df(data: Any) -> pd.DataFrame:
    if data is None:
        return None
    if isinstance(data, dict):
        return pd.DataFrame([data])
    if isinstance(data, list):
        return pd.DataFrame(data)
    return data
