"""
Unit tests for notification/Notification.py

Covers:
- send_notification(): production guard, success, retry on HTTP error,
  retry on Timeout, retry on ConnectionError, exception fallback
- send_live_options_notification(): production guard, no-token guard,
  success, HTTP error, exception
"""
import pytest
from unittest.mock import MagicMock, patch, call
from notification.Notification import TELEGRAM_NOTIFICATIONS


# ── Helpers ───────────────────────────────────────────────────────────────

def _set_production(value: int):
    TELEGRAM_NOTIFICATIONS.is_production = value


def _restore_production():
    TELEGRAM_NOTIFICATIONS.is_production = 0


@pytest.fixture(autouse=True)
def reset_production():
    """Ensure is_production is always reset to 0 after each test."""
    yield
    _restore_production()


# ════════════════════════════════════════════════════════════════════════════
# send_notification()
# ════════════════════════════════════════════════════════════════════════════

class TestSendNotification:

    def test_no_op_when_not_in_production(self):
        """Returns None immediately without making any HTTP call."""
        _set_production(0)
        with patch("notification.Notification.requests.post") as mock_post:
            result = TELEGRAM_NOTIFICATIONS.send_notification("hello")
        mock_post.assert_not_called()
        assert result is None

    def test_returns_true_on_http_200(self):
        _set_production(1)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("notification.Notification.requests.post", return_value=mock_resp):
            result = TELEGRAM_NOTIFICATIONS.send_notification("hello")
        assert result is True

    def test_retries_on_non_200_and_succeeds(self):
        """First call returns 500, second returns 200 → True."""
        _set_production(1)
        fail_resp = MagicMock()
        fail_resp.status_code = 500
        fail_resp.text = "Server Error"

        ok_resp = MagicMock()
        ok_resp.status_code = 200

        with patch("notification.Notification.requests.post",
                   side_effect=[fail_resp, ok_resp]) as mock_post, \
             patch("notification.Notification.time.sleep" if hasattr(
                 __import__("notification.Notification", fromlist=["Notification"]),
                 "time") else "time.sleep"):
            # patch sleep to avoid slowing tests
            with patch("time.sleep"):
                result = TELEGRAM_NOTIFICATIONS.send_notification("hello")

        assert result is True
        assert mock_post.call_count == 2

    def test_returns_false_after_all_retries_fail(self):
        """All 3 attempts return 500 → False."""
        _set_production(1)
        fail_resp = MagicMock()
        fail_resp.status_code = 500
        fail_resp.text = "Server Error"

        with patch("notification.Notification.requests.post",
                   return_value=fail_resp), \
             patch("time.sleep"):
            result = TELEGRAM_NOTIFICATIONS.send_notification("hello")

        assert result is False

    def test_retries_on_timeout(self):
        """Timeout on first two, success on third → True."""
        import requests as req_lib
        _set_production(1)
        ok_resp = MagicMock()
        ok_resp.status_code = 200

        with patch("notification.Notification.requests.post",
                   side_effect=[
                       req_lib.Timeout,
                       req_lib.Timeout,
                       ok_resp,
                   ]), patch("time.sleep"):
            result = TELEGRAM_NOTIFICATIONS.send_notification("hello")

        assert result is True

    def test_retries_on_connection_error(self):
        """ConnectionError on all 3 attempts → False."""
        import requests as req_lib
        _set_production(1)

        with patch("notification.Notification.requests.post",
                   side_effect=req_lib.ConnectionError), \
             patch("time.sleep"):
            result = TELEGRAM_NOTIFICATIONS.send_notification("hello")

        assert result is False

    def test_passes_parse_mode_in_payload(self):
        """parse_mode=HTML is forwarded in the POST body."""
        _set_production(1)
        ok_resp = MagicMock()
        ok_resp.status_code = 200

        with patch("notification.Notification.requests.post",
                   return_value=ok_resp) as mock_post:
            TELEGRAM_NOTIFICATIONS.send_notification("bold text", parse_mode="HTML")

        _, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        assert payload.get("parse_mode") == "HTML"

    def test_no_parse_mode_by_default(self):
        """Default call must not include parse_mode key in payload."""
        _set_production(1)
        ok_resp = MagicMock()
        ok_resp.status_code = 200

        with patch("notification.Notification.requests.post",
                   return_value=ok_resp) as mock_post:
            TELEGRAM_NOTIFICATIONS.send_notification("plain text")

        _, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        assert "parse_mode" not in payload


# ════════════════════════════════════════════════════════════════════════════
# send_live_options_notification()
# ════════════════════════════════════════════════════════════════════════════

class TestSendLiveOptionsNotification:

    def test_no_op_when_not_in_production(self):
        _set_production(0)
        with patch("notification.Notification.requests.post") as mock_post:
            result = TELEGRAM_NOTIFICATIONS.send_live_options_notification("alert")
        mock_post.assert_not_called()
        assert result is None

    def test_no_op_when_token_not_configured(self):
        """If TELEGRAM_LIVE_OPTIONS_TOKEN is empty, skip silently."""
        _set_production(1)
        with patch("notification.Notification.TELEGRAM_LIVE_OPTIONS_TOKEN", ""), \
             patch("notification.Notification.TELEGRAM_LIVE_OPTIONS_CHAT_ID", ""), \
             patch("notification.Notification.requests.post") as mock_post:
            result = TELEGRAM_NOTIFICATIONS.send_live_options_notification("alert")
        mock_post.assert_not_called()
        assert result is False

    def test_returns_true_on_http_200(self):
        _set_production(1)
        ok_resp = MagicMock()
        ok_resp.status_code = 200

        with patch("notification.Notification.TELEGRAM_LIVE_OPTIONS_TOKEN", "token123"), \
             patch("notification.Notification.TELEGRAM_LIVE_OPTIONS_CHAT_ID", "chat123"), \
             patch("notification.Notification.requests.post", return_value=ok_resp):
            result = TELEGRAM_NOTIFICATIONS.send_live_options_notification("alert")

        assert result is True

    def test_returns_false_on_http_error(self):
        _set_production(1)
        err_resp = MagicMock()
        err_resp.status_code = 400
        err_resp.text = "Bad Request"

        with patch("notification.Notification.TELEGRAM_LIVE_OPTIONS_TOKEN", "token123"), \
             patch("notification.Notification.TELEGRAM_LIVE_OPTIONS_CHAT_ID", "chat123"), \
             patch("notification.Notification.requests.post", return_value=err_resp):
            result = TELEGRAM_NOTIFICATIONS.send_live_options_notification("alert")

        assert result is False

    def test_returns_false_on_exception(self):
        _set_production(1)
        with patch("notification.Notification.TELEGRAM_LIVE_OPTIONS_TOKEN", "token123"), \
             patch("notification.Notification.TELEGRAM_LIVE_OPTIONS_CHAT_ID", "chat123"), \
             patch("notification.Notification.requests.post",
                   side_effect=Exception("network down")):
            result = TELEGRAM_NOTIFICATIONS.send_live_options_notification("alert")

        assert result is False
