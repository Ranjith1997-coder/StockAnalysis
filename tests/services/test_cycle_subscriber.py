"""Tests for services/common/cycle_subscriber.py — CycleSubscriber."""
import threading
import time
import pytest
from unittest.mock import MagicMock, patch, call


# ═══════════════════════════════════════════════════════════════════════════
# Construction & Properties
# ═══════════════════════════════════════════════════════════════════════════

class TestConstruction:
    """Test CycleSubscriber initial state."""

    def test_initial_state(self):
        from services.common.cycle_subscriber import CycleSubscriber
        sub = CycleSubscriber(MagicMock())
        assert sub._running is False
        assert sub._cycle_count == 0
        assert sub._last_cycle_id == "0"
        assert sub._sub_thread is None

    def test_cycle_count_property(self):
        from services.common.cycle_subscriber import CycleSubscriber
        sub = CycleSubscriber(MagicMock())
        sub._cycle_count = 42
        assert sub.cycle_count == 42

    def test_custom_consumer_name(self):
        from services.common.cycle_subscriber import CycleSubscriber
        sub = CycleSubscriber(MagicMock(), consumer_name="prod-2")
        assert sub._consumer == "prod-2"

    def test_default_consumer_name(self):
        from services.common.cycle_subscriber import CycleSubscriber
        sub = CycleSubscriber(MagicMock())
        assert sub._consumer == "prod-1"


# ═══════════════════════════════════════════════════════════════════════════
# wait_for_cycle
# ═══════════════════════════════════════════════════════════════════════════

class TestWaitForCycle:
    """Test wait_for_cycle()."""

    def test_returns_true_when_event_set(self):
        from services.common.cycle_subscriber import CycleSubscriber
        sub = CycleSubscriber(MagicMock())
        sub._event.set()
        result = sub.wait_for_cycle(timeout=0.1)
        assert result is True

    def test_clears_event_after_successful_wait(self):
        from services.common.cycle_subscriber import CycleSubscriber
        sub = CycleSubscriber(MagicMock())
        sub._event.set()
        sub.wait_for_cycle(timeout=0.1)
        assert sub._event.is_set() is False

    def test_returns_false_on_timeout(self):
        from services.common.cycle_subscriber import CycleSubscriber
        sub = CycleSubscriber(MagicMock())
        result = sub.wait_for_cycle(timeout=0.05)
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════
# stop
# ═══════════════════════════════════════════════════════════════════════════

class TestStop:
    """Test stop()."""

    def test_stop_sets_running_false(self):
        from services.common.cycle_subscriber import CycleSubscriber
        sub = CycleSubscriber(MagicMock())
        sub._running = True
        sub.stop()
        assert sub._running is False

    def test_stop_sets_event(self):
        from services.common.cycle_subscriber import CycleSubscriber
        sub = CycleSubscriber(MagicMock())
        sub.stop()
        assert sub._event.is_set() is True


# ═══════════════════════════════════════════════════════════════════════════
# _ensure_consumer_group
# ═══════════════════════════════════════════════════════════════════════════

class TestEnsureConsumerGroup:
    """Test _ensure_consumer_group()."""

    def test_calls_xgroup_create(self):
        from services.common.cycle_subscriber import CycleSubscriber, CYCLE_GROUP, CYCLE_STREAM
        redis = MagicMock()
        sub = CycleSubscriber(redis)
        sub._ensure_consumer_group()
        redis.xgroup_create.assert_called_once_with(CYCLE_GROUP, CYCLE_STREAM, mkstream=True)

    def test_swallows_exception_on_existing_group(self):
        from services.common.cycle_subscriber import CycleSubscriber
        redis = MagicMock()
        redis.xgroup_create.side_effect = Exception("BUSYGROUP Consumer Group name already exists")
        sub = CycleSubscriber(redis)
        # Should not raise
        sub._ensure_consumer_group()


# ═══════════════════════════════════════════════════════════════════════════
# catch_up_on_startup
# ═══════════════════════════════════════════════════════════════════════════

class TestCatchUpOnStartup:
    """Test catch_up_on_startup()."""

    def test_returns_zero_when_no_pending_cycles(self):
        from services.common.cycle_subscriber import CycleSubscriber
        redis = MagicMock()
        redis.xreadgroup.return_value = []
        sub = CycleSubscriber(redis)
        sub._running = True
        result = sub.catch_up_on_startup(timeout=0.1)
        assert result == 0

    def test_returns_count_and_updates_cycle_count(self):
        from services.common.cycle_subscriber import CycleSubscriber, CYCLE_STREAM
        redis = MagicMock()
        redis.xreadgroup.return_value = [
            (CYCLE_STREAM, [("1234-0", {"cycle": "42", "timestamp": "2026-07-12"})])
        ]
        sub = CycleSubscriber(redis)
        sub._running = True
        result = sub.catch_up_on_startup(timeout=0.5)
        assert result == 1
        assert sub._cycle_count == 42

    def test_acks_message_after_reading(self):
        from services.common.cycle_subscriber import CycleSubscriber, CYCLE_STREAM, CYCLE_GROUP
        redis = MagicMock()
        msg_id = "1234-0"
        redis.xreadgroup.return_value = [
            (CYCLE_STREAM, [(msg_id, {"cycle": "5"})])
        ]
        sub = CycleSubscriber(redis)
        sub._running = True
        sub.catch_up_on_startup(timeout=0.5)
        redis.xack.assert_called_once_with(CYCLE_STREAM, CYCLE_GROUP, msg_id)

    def test_handles_non_dict_fields(self):
        from services.common.cycle_subscriber import CycleSubscriber, CYCLE_STREAM
        redis = MagicMock()
        redis.xreadgroup.return_value = [
            (CYCLE_STREAM, [("1234-0", "not_a_dict")])
        ]
        sub = CycleSubscriber(redis)
        sub._running = True
        # Should not crash — non-dict entries are skipped
        result = sub.catch_up_on_startup(timeout=0.5)
        assert result == 0  # no dict entries processed

    def test_stops_when_running_false(self):
        from services.common.cycle_subscriber import CycleSubscriber
        redis = MagicMock()
        redis.xreadgroup.return_value = []
        sub = CycleSubscriber(redis)
        sub._running = False
        # Should return quickly without looping
        result = sub.catch_up_on_startup(timeout=5.0)
        assert result == 0

    def test_updates_last_cycle_id(self):
        from services.common.cycle_subscriber import CycleSubscriber, CYCLE_STREAM
        redis = MagicMock()
        msg_id = "9999-1"
        redis.xreadgroup.return_value = [
            (CYCLE_STREAM, [(msg_id, {"cycle": "10"})])
        ]
        sub = CycleSubscriber(redis)
        sub._running = True
        sub.catch_up_on_startup(timeout=0.5)
        assert sub._last_cycle_id == msg_id

    def test_default_cycle_count_when_missing_field(self):
        from services.common.cycle_subscriber import CycleSubscriber, CYCLE_STREAM
        redis = MagicMock()
        redis.xreadgroup.return_value = [
            (CYCLE_STREAM, [("1234-0", {"timestamp": "2026-07-12"})])
        ]
        sub = CycleSubscriber(redis)
        sub._running = True
        sub.catch_up_on_startup(timeout=0.5)
        # "cycle" key missing → defaults to int("0") = 0
        assert sub._cycle_count == 0


# ═══════════════════════════════════════════════════════════════════════════
# _pubsub_loop — payload parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestPubsubLoop:
    """Test _pubsub_loop() payload parsing via mocked pubsub."""

    def _make_sub_with_pubsub(self, messages):
        """Create a CycleSubscriber whose redis.pubsub().listen() yields the given messages.

        After the last message, listen() raises StopIteration to break the loop.
        We also set _running=False after each message to ensure the loop exits.
        """
        from services.common.cycle_subscriber import CycleSubscriber

        redis = MagicMock()
        ps = MagicMock()
        ps.listen.side_effect = lambda: iter(messages)
        redis.pubsub.return_value = ps

        sub = CycleSubscriber(redis)
        sub._running = True
        return sub, ps

    def test_parses_cycle_count_from_payload(self):
        from services.common.cycle_subscriber import CycleSubscriber
        messages = [
            {"type": "subscribe", "data": 1},
            {"type": "message", "data": "cycle=5,ts=2026-07-12T10:00:00"},
        ]
        sub, ps = self._make_sub_with_pubsub(messages)

        # Run the loop — it will process messages then hit StopIteration
        # But we need _running=False to break cleanly. Set it after first message.
        original_listen = ps.listen.side_effect

        def controlled_listen():
            for msg in messages:
                if msg["type"] == "message":
                    # Process this one then stop
                    yield msg
                    sub._running = False
                    return
                yield msg

        ps.listen.side_effect = controlled_listen
        sub._pubsub_loop()

        assert sub._cycle_count == 5

    def test_parses_bytes_payload(self):
        from services.common.cycle_subscriber import CycleSubscriber
        messages = [
            {"type": "message", "data": b"cycle=10,ts=2026-07-12"},
        ]
        sub, ps = self._make_sub_with_pubsub(messages)

        def controlled_listen():
            for msg in messages:
                yield msg
                sub._running = False
                return

        ps.listen.side_effect = controlled_listen
        sub._pubsub_loop()

        assert sub._cycle_count == 10

    def test_sets_event_on_message(self):
        from services.common.cycle_subscriber import CycleSubscriber
        messages = [{"type": "message", "data": "cycle=1"}]
        sub, ps = self._make_sub_with_pubsub(messages)

        def controlled_listen():
            for msg in messages:
                yield msg
                sub._running = False
                return

        ps.listen.side_effect = controlled_listen
        assert sub._event.is_set() is False
        sub._pubsub_loop()
        assert sub._event.is_set() is True

    def test_ignores_non_message_types(self):
        from services.common.cycle_subscriber import CycleSubscriber
        messages = [
            {"type": "subscribe", "data": 1},
            {"type": "message", "data": "cycle=3"},
        ]
        sub, ps = self._make_sub_with_pubsub(messages)

        def controlled_listen():
            for msg in messages:
                yield msg
                if msg["type"] == "message":
                    sub._running = False
                    return

        ps.listen.side_effect = controlled_listen
        sub._pubsub_loop()
        # subscribe message should not set event or update cycle_count
        assert sub._cycle_count == 3

    def test_malformed_payload_does_not_crash(self):
        from services.common.cycle_subscriber import CycleSubscriber
        messages = [{"type": "message", "data": "garbage_no_equals"}]
        sub, ps = self._make_sub_with_pubsub(messages)

        def controlled_listen():
            for msg in messages:
                yield msg
                sub._running = False
                return

        ps.listen.side_effect = controlled_listen
        # Should not raise
        sub._pubsub_loop()
        # Event still set (wake-up signal regardless of payload parsing)
        assert sub._event.is_set() is True

    def test_handles_non_numeric_cycle(self):
        from services.common.cycle_subscriber import CycleSubscriber
        messages = [{"type": "message", "data": "cycle=abc"}]
        sub, ps = self._make_sub_with_pubsub(messages)
        sub._cycle_count = 7  # pre-set to verify it's not corrupted

        def controlled_listen():
            for msg in messages:
                yield msg
                sub._running = False
                return

        ps.listen.side_effect = controlled_listen
        sub._pubsub_loop()
        # ValueError caught — cycle_count unchanged
        assert sub._cycle_count == 7

    def test_exits_when_running_false(self):
        from services.common.cycle_subscriber import CycleSubscriber
        messages = []  # empty — no messages at all
        sub, ps = self._make_sub_with_pubsub(messages)
        sub._running = False

        # With _running=False, the loop should check and break immediately
        # But listen() is a generator — the for loop starts before the check
        # Actually the loop checks `if not self._running: break` at the top of each iteration
        ps.listen.side_effect = lambda: iter([])
        sub._pubsub_loop()
        # No crash, no messages processed

    def test_unsubscribes_on_exit(self):
        from services.common.cycle_subscriber import CycleSubscriber
        messages = [{"type": "message", "data": "cycle=1"}]
        sub, ps = self._make_sub_with_pubsub(messages)

        def controlled_listen():
            for msg in messages:
                yield msg
                sub._running = False
                return

        ps.listen.side_effect = controlled_listen
        sub._pubsub_loop()
        ps.unsubscribe.assert_called_once()
        ps.close.assert_called_once()

    def test_pubsub_error_does_not_crash(self):
        from services.common.cycle_subscriber import CycleSubscriber
        redis = MagicMock()
        ps = MagicMock()
        ps.listen.side_effect = Exception("Connection lost")
        redis.pubsub.return_value = ps

        sub = CycleSubscriber(redis)
        sub._running = True
        # Should catch the exception and exit gracefully
        sub._pubsub_loop()
        # unsubscribe still called in finally
        ps.unsubscribe.assert_called_once()
