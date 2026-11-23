#!/usr/bin/env python3
"""Regenerate tests/contract/silent_pass_allowlist.txt.

The allowlist freezes legacy ``except: pass`` handlers so the contract test only
rejects *new* ones. Run this only after deliberately accepting a new silent
handler (and prefer fixing it with a log line instead).

Usage:
    PYTHONPATH=. .venv/bin/python scripts/gen_silent_allowlist.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.contract.test_static_rules import ALLOWLIST, silent_handler_sites  # noqa: E402

HEADER = """# Frozen inventory of legacy `except: pass` / `except: continue` handlers.
#
# Goal: no NEW silent handlers. When you touch a file, replace the silent
# handler with a log line and delete its entry here.
#
# Format: <path relative to repo root>::<function qualname>
# Regenerate deliberately (question the churn):
#   PYTHONPATH=. .venv/bin/python scripts/gen_silent_allowlist.py
"""


def main() -> int:
    sites = sorted(silent_handler_sites())
    ALLOWLIST.write_text(HEADER + "\n".join(sites) + "\n")
    print(f"wrote {len(sites)} entries to {ALLOWLIST.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
