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
COOLDOWN_TTL_SECONDS = 900              # 15 min, set on exit
STOP_LOSS_COOLDOWN_TTL_SECONDS = 1800   # 30 min after a STOP_LOSS / GAMMA_TRAP exit
INSTRUMENTS_CACHE_TTL_SECONDS = 86400   # 1 day
INSTRUMENTS_FETCHED_AT_KEY = "paper:instruments:fetched_at"


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
# Index options statutory charges (NSE, effective Oct 2024):
DEFAULT_STT_PCT = 0.001                 # STT 0.1% of premium, SELL trades only
DEFAULT_EXCHANGE_CHARGES_PCT = 0.0003503  # NSE txn charge 0.03503% of premium (all trades)
BSE_EXCHANGE_CHARGES_PCT = 0.000325     # BSE (SENSEX/BANKEX) options txn charge 0.0325%
DEFAULT_STAMP_DUTY_PCT = 0.00003        # stamp duty 0.003% of premium, BUY trades only
SEBI_FEE_PER_CRORE = 10.0               # Rs 10 per crore of turnover (all trades)
GST_PCT = 0.18                          # 18% on brokerage + exchange charges + SEBI fee
DEFAULT_CAPITAL = 1_000_000.0           # Rs 10L virtual capital

BSE_SYMBOLS = {"SENSEX", "BANKEX"}


def exchange_charges_pct_for(symbol: str) -> float:
    return BSE_EXCHANGE_CHARGES_PCT if symbol in BSE_SYMBOLS else DEFAULT_EXCHANGE_CHARGES_PCT


def apply_slippage(ltp: float, side: str, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> float:
    """Simulate a realistic fill: sells fill below LTP, buys fill above."""
    factor = slippage_bps / 10000
    if side == "SELL":
        return ltp * (1 - factor)
    return ltp * (1 + factor)


def compute_stt(sell_premium: float, qty: int, stt_pct: float = DEFAULT_STT_PCT) -> float:
    """STT applies only to SELL trades (premium of the option being sold)."""
    return stt_pct * sell_premium * qty


def compute_exchange_charges(premium: float, qty: int,
                              pct: float = DEFAULT_EXCHANGE_CHARGES_PCT) -> float:
    return pct * premium * qty


def compute_stamp_duty(buy_premium: float, qty: int,
                        pct: float = DEFAULT_STAMP_DUTY_PCT) -> float:
    """Stamp duty applies only to BUY trades (premium of the option being bought)."""
    return pct * buy_premium * qty


def compute_brokerage(num_legs: int, per_order: float = DEFAULT_BROKERAGE_PER_ORDER) -> float:
    """Brokerage for one side (entry OR exit) of a multi-leg position."""
    return num_legs * per_order


def compute_transaction_costs(trades: list[tuple[float, str]], qty: int,
                               brokerage_per_order: float = DEFAULT_BROKERAGE_PER_ORDER,
                               stt_pct: float = DEFAULT_STT_PCT,
                               exchange_charges_pct: float = DEFAULT_EXCHANGE_CHARGES_PCT,
                               stamp_duty_pct: float = DEFAULT_STAMP_DUTY_PCT) -> float:
    """Total charges for one batch of orders (all legs of an entry OR an exit).

    trades: [(fill_premium_per_unit, action)] where action is the order
    actually being placed now ("SELL" or "BUY") -- at entry that's the leg's
    side, at exit it's the opposite (shorts are bought back, longs sold).
    STT on SELL actions, stamp duty on BUY actions, exchange + SEBI on all,
    GST on brokerage + exchange + SEBI.
    """
    brokerage = compute_brokerage(len(trades), brokerage_per_order)
    exchange = sebi = stt = stamp = 0.0
    for premium, action in trades:
        exchange += compute_exchange_charges(premium, qty, exchange_charges_pct)
        sebi += premium * qty * SEBI_FEE_PER_CRORE / 1e7
        if action == "SELL":
            stt += compute_stt(premium, qty, stt_pct)
        else:
            stamp += compute_stamp_duty(premium, qty, stamp_duty_pct)
    gst = GST_PCT * (brokerage + exchange + sebi)
    return brokerage + exchange + sebi + stt + stamp + gst


# ── Runtime config overrides (paper:config, set via /paper_config) ─────────
# Only this small, validated subset is honoured; anything else is rejected.
# key -> (type, min, max)
CONFIG_SCHEMA: dict[str, tuple[type, float, float]] = {
    "max_positions": (int, 1, 20),
    "risk_scale": (float, 0.1, 3.0),          # multiplier on per-strategy RISK_PCT
    "stop_loss_multiple": (float, 0.5, 5.0),  # x entry credit
    "profit_target_pct": (float, 0.1, 1.0),   # fraction of entry credit
}


def parse_config_value(key: str, value) -> Optional[float]:
    """Validate a /paper_config value. Returns the typed value or None if invalid."""
    spec = CONFIG_SCHEMA.get(key)
    if spec is None:
        return None
    typ, lo, hi = spec
    try:
        parsed = typ(float(value)) if typ is int else typ(value)
    except (TypeError, ValueError):
        return None
    if not (lo <= parsed <= hi):
        return None
    return parsed


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
    trading_day: str = ""      # ISO date the daily_* counters belong to

    _FLOAT_FIELDS = {
        "capital", "realized_pnl", "unrealized_pnl", "margin_used",
        "available_margin", "day_start_capital", "max_drawdown", "daily_realized_pnl",
    }
    _INT_FIELDS = {"open_positions", "daily_trades", "daily_wins", "daily_losses"}
    _STR_FIELDS = {"trading_day"}

    def roll_day(self, today: str) -> bool:
        """Reset the daily_* counters when the trading day changes. Returns True if reset."""
        if self.trading_day == today:
            return False
        self.trading_day = today
        self.daily_realized_pnl = 0.0
        self.daily_trades = 0
        self.daily_wins = 0
        self.daily_losses = 0
        self.day_start_capital = self.capital + self.realized_pnl
        return True

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
            elif k in cls._STR_FIELDS:
                kwargs[k] = str(v)
        return cls(**kwargs)
