"""
Publishes PinnSignal objects (services/volatility_engine/comparator.py) to
Redis for paper-trading to consume, and caches per-strike fair IV/z-scores
for the confirmation-mode check in signal_router.py. See design doc section
8.6/8.7.
"""
from __future__ import annotations

import math
import time

from services.volatility_engine.comparator import EvalResult, PinnSignal

SIGNALS_STREAM = "pinn:signals"
SIGNALS_STREAM_MAXLEN = 1000
ZSCORE_CACHE_TTL_SECONDS = 30  # stale data expires fast -- matches design doc 8.7


def emit_signal(redis, signal: PinnSignal) -> None:
    fields = {
        "signal_type": signal.signal_type,
        "symbol": signal.symbol,
        "strategy": signal.strategy,
        "direction": signal.direction,
        "z_score": str(signal.z_score),
        "expiry": signal.expiry,
        "timestamp": signal.timestamp.isoformat(),
        "signal_source": "PINN_MISPRICING",
    }
    if signal.signal_type == "SKEW_FADE_SETUP":
        fields.update({
            "sr_level": str(signal.sr_level),
            "overpriced_type": signal.overpriced_type,
            "fair_iv": str(signal.fair_iv),
            "live_iv": str(signal.live_iv),
        })
    elif signal.signal_type == "RANGE_BOUND_SETUP":
        fields.update({
            "put_wall_strike": str(signal.put_wall_strike),
            "call_wall_strike": str(signal.call_wall_strike),
            "fair_iv_ce": str(signal.fair_iv_ce),
            "fair_iv_pe": str(signal.fair_iv_pe),
            "live_iv_ce": str(signal.live_iv_ce),
            "live_iv_pe": str(signal.live_iv_pe),
        })

    redis.xadd(SIGNALS_STREAM, fields, maxlen=SIGNALS_STREAM_MAXLEN)


def write_fair_iv_to_redis(redis, symbol: str, results: list[EvalResult], tau: float) -> None:
    """Cache per-strike fair IV + z-score so paper-trading's
    _handle_entry_signal() can check PINN confirmation when a composite
    analyser signal fires, without waiting for the next PinnSignal."""
    if not results:
        return

    mapping = {}
    for r in results:
        key = f"{r.strike_data.strike}_{r.strike_data.option_type}"
        fair_iv = math.sqrt(max(r.fair_w, 0.0) / tau)
        mapping[f"fair_iv_{key}"] = str(fair_iv)
        mapping[f"zscore_{key}"] = str(r.z)
    mapping["last_updated"] = str(time.time())

    key = f"pinn:zscore:{symbol}"
    redis.hset(key, mapping=mapping)
    redis.expire(key, ZSCORE_CACHE_TTL_SECONDS)
