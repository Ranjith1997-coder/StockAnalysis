"""
PINN Trainer — Service Entry Point

Always-running microservice that self-schedules the nightly PINN training
run, once per trading day, after services/orchestrator's positional_analysis()
has finished (~20:30). Mirrors services/auth_service/main.py's fixed-time
self-scheduling pattern (09:00 + 18:50 refresh loop) rather than a systemd
.timer -- this project deliberately moved away from timer units in favor of
self-scheduling services (README: "No timers. All services self-schedule.").
Design doc section 7.5 assumed a `stockanalysis-pinn-training.timer`; that's
stale relative to this convention -- see docs/pinn-volatility-engine.md
section 0 for the deviation note.

Usage:
    python -m services.pinn_trainer.main
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from datetime import date, datetime, time as dtime, timedelta

import common.constants as constant
from lib.logging_util import get_logger
logger = get_logger("pinn-trainer")
from lib.notification.Notification import TELEGRAM_NOTIFICATIONS
from services.common.redis_proxy import RedisProxy
from services.common.version import BUILD_LABEL, GIT_COMMIT, GIT_DIRTY
from tools.pinn_volatility.config import PINNConfig
from tools.pinn_volatility.training.run_training import TrainingRunResult, train_one_symbol

TRAIN_TIME = dtime(21, 0)     # after positional_analysis() (~20:30 done, see orchestrator/main.py)
CUTOFF_TIME = dtime(23, 0)    # give up (1 alert) + wait for tomorrow if still not done by here
RETRY_INTERVAL_SECONDS = 900  # 15 min between Bhavcopy-availability retries

_running = True


def signal_handler(signum, frame):
    global _running
    logger.info("[pinn-trainer] Received signal %s, shutting down...", signum)
    _running = False


def _already_trained_today(redis, symbol: str) -> bool:
    return redis.hget(f"pinn:model:{symbol}", "train_date") == date.today().isoformat()


def _all_trained_today(redis, config: PINNConfig) -> bool:
    return all(_already_trained_today(redis, s) for s in config.symbols)


def _send_alert(message: str) -> None:
    # Training happens post-market -- use the positional/EOD channel
    # convention (services/orchestrator/main.py sets this the same way
    # before its own EOD alerts).
    TELEGRAM_NOTIFICATIONS.is_intraday = False
    TELEGRAM_NOTIFICATIONS.send_notification(message, parse_mode="HTML")


def _format_result_alert(symbol: str, result: TrainingRunResult) -> str | None:
    """None means "don't alert" -- an early failure (no Bhavcopy data yet)
    is retry-eligible and not itself alert-worthy on every 15-min retry;
    only the end-of-day give-up in _run_schedule() alerts for that case."""
    if result.holdout_date is None:
        return None
    if result.accepted:
        return (f"✅ <b>PINN trained: {symbol}</b> wings={result.wings_mae * 100:.2f}% "
                f"but_viol={result.butterfly_violation_rate * 100:.2f}%")
    return (f"❌ <b>PINN training FAILED for {symbol}</b>: "
            f"{'; '.join(result.reasons)}. Previous model retained.")


def run_training_cycle(redis, config: PINNConfig) -> bool:
    """Attempt training for every symbol not already trained today.

    Returns True iff every symbol has now reached the acceptance gate
    (accepted or genuinely rejected) -- i.e. nothing left to retry today.
    """
    pending = [s for s in config.symbols if not _already_trained_today(redis, s)]
    for symbol in pending:
        logger.info("[pinn-trainer] Training %s (end_date=%s)", symbol, date.today())
        result = train_one_symbol(symbol, config, redis=redis, end_date=date.today())
        alert = _format_result_alert(symbol, result)
        if alert:
            _send_alert(alert)
        if result.holdout_date is None:
            logger.warning("[pinn-trainer] %s: not trainable yet (%s)",
                            symbol, "; ".join(result.reasons))
    return _all_trained_today(redis, config)


def _wait_until(hour: int, minute: int) -> None:
    """Sleep until the given time today. Returns immediately if already past."""
    while _running:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        sleep_sec = (target - now).total_seconds()
        if sleep_sec <= 0:
            return
        time.sleep(min(sleep_sec, 60))


def _sleep_until_midnight() -> None:
    target = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while _running:
        sleep_sec = (target - datetime.now()).total_seconds()
        if sleep_sec <= 0:
            return
        time.sleep(min(sleep_sec, 300))


def _sleep_responsive(seconds: float) -> None:
    end = time.time() + seconds
    while _running and time.time() < end:
        time.sleep(min(end - time.time(), 30))


def _run_schedule(redis, config: PINNConfig) -> None:
    """Self-scheduling loop: train once/day at TRAIN_TIME, retrying every
    RETRY_INTERVAL_SECONDS until either every symbol reaches the gate or
    CUTOFF_TIME passes (one give-up alert, then idle until midnight)."""
    logger.info("[pinn-trainer] v%s starting -- entering scheduling loop", BUILD_LABEL)

    schedule_date = datetime.now().date()
    started_today = False
    gave_up_today = False

    while _running:
        now = datetime.now()
        if now.date() != schedule_date:
            schedule_date = now.date()
            started_today = False
            gave_up_today = False
            logger.info("[pinn-trainer] New day %s -- flags reset", schedule_date)

        if _all_trained_today(redis, config):
            _sleep_until_midnight()
            continue

        if now.time() < TRAIN_TIME:
            logger.info("[pinn-trainer] Waiting until %s for nightly training", TRAIN_TIME.strftime("%H:%M"))
            _wait_until(TRAIN_TIME.hour, TRAIN_TIME.minute)
            continue

        if gave_up_today:
            _sleep_until_midnight()
            continue

        if not started_today:
            _send_alert(f"\U0001F9EE <b>PINN training started</b> for {', '.join(config.symbols)}")
            started_today = True

        all_done = run_training_cycle(redis, config)
        if all_done:
            continue

        if datetime.now().time() >= CUTOFF_TIME:
            _send_alert(f"⚠️ <b>PINN training</b>: Bhavcopy still unavailable as of "
                        f"{CUTOFF_TIME.strftime('%H:%M')} -- giving up for today.")
            gave_up_today = True
            continue

        logger.info("[pinn-trainer] Not all symbols trainable yet -- retrying in %ds", RETRY_INTERVAL_SECONDS)
        _sleep_responsive(RETRY_INTERVAL_SECONDS)


def update_heartbeat(redis) -> None:
    redis.hset("service:registry:pinn-trainer", mapping={
        "name": "pinn-trainer",
        "pid": str(os.getpid()),
        "status": "healthy",
        "last_heartbeat": str(time.time()),
        "version": BUILD_LABEL,
        "commit": GIT_COMMIT,
        "dirty": str(GIT_DIRTY),
    })
    redis.expire("service:registry:pinn-trainer", 120)


def main():
    global _running
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = RedisProxy(redis_url)
    try:
        redis.get("ping")
        logger.info("[pinn-trainer] Connected to Redis at %s", redis_url)
    except Exception as e:
        logger.error("[pinn-trainer] Cannot connect to Redis at %s: %s", redis_url, e, exc_info=True)
        sys.exit(1)

    from services.common.crash_handler import install_crash_handler
    install_crash_handler("pinn-trainer")

    TELEGRAM_NOTIFICATIONS.is_production = os.getenv(constant.ENV_PRODUCTION, "0") == "1"

    config = PINNConfig()
    schedule_thread = threading.Thread(
        target=_run_schedule, args=(redis, config), name="pinn-trainer-schedule", daemon=True,
    )
    schedule_thread.start()

    from lib.logging_util import refresh_level_from_redis

    while _running:
        update_heartbeat(redis)
        refresh_level_from_redis(redis, "pinn-trainer")
        time.sleep(30)

    logger.info("[pinn-trainer] Shutting down...")
    schedule_thread.join(timeout=5)
    redis.hset("service:registry:pinn-trainer", mapping={
        "status": "shutdown", "last_heartbeat": str(time.time()),
    })
    redis.close()


if __name__ == "__main__":
    main()
