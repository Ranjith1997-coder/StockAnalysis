
# Import the following modules
import requests
import json
import os
from common.constants import (
    TELEGRAM_INTRADAY_CHAT_ID,
    TELEGRAM_INTRADAY_TOKEN,
    TELEGRAM_POSITIONAL_CHAT_ID,
    TELEGRAM_POSITIONAL_TOKEN,
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
    def send_notification(cls, message):
         # Pass your Access Token here
        # Make a dictionary that includes, title and body
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
        resp = None
        try:
            resp = requests.post(
                TELEGRAM_URL + TELEGRAM_TOKEN + "/sendMessage",
                json=msg,
                timeout=10
            )
            
            if resp.status_code != 200:
                logger.error(f"Telegram send failed with status {resp.status_code}: {resp.text}")
                return False
            
            logger.info("Message sent successfully")
            logger.debug(f"Message: {message}")
            return True
            
        except requests.Timeout:
            logger.error("Telegram send timeout")
            return False
        except requests.ConnectionError:
            logger.error("Telegram connection error")
            return False
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False
