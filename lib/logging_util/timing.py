"""
Timing helpers for measuring and logging operation durations.

``log_duration`` is a context manager that logs elapsed time when the block exits.
Use it to instrument external calls, data processing, and any expensive operation.

Usage::

    with log_duration(logger, "fetch_option_chain", symbol="NIFTY", expiry="27AUG"):
        data = fetch()

    # Logs:  fetch_option_chain symbol=NIFTY expiry=27AUG completed in 1.23s   [INFO]
    #        fetch_option_chain symbol=NIFTY expiry=27AUG failed in 0.45s       [ERROR, if exception]

    @timed(logger, "backtest_run")
    def run_backtest(symbol):
        ...

Decorator ``@timed`` wraps a function and logs start + completion/failure + duration.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import time
from typing import Any, Callable


@contextlib.contextmanager
def log_duration(
    logger: logging.Logger,
    operation: str,
    level: int = logging.INFO,
    error_level: int = logging.ERROR,
    **attrs: Any,
):
    attrs_str = " ".join(f"{k}={v}" for k, v in attrs.items()) if attrs else ""
    label = f"{operation} {attrs_str}".strip()
    start = time.perf_counter()
    try:
        yield
        elapsed = time.perf_counter() - start
        logger.log(level, "%s completed in %.2fs", label, elapsed)
    except Exception:
        elapsed = time.perf_counter() - start
        logger.log(error_level, "%s failed in %.2fs", label, elapsed)
        raise


def timed(
    logger: logging.Logger,
    operation: str | None = None,
    level: int = logging.DEBUG,
    error_level: int = logging.ERROR,
):
    def decorator(func: Callable):
        op_name = operation or func.__name__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                elapsed = time.perf_counter() - start
                logger.log(level, "%s completed in %.2fs", op_name, elapsed)
                return result
            except Exception:
                elapsed = time.perf_counter() - start
                logger.log(error_level, "%s failed in %.2fs", op_name, elapsed)
                raise

        return wrapper

    return decorator
