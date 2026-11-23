"""
SnapshotPublisher — publishes in-memory TickStore state to Redis every 1 second.

Reads from the market-data service's own Stock objects (populated by WS ticks)
and writes to Redis hashes so that the orchestrator (bot commands, narrator,
check_data_freshness) and analysis-engine workers can read live tick data
without holding WebSocket connections.

Redis keys written:
  data:tick:{symbol}          — equity/index tick (last_price, ohlc, volume, ...)
  data:options_live:{symbol}  — per-strike CE/PE tick JSON (atomic MULTI: DEL + HSET)
  data:options_agg:{symbol}   — aggregate metrics (PCR, ATM, walls, gex_*, expiry, ...)
  data:futures_live:{symbol}  — current/next futures tick
"""
from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.common.redis_proxy import RedisProxy

import common.constants as constant
from lib.logging_util import get_logger
logger = get_logger("market-data")


class SnapshotPublisher:
    """Publishes tick snapshots to Redis at a fixed interval."""

    INTERVAL = 1.0  # seconds

    def __init__(self, redis: "RedisProxy", stock_objs: list, index_objs: list):
        self._redis = redis
        self._stock_objs = stock_objs
        self._index_objs = index_objs
        self._running = False
        self._thread: threading.Thread | None = None
        self.publish_count = 0
        self.last_publish_time = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="snapshot-publisher")
        self._thread.start()
        logger.info("[snapshot] Publisher started (interval=1s)")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while self._running:
            try:
                self._publish_all()
                self.publish_count += 1
                self.last_publish_time = time.time()
            except Exception as e:
                logger.error(f"[snapshot] Publish error: {e}")
            time.sleep(self.INTERVAL)

    def _publish_all(self) -> None:
        self._publish_equity_ticks()
        self._publish_options()
        self._publish_futures()

    def _publish_equity_ticks(self) -> None:
        for obj in self._stock_objs + self._index_objs:
            ts = obj._tick_store
            zd = ts._zerodha_data
            if zd.get("last_price", 0) <= 0:
                continue
            self._redis.hset(f"data:tick:{obj.stock_symbol}", mapping={
                "last_price": str(zd["last_price"]),
                "open": str(zd["open"]),
                "high": str(zd["high"]),
                "low": str(zd["low"]),
                "close": str(zd["close"]),
                "volume_traded": str(zd["volume_traded"]),
                "total_buy_quantity": str(zd["total_buy_quantity"]),
                "total_sell_quantity": str(zd["total_sell_quantity"]),
                "average_traded_price": str(zd["average_traded_price"]),
                "change": str(zd["change"]),
                "timestamp": str(zd.get("timestamp", "")),
                "tick_count": str(ts.tick_count),
            })

    def _replace_hash_atomically(self, key: str, mapping: dict) -> None:
        """Replace hash ``key`` with ``mapping`` without readers ever seeing it empty.

        Preferred: MULTI/EXEC pipeline (DEL + HSET applied atomically).
        Fallback (client without pipeline): HSET first, then HDEL stale fields —
        the hash is never empty, at worst briefly holds extra stale strikes.
        """
        client = self._redis
        pipeline_fn = getattr(client, "pipeline", None)
        if pipeline_fn is None:
            pipeline_fn = getattr(getattr(client, "_client", None), "pipeline", None)
        if callable(pipeline_fn):
            pipe = pipeline_fn(transaction=True)
            pipe.delete(key)
            pipe.hset(key, mapping=mapping)
            pipe.execute()
            return
        client.hset(key, mapping=mapping)
        stale = [f for f in (client.hkeys(key) or []) if f not in mapping]
        if stale:
            client.hdel(key, *stale)

    def _publish_options(self) -> None:
        for idx in self._index_objs:
            if idx.stock_symbol not in constant.LIVE_OPTIONS_INDICES:
                continue
            ts = idx._tick_store

            # Snapshot under the TickStore lock so the tick thread can't mutate
            # the dicts mid-iteration.
            with ts._lock:
                mapping = {}
                for strike, sides in ts.options_live.items():
                    strike_key = str(float(strike))
                    for opt_type in ("CE", "PE"):
                        tick = sides.get(opt_type)
                        if tick:
                            mapping[f"{strike_key}_{opt_type}"] = json.dumps(tick, default=str)
                agg = dict(ts.options_aggregate)
                option_tick_count = ts.option_tick_count
                tick_count = ts.tick_count

            if mapping:
                self._replace_hash_atomically(f"data:options_live:{idx.stock_symbol}", mapping)

            if agg.get("last_updated", 0) > 0:
                agg_mapping = {}
                for k, v in agg.items():
                    if k == "gex_by_strike":
                        continue
                    agg_mapping[k] = str(v) if v is not None else ""
                agg_mapping["option_tick_count"] = str(option_tick_count)
                agg_mapping["tick_count"] = str(tick_count)
                self._redis.hset(f"data:options_agg:{idx.stock_symbol}", mapping=agg_mapping)

    def _publish_futures(self) -> None:
        for obj in self._stock_objs + self._index_objs:
            ts = obj._tick_store
            if not ts.futures_live:
                continue
            mapping = {}
            for expiry_key in ("current", "next"):
                ft = ts.futures_live.get(expiry_key, {})
                if not ft:
                    continue
                for field in ("ltp", "oi", "volume", "change", "buy_qty", "sell_qty"):
                    if ft.get(field) is not None:
                        mapping[f"{expiry_key}_{field}"] = str(ft[field])
                depth = ft.get("depth") or {}
                for side in ("buy", "sell"):
                    for i, level in enumerate((depth.get(side) or [])[:5], start=1):
                        mapping[f"{expiry_key}_{side}_px_{i}"] = str(level.get("price", ""))
                        mapping[f"{expiry_key}_{side}_qty_{i}"] = str(level.get("quantity", ""))
                        mapping[f"{expiry_key}_{side}_ord_{i}"] = str(level.get("orders", ""))
            if mapping:
                self._redis.hset(f"data:futures_live:{obj.stock_symbol}", mapping=mapping)
