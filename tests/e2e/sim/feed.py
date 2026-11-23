"""Tick feed + subscription bookkeeping for the E2E simulator (T5.4).

Writes into the real ``TickStore``/``Stock`` objects and drives the real
``TokenRegistry``, enforcing Kite's subscription cap exactly like production:
only subscribed strikes receive ticks, so a cap breach starves the aggregate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from common.Stock import Stock
from common.token_registry import (
    WS_MAX_OPTION_SUBSCRIPTIONS,
    TokenInfo,
    TokenRegistry,
    TokenType,
)

from .config import Scenario
from .market import MarketGenerator, MarketState

STRIKE_WINDOW_PCT = 0.06          # register ±6 % around spot


@dataclass
class SubscriptionTrace:
    samples: list[tuple[int, int]] = field(default_factory=list)    # (elapsed, count)
    atm_misses: list[int] = field(default_factory=list)             # elapsed seconds
    max_seen: int = 0

    def record(self, elapsed: int, count: int) -> None:
        self.samples.append((elapsed, count))
        self.max_seen = max(self.max_seen, count)


class FakeFeed:
    """One ``Stock`` per index symbol plus a shared registry."""

    def __init__(self, scenario: Scenario, generator: MarketGenerator) -> None:
        self.scenario = scenario
        self.generator = generator
        self.registry = TokenRegistry()
        self.stocks: dict[str, Stock] = {}
        self._token_index: dict[str, dict[tuple[float, str], int]] = {}
        self._subscribed: dict[str, set[int]] = {}
        self._last_fut: dict[str, float | None] = {}
        self._next_token = 500_000
        self.trace = SubscriptionTrace()

        for symbol in scenario.symbols:
            self._register_chain(symbol)
            self.registry.initial_subscribe_options(symbol, scenario.spot[symbol])
            self._subscribed[symbol] = set(self.registry.get_all_subscribed_tokens())
            self.stocks[symbol] = Stock(symbol, symbol, is_index=True)
            self.stocks[symbol]._tick_store.set_options_expiry(
                scenario.expiry[symbol].isoformat()
            )
            self._last_fut[symbol] = None

    # ── setup ─────────────────────────────────────────────────────────────

    def _register_chain(self, symbol: str) -> None:
        scenario = self.scenario
        spot = scenario.spot[symbol]
        gap = scenario.strike_gap[symbol]
        self.registry.set_strike_gap(symbol, gap)
        index: dict[tuple[float, str], int] = {}
        steps = int((spot * STRIKE_WINDOW_PCT) // gap)
        for step in range(-steps, steps + 1):
            strike = round(spot + step * gap, 2)
            for side in ("CE", "PE"):
                token = self._next_token
                self._next_token += 1
                self.registry.register(TokenInfo(
                    token=token,
                    token_type=TokenType.OPTION,
                    parent_symbol=symbol,
                    tradingsymbol=f"{symbol}{strike:.0f}{side}",
                    strike=strike,
                    option_type=side,
                    expiry=scenario.expiry[symbol],
                ))
                index[(strike, side)] = token
        self._token_index[symbol] = index

    # ── per-step ──────────────────────────────────────────────────────────

    def push(self, state: MarketState) -> None:
        for symbol in self.scenario.symbols:
            stock = self.stocks[symbol]
            ts = stock._tick_store
            spot = state.spot[symbol]

            ts.update_zerodha_data({
                "last_price": spot,
                "change": (spot - self.generator.open_spot[symbol])
                / self.generator.open_spot[symbol] * 100,
                "ohlc": {"open": self.generator.open_spot[symbol],
                         "high": max(spot, self.generator.open_spot[symbol]),
                         "low": min(spot, self.generator.open_spot[symbol]),
                         "close": spot},
                "volume_traded": 1_000_000 + state.elapsed * 100,
                "average_traded_price": spot,
                "total_buy_quantity": 5_000,
                "total_sell_quantity": 5_100,
            })

            for (strike, side), token in self._token_index[symbol].items():
                if token not in self._subscribed[symbol]:
                    continue
                tick = self.generator.option_tick(symbol, state, strike, side)
                ts.update_option_tick(strike, side, tick)
                # Production enriches the Zerodha tick with Sensibull greeks
                # (merge=True): paper trading refuses to sell a leg without IV.
                ts.update_option_tick(
                    strike, side,
                    {"iv": tick["iv"], "delta": 0.5, "gamma": 0.0002,
                     "theta": -1.2, "vega": 8.0, "iv_change": 0.1},
                    merge=True,
                )

            ts.set_options_expiry(self.scenario.expiry[symbol].isoformat())
            ts.recompute_options_aggregate(spot)

            fut_tick = self.generator.futures_tick(symbol, state, self._last_fut[symbol])
            ts.update_futures_tick("current", fut_tick)
            self._last_fut[symbol] = state.fut[symbol]

            self.registry.recenter_and_get_subscription_changes(symbol, spot)
            self._subscribed[symbol] = set(self.registry.get_all_subscribed_tokens())
            count = len(self._subscribed[symbol])
            self.trace.record(state.elapsed, count)

            atm = self.registry.round_to_strike(spot, symbol)
            atm_tokens = {
                self._token_index[symbol].get((atm, "CE")),
                self._token_index[symbol].get((atm, "PE")),
            } - {None}
            if not atm_tokens.issubset(self._subscribed[symbol]):
                self.trace.atm_misses.append(state.elapsed)

    # ── introspection ─────────────────────────────────────────────────────

    @property
    def objects(self) -> list[Stock]:
        return [self.stocks[s] for s in self.scenario.symbols]

    def subscribed_count(self, symbol: str) -> int:
        return len(self._subscribed[symbol])

    def atm_strike(self, symbol: str, spot: float) -> float:
        return self.registry.round_to_strike(spot, symbol)

    def strikes(self, symbol: str) -> list[float]:
        return sorted({strike for strike, _side in self._token_index[symbol]})

    @property
    def cap(self) -> int:
        return WS_MAX_OPTION_SUBSCRIPTIONS
