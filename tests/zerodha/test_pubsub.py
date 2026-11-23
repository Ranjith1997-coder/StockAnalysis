"""Tests for the shared Pub/Sub listener (lib/zerodha/pubsub.py)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import redis as redis_lib

import lib.zerodha.pubsub as pubsub_module
from lib.zerodha.pubsub import listen_until_closed


class _FakePubSub:
    def __init__(self, messages, error=None):
        self._messages = list(messages)
        self._error = error
        self.closed = False

    def listen(self):
        yield from self._messages
        if self._error is not None:
            raise self._error

    def close(self):
        self.closed = True


def test_redis_error_on_close_is_quiet():
    logger = MagicMock()
    pubsub = _FakePubSub(
        [{"type": "message", "data": "a"}],
        error=redis_lib.exceptions.ConnectionError("Connection closed by server."),
    )
    seen = []

    listen_until_closed(pubsub, seen.append, logger=logger, label="test")

    assert seen == [{"type": "message", "data": "a"}]
    assert pubsub.closed
    logger.debug.assert_called_once()
    logger.error.assert_not_called()


def test_subscribe_messages_are_forwarded():
    logger = MagicMock()
    pubsub = _FakePubSub([{"type": "subscribe"}, {"type": "message", "data": "b"}])
    seen = []

    listen_until_closed(pubsub, seen.append, logger=logger, label="test")

    assert seen == [{"type": "subscribe"}, {"type": "message", "data": "b"}]


def test_handler_exceptions_are_not_swallowed():
    logger = MagicMock()
    pubsub = _FakePubSub([{"type": "message", "data": "x"}])

    def boom(_message):
        raise RuntimeError("handler bug")

    with pytest.raises(RuntimeError, match="handler bug"):
        listen_until_closed(pubsub, boom, logger=logger, label="test")
    assert pubsub.closed


def test_non_redis_errors_propagate():
    logger = MagicMock()
    pubsub = _FakePubSub([], error=ValueError("not a redis error"))

    with pytest.raises(ValueError, match="not a redis error"):
        listen_until_closed(pubsub, lambda _m: None, logger=logger, label="test")


def test_chained_redis_error_is_swallowed():
    """The generator-unwind exception is often generic, with Redis as context."""
    logger = MagicMock()
    error = RuntimeError("generator ignored GeneratorExit")
    error.__context__ = redis_lib.exceptions.ConnectionError("closed")
    pubsub = _FakePubSub([], error=error)

    listen_until_closed(pubsub, lambda _m: None, logger=logger, label="test")

    assert pubsub.closed


def test_should_stop_marks_any_error_as_shutdown():
    logger = MagicMock()
    pubsub = _FakePubSub([], error=ValueError("misc"))

    listen_until_closed(pubsub, lambda _m: None, logger=logger,
                        label="test", should_stop=lambda: True)

    assert pubsub.closed


def test_no_logging_during_interpreter_shutdown(monkeypatch):
    """Logging after handlers close raises and re-prints the traceback."""
    monkeypatch.setattr(pubsub_module.sys, "is_finalizing", lambda: True)
    logger = MagicMock()
    pubsub = _FakePubSub(
        [], error=redis_lib.exceptions.ConnectionError("Connection closed by server.")
    )

    listen_until_closed(pubsub, lambda _m: None, logger=logger, label="test")

    logger.debug.assert_not_called()
    assert pubsub.closed


def test_a_failing_logger_never_breaks_shutdown():
    logger = MagicMock()
    logger.debug.side_effect = ValueError("I/O operation on closed file")
    pubsub = _FakePubSub(
        [], error=redis_lib.exceptions.ConnectionError("Connection closed by server.")
    )

    listen_until_closed(pubsub, lambda _m: None, logger=logger, label="test")

    assert pubsub.closed
