"""Central version source for all services.

Captures the git commit SHA + dirty flag at import time. Every service
writes these fields to its Redis registry hash so the /version bot
command can verify which commit is running on the server.

Fail-silent: if git is unavailable (not installed, not a repo, detached
HEAD), all fields default to "unknown" and GIT_DIRTY is False.
"""
from __future__ import annotations

import subprocess

SERVICE_VERSION = "1.0.0"


def _capture_git_info() -> tuple[str, bool]:
    """Return (short_commit_sha, is_dirty) or ("unknown", False) on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return "unknown", False
        commit = result.stdout.strip() or "unknown"

        dirty_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        is_dirty = bool(dirty_result.stdout.strip())

        return commit, is_dirty
    except Exception:
        return "unknown", False


GIT_COMMIT, GIT_DIRTY = _capture_git_info()

BUILD_LABEL = f"{SERVICE_VERSION}+{GIT_COMMIT}"
if GIT_DIRTY:
    BUILD_LABEL += "+dirty"
