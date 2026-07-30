"""/loglevel — runtime log level control for any module/service.

Usage:
  /loglevel                          Show current log levels
  /loglevel analyser DEBUG          Set analyser to DEBUG
  /loglevel market-data WARNING     Set market-data to WARNING
  /loglevel all INFO                Reset all services to INFO
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from notification.commands._guard import guard, debug_chat_only
from services.common.logging import set_runtime_level, reset_runtime_level

ALL_SERVICES = [
    "analyser", "zerodha", "intelligence", "fno", "notification", "common",
    "premarket", "post-market-analysis", "backtest", "nse", "sentiment",
    "auth-service", "analysis-engine", "market-data", "data-gateway",
    "orchestrator", "notification-service", "resource-monitor",
    "paper-trading", "signal-intelligence",
]


@guard
async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not debug_chat_only(update):
        return
    from notification.commands._helpers import get_redis

    redis = get_redis()
    args = context.args or []

    if not args:
        lines = [f"<b>Log Levels</b>"]
        for svc in sorted(ALL_SERVICES):
            try:
                val = redis.get(f"service:log_level:{svc}")
                if val:
                    level = val.decode() if isinstance(val, bytes) else val
                    lines.append(f"  {svc}: <b>{level}</b>")
                else:
                    lines.append(f"  {svc}: <i>default</i>")
            except Exception:
                lines.append(f"  {svc}: <i>N/A</i>")
        lines.append("")
        lines.append("Use /loglevel &lt;service&gt; &lt;level&gt; to change")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="\n".join(lines), parse_mode="HTML",
        )
        return

    service = args[0]
    if service == "all":
        level = args[1].upper() if len(args) > 1 else "INFO"
        for svc in ALL_SERVICES:
            set_runtime_level(redis, svc, level)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"<b>Set ALL services to {level}</b>\n(up to 30s to take effect)",
            parse_mode="HTML",
        )
        return

    if len(args) < 2:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Usage: /loglevel &lt;service&gt; &lt;DEBUG|INFO|WARNING|ERROR&gt;",
        )
        return

    level = args[1].upper()
    valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR")
    if level not in valid_levels:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Invalid level: {level}. Use DEBUG, INFO, WARNING, or ERROR.",
        )
        return

    if level == "INFO":
        reset_runtime_level(redis, service)
    else:
        set_runtime_level(redis, service, level)

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"<b>{service}</b> → <b>{level}</b> (up to 30s to take effect)",
        parse_mode="HTML",
    )


HANDLERS = [
    ("loglevel", _handler),
]
