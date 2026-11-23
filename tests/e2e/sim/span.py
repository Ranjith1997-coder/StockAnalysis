"""Deterministic SPAN stand-in for the E2E simulator (T5.4).

SPAN margin is an external Zerodha API; the simulator must not depend on the
network. The stub keeps the *shape* of the real calculator (lot size, scrip,
margin per lot) and uses a fixed margin so sizing is reproducible.
"""
from __future__ import annotations

from .config import Scenario

MARGIN_PER_LOT = 60_000.0


class FakeSpanCalculator:
    def __init__(self, scenario: Scenario, margin_per_lot: float = MARGIN_PER_LOT) -> None:
        self._scenario = scenario
        self._margin = margin_per_lot
        self.calls: list[list] = []

    def get_lot_size(self, symbol: str, expiry: str) -> int | None:
        if expiry != self._scenario.expiry[symbol].isoformat():
            return None
        return self._scenario.lot(symbol)

    def get_scrip(self, symbol: str, expiry: str) -> str | None:
        if expiry != self._scenario.expiry[symbol].isoformat():
            return None
        return f"{symbol}{expiry.replace('-', '')}"

    def calculate_margin(self, legs) -> float | None:
        self.calls.append(list(legs))
        return self._margin if legs else None
