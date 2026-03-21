"""
Thread-safe signal bus for cross-layer communication.

All analysers emit Signal objects here. The SignalCorrelator subscribes
to detect cross-layer confluence in real time.
"""

from __future__ import annotations
import threading
from typing import Callable

from intelligence.signal import Signal
from common.logging_util import logger

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
        for cb in listeners:
            try:
                cb(signal)
            except Exception as e:
                logger.error(f"[SignalBus] subscriber error on {signal.source}: {e}")

    @property
    def total_emitted(self) -> int:
        return self._total_emitted
