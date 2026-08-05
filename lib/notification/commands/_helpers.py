"""Shared helper utilities used across multiple command modules."""
from __future__ import annotations

import common.shared as shared
from lib.logging_util import get_logger
logger = get_logger("notification")


def _get_redis():
    """Get a Redis connection — monolith proxy first, direct fallback for standalone services."""
    try:
        import intraday.intraday_monitor as _im
        proxy = getattr(_im, "redis_proxy", None)
        if proxy is not None:
            return proxy
    except Exception as e:
        logger.debug("[helpers] Redis proxy import failed: %s", e)
    try:
        import redis as _redis
        import os as _os
        return _redis.from_url(_os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
    except Exception as e:
        logger.debug("[helpers] Redis direct connect failed: %s", e)
        return None


def find_stock_by_symbol(symbol: str):
    """Look up a Stock object by symbol across all tracked dicts."""
    symbol_upper = symbol.upper().strip()
    for d in (
        shared.app_ctx.index_token_obj_dict,
        shared.app_ctx.stock_token_obj_dict,
        shared.app_ctx.commodity_token_obj_dict,
        shared.app_ctx.global_indices_token_obj_dict,
    ):
        for obj in d.values():
            if obj.stock_symbol.upper() == symbol_upper:
                return obj
    return None


def refresh_stock_from_redis(symbol: str) -> bool:
    """Refresh a Stock's live tick data from Redis (market-data service snapshots).

    Loads data:tick:*, data:options_live:*, data:options_agg:* into the
    Stock's TickStore so bot commands see fresh data without WS connections.
    Also loads priceData + prevDayOHLCV if not already loaded, and updates
    stock.ltp and ltp_change_perc.
    """
    stock = find_stock_by_symbol(symbol)
    if stock is None:
        return False
    redis = _get_redis()
    if redis is None:
        return False
    try:
        from services.common.stock_loader import load_tick_from_redis, load_price_data_from_redis

        # Load priceData if not already loaded (needed for update_latest_data)
        if stock.is_price_data_empty():
            if stock.is_index:
                load_price_data_from_redis(redis, [], [stock])
            else:
                load_price_data_from_redis(redis, [stock], [])

        load_tick_from_redis(redis, stock)
        stock.update_latest_data()
        return True
    except Exception as e:
        logger.debug(f"[helpers] refresh_stock_from_redis({symbol}): {e}")
        return False


def build_gainers_losers():
    """Compute top 5 gainers and losers from live stock data."""
    from common.helperFunctions import percentageChange

    redis = _get_redis()
    if redis is not None:
        from services.common.stock_loader import load_tick_from_redis
        for _, stock in shared.app_ctx.stock_token_obj_dict.items():
            try:
                load_tick_from_redis(redis, stock)
                stock.update_latest_data()
            except Exception as e:
                logger.debug("[helpers] build_gainers_losers skip %s: %s", stock.stock_symbol, e)
                continue

    gainers, losers = [], []
    for _, stock in shared.app_ctx.stock_token_obj_dict.items():
        try:
            if stock.ltp is not None and stock.prevDayOHLCV is not None:
                prev_close = stock.prevDayOHLCV.get("CLOSE")
                if prev_close and prev_close > 0:
                    change = percentageChange(stock.ltp, prev_close)
                    if isinstance(change, float) and change == change:  # NaN guard
                        if change > 0:
                            gainers.append((stock.stock_symbol, change))
                        else:
                            losers.append((stock.stock_symbol, change))
        except Exception as e:
            logger.debug("[helpers] build_gainers_losers score skip %s: %s", stock.stock_symbol, e)
            continue

    gainers.sort(key=lambda x: x[1], reverse=True)
    losers.sort(key=lambda x: x[1])
    return gainers[:5], losers[:5]
