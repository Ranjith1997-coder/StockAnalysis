
# Import the following modules
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
)
from common.logging_util import logger
 
# Function to send Push Notification
 
class TELEGRAM_NOTIFICATIONS:
    is_production = 0
    is_intraday = True
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
        """Send a Telegram message.

        Args:
            message: The text to send.
            parse_mode: Optional. 'HTML' or 'Markdown' for rich formatting.
                        None sends as plain text (backward-compatible default).
        """
        if not cls.is_production:
            return
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
        resp = None
        for attempt in range(1, 4):  # up to 3 attempts
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
                        _sleep(2 ** attempt)  # 2s, 4s back-off
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

        return False

    @classmethod
    def send_live_options_notification(cls, message, parse_mode="HTML"):
        """Send a real-time options alert to the dedicated live options Telegram channel."""
        if not cls.is_production:
            return
        if not TELEGRAM_LIVE_OPTIONS_TOKEN or not TELEGRAM_LIVE_OPTIONS_CHAT_ID:
            logger.debug("TELEGRAM_LIVE_OPTIONS_TOKEN/CHAT_ID not configured — skipping live options alert")
            return False
        msg = {"chat_id": TELEGRAM_LIVE_OPTIONS_CHAT_ID, "text": message}
        if parse_mode:
            msg["parse_mode"] = parse_mode
        try:
            resp = requests.post(
                TELEGRAM_URL + TELEGRAM_LIVE_OPTIONS_TOKEN + "/sendMessage",
                json=msg,
                timeout=10
            )
            if resp.status_code != 200:
                logger.error(f"Live options Telegram send failed: {resp.status_code}: {resp.text}")
                return False
            return True
        except Exception as e:
            logger.error(f"Live options Telegram send failed: {e}")
            return False
