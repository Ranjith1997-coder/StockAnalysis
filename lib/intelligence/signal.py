"""
Standard signal format emitted by all analysis layers.

Every analyser (live, intraday, positional) emits Signal objects through
the SignalBus so the SignalCorrelator can detect cross-layer confluence.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import time as _time


class Direction(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class Layer(Enum):
    LIVE = "live"              # Per-tick (LiveOptionsEngine + LiveStockEngine)
    INTRADAY = "intraday"      # ~5-min cycle
    POSITIONAL = "positional"  # EOD / morning bias


class SignalStrength(Enum):
    WEAK = 1       # Single low-weight indicator
    MODERATE = 2   # Mid-weight indicator or 2+ low-weight
    STRONG = 3     # High-weight indicator (RSI divergence, cross-category)


# Map analysis weight ranges to SignalStrength
def weight_to_strength(weight: float) -> SignalStrength:
    if weight >= 16:
        return SignalStrength.STRONG
    if weight >= 10:
        return SignalStrength.MODERATE
    return SignalStrength.WEAK


@dataclass(frozen=True)
class Signal:
    symbol: str                  # "NIFTY", "RELIANCE"
    direction: Direction         # BULLISH / BEARISH / NEUTRAL
    source: str                  # "rsi_divergence", "pcr_crossover", "vwap_cross"
    layer: Layer                 # live / intraday / positional
    strength: SignalStrength     # WEAK / MODERATE / STRONG
    timestamp: float = field(default_factory=_time.time)
    context: dict = field(default_factory=dict, hash=False, compare=False)

    @property
    def age_seconds(self) -> float:
        return _time.time() - self.timestamp

    @property
    def key(self) -> str:
        """Unique key for dedup: same source+layer+direction within a window."""
        return f"{self.layer.value}.{self.source}.{self.direction.value}"
