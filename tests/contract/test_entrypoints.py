"""Contract tests: services must fail cleanly without Redis, and every
heartbeat writer must actually publish a valid registry entry (T1.4).

Why: the orchestrator heartbeat called ``time.time()`` although ``time`` was
``datetime.time`` — the ``TypeError`` was swallowed and ``/version`` silently
lost the service for months. Running each writer once against fakeredis turns
that class of bug into a test failure.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from typing import NamedTuple

import pytest

from tests.contract.schema import REGISTRY_REQUIRED, registry_problem
from tests.contract.unit_files import REPO_ROOT, load_units

pytestmark = pytest.mark.contract

UNITS = load_units()

# Registry writers that take only (redis[, ...]) and are safe to call once.
# market-data's writer needs a live ticker manager and notification-service's is
# inline in main(); both are asserted statically below instead.
#
# ``full=False`` marks a liveness-only refresh (pid + last_heartbeat); the full
# name/status/version mapping is written by the service loop.
class Writer(NamedTuple):
    module: str
    attr: str
    args: tuple
    full: bool = True
    key: str | None = None


WRITERS: dict[str, Writer] = {
    "auth-service": Writer("services.auth_service.main", "_update_heartbeat", ()),
    "analysis-engine": Writer(
        "services.analysis_engine.main", "_update_heartbeat", ("t1",),
        key="service:registry:analysis-engine:t1",
    ),
    "data-gateway": Writer(
        "services.data_gateway.main", "_touch_heartbeat", (), full=False,
    ),
    "orchestrator": Writer("services.orchestrator.main", "_write_orchestrator_heartbeat", ()),
    "paper-trading": Writer("services.paper_trading.main", "update_heartbeat", ()),
    "pinn-trainer": Writer("services.pinn_trainer.main", "update_heartbeat", ()),
    "resource-monitor": Writer("services.resource_monitor.main", "write_heartbeat", ("raw",)),
    "signal-intelligence": Writer("services.signal_intelligence.main", "_update_heartbeat", (0,)),
    "volatility-engine": Writer("services.volatility_engine.main", "update_heartbeat", ()),
}


def _entry_command(unit) -> list[str]:
    if unit.uses_module:
        return ["-m", unit.module, *unit.args]
    assert unit.script is not None
    return [str(unit.script), *unit.args]


@pytest.mark.parametrize("unit", UNITS, ids=lambda u: u.name)
def test_service_fails_cleanly_without_redis(unit) -> None:
    """A dead Redis must produce exit 1 and a clear message, not a silent hang."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["REDIS_URL"] = "redis://127.0.0.1:1"  # reserved port, nothing listens

    proc = subprocess.Popen(
        [sys.executable, *_entry_command(unit)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = proc.communicate(timeout=45)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        output, _ = proc.communicate()
        timed_out = True

    assert not timed_out, (
        f"{unit.name}: still running after 45 s with Redis unreachable "
        "(startup must not block forever)"
    )
    assert proc.returncode == 1, (
        f"{unit.name}: expected exit code 1 on Redis failure, got "
        f"{proc.returncode}\n{output[-3000:]}"
    )
    assert "cannot connect to redis" in output.lower(), (
        f"{unit.name}: no clear 'Cannot connect to Redis' message; operators see "
        f"only this:\n{output[-3000:]}"
    )
    for broken in ("AttributeError", "NameError", "ModuleNotFoundError"):
        assert broken not in output, (
            f"{unit.name}: startup crashed with {broken} before reporting the "
            f"Redis failure (wiring bug, not an outage):\n{output[-3000:]}"
        )


@pytest.mark.parametrize("service", sorted(WRITERS), ids=sorted(WRITERS))
def test_heartbeat_writer_publishes_valid_registry_entry(service, fake_redis) -> None:
    writer = WRITERS[service]
    func = getattr(importlib.import_module(writer.module), writer.attr)
    call_args = (fake_redis._client,) if writer.args == ("raw",) else (
        fake_redis, *writer.args
    )

    func(*call_args)

    key = writer.key or f"service:registry:{service}"
    assert key in _keys(fake_redis), (
        f"{service}: {writer.module}.{writer.attr}() wrote no {key} — the writer "
        "raised and its except-swallow hid it"
    )
    mapping = fake_redis.hgetall(key)
    assert float(mapping["last_heartbeat"]) > 0
    assert 0 < fake_redis._client.ttl(key), f"{service}: heartbeat has no TTL"
    if writer.full:
        assert not registry_problem(mapping), registry_problem(mapping)
        assert mapping["name"] in {service, service.replace("-", "_")}
    else:
        assert {"pid", "last_heartbeat"} <= set(mapping)


@pytest.mark.parametrize(
    "service,path",
    [
        ("market-data", "services/market_data/main.py"),
        ("notification-service", "services/notification-service/main.py"),
    ],
)
def test_inline_registry_writers_reference_the_key(service, path) -> None:
    """Writers we cannot call in isolation must at least target the right key."""
    source = (REPO_ROOT / path).read_text()
    assert f'"service:registry:{service}"' in source


def _keys(fake_redis) -> set[str]:
    return {k for k in fake_redis._client.scan_iter("service:registry:*")}


def test_registry_required_fields_are_documented() -> None:
    assert REGISTRY_REQUIRED <= {"name", "pid", "status", "last_heartbeat", "version"}
