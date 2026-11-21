"""Correlator independence: confluence level counts distinct signal families."""
import time
from unittest.mock import MagicMock

import pytest

from lib.intelligence.correlator import Confluence, SignalCorrelator, signal_family
from lib.intelligence.signal import Direction, Layer, Signal, SignalStrength


def _sig(source, layer, direction=Direction.BULLISH, ts=None):
    return Signal(symbol="NIFTY", direction=direction, source=source, layer=layer,
                  strength=SignalStrength.MODERATE, timestamp=ts or time.time())


@pytest.mark.parametrize("source,family", [
    ("pcr_bias", "pcr"), ("pcr_crossover_bullish", "pcr"), ("pcr_sustained_bearish", "pcr"),
    ("oi_wall", "oi"), ("ce_wall_breach", "oi"), ("gex_wall_breach", "gex"),
    ("iv_spike", "iv"), ("skew_flip_bullish", "iv"), ("future_action", "futures"),
    ("max_pain", "max_pain"), ("rsi_divergence", "technical"), ("vwap_cross", "technical"),
    ("double_candle_stick_pattern", "technical"), ("volume_breakout", "volume"),
    ("something_new", "something_new"),
])
def test_signal_family(source, family):
    assert signal_family(source) == family


def test_same_family_across_three_layers_is_moderate():
    c = Confluence(
        symbol="NIFTY", direction=Direction.BULLISH,
        signals=[_sig("pcr_crossover_bullish", Layer.LIVE),
                 _sig("pcr_bias", Layer.INTRADAY),
                 _sig("pcr_bias", Layer.POSITIONAL)],
        layers_involved={Layer.LIVE, Layer.INTRADAY, Layer.POSITIONAL},
    )
    assert c.families == {"pcr"}
    assert c.level == "MODERATE"


def test_two_families_across_three_layers_is_high_and_round_trips():
    c = Confluence(
        symbol="NIFTY", direction=Direction.BULLISH,
        signals=[_sig("pcr_crossover_bullish", Layer.LIVE),
                 _sig("pcr_bias", Layer.INTRADAY),
                 _sig("oi_wall", Layer.POSITIONAL)],
        layers_involved={Layer.LIVE, Layer.INTRADAY, Layer.POSITIONAL},
    )
    assert c.level == "HIGH"
    fields = c.to_stream_fields()
    assert fields["families"] == "oi,pcr"
    assert Confluence.from_stream_fields(fields).level == "HIGH"


def test_correlator_pcr_only_confluence_not_high():
    cb = MagicMock()
    corr = SignalCorrelator(on_confluence=cb)
    now = time.time()
    corr.on_signal(_sig("pcr_bias", Layer.POSITIONAL, ts=now))
    corr.on_signal(_sig("pcr_bias", Layer.INTRADAY, ts=now))   # fires MODERATE (2 layers)
    cb.assert_called_once()
    assert cb.call_args[0][0].level == "MODERATE"
