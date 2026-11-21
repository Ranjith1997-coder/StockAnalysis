"""
Paper Trading — Strategy Builder

Turns an EntrySignal (already past entry_router's account/position-level
filters) into a PaperPosition: expiry selection, strike selection, premium
lookup + slippage, SPAN margin, position sizing, and cost accounting.
See docs/PAPER_TRADING_DESIGN.md section 7 for the full spec.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from typing import Optional

from lib.logging_util import get_logger
logger = get_logger("paper-trading")
from services.paper_trading.models import (
    DEFAULT_BROKERAGE_PER_ORDER,
    DEFAULT_EXCHANGE_CHARGES_PCT,
    DEFAULT_STAMP_DUTY_PCT,
    DEFAULT_STT_PCT,
    OptionLeg,
    PaperAccount,
    PaperPosition,
    apply_slippage,
    compute_strike_gap,
    compute_transaction_costs,
    exchange_charges_pct_for,
    select_expiry,
)
from services.paper_trading.signal_router import MAX_PORTFOLIO_MARGIN_PCT, EntrySignal
from services.paper_trading.span_calculator import SpanCalculator, SpanLegRequest

# Risk percentages by strategy — verified against real Zerodha SPAN margin
# (docs/PAPER_TRADING_DESIGN.md section 7.5). Naked/strangle strategies get no
# SPAN spread-margin offset, so they carry ~4-5x the margin of defined-risk
# spreads at the same risk_pct -- these values are the minimum needed for 1
# lot at those verified margin levels, not arbitrary round numbers.
RISK_PCT = {
    "IRON_CONDOR": 0.08,
    "STRANGLE": 0.18,
    "CREDIT_SPREAD": 0.05,
    "NAKED_CE": 0.16,
    "NAKED_PE": 0.16,
}

# HIGH-level CONFLUENCE credit spreads: short leg 2 strikes OTM (3 above this
# score), protective wing HIGH_CONFLUENCE_WING_STRIKES further out.  These
# replaced the old naked 1-strike-from-ATM NAKED_CE/NAKED_PE entries.
HIGH_SCORE_OTM_THRESHOLD = 15
HIGH_CONFLUENCE_WING_STRIKES = 2

# No new entries on the position's own expiry day after this time.
EXPIRY_DAY_ENTRY_CUTOFF = dtime(14, 0)


@dataclass
class PlannedLeg:
    strike: float
    option_type: str    # "CE" | "PE"
    side: str             # "SELL" | "BUY"


@dataclass
class FilledLeg:
    strike: float
    option_type: str
    side: str
    fill_price: float    # post-slippage


# ── Position sizing (docs/PAPER_TRADING_DESIGN.md section 7.5) ─────────────

def compute_lots(capital: float, margin_per_lot: float, risk_pct: float) -> int:
    if margin_per_lot <= 0:
        return 0
    risk_amount = capital * risk_pct
    return max(int(risk_amount / margin_per_lot), 0)


# ── Strike selection (docs/PAPER_TRADING_DESIGN.md section 7.2) ────────────

def select_strikes(signal: EntrySignal, strike_gap: float,
                    atm_strike: Optional[float] = None) -> list[PlannedLeg]:
    """Select strikes for a signal's strategy. Returns [] if required inputs are missing."""
    strategy = signal.strategy

    if strategy in ("IRON_CONDOR", "STRANGLE"):
        if signal.put_wall_strike is None or signal.call_wall_strike is None:
            return []
        legs = [
            PlannedLeg(signal.put_wall_strike, "PE", "SELL"),
            PlannedLeg(signal.call_wall_strike, "CE", "SELL"),
        ]
        if strategy == "IRON_CONDOR":
            legs.append(PlannedLeg(signal.put_wall_strike - 2 * strike_gap, "PE", "BUY"))
            legs.append(PlannedLeg(signal.call_wall_strike + 2 * strike_gap, "CE", "BUY"))
        return legs

    if strategy == "CREDIT_SPREAD":
        if signal.sr_level is not None:
            # Any signal with sr_level set (SKEW_FADE_SETUP or PINN_MISPRICING)
            # uses the specific strike as the short leg.
            if signal.direction == "BULLISH":
                return [
                    PlannedLeg(signal.sr_level, "PE", "SELL"),
                    PlannedLeg(signal.sr_level - 2 * strike_gap, "PE", "BUY"),
                ]
            return [
                PlannedLeg(signal.sr_level, "CE", "SELL"),
                PlannedLeg(signal.sr_level + 2 * strike_gap, "CE", "BUY"),
            ]
        # CONFLUENCE-sourced CREDIT_SPREAD (MODERATE, 2-layer): not explicitly
        # specified in section 7.2 (which only documents SKEW_FADE_SETUP's
        # sr_level for this strategy) -- inferred rule, built the same way as
        # the naked-directional case below but with one protective long leg,
        # since a MODERATE confluence is meant to be defined-risk, not naked.
        # HIGH confluence: further OTM short + wider wing (defined risk).
        if atm_strike is None or signal.direction not in ("BULLISH", "BEARISH"):
            return []
        if signal.level == "HIGH":
            otm = 3 if (signal.score or 0) > HIGH_SCORE_OTM_THRESHOLD else 2
            wing = HIGH_CONFLUENCE_WING_STRIKES
        else:
            otm, wing = 1, 1
        if signal.direction == "BEARISH":
            short_strike = atm_strike + otm * strike_gap
            return [
                PlannedLeg(short_strike, "CE", "SELL"),
                PlannedLeg(short_strike + wing * strike_gap, "CE", "BUY"),
            ]
        short_strike = atm_strike - otm * strike_gap
        return [
            PlannedLeg(short_strike, "PE", "SELL"),
            PlannedLeg(short_strike - wing * strike_gap, "PE", "BUY"),
        ]

    # NAKED_CE / NAKED_PE are no longer opened (unbounded risk 1 strike from
    # ATM); CONFLUENCE now routes to defined-risk CREDIT_SPREAD instead.
    return []


# ── Premium / greeks lookup (docs/PAPER_TRADING_DESIGN.md section 7.4) ─────

def get_ltp(tick: dict) -> Optional[float]:
    ltp = float(tick.get("ltp", 0) or 0)
    return ltp if ltp > 0 else None


def has_valid_greeks(tick: dict) -> bool:
    iv = float(tick.get("iv", 0) or 0)
    return iv > 0


# ── Cost / credit accounting (docs/PAPER_TRADING_DESIGN.md section 5.3) ────

def compute_entry_costs(legs: list[FilledLeg], qty: int,
                         brokerage_per_order: float = DEFAULT_BROKERAGE_PER_ORDER,
                         stt_pct: float = DEFAULT_STT_PCT,
                         exchange_charges_pct: float = DEFAULT_EXCHANGE_CHARGES_PCT,
                         stamp_duty_pct: float = DEFAULT_STAMP_DUTY_PCT) -> float:
    """Entry orders: STT on legs sold, stamp duty on legs bought, plus
    exchange/SEBI/GST on all (models.compute_transaction_costs)."""
    return compute_transaction_costs(
        [(leg.fill_price, leg.side) for leg in legs], qty,
        brokerage_per_order=brokerage_per_order, stt_pct=stt_pct,
        exchange_charges_pct=exchange_charges_pct, stamp_duty_pct=stamp_duty_pct,
    )


def compute_entry_credit(legs: list[FilledLeg], qty: int, entry_costs: float) -> float:
    """Net premium received: (sell fills - buy fills) x qty, minus entry-side costs."""
    gross_per_unit = (
        sum(leg.fill_price for leg in legs if leg.side == "SELL")
        - sum(leg.fill_price for leg in legs if leg.side == "BUY")
    )
    return gross_per_unit * qty - entry_costs


# ── Redis-facing wrappers around the pure helpers in models.py ─────────────

def fetch_expiry(symbol: str, mode: str, redis, today: Optional[date] = None) -> str:
    raw = redis.hget(f"data:sensibull:{symbol}", "current_json")
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        keys = list(data.get("per_expiry_map", {}).keys())
    except (json.JSONDecodeError, TypeError):
        return ""
    return select_expiry(keys, mode, today or date.today())


def resolve_expiry(signal: EntrySignal, mode: str, redis, today: date) -> str:
    """Use the signal's own expiry (PINN signals carry one) when it is a valid
    ISO date not in the past; otherwise select from the sensibull chain."""
    if signal.expiry:
        try:
            if date.fromisoformat(signal.expiry) >= today:
                return signal.expiry
        except ValueError:
            pass
        logger.debug(f"[strategy_builder] {signal.symbol} | SOURCE expiry={signal.expiry} invalid/past — "
                     f"falling back to chain selection")
    return fetch_expiry(signal.symbol, mode, redis, today=today)


def fetch_strike_gap(symbol: str, redis) -> float:
    raw = redis.hgetall(f"data:options_live:{symbol}")
    return compute_strike_gap(list(raw.keys()) if raw else [])


def fetch_atm_strike(symbol: str, redis) -> Optional[float]:
    raw = redis.hget(f"data:options_agg:{symbol}", "atm_strike")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def fetch_tick(symbol: str, strike: float, option_type: str, redis) -> Optional[dict]:
    raw = redis.hget(f"data:options_live:{symbol}", f"{float(strike)}_{option_type}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


# ── Full entry flow (docs/PAPER_TRADING_DESIGN.md section 7.6) ─────────────

def build_position(signal: EntrySignal, redis, span_calculator: SpanCalculator,
                    account: PaperAccount, mode: str = "intraday",
                    risk_scale: float = 1.0, now: Optional[datetime] = None) -> Optional[PaperPosition]:
    """Run the full strike-selection -> SPAN -> sizing pipeline for one signal.

    Returns None (with a logged reason) if the signal can't produce a valid,
    fundable position -- this function assumes entry_router's account-level
    filters already passed.
    """
    symbol = signal.symbol
    now = now or datetime.now()

    expiry = resolve_expiry(signal, mode, redis, now.date())
    if not expiry:
        logger.warning(f"[strategy_builder] No expiry available for {symbol}")
        return None
    if expiry == now.date().isoformat() and now.time() >= EXPIRY_DAY_ENTRY_CUTOFF:
        logger.debug(f"[strategy_builder] {symbol} | SOURCE expiry={expiry} — expiry day after "
                     f"{EXPIRY_DAY_ENTRY_CUTOFF:%H:%M}, no new entries")
        return None

    strike_gap = fetch_strike_gap(symbol, redis)
    atm_strike = fetch_atm_strike(symbol, redis)

    planned_legs = select_strikes(signal, strike_gap, atm_strike)
    if not planned_legs:
        logger.warning(f"[strategy_builder] No strikes selected for {symbol}/{signal.strategy}")
        return None

    filled_legs: list[FilledLeg] = []
    for planned in planned_legs:
        tick = fetch_tick(symbol, planned.strike, planned.option_type, redis)
        if tick is None:
            logger.info(f"[strategy_builder] Skipping {symbol} {signal.strategy} — illiquid strike "
                        f"{planned.strike}{planned.option_type}")
            return None
        ltp = get_ltp(tick)
        if ltp is None:
            logger.info(f"[strategy_builder] Skipping {symbol} {signal.strategy} — LTP=0 at "
                        f"{planned.strike}{planned.option_type}")
            return None
        if planned.side == "SELL" and not has_valid_greeks(tick):
            logger.info(f"[strategy_builder] Skipping {symbol} {signal.strategy} — no greeks (iv) at "
                        f"{planned.strike}{planned.option_type}")
            return None
        fill_price = apply_slippage(ltp, planned.side)
        filled_legs.append(FilledLeg(planned.strike, planned.option_type, planned.side, fill_price))

    lot_size = span_calculator.get_lot_size(symbol, expiry)
    if not lot_size:
        logger.warning(f"[strategy_builder] No lot size available for {symbol}/{expiry}")
        return None

    scrip = span_calculator.get_scrip(symbol, expiry) or ""

    span_legs = [
        SpanLegRequest(symbol=symbol, expiry=expiry, strike=leg.strike,
                       option_type=leg.option_type, qty=lot_size, trade=leg.side.lower())
        for leg in filled_legs
    ]
    margin_per_lot = span_calculator.calculate_margin(span_legs)
    if margin_per_lot is None:
        logger.warning(f"[strategy_builder] SPAN margin unavailable for {symbol}/{signal.strategy} — skipping")
        return None

    risk_pct = RISK_PCT.get(signal.strategy, 0.05) * risk_scale
    lots = compute_lots(account.capital, margin_per_lot, risk_pct)
    # Never size past the portfolio margin cap (signal_router filter only
    # checks margin already used, not this trade's).
    headroom = MAX_PORTFOLIO_MARGIN_PCT * account.capital - account.margin_used
    lots = min(lots, max(int(headroom / margin_per_lot), 0))
    if lots == 0:
        logger.info(f"[strategy_builder] Skipping {symbol} {signal.strategy} — "
                    f"insufficient capital/margin headroom (₹{margin_per_lot:.0f}/lot)")
        return None

    qty = lot_size * lots
    exchange_pct = exchange_charges_pct_for(symbol)
    entry_costs = compute_entry_costs(filled_legs, qty, exchange_charges_pct=exchange_pct)
    entry_credit = compute_entry_credit(filled_legs, qty, entry_costs)

    legs = [
        OptionLeg(strike=leg.strike, option_type=leg.option_type, side=leg.side,
                  lots=lots, entry_premium=leg.fill_price, current_premium=leg.fill_price,
                  entry_timestamp=time.time())
        for leg in filled_legs
    ]

    return PaperPosition(
        position_id=str(uuid.uuid4()),
        symbol=symbol,
        strategy=signal.strategy,
        mode=mode,
        direction=signal.direction,
        legs=legs,
        expiry=expiry,
        scrip=scrip,
        lot_size=lot_size,
        entry_timestamp=time.time(),
        entry_credit=entry_credit,
        margin_blocked=margin_per_lot * lots,
        signal_source=signal.signal_source,
        signal_score=signal.score,
        signal_context=signal.signal_context,
        exchange_charges_pct=exchange_pct,
    )
