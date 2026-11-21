"""Tests for lib/notification/commands/paper_trading_cmds.py (write commands)."""

from unittest.mock import AsyncMock, MagicMock, patch

from lib.notification.commands import paper_trading_cmds as cmds


def _ctx(args):
    ctx = MagicMock()
    ctx.args = args
    ctx.bot.send_message = AsyncMock()
    return ctx


def _update():
    update = MagicMock()
    update.effective_chat.id = 1
    return update


def _sent_text(ctx) -> str:
    return ctx.bot.send_message.call_args.kwargs["text"]


async def test_close_all_sends_command_without_await_error():
    rc = MagicMock()
    rc.hgetall.return_value = {"pos-1": "{}"}
    ctx = _ctx(["all"])
    with patch.object(cmds, "_get_redis", return_value=rc):
        await cmds.cmd_paper_close.__wrapped__(_update(), ctx)
    rc.xadd.assert_called_once_with(cmds.COMMANDS_STREAM, {"command": "close", "position_id": "all"}, maxlen=100)
    assert "Closing" in _sent_text(ctx)


async def test_close_single_position():
    rc = MagicMock()
    rc.hgetall.return_value = {"abc-123": "{}"}
    ctx = _ctx(["abc"])
    with patch.object(cmds, "_get_redis", return_value=rc):
        await cmds.cmd_paper_close.__wrapped__(_update(), ctx)
    rc.xadd.assert_called_once_with(cmds.COMMANDS_STREAM, {"command": "close", "position_id": "abc-123"}, maxlen=100)


async def test_config_set_supported_key():
    rc = MagicMock()
    ctx = _ctx(["max_positions", "5"])
    with patch.object(cmds, "_get_redis", return_value=rc):
        await cmds.cmd_paper_config.__wrapped__(_update(), ctx)
    rc.xadd.assert_called_once()
    assert "Set" in _sent_text(ctx)


async def test_config_set_unsupported_key_is_rejected_with_reply():
    rc = MagicMock()
    ctx = _ctx(["slippage", "10"])
    with patch.object(cmds, "_get_redis", return_value=rc):
        await cmds.cmd_paper_config.__wrapped__(_update(), ctx)
    rc.xadd.assert_not_called()
    assert "Unsupported" in _sent_text(ctx)
    assert "max_positions" in _sent_text(ctx)


async def test_reset_confirm_sends_command_and_is_honest_about_positions():
    rc = MagicMock()
    ctx = _ctx(["confirm"])
    with patch.object(cmds, "_get_redis", return_value=rc):
        await cmds.cmd_paper_reset.__wrapped__(_update(), ctx)
    rc.xadd.assert_called_once_with(cmds.COMMANDS_STREAM, {"command": "reset"}, maxlen=100)
    assert "RESET" in _sent_text(ctx)


async def test_reset_preview_without_capital_does_not_crash():
    rc = MagicMock()
    rc.hgetall.return_value = {}
    ctx = _ctx([])
    with patch.object(cmds, "_get_redis", return_value=rc):
        await cmds.cmd_paper_reset.__wrapped__(_update(), ctx)
    rc.xadd.assert_not_called()
    assert "confirm" in _sent_text(ctx)
