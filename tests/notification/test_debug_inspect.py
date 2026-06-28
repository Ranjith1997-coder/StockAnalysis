"""Tests for notification/commands/debug_inspect.py — pure inspection functions."""
import pytest
import time
import pandas as pd
from unittest.mock import MagicMock, patch

import common.shared as shared
from tests.conftest import FakeStock, FakeAppCtx


@pytest.fixture
def setup_ctx(monkeypatch):
    """Set up a fake app_ctx with stocks, signal_bus, correlator."""
    ctx = FakeAppCtx()
    ctx.mode = shared.Mode.INTRADAY
    ctx.intraday_cycle_count = 5
    ctx.last_cycle_time = time.time()
    ctx.error_count = 2
    ctx.monitor_result_counts = {"SUCCESS": 10, "NO_DATA": 3, "ERROR": 2}
    ctx.options_source = "both"

    # Add a stock
    stock = FakeStock("RELIANCE", ltp=1313.6, change_perc=0.6, prev_close=1305.7)
    ctx.stock_token_obj_dict = {12345: stock}

    # Mock signal_bus
    ctx.signal_bus = MagicMock()
    ctx.signal_bus.total_emitted = 42

    # Mock correlator
    ctx.correlator = MagicMock()
    ctx.correlator.total_confluences = 3
    ctx.correlator.get_buffer_snapshot.return_value = []

    # Mock ticker manager
    tm = MagicMock()
    tm.connected = True
    tm._tick_count = 1500
    tm.reconnect_attempts = 1
    tm.tick_queue.qsize.return_value = 5
    tm._unknown_tokens = {"UNKNOWN1"}
    ctx.zd_ticker_manager = tm

    original = shared.app_ctx
    shared.app_ctx = ctx
    yield ctx
    shared.app_ctx = original


class TestInspectOverview:

    def test_returns_dict_with_expected_keys(self, setup_ctx):
        from notification.commands.debug_inspect import inspect_overview
        d = inspect_overview()
        assert d["mode"] == "INTRADAY"
        assert d["intraday_cycle_count"] == 5
        assert d["stocks"] == 1
        assert d["signals_emitted"] == 42
        assert d["confluences"] == 3
        assert d["ws_connected"] is True
        assert d["ws_tick_count"] == 1500
        assert d["error_count"] == 2
        assert d["monitor_results"] == {"SUCCESS": 10, "NO_DATA": 3, "ERROR": 2}


class TestInspectStock:

    def test_found_symbol(self, setup_ctx):
        from notification.commands.debug_inspect import inspect_stock
        d = inspect_stock("RELIANCE")
        assert d["symbol"] == "RELIANCE"
        assert d["ltp"] == 1313.6
        assert d["daily_hv"] == 25.0
        assert "priceData" in d
        assert "analysis" in d
        assert "sensibull" in d

    def test_not_found_symbol(self, setup_ctx):
        from notification.commands.debug_inspect import inspect_stock
        d = inspect_stock("NONEXIST")
        assert "error" in d


class TestInspectSignals:

    def test_without_symbol(self, setup_ctx):
        from notification.commands.debug_inspect import inspect_signals
        d = inspect_signals()
        assert d["signals_emitted"] == 42
        assert d["confluences"] == 3

    def test_with_symbol(self, setup_ctx):
        from notification.commands.debug_inspect import inspect_signals
        d = inspect_signals("RELIANCE")
        assert d["signals_emitted"] == 42
        assert d["symbol"] == "RELIANCE"
        assert "active_signals" in d
        assert d["active_signal_count"] == 0


class TestInspectCounters:

    def test_returns_all_counters(self, setup_ctx):
        from notification.commands.debug_inspect import inspect_counters
        d = inspect_counters()
        assert d["intraday_cycle_count"] == 5
        assert d["signals_emitted"] == 42
        assert d["confluences"] == 3
        assert d["error_count"] == 2
        assert d["ws_tick_count"] == 1500
        assert d["monitor_results"] == {"SUCCESS": 10, "NO_DATA": 3, "ERROR": 2}


class TestInspectMemory:

    def test_returns_appctx_layout(self, setup_ctx):
        from notification.commands.debug_inspect import inspect_memory
        d = inspect_memory()
        assert d["mode"] == "INTRADAY"
        assert d["stock_token_obj_dict"] == 1
        assert d["options_source"] == "both"
        assert "memory_rss_mb" in d
