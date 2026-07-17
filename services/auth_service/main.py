"""
Auth Service — Zerodha enctoken lifecycle manager.

Runs 24/7 as a standalone systemd service. Responsibilities:
  1. Scheduled TOTP login at 09:00 (pre-market) and 18:50 (evening proactive)
  2. Reactive refresh via auth:commands stream (triggered by data-gateway 403s)
  3. Publishes fresh enctoken to Redis hash + Pub/Sub for all consumers

Redis interactions:
  - HSET auth:zerodha {enctoken, issued_at, user_id, last_reason}
  - PUBLISH auth:enctoken_refreshed
  - XREADGROUP auth:commands (consumer group "auth-service")
    -> on "refresh_enctoken" command, runs TOTP login

No KiteConnect instance — this service only does HTTP login + Redis publish.
It never connects to Zerodha WebSocket or calls Zerodha REST APIs.
"""
from __future__ import annotations

import os
import sys
import time
import signal
import threading
from datetime import datetime, time as dtime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from dotenv import load_dotenv
load_dotenv()

import common.constants as constant
from services.common.logging import get_logger
logger = get_logger("auth-service")
from services.common.redis_proxy import RedisProxy
from services.common.version import BUILD_LABEL, GIT_COMMIT, GIT_DIRTY
from services.common.metrics import incr_system

AUTH_HASH = "auth:zerodha"
AUTH_CHANNEL = "auth:enctoken_refreshed"
AUTH_COMMANDS_STREAM = "auth:commands"
AUTH_COMMANDS_GROUP = "auth-service"
SENSIBULL_HASH = "auth:sensibull"
SENSIBULL_CHANNEL = "auth:sensibull_refreshed"

SENSIBULL_LOGIN_URL = "https://oxide.sensibull.com/v1/pluto/auth/web/session/b/u/kite/platform/login"
SENSIBULL_GENERATE_URL = "https://oxide.sensibull.com/v1/pluto/auth/web/session/b/u/kite/platform/generate"

_running = True


def signal_handler(signum, frame):
    global _running
    logger.info(f"[auth-service] Received signal {signum}, shutting down...")
    _running = False


# ═══════════════════════════════════════════════════════════════════════════
# Core: TOTP login + Redis publish
# ═══════════════════════════════════════════════════════════════════════════

def _do_refresh(redis: RedisProxy, reason: str = "scheduled") -> bool:
    """Run TOTP login and publish enctoken to Redis.

    Returns True on success, False on failure.
    """
    try:
        from auth.auth_login import generate_enctoken
        success, session = generate_enctoken()
        if not success:
            logger.error(f"[auth-service] TOTP login failed (reason={reason})")
            _send_alert(redis, f"Zerodha auth refresh failed ({reason})")
            return False
    except Exception as e:
        logger.exception(f"[auth-service] TOTP login error (reason={reason}): {e}")
        _send_alert(redis, f"Zerodha auth refresh error: {e}")
        return False

    load_dotenv(override=True)
    enctoken = os.getenv(constant.ENV_ZERODHA_ENC_TOKEN)
    if not enctoken:
        logger.error("[auth-service] enctoken not found in .env after login")
        _send_alert(redis, "Zerodha auth: enctoken missing after login")
        return False

    now_ts = time.time()
    redis.hset(AUTH_HASH, mapping={
        "enctoken": enctoken,
        "issued_at": str(now_ts),
        "user_id": os.getenv("ZERODHA_USER", ""),
        "last_reason": reason,
    })
    redis.publish(AUTH_CHANNEL, f"issued_at={int(now_ts)}")
    logger.info(f"[auth-service] Enctoken refreshed and published (reason={reason})")
    incr_system("auth_refreshes")

    # Also refresh Sensibull session — reuse the Zerodha session to avoid
    # a second login that would invalidate this enctoken.
    _do_sensibull_refresh(redis, reason=reason, zerodha_session=session)

    return True


def _do_sensibull_refresh(redis: RedisProxy, reason: str = "scheduled",
                           zerodha_session=None) -> bool:
    """Refresh Sensibull access_token using the Zerodha session.

    Flow:
      1. Use existing Zerodha session (from generate_enctoken) to hit Sensibull login endpoint
      2. Follow OAuth redirect chain → get request_token
      3. POST request_token to Sensibull generate endpoint → get access_token + client_info

    If zerodha_session is None, does a fresh Zerodha login (fallback for standalone use).
    WARNING: a fresh login invalidates any existing enctoken — prefer passing the session.

    Publishes access_token + client_info to Redis hash 'auth:sensibull'.
    """
    import requests
    import pyotp
    from urllib.parse import urlparse, parse_qs

    try:
        if zerodha_session is not None:
            session = zerodha_session
        else:
            logger.warning("[auth-service] Sensibull refresh: no Zerodha session — doing fresh login (may invalidate existing enctoken)")
            load_dotenv(override=True)
            user_id = os.getenv("ZERODHA_USER")
            password = os.getenv("ZERODHA_PASS")
            totp_secret = os.getenv("ZERODHA_TOTP_SECRET")

            if not all([user_id, password, totp_secret]):
                logger.error("[auth-service] Sensibull refresh: missing Zerodha credentials")
                return False

            session = requests.Session()
            zk_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "X-Kite-Version": "3",
            }
            login_resp = session.post(
                "https://kite.zerodha.com/api/login",
                data={"user_id": user_id, "password": password},
                headers=zk_headers, timeout=10,
            ).json()

            if login_resp.get("status") != "success":
                logger.error(f"[auth-service] Sensibull refresh: Zerodha login failed: {login_resp.get('message')}")
                return False

            request_id = login_resp["data"]["request_id"]
            totp_pin = pyotp.TOTP(totp_secret).now()
            twofa_resp = session.post(
                "https://kite.zerodha.com/api/twofa",
                data={"user_id": user_id, "request_id": request_id, "twofa_value": totp_pin},
                headers=zk_headers, timeout=10,
            ).json()

            if twofa_resp.get("status") != "success":
                logger.error(f"[auth-service] Sensibull refresh: Zerodha 2FA failed: {twofa_resp.get('message')}")
                return False

        # Step 2: Follow Sensibull OAuth redirect chain (auto-approves since we're logged in)
        r = session.get(
            SENSIBULL_LOGIN_URL,
            headers={"Origin": "https://web.sensibull.com"},
            allow_redirects=True, timeout=15,
        )

        final_url = r.url
        parsed = urlparse(final_url)
        params = parse_qs(parsed.query)
        request_token = params.get("request_token", [None])[0]

        if not request_token:
            logger.error(f"[auth-service] Sensibull refresh: no request_token in redirect URL: {final_url}")
            _send_alert(redis, "Sensibull auth: no request_token in OAuth redirect")
            return False

        logger.debug(f"[auth-service] Sensibull OAuth: got request_token from redirect")

        # Step 3: Exchange request_token for Sensibull access_token
        gen_r = requests.post(
            SENSIBULL_GENERATE_URL,
            json={"request_token": request_token},
            headers={
                "Origin": "https://web.sensibull.com",
                "Referer": "https://web.sensibull.com/",
                "Content-Type": "application/json",
            },
            timeout=10,
        )

        if gen_r.status_code != 200 or not gen_r.json().get("success"):
            logger.error(f"[auth-service] Sensibull refresh: generate failed: {gen_r.status_code} {gen_r.text[:200]}")
            _send_alert(redis, f"Sensibull auth: generate endpoint failed ({gen_r.status_code})")
            return False

        access_token = gen_r.cookies.get("access_token")
        client_info = gen_r.cookies.get("client_info")

        if not access_token:
            logger.error("[auth-service] Sensibull refresh: no access_token in generate response cookies")
            return False

        # Step 4: Publish to Redis
        redis.hset(SENSIBULL_HASH, mapping={
            "access_token": access_token,
            "client_info": client_info or "",
            "issued_at": str(time.time()),
            "last_reason": reason,
        })
        redis.publish(SENSIBULL_CHANNEL, f"issued_at={int(time.time())}")

        # Also update .env so the data-gateway picks it up on restart
        from dotenv import set_key
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        set_key(env_path, "SENSIBULL_ACCESS_TOKEN", access_token)
        if client_info:
            set_key(env_path, "SENSIBULL_CLIENT_INFO", client_info)

        logger.info(f"[auth-service] Sensibull access_token refreshed (reason={reason})")
        return True

    except Exception as e:
        logger.exception(f"[auth-service] Sensibull refresh error: {e}")
        _send_alert(redis, f"Sensibull auth refresh error: {e}")
        return False


def _send_alert(redis: RedisProxy, message: str):
    """Send a crash/ failure alert via notification:jobs stream."""
    try:
        redis.xadd("notification:jobs", {
            "chat_type": "intraday",
            "message": f"\U0001F6A8 <b>Auth Service Alert</b>\n\n{message}",
            "parse_mode": "HTML",
            "message_type": "auth_alert",
            "priority": "HIGH",
            "timestamp": str(datetime.now().isoformat()),
        }, maxlen=100)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Reactive refresh: auth:commands stream consumer
# ═══════════════════════════════════════════════════════════════════════════

_last_refresh_ts = 0.0
_REFRESH_COOLDOWN = 30.0


def _start_auth_commands_consumer(redis: RedisProxy):
    """Background thread consuming auth:commands stream for reactive enctoken refresh.

    The data-gateway publishes 'refresh_enctoken' commands when it gets 403/Bad Request
    from Zerodha. This consumer runs the TOTP login and publishes the new token.
    """
    global _last_refresh_ts

    consumer = "auth-1"
    try:
        redis.xgroup_create(AUTH_COMMANDS_GROUP, AUTH_COMMANDS_STREAM, mkstream=True)
    except Exception:
        pass
    logger.info("[auth-service] Started auth:commands consumer thread")

    def _consume():
        global _last_refresh_ts
        while _running:
            try:
                messages = redis.xreadgroup(
                    AUTH_COMMANDS_GROUP, consumer,
                    {AUTH_COMMANDS_STREAM: ">"},
                    count=1, block=10000,
                )
                if not messages:
                    continue
                entries = messages[0][1] if isinstance(messages, list) and messages else []
                for msg_id, fields in entries:
                    command = fields.get("command", "")
                    if command == "refresh_enctoken":
                        now = time.time()
                        if now - _last_refresh_ts < _REFRESH_COOLDOWN:
                            reason = fields.get("reason", "unknown")
                            logger.debug(
                                f"[auth-service] Refresh throttled (reason={reason}, "
                                f"cooldown={_REFRESH_COOLDOWN}s)"
                            )
                        else:
                            _last_refresh_ts = now
                            reason = fields.get("reason", "unknown")
                            logger.info(f"[auth-service] Reactive refresh (reason={reason})")
                            _do_refresh(redis, reason=f"reactive:{reason}")
                    elif command == "refresh_sensibull":
                        reason = fields.get("reason", "unknown")
                        logger.info(f"[auth-service] Reactive Sensibull refresh (reason={reason})")
                        _do_sensibull_refresh(redis, reason=f"reactive:{reason}")
                    try:
                        redis.xack(AUTH_COMMANDS_STREAM, AUTH_COMMANDS_GROUP, msg_id)
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"[auth-service] Consumer error: {e}")
                time.sleep(5)

    t = threading.Thread(target=_consume, daemon=True, name="auth-commands-consumer")
    t.start()


# ═══════════════════════════════════════════════════════════════════════════
# Heartbeat
# ═══════════════════════════════════════════════════════════════════════════

def _update_heartbeat(redis: RedisProxy):
    redis.hset("service:registry:auth-service", mapping={
        "name": "auth-service",
        "pid": str(os.getpid()),
        "status": "healthy",
        "last_heartbeat": str(time.time()),
        "version": BUILD_LABEL,
        "commit": GIT_COMMIT,
        "dirty": str(GIT_DIRTY),
    })
    redis.expire("service:registry:auth-service", 120)


# ═══════════════════════════════════════════════════════════════════════════
# Scheduling
# ═══════════════════════════════════════════════════════════════════════════

def _wait_until(hour: int, minute: int):
    """Sleep until specified time today. Returns immediately if already past."""
    while _running:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        sleep_sec = (target - now).total_seconds()
        if sleep_sec <= 0:
            return
        time.sleep(min(sleep_sec, 60))


def _sleep_until_midnight():
    """Sleep until midnight, re-checking every 5 min."""
    target = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while _running:
        sleep_sec = (target - datetime.now()).total_seconds()
        if sleep_sec <= 0:
            break
        time.sleep(min(sleep_sec, 300))


def _run_schedule(redis: RedisProxy):
    """Self-scheduling loop: refresh at 09:00 and 18:50, idle otherwise."""
    global _last_refresh_ts

    logger.info(f"[auth-service] v{BUILD_LABEL} starting")
    logger.info("[auth-service] Entering scheduling loop")

    _morning_done = False
    _evening_done = False
    _schedule_date = datetime.now().date()

    while _running:
        try:
            now = datetime.now()
            today = now.date()

            # Reset daily flags at midnight
            if today != _schedule_date:
                _morning_done = False
                _evening_done = False
                _schedule_date = today

            hour_min = now.hour * 60 + now.minute

            if hour_min < 555 and not _morning_done:  # before 09:15 and morning not done
                _wait_until(9, 0)
                if not _running:
                    break
                _last_refresh_ts = time.time()
                _do_refresh(redis, reason="scheduled_morning")
                _morning_done = True
            elif hour_min < 1130 and not _evening_done:  # before 18:50 and evening not done
                _wait_until(18, 50)
                if not _running:
                    break
                _last_refresh_ts = time.time()
                _do_refresh(redis, reason="scheduled_evening")
                _evening_done = True
            elif hour_min >= 1130:  # past 18:50
                logger.info("[auth-service] Past 18:50 — sleeping until midnight")
                _sleep_until_midnight()
                continue
            else:
                # Both morning and evening done — sleep until next event
                time.sleep(60)

            _update_heartbeat(redis)
        except Exception as e:
            logger.exception(f"[auth-service] Schedule error: {e}")
            time.sleep(60)


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    global _running

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = RedisProxy(redis_url)

    try:
        redis.get("ping")
        logger.info(f"[auth-service] Connected to Redis at {redis_url}")
    except Exception as e:
        logger.error(f"[auth-service] Cannot connect to Redis: {e}")
        sys.exit(1)

    from services.common.crash_handler import install_crash_handler
    install_crash_handler("auth-service")

    _update_heartbeat(redis)

    # Check if enctoken already exists in Redis (from a previous run)
    existing = redis.hget(AUTH_HASH, "enctoken")
    if existing:
        logger.info("[auth-service] Existing enctoken found in Redis — waiting for next scheduled refresh")
    else:
        # No enctoken yet — if before 09:15, refresh immediately
        now = datetime.now()
        if now.time() < dtime(9, 15):
            logger.info("[auth-service] No enctoken in Redis and before market open — refreshing now")
            _last_refresh_ts = time.time()
            _do_refresh(redis, reason="startup")
        else:
            logger.info("[auth-service] No enctoken in Redis — waiting for next scheduled refresh")

    # Start reactive refresh consumer
    _start_auth_commands_consumer(redis)

    # Enter scheduling loop
    _heartbeat_counter = 0
    schedule_thread = threading.Thread(target=_run_schedule, args=(redis,), daemon=True, name="auth-schedule")
    schedule_thread.start()

    # Heartbeat loop (main thread)
    while _running:
        time.sleep(30)
        _heartbeat_counter += 1
        try:
            _update_heartbeat(redis)
        except Exception as e:
            logger.error(f"[auth-service] Heartbeat error: {e}")

    # Shutdown
    logger.info("[auth-service] Shutting down...")
    try:
        redis.hset("service:registry:auth-service", mapping={
            "status": "shutdown",
            "last_heartbeat": str(time.time()),
        })
    except Exception:
        pass
    redis.close()
    logger.info("[auth-service] Shutdown complete")


if __name__ == "__main__":
    main()
