"""The /version command must list every always-running service."""
from lib.notification.commands.system import _VERSION_SERVICES

EXPECTED = {
    "orchestrator",
    "data-gateway",
    "market-data",
    "analysis-engine",
    "signal-intelligence",
    "notification-service",
    "resource-monitor",
    "paper-trading",
    "volatility-engine",
    "pinn-trainer",
    "auth-service",
}


def test_version_covers_all_services():
    missing = EXPECTED - set(_VERSION_SERVICES)
    assert not missing, f"/version is missing: {sorted(missing)}"


def test_no_duplicate_entries():
    assert len(_VERSION_SERVICES) == len(set(_VERSION_SERVICES))
