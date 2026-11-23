"""Contract tests: the real snapshot publisher writes only declared fields
(T1.1).

Producer/consumer field drift has caused real incidents (``buy_qty`` read as a
price, ``max_pain`` vs ``max_pain_strike``, ``iv_percentile=None``). This test
runs ``SnapshotPublisher._publish_all()`` — not a mock — against fakeredis and
compares every field it writes with ``tests/contract/schema.py``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import common.constants as constant
from lib.zerodha.tick_store import TickStore
from services.market_data.snapshot_publisher import SnapshotPublisher
from tests.contract.schema import (
    FUTURES_DEPTH_FIELDS,
    FUTURES_FIELDS,
    OPTIONS_AGG,
    OPTIONS_LIVE,
    TICK,
    registry_problem,
)

pytestmark = pytest.mark.contract

SKILL = (
    Path(__file__).resolve().parents[2]
    / ".kilo/skills/options-data-contract/SKILL.md"
)


class _Obj:
    """Minimal Stock-like object: publisher only reads these two attributes."""

    def __init__(self, symbol: str) -> None:
        self.stock_symbol = symbol
        self._tick_store = TickStore()


def _fill_equity(obj: _Obj, price: float) -> None:
    obj._tick_store.update_zerodha_data({
        "last_price": price,
        "change": 1.2,
        "ohlc": {"open": price - 5, "high": price + 10, "low": price - 8, "close": price - 2},
        "volume_traded": 1_234_567,
        "average_traded_price": price,
        "total_buy_quantity": 100,
        "total_sell_quantity": 120,
    })


def _fill_index(obj: _Obj, spot: float, expiry: str) -> None:
    ts = obj._tick_store
    ts.update_zerodha_data({
        "last_price": spot,
        "ohlc": {"open": spot, "high": spot + 50, "low": spot - 50, "close": spot - 10},
    })
    for offset, oi in ((-200, 400_000), (-100, 900_000), (0, 1_500_000), (100, 1_100_000)):
        strike = float(spot + offset)
        for side, side_oi in (("CE", oi), ("PE", int(oi * 1.2))):
            ts.update_option_tick(strike, side, {
                "last_price": 120.0 + offset / 10,
                "oi": side_oi,
                "volume_traded": 5_000,
                "total_buy_quantity": 40,
                "total_sell_quantity": 45,
                "depth": {
                    "buy": [{"price": 119.5, "quantity": 75, "orders": 3}],
                    "sell": [{"price": 120.5, "quantity": 80, "orders": 2}],
                },
            })
    ts.set_options_expiry(expiry)
    ts.recompute_options_aggregate(spot)

    depth = {
        side: [
            {"price": spot + delta, "quantity": 100 * level, "orders": level}
            for level, delta in enumerate((-2, -1, 0, 1, 2), start=1)
        ]
        for side in ("buy", "sell")
    }
    ts.update_futures_tick("current", {
        "last_price": spot + 25,
        "oi": 6_000_000,
        "volume_traded": 900_000,
        "change": 0.35,
        "total_buy_quantity": 10,
        "total_sell_quantity": 12,
        "depth": depth,
    })


@pytest.fixture
def published(fake_redis):
    equity = _Obj("RELIANCE")
    _fill_equity(equity, price=2_950.0)
    indices = [_Obj(sym) for sym in constant.LIVE_OPTIONS_INDICES]
    for i, obj in enumerate(indices):
        _fill_index(obj, spot=25_000.0 + i * 1_000, expiry="2026-11-25")

    publisher = SnapshotPublisher(fake_redis, stock_objs=[equity], index_objs=indices)
    publisher._publish_all()
    return fake_redis, equity, indices


def _keys_for(redis, prefix: str) -> dict[str, dict]:
    return {
        key: redis.hgetall(key)
        for key in redis._client.scan_iter(f"{prefix}*")
    }


def test_writes_only_data_keys(published) -> None:
    redis, _, _ = published
    written = set(redis._client.scan_iter("data:*"))
    assert written, "publisher wrote nothing"
    for key in written:
        assert key.split(":", 2)[0] + ":" + key.split(":", 2)[1] in {
            "data:tick", "data:options_live", "data:options_agg", "data:futures_live"
        }, f"unexpected data key {key}"


def test_tick_fields_match_schema(published) -> None:
    redis, equity, _ = published
    mapping = redis.hgetall(f"data:tick:{equity.stock_symbol}")
    assert mapping, "no equity tick published"
    undeclared = set(mapping) - TICK.all_fields
    assert not undeclared, f"{TICK.key}: undeclared fields {sorted(undeclared)}"
    assert TICK.required <= set(mapping), f"missing {sorted(TICK.required - set(mapping))}"


@pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "SENSEX"])
def test_options_agg_fields_match_schema(published, symbol) -> None:
    redis, _, _ = published
    mapping = redis.hgetall(f"data:options_agg:{symbol}")
    assert mapping, f"no aggregate published for {symbol}"

    undeclared = set(mapping) - OPTIONS_AGG.all_fields
    assert not undeclared, (
        f"data:options_agg:{symbol}: undeclared fields {sorted(undeclared)}. "
        "Declare them in tests/contract/schema.py and the skill, or stop writing them."
    )
    missing = OPTIONS_AGG.required - set(mapping)
    assert not missing, f"data:options_agg:{symbol}: missing {sorted(missing)}"
    assert mapping["expiry"] == "2026-11-25"
    assert float(mapping["last_updated"]) > 0


@pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "SENSEX"])
def test_options_live_json_matches_schema(published, symbol) -> None:
    redis, _, _ = published
    mapping = redis.hgetall(f"data:options_live:{symbol}")
    assert mapping, f"no live chain published for {symbol}"

    for field, raw in mapping.items():
        assert re.fullmatch(OPTIONS_LIVE.field_regex, field), (
            f"data:options_live:{symbol}: bad field name {field!r}"
        )
        payload = OPTIONS_LIVE.parse_json(raw)
        problems = OPTIONS_LIVE.json_problem(payload)
        assert not problems, "\n".join(problems)

    # Best prices must be bid/ask; quantities must stay quantities.
    sample = OPTIONS_LIVE.parse_json(next(iter(mapping.values())))
    assert isinstance(sample["buy_qty"], int)
    assert "bid" in sample and "ask" in sample


@pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "SENSEX"])
def test_futures_fields_match_schema(published, symbol) -> None:
    redis, _, _ = published
    mapping = redis.hgetall(f"data:futures_live:{symbol}")
    assert mapping, f"no futures tick published for {symbol}"

    seen_fields = set()
    for field in mapping:
        prefix, _, name = field.partition("_")
        assert prefix in {"current", "next"}, f"bad futures field {field!r}"
        if name in FUTURES_FIELDS:
            seen_fields.add(name)
        else:
            assert name in FUTURES_DEPTH_FIELDS, f"undeclared futures field {field!r}"
    assert FUTURES_FIELDS <= seen_fields, f"missing {sorted(FUTURES_FIELDS - seen_fields)}"
    assert {f"current_buy_px_{i}" for i in range(1, 6)} <= set(mapping)
    assert {f"current_sell_ord_{i}" for i in range(1, 6)} <= set(mapping)


def test_data_keys_have_no_ttl(published) -> None:
    """These hashes are refreshed every second; a TTL would make them vanish."""
    redis, _, _ = published
    for key in redis._client.scan_iter("data:*"):
        assert redis._client.ttl(key) == -1, f"{key} has a TTL; contract says none"


def test_live_chain_is_replaced_atomically(published) -> None:
    """A shrinking chain must not leave stale strikes behind (empty-hash race)."""
    redis, _, indices = published
    nifty = indices[0]
    key = "data:options_live:NIFTY"
    before = set(redis.hgetall(key))
    assert before

    # Drop the farthest strike, as a new expiry/strike upload would.
    with nifty._tick_store._lock:
        nifty._tick_store.options_live.pop(max(nifty._tick_store.options_live), None)
    SnapshotPublisher(redis, stock_objs=[], index_objs=[nifty])._publish_all()

    after = set(redis.hgetall(key))
    assert after < before, "stale strikes survived the atomic replace"
    assert after, "atomic replace left the hash empty"


def test_skill_documents_every_declared_field() -> None:
    """schema.py is derived from the skill; they must not drift apart."""
    text = SKILL.read_text()
    missing = [
        (schema.key, name)
        for schema in (OPTIONS_AGG, OPTIONS_LIVE)
        for name in sorted(schema.all_fields | schema.all_json_fields)
        if not re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text)
    ]
    assert not missing, (
        "fields declared in tests/contract/schema.py but missing from the data "
        f"contract skill: {missing}"
    )


def test_registry_field_check_is_not_vacuous() -> None:
    """Guard: the registry helper must actually reject a bad mapping."""
    assert registry_problem({"name": "x"})
    assert not registry_problem({
        "name": "x", "pid": "1", "status": "healthy",
        "last_heartbeat": "1.0", "version": "v",
    })
