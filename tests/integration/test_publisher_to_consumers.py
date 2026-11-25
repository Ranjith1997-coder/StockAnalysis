"""Integration: the data market-data publishes is what paper-trading reads (T4.1).

Both sides were unit-tested with hand-built dicts, so a producer/consumer
disagreement (field name, staleness semantics) only appeared live.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from common.token_registry import TokenRegistry, TokenType, TokenInfo
from lib.zerodha.tick_store import TickStore
from services.market_data.snapshot_publisher import SnapshotPublisher
from services.volatility_engine.comparator import EvalResult, StrikeData
from services.paper_trading.main import check_entry_gate
from services.paper_trading.signal_router import EntrySignal, get_pinn_confirmation

pytestmark = pytest.mark.integration


class _Obj:
    def __init__(self, symbol: str) -> None:
        self.stock_symbol = symbol
        self._tick_store = TickStore()


@pytest.fixture(autouse=True)
def _open_market(monkeypatch):
    """Gate tests are about data wiring, not the clock or the calendar.

    Without this, ``is_trading_day`` calls the NSE holiday API (network) and the
    entry window would depend on when the suite runs.
    """
    from datetime import time as dtime

    from services.paper_trading import main as pt_main

    monkeypatch.setattr(pt_main, "is_trading_day", lambda _d: True)
    monkeypatch.setattr(pt_main, "ENTRY_WINDOW_START", dtime(0, 0))
    monkeypatch.setattr(pt_main, "ENTRY_WINDOW_END", dtime(23, 59))


def _publish_sensibull(redis, symbol: str = "NIFTY", fetched_at: datetime | None = None) -> None:
    """The gate also requires a fresh Sensibull fetch (chain context)."""
    redis.hset(f"data:sensibull:{symbol}", mapping={
        "last_fetch_time": (fetched_at or datetime.now()).isoformat(),
    })


def _publish_chain(redis, symbol: str = "NIFTY", spot: float = 25_000.0) -> _Obj:
    obj = _Obj(symbol)
    ts = obj._tick_store
    ts.update_zerodha_data({"last_price": spot, "ohlc": {"open": spot, "high": spot, "low": spot, "close": spot}})
    for strike in (spot - 100, spot, spot + 100):
        for side in ("CE", "PE"):
            ts.update_option_tick(float(strike), side, {
                "last_price": 100.0, "oi": 1_000_000, "volume_traded": 500,
                "total_buy_quantity": 5, "total_sell_quantity": 6,
            })
    ts.set_options_expiry("2026-11-25")
    ts.recompute_options_aggregate(spot)
    SnapshotPublisher(redis, stock_objs=[], index_objs=[obj])._publish_all()
    return obj


def test_fresh_published_aggregate_passes_the_entry_gate(fake_redis) -> None:
    _publish_chain(fake_redis)
    _publish_sensibull(fake_redis)
    assert check_entry_gate(fake_redis, "NIFTY", now=datetime.now()) == ""


def test_stale_aggregate_is_rejected(fake_redis) -> None:
    _publish_chain(fake_redis)
    _publish_sensibull(fake_redis)
    # Same data, evaluated 11 s later: past ENTRY_MAX_AGG_AGE_SECONDS (10 s).
    later = datetime.now() + timedelta(seconds=11)
    assert check_entry_gate(fake_redis, "NIFTY", now=later) == "stale_options_agg"


def test_missing_aggregate_is_rejected(fake_redis) -> None:
    assert check_entry_gate(fake_redis, "NIFTY", now=datetime.now()) == "stale_options_agg"


@pytest.mark.parametrize("hour,minute", [(9, 10), (15, 5), (20, 0)])
def test_entry_gate_respects_the_time_window(fake_redis, hour, minute) -> None:
    from datetime import time as dtime

    from services.paper_trading import main as pt_main

    _publish_chain(fake_redis)
    _publish_sensibull(fake_redis)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pt_main, "ENTRY_WINDOW_START", dtime(9, 20))
        patch.setattr(pt_main, "ENTRY_WINDOW_END", dtime(15, 0))
        now = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        assert check_entry_gate(fake_redis, "NIFTY", now=now) == "outside_entry_window"


def test_published_aggregate_is_readable_by_the_engine(fake_redis) -> None:
    """The engine's staleness check must agree with what the publisher wrote."""
    from services.paper_trading import engine

    _publish_chain(fake_redis)
    agg = fake_redis.hgetall("data:options_agg:NIFTY")
    assert agg["expiry"] == "2026-11-25"
    assert not engine.is_agg_data_stale(agg, now=time.time())
    assert engine.is_agg_data_stale(agg, now=time.time() + 60)


def test_pinn_zscore_cache_round_trip(fake_redis) -> None:
    """The vol-engine's z-score hash is what the router reads for confirmation."""
    from services.volatility_engine.signal_emitter import write_fair_iv_to_redis

    registry = TokenRegistry()
    registry.register(TokenInfo(
        token=101, token_type=TokenType.OPTION, parent_symbol="NIFTY",
        tradingsymbol="NIFTY25000PE", strike=24_900.0, option_type="PE",
        expiry="2026-11-25",
    ))
    registry.register(TokenInfo(
        token=102, token_type=TokenType.OPTION, parent_symbol="NIFTY",
        tradingsymbol="NIFTY25100CE", strike=25_100.0, option_type="CE",
        expiry="2026-11-25",
    ))
    registry.set_strike_gap("NIFTY", 50.0)

    def _result(strike: float, side: str, z: float) -> EvalResult:
        return EvalResult(
            strike_data=StrikeData(
                strike=strike, option_type=side, ltp=100.0, k=0.0,
                live_sigma=0.15, live_w=0.15,
            ),
            fair_w=0.0225, uncertainty=0.02, z=z,
        )

    write_fair_iv_to_redis(
        fake_redis, "NIFTY",
        [_result(24_900.0, "PE", 1.8), _result(25_100.0, "CE", 2.4)],
        tau=0.02,
    )

    signal = EntrySignal(
        strategy="IRON_CONDOR", symbol="NIFTY",
        put_wall_strike=24_900.0, call_wall_strike=25_100.0,
    )
    assert get_pinn_confirmation(fake_redis, signal) == pytest.approx(2.4)

    only_put = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY", put_wall_strike=24_900.0)
    assert get_pinn_confirmation(fake_redis, only_put) == pytest.approx(1.8)


def test_missing_pinn_cache_fails_open(fake_redis) -> None:
    signal = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY", put_wall_strike=24_900.0)
    assert get_pinn_confirmation(fake_redis, signal) is None


class TestSensibullFreshnessGate:
    def test_stale_sensibull_blocks_an_otherwise_valid_entry(self, fake_redis):
        _publish_chain(fake_redis)
        _publish_sensibull(fake_redis, fetched_at=datetime.now() - timedelta(hours=5))

        assert check_entry_gate(fake_redis, "NIFTY", now=datetime.now()) == "stale_sensibull"

    def test_missing_sensibull_blocks(self, fake_redis):
        _publish_chain(fake_redis)
        assert check_entry_gate(fake_redis, "NIFTY", now=datetime.now()) == "stale_sensibull"

    def test_fresh_sensibull_allows(self, fake_redis):
        _publish_chain(fake_redis)
        _publish_sensibull(fake_redis)
        assert check_entry_gate(fake_redis, "NIFTY", now=datetime.now()) == ""

    def test_guard_can_be_disabled(self, fake_redis, monkeypatch):
        monkeypatch.setenv("PAPER_MAX_SENSIBULL_AGE_SECONDS", "0")
        _publish_chain(fake_redis)
        assert check_entry_gate(fake_redis, "NIFTY", now=datetime.now()) == ""

    def test_unparseable_timestamp_blocks(self, fake_redis):
        _publish_chain(fake_redis)
        fake_redis.hset("data:sensibull:NIFTY", mapping={"last_fetch_time": "not-a-date"})
        assert check_entry_gate(fake_redis, "NIFTY", now=datetime.now()) == "stale_sensibull"
