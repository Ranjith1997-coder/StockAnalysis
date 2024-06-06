
# Import the following modules
import requests
import json
 
# Function to send Push Notification
 
 
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

def telegram_notif(message):
 
    TOKEN = '7042349293:AAGW0-OzOwfvbKdkuM6G40UfcXIHcs_YJwk'  # Pass your Access Token here
    # Make a dictionary that includes, title and body
    msg = {"chat_id": "1462841143", "text": message}
    # Sent a posts request
    resp = requests.post(' https://api.telegram.org/bot'+TOKEN+"/sendMessage",
                         data=json.dumps(msg),
                         headers={'Content-Type': 'application/json'})
    if resp.status_code != 200:  # Check if fort message send with the help of status code
        raise Exception('Error', resp.status_code)
    else:
        print('Message sent')
