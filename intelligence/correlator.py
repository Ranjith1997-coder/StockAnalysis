"""
SignalCorrelator — time-windowed cross-layer confluence detector.

Buffers signals per symbol. When a new signal arrives, checks whether
signals from OTHER layers align in the same direction within the time window.

Confluence levels:
  - 2 layers aligned (e.g., live + intraday)        -> MODERATE
  - 3 layers aligned (live + intraday + positional)  -> HIGH
  - Contradicting layers present                     -> adds CAUTION flag
"""

from __future__ import annotations
import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from intelligence.signal import Signal, Direction, Layer, SignalStrength
from common.logging_util import logger


@dataclass
class Confluence:
    """A detected confluence of aligned signals across layers."""
    symbol: str
    direction: Direction
    signals: list[Signal]
    layers_involved: set[Layer] = field(default_factory=set)
    score: float = 0.0
    has_contradiction: bool = False   # True if opposing signals exist from other layers
    timestamp: float = field(default_factory=time.time)

    @property
    def layer_count(self) -> int:
        return len(self.layers_involved)

    @property
    def level(self) -> str:
        if self.layer_count >= 3:
            return "HIGH"
        return "MODERATE"


class SignalCorrelator:
    """
    Detects cross-layer signal confluence in real time.

    Subscribes to SignalBus via on_signal(). When confluence is detected,
    calls the on_confluence callback.
    """

    # How long a signal from each layer stays relevant (seconds)
    WINDOW: dict[Layer, int] = {
        Layer.LIVE:       300,     # 5 min
        Layer.INTRADAY:   900,     # 15 min
        Layer.POSITIONAL: 21600,   # 6 hours (covers full trading day)
    }

    # Minimum seconds between firing the same confluence for a symbol+direction
    CONFLUENCE_COOLDOWN = 600  # 10 min

    def __init__(self, on_confluence: Callable[[Confluence], None] | None = None):
        self._buffer: dict[str, list[Signal]] = defaultdict(list)
        self._lock = threading.Lock()
        self._on_confluence = on_confluence
        self._last_confluence: dict[tuple[str, str], float] = {}
        self._total_confluences = 0

    def on_signal(self, signal: Signal):
        """Entry point — called by SignalBus on every new signal."""
        with self._lock:
            self._prune(signal.symbol)
            self._dedupe_and_add(signal)
            self._check_confluence(signal.symbol)

    def _prune(self, symbol: str):
        """Remove expired signals outside their layer's time window."""
        now = time.time()
        self._buffer[symbol] = [
            s for s in self._buffer[symbol]
            if (now - s.timestamp) < self.WINDOW[s.layer]
        ]

    def _dedupe_and_add(self, signal: Signal):
        """Replace any existing signal with same key (source+layer+direction)."""
        self._buffer[signal.symbol] = [
            s for s in self._buffer[signal.symbol]
            if s.key != signal.key
        ]
        self._buffer[signal.symbol].append(signal)

    def _check_confluence(self, symbol: str):
        """Check if buffered signals form a cross-layer confluence."""
        signals = self._buffer[symbol]
        if len(signals) < 2:
            return

        # Group by direction (skip NEUTRAL — they don't form directional confluence)
        by_direction: dict[Direction, list[Signal]] = defaultdict(list)
        for s in signals:
            if s.direction != Direction.NEUTRAL:
                by_direction[s.direction].append(s)

        for direction, aligned_signals in by_direction.items():
            layers = {s.layer for s in aligned_signals}

            # Need at least 2 different layers for confluence
            if len(layers) < 2:
                continue

            # Cooldown check
            cooldown_key = (symbol, direction.value)
            last = self._last_confluence.get(cooldown_key, 0.0)
            if (time.time() - last) < self.CONFLUENCE_COOLDOWN:
                continue

            # Check for contradicting signals from other layers
            opposite = Direction.BEARISH if direction == Direction.BULLISH else Direction.BULLISH
            opposing_signals = by_direction.get(opposite, [])
            opposing_layers = {s.layer for s in opposing_signals}
            has_contradiction = len(opposing_layers) > 0

            score = self._score(aligned_signals, layers, has_contradiction)

            confluence = Confluence(
                symbol=symbol,
                direction=direction,
                signals=aligned_signals,
                layers_involved=layers,
                score=score,
                has_contradiction=has_contradiction,
                timestamp=time.time(),
            )

            self._last_confluence[cooldown_key] = time.time()
            self._total_confluences += 1

            level = confluence.level
            caution = " [CAUTION: contradicting signals]" if has_contradiction else ""
            logger.info(
                f"[Correlator] {symbol} {direction.value} {level} confluence "
                f"({len(layers)} layers, score={score:.1f}){caution}"
            )

            if self._on_confluence:
                self._on_confluence(confluence)

    def _score(self, signals: list[Signal], layers: set[Layer],
               has_contradiction: bool) -> float:
        """
        Score a confluence.

        Base: each signal contributes its strength value (1/2/3).
        Bonuses:
          +5 per additional layer beyond the first
          +3 if LIVE layer present (most timely confirmation)
          -3 if contradicting signals exist (dampener)
        """
        base = sum(s.strength.value for s in signals)
        layer_bonus = (len(layers) - 1) * 5
        live_bonus = 3 if Layer.LIVE in layers else 0
        contradiction_penalty = -3 if has_contradiction else 0
        return base + layer_bonus + live_bonus + contradiction_penalty

    def get_buffer_snapshot(self, symbol: str) -> list[Signal]:
        """Return current active signals for a symbol (for debugging/display)."""
        with self._lock:
            self._prune(symbol)
            return list(self._buffer.get(symbol, []))

    @property
    def total_confluences(self) -> int:
        return self._total_confluences
