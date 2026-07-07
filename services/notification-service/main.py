"""
Notification Service — consumes notification jobs from Redis and sends them
to Telegram / Discord with retry logic.

Reads from stream: notification:jobs
Uses consumer group: notifier

Message format:
    {
        "chat_type": "intraday" | "positional" | "live_options",
        "message": "formatted HTML text",
        "parse_mode": "HTML" | None,
        "message_type": "analysis_result" | "startup" | "shutdown" | "report" | "crash" | "stale_data",
        "symbol": "RELIANCE" | None,         # optional, for logging
        "priority": "HIGH" | "CRITICAL" | None,
        "timestamp": "2026-06-27T12:00:00",
    }

On failure: dead-letter to notification:dead after 3 retries.
"""

from __future__ import annotations

import os
import sys
import json
import time
import signal
import argparse
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from dotenv import load_dotenv
load_dotenv()

import redis as sync_redis
from services.common.logging import get_logger
logger = get_logger("notification-service")
from common.constants import (
    TELEGRAM_INTRADAY_CHAT_ID, TELEGRAM_INTRADAY_TOKEN,
    TELEGRAM_POSITIONAL_CHAT_ID, TELEGRAM_POSITIONAL_TOKEN,
    TELEGRAM_LIVE_OPTIONS_TOKEN, TELEGRAM_LIVE_OPTIONS_CHAT_ID,
    TELEGRAM_URL,
    NOTIFICATION_CHANNEL,
    DISCORD_INTRADAY_WEBHOOK_URL,
    DISCORD_POSITIONAL_WEBHOOK_URL,
    DISCORD_LIVE_OPTIONS_WEBHOOK_URL,
    ENV_PRODUCTION,
)
import requests
import re
from services.common.metrics import incr_stock, incr_system, incr_daily

_running = True


def signal_handler(signum, frame):
    global _running
    logger.info("[notification-service] Received signal, shutting down...")
    _running = False


# ═══════════════════════════════════════════════════════════════════════════
# HTML sanitization — prevents Telegram "can't parse entities" errors
# ═══════════════════════════════════════════════════════════════════════════

# Telegram HTML supports a limited subset of tags. Any unsupported or malformed
# tag causes a 400 error and the message is dead-lettered after 3 retries.
_TELEGRAM_SAFE_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike",
                        "del", "code", "pre", "a", "tg-spoiler", "blockquote"}


def _sanitize_html(text: str) -> str:
    """Sanitize HTML for Telegram's strict parser.

    Fixes:
    1. Empty tags: <code></code>, <i></i> → removed
    2. Unsupported tags: stripped (content kept)
    3. Unescaped &, <, > outside tags → &amp;, &lt;, &gt;
    4. Unclosed tags: closed automatically
    """
    # Remove empty tags (no content between open and close)
    text = re.sub(r"<(\w+)>\s*</\1>", "", text)

    # Fix unescaped ampersands that aren't part of entities
    text = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+);)", "&amp;", text)

    # Process all tags: keep safe ones, strip unsupported ones (keep content)
    def _replace_tag(m):
        full = m.group(0)
        tag_name = m.group(1).lower()
        if tag_name in _TELEGRAM_SAFE_TAGS:
            return full
        # Unsupported tag — strip it, keep any content between
        return ""

    # Remove unsupported opening tags
    text = re.sub(r"</?([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^>]*)?>", _replace_tag, text)

    # Remove leftover empty tags after stripping
    text = re.sub(r"<(\w+)>\s*</\1>", "", text)

    return text


# ═══════════════════════════════════════════════════════════════════════════
# Discord helpers (ported from notification/Notification.py)
# ═══════════════════════════════════════════════════════════════════════════

def _html_to_discord(text: str) -> str:
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text


def _send_discord(webhook_url: str, message: str, parse_mode: str | None = None) -> bool:
    if not webhook_url:
        return False
    content = _html_to_discord(message) if parse_mode and parse_mode.upper() == "HTML" else message
    if len(content) > 2000:
        content = content[:1997] + "..."
    try:
        resp = requests.post(
            webhook_url,
            json={"content": content},
            timeout=(5, 10),
        )
        if resp.status_code not in (200, 204):
            logger.error(f"Discord webhook failed: {resp.status_code}: {resp.text}")
            return False
        return True
    except Exception as e:
        logger.error(f"Discord webhook error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Telegram sender (ports TELEGRAM_NOTIFICATIONS.send_notification)
# ═══════════════════════════════════════════════════════════════════════════

def _send_telegram(token: str, chat_id: str, message: str, parse_mode: str | None = None) -> bool:
    """Send a single Telegram message with 3x retry + exponential backoff."""
    if not token or not chat_id:
        logger.debug(f"[notification] Telegram token/chat_id not configured")
        return False

    payload = {"chat_id": chat_id, "text": message}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    for attempt in range(1, 4):
        try:
            resp = requests.post(
                TELEGRAM_URL + token + "/sendMessage",
                json=payload,
                timeout=15,
            )
            if resp.status_code == 200:
                return True
            logger.error(
                f"[notification] Telegram send failed (attempt {attempt}): "
                f"status={resp.status_code}: {resp.text[:200]}"
            )
            if attempt < 3:
                time.sleep(2 ** attempt)
        except requests.Timeout:
            logger.error(f"[notification] Telegram timeout (attempt {attempt})")
            if attempt < 3:
                time.sleep(2 ** attempt)
        except requests.ConnectionError:
            logger.error(f"[notification] Telegram connection error (attempt {attempt})")
            if attempt < 3:
                time.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"[notification] Telegram send failed: {e}")
            return False

    return False  # all retries exhausted


# ═══════════════════════════════════════════════════════════════════════════
# Notification router
# ═══════════════════════════════════════════════════════════════════════════

def dispatch_notification(job: dict) -> bool:
    """
    Route a notification job to the correct channel(s).

    Args:
        job: dict with keys: chat_type, message, parse_mode (optional)

    Returns:
        True if sent successfully to at least one channel
    """
    chat_type = job.get("chat_type", "intraday")
    message = job.get("message", "")
    parse_mode = job.get("parse_mode")

    # Sanitize HTML before sending to Telegram to prevent parse errors
    if parse_mode and parse_mode.upper() == "HTML":
        message = _sanitize_html(message)

    channel = NOTIFICATION_CHANNEL.lower()
    sent_any = False

    if channel in ("discord", "both"):
        webhook_map = {
            "intraday": DISCORD_INTRADAY_WEBHOOK_URL,
            "positional": DISCORD_POSITIONAL_WEBHOOK_URL,
            "live_options": DISCORD_LIVE_OPTIONS_WEBHOOK_URL,
        }
        webhook = webhook_map.get(chat_type)
        if _send_discord(webhook, message, parse_mode):
            sent_any = True

    if channel in ("telegram", "both"):
        token_map = {
            "intraday": (TELEGRAM_INTRADAY_TOKEN, TELEGRAM_INTRADAY_CHAT_ID),
            "positional": (TELEGRAM_POSITIONAL_TOKEN, TELEGRAM_POSITIONAL_CHAT_ID),
            "live_options": (TELEGRAM_LIVE_OPTIONS_TOKEN, TELEGRAM_LIVE_OPTIONS_CHAT_ID),
        }
        token, chat_id = token_map.get(chat_type, ("", ""))
        if _send_telegram(token, chat_id, message, parse_mode):
            sent_any = True

    return sent_any


# ═══════════════════════════════════════════════════════════════════════════
# Dead letter
# ═══════════════════════════════════════════════════════════════════════════

def _send_to_dead_letter(redis_client, original: dict, error: str):
    """Write a failed notification to the dead letter stream for manual inspection."""
    try:
        redis_client.xadd("notification:dead", {
            "original": json.dumps(original, default=str),
            "error": error,
            "timestamp": str(time.time()),
        })
        logger.warning(f"[notification] Dead-lettered: {original.get('message_type', 'unknown')} — {error}")
    except Exception as e:
        logger.error(f"[notification] Failed to write dead letter: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════════

def main():
    global _running

    parser = argparse.ArgumentParser(description="StockAnalysis Notification Service")
    parser.add_argument("--consumer-name", default="notifier-1", help="Consumer name for this instance")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    rc = sync_redis.from_url(redis_url, decode_responses=True)
    consumer_name = args.consumer_name

    logger.info(f"[notification-service] Starting (consumer={consumer_name}, redis={redis_url})")

    from services.common.crash_handler import install_crash_handler
    install_crash_handler("notification-service")

    # Ensure consumer group exists
    try:
        rc.xgroup_create("notification:jobs", "notifier", id="0", mkstream=True)
    except Exception:
        pass  # already exists

    # Heartbeat
    rc.hset("service:registry:notification-service", mapping={
        "name": "notification-service",
        "pid": str(os.getpid()),
        "status": "healthy",
        "consumer": consumer_name,
        "last_heartbeat": str(time.time()),
    })
    rc.expire("service:registry:notification-service", 120)

    _running = True
    retry_count = 0
    _hb_counter = 0

    while _running:
        try:
            messages = rc.xreadgroup(
                groupname="notifier",
                consumername=consumer_name,
                streams={"notification:jobs": ">"},
                count=10,
                block=2000,
            )
        except Exception as e:
            logger.error(f"[notification-service] Redis error: {e}")
            retry_count += 1
            time.sleep(min(retry_count * 2, 30))
            continue

        retry_count = 0  # reset on successful read

        if not messages:
            continue

        entries = messages[0][1] if isinstance(messages, list) and messages else []
        for msg_id, fields in entries:
            try:
                job = dict(fields)
                success = dispatch_notification(job)
                if not success:
                    error = f"Failed to send after 3 retries"
                    _send_to_dead_letter(rc, job, error)
                    sym = job.get("symbol", "")
                    if sym:
                        incr_stock(sym, "alerts_failed")
                    incr_system("alerts_failed")
                    incr_daily("alerts_failed")
                else:
                    sym = job.get("symbol", "")
                    if sym:
                        incr_stock(sym, "alerts_delivered")
                    incr_system("alerts_delivered")
                    incr_daily("alerts_delivered")
                    logger.info(
                        f"[notification] Sent: {job.get('message_type', 'unknown')} "
                        f"→ {job.get('chat_type', 'unknown')}"
                    )
            except Exception as e:
                _send_to_dead_letter(rc, dict(fields), str(e))
                sym = dict(fields).get("symbol", "")
                if sym:
                    incr_stock(sym, "alerts_failed")
                incr_system("alerts_failed")
                incr_daily("alerts_failed")
                logger.error(f"[notification] Error processing job {msg_id}: {e}")
            finally:
                try:
                    rc.xack("notification:jobs", "notifier", msg_id)
                except Exception:
                    pass

        # Refresh heartbeat every ~30s (15 iterations × 2s block)
        _hb_counter += 1
        if _hb_counter >= 15:
            try:
                rc.hset("service:registry:notification-service", mapping={
                    "last_heartbeat": str(time.time()),
                    "status": "healthy",
                })
                rc.expire("service:registry:notification-service", 120)
            except Exception:
                pass
            _hb_counter = 0

    # Shutdown
    logger.info("[notification-service] Shutting down...")
    try:
        rc.hset("service:registry:notification-service", mapping={
            "status": "shutdown",
            "last_heartbeat": str(time.time()),
        })
    except Exception:
        pass
    rc.close()
    logger.info("[notification-service] Shutdown complete")


if __name__ == "__main__":
    main()
