"""Shared Pub/Sub listen loop with a quiet shutdown path.

Every service runs a daemon thread on ``pubsub.listen()``. When systemd stops
the process, Redis closes the socket and the thread used to print a
``ConnectionError`` traceback *after* "Shutdown complete" — noise that made the
post-deploy smoke check fail and would hide real errors in the journal.
"""
from __future__ import annotations

from typing import Callable

import redis as redis_lib


def listen_until_closed(pubsub, on_message: Callable[[dict], None], *, logger, label: str) -> None:
    """Iterate ``pubsub.listen()`` until the process/Redis closes the socket.

    ``on_message`` runs inside the loop; exceptions it raises are *not* caught
    (a handler bug must stay visible). Only Redis connection errors are
    swallowed, and only at debug level.
    """
    try:
        for message in pubsub.listen():
            on_message(message)
    except redis_lib.exceptions.RedisError as exc:
        logger.debug(f"[{label}] Pub/Sub closed: {exc}")
    finally:
        try:
            pubsub.close()
        except redis_lib.exceptions.RedisError as exc:
            logger.debug(f"[{label}] Pub/Sub close skipped: {exc}")
        except Exception as exc:          # noqa: BLE001 - close is best-effort
            logger.debug(f"[{label}] Pub/Sub close failed: {exc}")
