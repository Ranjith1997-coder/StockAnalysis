"""Oracles for the E2E simulator (T5.7).

Each oracle takes a ``RunResult`` and returns ``(ok, detail)``. They read only
public artefacts (Redis hashes, the trade stream, the capture manifest, logs), so
they cannot silently pass on an internal shortcut. Every oracle is exercised by a
deliberately-failing test in ``tests/e2e/test_oracles.py``.
"""
from __future__ import annotations

from datetime import time as dtime

from services.paper_trading.engine import compute_max_loss
from services.paper_trading.models import PaperPosition

from .result import RunResult

MARKET_ENTRY_START = dtime(9, 20)
MARKET_ENTRY_END = dtime(15, 0)
EXPIRY_ENTRY_CUTOFF = dtime(14, 0)
MAX_OPEN_POSITIONS = 6
MAX_POSITIONS_PER_INDEX = 2
MAX_OPEN_LOSS_PCT = 0.05
TOLERANCE = 1.0


def no_errors(r: RunResult) -> tuple[bool, str]:
    problems = [f"{rec['name']}: {rec['msg']}" for rec in r.errors_in_logs()]
    problems += r.errors
    if problems:
        return False, " | ".join(problems[:5])
    return True, "no errors"


def subs_within_cap(r: RunResult) -> tuple[bool, str]:
    if r.subscription_cap and r.subscription_max > r.subscription_cap:
        return False, f"max subscriptions {r.subscription_max} > cap {r.subscription_cap}"
    return True, f"max {r.subscription_max}/{r.subscription_cap}"


def chain_follows_spot(r: RunResult) -> tuple[bool, str]:
    if r.atm_misses:
        return False, f"ATM unsubscribed at {len(r.atm_misses)} samples (first {r.atm_misses[0]}s)"
    return True, "ATM always subscribed"


def capture_coverage(r: RunResult) -> tuple[bool, str]:
    rows = r.capture.get("rows", {})
    files = r.capture.get("files", 0)
    if not any(r.capture_expected.values()):
        return True, "no capture expected (non-trading day)"
    if not files:
        return False, "no capture files written"
    problems = []
    for key, expected in r.capture_expected.items():
        actual = rows.get(key, 0)
        if expected and actual < expected * 0.99:
            problems.append(f"{key}: {actual}/{expected} rows")
    if problems:
        return False, "; ".join(problems)
    return True, f"{files} files, rows {rows}"


def entry_gates(r: RunResult) -> tuple[bool, str]:
    for attempt in r.signal_attempts:
        if attempt["expected"] != attempt["opened"]:
            return False, (
                f"{attempt['symbol']} signal at {attempt['time']}: expected "
                f"opened={attempt['expected']}, got {attempt['opened']}"
            )
    for entry in r.entries:
        at = entry["time"]
        if not (MARKET_ENTRY_START <= at <= MARKET_ENTRY_END):
            return False, f"{entry['symbol']} entered at {at} outside {MARKET_ENTRY_START}-{MARKET_ENTRY_END}"
        expiry = r.scenario.expiry.get(entry["symbol"])
        if expiry == r.scenario.date and at > EXPIRY_ENTRY_CUTOFF:
            return False, f"{entry['symbol']} entered at {at} after expiry-day cutoff"
    return True, f"{len(r.entries)} entries inside the window"


def legs_priced_from_market(r: RunResult) -> tuple[bool, str]:
    if r.pricing_issues:
        first = r.pricing_issues[0]
        return False, (
            f"{len(r.pricing_issues)} leg pricing issue(s); first: "
            f"{first['leg']} {first['issue']}"
        )
    return True, "all open legs priced from the live chain"


def exits_valid(r: RunResult) -> tuple[bool, str]:
    seen = set()
    for trade in r.trades:
        if not trade.get("exit_reason"):
            return False, f"{trade['position_id']} closed without a reason"
        if trade["position_id"] in seen:
            return False, f"{trade['position_id']} closed twice"
        seen.add(trade["position_id"])
    open_ids = {p["position_id"] for p in r.open_positions}
    if seen & open_ids:
        return False, f"position(s) both closed and open: {sorted(seen & open_ids)}"
    if len(r.trades) < r.scenario.expect_exits:
        return False, (
            f"{len(r.trades)} exits < expected {r.scenario.expect_exits}"
        )
    expected = r.scenario.expect_exit_reason
    if expected:
        wrong = [t["exit_reason"] for t in r.trades if t["exit_reason"] != expected]
        if wrong:
            return False, f"expected {expected}, got {wrong}"
    return True, f"{len(r.trades)} closes valid"


def pnl_reconciles(r: RunResult) -> tuple[bool, str]:
    trade_sum = sum(float(t["pnl"]) for t in r.trades)
    realized = float(r.account.get("realized_pnl", 0.0))
    daily = float(r.daily_pnl.get("realized", 0.0))
    if abs(trade_sum - realized) > TOLERANCE:
        return False, f"trade pnl {trade_sum:.2f} != account {realized:.2f}"
    if abs(trade_sum - daily) > TOLERANCE:
        return False, f"trade pnl {trade_sum:.2f} != daily hash {daily:.2f}"
    if int(r.daily_pnl.get("trades_count", 0)) != len(r.trades):
        return False, f"daily_trades {r.daily_pnl.get('trades_count')} != {len(r.trades)}"
    return True, f"realised {trade_sum:.2f} reconciles"


def risk_limits(r: RunResult) -> tuple[bool, str]:
    capital = float(r.account.get("capital", 1.0)) or 1.0
    limit = capital * MAX_OPEN_LOSS_PCT
    per_symbol: dict[str, int] = {}
    total_loss = 0.0
    for raw in r.open_positions:
        position = PaperPosition.from_json(raw["json"])
        per_symbol[position.symbol] = per_symbol.get(position.symbol, 0) + 1
        max_loss = compute_max_loss(position)
        if max_loss is not None:
            total_loss += max_loss
    if len(r.open_positions) > MAX_OPEN_POSITIONS:
        return False, f"{len(r.open_positions)} open > {MAX_OPEN_POSITIONS}"
    for symbol, count in per_symbol.items():
        if count > MAX_POSITIONS_PER_INDEX:
            return False, f"{symbol}: {count} positions > {MAX_POSITIONS_PER_INDEX}"
    if total_loss > limit + TOLERANCE:
        return False, f"open max loss {total_loss:.0f} > {MAX_OPEN_LOSS_PCT:.0%} of capital"
    return True, f"open max loss {total_loss:.0f}/{limit:.0f}"


def heartbeats_present(r: RunResult) -> tuple[bool, str]:
    if not r.heartbeat_keys:
        return False, "no heartbeats written"
    if r.heartbeat_violations:
        return False, "; ".join(r.heartbeat_violations[:3])
    return True, f"{len(r.heartbeat_keys)} heartbeat keys within TTL"


def analysis_ran(r: RunResult) -> tuple[bool, str]:
    if not r.analysis_results:
        return False, "no analysis results at all"
    errors = [
        f"{res.get('symbol')}: {res.get('error')}"
        for res in r.analysis_results
        if res.get("result") == "ERROR"
    ]
    if errors:
        return False, " | ".join(errors[:3])
    symbols = {res.get("symbol") for res in r.analysis_results if res.get("result") == "SUCCESS"}
    missing = set(r.scenario.symbols) - symbols
    if missing:
        return False, f"no successful analysis for {sorted(missing)}"
    return True, f"{len(r.analysis_results)} results"


ORACLES = {
    "no_errors": no_errors,
    "subs_within_cap": subs_within_cap,
    "chain_follows_spot": chain_follows_spot,
    "capture_coverage": capture_coverage,
    "entry_gates": entry_gates,
    "exits_valid": exits_valid,
    "legs_priced_from_market": legs_priced_from_market,
    "pnl_reconciles": pnl_reconciles,
    "risk_limits": risk_limits,
    "heartbeats_present": heartbeats_present,
    "analysis_ran": analysis_ran,
}


def evaluate(names, result: RunResult) -> list[tuple[str, bool, str]]:
    return [(name, *ORACLES[name](result)) for name in names]
