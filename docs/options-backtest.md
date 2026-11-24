# Options backtester (M2)

Replay a captured trading day through the **real** analysers and paper-trading
rules, and get a per-signal scoreboard: which signals fired, which were blocked
and why, what the trade did at +30/+60 min, and how it exited.

This is the measurement gate the programme requires before any strategy work
(`docs/testing-strategy.md`, `.kilo/plans/options-programme-plan.md` M2).

## Usage

```bash
# what has been captured
make backtest ARGS="--list"

# replay the latest day for every captured symbol
make backtest

# one date, chosen symbols, write the full report
make backtest ARGS="--date 2026-11-25 --symbols NIFTY,BANKNIFTY --json /tmp/report.json"

# replay only specific (scripted) signals, e.g. to reproduce a known trade
make backtest ARGS="--date 2026-11-25 --signals /tmp/signals.json"
```

`signals.json`:

```json
[{"at": "10:05", "symbol": "NIFTY", "strategy": "IRON_CONDOR",
  "put_wall_strike": 25350, "call_wall_strike": 25650}]
```

Output:

```
strategy                 signals entries  win%    net P&L   avg MFE   avg MAE  exits / blocks
------------------------------------------------------------------------------------------------
RANGE_BOUND_SETUP              6       4    50%       3120      1850     -2850  TARGET:3, STOP_LOSS:1
GAMMA_TRAP                     2       0     0%          0         -         -  blocked stale_options_agg:2
```

## How it works

```
chain/context Parquet ──▶ loader ──▶ ReplayContext (Redis-shaped hashes)
                                        │
                     process_job() ─────┤ real analysers, real serializer
                                        ▼
                          parse_analysis_result() ──▶ EntrySignal
                                        ▼
   check_entry_gate → check_entry_filters → build_position   (all production code)
                                        ▼
        evaluate_position every snapshot → determine_exit_reason / close_position
```

- The clock is moved to each captured snapshot (`time_machine`), so staleness
  gates and DTE logic behave exactly as they did live.
- `ReplayRedis` implements only the commands the engine uses and raises on
  anything else, so a future engine change that reads a new key fails loudly.
- P&L, costs and exit decisions come from the same functions as paper trading —
  the backtest cannot drift from the live rules.

## What is captured, and what is not

The recorder stores every subscribed strike (the same universe the live
analysers saw), so replay sees the same chain. It does **not** store:

- SPAN margin — the backtest uses a fixed margin per lot (`--margin`, default
  ₹60 000). Sizing therefore matches paper only when SPAN is near that value.
- Real fills/slippage — the same flat slippage model as paper is applied.
- Sensibull's full daily history — the replay rebuilds the intraday context
  (per-expiry stats, OI chain, ATM IV series) from the captured snapshots.

## Validation

`tests/tools/test_options_backtest.py`:

1. **Live vs replay** — a day recorded by the E2E harness is replayed and must
   produce the same exit reason and P&L within ₹1.
2. **Behaviour** — synthetic days prove a target exit, a pre-window block and a
   clean failure on missing capture.
3. **CLI** — `--list`, table output and JSON report.

## Reading the report

- **signals vs entries** — the gap is your rejection rate; `blocked` shows the
  exact gate (`outside_entry_window`, `stale_options_agg`, `no_position`,
  `cooldown_active`, …).
- **win %** — decided trades only (excludes still-open at end of day).
- **net P&L** — realised; open positions are marked `OPEN_AT_END` with their
  last unrealised P&L.
- **avg MFE / MAE** — best and worst unrealised P&L while the trade was open;
  use MAE to judge whether stops are too tight.
