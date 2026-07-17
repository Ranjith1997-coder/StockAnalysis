"""Tests for services/auth_service/main.py — enctoken lifecycle manager."""
import time
import pytest
from unittest.mock import MagicMock, patch, call
from datetime import datetime, time as dtime


# ═══════════════════════════════════════════════════════════════════════════
# _do_refresh
# ═══════════════════════════════════════════════════════════════════════════

class TestDoRefresh:
    """Test _do_refresh() — TOTP login + Redis publish."""

    @patch("services.auth_service.main.load_dotenv")
    @patch("services.auth_service.main.os.getenv")
    @patch("auth.auth_login.generate_enctoken")
    def test_success_publishes_enctoken(self, mock_gen, mock_getenv, mock_load):
        from services.auth_service.main import _do_refresh, AUTH_HASH, AUTH_CHANNEL

        mock_gen.return_value = (True, MagicMock())
        mock_getenv.side_effect = lambda key, default=None: {
            "ZERODHA_ENC_TOKEN": "fresh_token_abc123",
            "ZERODHA_USER": "user123",
        }.get(key, default or "")

        redis = MagicMock()
        result = _do_refresh(redis, reason="scheduled_morning")

        assert result is True
        # Verify HSET with correct fields
        redis.hset.assert_called_once()
        call_args = redis.hset.call_args
        assert call_args[0][0] == AUTH_HASH
        mapping = call_args[1]["mapping"]
        assert mapping["enctoken"] == "fresh_token_abc123"
        assert mapping["user_id"] == "user123"
        assert mapping["last_reason"] == "scheduled_morning"
        assert "issued_at" in mapping
        # Verify PUBLISH
        redis.publish.assert_called_once()
        pub_args = redis.publish.call_args
        assert pub_args[0][0] == AUTH_CHANNEL

    @patch("auth.auth_login.generate_enctoken")
    def test_failure_sends_alert(self, mock_gen):
        from services.auth_service.main import _do_refresh

        mock_gen.return_value = (False, None)
        redis = MagicMock()
        result = _do_refresh(redis, reason="scheduled_morning")

        assert result is False
        # Should NOT publish enctoken
        redis.hset.assert_not_called()
        redis.publish.assert_not_called()
        # Should send alert via notification:jobs
        redis.xadd.assert_called_once()
        xadd_args = redis.xadd.call_args
        assert xadd_args[0][0] == "notification:jobs"

    @patch("auth.auth_login.generate_enctoken")
    def test_exception_handled(self, mock_gen):
        from services.auth_service.main import _do_refresh

        mock_gen.side_effect = Exception("Network error")
        redis = MagicMock()
        result = _do_refresh(redis, reason="reactive:403")

        assert result is False
        redis.hset.assert_not_called()
        # Should send alert
        redis.xadd.assert_called_once()

    @patch("services.auth_service.main.load_dotenv")
    @patch("services.auth_service.main.os.getenv")
    @patch("auth.auth_login.generate_enctoken")
    def test_missing_enctoken_after_login(self, mock_gen, mock_getenv, mock_load):
        from services.auth_service.main import _do_refresh

        mock_gen.return_value = (True, MagicMock())
        mock_getenv.side_effect = lambda key, default=None: {
            "ZERODHA_ENC_TOKEN": None,  # missing!
            "ZERODHA_USER": "user123",
        }.get(key, default or "")

        redis = MagicMock()
        result = _do_refresh(redis, reason="startup")

        assert result is False
        redis.hset.assert_not_called()
        redis.publish.assert_not_called()
        # Should send alert
        redis.xadd.assert_called_once()

    @patch("services.auth_service.main.load_dotenv")
    @patch("services.auth_service.main.os.getenv")
    @patch("auth.auth_login.generate_enctoken")
    def test_publishes_correct_hash_fields(self, mock_gen, mock_getenv, mock_load):
        from services.auth_service.main import _do_refresh, AUTH_HASH

        mock_gen.return_value = (True, MagicMock())
        mock_getenv.side_effect = lambda key, default=None: {
            "ZERODHA_ENC_TOKEN": "tok_123",
            "ZERODHA_USER": "my_user",
        }.get(key, default or "")

        redis = MagicMock()
        _do_refresh(redis, reason="scheduled_evening")

        mapping = redis.hset.call_args[1]["mapping"]
        assert set(mapping.keys()) == {"enctoken", "issued_at", "user_id", "last_reason"}
        assert mapping["enctoken"] == "tok_123"
        assert mapping["user_id"] == "my_user"
        assert mapping["last_reason"] == "scheduled_evening"

    @patch("services.auth_service.main.load_dotenv")
    @patch("services.auth_service.main.os.getenv")
    @patch("auth.auth_login.generate_enctoken")
    def test_publishes_pubsub_channel(self, mock_gen, mock_getenv, mock_load):
        from services.auth_service.main import _do_refresh, AUTH_CHANNEL

        mock_gen.return_value = (True, MagicMock())
        mock_getenv.side_effect = lambda key, default=None: {
            "ZERODHA_ENC_TOKEN": "tok_123",
            "ZERODHA_USER": "user",
        }.get(key, default or "")

        redis = MagicMock()
        _do_refresh(redis, reason="test")

        channel = redis.publish.call_args[0][0]
        assert channel == AUTH_CHANNEL


# ═══════════════════════════════════════════════════════════════════════════
# _send_alert
# ═══════════════════════════════════════════════════════════════════════════

class TestSendAlert:
    """Test _send_alert()."""

    def test_sends_to_notification_jobs(self):
        from services.auth_service.main import _send_alert

        redis = MagicMock()
        _send_alert(redis, "Test alert message")

        redis.xadd.assert_called_once()
        args = redis.xadd.call_args
        assert args[0][0] == "notification:jobs"
        fields = args[0][1]
        assert "Test alert message" in fields["message"]
        assert fields["parse_mode"] == "HTML"
        assert fields["message_type"] == "auth_alert"

    def test_swallows_redis_error(self):
        from services.auth_service.main import _send_alert

        redis = MagicMock()
        redis.xadd.side_effect = Exception("Redis down")
        # Should not raise
        _send_alert(redis, "Test alert")


# ═══════════════════════════════════════════════════════════════════════════
# _update_heartbeat
# ═══════════════════════════════════════════════════════════════════════════

class TestHeartbeat:
    """Test _update_heartbeat()."""

    def test_writes_version_fields(self):
        from services.auth_service.main import _update_heartbeat

        redis = MagicMock()
        _update_heartbeat(redis)

        redis.hset.assert_called_once()
        call_args = redis.hset.call_args
        assert call_args[0][0] == "service:registry:auth-service"
        mapping = call_args[1]["mapping"]
        assert "version" in mapping
        assert "commit" in mapping
        assert "dirty" in mapping
        assert mapping["status"] == "healthy"

    def test_sets_ttl(self):
        from services.auth_service.main import _update_heartbeat

        redis = MagicMock()
        _update_heartbeat(redis)
        redis.expire.assert_called_once_with("service:registry:auth-service", 120)


# ═══════════════════════════════════════════════════════════════════════════
# Auth commands consumer
# ═══════════════════════════════════════════════════════════════════════════

class TestAuthCommandsConsumer:
    """Test _start_auth_commands_consumer() logic."""

    def test_ensures_consumer_group(self):
        from services.auth_service.main import _start_auth_commands_consumer, AUTH_COMMANDS_GROUP, AUTH_COMMANDS_STREAM

        redis = MagicMock()
        redis.xreadgroup.return_value = []
        _start_auth_commands_consumer(redis)
        redis.xgroup_create.assert_called_once_with(
            AUTH_COMMANDS_GROUP, AUTH_COMMANDS_STREAM, mkstream=True,
        )

    def test_swallows_existing_group_error(self):
        from services.auth_service.main import _start_auth_commands_consumer

        redis = MagicMock()
        redis.xgroup_create.side_effect = Exception("BUSYGROUP")
        redis.xreadgroup.return_value = []
        # Should not raise
        _start_auth_commands_consumer(redis)


# ═══════════════════════════════════════════════════════════════════════════
# Scheduling helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestScheduling:
    """Test scheduling helpers."""

    def test_wait_until_returns_immediately_if_past(self):
        from services.auth_service.main import _wait_until
        # Target time is in the past (00:01 today)
        with patch("services.auth_service.main.time.sleep") as mock_sleep:
            _wait_until(0, 1)
            mock_sleep.assert_not_called()

    def test_wait_until_sleeps_if_future(self):
        from services.auth_service import main as auth_main

        auth_main._running = True
        with patch("services.auth_service.main.time.sleep") as mock_sleep:
            # Set _running=False after first sleep to break the while loop
            mock_sleep.side_effect = lambda *a: setattr(auth_main, "_running", False)
            auth_main._wait_until(23, 59)
            mock_sleep.assert_called()


# ═══════════════════════════════════════════════════════════════════════════
# _run_schedule logic (tested via mocking, not full execution)
# ═══════════════════════════════════════════════════════════════════════════

class TestRunSchedule:
    """Test _run_schedule() decision logic with mocked time."""

    @patch("services.auth_service.main._do_refresh")
    @patch("services.auth_service.main._wait_until")
    @patch("services.auth_service.main._sleep_until_midnight")
    @patch("services.auth_service.main._update_heartbeat")
    def test_morning_refresh_before_0915(self, mock_hb, mock_midnight, mock_wait, mock_refresh):
        from services.auth_service import main as auth_main

        call_count = [0]
        def fake_now():
            call_count[0] += 1
            if call_count[0] <= 2:  # init + first loop iteration
                return datetime(2026, 7, 13, 8, 0)  # 08:00 → morning branch
            else:
                auth_main._running = False
                return datetime(2026, 7, 13, 10, 0)  # 10:00 → evening branch, but _running=False

        with patch.object(auth_main, "datetime") as mock_dt:
            mock_dt.now.side_effect = fake_now
            auth_main._running = True
            redis = MagicMock()
            auth_main._run_schedule(redis)

        mock_wait.assert_any_call(9, 0)
        mock_refresh.assert_called_once()
        assert mock_refresh.call_args[1]["reason"] == "scheduled_morning"

    @patch("services.auth_service.main._do_refresh")
    @patch("services.auth_service.main._wait_until")
    @patch("services.auth_service.main._sleep_until_midnight")
    @patch("services.auth_service.main._update_heartbeat")
    def test_evening_refresh_between_0915_and_1850(self, mock_hb, mock_midnight, mock_wait, mock_refresh):
        from services.auth_service import main as auth_main

        call_count = [0]
        def fake_now():
            call_count[0] += 1
            if call_count[0] <= 2:  # init + first loop iteration
                return datetime(2026, 7, 13, 12, 0)  # noon → evening branch
            else:
                auth_main._running = False
                return datetime(2026, 7, 13, 20, 0)  # 8 PM → midnight branch

        with patch.object(auth_main, "datetime") as mock_dt:
            mock_dt.now.side_effect = fake_now
            auth_main._running = True
            redis = MagicMock()
            auth_main._run_schedule(redis)

        mock_wait.assert_any_call(18, 50)
        mock_refresh.assert_called_once()
        assert mock_refresh.call_args[1]["reason"] == "scheduled_evening"

    @patch("services.auth_service.main._do_refresh")
    @patch("services.auth_service.main._wait_until")
    @patch("services.auth_service.main._sleep_until_midnight")
    @patch("services.auth_service.main._update_heartbeat")
    def test_past_1850_sleeps_until_midnight(self, mock_hb, mock_midnight, mock_wait, mock_refresh):
        from services.auth_service import main as auth_main

        call_count = [0]
        def fake_now():
            call_count[0] += 1
            if call_count[0] <= 2:  # init + first loop iteration
                return datetime(2026, 7, 13, 20, 0)  # 8 PM → midnight branch
            else:
                auth_main._running = False
                return datetime(2026, 7, 13, 20, 0)

        with patch.object(auth_main, "datetime") as mock_dt:
            mock_dt.now.side_effect = fake_now
            auth_main._running = True
            redis = MagicMock()
            auth_main._run_schedule(redis)

        mock_midnight.assert_called()
        mock_refresh.assert_not_called()
