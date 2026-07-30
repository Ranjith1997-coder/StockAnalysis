"""
Signal Intelligence — Worker Logic

Consumes intelligence:signals (LIVE + INTRADAY + POSITIONAL Signal events
reconstructed from Redis), feeds a single shared SignalCorrelator, and on
confluence:
  1. XADDs to intelligence:confluence for downstream subscribers (the
     monolith's HIGH-only narrator consumer, future paper-trading).
  2. Sends the base Telegram alert directly. This call only touches Redis
     (notification/Notification.py -> notification:jobs), so it needs no
     monolith-only state.
"""
from __future__ import annotations

import json

from intelligence.signal import Signal, Direction, Layer, SignalStrength
from intelligence.correlator import Confluence
from common.constants import CONFLUENCE_STREAM
from services.common.logging import get_logger
logger = get_logger("signal-intelligence")
from notification.Notification import TELEGRAM_NOTIFICATIONS
from services.common.metrics import incr_stock, incr_system, incr_daily


def reconstruct_signal(fields: dict) -> Signal:
    """Rebuild a Signal from an intelligence:signals stream message.

    Must use Direction[name]/Layer[name]/SignalStrength[name] -- the stream
    stores each enum's .name, not its .value (Layer.LIVE.value == "live" but
    the stream field is "LIVE").
    """
    return Signal(
        symbol=fields["symbol"],
        direction=Direction[fields["direction"]],
        source=fields["source"],
        layer=Layer[fields["layer"]],
        strength=SignalStrength[fields["strength"]],
        timestamp=float(fields["timestamp"]),
        context=json.loads(fields.get("context") or "{}"),
    )


def format_confluence_alert(confluence: Confluence) -> str:
    """Base Telegram alert text -- same format the monolith's old
    _handle_confluence used to build in-process."""
    layers_str = " + ".join(
        l.value.upper() for l in sorted(confluence.layers_involved, key=lambda l: l.value)
    )
    sources = "\n".join(
        f"  - {s.layer.value}: {s.source} ({s.strength.name})"
        for s in sorted(confluence.signals, key=lambda s: s.timestamp)
    )
    level = confluence.level
    caution = "\n  CAUTION: contradicting signals from other layers" if confluence.has_contradiction else ""

    return (
        f"{'[HIGH]' if level == 'HIGH' else '[MODERATE]'} "
        f"<b>{confluence.symbol} — {level} CONFLUENCE {confluence.direction.value}</b>\n\n"
        f"Layers: {layers_str}\n"
        f"Score: {confluence.score:.0f}\n\n"
        f"Signals:\n{sources}{caution}"
    )


def make_on_confluence(redis):
    """Build the on_confluence callback bound to a Redis connection."""

    def on_confluence(confluence: Confluence) -> None:
        try:
            redis.xadd(CONFLUENCE_STREAM, confluence.to_stream_fields(), maxlen=2000)
        except Exception:
            logger.exception(
                f"[signal-intelligence] Failed to XADD confluence for {confluence.symbol}"
            )

        TELEGRAM_NOTIFICATIONS.send_live_options_notification(
            format_confluence_alert(confluence), parse_mode="HTML", symbol=confluence.symbol
        )
        incr_stock(confluence.symbol, "alerts_confluence")
        incr_system("total_confluences")
        incr_daily("confluences")
        logger.info(
            f"[Confluence] {confluence.symbol} {confluence.direction.value} "
            f"{confluence.level} ({confluence.layer_count} layers, score={confluence.score:.0f})"
        )

    return on_confluence
