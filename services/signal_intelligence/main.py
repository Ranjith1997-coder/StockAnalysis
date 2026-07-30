"""
Signal Intelligence — Service Entry Point

Consumes intelligence:signals (LIVE from market-data, INTRADAY/POSITIONAL
from analysis-engine + monolith) and detects cross-layer confluence via a
single, process-lifetime SignalCorrelator instance.

IMPORTANT: run exactly ONE instance of this service. Unlike analysis-engine,
this is NOT horizontally scalable -- a symbol's signals across layers must
land in the same in-memory correlator buffer to detect confluence. Running
multiple consumers in the same consumer group would let Redis round-robin
a symbol's signals across processes and silently break detection.
"""

import gc
import os
import signal
import sys
import time

import common.constants as constant
import common.shared as shared
from intelligence.correlator import SignalCorrelator
from services.common.redis_proxy import RedisProxy
from services.common.version import BUILD_LABEL, GIT_COMMIT, GIT_DIRTY
from services.signal_intelligence.worker import reconstruct_signal, make_on_confluence
from notification.Notification import TELEGRAM_NOTIFICATIONS
from services.common.logging import get_logger
logger = get_logger("signal-intelligence")

CONSUMER_NAME = "signal-intelligence-1"

_running = True


def signal_handler(signum, frame):
    global _running
    logger.info(f"[signal-intelligence] Received signal {signum}, shutting down...")
    _running = False


def _update_heartbeat(redis: RedisProxy, total_confluences: int):
    redis.hset("service:registry:signal-intelligence", mapping={
        "name": "signal-intelligence",
        "pid": str(os.getpid()),
        "status": "healthy",
        "last_heartbeat": str(time.time()),
        "total_confluences": str(total_confluences),
        "version": BUILD_LABEL,
        "commit": GIT_COMMIT,
        "dirty": str(GIT_DIRTY),
    })
    redis.expire("service:registry:signal-intelligence", 120)


def main():
    global _running

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = RedisProxy(redis_url)

    try:
        redis.get("ping")
        logger.info(f"[signal-intelligence] Connected to Redis at {redis_url}")
        logger.info(f"[signal-intelligence] v{BUILD_LABEL} starting")
    except Exception as e:
        logger.error(f"[signal-intelligence] Cannot connect to Redis at {redis_url}: {e}")
        sys.exit(1)

    from services.common.crash_handler import install_crash_handler
    install_crash_handler("signal-intelligence")

    # Telegram alerts are gated by these process-local class flags, same as
    # every other service that sends notifications.
    TELEGRAM_NOTIFICATIONS.is_production = os.getenv(constant.ENV_PRODUCTION, "0") == "1"
    TELEGRAM_NOTIFICATIONS.is_intraday = True

    try:
        redis.xgroup_create(constant.SIGNALS_GROUP, constant.SIGNALS_STREAM, mkstream=True)
    except Exception:
        pass

    correlator = SignalCorrelator(on_confluence=make_on_confluence(redis))
    shared.app_ctx.correlator = correlator

    logger.info("[signal-intelligence] Started, correlator initialised")

    _update_heartbeat(redis, correlator.total_confluences)
    heartbeat_counter = 0

    while _running:
        try:
            messages = redis.xreadgroup(
                constant.SIGNALS_GROUP,
                CONSUMER_NAME,
                {constant.SIGNALS_STREAM: ">"},
                count=50,
                block=5000,
            )
        except Exception as e:
            logger.error(f"[signal-intelligence] Redis xreadgroup error: {e}")
            time.sleep(2)
            continue

        if not messages:
            heartbeat_counter += 1
            if heartbeat_counter % 6 == 0:
                _update_heartbeat(redis, correlator.total_confluences)
                gc.collect()
            continue

        entries = messages[0][1] if isinstance(messages, list) and messages else []
        for msg_id, fields in entries:
            if not _running:
                break

            try:
                signal_obj = reconstruct_signal(fields)
                correlator.on_signal(signal_obj)
            except Exception as e:
                logger.exception(f"[signal-intelligence] Error processing signal {msg_id}: {e}")
            finally:
                try:
                    redis.xack(constant.SIGNALS_STREAM, constant.SIGNALS_GROUP, msg_id)
                except Exception:
                    pass

        heartbeat_counter += 1
        _update_heartbeat(redis, correlator.total_confluences)
        gc.collect()

    logger.info("[signal-intelligence] Shutting down...")
    redis.hset("service:registry:signal-intelligence", mapping={
        "status": "shutdown",
        "last_heartbeat": str(time.time()),
    })
    redis.close()


if __name__ == "__main__":
    main()
