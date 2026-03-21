"""
Signal persistence for morning bias.

Saves positional signals to a JSON file so they can be reloaded
if the system restarts mid-day. Signals are tagged with a date
and discarded if stale.
"""

from __future__ import annotations
import json
import os
from datetime import date
from dataclasses import asdict

from intelligence.signal import Signal, Direction, Layer, SignalStrength
from common.logging_util import logger

SIGNAL_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "daily_signals.json")


def save_signals(signals: list[Signal]):
    """Persist positional signals. Overwritten each morning."""
    os.makedirs(os.path.dirname(SIGNAL_CACHE_PATH), exist_ok=True)
    data = {
        "date": date.today().isoformat(),
        "signals": [
            {
                "symbol": s.symbol,
                "direction": s.direction.value,
                "source": s.source,
                "layer": s.layer.value,
                "strength": s.strength.value,
                "timestamp": s.timestamp,
                "context": s.context,
            }
            for s in signals
        ],
    }
    try:
        with open(SIGNAL_CACHE_PATH, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"[SignalStore] Saved {len(signals)} signals to {SIGNAL_CACHE_PATH}")
    except Exception as e:
        logger.error(f"[SignalStore] Failed to save signals: {e}")


def load_signals() -> list[Signal]:
    """Load today's positional signals. Returns [] if stale or missing."""
    try:
        with open(SIGNAL_CACHE_PATH) as f:
            data = json.load(f)
        if data.get("date") != date.today().isoformat():
            logger.info("[SignalStore] Cached signals are from a previous day, ignoring")
            return []
        signals = []
        for s in data.get("signals", []):
            signals.append(Signal(
                symbol=s["symbol"],
                direction=Direction(s["direction"]),
                source=s["source"],
                layer=Layer(s["layer"]),
                strength=SignalStrength(s["strength"]),
                timestamp=s["timestamp"],
                context=s.get("context", {}),
            ))
        logger.info(f"[SignalStore] Loaded {len(signals)} cached signals for today")
        return signals
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"[SignalStore] Failed to load signals: {e}")
        return []
