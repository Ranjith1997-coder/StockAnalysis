"""Tests for zerodha/live_options_engine.py — LiveOptionsEngine."""
import time
import pytest
from unittest.mock import MagicMock, patch, call

from zerodha.live_options_engine import LiveOptionsEngine
from common.constants import LIVE_OPTIONS_INDICES
from intelligence.signal import Direction


# ── Helpers ───────────────────────────────────────────────────────────────────

_TELEGRAM = "zerodha.live_options_engine.TELEGRAM_NOTIFICATIONS"
_APP_CTX  = "common.shared.app_ctx"


def _engine():
    return LiveOptionsEngine()


def _stock(symbol="NIFTY"):
    s = MagicMock()
    s.stock_symbol = symbol
    s.options_aggregate = {"pcr": 1.0, "ce_oi": 100, "pe_oi": 100}
    s.options_live = {}
    s.ltp = 22000.0
    return s


def _fake_ctx(signal_bus=None):
    ctx = MagicMock()
    ctx.signal_bus = signal_bus
    return ctx


# ── _throttled ────────────────────────────────────────────────────────────────

class TestThrottled:
    def test_returns_false_when_never_fired(self):
        engine = _engine()
        assert engine._throttled("NIFTY", "PCR_CROSSOVER_BULLISH") is False

    def test_returns_true_immediately_after_fire(self):
        engine = _engine()
        engine._last_alert[("NIFTY", "PCR_CROSSOVER_BULLISH")] = time.time()
        assert engine._throttled("NIFTY", "PCR_CROSSOVER_BULLISH") is True

    def test_returns_false_after_cooldown_expires(self):
        engine = _engine()
        cooldown = engine.COOLDOWNS["PCR_CROSSOVER_BULLISH"]
        engine._last_alert[("NIFTY", "PCR_CROSSOVER_BULLISH")] = (
            time.time() - cooldown - 1
        )
        assert engine._throttled("NIFTY", "PCR_CROSSOVER_BULLISH") is False

    def test_uses_per_alert_type_cooldown(self):
        """Shorter cooldown alert type expires before longer one."""
        engine = _engine()
        # PCR_CROSSOVER_BULLISH cooldown = 600s, RANGE_BOUNDARY = 1800s
        elapsed = 700  # beyond 600 but within 1800
        engine._last_alert[("NIFTY", "PCR_CROSSOVER_BULLISH")] = time.time() - elapsed
        engine._last_alert[("NIFTY", "RANGE_BOUNDARY")] = time.time() - elapsed
        assert engine._throttled("NIFTY", "PCR_CROSSOVER_BULLISH") is False
        assert engine._throttled("NIFTY", "RANGE_BOUNDARY") is True

    def test_unknown_alert_type_defaults_to_600s(self):
        engine = _engine()
        # Fire 500s ago — within default 600s
        engine._last_alert[("NIFTY", "UNKNOWN_ALERT")] = time.time() - 500
        assert engine._throttled("NIFTY", "UNKNOWN_ALERT") is True

    def test_different_symbols_independent(self):
        engine = _engine()
        engine._last_alert[("NIFTY", "PCR_CROSSOVER_BULLISH")] = time.time()
        # BANKNIFTY was never fired
        assert engine._throttled("BANKNIFTY", "PCR_CROSSOVER_BULLISH") is False


# ── _fire ─────────────────────────────────────────────────────────────────────

class TestFire:
    def test_sends_telegram_notification(self):
        engine = _engine()
        with patch(_TELEGRAM) as mock_tele:
            with patch(_APP_CTX, _fake_ctx()):
                engine._fire("NIFTY", "PCR_CROSSOVER_BULLISH", "bullish pcr")
        mock_tele.send_live_options_notification.assert_called_once_with("bullish pcr", symbol="NIFTY")

    def test_updates_last_alert_timestamp(self):
        engine = _engine()
        before = time.time()
        with patch(_TELEGRAM):
            with patch(_APP_CTX, _fake_ctx()):
                engine._fire("NIFTY", "IV_EXPANDING", "iv msg")
        assert engine._last_alert[("NIFTY", "IV_EXPANDING")] >= before

    def test_emits_signal_to_signal_bus(self):
        engine = _engine()
        bus = MagicMock()
        with patch(_TELEGRAM):
            with patch(_APP_CTX, _fake_ctx(signal_bus=bus)):
                engine._fire("NIFTY", "PCR_CROSSOVER_BULLISH", "msg")
        bus.emit.assert_called_once()
        sig = bus.emit.call_args[0][0]
        assert sig.symbol == "NIFTY"
        assert sig.direction == Direction.BULLISH

    def test_no_crash_when_signal_bus_is_none(self):
        engine = _engine()
        with patch(_TELEGRAM):
            with patch(_APP_CTX, _fake_ctx(signal_bus=None)):
                engine._fire("NIFTY", "PCR_CROSSOVER_BEARISH", "msg")  # should not raise

    def test_bearish_alert_emits_bearish_direction(self):
        engine = _engine()
        bus = MagicMock()
        with patch(_TELEGRAM):
            with patch(_APP_CTX, _fake_ctx(signal_bus=bus)):
                engine._fire("NIFTY", "PCR_CROSSOVER_BEARISH", "msg")
        sig = bus.emit.call_args[0][0]
        assert sig.direction == Direction.BEARISH


# ── on_aggregate_updated ──────────────────────────────────────────────────────

class TestOnAggregateUpdated:
    def test_skips_symbol_not_in_live_options_indices(self):
        engine = _engine()
        s = _stock("RELIANCE")
        with patch.object(engine, "_run_oi_checks") as mock_oi:
            with patch.object(engine, "_run_straddle_checks") as mock_str:
                engine.on_aggregate_updated(s, 2900.0)
        mock_oi.assert_not_called()
        mock_str.assert_not_called()

    def test_skips_when_spot_zero(self):
        engine = _engine()
        s = _stock("NIFTY")
        with patch.object(engine, "_run_oi_checks") as mock_oi:
            engine.on_aggregate_updated(s, 0)
        mock_oi.assert_not_called()

    def test_skips_when_spot_negative(self):
        engine = _engine()
        s = _stock("NIFTY")
        with patch.object(engine, "_run_oi_checks") as mock_oi:
            engine.on_aggregate_updated(s, -100.0)
        mock_oi.assert_not_called()

    def test_calls_oi_checks_for_valid_index(self):
        engine = _engine()
        s = _stock(LIVE_OPTIONS_INDICES[0])
        with patch.object(engine, "_run_oi_checks") as mock_oi:
            with patch.object(engine, "_run_straddle_checks"):
                engine.on_aggregate_updated(s, 22000.0)
        mock_oi.assert_called_once()

    def test_calls_straddle_checks_for_valid_index(self):
        engine = _engine()
        s = _stock(LIVE_OPTIONS_INDICES[0])
        with patch.object(engine, "_run_oi_checks"):
            with patch.object(engine, "_run_straddle_checks") as mock_str:
                engine.on_aggregate_updated(s, 22000.0)
        mock_str.assert_called_once()

    def test_exception_in_checks_does_not_propagate(self):
        engine = _engine()
        s = _stock(LIVE_OPTIONS_INDICES[0])
        with patch.object(engine, "_run_oi_checks", side_effect=RuntimeError("boom")):
            with patch.object(engine, "_run_straddle_checks"):
                engine.on_aggregate_updated(s, 22000.0)  # must not raise

    def test_history_recorded_for_valid_index(self):
        engine = _engine()
        s = _stock(LIVE_OPTIONS_INDICES[0])
        with patch.object(engine, "_run_oi_checks"):
            with patch.object(engine, "_run_straddle_checks"):
                engine.on_aggregate_updated(s, 22000.0)
        assert engine.get_history(LIVE_OPTIONS_INDICES[0]) is not None


# ── get_history ───────────────────────────────────────────────────────────────

class TestGetHistory:
    def test_none_before_any_aggregate_update(self):
        engine = _engine()
        assert engine.get_history("NIFTY") is None

    def test_returns_history_after_update(self):
        engine = _engine()
        s = _stock(LIVE_OPTIONS_INDICES[0])
        with patch.object(engine, "_run_oi_checks"):
            with patch.object(engine, "_run_straddle_checks"):
                engine.on_aggregate_updated(s, 22000.0)
        assert engine.get_history(LIVE_OPTIONS_INDICES[0]) is not None


# ── Lazy analyser creation ────────────────────────────────────────────────────

class TestLazyAnalysers:
    def test_oi_analyser_same_instance_on_repeat_call(self):
        engine = _engine()
        a1 = engine._oi_analyser("NIFTY", 50.0)
        a2 = engine._oi_analyser("NIFTY", 50.0)
        assert a1 is a2

    def test_straddle_analyser_same_instance_on_repeat_call(self):
        engine = _engine()
        a1 = engine._straddle_analyser("NIFTY")
        a2 = engine._straddle_analyser("NIFTY")
        assert a1 is a2

    def test_different_symbols_get_different_oi_analysers(self):
        engine = _engine()
        a_nifty = engine._oi_analyser("NIFTY", 50.0)
        a_bank  = engine._oi_analyser("BANKNIFTY", 100.0)
        assert a_nifty is not a_bank

    def test_different_symbols_get_different_straddle_analysers(self):
        engine = _engine()
        a_nifty = engine._straddle_analyser("NIFTY")
        a_bank  = engine._straddle_analyser("BANKNIFTY")
        assert a_nifty is not a_bank


# ── COOLDOWNS dict ────────────────────────────────────────────────────────────

class TestCooldownsConfig:
    def test_all_known_alert_types_have_cooldowns(self):
        expected = {
            "PCR_CROSSOVER_BULLISH", "PCR_CROSSOVER_BEARISH",
            "PCR_EXTREME_PE", "PCR_EXTREME_CE",
            "CE_WALL_BREACH", "PE_WALL_BREACH",
            "IV_EXPANDING", "IV_COMPRESSING",
            "RANGE_BOUNDARY",
            "SKEW_FLIP_BULLISH", "SKEW_FLIP_BEARISH",
            "PCR_SUSTAINED_BULLISH", "PCR_SUSTAINED_BEARISH",
        }
        assert expected.issubset(set(LiveOptionsEngine.COOLDOWNS.keys()))

    def test_range_boundary_cooldown_is_longest(self):
        cooldowns = LiveOptionsEngine.COOLDOWNS
        assert cooldowns["RANGE_BOUNDARY"] >= max(
            v for k, v in cooldowns.items() if k != "RANGE_BOUNDARY"
        )
