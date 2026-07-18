"""Tests for services/signal_intelligence — worker.py + Confluence wire format."""

import json
from unittest.mock import MagicMock, patch

from intelligence.signal import Signal, Direction, Layer, SignalStrength
from intelligence.correlator import SignalCorrelator, Confluence
from services.signal_intelligence.worker import (
    reconstruct_signal,
    format_confluence_alert,
    make_on_confluence,
)


def _signal_fields(symbol="NIFTY", direction="BULLISH", source="vwap_cross",
                    layer="LIVE", strength="STRONG", timestamp=1000.0):
    return {
        "symbol": symbol,
        "direction": direction,
        "source": source,
        "layer": layer,
        "strength": strength,
        "timestamp": str(timestamp),
        "context": "{}",
    }


class TestReconstructSignal:
    def test_reconstructs_by_name_not_value(self):
        """Layer.LIVE.value == 'live' but the stream stores 'LIVE' (.name) —
        reconstruction must index enums by name."""
        fields = _signal_fields(layer="LIVE", strength="STRONG", direction="BEARISH")
        signal = reconstruct_signal(fields)

        assert signal.symbol == "NIFTY"
        assert signal.direction == Direction.BEARISH
        assert signal.layer == Layer.LIVE
        assert signal.strength == SignalStrength.STRONG
        assert signal.timestamp == 1000.0

    def test_context_defaults_to_empty_dict(self):
        fields = _signal_fields()
        del fields["context"]
        signal = reconstruct_signal(fields)
        assert signal.context == {}


class TestConfluenceWireFormat:
    def test_round_trip(self):
        signals = [
            Signal(symbol="NIFTY", direction=Direction.BULLISH, source="vwap_cross",
                   layer=Layer.LIVE, strength=SignalStrength.STRONG, timestamp=100.0),
            Signal(symbol="NIFTY", direction=Direction.BULLISH, source="rsi_divergence",
                   layer=Layer.INTRADAY, strength=SignalStrength.MODERATE, timestamp=90.0),
        ]
        confluence = Confluence(
            symbol="NIFTY",
            direction=Direction.BULLISH,
            signals=signals,
            layers_involved={Layer.LIVE, Layer.INTRADAY},
            score=11.0,
            has_contradiction=False,
            timestamp=100.0,
        )

        fields = confluence.to_stream_fields()
        restored = Confluence.from_stream_fields(fields)

        assert restored.symbol == "NIFTY"
        assert restored.direction == Direction.BULLISH
        assert restored.layers_involved == {Layer.LIVE, Layer.INTRADAY}
        assert restored.level == "MODERATE"
        assert restored.score == 11.0
        assert restored.has_contradiction is False
        assert len(restored.signals) == 2
        assert {s.source for s in restored.signals} == {"vwap_cross", "rsi_divergence"}

    def test_high_level_round_trips_with_three_layers(self):
        signals = [
            Signal(symbol="NIFTY", direction=Direction.BEARISH, source="s1",
                   layer=Layer.LIVE, strength=SignalStrength.WEAK, timestamp=1.0),
            Signal(symbol="NIFTY", direction=Direction.BEARISH, source="s2",
                   layer=Layer.INTRADAY, strength=SignalStrength.WEAK, timestamp=1.0),
            Signal(symbol="NIFTY", direction=Direction.BEARISH, source="s3",
                   layer=Layer.POSITIONAL, strength=SignalStrength.WEAK, timestamp=1.0),
        ]
        confluence = Confluence(
            symbol="NIFTY", direction=Direction.BEARISH, signals=signals,
            layers_involved={Layer.LIVE, Layer.INTRADAY, Layer.POSITIONAL},
        )
        restored = Confluence.from_stream_fields(confluence.to_stream_fields())
        assert restored.level == "HIGH"


class TestFormatConfluenceAlert:
    def test_contains_symbol_and_level(self):
        confluence = Confluence(
            symbol="BANKNIFTY",
            direction=Direction.BULLISH,
            signals=[
                Signal(symbol="BANKNIFTY", direction=Direction.BULLISH, source="oi_wall_breach",
                       layer=Layer.LIVE, strength=SignalStrength.STRONG),
            ],
            layers_involved={Layer.LIVE, Layer.INTRADAY},
        )
        msg = format_confluence_alert(confluence)
        assert "BANKNIFTY" in msg
        assert "MODERATE" in msg
        assert "oi_wall_breach" in msg


class TestOnConfluenceCallback:
    def test_publishes_to_confluence_stream_and_sends_alert(self):
        redis = MagicMock()
        on_confluence = make_on_confluence(redis)

        confluence = Confluence(
            symbol="NIFTY",
            direction=Direction.BULLISH,
            signals=[
                Signal(symbol="NIFTY", direction=Direction.BULLISH, source="vwap_cross",
                       layer=Layer.LIVE, strength=SignalStrength.STRONG),
            ],
            layers_involved={Layer.LIVE, Layer.INTRADAY},
        )

        with patch("services.signal_intelligence.worker.TELEGRAM_NOTIFICATIONS") as tg:
            on_confluence(confluence)

        redis.xadd.assert_called_once()
        stream_name, fields = redis.xadd.call_args[0]
        assert stream_name == "intelligence:confluence"
        assert fields["symbol"] == "NIFTY"

        tg.send_live_options_notification.assert_called_once()
        sent_msg = tg.send_live_options_notification.call_args[0][0]
        assert "NIFTY" in sent_msg

    def test_xadd_failure_does_not_block_alert(self):
        redis = MagicMock()
        redis.xadd.side_effect = Exception("redis down")
        on_confluence = make_on_confluence(redis)

        confluence = Confluence(
            symbol="NIFTY", direction=Direction.BULLISH,
            signals=[
                Signal(symbol="NIFTY", direction=Direction.BULLISH, source="vwap_cross",
                       layer=Layer.LIVE, strength=SignalStrength.STRONG),
            ],
            layers_involved={Layer.LIVE, Layer.INTRADAY},
        )

        with patch("services.signal_intelligence.worker.TELEGRAM_NOTIFICATIONS") as tg:
            on_confluence(confluence)  # must not raise

        tg.send_live_options_notification.assert_called_once()


class TestCorrelatorIntegration:
    """End-to-end: reconstruct signals from stream-shaped fields across
    layers, feed the real SignalCorrelator, confirm confluence fires and
    on_confluence is invoked -- this is the scenario that was structurally
    broken before (nothing fed >=2 layers into one correlator)."""

    def test_two_layers_fire_moderate_confluence(self):
        import time

        on_confluence = MagicMock()
        correlator = SignalCorrelator(on_confluence=on_confluence)

        now = time.time()
        live_fields = _signal_fields(layer="LIVE", direction="BULLISH", source="vwap_cross", timestamp=now)
        intraday_fields = _signal_fields(layer="INTRADAY", direction="BULLISH", source="rsi_divergence", timestamp=now)

        correlator.on_signal(reconstruct_signal(live_fields))
        correlator.on_signal(reconstruct_signal(intraday_fields))

        on_confluence.assert_called_once()
        confluence = on_confluence.call_args[0][0]
        assert confluence.level == "MODERATE"
        assert confluence.layers_involved == {Layer.LIVE, Layer.INTRADAY}

    def test_single_layer_never_fires(self):
        import time

        on_confluence = MagicMock()
        correlator = SignalCorrelator(on_confluence=on_confluence)

        now = time.time()
        correlator.on_signal(reconstruct_signal(_signal_fields(layer="LIVE", source="a", timestamp=now)))
        correlator.on_signal(reconstruct_signal(_signal_fields(layer="LIVE", source="b", timestamp=now)))

        on_confluence.assert_not_called()
