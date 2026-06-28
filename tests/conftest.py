"""
Shared fixtures used across all test modules.

Provides:
- mock_stock()      : lightweight Stock-like object
- mock_app_ctx()    : patch shared.app_ctx with a pre-populated AppContext
- make_update()     : Telegram Update + ContextTypes mock pair
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Minimal Stock-like object ─────────────────────────────────────────────

class FakeStock:
    """Mimics the attributes of common.Stock used by bot_listener helpers."""

    def __init__(
        self,
        symbol: str,
        ltp: float = 100.0,
        change_perc: float = 1.5,
        prev_close: float = 98.5,
    ):
        import pandas as pd
        self.stockName = symbol
        self.stock_symbol = symbol
        self.is_index = False
        self.ltp = ltp
        self.ltp_change_perc = change_perc
        self.daily_hv = 25.0
        self.prevDayOHLCV = {"OPEN": 98, "HIGH": 101, "LOW": 97, "CLOSE": prev_close, "VOLUME": 1000000}
        self._priceData = pd.DataFrame()
        self.zerodha_ctx = {}
        self.futures_live = {}
        self.zerodha_data = {}
        self.sensibull_ctx = {
            "last_fetch_time": None,
            "current": {},
            "historical_data": pd.DataFrame(),
            "oi_chain_history": [],
            "iv_chart_history": pd.DataFrame(),
            "oi_history": pd.DataFrame(),
        }
        self.analysis = {"Timestamp": None, "BULLISH": {}, "BEARISH": {}, "NEUTRAL": {}, "NoOfTrends": 0}
        self.options_aggregate = {}

    @property
    def priceData(self):
        return self._priceData


@pytest.fixture
def mock_stock():
    """Return a factory for FakeStock instances."""
    return FakeStock


# ── Patchable AppContext ──────────────────────────────────────────────────

class FakeAppCtx:
    """Minimal AppContext used in tests."""

    def __init__(self):
        self.stock_token_obj_dict = {}
        self.index_token_obj_dict = {}
        self.commodity_token_obj_dict = {}
        self.global_indices_token_obj_dict = {}
        self.stocks_list = []
        self.index_list = []
        self.commodity_list = []
        self.global_indices_list = []
        self.stockExpires = []
        self.mode = None
        self.zd_ticker_manager = None
        self.zd_kc = None
        self.token_registry = None
        self.signal_bus = None
        self.correlator = None
        self.narrator = None
        self.last_equity_tick_time = 0.0
        self.llm_budget_warned = False
        self.options_source = "zerodha"
        self.sensibull_feed = None
        self.intraday_cycle_count = 0
        self.monitor_result_counts = {"SUCCESS": 0, "NO_DATA": 0, "ERROR": 0}
        self.error_count = 0
        self.last_cycle_time = 0.0


@pytest.fixture
def fake_ctx():
    """Return a fresh FakeAppCtx."""
    return FakeAppCtx()


@pytest.fixture
def patch_app_ctx(fake_ctx):
    """Patch shared.app_ctx for the duration of a test."""
    import common.shared as shared
    original = shared.app_ctx
    shared.app_ctx = fake_ctx
    yield fake_ctx
    shared.app_ctx = original


# ── Telegram mock helpers ─────────────────────────────────────────────────

def make_update(chat_id: int = 12345):
    """Return (update, context) mocks suitable for passing to bot command handlers."""
    # Ensure guard allows the test chat ID (force-set, overriding .env)
    import os
    os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = str(chat_id)
    os.environ["TELEGRAM_DEBUG_CHAT_ID"] = ""
    from notification.commands._guard import init_guard
    init_guard()

    update = MagicMock()
    update.effective_chat.id = chat_id

    bot = AsyncMock()
    bot.send_message = AsyncMock()

    context = MagicMock()
    context.bot = bot
    context.args = []

    return update, context
