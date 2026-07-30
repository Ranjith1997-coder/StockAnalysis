"""
Runtime log-level control via Redis (per-logger overrides).

Redis key: ``service:log_level:{name}``  →  value: ``DEBUG`` | ``INFO`` | ``WARNING`` | ``ERROR``

Usage:
    Bot command ``/loglevel analyser DEBUG`` writes to Redis.
    CLI ``debug_cli.py loglevel analyser DEBUG`` writes to Redis.
    Each service calls ``refresh_level_from_redis(redis, name)`` in its heartbeat loop (every 30s).
    Changes take effect within one heartbeat interval — no restart needed.
"""

from __future__ import annotations

import logging

_RUNTIME_LEVEL_OVERRIDES: dict[str, str] = {}


def refresh_level_from_redis(redis, service_name: str) -> None:
    key = f"service:log_level:{service_name}"
    try:
        new_level_str = redis.get(key)
    except Exception:
        return
    if not new_level_str:
        return
    new_level_str = (
        new_level_str.decode() if isinstance(new_level_str, bytes) else new_level_str
    )
    new_level_str = new_level_str.upper()
    if new_level_str == _RUNTIME_LEVEL_OVERRIDES.get(service_name):
        return
    new_level = getattr(logging, new_level_str, None)
    if new_level is None:
        return
    logger = logging.getLogger(f"SA.{service_name}")
    logger.setLevel(new_level)
    for handler in logger.handlers:
        handler.setLevel(new_level)
    _RUNTIME_LEVEL_OVERRIDES[service_name] = new_level_str


def set_runtime_level(redis, service_name: str, level: str) -> None:
    key = f"service:log_level:{service_name}"
    redis.set(key, level.upper())


def reset_runtime_level(redis, service_name: str) -> None:
    key = f"service:log_level:{service_name}"
    redis.delete(key)
    _RUNTIME_LEVEL_OVERRIDES.pop(service_name, None)
