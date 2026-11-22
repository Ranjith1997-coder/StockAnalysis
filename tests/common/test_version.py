"""Tests for services/common/version.py."""
import subprocess
from unittest.mock import patch

import services.common.version as version


def _run_with(commit: str = "abc1234", porcelain: str = "", rc: int = 0):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, rc, commit, "")
        return subprocess.CompletedProcess(args, rc, porcelain, "")

    return fake_run, calls


def test_clean_tree_is_not_dirty():
    fake, _ = _run_with(porcelain="")
    with patch("subprocess.run", side_effect=fake):
        commit, dirty = version._capture_git_info()
    assert commit == "abc1234"
    assert dirty is False


def test_tracked_modification_is_dirty():
    fake, _ = _run_with(porcelain=" M services/x.py\n")
    with patch("subprocess.run", side_effect=fake):
        _, dirty = version._capture_git_info()
    assert dirty is True


def test_untracked_files_are_excluded_from_dirty_check():
    fake, calls = _run_with(porcelain="")
    with patch("subprocess.run", side_effect=fake):
        version._capture_git_info()
    status_calls = [c for c in calls if "status" in c]
    assert status_calls and "--untracked-files=no" in status_calls[0]


def test_git_failure_returns_unknown():
    fake, _ = _run_with(rc=128)
    with patch("subprocess.run", side_effect=fake):
        commit, dirty = version._capture_git_info()
    assert commit == "unknown"
    assert dirty is False
