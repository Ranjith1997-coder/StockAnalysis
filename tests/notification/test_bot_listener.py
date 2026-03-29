"""
Unit tests for notification/bot_listener.py

Covers:
- is_urlencoded()
- update_enctoken_in_env()
- _find_stock_by_symbol()
- _build_gainers_losers()
- cmd_help()
- cmd_status()
- cmd_ltp()
- cmd_gainers() / cmd_losers()
- cmd_holidays()
- cmd_watchlist()
"""
import os
import pytest
import pytest_asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

# conftest.py provides: make_update, FakeStock, FakeAppCtx, patch_app_ctx
from tests.conftest import make_update, FakeStock, FakeAppCtx

import common.shared as shared
import notification.bot_listener as bl


# ════════════════════════════════════════════════════════════════════════════
# is_urlencoded()
# ════════════════════════════════════════════════════════════════════════════

class TestIsUrlEncoded:

    def test_plain_token_is_not_encoded(self):
        assert bl.is_urlencoded("abc123") is False

    def test_encoded_token_detected(self):
        assert bl.is_urlencoded("abc%3D123") is True

    def test_empty_string_is_not_encoded(self):
        assert bl.is_urlencoded("") is False

    def test_fully_decoded_url_is_not_encoded(self):
        assert bl.is_urlencoded("hello world") is False

    def test_percent_with_valid_escape_is_encoded(self):
        assert bl.is_urlencoded("token%20value") is True


# ════════════════════════════════════════════════════════════════════════════
# update_enctoken_in_env()
# ════════════════════════════════════════════════════════════════════════════

class TestUpdateEnctokenInEnv:

    def test_creates_env_file_when_missing(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        monkeypatch.chdir(tmp_path)
        bl.update_enctoken_in_env("NEW_TOKEN")
        assert env_file.exists()
        assert "ZERODHA_ENC_TOKEN=NEW_TOKEN" in env_file.read_text()

    def test_updates_existing_token_line(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("ZERODHA_ENC_TOKEN=OLD_TOKEN\nSOME_OTHER=value\n")
        monkeypatch.chdir(tmp_path)
        bl.update_enctoken_in_env("NEW_TOKEN")
        content = env_file.read_text()
        assert "ZERODHA_ENC_TOKEN=NEW_TOKEN" in content
        assert "ZERODHA_ENC_TOKEN=OLD_TOKEN" not in content
        assert "SOME_OTHER=value" in content

    def test_appends_token_when_key_absent(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER_KEY=value\n")
        monkeypatch.chdir(tmp_path)
        bl.update_enctoken_in_env("MY_TOKEN")
        content = env_file.read_text()
        assert "ZERODHA_ENC_TOKEN=MY_TOKEN" in content
        assert "OTHER_KEY=value" in content

    def test_updates_os_environ(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        bl.update_enctoken_in_env("TOKEN_ENV")
        assert os.environ.get("ZERODHA_ENC_TOKEN") == "TOKEN_ENV"


# ════════════════════════════════════════════════════════════════════════════
# _find_stock_by_symbol()
# ════════════════════════════════════════════════════════════════════════════

class TestFindStockBySymbol:

    @pytest.fixture(autouse=True)
    def setup_ctx(self):
        ctx = FakeAppCtx()
        nifty = FakeStock("NIFTY", ltp=22000.0)
        reliance = FakeStock("RELIANCE", ltp=2900.0)
        gold = FakeStock("GOLD", ltp=70000.0)
        spx = FakeStock("SPX", ltp=5200.0)

        ctx.index_token_obj_dict = {1: nifty}
        ctx.stock_token_obj_dict = {2: reliance}
        ctx.commodity_token_obj_dict = {3: gold}
        ctx.global_indices_token_obj_dict = {4: spx}

        original = shared.app_ctx
        shared.app_ctx = ctx
        yield ctx
        shared.app_ctx = original

    def test_finds_stock_by_exact_symbol(self):
        assert bl._find_stock_by_symbol("RELIANCE").stock_symbol == "RELIANCE"

    def test_finds_index(self):
        assert bl._find_stock_by_symbol("NIFTY").stock_symbol == "NIFTY"

    def test_finds_commodity(self):
        assert bl._find_stock_by_symbol("GOLD").stock_symbol == "GOLD"

    def test_finds_global_index(self):
        assert bl._find_stock_by_symbol("SPX").stock_symbol == "SPX"

    def test_case_insensitive(self):
        assert bl._find_stock_by_symbol("reliance").stock_symbol == "RELIANCE"

    def test_returns_none_for_unknown_symbol(self):
        assert bl._find_stock_by_symbol("UNKNOWN") is None


# ════════════════════════════════════════════════════════════════════════════
# _build_gainers_losers()
# ════════════════════════════════════════════════════════════════════════════

class TestBuildGainersLosers:

    @pytest.fixture(autouse=True)
    def setup_ctx(self):
        self.ctx = FakeAppCtx()
        original = shared.app_ctx
        shared.app_ctx = self.ctx
        yield
        shared.app_ctx = original

    def _add_stock(self, symbol, ltp, prev_close):
        stock = FakeStock(symbol, ltp=ltp, prev_close=prev_close)
        token = abs(hash(symbol)) % 100000
        self.ctx.stock_token_obj_dict[token] = stock

    def test_empty_dict_returns_empty_lists(self):
        gainers, losers = bl._build_gainers_losers()
        assert gainers == []
        assert losers == []

    def test_positive_change_goes_to_gainers(self):
        self._add_stock("AAPL", ltp=110.0, prev_close=100.0)
        gainers, losers = bl._build_gainers_losers()
        symbols = [s for s, _ in gainers]
        assert "AAPL" in symbols
        assert losers == []

    def test_negative_change_goes_to_losers(self):
        self._add_stock("WIPRO", ltp=90.0, prev_close=100.0)
        gainers, losers = bl._build_gainers_losers()
        symbols = [s for s, _ in losers]
        assert "WIPRO" in symbols
        assert gainers == []

    def test_gainers_are_sorted_descending(self):
        self._add_stock("A", ltp=120.0, prev_close=100.0)  # +20%
        self._add_stock("B", ltp=105.0, prev_close=100.0)  # +5%
        self._add_stock("C", ltp=115.0, prev_close=100.0)  # +15%
        gainers, _ = bl._build_gainers_losers()
        pcts = [pct for _, pct in gainers]
        assert pcts == sorted(pcts, reverse=True)

    def test_losers_are_sorted_ascending(self):
        self._add_stock("X", ltp=80.0, prev_close=100.0)   # -20%
        self._add_stock("Y", ltp=95.0, prev_close=100.0)   # -5%
        self._add_stock("Z", ltp=85.0, prev_close=100.0)   # -15%
        _, losers = bl._build_gainers_losers()
        pcts = [pct for _, pct in losers]
        assert pcts == sorted(pcts)

    def test_returns_top_5_only(self):
        for i in range(10):
            self._add_stock(f"G{i}", ltp=100 + i + 1, prev_close=100.0)
        gainers, _ = bl._build_gainers_losers()
        assert len(gainers) == 5

    def test_stock_with_none_ltp_is_skipped(self):
        stock = FakeStock("BAD", ltp=None, prev_close=100.0)
        stock.ltp = None
        self.ctx.stock_token_obj_dict[9999] = stock
        gainers, losers = bl._build_gainers_losers()
        symbols = [s for s, _ in gainers + losers]
        assert "BAD" not in symbols


# ════════════════════════════════════════════════════════════════════════════
# Async command handlers — common setup
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=False)
def clean_ctx():
    """Replace shared.app_ctx with a fresh FakeAppCtx, restore after."""
    ctx = FakeAppCtx()
    original = shared.app_ctx
    shared.app_ctx = ctx
    yield ctx
    shared.app_ctx = original


# ════════════════════════════════════════════════════════════════════════════
# cmd_help()
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cmd_help_sends_message():
    update, context = make_update()
    await bl.cmd_help(update, context)
    context.bot.send_message.assert_called_once()
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "/help" in text
    assert "/ltp" in text
    assert "/gainers" in text
    assert "/watchlist" in text
    assert "/holidays" in text


# ════════════════════════════════════════════════════════════════════════════
# cmd_status()
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cmd_status_mode_not_set(clean_ctx):
    clean_ctx.mode = None
    update, context = make_update()
    with patch("common.market_calendar.is_trading_day", return_value=True):
        await bl.cmd_status(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "NOT SET" in text


@pytest.mark.asyncio
async def test_cmd_status_shows_stock_count(clean_ctx):
    from common.shared import Mode
    clean_ctx.mode = Mode.INTRADAY
    clean_ctx.stock_token_obj_dict = {1: FakeStock("A"), 2: FakeStock("B")}
    clean_ctx.index_token_obj_dict = {3: FakeStock("NIFTY")}
    update, context = make_update()
    with patch("common.market_calendar.is_trading_day", return_value=True):
        await bl.cmd_status(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "2" in text   # stocks tracked


@pytest.mark.asyncio
async def test_cmd_status_websocket_disconnected(clean_ctx):
    tm = MagicMock()
    tm.connected = False
    clean_ctx.zd_ticker_manager = tm
    update, context = make_update()
    with patch("common.market_calendar.is_trading_day", return_value=False):
        await bl.cmd_status(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "Disconnected" in text


# ════════════════════════════════════════════════════════════════════════════
# cmd_ltp()
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cmd_ltp_no_args_sends_usage(clean_ctx):
    update, context = make_update()
    context.args = []
    await bl.cmd_ltp(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "Usage" in text

@pytest.mark.asyncio
async def test_cmd_ltp_unknown_symbol(clean_ctx):
    update, context = make_update()
    context.args = ["UNKNOWN"]
    await bl.cmd_ltp(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "not found" in text.lower()

@pytest.mark.asyncio
async def test_cmd_ltp_no_price_yet(clean_ctx):
    stock = FakeStock("TCS", ltp=None)
    clean_ctx.stock_token_obj_dict = {1: stock}
    update, context = make_update()
    context.args = ["TCS"]
    await bl.cmd_ltp(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "No price data" in text

@pytest.mark.asyncio
async def test_cmd_ltp_positive_change(clean_ctx):
    stock = FakeStock("INFY", ltp=1500.0, change_perc=2.5, prev_close=1463.0)
    clean_ctx.stock_token_obj_dict = {1: stock}
    update, context = make_update()
    context.args = ["INFY"]
    await bl.cmd_ltp(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "INFY" in text
    assert "1500.00" in text
    assert "+2.50%" in text

@pytest.mark.asyncio
async def test_cmd_ltp_negative_change(clean_ctx):
    stock = FakeStock("WIPRO", ltp=400.0, change_perc=-3.1, prev_close=413.0)
    clean_ctx.stock_token_obj_dict = {1: stock}
    update, context = make_update()
    context.args = ["WIPRO"]
    await bl.cmd_ltp(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "-3.10%" in text


# ════════════════════════════════════════════════════════════════════════════
# cmd_gainers() / cmd_losers()
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cmd_gainers_empty(clean_ctx):
    update, context = make_update()
    await bl.cmd_gainers(update, context)
    text = context.bot.send_message.call_args.args[0] \
           if context.bot.send_message.call_args.args \
           else context.bot.send_message.call_args.kwargs.get("text", "")
    assert "No gainer" in text

@pytest.mark.asyncio
async def test_cmd_gainers_populated(clean_ctx):
    for i, sym in enumerate(["AA", "BB", "CC"]):
        clean_ctx.stock_token_obj_dict[i] = FakeStock(sym, ltp=110.0, prev_close=100.0)
    update, context = make_update()
    await bl.cmd_gainers(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "Gainers" in text

@pytest.mark.asyncio
async def test_cmd_losers_empty(clean_ctx):
    update, context = make_update()
    await bl.cmd_losers(update, context)
    text = context.bot.send_message.call_args.args[0] \
           if context.bot.send_message.call_args.args \
           else context.bot.send_message.call_args.kwargs.get("text", "")
    assert "No loser" in text

@pytest.mark.asyncio
async def test_cmd_losers_populated(clean_ctx):
    for i, sym in enumerate(["DD", "EE", "FF"]):
        clean_ctx.stock_token_obj_dict[i] = FakeStock(sym, ltp=90.0, prev_close=100.0)
    update, context = make_update()
    await bl.cmd_losers(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "Losers" in text


# ════════════════════════════════════════════════════════════════════════════
# cmd_holidays()
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cmd_holidays_trading_day(clean_ctx):
    update, context = make_update()
    with patch("common.market_calendar.is_trading_day", return_value=True), \
         patch("common.market_calendar.get_upcoming_holidays", return_value=[]):
        await bl.cmd_holidays(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "NSE Market Holidays" in text
    assert "Open" in text
    assert "No holidays" in text

@pytest.mark.asyncio
async def test_cmd_holidays_shows_upcoming(clean_ctx):
    import datetime
    holiday = datetime.date(2026, 4, 14)
    update, context = make_update()
    with patch("notification.bot_listener.datetime") as mock_dt, \
         patch("common.market_calendar.is_trading_day", return_value=True), \
         patch("common.market_calendar.get_upcoming_holidays",
               return_value=[holiday]):
        mock_dt.now.return_value.date.return_value = datetime.date(2026, 3, 30)
        await bl.cmd_holidays(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "1 holiday" in text

@pytest.mark.asyncio
async def test_cmd_holidays_handles_exception(clean_ctx):
    update, context = make_update()
    with patch("common.market_calendar.is_trading_day",
               side_effect=Exception("network error")):
        await bl.cmd_holidays(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "Could not fetch" in text


# ════════════════════════════════════════════════════════════════════════════
# cmd_watchlist()
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cmd_watchlist_empty_ctx(clean_ctx):
    update, context = make_update()
    await bl.cmd_watchlist(update, context)
    context.bot.send_message.assert_called()
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "Subscription Overview" in text
    assert "Total instruments: 0" in text

@pytest.mark.asyncio
async def test_cmd_watchlist_shows_indices(clean_ctx):
    clean_ctx.index_list = ["NIFTY", "BANKNIFTY"]
    clean_ctx.index_token_obj_dict = {
        1: FakeStock("NIFTY"), 2: FakeStock("BANKNIFTY")
    }
    update, context = make_update()
    await bl.cmd_watchlist(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "BANKNIFTY" in text
    assert "NIFTY" in text

@pytest.mark.asyncio
async def test_cmd_watchlist_shows_total_instruments(clean_ctx):
    clean_ctx.stock_token_obj_dict = {i: FakeStock(f"S{i}") for i in range(5)}
    clean_ctx.index_token_obj_dict = {10: FakeStock("NIFTY")}
    update, context = make_update()
    await bl.cmd_watchlist(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "Total instruments: 6" in text

@pytest.mark.asyncio
async def test_cmd_watchlist_websocket_connected(clean_ctx):
    tm = MagicMock()
    tm.connected = True
    tm.index_only_mode = False
    clean_ctx.zd_ticker_manager = tm
    clean_ctx.stock_token_obj_dict = {1: FakeStock("X"), 2: FakeStock("Y")}
    clean_ctx.index_token_obj_dict = {3: FakeStock("NIFTY")}
    update, context = make_update()
    await bl.cmd_watchlist(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "Connected" in text
    assert "Full (Equity + Index)" in text

@pytest.mark.asyncio
async def test_cmd_watchlist_websocket_disconnected(clean_ctx):
    tm = MagicMock()
    tm.connected = False
    clean_ctx.zd_ticker_manager = tm
    update, context = make_update()
    await bl.cmd_watchlist(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "Disconnected" in text

@pytest.mark.asyncio
async def test_cmd_watchlist_shows_expiries(clean_ctx):
    clean_ctx.stockExpires = ["2026-03-27", "2026-04-24"]
    update, context = make_update()
    await bl.cmd_watchlist(update, context)
    text = context.bot.send_message.call_args.kwargs.get("text", "")
    assert "Current" in text
    assert "2026-03-27" in text
    assert "Next" in text

@pytest.mark.asyncio
async def test_cmd_watchlist_long_message_is_split(clean_ctx):
    """If message exceeds 4096 chars the handler must call send_message multiple times."""
    # index_list has no preview limit — 300 long names generate a message > 4096 chars
    long_names = [f"VERY_LONG_INDEX_NAME_{i:04d}" for i in range(300)]
    clean_ctx.index_list = long_names
    clean_ctx.index_token_obj_dict = {i: FakeStock(long_names[i]) for i in range(300)}
    update, context = make_update()
    await bl.cmd_watchlist(update, context)
    assert context.bot.send_message.call_count >= 2
