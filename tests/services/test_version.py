"""Tests for services/common/version.py — git SHA capture and /version bot command."""
import importlib
import time
import subprocess
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


# ─── Version module tests ─────────────────────────────────────────────────────


class TestVersionModule:
    """Test the central version source module."""

    def test_version_constants_exist(self):
        """All version constants must be defined and be strings."""
        from services.common import version
        assert hasattr(version, "SERVICE_VERSION")
        assert hasattr(version, "GIT_COMMIT")
        assert hasattr(version, "GIT_DIRTY")
        assert hasattr(version, "BUILD_LABEL")
        assert isinstance(version.SERVICE_VERSION, str)
        assert isinstance(version.GIT_COMMIT, str)
        assert isinstance(version.GIT_DIRTY, bool)
        assert isinstance(version.BUILD_LABEL, str)

    def test_build_label_contains_version_and_commit(self):
        """BUILD_LABEL should contain both SERVICE_VERSION and GIT_COMMIT."""
        from services.common import version
        assert version.SERVICE_VERSION in version.BUILD_LABEL
        assert version.GIT_COMMIT in version.BUILD_LABEL

    def test_build_label_dirty_suffix(self):
        """When git is dirty, BUILD_LABEL should contain 'dirty'."""
        rev_result = MagicMock()
        rev_result.returncode = 0
        rev_result.stdout = "abc1234\n"

        status_result = MagicMock()
        status_result.returncode = 0
        status_result.stdout = " M some_file.py\n"

        with patch("subprocess.run", side_effect=[rev_result, status_result]):
            mod = importlib.reload(importlib.import_module("services.common.version"))
            assert "dirty" in mod.BUILD_LABEL
            assert mod.GIT_DIRTY is True

    def test_build_label_clean_no_dirty_suffix(self):
        """When git is clean, BUILD_LABEL should NOT contain 'dirty'."""
        rev_result = MagicMock()
        rev_result.returncode = 0
        rev_result.stdout = "abc1234\n"

        status_result = MagicMock()
        status_result.returncode = 0
        status_result.stdout = ""

        with patch("subprocess.run", side_effect=[rev_result, status_result]):
            mod = importlib.reload(importlib.import_module("services.common.version"))
            assert "dirty" not in mod.BUILD_LABEL
            assert mod.GIT_DIRTY is False

    def test_git_unavailable_defaults_to_unknown(self):
        """When git command fails, all fields should default to 'unknown'."""
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            mod = importlib.reload(importlib.import_module("services.common.version"))
            assert mod.GIT_COMMIT == "unknown"
            assert mod.GIT_DIRTY is False

    def test_capture_git_info_returns_tuple(self):
        """_capture_git_info should return (str, bool) tuple."""
        from services.common.version import _capture_git_info
        result = _capture_git_info()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], bool)

    def test_capture_git_info_subprocess_failure(self):
        """When subprocess.run raises, should return ('unknown', False)."""
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            from services.common.version import _capture_git_info
            commit, dirty = _capture_git_info()
            assert commit == "unknown"
            assert dirty is False

    def test_capture_git_info_timeout(self):
        """When subprocess.run times out, should return ('unknown', False)."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 5)):
            from services.common.version import _capture_git_info
            commit, dirty = _capture_git_info()
            assert commit == "unknown"
            assert dirty is False

    def test_capture_git_info_nonzero_returncode(self):
        """When git returns non-zero exit code, should return ('unknown', False)."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            from services.common.version import _capture_git_info
            commit, dirty = _capture_git_info()
            assert commit == "unknown"
            assert dirty is False

    def test_capture_git_info_dirty_detection(self):
        """When git status --porcelain has output, dirty should be True."""
        rev_result = MagicMock()
        rev_result.returncode = 0
        rev_result.stdout = "abc1234\n"

        status_result = MagicMock()
        status_result.returncode = 0
        status_result.stdout = " M some_file.py\n"

        with patch("subprocess.run", side_effect=[rev_result, status_result]):
            from services.common.version import _capture_git_info
            commit, dirty = _capture_git_info()
            assert commit == "abc1234"
            assert dirty is True

    def test_capture_git_info_clean_working_tree(self):
        """When git status --porcelain is empty, dirty should be False."""
        rev_result = MagicMock()
        rev_result.returncode = 0
        rev_result.stdout = "abc1234\n"

        status_result = MagicMock()
        status_result.returncode = 0
        status_result.stdout = ""

        with patch("subprocess.run", side_effect=[rev_result, status_result]):
            from services.common.version import _capture_git_info
            commit, dirty = _capture_git_info()
            assert commit == "abc1234"
            assert dirty is False

    @pytest.fixture(autouse=True)
    def _restore_version(self):
        """Restore the real version module after each test."""
        yield
        importlib.reload(importlib.import_module("services.common.version"))


# ─── /version bot command tests ───────────────────────────────────────────────


class TestVersionCommand:
    """Test the /version Telegram bot command."""

    @pytest.fixture
    def mock_update(self):
        update = MagicMock()
        update.effective_chat.id = -1001234567890
        return update

    @pytest.fixture
    def mock_context(self):
        context = MagicMock()
        context.bot.send_message = AsyncMock()
        return context

    @pytest.mark.asyncio
    async def test_version_redis_unavailable(self, mock_update, mock_context):
        """When Redis is unavailable, should send error message."""
        with patch("notification.commands._guard.chat_allowed", return_value=True), \
             patch("notification.commands.system.debug_chat_only", return_value=True), \
             patch("notification.commands._helpers._get_redis", return_value=None):
            from notification.commands.system import cmd_version
            await cmd_version(mock_update, mock_context)

        mock_context.bot.send_message.assert_called_once()
        args, kwargs = mock_context.bot.send_message.call_args
        assert "Redis unavailable" in kwargs.get("text", args[0] if args else "")

    @pytest.mark.asyncio
    async def test_version_debug_chat_restricted(self, mock_update, mock_context):
        """When not in debug chat, should silently drop."""
        with patch("notification.commands._guard.chat_allowed", return_value=True), \
             patch("notification.commands.system.debug_chat_only", return_value=False):
            from notification.commands.system import cmd_version
            await cmd_version(mock_update, mock_context)

        mock_context.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_version_all_services_present(self, mock_update, mock_context):
        """Should list all 6 known services."""
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {
            "name": "monolith",
            "version": "1.0.0+abc1234",
            "commit": "abc1234",
            "dirty": "False",
            "last_heartbeat": str(time.time()),
        }

        with patch("notification.commands._guard.chat_allowed", return_value=True), \
             patch("notification.commands.system.debug_chat_only", return_value=True), \
             patch("notification.commands._helpers._get_redis", return_value=mock_redis):
            from notification.commands.system import cmd_version
            await cmd_version(mock_update, mock_context)

        mock_context.bot.send_message.assert_called_once()
        text = mock_context.bot.send_message.call_args.kwargs.get("text", "")
        for svc in ["monolith", "data-gateway", "market-data", "analysis-engine", "notification-service", "resource-monitor"]:
            assert svc in text

    @pytest.mark.asyncio
    async def test_version_stale_service_shown(self, mock_update, mock_context):
        """Stale service (heartbeat > 240s) should show stale marker."""
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {
            "name": "monolith",
            "version": "1.0.0+abc1234",
            "commit": "abc1234",
            "dirty": "False",
            "last_heartbeat": str(time.time() - 300),
        }

        with patch("notification.commands._guard.chat_allowed", return_value=True), \
             patch("notification.commands.system.debug_chat_only", return_value=True), \
             patch("notification.commands._helpers._get_redis", return_value=mock_redis):
            from notification.commands.system import cmd_version
            await cmd_version(mock_update, mock_context)

        text = mock_context.bot.send_message.call_args.kwargs.get("text", "")
        assert "stale" in text

    @pytest.mark.asyncio
    async def test_version_dirty_flag_shown(self, mock_update, mock_context):
        """Dirty service should show dirty warning."""
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {
            "name": "monolith",
            "version": "1.0.0+abc1234+dirty",
            "commit": "abc1234",
            "dirty": "True",
            "last_heartbeat": str(time.time()),
        }

        with patch("notification.commands._guard.chat_allowed", return_value=True), \
             patch("notification.commands.system.debug_chat_only", return_value=True), \
             patch("notification.commands._helpers._get_redis", return_value=mock_redis):
            from notification.commands.system import cmd_version
            await cmd_version(mock_update, mock_context)

        text = mock_context.bot.send_message.call_args.kwargs.get("text", "")
        assert "dirty" in text

    @pytest.mark.asyncio
    async def test_version_no_data_service_shown(self, mock_update, mock_context):
        """Service with no registry data should show 'no data'."""
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {}

        with patch("notification.commands._guard.chat_allowed", return_value=True), \
             patch("notification.commands.system.debug_chat_only", return_value=True), \
             patch("notification.commands._helpers._get_redis", return_value=mock_redis):
            from notification.commands.system import cmd_version
            await cmd_version(mock_update, mock_context)

        text = mock_context.bot.send_message.call_args.kwargs.get("text", "")
        assert "no data" in text

    @pytest.mark.asyncio
    async def test_version_includes_git_head(self, mock_update, mock_context):
        """Output should include 'Git HEAD' line."""
        mock_redis = MagicMock()
        mock_redis.hgetall.return_value = {}

        with patch("notification.commands._guard.chat_allowed", return_value=True), \
             patch("notification.commands.system.debug_chat_only", return_value=True), \
             patch("notification.commands._helpers._get_redis", return_value=mock_redis):
            from notification.commands.system import cmd_version
            await cmd_version(mock_update, mock_context)

        text = mock_context.bot.send_message.call_args.kwargs.get("text", "")
        assert "Git HEAD" in text
