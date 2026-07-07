"""
RedisProxy — synchronous wrapper around Redis for use by data-gateway.

In Phase 1A, the data-gateway runs synchronously (same pattern as the monolith).
This proxy wraps the redis-py client for synchronous operations.
In Phase 2, this will be replaced by the async redis.asyncio client.
"""

from __future__ import annotations

import redis


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

    def set_with_ttl(self, name: str, value: str, ex: int, nx: bool = False) -> bool:
        """Set a key with TTL and optional NX (only if not exists). Returns True if set."""
        if nx:
            result = self._client.set(name, value, ex=ex, nx=True)
            return result is not None
        return self._client.set(name, value, ex=ex)

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

    def expire(self, name: str, seconds: int) -> bool:
        return self._client.expire(name, seconds)

    def scan(self, cursor: int = 0, match: str | None = None, count: int | None = None) -> tuple:
        return self._client.scan(cursor, match=match, count=count)

    def pubsub(self):
        return self._client.pubsub()

    def hkeys(self, name: str) -> list:
        return self._client.hkeys(name)

    def xlen(self, stream: str) -> int:
        return self._client.xlen(stream)

    def xread(self, streams: dict, count: int | None = None, block: int | None = None) -> list | None:
        return self._client.xread(streams, count=count, block=block)

    def close(self):
        self._client.close()
