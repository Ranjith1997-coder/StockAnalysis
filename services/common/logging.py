"""
BACKWARD-COMPAT SHIM — re-exports from the canonical ``lib.logging_util``.

**DO NOT add new imports from this module.** Use ``lib.logging_util`` directly.

This shim exists so existing ``from services.common.logging import get_logger``
continues to work while we migrate imports to ``lib.logging_util``.
"""

from lib.logging_util.factory import get_logger
from lib.logging_util.levels import (
    refresh_level_from_redis,
    reset_runtime_level,
    set_runtime_level,
)

__all__ = [
    "get_logger",
    "refresh_level_from_redis",
    "set_runtime_level",
    "reset_runtime_level",
]
