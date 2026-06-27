"""
StockProxy — reconstructs Stock objects from Redis hashes and publishes Stock data to Redis.

Used by:
- data-gateway: writes fetched data to Redis (to_redis methods)
- analysis-engine: reads data from Redis to reconstruct Stock objects (from_redis)
- bot-service: reads specific fields for bot commands
"""

from __future__ import annotations

import json
import pandas as pd
from redis.asyncio import Redis
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from common.Stock import Stock

from services.common.serialization import (
    dataframe_to_json,
    dataframe_from_json,
    safe_json_dumps,
    safe_json_loads,
)


class StockProxy:
    """Reconstructs Stock objects from Redis for analysis-engine workers."""

    @staticmethod
    async def from_redis(redis: Redis, symbol: str, is_index: bool = False) -> Stock:
        from common.Stock import Stock

        stock = Stock(symbol, symbol, is_index=is_index)

        price_data = await redis.hgetall(f"data:price:{symbol}")
        if price_data:
            stock.priceData = dataframe_from_json(
                price_data.get("priceData_json", "{}")
            )
            stock.ltp = _safe_float(price_data.get("ltp"))
            stock.ltp_change_perc = _safe_float(price_data.get("ltp_change_perc"))
            stock.prevDayOHLCV = safe_json_loads(
                price_data.get("prevDayOHLCV_json", "{}")
            )
            stock.daily_hv = _safe_float(price_data.get("daily_hv"))

        sensibull_data = await redis.hgetall(f"data:sensibull:{symbol}")
        if sensibull_data:
            stock.sensibull_ctx = {
                "last_fetch_time": sensibull_data.get("last_fetch_time"),
                "current": safe_json_loads(
                    sensibull_data.get("current_json", "{}")
                ) or {"underlying_info": None, "stats": None, "per_expiry_map": None, "nse_stats": None},
                "historical_data": dataframe_from_json(
                    sensibull_data.get("historical_data_json", "{}")
                ),
                "oi_chain": safe_json_loads(sensibull_data.get("oi_chain_json", "null")),
                "oi_chain_history": safe_json_loads(
                    sensibull_data.get("oi_chain_history_json", "[]")
                ) or [],
                "iv_chart_history": dataframe_from_json(
                    sensibull_data.get("iv_chart_history_json", "{}")
                ),
                "oi_history": dataframe_from_json(
                    sensibull_data.get("oi_history_json", "{}")
                ),
            }

        zerodha_data = await redis.hgetall(f"data:zerodha:{symbol}")
        if zerodha_data:
            stock.zerodha_ctx = {
                "last_notification_time": None,
                "option_chain": {
                    "current": dataframe_from_json(
                        zerodha_data.get("option_chain_current_json", "{}")
                    ),
                    "next": None,
                },
                "futures_mdata": safe_json_loads(
                    zerodha_data.get("futures_mdata_json", "{}")
                ) or {"current": None, "next": None},
                "futures_data": {
                    "current": dataframe_from_json(
                        zerodha_data.get("futures_data_current_json", "{}")
                    ),
                    "next": pd.DataFrame(),
                },
            }

        return stock

    @staticmethod
    async def get_options_live(redis: Redis, symbol: str) -> dict:
        raw = await redis.hgetall(f"data:options_live:{symbol}")
        options_live = {}
        for key, value in raw.items():
            parts = key.rsplit("_", 1)
            if len(parts) == 2:
                strike = float(parts[0])
                opt_type = parts[1]
                if strike not in options_live:
                    options_live[strike] = {}
                options_live[strike][opt_type] = safe_json_loads(value) or {}
        return options_live

    @staticmethod
    async def get_options_aggregate(redis: Redis, symbol: str) -> dict:
        raw = await redis.hgetall(f"data:options_agg:{symbol}")
        return {k: safe_json_loads(v) if isinstance(v, str) and v.startswith("{") else v
                for k, v in raw.items()}

    # ── Publish methods (used by data-gateway) ──────────────────────────

    @staticmethod
    async def publish_price_data(
        redis: Redis, symbol: str, stock: Stock,
    ):
        mapping = {
            "priceData_json": dataframe_to_json(stock.priceData),
            "ltp": str(stock.ltp) if stock.ltp is not None else "",
            "ltp_change_perc": str(stock.ltp_change_perc) if stock.ltp_change_perc is not None else "",
            "prevDayOHLCV_json": safe_json_dumps(stock.prevDayOHLCV),
            "daily_hv": str(stock.daily_hv) if stock.daily_hv is not None else "",
        }
        await redis.hset(f"data:price:{symbol}", mapping=mapping)

    @staticmethod
    async def publish_sensibull_data(
        redis: Redis, symbol: str, sensibull_ctx: dict,
    ):
        mapping = {
            "last_fetch_time": str(sensibull_ctx.get("last_fetch_time", "")),
            "current_json": safe_json_dumps(sensibull_ctx.get("current", {})),
            "historical_data_json": dataframe_to_json(
                sensibull_ctx.get("historical_data", pd.DataFrame())
            ),
            "oi_chain_json": safe_json_dumps(sensibull_ctx.get("oi_chain")),
            "oi_chain_history_json": safe_json_dumps(
                sensibull_ctx.get("oi_chain_history", [])
            ),
            "iv_chart_history_json": dataframe_to_json(
                sensibull_ctx.get("iv_chart_history", pd.DataFrame())
            ),
            "oi_history_json": dataframe_to_json(
                sensibull_ctx.get("oi_history", pd.DataFrame())
            ),
        }
        await redis.hset(f"data:sensibull:{symbol}", mapping=mapping)


def _safe_float(val) -> float | None:
    if val is None or val == "" or val == "None":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
