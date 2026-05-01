"""Tests for zerodha/live_stock_engine.py — LiveStockEngine signal detection."""
import time
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, time as dtime

from zerodha.live_stock_engine import LiveStockEngine
from intelligence.signal import Direction, SignalStrength


# ── Helpers ───────────────────────────────────────────────────────────────────

def _engine():
    bus = MagicMock()
    return LiveStockEngine(bus), bus


def _stock(symbol="RELIANCE", last_price=2900.0, vwap=2800.0,
           buy_q=10_000, sell_q=5_000, high=2950.0, low=2850.0):
    s = MagicMock()
    s.stock_symbol = symbol
    s.zerodha_data = {
        "last_price": last_price,
        "average_traded_price": vwap,
        "total_buy_quantity": buy_q,
        "total_sell_quantity": sell_q,
        "high": high,
        "low": low,
    }
    return s


# ── Tick throttle ─────────────────────────────────────────────────────────────

class TestTickThrottle:
    def test_second_call_within_interval_is_skipped(self):
        engine, bus = _engine()
        s = _stock()
        with patch("zerodha.live_stock_engine.time.time", return_value=1000.0):
            engine.on_tick(s)
        first_call_count = bus.emit.call_count
        # Same second — must be below TICK_INTERVAL
        with patch("zerodha.live_stock_engine.time.time", return_value=1000.1):
            engine.on_tick(s)
        assert bus.emit.call_count == first_call_count  # no new emit

    def test_call_after_interval_is_processed(self):
        engine, bus = _engine()
        # Build VWAP side so a cross fires on second call
        s = _stock(last_price=2900.0, vwap=2950.0)   # price < vwap → "below"
        with patch("zerodha.live_stock_engine.time.time", return_value=1000.0):
            engine.on_tick(s)
        # Beyond TICK_INTERVAL (5 s) and beyond SIGNAL_COOLDOWN (300 s)
        s2 = _stock(last_price=3000.0, vwap=2950.0)   # price > vwap → cross to "above"
        with patch("zerodha.live_stock_engine.time.time", return_value=1310.0):
            engine.on_tick(s2)
        assert bus.emit.call_count >= 1


# ── Zero price guard ──────────────────────────────────────────────────────────

class TestOnTickGuards:
    def test_zero_price_returns_early_no_emit(self):
        engine, bus = _engine()
        s = _stock(last_price=0)
        engine.on_tick(s)
        bus.emit.assert_not_called()

    def test_negative_price_returns_early(self):
        engine, bus = _engine()
        s = _stock(last_price=-5.0)
        engine.on_tick(s)
        bus.emit.assert_not_called()


# ── VWAP Cross ────────────────────────────────────────────────────────────────

class TestVwapCross:
    def _run(self, engine, symbol, price, vwap, t=0.0):
        s = _stock(symbol=symbol, last_price=price, vwap=vwap)
        with patch("zerodha.live_stock_engine.time.time", return_value=t):
            engine._check_vwap_cross(symbol, price, s.zerodha_data)

    def test_first_call_no_previous_side_no_emit(self):
        engine, bus = _engine()
        self._run(engine, "X", price=100.0, vwap=90.0)
        bus.emit.assert_not_called()

    def test_below_to_above_emits_bullish(self):
        engine, bus = _engine()
        self._run(engine, "X", price=80.0, vwap=100.0, t=0.0)   # below
        self._run(engine, "X", price=110.0, vwap=100.0, t=400.0) # above → cross
        bus.emit.assert_called_once()
        sig = bus.emit.call_args[0][0]
        assert sig.direction == Direction.BULLISH

    def test_above_to_below_emits_bearish(self):
        engine, bus = _engine()
        self._run(engine, "X", price=110.0, vwap=100.0, t=0.0)   # above
        self._run(engine, "X", price=80.0, vwap=100.0, t=400.0)   # below → cross
        bus.emit.assert_called_once()
        sig = bus.emit.call_args[0][0]
        assert sig.direction == Direction.BEARISH

    def test_same_side_no_emit(self):
        engine, bus = _engine()
        self._run(engine, "X", price=110.0, vwap=100.0, t=0.0)
        self._run(engine, "X", price=120.0, vwap=100.0, t=400.0)  # still above
        bus.emit.assert_not_called()

    def test_zero_vwap_no_emit(self):
        engine, bus = _engine()
        engine._check_vwap_cross("X", 100.0, {"average_traded_price": 0})
        bus.emit.assert_not_called()


# ── Bid/Ask Imbalance ─────────────────────────────────────────────────────────

class TestBidAskImbalance:
    def _run(self, engine, symbol, buy_q, sell_q, t=400.0):
        data = {"total_buy_quantity": buy_q, "total_sell_quantity": sell_q}
        with patch("zerodha.live_stock_engine.time.time", return_value=t):
            engine._check_bid_ask_imbalance(symbol, 100.0, data)

    def test_high_ratio_emits_bullish(self):
        engine, bus = _engine()
        self._run(engine, "X", buy_q=30_000, sell_q=10_000)  # ratio=3.0 > 2.5
        bus.emit.assert_called_once()
        assert bus.emit.call_args[0][0].direction == Direction.BULLISH

    def test_low_ratio_emits_bearish(self):
        engine, bus = _engine()
        self._run(engine, "X", buy_q=2_000, sell_q=10_000)   # ratio=0.2 < 0.4
        bus.emit.assert_called_once()
        assert bus.emit.call_args[0][0].direction == Direction.BEARISH

    def test_ratio_in_normal_range_no_emit(self):
        engine, bus = _engine()
        self._run(engine, "X", buy_q=10_000, sell_q=10_000)  # ratio=1.0
        bus.emit.assert_not_called()

    def test_zero_sell_qty_no_emit(self):
        engine, bus = _engine()
        self._run(engine, "X", buy_q=10_000, sell_q=0)
        bus.emit.assert_not_called()

    def test_zero_buy_qty_no_emit(self):
        engine, bus = _engine()
        self._run(engine, "X", buy_q=0, sell_q=10_000)
        bus.emit.assert_not_called()


# ── Opening Range Build ───────────────────────────────────────────────────────

class TestORBBuild:
    def test_during_opening_range_no_emit(self):
        engine, bus = _engine()
        mock_time = dtime(9, 20)
        with patch("zerodha.live_stock_engine.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = mock_time
            engine._check_orb("X", 100.0, {})
        bus.emit.assert_not_called()

    def test_orb_high_tracked(self):
        engine, bus = _engine()
        with patch("zerodha.live_stock_engine.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = dtime(9, 20)
            engine._check_orb("X", 105.0, {})
            engine._check_orb("X", 110.0, {})
        assert engine._orb["X"]["high"] == 110.0

    def test_orb_low_tracked(self):
        engine, bus = _engine()
        with patch("zerodha.live_stock_engine.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = dtime(9, 20)
            engine._check_orb("X", 105.0, {})
            engine._check_orb("X", 100.0, {})
        assert engine._orb["X"]["low"] == 100.0


# ── ORB Breakout ──────────────────────────────────────────────────────────────

class TestORBBreakout:
    def _seed_orb(self, engine, symbol, high=110.0, low=100.0):
        engine._orb[symbol] = {"high": high, "low": low}

    def _run_post930(self, engine, symbol, price, t=400.0):
        with patch("zerodha.live_stock_engine.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = dtime(9, 35)
            with patch("zerodha.live_stock_engine.time.time", return_value=t):
                engine._check_orb(symbol, price, {})

    def test_breakout_above_high_emits_bullish_strong(self):
        engine, bus = _engine()
        self._seed_orb(engine, "X")
        self._run_post930(engine, "X", price=115.0)
        bus.emit.assert_called_once()
        sig = bus.emit.call_args[0][0]
        assert sig.direction == Direction.BULLISH
        assert sig.strength == SignalStrength.STRONG

    def test_breakdown_below_low_emits_bearish_strong(self):
        engine, bus = _engine()
        self._seed_orb(engine, "X")
        self._run_post930(engine, "X", price=95.0)
        bus.emit.assert_called_once()
        sig = bus.emit.call_args[0][0]
        assert sig.direction == Direction.BEARISH
        assert sig.strength == SignalStrength.STRONG

    def test_price_inside_range_no_emit(self):
        engine, bus = _engine()
        self._seed_orb(engine, "X")
        self._run_post930(engine, "X", price=105.0)
        bus.emit.assert_not_called()

    def test_no_orb_data_no_emit(self):
        engine, bus = _engine()
        # No ORB seeded for symbol Y
        self._run_post930(engine, "Y", price=200.0)
        bus.emit.assert_not_called()


# ── Day High/Low Break ────────────────────────────────────────────────────────

class TestDayHighLow:
    def _run(self, engine, symbol, price, high, low, t_time, t_ts=0.0):
        data = {"high": high, "low": low}
        with patch("zerodha.live_stock_engine.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t_time
            with patch("zerodha.live_stock_engine.time.time", return_value=t_ts):
                engine._check_day_high_low(symbol, price, data)

    def test_new_day_high_after_945_emits_bullish(self):
        engine, bus = _engine()
        # Seed a previous high
        engine._day_high["X"] = 100.0
        self._run(engine, "X", price=110.0, high=110.0, low=90.0,
                  t_time=dtime(10, 0), t_ts=400.0)
        bus.emit.assert_called_once()
        assert bus.emit.call_args[0][0].direction == Direction.BULLISH

    def test_new_day_low_after_945_emits_bearish(self):
        engine, bus = _engine()
        engine._day_low["X"] = 100.0
        self._run(engine, "X", price=88.0, high=110.0, low=88.0,
                  t_time=dtime(10, 0), t_ts=400.0)
        bus.emit.assert_called_once()
        assert bus.emit.call_args[0][0].direction == Direction.BEARISH

    def test_before_945_no_emit(self):
        engine, bus = _engine()
        engine._day_high["X"] = 100.0
        self._run(engine, "X", price=115.0, high=115.0, low=90.0,
                  t_time=dtime(9, 30))
        bus.emit.assert_not_called()

    def test_same_high_no_emit(self):
        engine, bus = _engine()
        engine._day_high["X"] = 110.0
        self._run(engine, "X", price=110.0, high=110.0, low=90.0,
                  t_time=dtime(10, 0), t_ts=400.0)
        bus.emit.assert_not_called()

    def test_missing_high_low_data_no_emit(self):
        engine, bus = _engine()
        with patch("zerodha.live_stock_engine.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = dtime(10, 0)
            engine._check_day_high_low("X", 100.0, {})
        bus.emit.assert_not_called()


# ── Signal cooldown ───────────────────────────────────────────────────────────

class TestSignalCooldown:
    # Use a base time far above 0.0 so (base - 0.0_default) >= 300 → first emit always fires.
    _BASE = 1_000_000.0

    def test_second_signal_within_cooldown_not_emitted(self):
        engine, bus = _engine()
        # First emit: _BASE - 0.0_default = 1M >= 300 → fires
        with patch("zerodha.live_stock_engine.time.time", return_value=self._BASE):
            engine._emit("X", "vwap_cross", Direction.BULLISH, SignalStrength.MODERATE, {})
        first = bus.emit.call_count   # 1
        # Second attempt 100s later — within 300s cooldown → blocked
        with patch("zerodha.live_stock_engine.time.time", return_value=self._BASE + 100.0):
            engine._emit("X", "vwap_cross", Direction.BULLISH, SignalStrength.MODERATE, {})
        assert bus.emit.call_count == first

    def test_signal_emitted_after_cooldown_expires(self):
        engine, bus = _engine()
        with patch("zerodha.live_stock_engine.time.time", return_value=self._BASE):
            engine._emit("X", "vwap_cross", Direction.BULLISH, SignalStrength.MODERATE, {})
        # 400s later — beyond 300s cooldown → fires again
        with patch("zerodha.live_stock_engine.time.time", return_value=self._BASE + 400.0):
            engine._emit("X", "vwap_cross", Direction.BULLISH, SignalStrength.MODERATE, {})
        assert bus.emit.call_count == 2


# ── reset_day ─────────────────────────────────────────────────────────────────

class TestResetDay:
    def test_orb_cleared(self):
        engine, _ = _engine()
        engine._orb["X"] = {"high": 100, "low": 90}
        engine.reset_day()
        assert engine._orb == {}

    def test_vwap_side_cleared(self):
        engine, _ = _engine()
        engine._last_vwap_side["X"] = "above"
        engine.reset_day()
        assert engine._last_vwap_side == {}

    def test_day_high_cleared(self):
        engine, _ = _engine()
        engine._day_high["X"] = 100.0
        engine.reset_day()
        assert engine._day_high == {}

    def test_day_low_cleared(self):
        engine, _ = _engine()
        engine._day_low["X"] = 90.0
        engine.reset_day()
        assert engine._day_low == {}

    def test_last_signal_cleared(self):
        engine, _ = _engine()
        engine._last_signal[("X", "vwap_cross")] = 123.0
        engine.reset_day()
        assert engine._last_signal == {}

    def test_tick_time_cleared(self):
        engine, _ = _engine()
        engine._last_tick_time["X"] = 999.0
        engine.reset_day()
        assert dict(engine._last_tick_time) == {}
