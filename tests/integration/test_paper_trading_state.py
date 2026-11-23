"""Integration: paper-trading state transitions are exactly-once (T4.3/T4.4).

Two threads can evaluate the same position (MTM thread + exit-signal consumer).
The bookkeeping must charge the P&L once, never resurrect a closed position, and
reset daily counters on a new trading day without losing cumulative P&L.
"""
from __future__ import annotations

import threading
import time

import pytest

from services.paper_trading import ledger
from services.paper_trading import main as pt_main
from services.paper_trading.models import (
    ACCOUNT_KEY,
    POSITIONS_OPEN_KEY,
    PaperAccount,
    PaperPosition,
    OptionLeg,
    cooldown_key,
    daily_pnl_key,
    positions_closed_key,
    TRADES_STREAM,
)

pytestmark = pytest.mark.integration

MARGIN = 120_000.0
CREDIT = 12_000.0
PNL = 3_500.0


class _TelegramStub:
    def __init__(self) -> None:
        self.entries = 0
        self.exits = 0

    def send_live_options_notification(self, *_args, **_kwargs) -> None:
        pass


def _position(position_id: str = "pos-1", *, closed: bool = False) -> PaperPosition:
    legs = [
        OptionLeg(strike=25_100.0, option_type="CE", side="SELL", lots=1,
                  entry_premium=120.0, current_premium=60.0),
        OptionLeg(strike=25_200.0, option_type="CE", side="BUY", lots=1,
                  entry_premium=60.0, current_premium=30.0),
    ]
    return PaperPosition(
        position_id=position_id,
        symbol="NIFTY",
        strategy="CREDIT_SPREAD",
        mode="intraday",
        direction="BEARISH",
        legs=legs,
        expiry="2026-11-27",
        scrip="NIFTY26NOV25100",
        lot_size=75,
        entry_timestamp=time.time() - 600,
        entry_credit=CREDIT,
        margin_blocked=MARGIN,
        status="CLOSED" if closed else "OPEN",
        exit_timestamp=time.time() if closed else None,
        exit_premium=8_500.0 if closed else None,
        pnl=PNL if closed else None,
        exit_reason="TARGET" if closed else None,
        signal_source="RANGE_BOUND_SETUP",
    )


@pytest.fixture
def paper(fake_redis, monkeypatch, tmp_path):
    """Real persistence + real SQLite ledger, isolated in tmp_path."""
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "paper_trades.db"))
    ledger.init_db()
    monkeypatch.setattr(pt_main, "TELEGRAM_NOTIFICATIONS", _TelegramStub())
    return fake_redis


def test_open_then_close_books_the_pnl_once(paper) -> None:
    position = _position()
    pt_main.persist_new_position(paper, position)
    account = pt_main.get_account(paper)
    assert account.open_positions == 1
    assert account.margin_used == MARGIN

    assert pt_main.persist_closed_position(paper, _position(closed=True)) is True

    account = pt_main.get_account(paper)
    assert account.open_positions == 0
    assert account.margin_used == 0
    assert account.realized_pnl == pytest.approx(PNL)
    assert account.daily_trades == 1
    assert account.daily_wins == 1
    assert paper.hget(POSITIONS_OPEN_KEY, "pos-1") is None
    assert len(paper.hgetall(positions_closed_key(account.trading_day))) == 1
    assert paper.xlen(TRADES_STREAM) == 1


def test_double_close_is_a_noop(paper) -> None:
    pt_main.persist_new_position(paper, _position())
    assert pt_main.persist_closed_position(paper, _position(closed=True)) is True
    assert pt_main.persist_closed_position(paper, _position(closed=True)) is False

    account = pt_main.get_account(paper)
    assert account.realized_pnl == pytest.approx(PNL), "P&L booked twice"
    assert paper.xlen(TRADES_STREAM) == 1


def test_concurrent_close_claims_the_position_once(paper) -> None:
    pt_main.persist_new_position(paper, _position())

    results: list[bool] = []
    barrier = threading.Barrier(2)

    def close() -> None:
        barrier.wait()
        results.append(pt_main.persist_closed_position(paper, _position(closed=True)))

    threads = [threading.Thread(target=close) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(results) == [False, True], f"exactly one close must win, got {results}"
    assert pt_main.get_account(paper).realized_pnl == pytest.approx(PNL)


def test_mtm_does_not_resurrect_a_closed_position(paper) -> None:
    pt_main.persist_new_position(paper, _position())
    pt_main.persist_closed_position(paper, _position(closed=True))

    stale_mtm_result = _position()          # OPEN snapshot from before the close
    assert pt_main.apply_mtm_result(paper, stale_mtm_result) is False
    assert paper.hget(POSITIONS_OPEN_KEY, "pos-1") is None


def test_closed_position_sets_a_cooldown(paper) -> None:
    pt_main.persist_new_position(paper, _position())
    pt_main.persist_closed_position(paper, _position(closed=True))

    key = cooldown_key("NIFTY", "CREDIT_SPREAD")
    assert paper.get(key) == "1"
    assert 0 < paper._client.ttl(key)


def test_daily_reset_keeps_cumulative_pnl(paper) -> None:
    account = PaperAccount(
        capital=1_000_000.0,
        realized_pnl=45_000.0,
        daily_realized_pnl=12_000.0,
        daily_trades=7,
        daily_wins=5,
        daily_losses=2,
        trading_day="2026-11-20",
    )
    pt_main.save_account(paper, account)

    reset = pt_main.get_account(paper, today="2026-11-21")

    assert reset.trading_day == "2026-11-21"
    assert reset.daily_realized_pnl == 0
    assert reset.daily_trades == 0
    assert reset.daily_wins == 0
    assert reset.daily_losses == 0
    assert reset.realized_pnl == pytest.approx(45_000.0), "cumulative P&L must survive"
    assert reset.day_start_capital == pytest.approx(1_045_000.0)

    pt_main.save_account(paper, reset)
    assert paper.hget(ACCOUNT_KEY, "trading_day") == "2026-11-21"


def test_same_day_does_not_reset(paper) -> None:
    account = PaperAccount(daily_trades=3, trading_day="2026-11-21")
    pt_main.save_account(paper, account)
    assert pt_main.get_account(paper, today="2026-11-21").daily_trades == 3


def test_daily_pnl_hash_matches_the_account(paper) -> None:
    pt_main.persist_new_position(paper, _position())
    pt_main.persist_closed_position(paper, _position(closed=True))

    account = pt_main.get_account(paper)
    pnl_hash = paper.hgetall(daily_pnl_key(account.trading_day))
    assert float(pnl_hash["realized"]) == pytest.approx(account.daily_realized_pnl)
    assert int(pnl_hash["trades_count"]) == account.daily_trades
