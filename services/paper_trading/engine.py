"""
Paper Trading — MTM & Exit Engine

Marks every open position to market from live tick data and checks exit
rules in priority order. See docs/PAPER_TRADING_DESIGN.md section 8 for the
full spec. Mirrors strategy_builder.py's split: this module computes what
*should* happen to a position (closed or not, and why) but leaves Redis
persistence (moving keys, updating the account, notifications) to main.py.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, time as dtime
from typing import Optional

from lib.logging_util import get_logger
logger = get_logger("paper-trading")
from services.paper_trading.models import (
    DEFAULT_EXCHANGE_CHARGES_PCT,
    DEFAULT_STAMP_DUTY_PCT,
    DEFAULT_STT_PCT,
    PaperPosition,
    compute_brokerage,
    compute_exchange_charges,
    compute_stamp_duty,
    compute_stt,
)

STALE_AGG_DATA_MAX_AGE_SECONDS = 10.0
GAMMA_TRAP_PROXY_MOVE_PCT = 2.0

STOP_LOSS_MULTIPLE = 2.0        # 200% of entry credit
PROFIT_TARGET_PCT = 0.5         # 50% of entry credit
THETA_DECAY_PCT = 0.25          # exit once only 25% of entry credit remains (75% decayed)
THETA_DECAY_INTRADAY_TIME = dtime(14, 0)
INTRADAY_SQUAREOFF_TIME = dtime(15, 15)
POSITIONAL_SQUAREOFF_TIME = dtime(15, 0)
POSITIONAL_SQUAREOFF_DTE = 1
POSITIONAL_THETA_DECAY_DTE = 3


# ── Stale-data guards (docs/PAPER_TRADING_DESIGN.md section 8.1 steps 1-2) ──

def is_agg_data_stale(options_agg: dict, now: Optional[float] = None,
                       max_age_seconds: float = STALE_AGG_DATA_MAX_AGE_SECONDS) -> bool:
    """Missing key (race condition) or too old both count as stale -- skip MTM."""
    if not options_agg:
        return True
    last_updated = float(options_agg.get("last_updated", 0) or 0)
    now = now if now is not None else time.time()
    return (now - last_updated) > max_age_seconds


# ── Leg premium updates (docs/PAPER_TRADING_DESIGN.md section 8.1 step 3) ───

def update_leg_premiums(position: PaperPosition, options_live: dict) -> bool:
    """Update each leg's current_premium from the options_live hash.

    Legs with a missing/malformed tick keep their last known premium (a
    stale-but-present fill is safer than crashing or force-skipping the
    whole position for one illiquid leg) -- returns False if any leg
    couldn't be updated, purely for logging/observability upstream.
    """
    all_found = True
    for leg in position.legs:
        raw = options_live.get(f"{leg.strike}_{leg.option_type}")
        if raw is None:
            logger.warning(
                f"[engine] {position.symbol} {position.position_id}: no tick for "
                f"{leg.strike}{leg.option_type}, using last known premium"
            )
            all_found = False
            continue
        try:
            tick = json.loads(raw)
            ltp = float(tick.get("ltp", 0) or 0)
            if ltp > 0:
                leg.current_premium = ltp
        except (json.JSONDecodeError, TypeError):
            all_found = False
    return all_found


# ── MTM computation (docs/PAPER_TRADING_DESIGN.md section 8.1 step 4) ──────

def compute_qty(position: PaperPosition) -> int:
    lots = position.legs[0].lots if position.legs else 0
    return position.lot_size * lots


def compute_current_debit(position: PaperPosition) -> float:
    """What it costs to close the position right now (total rupees)."""
    qty = compute_qty(position)
    sell_value = sum(leg.current_premium for leg in position.legs if leg.side == "SELL") * qty
    buy_value = sum(leg.current_premium for leg in position.legs if leg.side == "BUY") * qty
    return sell_value - buy_value


def compute_unrealized_pnl(position: PaperPosition, current_debit: float) -> float:
    return position.entry_credit - current_debit


# ── GAMMA_TRAP proxy (docs/PAPER_TRADING_DESIGN.md section 8.3) ────────────

def check_gamma_trap_proxy(tick: dict, threshold_pct: float = GAMMA_TRAP_PROXY_MOVE_PCT) -> bool:
    """Proxy for the 5-min-lagged real GAMMA_TRAP signal: force-close if spot
    has moved more than threshold_pct since the last daily close."""
    if not tick:
        return False
    last_price = float(tick.get("last_price", 0) or 0)
    close = float(tick.get("close", 0) or 0)
    if close <= 0:
        return False
    change_pct = abs((last_price - close) / close * 100)
    return change_pct >= threshold_pct


# ── Exit rules, priority order (docs/PAPER_TRADING_DESIGN.md section 8.2) ──
#
# DTE-driven, not mode-driven. `position.mode` ("intraday"/"positional") only
# ever affected *which expiry got selected at entry* (strategy_builder.py) --
# it was never a meaningful input to how a position should be managed once
# open, and deriving exit behavior from it meant a position's exit discipline
# depended on which pipeline happened to emit the signal (see the mode-
# propagation fix), not on the trade's own state. Using dte (days to expiry)
# directly instead means the "intraday-style" tight time gate naturally
# applies once a position reaches its expiry day, and the "positional-style"
# day-based gate applies otherwise -- regardless of how long it's been held
# or which mode it was opened under. A position that happens to still be open
# 2 days before its own expiry gets the same treatment whether it started
# life as a same-day trade or a multi-day hold, which is the behavior that
# actually matters.

def check_stop_loss(unrealized_pnl: float, entry_credit: float) -> bool:
    return unrealized_pnl <= -STOP_LOSS_MULTIPLE * entry_credit


def check_profit_target(unrealized_pnl: float, entry_credit: float) -> bool:
    return unrealized_pnl >= PROFIT_TARGET_PCT * entry_credit


def check_theta_decay(current_debit: float, entry_credit: float, dte: int, now: datetime) -> bool:
    decayed_enough = current_debit <= THETA_DECAY_PCT * entry_credit
    if not decayed_enough:
        return False
    if dte == 0:
        return now.time() >= THETA_DECAY_INTRADAY_TIME
    return dte <= POSITIONAL_THETA_DECAY_DTE


def check_time_squareoff(dte: int, now: datetime) -> bool:
    if dte == 0:
        return now.time() >= INTRADAY_SQUAREOFF_TIME
    return dte == POSITIONAL_SQUAREOFF_DTE and now.time() >= POSITIONAL_SQUAREOFF_TIME


def compute_dte(expiry: str, today: date) -> int:
    return (date.fromisoformat(expiry) - today).days


def determine_exit_reason(position: PaperPosition, unrealized_pnl: float, current_debit: float,
                           now: datetime, gamma_trap_triggered: bool = False) -> Optional[str]:
    """Priority order: GAMMA_TRAP > stop loss > target > theta decay > square-off/expiry."""
    if gamma_trap_triggered:
        return "GAMMA_TRAP"
    if check_stop_loss(unrealized_pnl, position.entry_credit):
        return "STOP_LOSS"
    if check_profit_target(unrealized_pnl, position.entry_credit):
        return "TARGET"
    dte = compute_dte(position.expiry, now.date())
    if check_theta_decay(current_debit, position.entry_credit, dte, now):
        return "THETA_DECAY"
    if check_time_squareoff(dte, now):
        # Same 15:15 same-day gate either way; tag distinctly for the trade
        # log -- "this is expiry day" vs "this is the day before expiry".
        return "EXPIRY" if dte == 0 else "SQUARE_OFF"
    return None


# ── Exit costs / net P&L (docs/PAPER_TRADING_DESIGN.md sections 5.3, 8.1 step 6) ──

def compute_exit_costs(position: PaperPosition, qty: int) -> float:
    """Brokerage per leg + exchange charges on all legs + STT/stamp duty on
    buy-back legs (legs that were originally SELL, now being bought to close)."""
    total = compute_brokerage(len(position.legs), position.brokerage_per_order)
    for leg in position.legs:
        exit_price = leg.current_premium
        total += compute_exchange_charges(exit_price, qty, position.exchange_charges_pct)
        if leg.side == "SELL":   # buy-back leg
            total += compute_stt(exit_price, qty, position.stt_pct)
            total += compute_stamp_duty(exit_price, qty, DEFAULT_STAMP_DUTY_PCT)
    return total


def compute_exit_pnl(position: PaperPosition, current_debit: float, exit_costs: float) -> float:
    """Net P&L on close.

    NOTE: position.entry_credit is already net of entry-side costs (per its
    own docstring in models.py) -- only exit_costs need subtracting here.
    The design doc's section 8.1 step 6 literally says
    "(entry_credit - exit_debit) - entry_costs - exit_costs", which would
    double-subtract entry costs given entry_credit's documented semantics;
    this implementation follows the dataclass's own definition instead.
    """
    return (position.entry_credit - current_debit) - exit_costs


def close_position(position: PaperPosition, exit_reason: str, current_debit: float,
                    now: Optional[float] = None) -> PaperPosition:
    qty = compute_qty(position)
    exit_costs = compute_exit_costs(position, qty)
    position.status = "CLOSED"
    position.exit_timestamp = now if now is not None else time.time()
    position.exit_premium = current_debit
    position.pnl = compute_exit_pnl(position, current_debit, exit_costs)
    position.exit_reason = exit_reason
    return position


# ── Per-position MTM evaluation (docs/PAPER_TRADING_DESIGN.md section 8.1) ─

def evaluate_position(position: PaperPosition, redis, now: Optional[datetime] = None,
                       gamma_trap_triggered: bool = False) -> Optional[PaperPosition]:
    """Run one MTM cycle for a single open position.

    Returns None if the cycle should be skipped entirely (stale/missing
    data), otherwise the position -- closed (status/exit fields set) if an
    exit rule fired, or just updated (current_premium, unrealized P&L
    tracked by the caller) if still open.
    """
    now = now or datetime.now()

    options_agg = redis.hgetall(f"data:options_agg:{position.symbol}")
    if is_agg_data_stale(options_agg, now=now.timestamp()):
        logger.debug(f"[engine] Skipping MTM for {position.symbol} — stale/missing agg data")
        return None

    options_live = redis.hgetall(f"data:options_live:{position.symbol}")
    if not options_live:
        logger.debug(f"[engine] Skipping MTM for {position.symbol} — empty options_live (race)")
        return None

    update_leg_premiums(position, options_live)
    current_debit = compute_current_debit(position)
    unrealized_pnl = compute_unrealized_pnl(position, current_debit)

    exit_reason = determine_exit_reason(position, unrealized_pnl, current_debit, now,
                                         gamma_trap_triggered=gamma_trap_triggered)
    if exit_reason:
        return close_position(position, exit_reason, current_debit, now=now.timestamp())

    return position
