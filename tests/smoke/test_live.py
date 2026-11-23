"""Post-deploy smoke tests against the live server (T6).

Opt-in only: these need SSH access to production and never run in CI.
Enable with ``RUN_SMOKE=1`` or just run ``make smoke`` (scripts/smoke_check.py).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

RUN = os.getenv("RUN_SMOKE") == "1"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke_check.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("smoke_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def smoke():
    if not RUN:
        pytest.skip("live-server smoke tests: set RUN_SMOKE=1 (or run `make smoke`)")
    return _load_smoke_module()


def test_every_service_is_active(smoke) -> None:
    checks = smoke.check_services()
    failed = [c for c in checks if not c.ok]
    assert not failed, failed


def test_every_service_has_a_fresh_heartbeat(smoke) -> None:
    checks = smoke.check_heartbeats()
    assert checks, "no service:registry:* keys found"
    failed = [c for c in checks if not c.ok]
    assert not failed, failed


def test_ws2_subscriptions_are_below_the_broker_cap(smoke) -> None:
    checks = smoke.check_ws2_subs()
    assert all(c.ok for c in checks), checks


def test_options_data_is_fresh_during_market_hours(smoke) -> None:
    checks = smoke.check_options_fresh()
    assert all(c.ok for c in checks), checks


def test_capture_wrote_a_manifest_today(smoke) -> None:
    checks = smoke.check_capture()
    assert all(c.ok for c in checks), checks


def test_no_recent_tracebacks(smoke) -> None:
    checks = smoke.check_recent_errors()
    assert all(c.ok for c in checks), checks
