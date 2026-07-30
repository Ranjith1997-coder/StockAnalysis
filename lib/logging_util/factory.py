"""
Logger factory — the SINGLE entry point for all project loggers.

Every module imports ``get_logger`` and nothing else for logger creation.
No module should call ``logging.getLogger()`` or ``logging.basicConfig()`` directly.

Runtime level overrides change the effective level WITHOUT modifying handlers,
preserving the static configure-once model while allowing dynamic control.
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

_LOGGER_INSTANCES: dict[str, logging.Logger] = {}


def _resolve_effective_level(service_name: str) -> int:
    env_key = f"{service_name.upper().replace('-', '_')}_LOG_LEVEL"
    svc_env = os.environ.get(env_key, "").upper()
    if svc_env:
        svc_level = getattr(logging, svc_env, None)
        if svc_level:
            return svc_level
    return _DEFAULT_LEVEL


def get_logger(service_name: str) -> logging.Logger:
    logger = logging.getLogger(f"SA.{service_name}")
    logger.propagate = False

    if logger.handlers:
        return logger

    level = _resolve_effective_level(service_name)
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
