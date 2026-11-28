#!/usr/bin/env python3
"""Refresh the Sensibull auth pair through a real browser login.

Kite's app-consent page is a JS app whose API is not callable outside the page,
so the only reliable automation is to drive it: Zerodha login -> TOTP ->
"Authorize" -> read the Sensibull cookies.

Usage:
    python scripts/sensibull_login.py --check     # login, verify API, no Redis write
    python scripts/sensibull_login.py             # login and publish to Redis
    python scripts/sensibull_login.py --headed    # debug locally

Exit codes: 0 ok, 2 login/cookie failure, 3 API verification failure.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))     # runnable as a plain script
load_dotenv(REPO_ROOT / ".env", override=True)

LOGIN_URL = (
    "https://oxide.sensibull.com/v1/pluto/auth/web/session/b/u/kite/platform/login"
)
API_PROBE = (
    "https://oxide.sensibull.com/v1/compute/cache/stock_info?tradingsymbol=NIFTY"
)
SENSIBULL_HASH = "auth:sensibull"
SENSIBULL_CHANNEL = "auth:sensibull_refreshed"
SHOT = Path("logs/sensibull_login_failure.png")

USER_SELECTORS = ("input#userid", "input[name='user_id']", "input[type='text']")
PASSWORD_SELECTORS = ("input#password", "input[name='password']", "input[type='password']")
# Kite reuses #userid for the TOTP step, switching it to type=number with a
# masked placeholder ("••••••") and a "Continue" submit button.
OTP_SELECTORS = (
    "input#userid[type='number']", "input[type='number']",
    "input[placeholder='••••••']",
)
SUBMIT_SELECTORS = ("button[type='submit']", "button:has-text('Login')", "button:has-text('Continue')")


def _first(page, selectors: tuple[str, ...]):
    for selector in selectors:
        element = page.query_selector(selector)
        if element and element.is_visible():
            return selector
    return None


def _screenshot(page, reason: str) -> None:
    try:
        SHOT.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOT), full_page=True)
        print(f"[sensibull-login] screenshot: {SHOT} ({reason})", file=sys.stderr)
        print(f"[sensibull-login] url at failure: {page.url}", file=sys.stderr)
    except Exception:
        pass


def login(headless: bool = True, channel: str | None = None, timeout_ms: int = 60_000) -> dict:
    """Drive the browser and return the Sensibull cookies (access_token/client_info)."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    user = os.environ.get("ZERODHA_USER")
    password = os.environ.get("ZERODHA_PASS")
    secret = os.environ.get("ZERODHA_TOTP_SECRET")
    if not all((user, password, secret)):
        raise RuntimeError("ZERODHA_USER / ZERODHA_PASS / ZERODHA_TOTP_SECRET missing")

    import pyotp

    with sync_playwright() as playwright:
        launch = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
        if channel:
            launch["channel"] = channel
        try:
            browser = playwright.chromium.launch(**launch)
        except Exception:
            # Server may only have Playwright's bundled Chromium, or vice versa.
            if not channel:
                raise
            launch.pop("channel")
            browser = playwright.chromium.launch(**launch)
        context = browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            print("[sensibull-login] opening Sensibull login", file=sys.stderr)
            _open_with_retry(page, timeout_ms)

            user_sel = _wait_for_any(page, USER_SELECTORS, timeout_ms)
            page.fill(user_sel, user)
            page.fill(_first(page, PASSWORD_SELECTORS) or PASSWORD_SELECTORS[0], password)
            _submit(page)

            otp_sel = _wait_for_any(page, OTP_SELECTORS, timeout_ms)
            page.fill(otp_sel, pyotp.TOTP(secret).now())
            _submit(page)

            # Consent page: click Authorize when it appears (already-approved apps
            # skip it and redirect straight through).
            try:
                button = page.get_by_role("button", name="Authorize")
                button.wait_for(timeout=20_000)
                print("[sensibull-login] clicking Authorize", file=sys.stderr)
                try:
                    # The click navigates immediately; Playwright's stability
                    # check would otherwise time out on the detached element.
                    button.click(timeout=5_000, no_wait_after=True)
                except PlaywrightTimeout:
                    pass        # cookies below are the real success signal
            except PlaywrightTimeout:
                print("[sensibull-login] no consent button; continuing", file=sys.stderr)

            # Wait for the Sensibull session cookies rather than the URL: the
            # callback may already have navigated before we start waiting.
            deadline = time.time() + timeout_ms / 1000
            cookies: dict[str, str] = {}
            while time.time() < deadline:
                cookies = {
                    cookie["name"]: cookie["value"]
                    for cookie in context.cookies("https://oxide.sensibull.com")
                }
                if cookies.get("access_token") and cookies.get("client_info"):
                    break
                if "kite_login_error" in page.url:
                    _screenshot(page, "kite_login_error")
                    raise RuntimeError("Sensibull rejected the login (kite_login_error)")
                page.wait_for_timeout(500)

            missing = [name for name in ("access_token", "client_info") if not cookies.get(name)]
            if missing:
                _screenshot(page, f"missing cookies {missing}")
                raise RuntimeError(f"Sensibull cookies missing: {missing}")
            return cookies
        except Exception:
            _screenshot(page, "exception")
            raise
        finally:
            browser.close()


def _open_with_retry(page, timeout_ms: int, attempts: int = 4) -> None:
    """Kite sometimes serves 503s; retry with backoff before failing."""
    for attempt in range(1, attempts + 1):
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1500)
        title = (page.title() or "")[:40]
        body = page.content()[:600]
        if "Service Unavailable" in body or title.startswith("50"):
            print(f"[sensibull-login] upstream {title!r} (attempt {attempt})", file=sys.stderr)
            if attempt == attempts:
                raise RuntimeError("Kite login page unavailable (5xx)")
            page.wait_for_timeout(20_000 * attempt)
            continue
        return


def _wait_for_any(page, selectors: tuple[str, ...], timeout_ms: int) -> str:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        found = _first(page, selectors)
        if found:
            return found
        page.wait_for_timeout(500)
    raise RuntimeError(f"none of the selectors appeared: {selectors} (url={page.url})")


def _submit(page) -> None:
    """Submit the current step; navigation detaches the button, so don't wait."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    selector = _first(page, SUBMIT_SELECTORS)
    if selector:
        try:
            page.click(selector, timeout=5_000, no_wait_after=True)
        except PlaywrightTimeout:
            pass            # the step navigated; the next selector proves it
    else:
        page.keyboard.press("Enter")


def verify_api(cookies: dict) -> tuple[bool, str]:
    import requests

    response = requests.get(API_PROBE, cookies=cookies, timeout=20,
                            headers={"Origin": "https://web.sensibull.com"})
    ok = response.status_code == 200 and bool((response.json() or {}).get("success"))
    return ok, f"http={response.status_code}"


def publish(cookies: dict) -> str:
    from lib.sensibull_auth import client_info_expiry
    from services.common.redis_proxy import RedisProxy

    redis = RedisProxy(os.environ.get("REDIS_URL", "redis://localhost:6379"))
    redis.hset(SENSIBULL_HASH, mapping={
        "access_token": cookies["access_token"],
        "client_info": cookies["client_info"],
        "issued_at": str(time.time()),
        "last_reason": "browser_auto",
    })
    redis.publish(SENSIBULL_CHANNEL, f"issued_at={int(time.time())}")
    expiry = client_info_expiry(cookies["client_info"])
    return expiry.isoformat() if expiry else "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify only; do not write Redis")
    parser.add_argument("--headed", action="store_true", help="show the browser (debug)")
    parser.add_argument("--channel", default=os.getenv("SENSIBULL_BROWSER_CHANNEL", "chrome"),
                        help="Playwright browser channel (chrome, chromium, msedge)")
    args = parser.parse_args(argv)

    try:
        cookies = login(headless=not args.headed, channel=args.channel or None)
    except Exception as exc:
        print(f"[sensibull-login] FAILED: {exc}", file=sys.stderr)
        return 2

    ok, detail = verify_api(cookies)
    if not ok:
        print(f"[sensibull-login] API check failed: {detail}", file=sys.stderr)
        return 3

    if args.check:
        print(f"[sensibull-login] check ok ({detail}); cookies acquired")
        return 0

    expiry = publish(cookies)
    print(f"[sensibull-login] published to Redis; client_info expires {expiry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
