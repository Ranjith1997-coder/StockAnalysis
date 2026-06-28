"""
Monolith logging — delegates to services/common/logging.py factory.

All modules that do ``from common.logging_util import logger`` get the same
unified logger as the microservices, writing to:

  - stdout (captured by systemd journal on server)
  - logs/monolith.log (10 MB rotating, 3 backups)

Format (NEW+):
  28 13:26:31 | WARNING | SA.monolith              | intraday_monitor.py:1234 | message

This file is kept as a thin shim so 44+ modules don't need import changes.
"""

from __future__ import annotations

from services.common.logging import get_logger

logger = get_logger("monolith")
