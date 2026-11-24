"""CLI: replay a captured day and print signal outcomes.

    python -m tools.options_backtest --date 2026-11-25 --symbols NIFTY,BANKNIFTY

Exit code 1 when the day is missing or every analysis cycle failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .loader import MissingCaptureError, available_days
from .outcomes import summarize
from .report import format_table, write_json
from .simulate import BacktestConfig, Backtester

DEFAULT_ROOT = Path("data/options_history")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="capture root")
    parser.add_argument("--date", help="YYYY-MM-DD (default: latest available)")
    parser.add_argument("--symbols", help="comma separated (default: every symbol on that date)")
    parser.add_argument("--signals", help="JSON file with scripted signals (optional)")
    parser.add_argument("--json", dest="json_path", help="write the full report here")
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--margin", type=float, default=60_000.0,
                        help="fixed SPAN margin per lot (external API is not used)")
    parser.add_argument("--list", action="store_true", help="list captured days and exit")
    args = parser.parse_args(argv)

    root = Path(args.root)
    days = available_days(root)
    if args.list:
        for symbol, date in days:
            print(f"{date}  {symbol}")
        return 0 if days else 1

    if not days:
        print(f"no capture under {root} — nothing to replay", file=sys.stderr)
        return 1

    date = args.date or max(d for _symbol, d in days)
    on_date = sorted({symbol for symbol, day_date in days if day_date == date})
    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else on_date)
    missing = [s for s in symbols if s not in on_date]
    if missing:
        print(f"no capture for {missing} on {date}", file=sys.stderr)
        return 1

    signals = ()
    if args.signals:
        signals = tuple(json.loads(Path(args.signals).read_text()))

    config = BacktestConfig(
        root=root, date=date, symbols=tuple(symbols), signals=signals,
        capital=args.capital, margin_per_lot=args.margin,
    )
    try:
        result = Backtester(config).run()
    except MissingCaptureError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    rows = summarize(result.outcomes)
    print(f"\n{date}  symbols={','.join(symbols)}  "
          f"analysis_cycles={result.analysis_cycles} "
          f"failures={result.analysis_failures}\n")
    print(format_table(rows))

    if args.json_path:
        write_json(Path(args.json_path), date=date, result=result, rows=rows)
        print(f"\nreport: {args.json_path}")

    if result.analysis_cycles == 0:
        print("every analysis cycle failed — see the warnings above", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
