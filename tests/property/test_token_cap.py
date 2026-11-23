"""Property tests: the WS subscription cap (T3).

The 2026-11-23 incident: WS2 held 542 subscriptions against Kite's 500 limit
and the subscribe call failed, so the chain stopped following spot. These tests
walk spot around and assert the registry never exceeds the cap, never evicts a
CORE (ATM ±1 %) strike, and never reports an unsubscribe it did not perform.
"""
from __future__ import annotations

from datetime import date

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from common.token_registry import (
    WS_MAX_OPTION_SUBSCRIPTIONS,
    OptionZone,
    TokenInfo,
    TokenRegistry,
    TokenType,
)

pytestmark = pytest.mark.property

SPOT = 25_000.0
GAP = 10.0          # dense chain so the initial window alone exceeds the cap
PERCENT = 3.0       # ±3 % random walk


def _option_token(token: int, parent: str, strike: float, side: str) -> TokenInfo:
    return TokenInfo(
        token=token,
        token_type=TokenType.OPTION,
        parent_symbol=parent,
        tradingsymbol=f"{parent}{strike:.0f}{side}",
        strike=strike,
        option_type=side,
        expiry="2026-11-25",
    )


def _build_registry() -> TokenRegistry:
    registry = TokenRegistry()
    registry.set_strike_gap("NIFTY", GAP)
    token = 100_000
    for step in range(-130, 131):              # ±1300 points at gap 10
        strike = SPOT + step * GAP
        for side in ("CE", "PE"):
            registry.register(_option_token(token, "NIFTY", strike, side))
            token += 1

    registry.set_strike_gap("BANKNIFTY", 100.0)
    for step in range(-30, 31):                # ±3000 points
        strike = 63_000.0 + step * 100.0
        for side in ("CE", "PE"):
            registry.register(_option_token(token, "BANKNIFTY", strike, side))
            token += 1
    return registry


def _core_tokens(registry: TokenRegistry, symbol: str, spot: float) -> set[int]:
    zones = registry.calculate_zones(symbol, spot)
    strike_map = registry.get_option_tokens_for(symbol)
    return {
        token
        for strike, zone in zones.items()
        if zone is OptionZone.CORE
        for token in strike_map.get(strike, {}).values()
    }


def test_initial_subscription_respects_the_cap_and_keeps_core() -> None:
    registry = _build_registry()

    # Small symbol first, then the dense one, so the dense one is capped.
    bn_tokens, _ = registry.initial_subscribe_options("BANKNIFTY", 63_000.0)
    nifty_tokens, mode_map = registry.initial_subscribe_options("NIFTY", SPOT)
    subscribed = set(registry.get_all_subscribed_tokens())

    assert len(bn_tokens) > 0
    assert len(subscribed) <= WS_MAX_OPTION_SUBSCRIPTIONS
    assert len(nifty_tokens) > 0
    assert set(nifty_tokens).isdisjoint(bn_tokens)
    assert set(mode_map).issubset({"full", "quote", "ltp"})
    assert sum(len(v) for v in mode_map.values()) == len(nifty_tokens)
    assert _core_tokens(registry, "NIFTY", SPOT) <= subscribed


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(moves=st.lists(st.integers(min_value=-30, max_value=30), min_size=3, max_size=15))
def test_cap_holds_and_core_survives_a_random_walk(moves) -> None:
    registry = _build_registry()
    registry.initial_subscribe_options("BANKNIFTY", 63_000.0)
    registry.initial_subscribe_options("NIFTY", SPOT)

    spot = SPOT
    for move in moves:
        spot *= 1 + move / 1000          # move/1000 = 0.1 % per unit
        new_sub, unsub, mode_changes = registry.recenter_and_get_subscription_changes(
            "NIFTY", round(spot, 2)
        )
        subscribed = set(registry.get_all_subscribed_tokens())

        assert len(subscribed) <= WS_MAX_OPTION_SUBSCRIPTIONS, (
            f"cap breached at spot {spot:.2f}: {len(subscribed)}"
        )
        assert set(new_sub) <= subscribed
        assert set(unsub).isdisjoint(subscribed)
        assert set(new_sub).isdisjoint(unsub), "token both subscribed and unsubscribed"

        change_tokens = [t for tokens in mode_changes.values() for t in tokens]
        assert len(change_tokens) == len(set(change_tokens)), "duplicate mode change"
        assert set(change_tokens) <= subscribed

        assert _core_tokens(registry, "NIFTY", round(spot, 2)) <= subscribed, (
            f"CORE strike unsubscribed at spot {spot:.2f}"
        )


def test_cap_is_checked_across_symbols_not_per_symbol() -> None:
    """The broker limit is per connection, so eviction must look globally."""
    registry = _build_registry()
    registry.initial_subscribe_options("BANKNIFTY", 63_000.0)
    registry.initial_subscribe_options("NIFTY", SPOT)
    assert len(registry.get_all_subscribed_tokens()) <= WS_MAX_OPTION_SUBSCRIPTIONS

    registry.recenter_and_get_subscription_changes("NIFTY", SPOT + 500)
    assert len(registry.get_all_subscribed_tokens()) <= WS_MAX_OPTION_SUBSCRIPTIONS


def test_round_to_strike_rejects_invalid_prices() -> None:
    registry = _build_registry()
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            registry.round_to_strike(bad, "NIFTY")


def test_registry_tokens_carry_an_expiry() -> None:
    """Sanity: the fixtures model a real chain (guards against silent no-op tests)."""
    registry = _build_registry()
    assert date.fromisoformat(str(registry.lookup(100_000).expiry))
