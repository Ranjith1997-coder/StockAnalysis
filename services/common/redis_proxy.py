"""
RedisProxy — synchronous wrapper around Redis for use by data-gateway.

In Phase 1A, the data-gateway runs synchronously (same pattern as the monolith).
This proxy wraps the redis-py client for synchronous operations.
In Phase 2, this will be replaced by the async redis.asyncio client.
"""

from __future__ import annotations

import redis
from typing import Any


class RedisProxy:
    """Synchronous Redis wrapper for data-gateway."""

    def __init__(self, url: str = "redis://localhost:6379"):
        self._client = redis.from_url(url, decode_responses=True)

    def hset(self, name: str, mapping: dict) -> int:
        return self._client.hset(name, mapping=mapping)

    def hgetall(self, name: str) -> dict:
        return self._client.hgetall(name) or {}

    def hget(self, name: str, key: str) -> str | None:
        return self._client.hget(name, key)

    def set(self, name: str, value: str) -> bool:
        return self._client.set(name, value)

    def get(self, name: str) -> str | None:
        return self._client.get(name)

    def publish(self, channel: str, message: str) -> int:
        return self._client.publish(channel, message)

    def xadd(self, stream: str, fields: dict, maxlen: int | None = None) -> str:
        if maxlen is not None:
            return self._client.xadd(stream, fields, maxlen=maxlen)
        return self._client.xadd(stream, fields)

    def xreadgroup(self, groupname: str, consumername: str, streams: dict,
                   count: int | None = None, block: int | None = None) -> list | None:
        return self._client.xreadgroup(groupname, consumername, streams, count=count, block=block)

    def xgroup_create(self, groupname: str, stream: str, mkstream: bool = True):
        try:
            return self._client.xgroup_create(stream, groupname, mkstream=mkstream)
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" in str(e):
                return None
            raise

    def xack(self, stream: str, groupname: str, *ids: str) -> int:
        return self._client.xack(stream, groupname, *ids)

    def delete(self, *names: str) -> int:
        return self._client.delete(*names)

    def pubsub(self):
        return self._client.pubsub()

    def close(self):
        self._client.close()
