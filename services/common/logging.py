"""
Per-directory logger factory for all modules and services.

Each module calls ``get_logger(name)`` to create a logger that writes to:
- stdout (captured by systemd journal on server)
- logs/{name}.log (10 MB rotating, 3 backups)

Usage::

    from services.common.logging import get_logger
    logger = get_logger("analyser")
    logger.info("Stock analysis complete")

Output format::

    14:10:23 | INFO    | SA.analyser             | Analyser.py:25 | Stock analysis complete

Runtime level control via Redis key ``service:log_level:{name}``.
Bot command ``/loglevel <name> <level>`` writes to this key.
Each service's heartbeat (30s) calls ``refresh_level_from_redis()``.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

from dotenv import load_dotenv

load_dotenv()

_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "logs",
)
os.makedirs(_LOG_DIR, exist_ok=True)

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-24s | %(filename)s:%(lineno)d | %(message)s"
_LOG_DATE_FORMAT = "%d %H:%M:%S"

_DEFAULT_LEVEL_NAME = os.environ.get("LOG_LEVEL", "INFO").upper()
_DEFAULT_LEVEL = getattr(logging, _DEFAULT_LEVEL_NAME, logging.INFO)

_RUNTIME_LEVEL_OVERRIDES: dict[str, str] = {}
_LOGGER_INSTANCES: dict[str, logging.Logger] = {}


def get_logger(service_name: str) -> logging.Logger:
    logger = logging.getLogger(f"SA.{service_name}")
    logger.propagate = False

    if logger.handlers:
        return logger

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

    _LOGGER_INSTANCES[service_name] = logger
    return logger


def refresh_level_from_redis(redis, service_name: str) -> None:
    from services.common.logging import get_logger as _get  # deferred import
    key = f"service:log_level:{service_name}"
    try:
        new_level_str = redis.get(key)
    except Exception:
        return
    if not new_level_str:
        return
    new_level_str = new_level_str.decode() if isinstance(new_level_str, bytes) else new_level_str
    new_level_str = new_level_str.upper()
    if new_level_str == _RUNTIME_LEVEL_OVERRIDES.get(service_name):
        return
    new_level = getattr(logging, new_level_str, None)
    if new_level is None:
        return
    logger = logging.getLogger(f"SA.{service_name}")
    logger.setLevel(new_level)
    for handler in logger.handlers:
        handler.setLevel(new_level)
    _RUNTIME_LEVEL_OVERRIDES[service_name] = new_level_str


def set_runtime_level(redis, service_name: str, level: str) -> None:
    key = f"service:log_level:{service_name}"
    redis.set(key, level.upper())


def reset_runtime_level(redis, service_name: str) -> None:
    key = f"service:log_level:{service_name}"
    redis.delete(key)
    _RUNTIME_LEVEL_OVERRIDES.pop(service_name, None)
