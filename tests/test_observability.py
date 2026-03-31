"""
Unit tests for the 3-Layer Observability Architecture in intraday/intraday_monitor.py

Layer 1: Global Exception Catcher  (_crash_handler / sys.excepthook)
Layer 2: Heartbeat                 (_ping_healthcheck)
Layer 3: Zombie Data Watchdog      (check_data_freshness)

All network calls are patched — zero real HTTP traffic.
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock, ANY
from datetime import datetime, time


# ════════════════════════════════════════════════════════════════════════════
# Layer 1 — Global Exception Catcher
# ════════════════════════════════════════════════════════════════════════════

class TestCrashHandler(unittest.TestCase):
    """Verify _crash_handler formats tracebacks and calls Telegram."""

    def setUp(self):
        # Import here so the module-level sys.excepthook assignment runs
        import intraday.intraday_monitor as im
        self.im = im

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_sends_telegram_on_uncaught_exception(self, mock_tg):
        """A ValueError should produce a Telegram message with the traceback."""
        try:
            raise ValueError("test crash")
        except ValueError:
            exc_type, exc_value, exc_tb = sys.exc_info()
            self.im._crash_handler(exc_type, exc_value, exc_tb)

        mock_tg.send_notification.assert_called_once()
        msg = mock_tg.send_notification.call_args[0][0]
        self.assertIn("FATAL CRASH", msg)
        self.assertIn("ValueError", msg)
        self.assertIn("test crash", msg)
        self.assertIn("<pre>", msg)  # HTML formatting

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_html_parse_mode(self, mock_tg):
        """Message must be sent with parse_mode='HTML'."""
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            exc_type, exc_value, exc_tb = sys.exc_info()
            self.im._crash_handler(exc_type, exc_value, exc_tb)

        _, kwargs = mock_tg.send_notification.call_args
        self.assertEqual(kwargs.get("parse_mode"), "HTML")

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_keyboard_interrupt_not_sent(self, mock_tg):
        """KeyboardInterrupt should be forwarded to the default hook, not Telegram."""
        try:
            raise KeyboardInterrupt()
        except KeyboardInterrupt:
            exc_type, exc_value, exc_tb = sys.exc_info()
            with patch.object(sys, "__excepthook__") as mock_default:
                self.im._crash_handler(exc_type, exc_value, exc_tb)
                mock_default.assert_called_once()

        mock_tg.send_notification.assert_not_called()

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_truncates_long_tracebacks(self, mock_tg):
        """A traceback exceeding 3500 chars must be truncated."""
        # Simulate a very long exception message to force truncation
        long_msg = "x" * 5000
        try:
            raise RuntimeError(long_msg)
        except RuntimeError:
            exc_type, exc_value, exc_tb = sys.exc_info()
            self.im._crash_handler(exc_type, exc_value, exc_tb)

        msg = mock_tg.send_notification.call_args[0][0]
        # Full message must fit within ~4096 chars
        self.assertLess(len(msg), 4096)
        self.assertIn("truncated", msg)

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_survives_telegram_failure(self, mock_tg):
        """If Telegram itself is down, the handler must not raise."""
        mock_tg.send_notification.side_effect = Exception("network dead")

        try:
            raise RuntimeError("crash while offline")
        except RuntimeError:
            exc_type, exc_value, exc_tb = sys.exc_info()
            # Must not raise
            self.im._crash_handler(exc_type, exc_value, exc_tb)

    def test_excepthook_is_installed(self):
        """sys.excepthook must point to our crash handler after import."""
        self.assertIs(sys.excepthook, self.im._crash_handler)

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_angle_brackets_in_traceback_are_html_escaped(self, mock_tg):
        """Repr strings like <urllib3.HTTPSConnection object> must be escaped so
        Telegram's HTML parser doesn't return 400 'Unsupported start tag'."""
        class _FakeConnection:
            def __repr__(self):
                return "<urllib3.connection.HTTPSConnection object at 0x7f>"

        try:
            raise ConnectionError(f"Failed via {_FakeConnection()}")
        except ConnectionError:
            exc_type, exc_value, exc_tb = sys.exc_info()
            self.im._crash_handler(exc_type, exc_value, exc_tb)

        msg = mock_tg.send_notification.call_args[0][0]
        # Raw angle brackets must not appear inside the pre/code blocks
        # (the structural <pre>, <b>, <code> tags themselves are fine)
        import html
        # Strip structural tags so we only inspect escaped content
        self.assertIn("&lt;urllib3", msg)
        self.assertNotIn("<urllib3", msg)


# ════════════════════════════════════════════════════════════════════════════
# Layer 2 — Heartbeat (healthchecks.io)
# ════════════════════════════════════════════════════════════════════════════

class TestHeartbeat(unittest.TestCase):
    """Verify _ping_healthcheck sends a GET to the configured URL."""

    def setUp(self):
        import intraday.intraday_monitor as im
        self.im = im

    @patch.dict(os.environ, {"HEALTHCHECK_URL": "https://hc-ping.com/test-uuid"})
    @patch("requests.get")
    def test_pings_when_url_set(self, mock_get):
        """GET request must be made with timeout=5 when HEALTHCHECK_URL is set."""
        self.im._ping_healthcheck()
        mock_get.assert_called_once_with("https://hc-ping.com/test-uuid", timeout=5)

    @patch.dict(os.environ, {}, clear=True)
    @patch("requests.get")
    def test_no_op_when_url_not_set(self, mock_get):
        """Must silently return when HEALTHCHECK_URL is not in env."""
        # Remove HEALTHCHECK_URL if it's somehow present
        os.environ.pop("HEALTHCHECK_URL", None)
        self.im._ping_healthcheck()
        mock_get.assert_not_called()

    @patch.dict(os.environ, {"HEALTHCHECK_URL": "https://hc-ping.com/test-uuid"})
    @patch("requests.get")
    def test_suppresses_timeout(self, mock_get):
        """Network timeout must not propagate — the trading loop must survive."""
        import requests as req_lib
        mock_get.side_effect = req_lib.Timeout("timed out")
        # Must not raise
        self.im._ping_healthcheck()

    @patch.dict(os.environ, {"HEALTHCHECK_URL": "https://hc-ping.com/test-uuid"})
    @patch("requests.get")
    def test_suppresses_connection_error(self, mock_get):
        """ConnectionError must not propagate."""
        import requests as req_lib
        mock_get.side_effect = req_lib.ConnectionError("dns fail")
        # Must not raise
        self.im._ping_healthcheck()

    @patch.dict(os.environ, {"HEALTHCHECK_URL": "https://hc-ping.com/test-uuid"})
    @patch("requests.get")
    def test_suppresses_generic_exception(self, mock_get):
        """Any exception (e.g. SSL error) must be swallowed."""
        mock_get.side_effect = Exception("ssl handshake failed")
        self.im._ping_healthcheck()


# ════════════════════════════════════════════════════════════════════════════
# Layer 3 — Zombie Data Watchdog
# ════════════════════════════════════════════════════════════════════════════

class _FakeStock:
    """Lightweight mock for Stock with options_aggregate."""

    def __init__(self, symbol="NIFTY", last_updated=None):
        self.stock_symbol = symbol
        self.options_aggregate = {}
        if last_updated is not None:
            self.options_aggregate["last_updated"] = last_updated


class TestZombieDataWatchdog(unittest.TestCase):
    """Verify check_data_freshness correctly identifies stale vs fresh data."""

    def setUp(self):
        import intraday.intraday_monitor as im
        self.im = im
        # Reset the per-session alert tracker between tests
        self.im._stale_alerts_sent.clear()

    # ── Market hours helpers ──────────────────────────────────────────────

    def _patch_market_hours(self, in_hours=True, is_trading=True):
        """Return a context manager that fakes the time and calendar check."""
        if in_hours:
            fake_now = datetime(2026, 3, 30, 10, 30, 0)  # Monday 10:30 AM
        else:
            fake_now = datetime(2026, 3, 30, 16, 0, 0)   # Monday 4:00 PM

        patches = [
            patch("intraday.intraday_monitor.datetime", wraps=datetime),
            patch("intraday.intraday_monitor.isNowInTimePeriod", return_value=in_hours),
            patch("common.market_calendar.is_trading_day", return_value=is_trading),
        ]
        return patches, fake_now

    def _apply_patches(self, in_hours=True, is_trading=True):
        patches, fake_now = self._patch_market_hours(in_hours, is_trading)
        mocks = [p.start() for p in patches]
        # Make datetime.now() return our fake time
        mocks[0].now.return_value = fake_now
        # Keep isinstance() working — point the mock's class identity to real datetime
        mocks[0].side_effect = lambda *a, **kw: datetime(*a, **kw)
        self.addCleanup(lambda: [p.stop() for p in patches])
        return fake_now

    # ── Fresh data → no alert ─────────────────────────────────────────────

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_fresh_data_returns_true(self, mock_tg):
        """Data updated 30s ago (< 120s threshold) → True, no Telegram alert."""
        fake_now = self._apply_patches(in_hours=True)
        stock = _FakeStock("NIFTY", last_updated=fake_now.timestamp() - 30)

        result = self.im.check_data_freshness(stock)
        self.assertTrue(result)
        mock_tg.send_notification.assert_not_called()

    # ── Stale data → alert ────────────────────────────────────────────────

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_stale_data_returns_false_and_alerts(self, mock_tg):
        """Data updated 200s ago (> 120s threshold) → False + Telegram warning."""
        fake_now = self._apply_patches(in_hours=True)
        stock = _FakeStock("NIFTY", last_updated=fake_now.timestamp() - 200)

        result = self.im.check_data_freshness(stock)
        self.assertFalse(result)
        mock_tg.send_notification.assert_called_once()
        msg = mock_tg.send_notification.call_args[0][0]
        self.assertIn("STALE DATA", msg)
        self.assertIn("NIFTY", msg)

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_stale_alert_sent_only_once_per_symbol(self, mock_tg):
        """Duplicate stale alerts for the same symbol must be suppressed."""
        fake_now = self._apply_patches(in_hours=True)
        stock = _FakeStock("NIFTY", last_updated=fake_now.timestamp() - 200)

        self.im.check_data_freshness(stock)
        self.im.check_data_freshness(stock)  # second call
        self.assertEqual(mock_tg.send_notification.call_count, 1)

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_different_symbols_both_alerted(self, mock_tg):
        """Each stale symbol gets its own alert."""
        fake_now = self._apply_patches(in_hours=True)
        nifty = _FakeStock("NIFTY", last_updated=fake_now.timestamp() - 200)
        bank = _FakeStock("BANKNIFTY", last_updated=fake_now.timestamp() - 300)

        self.im.check_data_freshness(nifty)
        self.im.check_data_freshness(bank)
        self.assertEqual(mock_tg.send_notification.call_count, 2)

    # ── Outside market hours → skip ───────────────────────────────────────

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_outside_market_hours_returns_true(self, mock_tg):
        """After 3:30 PM, stale data should be ignored (market closed)."""
        self._apply_patches(in_hours=False)
        stock = _FakeStock("NIFTY", last_updated=0)  # extremely stale

        result = self.im.check_data_freshness(stock)
        self.assertTrue(result)
        mock_tg.send_notification.assert_not_called()

    # ── Holiday → skip ────────────────────────────────────────────────────

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_holiday_returns_true(self, mock_tg):
        """On a market holiday, stale data should not trigger an alert."""
        self._apply_patches(in_hours=True, is_trading=False)
        stock = _FakeStock("NIFTY", last_updated=0)

        result = self.im.check_data_freshness(stock)
        self.assertTrue(result)
        mock_tg.send_notification.assert_not_called()

    # ── No options_aggregate → skip ───────────────────────────────────────

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_no_options_aggregate_returns_true(self, mock_tg):
        """Stock without options_aggregate (non-index equity) → pass through."""
        self._apply_patches(in_hours=True)
        stock = MagicMock()
        stock.options_aggregate = None

        result = self.im.check_data_freshness(stock)
        self.assertTrue(result)
        mock_tg.send_notification.assert_not_called()

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_no_last_updated_field_returns_true(self, mock_tg):
        """options_aggregate exists but last_updated not yet set → pass."""
        self._apply_patches(in_hours=True)
        stock = _FakeStock("NIFTY", last_updated=None)
        stock.options_aggregate = {}  # exists but no timestamp

        result = self.im.check_data_freshness(stock)
        self.assertTrue(result)
        mock_tg.send_notification.assert_not_called()

    # ── Custom threshold ──────────────────────────────────────────────────

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_custom_threshold(self, mock_tg):
        """With a 60s threshold, 90s-old data should be stale."""
        fake_now = self._apply_patches(in_hours=True)
        stock = _FakeStock("NIFTY", last_updated=fake_now.timestamp() - 90)

        result = self.im.check_data_freshness(stock, stale_threshold_sec=60)
        self.assertFalse(result)
        mock_tg.send_notification.assert_called_once()

    # ── datetime-typed last_updated ───────────────────────────────────────

    @patch("intraday.intraday_monitor.TELEGRAM_NOTIFICATIONS")
    def test_datetime_typed_last_updated_fresh(self, mock_tg):
        """last_updated as a datetime object (not epoch) should also work."""
        fake_now = self._apply_patches(in_hours=True)
        stock = _FakeStock("NIFTY")
        # 15 seconds ago — well within 120s threshold
        from datetime import timedelta
        stock.options_aggregate["last_updated"] = fake_now - timedelta(seconds=15)

        result = self.im.check_data_freshness(stock)
        self.assertTrue(result)
        mock_tg.send_notification.assert_not_called()


if __name__ == "__main__":
    unittest.main()
