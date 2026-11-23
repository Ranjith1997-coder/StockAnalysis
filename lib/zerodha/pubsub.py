"""Shared Pub/Sub listen loop with a quiet shutdown path.

Every service runs a daemon thread on ``pubsub.listen()``. When systemd stops
the process, Redis closes the socket and the thread used to print a
``ConnectionError`` traceback *after* "Shutdown complete" — noise that made the
post-deploy smoke check fail and would hide real errors in the journal.
"""
from __future__ import annotations

import sys
from typing import Callable

import redis as redis_lib


def _log_quietly(logger, message: str) -> None:
    """Log unless the interpreter is tearing down.

    On SIGTERM the daemon thread wakes after "Shutdown complete"; by then the
    logging handlers may already be closed, so a plain ``logger.debug`` raises
    *inside* the handler and Python prints a chained traceback anyway.
    """
    if sys.is_finalizing():
        return
    try:
        logger.debug(message)
    except Exception:                 # noqa: BLE001 - logging must never fail a shutdown
        return


def _is_shutdown_error(exc: BaseException, should_stop: Callable[[], bool] | None) -> bool:
    """True when the error is connection teardown rather than a real failure.

    The second exception raised while a generator unwinds (redis closing the
    socket mid-``listen``) is often not a ``RedisError`` itself, but its
    ``__context__`` is.
    """
    if isinstance(exc, redis_lib.exceptions.RedisError):
        return True
    context = exc.__context__
    while context is not None:
        if isinstance(context, redis_lib.exceptions.RedisError):
            return True
        context = context.__context__
    if should_stop is not None and should_stop():
        return True
    return sys.is_finalizing()


def listen_until_closed(pubsub, on_message: Callable[[dict], None], *, logger, label: str,
                        should_stop: Callable[[], bool] | None = None) -> None:
    """Iterate ``pubsub.listen()`` until the process/Redis closes the socket.

    ``on_message`` runs inside the loop; exceptions it raises are *not* caught
    (a handler bug must stay visible). Connection teardown - including the
    chained exception raised while the listen generator unwinds - is swallowed
    quietly, and ``should_stop`` tells the helper the service is going down.
    """
    try:
        for message in pubsub.listen():
            on_message(message)
    except Exception as exc:          # noqa: BLE001 - classified below
        if not _is_shutdown_error(exc, should_stop):
            raise
        _log_quietly(logger, f"[{label}] Pub/Sub closed: {exc}")
    finally:
        try:
            pubsub.close()
        except redis_lib.exceptions.RedisError as exc:
            _log_quietly(logger, f"[{label}] Pub/Sub close skipped: {exc}")
        except Exception as exc:          # noqa: BLE001 - close is best-effort
            _log_quietly(logger, f"[{label}] Pub/Sub close failed: {exc}")
