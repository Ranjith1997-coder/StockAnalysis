"""
Unified logging library for ALL project code — services, libs, tools, common.

Provides a single ``get_logger(name)`` entry point that every module uses.
No other logging setup should exist anywhere in the project.

Usage::

    from lib.logging_util import get_logger
    logger = get_logger("orchestrator")
    logger.info("Service started (pid=%d)", os.getpid())

Output::

    console  →  systemd journal on server, stderr locally
    file     →  logs/{name}.log  (10 MB rotating, 3 backups)

    Format:  14:10:23 | INFO    | SA.orchestrator          | main.py:25 | Service started

Runtime level control via Redis key ``service:log_level:{name}``.
Bot command ``/loglevel <name> <level>`` or CLI ``debug_cli.py loglevel <name> <level>``.

Style Guide
-----------
- **DEBUG**: Internal state, variable values, step-by-step flow.
  Use lazy %-formatting, NOT f-strings (evaluated eagerly).
  ``logger.debug("Processing tick for %s: price=%.2f", symbol, price)``

- **INFO**: Significant lifecycle events: service start/stop, operation completion.
  ``logger.info("Fetched %d rows for %s in %.2fs", count, symbol, elapsed)``

- **WARNING**: Recoverable issues, degraded operation, fallback paths.
  ``logger.warning("Stale WS data (age=%.1fs), using fallback close", ws_age)``

- **ERROR**: Operation failures. Always include ``exc_info=True`` for unexpected errors.
  ``logger.error("Failed to fetch %s: %s", url, e, exc_info=True)``

- **CRITICAL**: Service-crashing errors, data corruption.

DO NOT
~~~~~~
- Use f-strings in log calls (evaluated eagerly, wastes CPU)
- Use ``print()`` — always use the logger
- Use ``logging.getLogger()`` directly — always use ``get_logger()``
- Bare ``except: pass`` — at minimum ``logger.debug("Ignoring %s", e)``

External call pattern
~~~~~~~~~~~~~~~~~~~~~
::

    with log_duration(logger, "api_call", url=url):
        response = requests.get(url)
    logger.debug("HTTP %s -> %d in %.2fs", url, response.status_code, elapsed)

Error handling pattern
~~~~~~~~~~~~~~~~~~~~~~
::

    try:
        do_thing()
    except Exception as e:
        logger.error("Thing failed: %s", e, exc_info=True)
        raise

Startup/shutdown pattern
~~~~~~~~~~~~~~~~~~~~~~~~
::

    logger.info("%s v%s starting (pid=%d, mode=%s)", name, version, os.getpid(), mode)
    ...
    logger.info("%s shutting down", name)
"""

from lib.logging_util.factory import get_logger
from lib.logging_util.levels import (
    refresh_level_from_redis,
    reset_runtime_level,
    set_runtime_level,
)
from lib.logging_util.timing import log_duration, timed
from lib.logging_util.trace import trace_id, traced

__all__ = [
    "get_logger",
    "refresh_level_from_redis",
    "set_runtime_level",
    "reset_runtime_level",
    "log_duration",
    "timed",
    "trace_id",
    "traced",
]
