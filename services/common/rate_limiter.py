"""
Rate limiter + retry utility for HTTP fetchers.

Provides:
  - RateLimiter: thread-safe token bucket for proactive throttling across workers
  - retry_on_429: decorator with exponential backoff + jitter, honors Retry-After header
"""

from __future__ import annotations

import functools
import random
import threading
import time

import requests


class RateLimiter:
    """Thread-safe token bucket — proactively limits request rate across all workers.

    Usage:
        limiter = RateLimiter(max_calls=3, per_seconds=1)  # 3 req/s
        limiter.acquire()  # blocks until a slot is available
        requests.get(url)
    """

    def __init__(self, max_calls: int, per_seconds: float):
        self._min_interval = per_seconds / max_calls
        self._lock = threading.Lock()
        self._next_available = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_available - now
            if wait > 0:
                self._next_available += self._min_interval
            else:
                self._next_available = now + self._min_interval
        if wait > 0:
            time.sleep(wait)


# Module-level rate limiters — shared across all worker threads
_sensibull_limiter: RateLimiter | None = None
_zerodha_limiter: RateLimiter | None = None


def get_sensibull_limiter() -> RateLimiter:
    global _sensibull_limiter
    if _sensibull_limiter is None:
        _sensibull_limiter = RateLimiter(max_calls=3, per_seconds=1)
    return _sensibull_limiter


def get_zerodha_limiter() -> RateLimiter:
    global _zerodha_limiter
    if _zerodha_limiter is None:
        _zerodha_limiter = RateLimiter(max_calls=3, per_seconds=1)
    return _zerodha_limiter


def retry_on_429(max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 8.0):
    """Decorator: retry on HTTP 429 with exponential backoff + jitter.

    Honors the Retry-After response header if present.
    For non-429 errors, re-raises immediately (no retry).

    Args:
        max_retries: maximum number of retry attempts (total tries = max_retries + 1)
        base_delay: initial delay in seconds (doubles each retry)
        max_delay: maximum delay cap in seconds
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.HTTPError as e:
                    status = getattr(e.response, "status_code", None)
                    if status != 429 or attempt == max_retries:
                        raise
                    retry_after = e.response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = min(float(retry_after), max_delay)
                        except ValueError:
                            pass
                    jitter = random.uniform(0, delay * 0.25)
                    time.sleep(delay + jitter)
                    delay = min(delay * 2, max_delay)
                    last_exc = e
                except Exception:
                    raise
            if last_exc:
                raise last_exc

        return wrapper

    return decorator
