"""Static check: logger format strings must match their arguments.

A mismatch (`logger.error("... %s", a, b)`) is silent at runtime until the
logging handler formats it, and only surfaces in environments with strict
logging (CI caught one that local runs missed). This test makes it
deterministic.
"""
import ast
import pathlib
import re

LEVELS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
PLACEHOLDER = re.compile(r"%(?!%)[#0\- +]?\d*(?:\.\d+)?[sdifr]")
SKIP_PARTS = (".venv", "node_modules", "/.git/", "/.kilo/", "/backups/")


def _mismatches():
    found = []
    for path in pathlib.Path(".").rglob("*.py"):
        if any(part in str(path) for part in SKIP_PARTS):
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != "logger":
                continue
            if node.func.attr not in LEVELS or not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            expected = len(node.args) - 1
            actual = len(PLACEHOLDER.findall(first.value))
            if actual != expected:
                found.append(
                    f"{path}:{node.lineno} has {actual} placeholder(s) for "
                    f"{expected} argument(s): {first.value[:60]!r}"
                )
    return found


def test_logger_format_strings_match_arguments():
    found = _mismatches()
    assert not found, "logger format mismatches:\n" + "\n".join(found)
