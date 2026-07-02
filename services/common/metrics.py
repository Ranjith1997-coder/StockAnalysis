"""
Metrics — lightweight per-stock + system-wide counters in Redis.

All functions are fail-safe: they never raise. If Redis is unavailable,
the impact is a single log line \u2014 business logic is never affected.

Usage:
    from services.common.metrics import incr_stock, set_stock, incr_system

    # Per-stock counter
    incr_stock("RELIANCE", "tick_count")
    incr_stock("RELIANCE", "alerts_trend")
    set_stock("RELIANCE", last_analysis_result="SUCCESS", last_analysis_duration_ms="280")

    # System counter
    incr_system("total_ticks")
    incr_system("jobs_dispatched", 5)
    set_system(last_cycle_age_s="0", intraday_cycle_count="247")

    # Read back
    stats = get_stock_stats("RELIANCE")
    sys_stats = get_system_stats()
    all_stats = get_all_stock_stats()
    top = get_top_stocks("alerts_total", limit=10)

Redis keys written:
    stats:stock:{symbol}     \u2014 per-stock counters (HASH)
    stats:system             \u2014 system-wide counters (HASH)
    stats:daily:{YYYY-MM-DD} \u2014 daily rollup (HASH, 30-day TTL)
"""
from __future__ import annotations

import os
import time
from datetime import date

from common.logging_util import logger

_REDIS_CLIENT = None


def _get_redis():
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        import redis as _redis_mod
        _REDIS_CLIENT = _redis_mod.from_url(redis_url, decode_responses=True)
        _REDIS_CLIENT.ping()
        logger.info(f"[metrics] Connected at {redis_url}")
    except Exception as e:
        logger.warning(f"[metrics] Redis unavailable: {e}")
        _REDIS_CLIENT = None
    return _REDIS_CLIENT


def _key(symbol: str) -> str:
    return f"stats:stock:{symbol.upper()}"


def _today() -> str:
    return str(date.today())


# ── Writer helpers ────────────────────────────────────────────────────────────


def incr_stock(symbol: str, field: str, amount: int = 1) -> None:
    if not symbol:
        return
    _r = _get_redis()
    if _r is None:
        return
    try:
        _r.hincrby(_key(symbol), field, amount)
    except Exception as exc:
        logger.debug(f"[metrics] incr_stock({symbol}, {field}) failed: {exc}")


def set_stock(symbol: str, **fields) -> None:
    if not symbol:
        return
    _r = _get_redis()
    if _r is None or not fields:
        return
    try:
        _r.hset(_key(symbol), mapping={k: str(v) for k, v in fields.items()})
    except Exception as exc:
        logger.debug(f"[metrics] set_stock({symbol}, ...) failed: {exc}")


def incr_system(field: str, amount: int = 1) -> None:
    _r = _get_redis()
    if _r is None:
        return
    try:
        pipe = _r.pipeline()
        pipe.hincrby("stats:system", field, amount)
        pipe.hset("stats:system", "last_updated", str(time.time()))
        pipe.execute()
    except Exception as exc:
        logger.debug(f"[metrics] incr_system({field}) failed: {exc}")


def set_system(**fields) -> None:
    _r = _get_redis()
    if _r is None or not fields:
        return
    try:
        fields["last_updated"] = str(time.time())
        _r.hset("stats:system", mapping={k: str(v) for k, v in fields.items()})
    except Exception as exc:
        logger.debug(f"[metrics] set_system(...) failed: {exc}")


def incr_daily(field: str, amount: int = 1) -> None:
    _r = _get_redis()
    if _r is None:
        return
    key = f"stats:daily:{_today()}"
    try:
        pipe = _r.pipeline()
        pipe.hincrby(key, field, amount)
        pipe.expire(key, 86400 * 30)
        pipe.execute()
    except Exception as exc:
        logger.debug(f"[metrics] incr_daily({field}) failed: {exc}")


# ── Reader helpers (for bot commands / debugging) ─────────────────────────────


def get_stock_stats(symbol: str) -> dict:
    _r = _get_redis()
    if _r is None:
        return {}
    try:
        return _r.hgetall(_key(symbol)) or {}
    except Exception:
        return {}


def get_system_stats() -> dict:
    _r = _get_redis()
    if _r is None:
        return {}
    try:
        return _r.hgetall("stats:system") or {}
    except Exception:
        return {}


def get_all_stock_stats() -> dict[str, dict]:
    _r = _get_redis()
    if _r is None:
        return {}
    try:
        cursor = 0
        result = {}
        while True:
            cursor, keys = _r.scan(cursor=cursor, match="stats:stock:*", count=500)
            for key in keys:
                symbol = key.split(":", 2)[-1]
                result[symbol] = _r.hgetall(key) or {}
            if cursor == 0:
                break
        return result
    except Exception:
        return {}


def get_top_stocks(field: str, limit: int = 10) -> list[tuple[str, int]]:
    all_stats = get_all_stock_stats()
    ranked = []
    for symbol, stats in all_stats.items():
        val = stats.get(field, "0")
        try:
            ranked.append((symbol, int(val)))
        except (ValueError, TypeError):
            ranked.append((symbol, 0))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked[:limit]
