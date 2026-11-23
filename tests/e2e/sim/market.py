"""Deterministic market generator for the E2E simulator (T5.3).

Produces spot/futures paths, a moving IV smile and OI build-up. Option prices
come from the production Black-Scholes helper, so the simulator cannot drift
from the pricing the repo already uses.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from tools.pinn_volatility.model.bs_utils import bs_price

from .config import Scenario

TRADING_MINUTES = 375          # 09:15 → 15:30
TRADING_SECONDS = TRADING_MINUTES * 60
RISK_FREE = 0.065
DIV_YIELD = 0.0
BASE_OI = 2_000_000.0
SPREAD_PCT = 0.002             # ±0.1 % half-spread


@dataclass(frozen=True)
class MarketState:
    t: datetime
    elapsed: int                      # seconds since the session open
    spot: dict[str, float]
    fut: dict[str, float]
    vix: float
    atm_iv: dict[str, float]


class MarketGenerator:
    """One simulated second of market movement per ``step`` call."""

    def __init__(self, scenario: Scenario, rng: np.random.Generator) -> None:
        self._scenario = scenario
        self._rng = rng
        self._spot = {
            symbol: scenario.spot[symbol] * (1 + scenario.gap_pct / 100.0)
            for symbol in scenario.symbols
        }
        self._open_spot = dict(self._spot)
        self._elapsed = 0

    @property
    def open_spot(self) -> dict[str, float]:
        return dict(self._open_spot)

    def step(self, dt_seconds: int) -> MarketState:
        self._elapsed += dt_seconds
        fraction = min(self._elapsed / TRADING_SECONDS, 1.0)
        drift_per_second = (self._scenario.drift_pct_per_day / 100.0) / TRADING_SECONDS
        sigma_day = self._scenario.vol_annual / math.sqrt(252.0)
        sigma_step = sigma_day * math.sqrt(dt_seconds / TRADING_SECONDS)

        for symbol in self._scenario.symbols:
            z = float(self._rng.standard_normal())
            self._spot[symbol] *= math.exp(drift_per_second * dt_seconds
                                           - 0.5 * sigma_step ** 2 + sigma_step * z)

        vix = self._scenario.vix_start + (
            self._scenario.vix_end - self._scenario.vix_start
        ) * fraction
        atm_iv = {
            symbol: self._scenario.iv_atm + (vix - 14.0) / 100.0
            for symbol in self._scenario.symbols
        }
        return MarketState(
            t=self._scenario.session_start,   # the harness owns the wall clock
            elapsed=self._elapsed,
            spot=dict(self._spot),
            fut={
                symbol: self._spot[symbol] * (1 + 0.0015)
                for symbol in self._scenario.symbols
            },
            vix=vix,
            atm_iv=atm_iv,
        )

    # ── option surface ────────────────────────────────────────────────────

    def strike_iv(self, symbol: str, strike: float, spot: float, atm_iv: float) -> float:
        k = math.log(strike / spot)
        iv = (atm_iv
              + self._scenario.iv_skew * k
              + self._scenario.iv_smile * k * k)
        return max(iv, 0.01)

    def tau(self, state: MarketState, expiry) -> float:
        """Years to expiry, floored at one second so dated contracts price normally."""
        end = datetime.combine(expiry, datetime.min.time()).replace(hour=15, minute=30)
        seconds = max((end - state.t).total_seconds(), 1.0)
        return seconds / (365.0 * 24 * 3600)

    def option_tick(self, symbol: str, state: MarketState, strike: float, side: str) -> dict:
        spot = state.spot[symbol]
        atm_iv = state.atm_iv[symbol]
        iv = self.strike_iv(symbol, strike, spot, atm_iv)
        tau = self.tau(state, self._scenario.expiry[symbol])
        price = max(bs_price(spot, strike, tau, RISK_FREE, DIV_YIELD, iv, side), 0.05)
        oi = self.open_interest(symbol, state, strike, side)
        return {
            "last_price": round(price, 2),
            "bid": round(price * (1 - SPREAD_PCT), 2),
            "ask": round(price * (1 + SPREAD_PCT), 2),
            "oi": int(oi),
            "volume_traded": int(1_000 + oi * 0.01),
            "total_buy_quantity": int(oi * 0.02),
            "total_sell_quantity": int(oi * 0.021),
            "iv": round(iv * 100, 2),
            "depth": {
                "buy": [{"price": round(price * (1 - SPREAD_PCT), 2),
                         "quantity": 750, "orders": 3}],
                "sell": [{"price": round(price * (1 + SPREAD_PCT), 2),
                          "quantity": 750, "orders": 3}],
            },
        }

    def open_interest(self, symbol: str, state: MarketState, strike: float, side: str) -> float:
        """ATM-anchored base OI plus deterministic writing over the session."""
        spot = state.spot[symbol]
        k = abs(math.log(strike / spot))
        base = BASE_OI * math.exp(-k / 0.02)
        minutes = state.elapsed / 60.0
        rate = (
            self._scenario.oi_ce_writing_pct_per_min if side == "CE"
            else self._scenario.oi_pe_writing_pct_per_min
        )
        return base * (1 + rate / 100.0 * minutes)

    def futures_tick(self, symbol: str, state: MarketState, previous: float | None) -> dict:
        price = state.fut[symbol]
        change = 0.0 if not previous else (price - previous) / previous * 100
        depth = {
            side: [
                {"price": round(price + sign * level, 2),
                 "quantity": 750 * level, "orders": level}
                for level, sign in enumerate((-2, -1, 0, 1, 2), start=1)
            ]
            for side, sign in (("buy", -1), ("sell", 1))
        }
        return {
            "last_price": round(price, 2),
            "oi": int(BASE_OI * 1.5 * (1 + 0.0005 * state.elapsed / 60.0)),
            "volume_traded": int(50_000 + state.elapsed),
            "change": round(change, 3),
            "total_buy_quantity": 1_000,
            "total_sell_quantity": 1_100,
            "depth": depth,
        }
