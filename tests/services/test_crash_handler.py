"""Tests for services/common/crash_handler.py"""
import sys
import os
from unittest.mock import MagicMock, patch, patch as mock_patch


class TestCrashHandler:
    """Test install_crash_handler and the installed excepthook."""

    def test_installs_excepthook(self):
        from services.common.crash_handler import install_crash_handler
        original = sys.excepthook
        try:
            install_crash_handler("test-service")
            assert sys.excepthook is not original
        finally:
            sys.excepthook = original

    def test_keyboard_interrupt_passes_through(self):
        from services.common.crash_handler import install_crash_handler
        original = sys.__excepthook__
        called = []

        def fake_default(exc_type, exc_value, exc_tb):
            called.append(True)

        try:
            with mock_patch.object(sys, "__excepthook__", fake_default):
                install_crash_handler("test-service")
                sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
                assert len(called) == 1
        finally:
            sys.excepthook = original

    def test_sends_to_redis_stream(self):
        from services.common.crash_handler import install_crash_handler

        mock_rc = MagicMock()
        original = sys.excepthook
        try:
            with mock_patch("redis.from_url", return_value=mock_rc):
                install_crash_handler("data-gateway")
                try:
                    raise ValueError("test crash")
                except ValueError:
                    exc_type, exc_value, exc_tb = sys.exc_info()
                    sys.excepthook(exc_type, exc_value, exc_tb)

                mock_rc.xadd.assert_called_once()
                call_args = mock_rc.xadd.call_args
                assert call_args[0][0] == "notification:jobs"
                fields = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("fields", {})
                assert fields["message_type"] == "crash"
                assert fields["symbol"] == "data-gateway"
                assert fields["priority"] == "CRITICAL"
                assert "FATAL CRASH" in fields["message"]
                assert "data-gateway" in fields["message"]
                assert "ValueError" in fields["message"]
                mock_rc.close.assert_called_once()
        finally:
            sys.excepthook = original

    def test_falls_back_when_redis_unavailable(self):
        from services.common.crash_handler import install_crash_handler

        original = sys.excepthook
        try:
            with mock_patch("redis.from_url", side_effect=Exception("Redis down")):
                install_crash_handler("market-data")
                try:
                    raise RuntimeError("test crash 2")
                except RuntimeError:
                    exc_type, exc_value, exc_tb = sys.exc_info()
                    # Should not raise
                    sys.excepthook(exc_type, exc_value, exc_tb)
        finally:
            sys.excepthook = original

    def test_truncates_long_traceback(self):
        from services.common.crash_handler import install_crash_handler

        mock_rc = MagicMock()
        original = sys.excepthook
        try:
            with mock_patch("redis.from_url", return_value=mock_rc):
                install_crash_handler("test-service")

                # Create a deeply nested exception with long traceback
                def deep_recurse(n):
                    if n == 0:
                        raise ValueError("x" * 5000)
                    deep_recurse(n - 1)

                try:
                    deep_recurse(200)
                except ValueError:
                    exc_type, exc_value, exc_tb = sys.exc_info()
                    sys.excepthook(exc_type, exc_value, exc_tb)

                fields = mock_rc.xadd.call_args[0][1]
                message = fields["message"]
                # The traceback portion should be truncated
                assert "truncated" in message
        finally:
            sys.excepthook = original

    def test_html_escapes_exception_text(self):
        from services.common.crash_handler import install_crash_handler

        mock_rc = MagicMock()
        original = sys.excepthook
        try:
            with mock_patch("redis.from_url", return_value=mock_rc):
                install_crash_handler("test-service")
                try:
                    raise ValueError("<script>alert('xss')</script>")
                except ValueError:
                    exc_type, exc_value, exc_tb = sys.exc_info()
                    sys.excepthook(exc_type, exc_value, exc_tb)

                fields = mock_rc.xadd.call_args[0][1]
                message = fields["message"]
                assert "<script>" not in message
                assert "&lt;script&gt;" in message
        finally:
            sys.excepthook = original
