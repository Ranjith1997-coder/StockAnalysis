"""
Paper Trading — Core Data Models

Dataclasses (OptionLeg, PaperPosition, PaperAccount), Redis key schema, cost
model, and expiry/strike-gap helpers shared by every other paper_trading
module. See docs/PAPER_TRADING_DESIGN.md section 5 for the full spec.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Optional


# ── Redis key schema (docs/PAPER_TRADING_DESIGN.md section 5.2) ────────────

ACCOUNT_KEY = "paper:account"
POSITIONS_OPEN_KEY = "paper:positions:open"
CONFIG_KEY = "paper:config"
TRADES_STREAM = "paper:trades"
TRADES_STREAM_MAXLEN = 5000
COOLDOWN_TTL_SECONDS = 900              # 15 min
INSTRUMENTS_CACHE_TTL_SECONDS = 86400   # 1 day


def positions_closed_key(day: str) -> str:
    return f"paper:positions:closed:{day}"


def daily_pnl_key(day: str) -> str:
    return f"paper:daily_pnl:{day}"


def cooldown_key(symbol: str, strategy: str) -> str:
    return f"paper:cooldown:{symbol}:{strategy}"


def instruments_key(symbol: str) -> str:
    return f"paper:instruments:{symbol}"


# ── Cost model (docs/PAPER_TRADING_DESIGN.md section 5.3) ──────────────────

DEFAULT_SLIPPAGE_BPS = 50               # 0.5%
DEFAULT_BROKERAGE_PER_ORDER = 20.0      # Rs 20 per leg per order (entry or exit)
DEFAULT_STT_PCT = 0.001                 # 0.1% on sell-side premium
DEFAULT_EXCHANGE_CHARGES_PCT = 0.0005   # 0.05% of premium
DEFAULT_STAMP_DUTY_PCT = 0.00003        # 0.003% on sell-side premium
DEFAULT_CAPITAL = 1_000_000.0           # Rs 10L virtual capital


def apply_slippage(ltp: float, side: str, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> float:
    """Simulate a realistic fill: sells fill below LTP, buys fill above."""
    factor = slippage_bps / 10000
    if side == "SELL":
        return ltp * (1 - factor)
    return ltp * (1 + factor)


def compute_stt(sell_premium: float, qty: int, stt_pct: float = DEFAULT_STT_PCT) -> float:
    """STT applies only to the sell side of a leg."""
    return stt_pct * sell_premium * qty


def compute_exchange_charges(premium: float, qty: int,
                              pct: float = DEFAULT_EXCHANGE_CHARGES_PCT) -> float:
    return pct * premium * qty


def compute_stamp_duty(sell_premium: float, qty: int,
                        pct: float = DEFAULT_STAMP_DUTY_PCT) -> float:
    """Stamp duty applies only to the sell side of a leg."""
    return pct * sell_premium * qty


def compute_brokerage(num_legs: int, per_order: float = DEFAULT_BROKERAGE_PER_ORDER) -> float:
    """Brokerage for one side (entry OR exit) of a multi-leg position."""
    return num_legs * per_order


# ── Expiry / strike-gap helpers (docs/PAPER_TRADING_DESIGN.md section 7.1/7.3) ──

def select_expiry(per_expiry_map_keys: list[str], mode: str, today: date) -> str:
    """
    Pick the expiry to trade.

    Args:
        per_expiry_map_keys: ISO date strings from data:sensibull's per_expiry_map,
            not necessarily sorted.
        mode: "intraday" or "positional"
        today: injected explicitly for testability
    """
    expiries = sorted(per_expiry_map_keys)
    if not expiries:
        return ""
    if mode == "intraday":
        return expiries[0]
    for exp in expiries:
        dte = (date.fromisoformat(exp) - today).days
        if dte >= 3:
            return exp
    return expiries[0]


def compute_strike_gap(strike_keys: list[str], default: float = 50.0) -> float:
    """Derive strike spacing from data:options_live hash keys ("{strike}_{CE|PE}")."""
    strikes = sorted({float(k.rsplit("_", 1)[0]) for k in strike_keys})
    if len(strikes) < 2:
        return default
    gaps = [strikes[i + 1] - strikes[i] for i in range(len(strikes) - 1)]
    return min(gaps)


# ── Dataclasses (docs/PAPER_TRADING_DESIGN.md section 5.1) ─────────────────

@dataclass
class OptionLeg:
    strike: float
    option_type: str        # "CE" | "PE"
    side: str                # "SELL" | "BUY"
    lots: int
    entry_premium: float     # per unit, after slippage
    current_premium: float = 0.0
    entry_timestamp: float = 0.0


@dataclass
class PaperPosition:
    position_id: str
    symbol: str              # "NIFTY" | "BANKNIFTY" | "SENSEX"
    strategy: str             # "IRON_CONDOR" | "STRANGLE" | "CREDIT_SPREAD" | "NAKED_CE" | "NAKED_PE"
    mode: str                 # "intraday" | "positional"
    direction: str            # "NEUTRAL" | "BULLISH" | "BEARISH"
    legs: list[OptionLeg]
    expiry: str               # ISO date, e.g. "2026-07-21"
    scrip: str                # SPAN scrip code, e.g. "NIFTY26721"
    lot_size: int
    entry_timestamp: float
    entry_credit: float       # net premium received (after slippage + costs)
    margin_blocked: float     # from SPAN API
    status: str = "OPEN"      # "OPEN" | "CLOSED"
    exit_timestamp: Optional[float] = None
    exit_premium: Optional[float] = None
    pnl: Optional[float] = None
    exit_reason: Optional[str] = None   # "STOP_LOSS" | "TARGET" | "THETA_DECAY" | "GAMMA_TRAP" | "SQUARE_OFF" | "EXPIRY" | "MANUAL"
    signal_source: str = ""             # "RANGE_BOUND_SETUP" | "SKEW_FADE_SETUP" | "CONFLUENCE"
    signal_score: Optional[float] = None
    signal_context: dict = field(default_factory=dict)
    slippage_bps: int = DEFAULT_SLIPPAGE_BPS
    brokerage_per_order: float = DEFAULT_BROKERAGE_PER_ORDER
    stt_pct: float = DEFAULT_STT_PCT
    exchange_charges_pct: float = DEFAULT_EXCHANGE_CHARGES_PCT

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "PaperPosition":
        d = json.loads(raw)
        d["legs"] = [OptionLeg(**leg) for leg in d.get("legs", [])]
        return cls(**d)


@dataclass
class PaperAccount:
    capital: float = DEFAULT_CAPITAL
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0
    available_margin: float = DEFAULT_CAPITAL
    open_positions: int = 0
    day_start_capital: float = DEFAULT_CAPITAL
    max_drawdown: float = 0.0
    daily_realized_pnl: float = 0.0
    daily_trades: int = 0
    daily_wins: int = 0
    daily_losses: int = 0

    _FLOAT_FIELDS = {
        "capital", "realized_pnl", "unrealized_pnl", "margin_used",
        "available_margin", "day_start_capital", "max_drawdown", "daily_realized_pnl",
    }
    _INT_FIELDS = {"open_positions", "daily_trades", "daily_wins", "daily_losses"}

    def to_redis_mapping(self) -> dict:
        return {k: str(v) for k, v in asdict(self).items()}

    @classmethod
    def from_redis_mapping(cls, mapping: dict) -> "PaperAccount":
        if not mapping:
            return cls()
        kwargs = {}
        for k, v in mapping.items():
            if k in cls._FLOAT_FIELDS:
                kwargs[k] = float(v)
            elif k in cls._INT_FIELDS:
                kwargs[k] = int(v)
        return cls(**kwargs)
