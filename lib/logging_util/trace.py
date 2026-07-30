"""
Trace ID support for cross-service request correlation.

``trace_id`` is a ``contextvars.ContextVar`` that carries a request-scoped identifier
across async boundaries. When a service processes a Redis stream job, it should
set/generate a trace_id so all log lines for that job share the same ID.

The ``TraceFilter`` injects ``trace_id=<hex>`` into every log record, making it
searchable across all log files (e.g., ``grep trace_id=a1b2c3d4 logs/*.log``).

Each service that enables trace injection adds the filter during startup::

    TraceFilter.install("orchestrator")

Usage::

    from lib.logging_util import trace_id, traced

    tid = trace_id.set(generate_trace_id())   # at job boundary
    logger.info("Processing job")              # → "... | trace_id=a1b2c3d4"

    @traced("process_signal")
    def process_signal(symbol):
        ...  # trace_id injected for duration, then cleared
"""

from __future__ import annotations

import contextvars
import functools
import logging
import os
import uuid
from typing import Any, Callable

trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)


def generate_trace_id() -> str:
    return uuid.uuid4().hex[:12]


class TraceFilter(logging.Filter):
    _installed: set[str] = set()

    def __init__(self, service_name: str):
        super().__init__()
        self._service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        tid = trace_id.get()
        record.trace_id = tid or "-"
        return True

    @classmethod
    def install(cls, service_name: str) -> None:
        if service_name in cls._installed:
            return
        logger = logging.getLogger(f"SA.{service_name}")
        logger.addFilter(cls(service_name))
        cls._installed.add(service_name)


def traced(
    operation: str | None = None,
    trace_id_source: Callable[..., str] | None = None,
) -> Callable:
    def decorator(func: Callable) -> Callable:
        op_name = operation or func.__name__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if trace_id_source:
                tid = trace_id_source(*args, **kwargs)
            else:
                tid = trace_id.get() or generate_trace_id()
            token = trace_id.set(tid)
            try:
                return func(*args, **kwargs)
            finally:
                trace_id.reset(token)

        return wrapper

    return decorator
