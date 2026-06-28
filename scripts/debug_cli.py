#!/usr/bin/env python3
"""Terminal debug client — sends commands to the Telegram bot via Telethon.

Acts as a Telegram *user* (not a bot) to send /debug* commands to the bot
and receive responses.  Reuses the bot's existing command handlers — zero
monolith code changes needed.

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

try:
    from telethon import TelegramClient
except ImportError:
    print("ERROR: telethon not installed. Run: pip install telethon")
    sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────────────

API_ID = os.environ.get("TELEGRAM_API_ID", "")
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "")
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


async def _run(command: str) -> None:
    if not API_ID or not API_HASH:
        print("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")
        print("Get them from https://my.telegram.org → API Development Tools")
        sys.exit(1)

    if not BOT_USERNAME:
        print("ERROR: TELEGRAM_BOT_USERNAME must be set in .env")
        print("Example: TELEGRAM_BOT_USERNAME=stock_notification_2_bot")
        sys.exit(1)

    # Ensure session directory exists
    session_dir = os.path.dirname(SESSION_PATH)
    if session_dir:
        os.makedirs(session_dir, exist_ok=True)

    client = TelegramClient(SESSION_PATH, int(API_ID), API_HASH)
    await client.start()

    # Send the command to the bot
    await client.send_message(BOT_USERNAME, command)

    # Wait for the bot's response
    deadline = time.time() + RESPONSE_TIMEOUT
    response_text = None
    while time.time() < deadline:
        await asyncio.sleep(1)
        async for msg in client.iter_messages(BOT_USERNAME, limit=1):
            if msg.text and not msg.out:
                # Only accept messages received after we sent our command
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


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nAvailable subcommands:")
        for cmd in sorted(COMMAND_MAP.keys()):
            print(f"  {cmd}")
        sys.exit(1)

    subcmd = sys.argv[1].lower().strip()
    args = sys.argv[2:]

    tg_command = COMMAND_MAP.get(subcmd)
    if tg_command is None:
        # Allow raw /command passthrough
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
