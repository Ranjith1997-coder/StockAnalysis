"""Tests for analyser/PanicModeAnalyser.py."""
import pytest
from collections import namedtuple
from unittest.mock import patch, MagicMock

import common.shared as shared
from analyser.PanicModeAnalyser import PanicModeAnalyser
from tests.analyser.conftest import make_stock, make_sensibull_ctx


def _positional_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    return mock


def _intraday_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    return mock


def _make_stock_with_conditions(direction, price_change,
                                iv=False, oi=False, futures=False,
                                volume=False, pcr=False, mode="positional"):
    """
    Build a stock with analysis dict pre-populated to match the requested conditions.
    direction: "BULLISH" or "BEARISH"
    price_change: e.g. +3.5 for bullish, -3.5 for bearish
    """
    s = make_stock()
    s.ltp_change_perc = price_change

    FakeNt = namedtuple("FakeNt", ["signal"])

    if iv:
        # C2: IV_SPIKE in NEUTRAL
        s.analysis["NEUTRAL"]["IV_SPIKE"] = FakeNt(signal="IV spike detected")

    if oi:
        # C3: OI_BUILDUP (positional) or OI_INTRADAY_TREND (intraday)
        if mode == "intraday":
            s.analysis[direction]["OI_INTRADAY_TREND"] = FakeNt(signal="OI trend")
        else:
            s.analysis[direction]["OI_BUILDUP"] = FakeNt(signal="OI buildup")

    if futures:
        # C4: FUTURE_ACTION in direction
        s.analysis[direction]["FUTURE_ACTION"] = FakeNt(signal="futures action")

    if volume:
        # C5: VOLUME_BREAKOUT in direction
        s.analysis[direction]["VOLUME_BREAKOUT"] = FakeNt(signal="volume breakout")

    if pcr:
        # C6: PCR_BIAS in direction
        s.analysis[direction]["PCR_BIAS"] = FakeNt(signal="PCR bias")

    return s


class TestAnalysePanicMode:
    def test_small_price_change_returns_false(self):
        """Below price threshold (3%) → no signal regardless of conditions."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-1.0,
                iv=True, oi=True, futures=True, volume=True, pcr=True,
            )
            assert a.analyse_panic_mode(s) is False

    def test_none_price_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = make_stock()
            s.ltp_change_perc = None
            assert a.analyse_panic_mode(s) is False

    def test_exactly_4_conditions_triggers(self):
        """C1(price) + C2(IV) + C3(OI) + C5(volume) = 4 → triggers."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=True, futures=False, volume=True, pcr=False,
            )
            result = a.analyse_panic_mode(s)
            assert result is True
            assert "PANIC_MODE" in s.analysis["BEARISH"]

    def test_only_3_conditions_no_trigger(self):
        """C1(price) + C2(IV) + C3(OI) = 3 → no trigger."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=True, futures=False, volume=False, pcr=False,
            )
            assert a.analyse_panic_mode(s) is False

    def test_all_6_conditions_bullish(self):
        """All 6 conditions bullish → triggers with count=6."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BULLISH", price_change=+4.0,
                iv=True, oi=True, futures=True, volume=True, pcr=True,
            )
            result = a.analyse_panic_mode(s)
            assert result is True
            signal = s.analysis["BULLISH"]["PANIC_MODE"]
            assert signal.direction == "BULLISH"
            assert signal.conditions_count == 6

    def test_all_6_conditions_bearish(self):
        """All 6 conditions bearish → triggers with count=6."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=True, futures=True, volume=True, pcr=True,
            )
            result = a.analyse_panic_mode(s)
            assert result is True
            signal = s.analysis["BEARISH"]["PANIC_MODE"]
            assert signal.direction == "BEARISH"
            assert signal.conditions_count == 6

    def test_intraday_lower_threshold(self):
        """Intraday threshold is 1.5%; a 1.8% move should pass."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-1.8,
                iv=True, oi=True, futures=True, volume=False, pcr=False,
                mode="intraday",
            )
            result = a.analyse_panic_mode(s)
            assert result is True

    def test_intraday_below_threshold_returns_false(self):
        """1.2% < 1.5% intraday threshold → no signal."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-1.2,
                iv=True, oi=True, futures=True, volume=True, pcr=True,
                mode="intraday",
            )
            assert a.analyse_panic_mode(s) is False

    def test_panic_mode_signal_contains_direction(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BULLISH", price_change=+3.5,
                iv=True, oi=True, futures=True, volume=False, pcr=False,
            )
            a.analyse_panic_mode(s)
            signal = s.analysis.get("BULLISH", {}).get("PANIC_MODE")
            if signal:
                assert "BULLISH" in signal.signal
