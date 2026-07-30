#!/usr/bin/env python3
"""Terminal debug client — READ commands via Telegram bot, WRITE commands via Redis.

READ commands (/debug, /status, /ltp, /signals, etc.) inspect the running
process's memory and must go through the Telegram bot (which runs inside
the orchestrator process).

WRITE commands (/loglevel) set Redis keys directly — the service picks
them up independently via heartbeat.  No Telegram bot needed.

One-time setup:
  1. pip install telethon
  2. Get API ID + API hash from https://my.telegram.org → API Development Tools
  3. Add to .env:
       TELEGRAM_API_ID=<your api id>
       TELEGRAM_API_HASH=<your api hash>
       TELEGRAM_BOT_USERNAME=StockAnalysisBot   (or whatever the bot username is)
  4. First run: enter phone number + code (session saved for subsequent runs)

Usage:
   python scripts/debug_cli.py overview
   python scripts/debug_cli.py stock RELIANCE
   python scripts/debug_cli.py signals
   python scripts/debug_cli.py signals RELIANCE
   python scripts/debug_cli.py cycle
   python scripts/debug_cli.py redis RELIANCE
   python scripts/debug_cli.py counters
   python scripts/debug_cli.py memory
   python scripts/debug_cli.py analyzers

   # Log level control:
   python scripts/debug_cli.py loglevel
   python scripts/debug_cli.py loglevel analyser DEBUG

   # Also supports existing bot commands:
   python scripts/debug_cli.py status
   python scripts/debug_cli.py ltp RELIANCE
   python scripts/debug_cli.py help
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_telethon_available = False
try:
    from telethon import TelegramClient  # noqa: F401
    _telethon_available = True
except ImportError:
    pass


# ── Config ────────────────────────────────────────────────────────────────────

API_ID = os.environ.get("TELEGRAM_API_ID", "")
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "")
DEBUG_CHAT_ID = os.environ.get("TELEGRAM_DEBUG_CHAT_ID", "")
SESSION_PATH = os.environ.get(
    "TELEGRAM_SESSION_PATH",
    os.path.expanduser("~/.config/stockanalysis/debug_session"),
)

RESPONSE_TIMEOUT = 15  # seconds to wait for bot response

# Map short CLI subcommands to full Telegram bot commands
COMMAND_MAP = {
    "overview":   "/debug",
    "stock":      "/debugstock",
    "signals":    "/debugsignals",
    "cycle":      "/debugcycle",
    "redis":      "/debugredis",
    "counters":   "/debugcounters",
    "memory":     "/debugmemory",
    "analyzers":  "/debuganalyzers",
    "loglevel":   "/loglevel",
    # existing bot commands (passthrough)
    "status":     "/status",
    "ltp":        "/ltp",
    "gainers":    "/gainers",
    "losers":     "/losers",
    "watchlist":  "/watchlist",
    "holidays":   "/holidays",
    "straddle":   "/straddle",
    "walls":      "/walls",
    "help":       "/help",
}


def _strip_html(text: str) -> str:
    """Strip simple HTML tags from Telegram messages for terminal display."""
    import re
    # Replace <br> and <br/> with newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # Remove all other HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&amp;", "&").replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    return text


async def _resolve_bot_id(client) -> int:
    """Resolve the bot's user ID from its username."""
    if not BOT_USERNAME:
        return 0
    entity = await client.get_entity(BOT_USERNAME)
    return entity.id


async def _run(command: str) -> None:
    if not API_ID or not API_HASH:
        print("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")
        print("Get them from https://my.telegram.org → API Development Tools")
        sys.exit(1)

    if not DEBUG_CHAT_ID:
        print("ERROR: TELEGRAM_DEBUG_CHAT_ID must be set in .env")
        sys.exit(1)

    # Ensure session directory exists
    session_dir = os.path.dirname(SESSION_PATH)
    if session_dir:
        os.makedirs(session_dir, exist_ok=True)

    client = TelegramClient(SESSION_PATH, int(API_ID), API_HASH)
    await client.start()

    # Send the command to the debug GROUP (not directly to the bot).
    # The bot is a member of the group and sees /-prefixed commands,
    # then replies in the group.  This ensures debug_chat_only() passes.
    group_entity = await client.get_entity(int(DEBUG_CHAT_ID))
    await client.send_message(group_entity, command)

    # Wait for the bot's response in the group
    bot_id = await _resolve_bot_id(client)
    deadline = time.time() + RESPONSE_TIMEOUT
    response_text = None
    while time.time() < deadline:
        await asyncio.sleep(1)
        async for msg in client.iter_messages(group_entity, limit=5):
            if msg.text and not msg.out and msg.sender_id == bot_id:
                if msg.date.timestamp() > (time.time() - RESPONSE_TIMEOUT):
                    response_text = msg.text
                    break
        if response_text:
            break

    await client.disconnect()

    if response_text:
        print(_strip_html(response_text))
    else:
        print(f"(No response from bot within {RESPONSE_TIMEOUT}s — is it running?)")


ALL_LOG_SERVICES = [
    "analyser", "zerodha", "intelligence", "fno", "notification", "common",
    "premarket", "post-market-analysis", "backtest", "nse", "sentiment",
    "auth-service", "analysis-engine", "market-data", "data-gateway",
    "orchestrator", "notification-service", "resource-monitor",
    "paper-trading", "signal-intelligence",
]


def _handle_loglevel_redis(args: list[str]) -> None:
    """Set or display log levels directly via Redis — no Telegram needed."""
    import redis as _redis

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        r = _redis.Redis.from_url(redis_url, socket_connect_timeout=5)
        r.ping()
    except Exception as e:
        print(f"ERROR: Cannot connect to Redis at {redis_url}: {e}")
        sys.exit(1)

    if not args:
        print(f"Log Levels (via {redis_url})")
        print("─" * 45)
        for svc in sorted(ALL_LOG_SERVICES):
            val = r.get(f"service:log_level:{svc}")
            if val:
                level = val.decode() if isinstance(val, bytes) else val
                print(f"  {svc:.<30} {level}")
            else:
                print(f"  {svc:.<30} default")
        return

    service = args[0]
    if service == "all":
        level = args[1].upper() if len(args) > 1 else "INFO"
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            print(f"Invalid level: {level}")
            sys.exit(1)
        for svc in ALL_LOG_SERVICES:
            r.set(f"service:log_level:{svc}", level)
        print(f"ALL services → {level}  (takes effect in ≤30s)")
        return
    if service not in ALL_LOG_SERVICES:
        print(f"Unknown service: {service}")
        print(f"Available: {', '.join(ALL_LOG_SERVICES)}")
        sys.exit(1)

    if len(args) < 2:
        val = r.get(f"service:log_level:{service}")
        if val:
            level = val.decode() if isinstance(val, bytes) else val
            print(f"{service}: {level}")
        else:
            print(f"{service}: default")
        return

    level = args[1].upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        print(f"Invalid level: {level}. Use DEBUG, INFO, WARNING, or ERROR")
        sys.exit(1)

    r.set(f"service:log_level:{service}", level)
    print(f"{service} → {level}  (takes effect in ≤30s)")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nAvailable subcommands:")
        for cmd in sorted(COMMAND_MAP.keys()):
            print(f"  {cmd}")
        sys.exit(1)

    subcmd = sys.argv[1].lower().strip()
    args = sys.argv[2:]

    # ── WRITE commands — Redis direct path (no Telegram needed) ──
    if subcmd == "loglevel":
        _handle_loglevel_redis(args)
        return

    # ── READ commands — Telegram bot path ──
    if not _telethon_available:
        print("ERROR: telethon not installed. Run: pip install telethon")
        print("READ commands (debug, status, ltp, etc.) require Telethon.")
        print("WRITE commands (loglevel) work without Telethon via Redis directly.")
        sys.exit(1)

    tg_command = COMMAND_MAP.get(subcmd)
    if tg_command is None:
        if subcmd.startswith("/"):
            tg_command = subcmd
        else:
            print(f"Unknown subcommand: {subcmd}")
            print(f"Available: {', '.join(sorted(COMMAND_MAP.keys()))}")
            sys.exit(1)

    if args:
        tg_command += " " + " ".join(args)

    asyncio.run(_run(tg_command))


if __name__ == "__main__":
    main()
