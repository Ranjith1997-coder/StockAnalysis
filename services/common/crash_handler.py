"""Shared crash handler — installs sys.excepthook to send fatal tracebacks
to the notification:jobs Redis stream.

Usage (in each service's main.py, after Redis connection is confirmed):

    from services.common.crash_handler import install_crash_handler
    install_crash_handler("data-gateway")

The handler sends via Redis notification:jobs stream (so the notification-service
delivers it). If Redis is unavailable, the traceback is logged at CRITICAL level
and captured by systemd journald.
"""
from __future__ import annotations

import sys
import html
import os
import time
import traceback

from services.common.logging import get_logger
logger = get_logger("crash-handler")


def install_crash_handler(service_name: str):
    """Install a global exception handler that alerts on uncaught exceptions.

    Sends via Redis notification:jobs stream. Falls back to CRITICAL logging
    if Redis is unavailable (journald captures it).

    Args:
        service_name: Display name for the alert (e.g. "data-gateway")
    """
    def _crash_handler(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        max_tb = 3500
        if len(tb_text) > max_tb:
            tb_text = tb_text[:max_tb] + "\n… (truncated)"

        exc_summary = f"{exc_type.__name__}: {exc_value}"
        if len(exc_summary) > 200:
            exc_summary = exc_summary[:200] + "…"

        message = (
            f"🚨 <b>FATAL CRASH — {service_name}</b>\n\n"
            f"<b>Exception:</b> <code>{html.escape(exc_summary)}</code>\n\n"
            f"<pre>{html.escape(tb_text)}</pre>"
        )

        try:
            import redis as sync_redis
            url = os.environ.get("REDIS_URL", "redis://localhost:6379")
            rc = sync_redis.from_url(url, decode_responses=True)
            rc.xadd("notification:jobs", {
                "chat_type": "intraday",
                "message": message,
                "parse_mode": "HTML",
                "message_type": "crash",
                "symbol": service_name,
                "priority": "CRITICAL",
                "timestamp": str(time.time()),
            }, maxlen=1000)
            rc.close()
        except Exception:
            pass

        logger.critical(
            f"Uncaught exception in {service_name} — crash handler fired",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _crash_handler
