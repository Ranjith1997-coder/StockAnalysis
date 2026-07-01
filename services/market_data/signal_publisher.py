"""
RedisSignalBus — drop-in replacement for in-memory SignalBus.

Live engines (LiveOptionsEngine, LiveStockEngine) emit Signal objects
to this bus instead of the in-process thread pub/sub. Signals are
published to the Redis stream `intelligence:signals` so the monolith's
SignalCorrelator can consume them cross-process.

Implements the same interface as intelligence.signal_bus.SignalBus so
it can be used as a transparent replacement.
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from services.common.redis_proxy import RedisProxy

from intelligence.signal import Signal
from common.logging_util import logger

Subscriber = Callable[[Signal], None]

SIGNALS_STREAM = "intelligence:signals"


class RedisSignalBus:
    """
    Publishes signals to Redis stream `intelligence:signals`.

    The monolith's SignalCorrelator subscribes to this stream via a
    consumer group to detect cross-layer confluence.
    """

    def __init__(self, redis: "RedisProxy"):
        self._redis = redis
        self._total_emitted = 0

    def subscribe(self, callback: Subscriber):
        pass

    def emit(self, signal: Signal):
        context_json = json.dumps(signal.context, default=str) if signal.context else "{}"
        self._redis.xadd(SIGNALS_STREAM, {
            "symbol": signal.symbol,
            "direction": signal.direction.name,
            "source": signal.source,
            "layer": signal.layer.name,
            "strength": signal.strength.name,
            "timestamp": str(signal.timestamp),
            "context": context_json,
        }, maxlen=2000)
        self._total_emitted += 1
        logger.debug(
            f"[RedisSignalBus] Emitted {signal.symbol} {signal.direction.name} "
            f"{signal.source} ({signal.layer.name})"
        )

    @property
    def total_emitted(self) -> int:
        return self._total_emitted
