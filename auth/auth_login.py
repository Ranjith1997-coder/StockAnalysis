import os
import sys
import datetime
import pathlib
import argparse
import requests
import pyotp
import logging
from dotenv import load_dotenv, set_key

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

# Persists across service restarts; outside the repo so it's never committed.
LOCK_FILE = pathlib.Path.home() / ".zerodha_auth_last_run"


def _already_ran_today() -> bool:
    try:
        if LOCK_FILE.exists():
            return LOCK_FILE.read_text().strip() == str(datetime.date.today())
    except Exception:
        pass
    return False


def _mark_ran_today() -> None:
    try:
        LOCK_FILE.write_text(str(datetime.date.today()))
    except Exception as e:
        logging.warning(f"Could not write lock file: {e}")


def generate_enctoken() -> tuple[bool, "requests.Session | None"]:
    """Run the full Zerodha login flow and write the enctoken to .env.

    Returns (True, session) on success, (False, None) on any failure.
    The session object is returned so callers can reuse it for Sensibull OAuth
    without doing a second login (which would invalidate this enctoken).
    """
    load_dotenv()
    user_id      = os.getenv("ZERODHA_USER")
    password     = os.getenv("ZERODHA_PASS")
    totp_secret  = os.getenv("ZERODHA_TOTP_SECRET")
    env_file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

    if not all([user_id, password, totp_secret]):
        logging.error("Missing ZERODHA_USER / ZERODHA_PASS / ZERODHA_TOTP_SECRET in .env")
        return False, None

    session = requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/114.0.0.0 Safari/537.36"
        ),
        "X-Kite-Version": "3",
    }

    try:
        logging.info("Initiating Zerodha login...")
        login_resp = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": user_id, "password": password},
            headers=headers,
        ).json()

        if login_resp.get("status") != "success":
            logging.error(f"Login failed: {login_resp.get('message')}")
            return False, None

        request_id = login_resp["data"]["request_id"]
        totp_pin   = pyotp.TOTP(totp_secret).now()

        logging.info("Completing 2FA...")
        twofa_resp = session.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id":    user_id,
                "request_id": request_id,
                "twofa_value": totp_pin,
            },
            headers=headers,
        ).json()

        if twofa_resp.get("status") != "success":
            logging.error(f"2FA failed: {twofa_resp.get('message')}")
            return False, None

        enctoken = session.cookies.get("enctoken")
        if not enctoken:
            logging.error("Could not find enctoken in cookies.")
            return False, None

        set_key(env_file_path, "ZERODHA_ENC_TOKEN", enctoken)
        logging.info("✅ enctoken refreshed successfully.")
        return True, session

    except Exception as e:
        logging.error(f"Auth failed: {e}")
        return False, None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Zerodha enctoken refresher")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the once-per-day guard and force a fresh login.",
    )
    args = parser.parse_args()

    if not args.force and _already_ran_today():
        logging.info(f"Auth already ran today ({datetime.date.today()}) — skipping.")
        logging.info("Run with --force or 'make auth-force' to override.")
        sys.exit(0)

    success, _session = generate_enctoken()
    if success:
        _mark_ran_today()   # only written on success — failure retries next run
        sys.exit(0)
    else:
        sys.exit(1)         # non-zero → systemd blocks the monitor service
