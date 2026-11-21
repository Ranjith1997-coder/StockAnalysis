"""/straddle + /walls: DTE-scaled expected move, SENSEX support, session wall delta from open_oi."""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import lib.notification.commands.market as market
from common.Stock import Stock


def _expiry_in(trading_days: int) -> str:
    d, n = date.today(), 0
    while True:
        if d.weekday() < 5:
            n += 1
            if n == trading_days:
                return d.isoformat()
        d += timedelta(days=1)


def _stock():
    s = Stock("Nifty 50", "NIFTY", is_index=True)
    s.ltp = 24000.0
    s.update_option_tick(24000.0, "CE", {"last_price": 110.0, "oi": 1000, "open_oi": 800})
    s.update_option_tick(24000.0, "PE", {"last_price": 90.0, "oi": 1500, "open_oi": 1600})
    s.recompute_options_aggregate(spot_price=24000.0)
    return s


def _ctx(symbol):
    ctx = SimpleNamespace(args=[symbol], bot=MagicMock())
    ctx.bot.send_message = AsyncMock()
    return ctx


def _update():
    return SimpleNamespace(effective_chat=SimpleNamespace(id=1))


@pytest.fixture(autouse=True)
def _weekday_calendar(monkeypatch):
    monkeypatch.setattr("common.market_calendar.is_trading_day", lambda d=None: d.weekday() < 5)


def test_sensex_supported():
    assert {"NIFTY", "BANKNIFTY", "SENSEX"} <= market._OPTIONS_SYMBOLS


def test_session_wall_delta_from_open_oi():
    live = {24000.0: {"CE": {"oi": 1000, "open_oi": 800}, "PE": {"oi": 5}}}
    assert market._session_wall_delta(live, 24000.0, "CE") == (800, 1000)
    assert market._session_wall_delta(live, 24000.0, "PE") is None
    assert market._session_wall_delta(live, 25000.0, "CE") is None


def test_expected_moves_dte_scaled():
    move, dte = market._expected_moves(200.0, _expiry_in(4))
    assert dte == 4
    assert move == pytest.approx(100.0)


async def test_straddle_shows_dte_scaled_range(monkeypatch):
    s = _stock()
    s._tick_store.set_options_expiry(_expiry_in(4))
    monkeypatch.setattr(market, "_resolve_options_stock", lambda sym: (s, None))
    ctx = _ctx("NIFTY")
    await market.cmd_straddle.__wrapped__(_update(), ctx)
    text = ctx.bot.send_message.call_args.kwargs["text"]
    # straddle 200, DTE 4 → ±100 → 23,900 ↔ 24,100
    assert "1-day expected move" in text
    assert "23,900.00" in text and "24,100.00" in text
    assert "4 DTE" in text
    assert "1-SD" not in text


async def test_walls_session_delta_rendered(monkeypatch):
    s = _stock()
    monkeypatch.setattr(market, "_resolve_options_stock", lambda sym: (s, None))
    ctx = _ctx("NIFTY")
    await market.cmd_walls.__wrapped__(_update(), ctx)
    text = ctx.bot.send_message.call_args.kwargs["text"]
    assert "Session: <b>+200 built</b>" in text
    assert "Session: <b>100 unwound</b>" in text
