"""Tests for services/common/rate_limiter.py — RateLimiter + retry_on_429."""
import time
import threading
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

import requests


# ── Fixture: reset module-level singletons between tests ──────────────────

@pytest.fixture(autouse=True)
def _reset_limiter_globals():
    import services.common.rate_limiter as rl
    rl._sensibull_limiter = None
    rl._zerodha_limiter = None
    yield
    rl._sensibull_limiter = None
    rl._zerodha_limiter = None


# ═══════════════════════════════════════════════════════════════════════════
# RateLimiter
# ═══════════════════════════════════════════════════════════════════════════

class TestRateLimiter:
    """Test the thread-safe token-bucket RateLimiter."""

    def test_min_interval_calculation(self):
        from services.common.rate_limiter import RateLimiter
        limiter = RateLimiter(max_calls=10, per_seconds=2)
        assert limiter._min_interval == pytest.approx(0.2)

    def test_min_interval_fractional(self):
        from services.common.rate_limiter import RateLimiter
        limiter = RateLimiter(max_calls=3, per_seconds=1)
        assert limiter._min_interval == pytest.approx(1 / 3)

    def test_acquire_returns_immediately_first_call(self):
        from services.common.rate_limiter import RateLimiter
        limiter = RateLimiter(max_calls=3, per_seconds=1)
        with patch("services.common.rate_limiter.time.sleep") as mock_sleep:
            limiter.acquire()
            mock_sleep.assert_not_called()

    def test_acquire_blocks_on_rapid_second_call(self):
        from services.common.rate_limiter import RateLimiter
        limiter = RateLimiter(max_calls=3, per_seconds=1)  # min_interval = 0.333s

        # Simulate two rapid calls: monotonic returns the same time for both
        call_count = [0]
        def fake_monotonic():
            call_count[0] += 1
            return 100.0  # same time for both calls

        with patch("services.common.rate_limiter.time.monotonic", side_effect=fake_monotonic), \
             patch("services.common.rate_limiter.time.sleep") as mock_sleep:
            limiter.acquire()   # first call — sets _next_available = 100.333
            limiter.acquire()   # second call — wait = 100.333 - 100.0 = 0.333
            mock_sleep.assert_called_once()
            slept = mock_sleep.call_args[0][0]
            assert slept == pytest.approx(0.333, abs=0.01)

    def test_acquire_after_interval_does_not_block(self):
        from services.common.rate_limiter import RateLimiter
        limiter = RateLimiter(max_calls=3, per_seconds=1)

        # Set _next_available to the past — enough time has elapsed
        limiter._next_available = 50.0

        with patch("services.common.rate_limiter.time.monotonic", return_value=100.0), \
             patch("services.common.rate_limiter.time.sleep") as mock_sleep:
            limiter.acquire()
            mock_sleep.assert_not_called()

    def test_acquire_advances_next_available(self):
        from services.common.rate_limiter import RateLimiter
        limiter = RateLimiter(max_calls=10, per_seconds=1)  # min_interval = 0.1

        with patch("services.common.rate_limiter.time.monotonic", return_value=100.0), \
             patch("services.common.rate_limiter.time.sleep"):
            limiter.acquire()
            # After first call, _next_available = 100.0 + 0.1 = 100.1
            assert limiter._next_available == pytest.approx(100.1)

    def test_thread_safety_lock_exists(self):
        from services.common.rate_limiter import RateLimiter
        limiter = RateLimiter(max_calls=1, per_seconds=1)
        assert hasattr(limiter, "_lock")
        # Verify it's a real lock — acquiring it twice in the same thread would block
        assert limiter._lock.acquire(blocking=False) is True
        limiter._lock.release()


# ═══════════════════════════════════════════════════════════════════════════
# Module-level singleton getters
# ═══════════════════════════════════════════════════════════════════════════

class TestSingletonLimiters:
    """Test get_sensibull_limiter / get_zerodha_limiter singletons."""

    def test_get_sensibull_limiter_singleton(self):
        from services.common.rate_limiter import get_sensibull_limiter
        a = get_sensibull_limiter()
        b = get_sensibull_limiter()
        assert a is b

    def test_get_zerodha_limiter_singleton(self):
        from services.common.rate_limiter import get_zerodha_limiter
        a = get_zerodha_limiter()
        b = get_zerodha_limiter()
        assert a is b

    def test_sensibull_and_zerodha_are_different_instances(self):
        from services.common.rate_limiter import get_sensibull_limiter, get_zerodha_limiter
        a = get_sensibull_limiter()
        b = get_zerodha_limiter()
        assert a is not b

    def test_limiters_start_as_none(self):
        import services.common.rate_limiter as rl
        assert rl._sensibull_limiter is None
        assert rl._zerodha_limiter is None

    def test_sensibull_limiter_rate_is_3_per_second(self):
        from services.common.rate_limiter import get_sensibull_limiter
        limiter = get_sensibull_limiter()
        assert limiter._min_interval == pytest.approx(1 / 3)

    def test_zerodha_limiter_rate_is_3_per_second(self):
        from services.common.rate_limiter import get_zerodha_limiter
        limiter = get_zerodha_limiter()
        assert limiter._min_interval == pytest.approx(1 / 3)


# ═══════════════════════════════════════════════════════════════════════════
# retry_on_429 decorator
# ═══════════════════════════════════════════════════════════════════════════

class TestRetryOn429:
    """Test the retry_on_429 decorator."""

    def test_success_on_first_call(self):
        from services.common.rate_limiter import retry_on_429

        @retry_on_429(max_retries=3, base_delay=0.01)
        def func():
            return "ok"

        assert func() == "ok"

    def test_retries_on_429_then_succeeds(self):
        from services.common.rate_limiter import retry_on_429

        call_count = [0]

        @retry_on_429(max_retries=3, base_delay=0.01, max_delay=0.05)
        def func():
            call_count[0] += 1
            if call_count[0] < 3:
                resp = MagicMock(status_code=429, headers={})
                raise requests.exceptions.HTTPError(response=resp)
            return "ok"

        with patch("services.common.rate_limiter.time.sleep"):
            result = func()
        assert result == "ok"
        assert call_count[0] == 3

    def test_raises_after_max_retries(self):
        from services.common.rate_limiter import retry_on_429

        @retry_on_429(max_retries=2, base_delay=0.01, max_delay=0.05)
        def func():
            resp = MagicMock(status_code=429, headers={})
            raise requests.exceptions.HTTPError(response=resp)

        with patch("services.common.rate_limiter.time.sleep"):
            with pytest.raises(requests.exceptions.HTTPError):
                func()

    def test_non_429_error_raises_immediately(self):
        from services.common.rate_limiter import retry_on_429

        call_count = [0]

        @retry_on_429(max_retries=3, base_delay=0.01)
        def func():
            call_count[0] += 1
            resp = MagicMock(status_code=500, headers={})
            raise requests.exceptions.HTTPError(response=resp)

        with pytest.raises(requests.exceptions.HTTPError):
            func()
        assert call_count[0] == 1  # no retries on non-429

    def test_generic_exception_raises_immediately(self):
        from services.common.rate_limiter import retry_on_429

        call_count = [0]

        @retry_on_429(max_retries=3, base_delay=0.01)
        def func():
            call_count[0] += 1
            raise ValueError("not an HTTP error")

        with pytest.raises(ValueError):
            func()
        assert call_count[0] == 1

    def test_honors_retry_after_header(self):
        from services.common.rate_limiter import retry_on_429

        @retry_on_429(max_retries=1, base_delay=0.01, max_delay=10.0)
        def func():
            resp = MagicMock(status_code=429, headers={"Retry-After": "5.0"})
            raise requests.exceptions.HTTPError(response=resp)

        with patch("services.common.rate_limiter.time.sleep") as mock_sleep:
            with pytest.raises(requests.exceptions.HTTPError):
                func()
            # Should have slept ~5.0 + jitter
            slept = mock_sleep.call_args[0][0]
            assert 5.0 <= slept <= 5.0 + 5.0 * 0.25

    def test_exponential_backoff_doubles_delay(self):
        from services.common.rate_limiter import retry_on_429

        @retry_on_429(max_retries=3, base_delay=1.0, max_delay=10.0)
        def func():
            resp = MagicMock(status_code=429, headers={})
            raise requests.exceptions.HTTPError(response=resp)

        with patch("services.common.rate_limiter.time.sleep") as mock_sleep:
            with pytest.raises(requests.exceptions.HTTPError):
                func()
            sleeps = [c[0][0] for c in mock_sleep.call_args_list]
            # 3 retries: delay starts at 1.0, doubles to 2.0, then 4.0
            # Each sleep = delay + jitter where jitter in [0, delay*0.25]
            assert len(sleeps) == 3
            assert 1.0 <= sleeps[0] <= 1.25
            assert 2.0 <= sleeps[1] <= 2.5
            assert 4.0 <= sleeps[2] <= 5.0

    def test_preserves_function_name(self):
        from services.common.rate_limiter import retry_on_429

        @retry_on_429(max_retries=1, base_delay=0.01)
        def my_function():
            return "ok"

        assert my_function.__name__ == "my_function"
