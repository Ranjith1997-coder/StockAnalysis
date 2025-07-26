
# Import the following modules
import requests
import json
from common.constants import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_URL
 
# Function to send Push Notification
 
class TELEGRAM_NOTIFICATIONS:
    @staticmethod
    def pushbullet_notif(title, body):
    
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
    @staticmethod
    def send_notification(message):
         # Pass your Access Token here
        # Make a dictionary that includes, title and body
        msg = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        # Sent a posts request
        resp = requests.post(TELEGRAM_URL + TELEGRAM_TOKEN+ "/sendMessage",
                            data=json.dumps(msg),
                            headers={'Content-Type': 'application/json'})
        if resp.status_code != 200:  # Check if fort message send with the help of status code
            raise Exception('Error: unable to send message', resp.status_code)
