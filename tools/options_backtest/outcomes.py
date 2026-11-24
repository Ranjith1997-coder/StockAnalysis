"""Per-signal records and the hit-rate summary the M2 gate asks for."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SignalOutcome:
    ts: datetime
    symbol: str
    strategy: str
    source: str
    status: str                     # "entered" | "blocked"
    block_reason: str = ""
    entry_credit: float | None = None
    max_loss: float | None = None
    mfe: float | None = None
    mae: float | None = None
    pnl_30m: float | None = None
    pnl_60m: float | None = None
    exit_reason: str = ""
    pnl: float | None = None
    exit_ts: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat(),
            "symbol": self.symbol,
            "strategy": self.strategy,
            "source": self.source,
            "status": self.status,
            "block_reason": self.block_reason,
            "entry_credit": _round(self.entry_credit),
            "max_loss": _round(self.max_loss),
            "mfe": _round(self.mfe),
            "mae": _round(self.mae),
            "pnl_30m": _round(self.pnl_30m),
            "pnl_60m": _round(self.pnl_60m),
            "exit_reason": self.exit_reason,
            "pnl": _round(self.pnl),
            "exit_ts": self.exit_ts.isoformat() if self.exit_ts else None,
        }


@dataclass
class StrategyRow:
    strategy: str
    signals: int = 0
    entries: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
    mfe_sum: float = 0.0
    mfe_n: int = 0
    mae_sum: float = 0.0
    mae_n: int = 0
    exits: Counter = field(default_factory=Counter)
    blocks: Counter = field(default_factory=Counter)

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return self.wins / decided if decided else 0.0

    @property
    def avg_mfe(self) -> float | None:
        return self.mfe_sum / self.mfe_n if self.mfe_n else None

    @property
    def avg_mae(self) -> float | None:
        return self.mae_sum / self.mae_n if self.mae_n else None


def summarize(outcomes: list[SignalOutcome]) -> list[StrategyRow]:
    rows: dict[str, StrategyRow] = {}
    for outcome in outcomes:
        row = rows.setdefault(outcome.strategy, StrategyRow(outcome.strategy))
        row.signals += 1
        if outcome.status == "blocked":
            row.blocks[outcome.block_reason or "unknown"] += 1
            continue
        row.entries += 1
        if outcome.pnl is not None:
            row.net_pnl += outcome.pnl
            if outcome.pnl > 0:
                row.wins += 1
            elif outcome.pnl < 0:
                row.losses += 1
        if outcome.mfe is not None:
            row.mfe_sum += outcome.mfe
            row.mfe_n += 1
        if outcome.mae is not None:
            row.mae_sum += outcome.mae
            row.mae_n += 1
        if outcome.exit_reason:
            row.exits[outcome.exit_reason] += 1
    return sorted(rows.values(), key=lambda r: (-r.entries, r.strategy))


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)
