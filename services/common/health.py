"""
Health check — each service writes a heartbeat to Redis every 10 seconds.

The orchestrator and bot-service read these heartbeats for the /status dashboard.
A service is considered "healthy" if its heartbeat is younger than 30 seconds.
"""

from __future__ import annotations

import os
import json
import time
import signal
import asyncio
import logging
from typing import Any
from redis.asyncio import Redis


HEARTBEAT_INTERVAL = 10  # seconds
HEARTBEAT_TTL = 30       # seconds — if no heartbeat in 30s, service is dead


async def heartbeat_loop(
    redis: Redis,
    service_name: str,
    get_stats: Any = None,
    stop_event: asyncio.Event | None = None,
    logger: logging.Logger | None = None,
):
    """
    Background task: write service health to Redis every HEARTBEAT_INTERVAL seconds.

    Args:
        redis: Redis async client
        service_name: unique name for this service (e.g., "data-gateway")
        get_stats: optional callable returning a dict of service-specific stats
        stop_event: when set, the loop exits
    """
    key = f"service:registry:{service_name}"
    stop = stop_event or asyncio.Event()

    while not stop.is_set():
        try:
            stats = get_stats() if callable(get_stats) else {}
            payload = {
                "name": service_name,
                "pid": os.getpid(),
                "status": "healthy",
                "last_heartbeat": time.time(),
                "version": "2.0.0",
                "stats_json": json.dumps(stats, default=str),
            }
            await redis.hset(key, mapping=payload)
            await redis.expire(key, HEARTBEAT_TTL + 10)
        except Exception as e:
            _log = logger or logging.getLogger(__name__)
            _log.error(f"[heartbeat:{service_name}] Failed to write heartbeat: {e}")

        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().create_future() if not stop.is_set()
                else asyncio.sleep(0),
                timeout=HEARTBEAT_INTERVAL,
            )
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
