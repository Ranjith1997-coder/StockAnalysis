#!/usr/bin/env python3
"""Post-deploy smoke check — run after `make deploy` and before trusting the day.

Runs read-only checks on the production server over SSH. Nothing here mutates
server state. Exit code 0 = all checks passed, 1 = at least one FAIL.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/smoke_check.py
    make smoke

SKIP_REASON=outside-hours make smoke   # suppress market-hours-only checks

Checks (see docs/testing-strategy.md T6):
    services        every unit active
    heartbeats      registry present, status != shutdown, heartbeat age < TTL
    options_agg     fresh within 10 s during market hours (per live index)
    ws2_subs        <= WS_MAX_OPTION_SUBSCRIPTIONS (the 2026-11-23 incident)
    capture         today's manifest exists and files > 0
    errors          no Traceback in the last 5 minutes of any service journal
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone

from dotenv import load_dotenv

load_dotenv(override=True)

SERVER = os.getenv("DEPLOY_SERVER", "hacker@100.92.21.31")
APP_DIR = os.getenv("DEPLOY_APP_DIR", "~/StockAnalysis")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

UNITS = [
    "stockanalysis-auth",
    "stockanalysis-notification",
    "stockanalysis-data-gateway",
    "stockanalysis-market-data",
    "stockanalysis-analysis-engine",
    "stockanalysis-volatility-engine",
    "stockanalysis-signal-intelligence",
    "stockanalysis-resource-monitor",
    "stockanalysis-paper-trading",
    "stockanalysis-pinn-trainer",
    "stockanalysis",
]

LIVE_INDICES = ("NIFTY", "BANKNIFTY", "SENSEX")
WS_LIMIT = 480
MAX_HEARTBEAT_AGE = 120        # seconds; a writer beating every 60 s must be well inside
CLOCK_SKEW_TOLERANCE = 15      # laptop vs server NTP drift, seconds
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def ssh(command: str, timeout: int = 60) -> tuple[int, str]:
    import subprocess

    try:
        result = subprocess.run(
            ["ssh", *SSH_OPTS, SERVER, command],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"ssh timed out after {timeout}s"
    except OSError as exc:
        return 1, f"ssh failed: {exc}"


def redis_cli(*args: str) -> str:
    quoted = " ".join(f"'{a}'" for a in args)
    _, out = ssh(f"redis-cli {quoted}")
    return out.strip()


def _now_ist() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def in_market_hours(now: datetime) -> bool:
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def check_services() -> list[Check]:
    _, out = ssh("systemctl is-active " + " ".join(UNITS), timeout=60)
    states = out.split()
    if len(states) != len(UNITS):
        return [Check("services", False, f"unexpected output: {out!r}")]
    checks = []
    for unit, state in zip(UNITS, states):
        checks.append(Check(
            f"service:{unit}", state == "active",
            state if state != "active" else "active",
        ))
    return checks


def check_heartbeats() -> list[Check]:
    key = "service:registry"
    _, keys_out = ssh(f"redis-cli --scan --pattern '{key}:*'", timeout=60)
    keys = [k for k in keys_out.split() if k]
    checks: list[Check] = []
    now = _now_ist().timestamp()
    for registry_key in sorted(keys):
        raw = redis_cli("HGETALL", registry_key)
        mapping = dict(zip(raw.splitlines()[0::2], raw.splitlines()[1::2]))
        if not mapping:
            continue
        name = registry_key.rsplit(":", 1)[-1]
        try:
            age = now - float(mapping.get("last_heartbeat", 0))
        except ValueError:
            checks.append(Check(f"heartbeat:{name}", False, "unparseable last_heartbeat"))
            continue
        ttl_raw = redis_cli("TTL", registry_key)
        try:
            ttl = int(ttl_raw)
        except ValueError:
            ttl = -1
        status = mapping.get("status", "unknown")
        if status == "shutdown":
            # A restart writes "shutdown" from the outgoing process before the
            # new one overwrites it; re-read once before calling it a failure.
            time.sleep(5)
            refreshed = redis_cli("HGET", registry_key, "status")
            status = refreshed.strip() or status
        # Redis returns *remaining* TTL, so "age < ttl" would be meaningless;
        # the meaningful invariants are: the key has not expired and the last
        # beat is recent. Negative age is NTP skew between laptop and server.
        ok = (
            status != "shutdown"
            and ttl > 0
            and -CLOCK_SKEW_TOLERANCE <= age < MAX_HEARTBEAT_AGE
        )
        detail = f"status={status} age={age:.0f}s ttl={ttl}s"
        if age < -CLOCK_SKEW_TOLERANCE:
            detail += " (clock skew > tolerance)"
        elif ttl <= 0:
            detail += " (key has no TTL)"
        checks.append(Check(f"heartbeat:{name}", ok, detail))
    return checks


def check_options_fresh() -> list[Check]:
    if not in_market_hours(_now_ist()):
        return [Check("options_agg", True, "skipped (outside market hours)")]
    now = _now_ist().timestamp()
    checks = []
    for symbol in LIVE_INDICES:
        raw = redis_cli("HGET", f"data:options_agg:{symbol}", "last_updated")
        try:
            age = now - float(raw)
        except ValueError:
            checks.append(Check(f"options_agg:{symbol}", False, "missing last_updated"))
            continue
        checks.append(Check(
            f"options_agg:{symbol}", age < 10,
            f"age={age:.1f}s (limit 10s)",
        ))
    return checks


def check_ws2_subs() -> list[Check]:
    raw = redis_cli("HGET", "service:registry:market-data", "ws2_subs")
    try:
        subs = int(raw)
    except ValueError:
        return [Check("ws2_subs", False, f"unreadable ws2_subs {raw!r}")]
    return [Check(
        "ws2_subs", subs <= WS_LIMIT,
        f"{subs} subscribed (limit {WS_LIMIT}); a value above 500 is rejected by Kite",
    )]


def _unit_start_epoch(unit: str) -> float | None:
    """Wall-clock epoch when systemd last started ``unit`` (None if unknown)."""
    code, out = ssh(
        f'ts=$(systemctl show -p ActiveEnterTimestamp --value {unit}); '
        '[ -z "$ts" ] && exit 3; date -d "$ts" +%s',
        timeout=30,
    )
    try:
        return float(out.strip().splitlines()[-1]) if code == 0 else None
    except ValueError:
        return None


def check_capture() -> list[Check]:
    today = _now_ist().strftime("%Y-%m-%d")
    cmd = (
        f"cd {APP_DIR} && f=data/options_history/eod/{today}/manifest.json; "
        'if [ -f "$f" ]; then cat "$f"; else echo MISSING; fi'
    )
    _, out = ssh(cmd, timeout=60)
    if out.strip() == "MISSING" or not out.startswith("{"):
        # A deploy after the close cannot have captured anything yet. Accept it
        # only when market-data was in fact started after today's close.
        now = _now_ist()
        started = _unit_start_epoch("stockanalysis-market-data")
        close = now.replace(
            hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0
        )
        if started is not None and now > close and started > close.timestamp():
            return [Check(
                "capture", True,
                f"deployed after close at {now.strftime('%H:%M')} — "
                "first manifest expected next session",
            )]
        return [Check("capture", False, f"no manifest for {today}")]
    files = _json_field(out, "files")
    rows = out.count('"chain:')
    return [Check(
        "capture", bool(files),
        f"manifest {today}: files={files} chain_keys={rows}",
    )]


def _json_field(payload: str, field: str) -> int:
    match = re.search(rf'"{field}"\s*:\s*(\d+)', payload)
    return int(match.group(1)) if match else 0


def check_recent_errors() -> list[Check]:
    """Count tracebacks since each unit last started, capped at 5 minutes.

    A deploy restarts every unit; scanning a fixed 5-minute window would blame
    the new build for shutdown noise from the old process.
    """
    # Filter by the unit's *current* MainPID: on a restart the outgoing process
    # logs its shutdown traceback in the same second the new one becomes active,
    # so a time window alone misattributes it to the new build.
    loop = (
        'for u in ' + " ".join(UNITS) + '; do '
        'ts=$(systemctl show -p ActiveEnterTimestamp --value "$u"); '
        'if [ -n "$ts" ]; then since="$ts"; else since="5 minutes ago"; fi; '
        'pid=$(systemctl show -p MainPID --value "$u"); '
        'n=$(journalctl --since "$since" --no-pager -o short -u "$u" 2>/dev/null '
        "| grep -F \"python[$pid]\" | grep -c Traceback); "
        'echo "$u ${n:-0}"; done'
    )
    _, out = ssh(loop, timeout=120)
    checks: list[Check] = []
    total = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        unit, count = parts[0], int(parts[1])
        total += count
        checks.append(Check(f"errors:{unit.removeprefix('stockanalysis-')}",
                            count == 0, f"{count} Traceback(s) since start"))
    if not checks:
        return [Check("errors", False, f"unreadable journal output {out!r}")]
    return checks if total else [Check("errors", True, "no Traceback since start")]


def run() -> list[Check]:
    code, out = ssh("echo ok", timeout=20)
    if code != 0:
        return [Check("connectivity", False, f"cannot SSH to {SERVER}: {out}")]
    checks = [Check("connectivity", True, SERVER)]
    for group in (
        check_services,
        check_heartbeats,
        check_options_fresh,
        check_ws2_subs,
        check_capture,
        check_recent_errors,
    ):
        checks.extend(group())
    return checks


def main() -> int:
    checks = run()
    width = max(len(c.name) for c in checks) + 2
    for check in checks:
        mark = "PASS" if check.ok else "FAIL"
        print(f"{mark}  {check.name:<{width}} {check.detail}")
    failed = [c for c in checks if not c.ok]
    print()
    if failed:
        print(f"{len(failed)} check(s) FAILED")
        return 1
    print(f"all {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
