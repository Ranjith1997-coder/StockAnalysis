"""Tests for zerodha/zerodha_analysis.py — ZerodhaTickerManager tick routing."""
import time
import queue
import threading
import pytest
from unittest.mock import MagicMock, patch, call

from zerodha.zerodha_analysis import ZerodhaTickerManager
from common.token_registry import TokenInfo, TokenType, OptionZone
from tests.zerodha.conftest import (
    make_stock, make_equity_info, make_index_info,
    make_option_info, make_future_info,
)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

_TELEGRAM = "zerodha.zerodha_analysis.TELEGRAM_NOTIFICATIONS"
_SHARED   = "zerodha.zerodha_analysis.shared"


def _manager():
    with patch("zerodha.zerodha_analysis.KiteTicker"):
        mgr = ZerodhaTickerManager("user", "pass", "enctoken")
    return mgr


def _setup_registry(mgr, info, parent_obj):
    """Wire a mock registry that returns *info* for its token and *parent_obj* as parent."""
    reg = MagicMock()
    reg.lookup.return_value = info
    reg.get_parent_object.return_value = parent_obj
    reg.get_strike_gap.return_value = 50.0

    ctx = MagicMock()
    ctx.token_registry = reg
    ctx.stock_token_obj_dict = {}
    ctx.index_token_obj_dict = {}
    ctx.commodity_token_obj_dict = {}
    ctx.global_indices_token_obj_dict = {}
    ctx.stockExpires = []
    ctx.signal_bus = None

    mgr._ctx = ctx
    return reg, ctx


# ── _route_tick dispatch ──────────────────────────────────────────────────────

class TestRouteTick:
    def test_none_token_returns_early(self, patched_app_ctx):
        mgr = _manager()
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch.object(mgr, "_process_equity_tick") as mock_eq:
                mgr._route_tick({"no_token": True})
        mock_eq.assert_not_called()

    def test_unknown_token_returns_early(self, patched_app_ctx):
        """Registry returns None → nothing processed."""
        mgr = _manager()
        patched_app_ctx.token_registry.lookup.return_value = None
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch.object(mgr, "_process_equity_tick") as mock_eq:
                mgr._route_tick({"instrument_token": 9999})
        mock_eq.assert_not_called()

    def test_equity_token_calls_process_equity_tick(self, patched_app_ctx):
        mgr = _manager()
        info = make_equity_info(token=1234, symbol="RELIANCE")
        patched_app_ctx.token_registry.lookup.return_value = info
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch.object(mgr, "_process_equity_tick") as mock_eq:
                mgr._route_tick({"instrument_token": 1234})
        mock_eq.assert_called_once()

    def test_index_token_calls_process_equity_tick(self, patched_app_ctx):
        mgr = _manager()
        info = make_index_info(token=256265, symbol="NIFTY")
        patched_app_ctx.token_registry.lookup.return_value = info
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch.object(mgr, "_process_equity_tick") as mock_eq:
                mgr._route_tick({"instrument_token": 256265})
        mock_eq.assert_called_once()

    def test_option_token_calls_process_option_tick(self, patched_app_ctx):
        mgr = _manager()
        info = make_option_info(token=99001, symbol="NIFTY")
        patched_app_ctx.token_registry.lookup.return_value = info
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch.object(mgr, "_process_option_tick") as mock_opt:
                mgr._route_tick({"instrument_token": 99001})
        mock_opt.assert_called_once()

    def test_future_token_calls_process_future_tick(self, patched_app_ctx):
        mgr = _manager()
        info = make_future_info(token=55001, symbol="NIFTY")
        patched_app_ctx.token_registry.lookup.return_value = info
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch.object(mgr, "_process_future_tick") as mock_fut:
                mgr._route_tick({"instrument_token": 55001})
        mock_fut.assert_called_once()

    def test_none_registry_calls_legacy_fallback(self, patched_app_ctx):
        mgr = _manager()
        patched_app_ctx.token_registry = None
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch.object(mgr, "_process_equity_tick_legacy") as mock_leg:
                mgr._route_tick({"instrument_token": 1234})
        mock_leg.assert_called_once()


# ── _process_equity_tick ──────────────────────────────────────────────────────

class TestProcessEquityTick:
    def test_calls_update_zerodha_data(self, patched_app_ctx):
        mgr = _manager()
        parent = make_stock("RELIANCE")
        info = make_equity_info(token=1234, symbol="RELIANCE")
        patched_app_ctx.token_registry.get_parent_object.return_value = parent
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            tick = {"instrument_token": 1234, "last_price": 2900.0}
            mgr._process_equity_tick(tick, info)
        parent.update_zerodha_data.assert_called_once_with(tick)

    def test_calls_live_stock_engine_on_tick_for_equity(self, patched_app_ctx):
        mgr = _manager()
        parent = make_stock("RELIANCE")
        info = make_equity_info(token=1234, symbol="RELIANCE")
        patched_app_ctx.token_registry.get_parent_object.return_value = parent
        live_engine = MagicMock()
        mgr.live_stock_engine = live_engine
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            mgr._process_equity_tick({"instrument_token": 1234}, info)
        live_engine.on_tick.assert_called_once_with(parent)

    def test_no_live_stock_engine_no_crash(self, patched_app_ctx):
        mgr = _manager()
        parent = make_stock("RELIANCE")
        info = make_equity_info(token=1234, symbol="RELIANCE")
        patched_app_ctx.token_registry.get_parent_object.return_value = parent
        mgr.live_stock_engine = None
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            mgr._process_equity_tick({"instrument_token": 1234}, info)  # must not raise

    def test_index_tick_calls_check_recentering(self, patched_app_ctx):
        mgr = _manager()
        parent = make_stock("NIFTY", 22000.0)
        info = make_index_info(token=256265, symbol="NIFTY")
        patched_app_ctx.token_registry.get_parent_object.return_value = parent
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch.object(mgr, "_check_recentering") as mock_rc:
                mgr._process_equity_tick(
                    {"instrument_token": 256265, "last_price": 22000.0}, info
                )
        mock_rc.assert_called_once_with("NIFTY", 22000.0)

    def test_none_parent_returns_early(self, patched_app_ctx):
        mgr = _manager()
        info = make_equity_info(token=1234, symbol="RELIANCE")
        patched_app_ctx.token_registry.get_parent_object.return_value = None
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            # Should not raise
            mgr._process_equity_tick({"instrument_token": 1234}, info)


# ── _process_option_tick ──────────────────────────────────────────────────────

class TestProcessOptionTick:
    def test_calls_update_option_tick(self, patched_app_ctx):
        mgr = _manager()
        parent = make_stock("NIFTY")
        info = make_option_info(token=99001, symbol="NIFTY", strike=22000.0, opt_type="CE")
        patched_app_ctx.token_registry.get_parent_object.return_value = parent
        tick = {"instrument_token": 99001, "last_price": 250.0}
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            mgr._process_option_tick(tick, info)
        parent.update_option_tick.assert_called_once_with(22000.0, "CE", tick)

    def test_aggregate_recomputed_first_time(self, patched_app_ctx):
        """First option tick → last_aggregate_time is 0 → recompute fires."""
        mgr = _manager()
        parent = make_stock("NIFTY", 22000.0)
        parent.zerodha_data = {"last_price": 22000.0}
        info = make_option_info(token=99001, symbol="NIFTY")
        patched_app_ctx.token_registry.get_parent_object.return_value = parent
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch("zerodha.zerodha_analysis.time.time", return_value=100.0):
                mgr._process_option_tick({"instrument_token": 99001}, info)
        parent.recompute_options_aggregate.assert_called_once()

    def test_aggregate_not_recomputed_within_interval(self, patched_app_ctx):
        mgr = _manager()
        parent = make_stock("NIFTY", 22000.0)
        parent.zerodha_data = {"last_price": 22000.0}
        info = make_option_info(token=99001, symbol="NIFTY")
        patched_app_ctx.token_registry.get_parent_object.return_value = parent
        mgr._last_aggregate_time["NIFTY"] = 100.0
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch("zerodha.zerodha_analysis.time.time", return_value=100.5):  # < 1s
                mgr._process_option_tick({"instrument_token": 99001}, info)
        parent.recompute_options_aggregate.assert_not_called()

    def test_live_options_engine_called_when_set(self, patched_app_ctx):
        mgr = _manager()
        parent = make_stock("NIFTY", 22000.0)
        parent.zerodha_data = {"last_price": 22000.0}
        info = make_option_info(token=99001, symbol="NIFTY")
        patched_app_ctx.token_registry.get_parent_object.return_value = parent
        live_opts = MagicMock()
        mgr.live_options_engine = live_opts
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch("zerodha.zerodha_analysis.time.time", return_value=200.0):
                mgr._process_option_tick({"instrument_token": 99001}, info)
        live_opts.on_aggregate_updated.assert_called_once_with(parent, 22000.0)

    def test_none_parent_returns_early(self, patched_app_ctx):
        mgr = _manager()
        info = make_option_info(token=99001, symbol="NIFTY")
        patched_app_ctx.token_registry.get_parent_object.return_value = None
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            mgr._process_option_tick({"instrument_token": 99001}, info)  # must not raise


# ── _process_future_tick ──────────────────────────────────────────────────────

class TestProcessFutureTick:
    def test_current_expiry_key_is_current(self, patched_app_ctx):
        mgr = _manager()
        parent = make_stock("NIFTY")
        expiry = "2026-04-24"
        info = make_future_info(token=55001, symbol="NIFTY", expiry=expiry)
        patched_app_ctx.token_registry.get_parent_object.return_value = parent
        patched_app_ctx.stockExpires = [expiry, "2026-05-29"]
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            mgr._process_future_tick({"instrument_token": 55001}, info)
        parent.update_futures_tick.assert_called_once_with("current", {"instrument_token": 55001})

    def test_next_expiry_key_is_next(self, patched_app_ctx):
        mgr = _manager()
        parent = make_stock("NIFTY")
        info = make_future_info(token=55001, symbol="NIFTY", expiry="2026-05-29")
        patched_app_ctx.token_registry.get_parent_object.return_value = parent
        patched_app_ctx.stockExpires = ["2026-04-24", "2026-05-29"]
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            mgr._process_future_tick({"instrument_token": 55001}, info)
        parent.update_futures_tick.assert_called_once_with("next", {"instrument_token": 55001})

    def test_none_parent_returns_early(self, patched_app_ctx):
        mgr = _manager()
        info = make_future_info(token=55001, symbol="NIFTY")
        patched_app_ctx.token_registry.get_parent_object.return_value = None
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            mgr._process_future_tick({"instrument_token": 55001}, info)  # must not raise


# ── _check_recentering ────────────────────────────────────────────────────────

class TestCheckRecentering:
    def test_first_call_sets_last_atm_no_subscribe(self, patched_app_ctx):
        mgr = _manager()
        patched_app_ctx.token_registry.round_to_strike = MagicMock(return_value=22000.0)
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            mgr._check_recentering("NIFTY", 22000.0)
        assert mgr._last_atm.get("NIFTY") == 22000.0

    def test_shift_below_threshold_no_recenter(self, patched_app_ctx):
        mgr = _manager()
        mgr._last_atm["NIFTY"] = 22000.0
        patched_app_ctx.token_registry.round_to_strike = MagicMock(return_value=22000.0)
        patched_app_ctx.token_registry.get_strike_gap.return_value = 50.0
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            mgr._check_recentering("NIFTY", 22020.0)  # < 1 strike gap
        patched_app_ctx.token_registry.recenter_and_get_subscription_changes.assert_not_called()

    def test_shift_gte_threshold_calls_recenter(self, patched_app_ctx):
        mgr = _manager()
        mgr._last_atm["NIFTY"] = 22000.0
        mgr._kt = None  # no active WebSocket — just testing the recenter logic
        patched_app_ctx.token_registry.round_to_strike = MagicMock(return_value=22050.0)
        patched_app_ctx.token_registry.get_strike_gap.return_value = 50.0
        patched_app_ctx.token_registry.recenter_and_get_subscription_changes.return_value = ([], [], {})
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            mgr._check_recentering("NIFTY", 22060.0)  # 1 strike gap shifted
        patched_app_ctx.token_registry.recenter_and_get_subscription_changes.assert_called_once()

    def test_none_registry_returns_early(self, patched_app_ctx):
        mgr = _manager()
        patched_app_ctx.token_registry = None
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            mgr._check_recentering("NIFTY", 22000.0)  # must not raise


# ── on_ticks ──────────────────────────────────────────────────────────────────

class TestOnTicks:
    def test_each_tick_put_on_queue(self):
        mgr = _manager()
        ticks = [{"instrument_token": 1}, {"instrument_token": 2}]
        mgr.on_ticks(None, ticks)
        queued = []
        while not mgr.tick_queue.empty():
            queued.append(mgr.tick_queue.get_nowait())
        assert queued == ticks

    def test_empty_ticks_list_no_error(self):
        mgr = _manager()
        mgr.on_ticks(None, [])  # must not raise
        assert mgr.tick_queue.empty()


# ── on_connect / on_close ─────────────────────────────────────────────────────

class TestWebSocketCallbacks:
    def test_on_connect_sets_connected_true(self, patched_app_ctx):
        mgr = _manager()
        ws = MagicMock()
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch.object(mgr, "start_tick_processor"):
                mgr.on_connect(ws, {})
        assert mgr.connected is True

    def test_on_connect_subscribes_base_tokens(self, patched_app_ctx):
        mgr = _manager()
        patched_app_ctx.index_token_obj_dict = {256265: MagicMock()}
        patched_app_ctx.stock_token_obj_dict = {1234: MagicMock()}
        ws = MagicMock()
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch.object(mgr, "start_tick_processor"):
                mgr.on_connect(ws, {})
        ws.subscribe.assert_called_once()
        subscribed = ws.subscribe.call_args[0][0]
        assert 256265 in subscribed
        assert 1234 in subscribed

    def test_on_connect_index_only_mode_skips_equities(self, patched_app_ctx):
        mgr = _manager()
        mgr.index_only_mode = True
        patched_app_ctx.index_token_obj_dict = {256265: MagicMock()}
        patched_app_ctx.stock_token_obj_dict = {1234: MagicMock()}
        ws = MagicMock()
        with patch(_SHARED) as mock_shared:
            mock_shared.app_ctx = patched_app_ctx
            with patch.object(mgr, "start_tick_processor"):
                mgr.on_connect(ws, {})
        subscribed = ws.subscribe.call_args[0][0]
        assert 1234 not in subscribed
        assert 256265 in subscribed

    def test_on_close_sets_connected_false(self):
        mgr = _manager()
        mgr.connected = True
        with patch.object(mgr, "stop_tick_processor"):
            mgr.on_close(None, 1000, "normal")
        assert mgr.connected is False

    def test_on_close_calls_stop_tick_processor(self):
        mgr = _manager()
        with patch.object(mgr, "stop_tick_processor") as mock_stop:
            mgr.on_close(None, 1000, "normal")
        mock_stop.assert_called_once()


# ── send_notification ─────────────────────────────────────────────────────────

class TestSendNotification:
    def test_calls_telegram_with_message(self):
        mgr = _manager()
        stock = MagicMock()
        stock.stockName = "Reliance"
        stock.stock_symbol = "RELIANCE"
        with patch(_TELEGRAM) as mock_tele:
            mgr.send_notification(stock, "BUY", 10_000, 5_000)
        mock_tele.send_notification.assert_called_once()
        msg = mock_tele.send_notification.call_args[0][0]
        assert "RELIANCE" in msg
        assert "10000" in msg

    def test_direction_included_in_message(self):
        mgr = _manager()
        stock = MagicMock()
        stock.stockName = "HDFC"
        stock.stock_symbol = "HDFCBANK"
        with patch(_TELEGRAM) as mock_tele:
            mgr.send_notification(stock, "SELL", 3_000, 8_000)
        msg = mock_tele.send_notification.call_args[0][0]
        assert "SELL" in msg


# ── tick processor thread ─────────────────────────────────────────────────────

class TestTickProcessor:
    def test_start_creates_daemon_thread(self):
        mgr = _manager()
        mgr.start_tick_processor()
        assert mgr.processor_thread is not None
        assert mgr.processor_thread.daemon is True
        mgr.stop_tick_processor()

    def test_stop_sets_flag_and_joins(self):
        mgr = _manager()
        mgr.start_tick_processor()
        mgr.stop_tick_processor()
        assert mgr.stop_processor is True
        # Thread should have joined (not alive)
        assert not mgr.processor_thread.is_alive()
