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
from collections import deque
from datetime import date, datetime, time as dtime
from typing import Optional

from lib.logging_util import get_logger
logger = get_logger("paper-trading")
from services.paper_trading.models import (
    DEFAULT_STAMP_DUTY_PCT,
    PaperPosition,
    apply_slippage,
    compute_transaction_costs,
)

STALE_AGG_DATA_MAX_AGE_SECONDS = 10.0
# Fast-move gamma proxy: spot must move >= this % within the rolling window.
GAMMA_TRAP_PROXY_MOVE_PCT = 1.0
GAMMA_TRAP_PROXY_WINDOW_SECONDS = 300

STOP_LOSS_MULTIPLE = 2.0        # 200% of entry credit (naked / strangle)
DEFINED_RISK_SL_MAX_LOSS_PCT = 0.6  # defined-risk SL: 60% of max loss, if tighter than 2x credit
PROFIT_TARGET_PCT = 0.5         # 50% of entry credit
# Theta-decay exit: near expiry, once at least this fraction of the entry
# credit is banked (but the 50% TARGET hasn't hit), close rather than carry
# rising gamma risk into the final sessions.
THETA_DECAY_MIN_PROFIT_PCT = 0.25
THETA_DECAY_INTRADAY_TIME = dtime(14, 0)
INTRADAY_SQUAREOFF_TIME = dtime(15, 15)
POSITIONAL_SQUAREOFF_TIME = dtime(15, 0)
POSITIONAL_SQUAREOFF_DTE = 1
POSITIONAL_THETA_DECAY_DTE = 3
EXPIRY_SETTLEMENT_TIME = dtime(15, 30)


def set_exit_params(stop_loss_multiple: Optional[float] = None,
                    profit_target_pct: Optional[float] = None) -> None:
    """Apply /paper_config overrides to the exit rules (main.py)."""
    global STOP_LOSS_MULTIPLE, PROFIT_TARGET_PCT
    if stop_loss_multiple is not None:
        STOP_LOSS_MULTIPLE = float(stop_loss_multiple)
    if profit_target_pct is not None:
        PROFIT_TARGET_PCT = float(profit_target_pct)


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

def option_field_key(strike: float, option_type: str) -> str:
    """Canonical Redis field for an option tick.

    Strikes reach the engine as int (from YAML/JSON) or float (from Sensibull);
    the publisher always writes ``f"{float(strike)}_{side}"``. Normalising here
    keeps the two from silently diverging — a mismatch leaves the leg on its
    entry premium, which hides real P&L.
    """
    return f"{float(strike)}_{option_type}"


def update_leg_premiums(position: PaperPosition, options_live: dict) -> bool:
    """Update each leg's current_premium from the options_live hash.

    Legs with a missing/malformed tick keep their last known premium (a
    stale-but-present fill is safer than crashing or force-skipping the
    whole position for one illiquid leg) -- returns False if any leg
    couldn't be updated, purely for logging/observability upstream.
    """
    all_found = True
    for leg in position.legs:
        raw = options_live.get(option_field_key(leg.strike, leg.option_type))
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
#
# The old proxy compared spot to the previous daily close (>=2%), which on a
# trend day fired every MTM cycle and churned every new entry.  The proxy is
# now a FAST-move detector (>=1% within 5 minutes, tracked in memory) and it
# only closes positions whose short legs sit on the side of the move (short
# CEs on a rally, short PEs on a sell-off) -- the side that is actually
# exposed to the gamma blow-up.

class SpotMoveTracker:
    """Rolling in-memory spot history per symbol (MTM thread only)."""

    def __init__(self, window_seconds: float = GAMMA_TRAP_PROXY_WINDOW_SECONDS):
        self.window_seconds = window_seconds
        self._history: dict[str, deque] = {}

    def update(self, symbol: str, price: float, ts: float) -> None:
        if price <= 0:
            return
        hist = self._history.setdefault(symbol, deque())
        hist.append((ts, price))
        while hist and ts - hist[0][0] > self.window_seconds:
            hist.popleft()

    def move_pct(self, symbol: str) -> float:
        """Signed % move from the oldest price in the window to the latest."""
        hist = self._history.get(symbol)
        if not hist or len(hist) < 2:
            return 0.0
        first = hist[0][1]
        return (hist[-1][1] - first) / first * 100


def move_direction(move_pct: float, threshold_pct: float = GAMMA_TRAP_PROXY_MOVE_PCT) -> Optional[str]:
    if move_pct >= threshold_pct:
        return "BULLISH"
    if move_pct <= -threshold_pct:
        return "BEARISH"
    return None


def is_threatened_by_move(position: PaperPosition, direction: Optional[str]) -> bool:
    """True if the position has a short leg on the side of a directional move."""
    if direction == "BULLISH":
        return any(leg.side == "SELL" and leg.option_type == "CE" for leg in position.legs)
    if direction == "BEARISH":
        return any(leg.side == "SELL" and leg.option_type == "PE" for leg in position.legs)
    return False


def check_gamma_trap_proxy(position: PaperPosition, move_pct: float,
                            threshold_pct: float = GAMMA_TRAP_PROXY_MOVE_PCT) -> bool:
    direction = move_direction(move_pct, threshold_pct)
    triggered = is_threatened_by_move(position, direction)
    if direction and not triggered:
        logger.debug(f"[engine] {position.symbol} {position.position_id}: fast {direction} move "
                     f"{move_pct:+.2f}% but no short leg on that side — gamma proxy not applied")
    return triggered


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

def compute_max_loss(position: PaperPosition) -> Optional[float]:
    """Max loss (rupees) of a defined-risk structure, or None if any short leg
    is unhedged (naked / strangle).  For an iron condor only one side can
    finish ITM, so max loss = widest wing * qty - credit."""
    widths = []
    for option_type in ("CE", "PE"):
        shorts = [leg.strike for leg in position.legs if leg.side == "SELL" and leg.option_type == option_type]
        longs = [leg.strike for leg in position.legs if leg.side == "BUY" and leg.option_type == option_type]
        if not shorts:
            continue
        if len(longs) < len(shorts):
            return None
        width = (max(longs) - min(shorts)) if option_type == "CE" else (max(shorts) - min(longs))
        if width <= 0:
            return None
        widths.append(width)
    if not widths:
        return None
    return max(widths) * compute_qty(position) - position.entry_credit


def stop_loss_amount(entry_credit: float, max_loss: Optional[float] = None) -> float:
    """Loss (positive rupees) at which STOP_LOSS fires.

    Naked/strangle: STOP_LOSS_MULTIPLE x credit.  Defined-risk: the tighter
    of that and DEFINED_RISK_SL_MAX_LOSS_PCT x max loss -- on a narrow spread
    2x credit can exceed the structure's max loss, making the SL unreachable.
    """
    sl = STOP_LOSS_MULTIPLE * entry_credit
    if max_loss is not None and max_loss > 0:
        sl = min(sl, DEFINED_RISK_SL_MAX_LOSS_PCT * max_loss)
    return sl


def check_stop_loss(unrealized_pnl: float, entry_credit: float,
                     max_loss: Optional[float] = None) -> bool:
    return unrealized_pnl <= -stop_loss_amount(entry_credit, max_loss)


def check_profit_target(unrealized_pnl: float, entry_credit: float) -> bool:
    return unrealized_pnl >= PROFIT_TARGET_PCT * entry_credit


def check_theta_decay(unrealized_pnl: float, entry_credit: float, dte: int, now: datetime) -> bool:
    """Near-expiry time exit: profit banked >= THETA_DECAY_MIN_PROFIT_PCT of
    credit (but below TARGET, which is checked first) and either dte in
    1..POSITIONAL_THETA_DECAY_DTE, or expiry day after 14:00.  Stops a
    mostly-decayed position from riding expiry gamma for the last few rupees."""
    if entry_credit <= 0 or unrealized_pnl < THETA_DECAY_MIN_PROFIT_PCT * entry_credit:
        return False
    if dte == 0:
        return now.time() >= THETA_DECAY_INTRADAY_TIME
    return 0 < dte <= POSITIONAL_THETA_DECAY_DTE


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
    if check_stop_loss(unrealized_pnl, position.entry_credit, compute_max_loss(position)):
        return "STOP_LOSS"
    if check_profit_target(unrealized_pnl, position.entry_credit):
        return "TARGET"
    dte = compute_dte(position.expiry, now.date())
    if check_theta_decay(unrealized_pnl, position.entry_credit, dte, now):
        return "THETA_DECAY"
    if check_time_squareoff(dte, now):
        # Same 15:15 same-day gate either way; tag distinctly for the trade
        # log -- "this is expiry day" vs "this is the day before expiry".
        return "EXPIRY" if dte == 0 else "SQUARE_OFF"
    return None


# ── Expiry settlement (positions still open past expiry) ────────────────────

def is_past_expiry(position: PaperPosition, now: datetime) -> bool:
    dte = compute_dte(position.expiry, now.date())
    return dte < 0 or (dte == 0 and now.time() >= EXPIRY_SETTLEMENT_TIME)


def intrinsic_value(strike: float, option_type: str, spot: float) -> float:
    if option_type == "CE":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def read_spot(redis, symbol: str) -> Optional[float]:
    tick = redis.hgetall(f"data:tick:{symbol}") or {}
    try:
        spot = float(tick.get("last_price", 0) or 0)
    except (TypeError, ValueError):
        return None
    return spot if spot > 0 else None


def settle_at_expiry(position: PaperPosition, spot: float, now: Optional[float] = None) -> PaperPosition:
    """Cash-settle every leg at intrinsic value from spot.  No brokerage /
    slippage on cash settlement.  NOTE: when settled a day or more after
    expiry (service was down), the latest spot is used as the proxy for the
    expiry-day closing spot."""
    for leg in position.legs:
        leg.current_premium = intrinsic_value(leg.strike, leg.option_type, spot)
    return close_position(position, "EXPIRY_SETTLEMENT", now=now, settlement=True)


# ── Exit fills / costs / net P&L (design doc sections 5.3, 8.1 step 6) ──────

def _exit_action(leg) -> str:
    """Closing order: shorts are bought back, longs are sold."""
    return "BUY" if leg.side == "SELL" else "SELL"


def _touch_price(tick: Optional[dict], action: str) -> Optional[float]:
    """Best ask for a BUY, best bid for a SELL: top-level bid/ask published by
    market-data when present, else Zerodha market depth level 0."""
    if not isinstance(tick, dict):
        return None
    try:
        price = float(tick.get("ask" if action == "BUY" else "bid", 0) or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price > 0:
        return price
    depth = tick.get("depth")
    if not isinstance(depth, dict):
        return None
    book = depth.get("sell" if action == "BUY" else "buy") or []
    try:
        price = float(book[0].get("price", 0) or 0)
    except (IndexError, AttributeError, TypeError, ValueError):
        return None
    return price if price > 0 else None


def compute_exit_fills(position: PaperPosition, options_live: Optional[dict] = None) -> list[float]:
    """Per-leg exit fill: touch price from depth when available, else LTP
    with the same flat slippage model used at entry."""
    fills = []
    for leg in position.legs:
        action = _exit_action(leg)
        tick = None
        raw = (options_live or {}).get(option_field_key(leg.strike, leg.option_type))
        if raw:
            try:
                tick = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except (json.JSONDecodeError, TypeError):
                tick = None
        touch = _touch_price(tick, action)
        fills.append(touch if touch is not None
                     else apply_slippage(leg.current_premium, action, position.slippage_bps))
    return fills


def compute_exit_costs(position: PaperPosition, qty: int, fills: Optional[list[float]] = None) -> float:
    """Charges on the closing orders: STT on longs being sold, stamp duty on
    shorts being bought back, exchange + SEBI + GST on all (models.py)."""
    fills = fills if fills is not None else [leg.current_premium for leg in position.legs]
    trades = [(fill, _exit_action(leg)) for leg, fill in zip(position.legs, fills)]
    return compute_transaction_costs(
        trades, qty,
        brokerage_per_order=position.brokerage_per_order,
        stt_pct=position.stt_pct,
        exchange_charges_pct=position.exchange_charges_pct,
        stamp_duty_pct=DEFAULT_STAMP_DUTY_PCT,
    )


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


def close_position(position: PaperPosition, exit_reason: str, now: Optional[float] = None,
                    options_live: Optional[dict] = None, settlement: bool = False) -> PaperPosition:
    """Close at realistic exit fills (slippage / touch price) or, for
    settlement, at the legs' current (intrinsic) premiums with no costs."""
    qty = compute_qty(position)
    if settlement:
        fills = [leg.current_premium for leg in position.legs]
        exit_costs = 0.0
    else:
        fills = compute_exit_fills(position, options_live)
        exit_costs = compute_exit_costs(position, qty, fills)
    exit_debit = sum(
        fill if leg.side == "SELL" else -fill for leg, fill in zip(position.legs, fills)
    ) * qty
    position.status = "CLOSED"
    position.exit_timestamp = now if now is not None else time.time()
    position.exit_premium = exit_debit
    position.pnl = compute_exit_pnl(position, exit_debit, exit_costs)
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

    # Past expiry: option data is gone/stale by definition, so settle at
    # intrinsic from spot instead of waiting on fresh option ticks forever.
    if is_past_expiry(position, now):
        spot = read_spot(redis, position.symbol)
        if spot is None:
            logger.debug(f"[engine] {position.symbol} {position.position_id}: past expiry "
                           f"{position.expiry} but no spot in data:tick — cannot settle yet")
            return None
        return settle_at_expiry(position, spot, now=now.timestamp())

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
        return close_position(position, exit_reason, now=now.timestamp(), options_live=options_live)

    return position
