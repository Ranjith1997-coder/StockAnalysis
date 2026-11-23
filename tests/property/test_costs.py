"""Property tests: the cost model (T3).

Unit tests check a few hand-picked numbers; these check the invariants that must
hold for *every* position — above all that charges never go negative and never
reward a bigger trade.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from services.paper_trading.models import (
    BSE_EXCHANGE_CHARGES_PCT,
    DEFAULT_BROKERAGE_PER_ORDER,
    DEFAULT_EXCHANGE_CHARGES_PCT,
    DEFAULT_STAMP_DUTY_PCT,
    DEFAULT_STT_PCT,
    GST_PCT,
    SEBI_FEE_PER_CRORE,
    compute_brokerage,
    compute_transaction_costs,
    exchange_charges_pct_for,
)

pytestmark = pytest.mark.property

PREMIUM = st.floats(min_value=0.05, max_value=500.0, allow_nan=False, allow_infinity=False)
QTY = st.integers(min_value=1, max_value=50_000)
ACTION = st.sampled_from(["BUY", "SELL"])
TRADES = st.lists(st.tuples(PREMIUM, ACTION), min_size=1, max_size=4)


def _expected(trades, qty, exchange_pct=DEFAULT_EXCHANGE_CHARGES_PCT,
              per_order=DEFAULT_BROKERAGE_PER_ORDER) -> float:
    brokerage = compute_brokerage(len(trades), per_order)
    exchange = sum(exchange_pct * p * qty for p, _ in trades)
    sebi = sum(p * qty * SEBI_FEE_PER_CRORE / 1e7 for p, _ in trades)
    stt = sum(DEFAULT_STT_PCT * p * qty for p, a in trades if a == "SELL")
    stamp = sum(DEFAULT_STAMP_DUTY_PCT * p * qty for p, a in trades if a == "BUY")
    gst = GST_PCT * (brokerage + exchange + sebi)
    return brokerage + exchange + sebi + stt + stamp + gst


@settings(max_examples=200, deadline=None)
@given(trades=TRADES, qty=QTY)
def test_costs_are_non_negative(trades, qty) -> None:
    assert compute_transaction_costs(trades, qty) >= 0


@settings(max_examples=200, deadline=None)
@given(trades=TRADES, qty=QTY)
def test_costs_match_the_documented_formula(trades, qty) -> None:
    assert compute_transaction_costs(trades, qty) == pytest.approx(
        _expected(trades, qty), rel=1e-9, abs=1e-6
    )


@settings(max_examples=100, deadline=None)
@given(trades=TRADES, qty=QTY)
def test_costs_are_monotonic_in_quantity(trades, qty) -> None:
    assert compute_transaction_costs(trades, qty + 1) > compute_transaction_costs(trades, qty)


@settings(max_examples=100, deadline=None)
@given(premium=PREMIUM, qty=QTY)
def test_stt_is_charged_on_sells_not_buys(premium, qty) -> None:
    sold = compute_transaction_costs([(premium, "SELL")], qty)
    bought = compute_transaction_costs([(premium, "BUY")], qty)
    # STT (0.1%) is larger than stamp duty (0.003%), so a sell must cost more.
    assert sold > bought


@settings(max_examples=100, deadline=None)
@given(trades=TRADES, qty=QTY)
def test_gst_is_exactly_eighteen_percent_of_taxable_base(trades, qty) -> None:
    costs = compute_transaction_costs(trades, qty)
    brokerage = compute_brokerage(len(trades))
    exchange = sum(DEFAULT_EXCHANGE_CHARGES_PCT * p * qty for p, _ in trades)
    sebi = sum(p * qty * SEBI_FEE_PER_CRORE / 1e7 for p, _ in trades)
    gst = costs - _expected_without_gst(trades, qty)
    assert gst == pytest.approx(GST_PCT * (brokerage + exchange + sebi), rel=1e-9)


def _expected_without_gst(trades, qty) -> float:
    brokerage = compute_brokerage(len(trades))
    exchange = sum(DEFAULT_EXCHANGE_CHARGES_PCT * p * qty for p, _ in trades)
    sebi = sum(p * qty * SEBI_FEE_PER_CRORE / 1e7 for p, _ in trades)
    stt = sum(DEFAULT_STT_PCT * p * qty for p, a in trades if a == "SELL")
    stamp = sum(DEFAULT_STAMP_DUTY_PCT * p * qty for p, a in trades if a == "BUY")
    return brokerage + exchange + sebi + stt + stamp


@given(st.sampled_from(["SENSEX", "BANKEX", "NIFTY", "BANKNIFTY", "MIDCPNIFTY"]))
def test_exchange_rate_follows_the_symbol_venue(symbol) -> None:
    expected = BSE_EXCHANGE_CHARGES_PCT if symbol in {"SENSEX", "BANKEX"} else DEFAULT_EXCHANGE_CHARGES_PCT
    assert exchange_charges_pct_for(symbol) == expected
