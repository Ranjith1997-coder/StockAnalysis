"""Integration: heartbeats are visible for their whole TTL and expire after (T4.2).

The data-gateway bug: a 120 s TTL with a 310 s cycle made the service vanish
from the registry between cycles. Unit tests with mocks never aged anything.
"""
from __future__ import annotations

import importlib

import pytest

from services.resource_monitor.main import discover_services

pytestmark = pytest.mark.integration

WRITERS = {
    "auth-service": ("services.auth_service.main", "_update_heartbeat", ()),
    "paper-trading": ("services.paper_trading.main", "update_heartbeat", ()),
    "signal-intelligence": ("services.signal_intelligence.main", "_update_heartbeat", (0,)),
    "data-gateway": ("services.data_gateway.main", "_touch_heartbeat", ()),
}


def _write(service: str, redis) -> None:
    module_name, attr, args = WRITERS[service]
    writer = getattr(importlib.import_module(module_name), attr)
    writer(redis, *args)


@pytest.mark.parametrize("service", sorted(WRITERS))
def test_service_stays_visible_for_its_ttl_then_expires(service, fake_redis, clock) -> None:
    _write(service, fake_redis)
    key = f"service:registry:{service}"
    ttl = fake_redis._client.ttl(key)
    assert ttl > 0, f"{service}: heartbeat written without a TTL"

    assert service in discover_services(fake_redis._client)

    clock.shift(ttl // 2)                 # halfway through its own TTL
    assert service in discover_services(fake_redis._client)

    clock.shift(ttl)                      # past it
    assert key not in fake_redis._client
    assert service not in discover_services(fake_redis._client), (
        f"{service} still listed after TTL expiry — discovery is reading stale state"
    )


def test_all_services_shown_together(fake_redis, clock) -> None:
    for service in WRITERS:
        _write(service, fake_redis)

    discovery = discover_services(fake_redis._client)
    missing = set(WRITERS) - set(discovery)
    assert not missing, f"not discovered: {sorted(missing)}"
    for name, info in discovery.items():
        # data-gateway writes liveness only between cycles, so its status comes
        # from the full cycle writer; "unknown" is the expected interim value.
        assert info["status"] in {"healthy", "unknown"}, f"{name}: {info}"
        assert float(info["last_heartbeat"]) > 0, f"{name}: no heartbeat timestamp"


def test_heartbeat_refresh_extends_visibility(fake_redis, clock) -> None:
    """A service that keeps beating must never disappear (data-gateway bug)."""
    service = "data-gateway"
    for _ in range(5):
        _write(service, fake_redis)
        clock.shift(100)                  # beat faster than any TTL
        assert service in discover_services(fake_redis._client)
