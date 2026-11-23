"""Contract tests: static rules that catch the bug classes unit tests missed
(T2).

1. **Silent exception handlers** — ``except: pass`` hid the orchestrator
   heartbeat ``TypeError`` for months. Legacy handlers are frozen in
   ``silent_pass_allowlist.txt``; new ones must log or be justified there.
2. **``time`` shadowing** — a module that imports ``datetime.time`` and then
   calls ``time.time()`` raises ``TypeError`` at runtime. Alias the module
   (``import time as _time``).
3. **Redis key typos** — a ``data:*`` literal outside the declared families is
   almost always a typo that silently reads/writes nothing.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.contract.schema import DATA_KEY_PREFIXES

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("services", "lib", "common")
ALLOWLIST = Path(__file__).with_name("silent_pass_allowlist.txt")

# data:* families that exist outside the options contract (documented in their
# own services). Extend deliberately, not to silence a typo.
OTHER_DATA_FAMILIES = (
    "data:price",
    "data:sensibull",
    "data:zerodha",
    "data:gex_state:",
    "data:cycle_ready",
    "data:cycle_stream",
)
KNOWN_DATA_FAMILIES = DATA_KEY_PREFIXES + OTHER_DATA_FAMILIES

SKIP_DIRS = {"__pycache__"}


def _sources():
    for root in SCAN_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.py"):
            if SKIP_DIRS & set(path.parts):
                continue
            try:
                yield path, ast.parse(path.read_text())
            except SyntaxError:  # pragma: no cover - ruff catches these first
                continue


class _SilentHandlerCollector(ast.NodeVisitor):
    """Collect ``except ...: pass`` sites as ``path::qualname`` keys."""

    def __init__(self, path: Path) -> None:
        self.rel = path.relative_to(REPO_ROOT).as_posix()
        self.stack: list[str] = []
        self.found: list[str] = []

    def _visit_named(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_named

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.body and all(isinstance(s, (ast.Pass, ast.Continue)) for s in node.body):
            qualname = ".".join(self.stack) or "<module>"
            self.found.append(f"{self.rel}::{qualname}")
        self.generic_visit(node)


def silent_handler_sites() -> set[str]:
    """Every (file, function) that swallows an exception with pass/continue."""
    sites: set[str] = set()
    for path, tree in _sources():
        collector = _SilentHandlerCollector(path)
        collector.visit(tree)
        sites.update(collector.found)
    return sites


def _read_allowlist() -> set[str]:
    if not ALLOWLIST.is_file():
        return set()
    return {
        line.strip()
        for line in ALLOWLIST.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def test_no_new_silent_exception_handlers() -> None:
    new = silent_handler_sites() - _read_allowlist()
    assert not new, (
        "New silent exception handler(s) — log the error, or add the site to "
        "tests/contract/silent_pass_allowlist.txt with a comment explaining why "
        "silence is correct:\n  " + "\n  ".join(sorted(new))
    )


def test_silent_handler_scan_is_not_vacuous() -> None:
    """Guard the scanner: a broken AST walk would silently pass everything."""
    sites = silent_handler_sites()
    assert len(sites) >= 40, f"scanner found only {len(sites)} sites — is it broken?"
    assert "common/market_calendar.py::_fetch_nse_holidays" in sites


def test_silent_exception_allowlist_has_no_stale_entries() -> None:
    stale = _read_allowlist() - silent_handler_sites()
    assert not stale, (
        "Allowlist entries no longer match any handler (the code was fixed or "
        "moved). Delete these lines:\n  " + "\n  ".join(sorted(stale))
    )


def test_no_time_class_shadowing() -> None:
    """``from datetime import time`` + ``time.time()`` is a guaranteed TypeError."""
    offenders: list[str] = []
    for path, tree in _sources():
        shadows = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "datetime"
            and any(a.name == "time" and a.asname is None for a in node.names)
            for node in ast.walk(tree)
        )
        if not shadows:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "time"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time"
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, (
        "time.time() where `time` is datetime.time (alias the module as _time):\n  "
        + "\n  ".join(offenders)
    )


def test_shared_listener_is_imported_where_used() -> None:
    """``from common.helperFunctions import *`` hides undefined names from ruff.

    Every module that calls ``listen_until_closed`` must import it explicitly;
    otherwise the daemon thread dies with NameError at startup (seen in the
    orchestrator on 2026-11-23, invisible to lint for exactly that reason).
    """
    import ast as _ast

    offenders: list[str] = []
    for path, tree in _sources():
        uses = {
            node.id for node in _ast.walk(tree)
            if isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Load)
            and node.id == "listen_until_closed"
        }
        if not uses:
            continue
        imported = any(
            isinstance(node, _ast.ImportFrom)
            and node.module == "lib.zerodha.pubsub"
            and any(alias.name == "listen_until_closed" for alias in node.names)
            for node in _ast.walk(tree)
        )
        defined = any(
            isinstance(node, _ast.FunctionDef) and node.name == "listen_until_closed"
            for node in _ast.walk(tree)
        )
        if not (imported or defined):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"listen_until_closed used but not imported: {offenders}"


def test_data_key_literals_use_declared_families() -> None:
    offenders: list[str] = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith("data:")
                and not node.value.startswith(KNOWN_DATA_FAMILIES)
            ):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {node.value!r}"
                )
    assert not offenders, (
        "Redis key literal outside the declared data:* families (typo?):\n  "
        + "\n  ".join(offenders)
    )
