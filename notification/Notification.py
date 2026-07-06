import re
import requests
import json
import os
import datetime
from common.constants import (
    TELEGRAM_INTRADAY_CHAT_ID,
    TELEGRAM_INTRADAY_TOKEN,
    TELEGRAM_POSITIONAL_CHAT_ID,
    TELEGRAM_POSITIONAL_TOKEN,
    TELEGRAM_LIVE_OPTIONS_TOKEN,
    TELEGRAM_LIVE_OPTIONS_CHAT_ID,
    TELEGRAM_URL,
    NOTIFICATION_CHANNEL,
    DISCORD_INTRADAY_WEBHOOK_URL,
    DISCORD_POSITIONAL_WEBHOOK_URL,
    DISCORD_LIVE_OPTIONS_WEBHOOK_URL,
)
from common.logging_util import logger
from services.common.metrics import incr_stock, incr_system, incr_daily


def _html_to_discord(text: str) -> str:
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text


_TELEGRAM_SAFE_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike",
                        "del", "code", "pre", "a", "tg-spoiler", "blockquote"}


def _sanitize_html(text: str) -> str:
    """Sanitize HTML for Telegram's strict parser — removes empty/unsupported tags."""
    text = re.sub(r"<(\w+)>\s*</\1>", "", text)
    text = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+);)", "&amp;", text)

    def _replace_tag(m):
        full = m.group(0)
        tag_name = m.group(1).lower()
        if tag_name in _TELEGRAM_SAFE_TAGS:
            return full
        return ""

    text = re.sub(r"</?([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^>]*)?>", _replace_tag, text)
    text = re.sub(r"<(\w+)>\s*</\1>", "", text)
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
# Redis dispatch — primary notification path.
# All notifications are written to Redis stream "notification:jobs" and
# the notification-service (separate process) reads and sends them.
# Direct HTTP is the fallback when Redis is unavailable.
# ═══════════════════════════════════════════════════════════════════════════

_REDIS_CLIENT = None
_REDIS_AVAILABLE = None  # None=not checked, True/False after first attempt

def _get_redis():
    global _REDIS_CLIENT, _REDIS_AVAILABLE
    if _REDIS_AVAILABLE is False:
        return None
    if _REDIS_CLIENT is None:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        try:
            import redis as _sync_redis
            _REDIS_CLIENT = _sync_redis.from_url(redis_url, decode_responses=True)
            _REDIS_CLIENT.ping()
            _REDIS_AVAILABLE = True
            logger.info(f"[Notification] Redis dispatch active at {redis_url}")
        except Exception as e:
            logger.warning(f"[Notification] Redis unavailable: {e}. Using direct HTTP.")
            _REDIS_AVAILABLE = False
            return None
    return _REDIS_CLIENT

def _notify_via_redis(chat_type: str, message: str, parse_mode=None, message_type="general", symbol=None) -> bool:
    rc = _get_redis()
    if not rc:
        return False
    try:
        rc.xadd("notification:jobs", {
            "chat_type": chat_type,
            "message": message,
            "parse_mode": parse_mode or "",
            "message_type": message_type,
            "symbol": symbol or "",
            "timestamp": str(datetime.datetime.now()),
        }, maxlen=1000)
        # Producer-side alert counters
        if symbol:
            incr_stock(symbol, "alerts_attempted")
        incr_system("alerts_attempted")
        incr_daily("alerts_attempted")
        return True
    except Exception as e:
        logger.error(f"[Notification] Redis dispatch failed: {e}")
        return False


class TELEGRAM_NOTIFICATIONS:
    is_production = 0
    is_intraday = True
    dev_notify = False

    @classmethod
    def send_notification(cls, message, parse_mode=None, symbol=None):
        if not cls.is_production and not cls.dev_notify:
            return

        # Primary: Redis stream → notification-service
        chat_type = "intraday" if cls.is_intraday else "positional"
        if _notify_via_redis(chat_type, message, parse_mode, message_type="general", symbol=symbol):
            return

        # Fallback: direct HTTP
        channel = NOTIFICATION_CHANNEL.lower()

        if channel in ("discord", "both"):
            webhook = (
                DISCORD_INTRADAY_WEBHOOK_URL
                if cls.is_intraday
                else DISCORD_POSITIONAL_WEBHOOK_URL
            )
            _send_discord(webhook, message, parse_mode)

        if channel in ("telegram", "both"):
            chat_id = TELEGRAM_INTRADAY_CHAT_ID if cls.is_intraday else TELEGRAM_POSITIONAL_CHAT_ID
            token = TELEGRAM_INTRADAY_TOKEN if cls.is_intraday else TELEGRAM_POSITIONAL_TOKEN
            if parse_mode and parse_mode.upper() == "HTML":
                message = _sanitize_html(message)
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
                        logger.info("Message sent directly (Redis fallback)")
                        return True
                    logger.error(f"Telegram send failed (attempt {attempt}): {resp.status_code}")
                    if attempt < 3:
                        from time import sleep as _sleep
                        _sleep(2 ** attempt)
                except (requests.Timeout, requests.ConnectionError) as e:
                    logger.error(f"Telegram {type(e).__name__} (attempt {attempt})")
                    if attempt < 3:
                        from time import sleep as _sleep
                        _sleep(2 ** attempt)
                except Exception as e:
                    logger.error(f"Telegram send failed: {e}")
                    return False
            return False
        return True

    @classmethod
    def send_live_options_notification(cls, message, parse_mode="HTML", symbol=None):
        if not cls.is_production and not cls.dev_notify:
            return

        # Primary: Redis stream
        if _notify_via_redis("live_options", message, parse_mode, message_type="live_options", symbol=symbol):
            return

        # Fallback: direct HTTP
        channel = NOTIFICATION_CHANNEL.lower()
        if channel in ("discord", "both"):
            _send_discord(DISCORD_LIVE_OPTIONS_WEBHOOK_URL, message, parse_mode)
        if channel in ("telegram", "both"):
            if not TELEGRAM_LIVE_OPTIONS_TOKEN or not TELEGRAM_LIVE_OPTIONS_CHAT_ID:
                logger.debug("Live options Telegram not configured")
                return False
            else:
                try:
                    if parse_mode and parse_mode.upper() == "HTML":
                        message = _sanitize_html(message)
                    resp = requests.post(
                        TELEGRAM_URL + TELEGRAM_LIVE_OPTIONS_TOKEN + "/sendMessage",
                        json={"chat_id": TELEGRAM_LIVE_OPTIONS_CHAT_ID, "text": message, "parse_mode": parse_mode},
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        logger.error(f"Live options send failed: {resp.status_code}: {resp.text}")
                        return False
                    return True
                except Exception as e:
                    logger.error(f"Live options send failed: {e}")
                    return False
        return True
