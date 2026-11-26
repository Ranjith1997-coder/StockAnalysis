"""Read a captured trading day from the chain recorder's Parquet tree.

Layout written by ``services/market_data/chain_recorder.py``::

    <root>/chain/<SYMBOL>/<DATE>/<HHMM>.parquet
    <root>/context/<SYMBOL>/<DATE>/<HHMM>.parquet
    <root>/futures/<SYMBOL>/<DATE>/<HHMM>.parquet
    <root>/eod/<DATE>/manifest.json
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from services.market_data.chain_recorder import (
    CHAIN_COLUMNS,
    CONTEXT_COLUMNS,
    FUTURES_COLUMNS,
)

# Columns added after capture went live (Block 2 vol context). Older days must
# still load; they simply lack those values.
OPTIONAL_CONTEXT_COLUMNS = (
    "atm_iv_percentile", "atm_iv_near", "atm_iv_next", "next_expiry", "term_slope",
)
REQUIRED_CONTEXT_COLUMNS = [c for c in CONTEXT_COLUMNS if c not in OPTIONAL_CONTEXT_COLUMNS]


class MissingCaptureError(RuntimeError):
    """No files for the requested symbol/date (or an incomplete schema)."""


@dataclass(frozen=True)
class DayData:
    root: Path
    date: str
    symbol: str
    chain: pd.DataFrame
    context: pd.DataFrame
    futures: pd.DataFrame

    @property
    def snapshot_ids(self) -> list[str]:
        return list(dict.fromkeys(self.chain["snapshot_id"].tolist()))

    def snapshots(self) -> list["Snapshot"]:
        ctx_by_id = {row["snapshot_id"]: row for _, row in self.context.iterrows()}
        out: list[Snapshot] = []
        for snapshot_id, frame in self.chain.groupby("snapshot_id", sort=False):
            ctx = ctx_by_id.get(snapshot_id)
            if ctx is None:
                continue
            out.append(Snapshot(
                snapshot_id=snapshot_id,
                ts=pd.Timestamp(frame["ts"].iloc[0]).to_pydatetime(),
                spot=float(ctx["spot"] or 0.0),
                frame=frame.sort_values(["strike", "side"]),
                ctx=dict(ctx),
            ))
        out.sort(key=lambda snap: snap.ts)
        return out


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    ts: object                     # datetime
    spot: float
    frame: pd.DataFrame            # CHAIN_COLUMNS rows for this snapshot
    ctx: dict                      # CONTEXT_COLUMNS row

    @property
    def expiry(self) -> str:
        return str(self.ctx.get("expiry") or "")[:10]

    @property
    def atm_strike(self) -> float | None:
        value = self.ctx.get("atm_strike")
        return float(value) if value not in (None, "") else None


def _read_parquet_tree(folder: Path, required: list[str], label: str,
                        optional: tuple[str, ...] = ()) -> pd.DataFrame:
    files = sorted(folder.rglob("*.parquet"))
    if not files:
        raise MissingCaptureError(f"no {label} parquet under {folder}")
    frame = pd.concat((pd.read_parquet(path) for path in files), ignore_index=True)
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise MissingCaptureError(
            f"{label} capture is missing columns {missing} (schema drift?)"
        )
    for column in optional:
        if column not in frame.columns:
            frame[column] = None
    return frame


def load_day(root: Path | str, date: str, symbol: str) -> DayData:
    root = Path(root)
    chain = _read_parquet_tree(root / "chain" / symbol / date, CHAIN_COLUMNS, "chain")
    context = _read_parquet_tree(
        root / "context" / symbol / date,
        REQUIRED_CONTEXT_COLUMNS,
        "context",
        optional=OPTIONAL_CONTEXT_COLUMNS,
    )
    futures_path = root / "futures" / symbol / date
    if futures_path.exists():
        futures = _read_parquet_tree(futures_path, FUTURES_COLUMNS, "futures")
    else:
        futures = pd.DataFrame(columns=FUTURES_COLUMNS)
    return DayData(root=root, date=date, symbol=symbol, chain=chain,
                   context=context, futures=futures)


def available_days(root: Path | str) -> list[tuple[str, str]]:
    """(symbol, date) pairs present under the capture root."""
    root = Path(root)
    out: list[tuple[str, str]] = []
    chain_root = root / "chain"
    if not chain_root.exists():
        return out
    for symbol_dir in sorted(chain_root.iterdir()):
        if not symbol_dir.is_dir():
            continue
        for date_dir in sorted(symbol_dir.iterdir()):
            if date_dir.is_dir() and any(date_dir.glob("*.parquet")):
                out.append((symbol_dir.name, date_dir.name))
    return out
