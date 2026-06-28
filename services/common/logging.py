"""
Per-service logger factory for the microservices architecture.

Each service calls ``get_logger(service_name)`` to create a logger that writes to:
- stdout (captured by systemd journal on server)
- logs/{service_name}.log (10 MB rotating, 3 backups)

The monolith (intraday_monitor.py) continues using ``common/logging_util.py``
during the transition and is decommissioned when all services are extracted.

Usage::

    from services.common.logging import get_logger
    logger = get_logger("notification-service")
    logger.info("Sent alert for NIFTY")

Output format::

    14:10:23 | INFO    | SA.notification-service  | Sent alert for NIFTY
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

load_dotenv()

_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "logs",
)
os.makedirs(_LOG_DIR, exist_ok=True)

# Timestamp | Level (7-char padded) | Service (24-char padded) | filename:lineno | Message
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-24s | %(filename)s:%(lineno)d | %(message)s"
_LOG_DATE_FORMAT = "%d %H:%M:%S"

# Global default — overridable per service via {SERVICE}_LOG_LEVEL env var
_DEFAULT_LEVEL_NAME = os.environ.get("LOG_LEVEL", "INFO").upper()
_DEFAULT_LEVEL = getattr(logging, _DEFAULT_LEVEL_NAME, logging.INFO)


def get_logger(service_name: str) -> logging.Logger:
    """
    Create a service-specific logger.

    Args:
        service_name: e.g. "notification-service", "data-gateway", "analysis-engine"

    Returns:
        Configured logger with console + rotating file handlers.
    """
    logger = logging.getLogger(f"SA.{service_name}")
    logger.propagate = False

    # Already configured — return cached instance
    if logger.handlers:
        return logger

    # Per-service log level override, e.g. NOTIFICATION_LOG_LEVEL=DEBUG
    svc_level_name = os.environ.get(
        f"{service_name.upper().replace('-', '_')}_LOG_LEVEL", ""
    ).upper()
    level = getattr(logging, svc_level_name, None) or _DEFAULT_LEVEL
    logger.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        os.path.join(_LOG_DIR, f"{service_name}.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
