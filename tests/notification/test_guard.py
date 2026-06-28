"""Tests for notification/commands/_guard.py — chat-ID filtering middleware."""
import os
import pytest
from unittest.mock import MagicMock

from notification.commands._guard import (
    init_guard,
    chat_allowed,
    debug_chat_only,
    guard,
)


class TestInitGuard:

    def test_parses_allowed_chat_ids(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-100,200,300")
        monkeypatch.setenv("TELEGRAM_DEBUG_CHAT_ID", "-100")
        init_guard()
        assert chat_allowed(_make_update(-100)) is True
        assert chat_allowed(_make_update(200)) is True
        assert chat_allowed(_make_update(999)) is False

    def test_empty_allowlist_allows_all(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
        monkeypatch.setenv("TELEGRAM_DEBUG_CHAT_ID", "")
        init_guard()
        assert chat_allowed(_make_update(12345)) is True

    def test_debug_chat_id_auto_added_to_allowlist(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "200")
        monkeypatch.setenv("TELEGRAM_DEBUG_CHAT_ID", "-999")
        init_guard()
        assert chat_allowed(_make_update(-999)) is True


class TestDebugChatOnly:

    def test_restricted_to_debug_chat(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-100,200")
        monkeypatch.setenv("TELEGRAM_DEBUG_CHAT_ID", "-100")
        init_guard()
        assert debug_chat_only(_make_update(-100)) is True
        assert debug_chat_only(_make_update(200)) is False

    def test_no_debug_chat_falls_back_to_allowlist(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "200")
        monkeypatch.setenv("TELEGRAM_DEBUG_CHAT_ID", "")
        init_guard()
        assert debug_chat_only(_make_update(200)) is True


class TestGuardDecorator:

    @pytest.mark.asyncio
    async def test_guard_blocks_unauthorized_chat(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-100")
        monkeypatch.setenv("TELEGRAM_DEBUG_CHAT_ID", "")
        init_guard()

        called = False

        @guard
        async def handler(update, context):
            nonlocal called
            called = True

        await handler(_make_update(999), None)
        assert called is False

    @pytest.mark.asyncio
    async def test_guard_allows_authorized_chat(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "-100")
        monkeypatch.setenv("TELEGRAM_DEBUG_CHAT_ID", "")
        init_guard()

        called = False

        @guard
        async def handler(update, context):
            nonlocal called
            called = True

        await handler(_make_update(-100), None)
        assert called is True


def _make_update(chat_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    return update
