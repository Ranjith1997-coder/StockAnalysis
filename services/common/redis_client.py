"""
Redis connection manager for all services.

Provides a singleton-pattern async Redis client that reuses a single connection pool.
Each service creates one Redis client at startup and shares it across all tasks.
"""

from __future__ import annotations

import os
from redis.asyncio import Redis as AsyncRedis


def create_redis_client(
    url: str | None = None,
    decode_responses: bool = True,
    max_connections: int = 10,
) -> AsyncRedis:
    redis_url = url or os.environ.get("REDIS_URL", "redis://localhost:6379")
    return AsyncRedis.from_url(
        redis_url,
        decode_responses=decode_responses,
        max_connections=max_connections,
        retry_on_timeout=True,
        health_check_interval=30,
    )
