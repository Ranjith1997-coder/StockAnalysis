"""Property tests: exit rules (T3).

The invariant that matters operationally: for a defined-risk structure the stop
loss must be *reachable* (never beyond the structure's own max loss), and a flat
position must never be closed. Unit tests only cover the configured examples.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from services.paper_trading import engine
from services.paper_trading.models import OptionLeg, PaperPosition

pytestmark = pytest.mark.property

CREDIT = st.floats(min_value=0.5, max_value=500_000.0, allow_nan=False, allow_infinity=False)
WIDTH = st.floats(min_value=50.0, max_value=20_000.0, allow_nan=False)
QTY_LOTS = st.integers(min_value=1, max_value=20)
LOT_SIZE = st.sampled_from([15, 25, 40, 75])


def _leg(strike, option_type, side, lots, premium) -> OptionLeg:
    return OptionLeg(
        strike=strike, option_type=option_type, side=side, lots=lots,
        entry_premium=premium,
    )


def _condor(width, qty_lots, lot_size, credit) -> PaperPosition:
    mid = 25_000.0
    legs = [
        _leg(mid + width, "CE", "SELL", qty_lots, 100.0),
        _leg(mid + 2 * width, "CE", "BUY", qty_lots, 40.0),
        _leg(mid - width, "PE", "SELL", qty_lots, 100.0),
        _leg(mid - 2 * width, "PE", "BUY", qty_lots, 40.0),
    ]
    return PaperPosition(
        position_id="p", symbol="NIFTY", strategy="IRON_CONDOR", mode="intraday",
        direction="NEUTRAL", legs=legs, expiry="2026-11-27", scrip="NIFTY",
        lot_size=lot_size, entry_timestamp=0.0, entry_credit=credit,
        margin_blocked=0.0,
    )


@pytest.mark.parametrize("strategy", ["STRANGLE", "NAKED_CE", "NAKED_PE"])
def test_undefined_risk_has_no_max_loss(strategy) -> None:
    position = PaperPosition(
        position_id="p", symbol="NIFTY", strategy=strategy, mode="intraday",
        direction="NEUTRAL",
        legs=[_leg(25_000.0, "CE", "SELL", 2, 100.0), _leg(24_000.0, "PE", "SELL", 2, 100.0)],
        expiry="2026-11-27", scrip="NIFTY", lot_size=75,
        entry_timestamp=0.0, entry_credit=15_000.0, margin_blocked=0.0,
    )
    assert engine.compute_max_loss(position) is None


@settings(max_examples=150, deadline=None)
@given(width=WIDTH, lots=QTY_LOTS, lot_size=LOT_SIZE,
       credit_frac=st.floats(min_value=0.05, max_value=0.95, allow_nan=False))
def test_defined_risk_max_loss_formula(width, lots, lot_size, credit_frac) -> None:
    """Credit is always a fraction of the wing width, so max loss stays positive."""
    credit = credit_frac * width * lots * lot_size
    position = _condor(width, lots, lot_size, credit)
    expected = width * (lots * lot_size) - credit
    assert engine.compute_max_loss(position) == pytest.approx(expected)


@settings(max_examples=200, deadline=None)
@given(credit=CREDIT, max_loss=st.one_of(st.none(), CREDIT))
def test_stop_loss_never_exceeds_max_loss(credit, max_loss) -> None:
    sl = engine.stop_loss_amount(credit, max_loss)
    assert sl > 0
    assert sl <= engine.STOP_LOSS_MULTIPLE * credit
    if max_loss is not None and max_loss > 0:
        assert sl <= engine.DEFINED_RISK_SL_MAX_LOSS_PCT * max_loss


@settings(max_examples=200, deadline=None)
@given(credit=CREDIT, pnl=st.floats(-1e6, 1e6, allow_nan=False))
def test_flat_or_profitable_position_never_hits_stop_loss(credit, pnl) -> None:
    if pnl >= 0:
        assert not engine.check_stop_loss(pnl, credit, None)


@settings(max_examples=100, deadline=None)
@given(width=WIDTH, lots=QTY_LOTS, lot_size=LOT_SIZE,
       credit_frac=st.floats(min_value=0.05, max_value=0.95, allow_nan=False))
def test_stop_loss_is_reachable_for_a_condor(width, lots, lot_size, credit_frac) -> None:
    credit = credit_frac * width * lots * lot_size
    position = _condor(width, lots, lot_size, credit)
    max_loss = engine.compute_max_loss(position)
    assert max_loss is not None and max_loss > 0
    sl = engine.stop_loss_amount(position.entry_credit, max_loss)
    assert sl <= max_loss, "SL beyond max loss can never fire before expiry settlement"


def _reason(pnl, credit=10_000.0):
    position = _condor(200.0, 1, 75, credit)
    return engine.determine_exit_reason(
        position, pnl, credit - pnl, datetime(2026, 11, 24, 10, 0)
    )


@settings(max_examples=200, deadline=None)
@given(pnl=st.floats(-1e6, 1e6, allow_nan=False))
def test_exit_reason_is_deterministic(pnl) -> None:
    assert _reason(pnl) == _reason(pnl)


@settings(max_examples=100, deadline=None)
@given(fraction=st.floats(0.0, 0.2, allow_nan=False))
def test_small_positive_pnl_inside_the_bands_does_not_exit(fraction) -> None:
    """A position earning 0-20% of credit at 10:00, DTE=3 must stay open."""
    credit = 10_000.0
    assert _reason(fraction * credit, credit) is None


@given(multiple=st.floats(1.01, 100.0, allow_nan=False))
def test_stop_loss_dominates_any_other_exit(multiple) -> None:
    """Any loss past the stop-loss amount exits as STOP_LOSS, not TARGET/THETA."""
    credit = 10_000.0
    position = _condor(200.0, 1, 75, credit)
    amount = engine.stop_loss_amount(credit, engine.compute_max_loss(position))
    pnl = -amount * multiple
    assert _reason(pnl, credit) == "STOP_LOSS"
