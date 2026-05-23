
import os
import requests
import pyotp
import logging
from dotenv import load_dotenv, set_key

# Set up basic logging
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

def generate_enctoken():
    # Load credentials
    load_dotenv()
    user_id = os.getenv("ZERODHA_USER")
    password = os.getenv("ZERODHA_PASS")
    totp_secret = os.getenv("ZERODHA_TOTP_SECRET")
    env_file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')

    if not all([user_id, password, totp_secret]):
        logging.error("Missing Zerodha credentials in .env file.")
        return

    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "X-Kite-Version": "3"
    }

    try:
        # 1. First POST request: Send Username and Password
        logging.info("Initiating Zerodha Login...")
        login_resp = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": user_id, "password": password},
            headers=headers
        ).json()

        if login_resp.get("status") != "success":
            logging.error(f"Login failed: {login_resp.get('message')}")
            return

        request_id = login_resp["data"]["request_id"]

        # 2. Generate live TOTP pin mathematically
        totp_pin = pyotp.TOTP(totp_secret).now()

        # 3. Second POST request: Send TOTP and Request ID
        logging.info("Bypassing 2FA...")
        twofa_resp = session.post(
            "https://kite.zerodha.com/api/twofa",
            data={"user_id": user_id, "request_id": request_id, "twofa_value": totp_pin},
            headers=headers
        ).json()

        if twofa_resp.get("status") != "success":
            logging.error(f"2FA failed: {twofa_resp.get('message')}")
            return

        # 4. Extract the holy grail (enctoken) from the cookies
        enctoken = session.cookies.get("enctoken")
        if enctoken:
            # Safely write it directly into your .env file
            set_key(env_file_path, "ZERODHA_ENC_TOKEN", enctoken)
            logging.info("✅ Successfully generated and injected new enctoken!")
        else:
            logging.error("Could not find enctoken in cookies.")

    except Exception as e:
        logging.error(f"Automation failed: {str(e)}")

if __name__ == "__main__":
    generate_enctoken()
