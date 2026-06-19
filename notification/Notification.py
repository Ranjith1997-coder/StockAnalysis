
# Import the following modules
import re
import requests
import json
import os
from common.constants import (
    TELEGRAM_INTRADAY_CHAT_ID,
    TELEGRAM_INTRADAY_TOKEN,
    TELEGRAM_POSITIONAL_CHAT_ID,
    TELEGRAM_POSITIONAL_TOKEN,
    TELEGRAM_LIVE_OPTIONS_TOKEN,
    TELEGRAM_LIVE_OPTIONS_CHAT_ID,
    TELEGRAM_URL,
    ENV_PRODUCTION,
    NOTIFICATION_CHANNEL,
    DISCORD_INTRADAY_WEBHOOK_URL,
    DISCORD_POSITIONAL_WEBHOOK_URL,
    DISCORD_LIVE_OPTIONS_WEBHOOK_URL,
)
from common.logging_util import logger


def _html_to_discord(text: str) -> str:
    """Convert Telegram HTML tags to Discord markdown."""
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)  # strip remaining tags
    return text


def _send_discord(webhook_url: str, message: str, parse_mode: str | None = None) -> bool:
    """POST a message to a Discord webhook. Returns True on success."""
    if not webhook_url:
        return False
    content = _html_to_discord(message) if parse_mode and parse_mode.upper() == "HTML" else message
    # Discord message limit is 2000 chars
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
 
# Function to send Push Notification
 
class TELEGRAM_NOTIFICATIONS:
    is_production = 0
    is_intraday = True
    dev_notify = False   # Set to True (via DEV_NOTIFY=1) to send alerts in dev mode
    @classmethod
    def pushbullet_notif(cls,title, body):
    
        TOKEN = 'o.6J1qIOmIRX4MEtgqCho761YLe0VJcanD'  # Pass your Access Token here
        # Make a dictionary that includes, title and body
        msg = {"type": "note", "title": title, "body": body}
        # Sent a posts request
        resp = requests.post('https://api.pushbullet.com/v2/pushes',
                            data=json.dumps(msg),
                            headers={'Authorization': 'Bearer ' + TOKEN,
                                    'Content-Type': 'application/json'})
        if resp.status_code != 200:  # Check if fort message send with the help of status code
            raise Exception('Error', resp.status_code)
        else:
            print('Message sent')
    @classmethod
    def send_notification(cls, message, parse_mode=None):
        """Send a notification via Telegram, Discord, or both (controlled by NOTIFICATION_CHANNEL)."""
        if not cls.is_production and not cls.dev_notify:
            return

        channel = NOTIFICATION_CHANNEL.lower()

        if channel in ("discord", "both"):
            webhook = (
                DISCORD_INTRADAY_WEBHOOK_URL
                if TELEGRAM_NOTIFICATIONS.is_intraday
                else DISCORD_POSITIONAL_WEBHOOK_URL
            )
            _send_discord(webhook, message, parse_mode)

        if channel in ("telegram", "both"):
            TELEGRAM_CHAT_ID = ""
            TELEGRAM_TOKEN = ""
            if TELEGRAM_NOTIFICATIONS.is_intraday:
                TELEGRAM_CHAT_ID = TELEGRAM_INTRADAY_CHAT_ID
                TELEGRAM_TOKEN = TELEGRAM_INTRADAY_TOKEN
            else:
                TELEGRAM_CHAT_ID = TELEGRAM_POSITIONAL_CHAT_ID
                TELEGRAM_TOKEN = TELEGRAM_POSITIONAL_TOKEN

            msg = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
            if parse_mode:
                msg["parse_mode"] = parse_mode
            for attempt in range(1, 4):
                try:
                    resp = requests.post(
                        TELEGRAM_URL + TELEGRAM_TOKEN + "/sendMessage",
                        json=msg,
                        timeout=15,
                    )
                    if resp.status_code != 200:
                        logger.error(f"Telegram send failed (attempt {attempt}) with status {resp.status_code}: {resp.text}")
                        if attempt < 3:
                            from time import sleep as _sleep
                            _sleep(2 ** attempt)
                        continue
                    logger.info("Message sent successfully")
                    logger.debug(f"Message: {message}")
                    return True
                except requests.Timeout:
                    logger.error(f"Telegram send timeout (attempt {attempt})")
                    if attempt < 3:
                        from time import sleep as _sleep
                        _sleep(2 ** attempt)
                except requests.ConnectionError:
                    logger.error(f"Telegram connection error (attempt {attempt})")
                    if attempt < 3:
                        from time import sleep as _sleep
                        _sleep(2 ** attempt)
                except Exception as e:
                    logger.error(f"Telegram send failed: {e}")
                    return False
            return False  # all retry attempts exhausted

        return True

    @classmethod
    def send_live_options_notification(cls, message, parse_mode="HTML"):
        """Send a real-time options alert to the dedicated live options channel."""
        if not cls.is_production and not cls.dev_notify:
            return

        channel = NOTIFICATION_CHANNEL.lower()

        if channel in ("discord", "both"):
            _send_discord(DISCORD_LIVE_OPTIONS_WEBHOOK_URL, message, parse_mode)

        if channel in ("telegram", "both"):
            if not TELEGRAM_LIVE_OPTIONS_TOKEN or not TELEGRAM_LIVE_OPTIONS_CHAT_ID:
                logger.debug("TELEGRAM_LIVE_OPTIONS_TOKEN/CHAT_ID not configured — skipping live options Telegram alert")
            else:
                msg = {"chat_id": TELEGRAM_LIVE_OPTIONS_CHAT_ID, "text": message}
                if parse_mode:
                    msg["parse_mode"] = parse_mode
                try:
                    resp = requests.post(
                        TELEGRAM_URL + TELEGRAM_LIVE_OPTIONS_TOKEN + "/sendMessage",
                        json=msg,
                        timeout=10,
                    )
                    if resp.status_code != 200:
                        logger.error(f"Live options Telegram send failed: {resp.status_code}: {resp.text}")
                except Exception as e:
                    logger.error(f"Live options Telegram send failed: {e}")

        return True
