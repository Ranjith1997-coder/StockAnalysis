"""
Thread-safe signal bus for cross-layer communication.

All analysers emit Signal objects here. The SignalCorrelator subscribes
to detect cross-layer confluence in real time.
"""

from __future__ import annotations
import threading
from typing import Callable

from .signal import Signal
from lib.logging_util import get_logger
logger = get_logger("intelligence")

Subscriber = Callable[[Signal], None]


class SignalBus:
    """
    Synchronous publish/subscribe bus.

    Usage:
        bus = SignalBus()
        bus.subscribe(correlator.on_signal)
        bus.emit(Signal(...))
    """

    def __init__(self):
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()
        self._total_emitted = 0

    def subscribe(self, callback: Subscriber):
        with self._lock:
            self._subscribers.append(callback)

    def emit(self, signal: Signal):
        with self._lock:
            listeners = list(self._subscribers)
            self._total_emitted += 1
        logger.debug("[SignalBus] emit #%d %s layer=%s source=%s dir=%s",
                     self._total_emitted, signal.symbol, signal.layer.value,
                     signal.source, signal.direction.value)
        for cb in listeners:
            try:
                cb(signal)
            except Exception as e:
                logger.error("[SignalBus] subscriber error on %s: %s", signal.source, e, exc_info=True)

    @property
    def total_emitted(self) -> int:
        return self._total_emitted
