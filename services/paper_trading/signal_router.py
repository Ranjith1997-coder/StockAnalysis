"""
Paper Trading — Signal Router

Two independent signal sources feed a shared entry/exit pipeline:
  1. Composite setups (RANGE_BOUND_SETUP, SKEW_FADE_SETUP, GAMMA_TRAP) parsed
     directly from analysis:results.
  2. Cross-layer confluence, already detected upstream by the standalone
     signal-intelligence service — this module does NOT run its own
     SignalCorrelator, it just deserializes intelligence:confluence messages.

See docs/PAPER_TRADING_DESIGN.md sections 2 and 6 for the full spec.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from common.constants import LIVE_OPTIONS_INDICES
from services.common.logging import get_logger
logger = get_logger("paper-trading")
from intelligence.correlator import Confluence
from intelligence.signal import Direction
from services.paper_trading.models import POSITIONS_OPEN_KEY, PaperAccount, PaperPosition, cooldown_key

MAX_POSITIONS = 8
MAX_PORTFOLIO_MARGIN_PCT = 0.40
DAILY_LOSS_LIMIT_PCT = -0.03
MAX_CORRELATED_NAKED = 2
NAKED_STRATEGIES = {"STRANGLE", "NAKED_CE", "NAKED_PE"}


@dataclass
class EntrySignal:
    strategy: str            # "IRON_CONDOR" | "STRANGLE" | "CREDIT_SPREAD" | "NAKED_CE" | "NAKED_PE"
    symbol: str
    direction: str = "NEUTRAL"
    # RANGE_BOUND_SETUP fields
    put_wall_strike: Optional[float] = None
    call_wall_strike: Optional[float] = None
    iv_percentile: Optional[float] = None
    # SKEW_FADE_SETUP fields
    sr_level: Optional[float] = None
    # CONFLUENCE fields
    score: Optional[float] = None
    level: Optional[str] = None      # "HIGH" | "MODERATE"
    # Common
    signal_source: str = ""
    signal_context: dict = field(default_factory=dict)
    mode: str = "intraday"    # "intraday" | "positional" -- which analysis cycle produced this


@dataclass
class ExitSignal:
    symbol: str
    reason: str                       # "GAMMA_TRAP" | "MANUAL"
    position_id: Optional[str] = None  # None = all positions on symbol


# ── Source 1: composite setups from analysis:results ───────────────────────

def parse_analysis_result(fields: dict) -> tuple[list[EntrySignal], list[ExitSignal]]:
    """Parse one analysis:results stream message into entry/exit signals.

    Fast path: skip JSON parsing entirely if this cycle has neither a trend
    nor a composite-setup override -- composite setups always set
    PRIORITY_OVERRIDE (a top-level key) even though their own weight is 0.
    """
    entries: list[EntrySignal] = []
    exits: list[ExitSignal] = []

    trend_found = fields.get("trend_found", "false").lower() == "true"
    if not trend_found and not fields.get("PRIORITY_OVERRIDE"):
        return entries, exits

    try:
        analysis_json = json.loads(fields.get("analysis_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"[signal_router] Malformed analysis_json for {fields.get('symbol')}")
        return entries, exits

    symbol = fields.get("symbol", "")
    # worker.py now echoes the job's mode back onto every analysis:results
    # message (services/analysis_engine/worker.py:_result_dict) -- this is
    # the only reliable way to know whether a composite setup came from an
    # intraday cycle (09:15-15:30) or the 8pm positional run, since both
    # dispatch through the same analysis-engine and stream.
    mode = fields.get("mode", "intraday")
    neutral = analysis_json.get("NEUTRAL", {})

    if "GAMMA_TRAP" in neutral or neutral.get("GAMMA_TRAP_ACTIVE"):
        exits.append(ExitSignal(symbol=symbol, reason="GAMMA_TRAP"))

    if "RANGE_BOUND_SETUP" in neutral:
        setup = neutral["RANGE_BOUND_SETUP"]
        try:
            entries.append(EntrySignal(
                strategy=setup["setup_type"],   # "IRON_CONDOR" | "STRANGLE"
                symbol=symbol,
                put_wall_strike=float(setup["put_wall_strike"]),
                call_wall_strike=float(setup["call_wall_strike"]),
                iv_percentile=float(setup.get("iv_percentile", 0)),
                signal_source="RANGE_BOUND_SETUP",
                mode=mode,
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"[signal_router] Malformed RANGE_BOUND_SETUP for {symbol}: {e}")

    if "SKEW_FADE_SETUP" in neutral:
        setup = neutral["SKEW_FADE_SETUP"]
        try:
            entries.append(EntrySignal(
                strategy="CREDIT_SPREAD",
                symbol=symbol,
                direction=setup["fade_direction"],
                sr_level=float(setup["sr_level"]),
                signal_source="SKEW_FADE_SETUP",
                mode=mode,
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"[signal_router] Malformed SKEW_FADE_SETUP for {symbol}: {e}")

    return entries, exits


# ── Source 2: already-detected confluence from intelligence:confluence ─────

def parse_confluence_message(fields: dict,
                              indices: tuple[str, ...] = tuple(LIVE_OPTIONS_INDICES)) -> Optional[EntrySignal]:
    """Deserialize an intelligence:confluence message into an EntrySignal.

    No local SignalCorrelator here -- confluence detection already happened
    in the standalone signal-intelligence service; this just reuses the
    shared wire-format helper to reconstruct the Confluence object.
    """
    confluence = Confluence.from_stream_fields(fields)
    if confluence.symbol not in indices:
        return None

    strategy = "NAKED_CE" if confluence.direction == Direction.BEARISH else "NAKED_PE"
    if confluence.level == "MODERATE":
        strategy = "CREDIT_SPREAD"   # defined risk for 2-layer confluence

    return EntrySignal(
        strategy=strategy,
        symbol=confluence.symbol,
        direction=confluence.direction.name,
        score=confluence.score,
        level=confluence.level,
        signal_source="CONFLUENCE",
    )


# ── Entry filters (docs/PAPER_TRADING_DESIGN.md section 6.2) ───────────────
# Account/position-level filters only. Strike-availability and greeks checks
# happen later in strategy_builder, once strikes are actually selected.

def has_cooldown_lock(redis, symbol: str, strategy: str) -> bool:
    return redis.get(cooldown_key(symbol, strategy)) is not None


def has_duplicate_position(open_positions: list[PaperPosition], symbol: str, strategy: str) -> bool:
    return any(p.symbol == symbol and p.strategy == strategy for p in open_positions)


def portfolio_margin_exceeded(account: PaperAccount, max_pct: float = MAX_PORTFOLIO_MARGIN_PCT) -> bool:
    return account.margin_used >= max_pct * account.capital


def daily_loss_limit_hit(account: PaperAccount, limit_pct: float = DAILY_LOSS_LIMIT_PCT) -> bool:
    return account.daily_realized_pnl <= limit_pct * account.capital


def correlated_naked_cap_hit(open_positions: list[PaperPosition], direction: str,
                              max_count: int = MAX_CORRELATED_NAKED) -> bool:
    naked_same_direction = [
        p for p in open_positions
        if p.strategy in NAKED_STRATEGIES and p.direction == direction
    ]
    return len(naked_same_direction) >= max_count


def check_entry_filters(signal: EntrySignal, redis, account: PaperAccount,
                         open_positions: list[PaperPosition],
                         max_positions: int = MAX_POSITIONS) -> tuple[bool, str]:
    """Run all entry filters in order. Returns (passed, reason_if_rejected)."""
    if has_cooldown_lock(redis, signal.symbol, signal.strategy):
        return False, "cooldown_active"
    if redis.hlen(POSITIONS_OPEN_KEY) >= max_positions:
        return False, "max_positions_reached"
    if has_duplicate_position(open_positions, signal.symbol, signal.strategy):
        return False, "duplicate_position"
    if portfolio_margin_exceeded(account):
        return False, "portfolio_margin_exceeded"
    if daily_loss_limit_hit(account):
        return False, "daily_loss_limit_hit"
    if signal.strategy in NAKED_STRATEGIES and correlated_naked_cap_hit(open_positions, signal.direction):
        return False, "correlated_naked_cap_hit"
    return True, ""
