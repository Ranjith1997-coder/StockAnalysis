from __future__ import annotations

import json
import threading
import time
from typing import Any

from services.common.logging import get_logger

logger = get_logger("cycle-subscriber")

CYCLE_STREAM = "data:cycle_stream"
CYCLE_CHANNEL = "data:cycle_ready"
CYCLE_GROUP = "monolith"


class CycleSubscriber:
    """Subscribes to data-gateway cycle signals via Redis Pub/Sub + stream.

    The data-gateway publishes two things after each cycle:
      1. XADD to data:cycle_stream (durable, survives restarts)
      2. PUBLISH to data:cycle_ready (instant notification)

    This subscriber handles both:
      - On startup: reads the stream to catch up on missed cycles
      - During operation: subscribes to Pub/Sub for instant wake-up
    """

    def __init__(self, redis_proxy, consumer_name: str = "prod-1"):
        self._redis = redis_proxy
        self._consumer = consumer_name
        self._event = threading.Event()
        self._sub_thread: threading.Thread | None = None
        self._running = False
        self._last_cycle_id = "0"
        self._cycle_count = 0

    # ── Public API ──────────────────────────────────────────────────────

    def start(self):
        self._running = True

        self._ensure_consumer_group()

        self._sub_thread = threading.Thread(
            target=self._pubsub_loop,
            name="cycle-subscriber",
            daemon=True,
        )
        self._sub_thread.start()
        logger.info("[cycle-subscriber] Subscribed to data:cycle_ready")

    def stop(self):
        self._running = False
        self._event.set()

    def wait_for_cycle(self, timeout: float = 120.0) -> bool:
        was_set = self._event.wait(timeout=timeout)
        if was_set:
            self._event.clear()
        return was_set

    def catch_up_on_startup(self, timeout: float = 30.0) -> int:
        deadline = time.time() + timeout
        entries = []
        while time.time() < deadline:
            result = self._redis.xreadgroup(
                CYCLE_GROUP, self._consumer,
                {CYCLE_STREAM: ">"},
                count=1, block=2000,
            )
            if result:
                for stream_name, msgs in result:
                    for msg_id, msg_fields in msgs:
                        if isinstance(msg_fields, dict):
                            entries.append(msg_fields)
                            self._redis.xack(CYCLE_STREAM, CYCLE_GROUP, msg_id)
                            self._last_cycle_id = msg_id
                if entries:
                    break
            if not self._running:
                break
        num_cycles = len(entries)
        if num_cycles > 0:
            last = entries[-1]
            self._cycle_count = int(last.get("cycle", "0"))
            logger.info(f"[cycle-subscriber] Caught up: {num_cycles} cycle(s), last id={self._cycle_count}")
        else:
            logger.info("[cycle-subscriber] No pending cycles in stream — waiting for first Pub/Sub signal")
        return num_cycles

    @property
    def cycle_count(self) -> int:
        return self._cycle_count

    # ── Private ─────────────────────────────────────────────────────────

    def _ensure_consumer_group(self):
        try:
            self._redis.xgroup_create(CYCLE_GROUP, CYCLE_STREAM, mkstream=True)
        except Exception as e:
            logger.debug(f"[cycle-subscriber] Consumer group create: {e}")

    def _pubsub_loop(self):
        ps = self._redis.pubsub()
        try:
            ps.subscribe(CYCLE_CHANNEL)
            logger.info(f"[cycle-subscriber] Subscribed to channel {CYCLE_CHANNEL}")
            for message in ps.listen():
                if not self._running:
                    break
                if message["type"] == "message":
                    payload = message["data"]
                    if isinstance(payload, bytes):
                        payload = payload.decode()
                    logger.debug(f"[cycle-subscriber] Received: {payload}")
                    try:
                        if "cycle=" in payload:
                            parts = payload.split(",")
                            for p in parts:
                                if p.startswith("cycle="):
                                    self._cycle_count = int(p.split("=")[1])
                    except (ValueError, IndexError):
                        pass
                    self._event.set()
        except Exception as e:
            logger.error(f"[cycle-subscriber] Pub/Sub error: {e}")
        finally:
            try:
                ps.unsubscribe()
                ps.close()
            except Exception:
                pass
