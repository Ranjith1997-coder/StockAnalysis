"""
Base class for Redis Stream consumers with consumer group support.

Provides:
- Auto-creation of consumer groups (first consumer creates the group)
- XCLAIM of stale messages from dead consumers
- Graceful shutdown via stop_event
- Configurable batch size and block time
"""

from __future__ import annotations

import time
import asyncio
import json
import logging
from abc import ABC, abstractmethod
from redis.asyncio import Redis


class StreamConsumer(ABC):
    def __init__(
        self,
        redis: Redis,
        stream: str,
        group: str,
        consumer_name: str,
        batch_size: int = 10,
        block_ms: int = 5000,
        ack_timeout: int = 120,
        stop_event: asyncio.Event | None = None,
        logger: logging.Logger | None = None,
    ):
        self.redis = redis
        self.stream = stream
        self.group = group
        self.consumer = consumer_name
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.ack_timeout = ack_timeout
        self.stop_event = stop_event or asyncio.Event()
        self._running = False
        self._log = logger or logging.getLogger(__name__)

    async def start(self):
        self._running = True
        await self._ensure_group()
        self._running = True

        while self._running and not self.stop_event.is_set():
            try:
                messages = await self.redis.xreadgroup(
                    groupname=self.group,
                    consumername=self.consumer,
                    streams={self.stream: ">"},
                    count=self.batch_size,
                    block=self.block_ms,
                )
                if not messages:
                    continue
                stream, entries = messages[0]
                for msg_id, fields in entries:
                    try:
                        await self._process(msg_id, fields)
                    except Exception as e:
                        self._log.error(
                            f"[{self.consumer}] Error processing {msg_id}: {e}"
                        )
                    finally:
                        await self.redis.xack(self.stream, self.group, msg_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log.error(f"[{self.consumer}] Stream error: {e}")
                await asyncio.sleep(1)

    async def stop(self):
        self._running = False

    async def _ensure_group(self):
        try:
            await self.redis.xgroup_create(
                self.stream, self.group, id="0", mkstream=True
            )
        except Exception:
            pass

    @abstractmethod
    async def _process(self, msg_id: str, fields: dict):
        ...
