"""Minimal in-memory Redis stand-in for the offline backtester.

The backtester calls the *real* ``build_position`` / ``evaluate_position`` /
``check_entry_filters``; this object implements only the handful of commands
those functions use. Any other attribute access raises ``AttributeError``, so a
future engine change that starts reading a new key fails loudly here instead of
silently returning an empty value.
"""
from __future__ import annotations

import fnmatch
from typing import Iterable


class ReplayRedis:
    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._strings: dict[str, str] = {}

    # ── hashes ───────────────────────────────────────────────────────────

    def hset(self, name: str, key: str | None = None, value=None, mapping: dict | None = None) -> int:
        target = self._hashes.setdefault(name, {})
        added = 0
        if mapping is not None:
            for field, field_value in mapping.items():
                if field not in target:
                    added += 1
                target[field] = "" if field_value is None else str(field_value)
        if key is not None:
            if key not in target:
                added += 1
            target[key] = "" if value is None else str(value)
        return added

    def hget(self, name: str, key: str) -> str | None:
        return self._hashes.get(name, {}).get(key)

    def hgetall(self, name: str) -> dict:
        return dict(self._hashes.get(name, {}))

    def hdel(self, name: str, *keys: str) -> int:
        target = self._hashes.get(name, {})
        removed = 0
        for key in keys:
            if key in target:
                del target[key]
                removed += 1
        return removed

    def hlen(self, name: str) -> int:
        return len(self._hashes.get(name, {}))

    # ── strings / keys ───────────────────────────────────────────────────

    def get(self, name: str) -> str | None:
        return self._strings.get(name)

    def set(self, name: str, value) -> bool:
        self._strings[name] = "" if value is None else str(value)
        return True

    def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            removed += int(self._hashes.pop(name, None) is not None)
            removed += int(self._strings.pop(name, None) is not None)
        return removed

    def keys(self, pattern: str = "*") -> list[str]:
        return [
            name for name in list(self._hashes) + list(self._strings)
            if fnmatch.fnmatch(name, pattern)
        ]

    def flush(self, names: Iterable[str] | None = None) -> None:
        if names is None:
            self._hashes.clear()
            self._strings.clear()
            return
        self.delete(*names)
