"""Chat-ID filtering middleware for Telegram bot commands.

Provides two layers of access control:
  1. ``@guard`` — allows the command only if the chat is in the allowlist
     (TELEGRAM_ALLOWED_CHAT_IDS).  Applied to *all* command handlers.
  2. ``debug_chat_only()`` — additionally restricts debug commands to the
     dedicated debug chat (TELEGRAM_DEBUG_CHAT_ID).
"""
from __future__ import annotations

import os
from functools import wraps
from typing import Callable

from telegram import Update
from telegram.ext import ContextTypes

from common.constants import ENV_DEBUG_CHAT_ID, ENV_ALLOWED_CHAT_IDS

_allowed_chat_ids: set[int] = set()
_debug_chat_id: int | None = None
_initialised = False


def init_guard() -> None:
    """Parse env vars into sets.  Called once during bot startup."""
    global _allowed_chat_ids, _debug_chat_id, _initialised

    _allowed_chat_ids = set()
    _debug_chat_id = None
    raw_allowed = os.environ.get(ENV_ALLOWED_CHAT_IDS, "")
    for raw in raw_allowed.split(","):
        raw = raw.strip()
        if raw:
            try:
                _allowed_chat_ids.add(int(raw))
            except ValueError:
                pass

    raw_debug = os.environ.get(ENV_DEBUG_CHAT_ID, "")
    if raw_debug:
        try:
            _debug_chat_id = int(raw_debug)
            _allowed_chat_ids.add(_debug_chat_id)
        except ValueError:
            pass

    _initialised = True


def chat_allowed(update: Update) -> bool:
    """True if the chat that sent this update is in the allowlist."""
    if not _initialised:
        init_guard()
    if not _allowed_chat_ids:
        return True  # no allowlist configured — allow all (backward compat)
    return update.effective_chat.id in _allowed_chat_ids


def debug_chat_only(update: Update) -> bool:
    """True if this update came from the dedicated debug chat.

    If no debug chat is configured, falls back to the general allowlist.
    """
    if not _initialised:
        init_guard()
    if _debug_chat_id is None:
        return chat_allowed(update)
    return update.effective_chat.id == _debug_chat_id


def guard(fn: Callable) -> Callable:
    """Decorator: silently drop the update if the chat is not allowlisted."""
    @wraps(fn)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not chat_allowed(update):
            return
        return await fn(update, context)
    return wrapped
