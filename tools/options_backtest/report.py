"""Render the hit-rate table and JSON report."""
from __future__ import annotations

import json
from pathlib import Path

from .outcomes import StrategyRow


def format_table(rows: list[StrategyRow]) -> str:
    if not rows:
        return "no signals"
    header = (
        f"{'strategy':24} {'signals':>7} {'entries':>7} {'win%':>6} "
        f"{'net P&L':>10} {'avg MFE':>9} {'avg MAE':>9}  exits / blocks"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        mfe = f"{row.avg_mfe:,.0f}" if row.avg_mfe is not None else "-"
        mae = f"{row.avg_mae:,.0f}" if row.avg_mae is not None else "-"
        detail = ", ".join(f"{k}:{v}" for k, v in row.exits.most_common()) or "-"
        if row.blocks:
            detail += " | blocked " + ", ".join(
                f"{k}:{v}" for k, v in row.blocks.most_common()
            )
        lines.append(
            f"{row.strategy:24} {row.signals:>7} {row.entries:>7} "
            f"{row.win_rate * 100:>5.0f}% {row.net_pnl:>10,.0f} "
            f"{mfe:>9} {mae:>9}  {detail}"
        )
    return "\n".join(lines)


def write_json(path: Path, *, date: str, result, rows: list[StrategyRow]) -> None:
    payload = {
        "date": date,
        "analysis_cycles": result.analysis_cycles,
        "analysis_failures": result.analysis_failures,
        "summary": [
            {
                "strategy": row.strategy,
                "signals": row.signals,
                "entries": row.entries,
                "wins": row.wins,
                "losses": row.losses,
                "win_rate": round(row.win_rate, 4),
                "net_pnl": round(row.net_pnl, 2),
                "avg_mfe": row.avg_mfe,
                "avg_mae": row.avg_mae,
                "exits": dict(row.exits),
                "blocks": dict(row.blocks),
            }
            for row in rows
        ],
        "signals": [outcome.to_dict() for outcome in result.outcomes],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
