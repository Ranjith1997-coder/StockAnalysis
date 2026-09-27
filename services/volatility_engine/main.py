"""
Volatility Engine — Service Entry Point

Always-running microservice (same pattern as services/paper_trading/main.py).
Loads the accept-gated PINN models produced by
tools/pinn_volatility/training/run_training.py, compares them against live
option prices every 3s during market hours, and emits mispricing signals for
paper-trading to consume. See docs/pinn-volatility-engine.md section 8.

Usage:
    python -m services.volatility_engine.main
"""
from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, time as dtime

import common.constants as constant
from lib.logging_util import get_logger
logger = get_logger("volatility-engine")
from lib.notification.Notification import TELEGRAM_NOTIFICATIONS
from services.common.redis_proxy import RedisProxy
from services.common.version import BUILD_LABEL, GIT_COMMIT, GIT_DIRTY
from services.volatility_engine.comparator import (
    build_strikes_data, check_signal_thresholds, compute_tau,
    evaluate_model, nearest_expiry_and_forward,
)
from services.volatility_engine.model_manager import ModelManager
from services.volatility_engine.signal_emitter import emit_signal, write_fair_iv_to_redis
from tools.pinn_volatility.losses.arbitrage import durrleman_density
from tools.pinn_volatility.model.pinn import K_RANGE, RawInputModel, TAU_RANGE
from tools.pinn_volatility.data.collocation import sample_collocation

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
INFERENCE_INTERVAL_SECONDS = 3
OUTSIDE_HOURS_SLEEP_SECONDS = 30
ARBITRAGE_CHECK_INTERVAL_SECONDS = 30
ARBITRAGE_AUDIT_POINTS = 2000
ALERT_COOLDOWN_SECONDS = 3600  # at most one Telegram alert per key per hour

_running = True

# Last-alert timestamps, keyed by failure site. Written from both the
# inference and arbitrage threads; worst case under a race is one duplicate
# alert, so no lock.
_last_alert_at: dict[str, float] = {}


def _sleep(seconds: float) -> None:
    """Indirection so tests can drive the loops deterministically."""
    time.sleep(seconds)


def _send_alert(message: str) -> None:
    TELEGRAM_NOTIFICATIONS.is_intraday = True
    TELEGRAM_NOTIFICATIONS.send_notification(message, parse_mode="HTML")


def _alert_throttled(key: str, message: str, now: float | None = None) -> bool:
    """Send a Telegram alert at most once per ALERT_COOLDOWN_SECONDS per key.

    Repeated identical failures (a broken data contract, a dead feed) would
    otherwise either spam Telegram or -- given the loops log-and-swallow --
    go unnoticed entirely. Returns True if the alert was sent.
    """
    now = time.time() if now is None else now
    if now - _last_alert_at.get(key, 0.0) < ALERT_COOLDOWN_SECONDS:
        return False
    _last_alert_at[key] = now
    _send_alert(message)
    return True


def signal_handler(signum, frame):
    global _running
    logger.info("[vol-engine] Received signal %s, shutting down...", signum)
    _running = False


def is_market_hours(now: datetime) -> bool:
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def evaluate_symbol(symbol: str, model, redis, now: datetime) -> tuple[list, float | None]:
    """Evaluate all live strikes for one symbol. Returns (signals, tau) --
    tau is None (and signals empty) if required live data isn't available
    yet (pre-market, feed gap, no nearest expiry)."""
    tick_raw = redis.hgetall(f"data:tick:{symbol}")
    spot = float(tick_raw.get("last_price") or 0)
    if spot <= 0:
        return [], None

    sensibull_raw = redis.hget(f"data:sensibull:{symbol}", "current_json")
    if not sensibull_raw:
        return [], None
    parsed = nearest_expiry_and_forward(sensibull_raw)
    if parsed is None:
        return [], None
    expiry, forward = parsed

    tau = compute_tau(expiry, now.date())
    if tau <= 0:
        return [], None

    options_live = redis.hgetall(f"data:options_live:{symbol}")
    if not options_live:
        return [], None

    strikes = build_strikes_data(options_live, spot=spot, forward=forward, tau=tau, now=now)
    if not strikes:
        return [], tau

    results = evaluate_model(model, strikes, tau)
    write_fair_iv_to_redis(redis, symbol, results, tau)
    signals = check_signal_thresholds(symbol, results, tau, expiry, now)
    return signals, tau


def _run_inference_iteration(redis, model_manager: ModelManager, now: datetime) -> None:
    model_manager.hot_reload_check()

    for symbol in model_manager.symbols:
        model = model_manager.get_model(symbol)
        if model is None:
            continue
        try:
            signals, _tau = evaluate_symbol(symbol, model, redis, now)
            for sig in signals:
                emit_signal(redis, sig)
                logger.info("[vol-engine] %s %s z=%.2f dir=%s",
                            symbol, sig.signal_type, sig.z_score, sig.direction)
        except Exception as e:
            logger.error("[vol-engine] Error evaluating %s", symbol, e, exc_info=True)
            _alert_throttled(
                f"eval:{symbol}",
                f"⚠️ <b>Vol-engine</b>: evaluation failing for {symbol} "
                f"({type(e).__name__}: {e}) -- see journalctl -u "
                f"stockanalysis-volatility-engine",
            )


def inference_loop(redis, model_manager: ModelManager) -> None:
    while _running:
        now = datetime.now()
        if not is_market_hours(now):
            _sleep(OUTSIDE_HOURS_SLEEP_SECONDS)
            continue

        # Last-resort guard: an exception here (e.g. the _latest.pt symlink
        # repointed between exists() and readlink() in hot_reload_check) must
        # not kill this thread -- the heartbeat loop would keep reporting
        # "healthy" while no signal was ever produced again.
        try:
            _run_inference_iteration(redis, model_manager, now)
        except Exception as e:
            logger.error("[vol-engine] Inference iteration failed: %s", e, exc_info=True)
            _alert_throttled(
                "inference-iteration",
                f"⚠️ <b>Vol-engine</b>: inference iteration failed "
                f"({type(e).__name__}: {e}) -- see journalctl -u "
                f"stockanalysis-volatility-engine",
            )

        _sleep(INFERENCE_INTERVAL_SECONDS)


def audit_model_surface(redis, symbol: str, model) -> float:
    """One Durrleman-density audit of a loaded model's own surface.

    Catches model degradation live (training converged badly but still slipped
    past the offline gate), distinct from that offline gate. Raises on
    failure -- the caller guards.
    """
    points = sample_collocation(ARBITRAGE_AUDIT_POINTS, k_range=K_RANGE, tau_range=TAU_RANGE)
    wrapped = RawInputModel(model)
    g = durrleman_density(wrapped, points).detach()
    violation_rate = (g < 0).float().mean().item()
    if violation_rate > 0:
        redis.hset(f"pinn:arbitrage_status:{symbol}", mapping={
            "violation_rate": str(violation_rate),
            "min_g": str(g.min().item()),
            "checked_at": str(time.time()),
        })
        if violation_rate >= 0.05:
            logger.warning("[vol-engine] %s live model shows %.2f%% butterfly-arbitrage "
                            "violation on a fresh audit sample -- model may have degraded "
                            "since the offline gate accepted it", symbol, violation_rate * 100)
    return violation_rate


def arbitrage_monitor(redis, model_manager: ModelManager) -> None:
    """Independent, periodic no-arbitrage check on each loaded model's surface.

    Gated to market hours: it audits the surface that is only served during
    the session, so running it all night is pure waste on a 2-core box.
    """
    while _running:
        _sleep(ARBITRAGE_CHECK_INTERVAL_SECONDS)
        if not is_market_hours(datetime.now()):
            continue
        for symbol in model_manager.symbols:
            model = model_manager.get_model(symbol)
            if model is None:
                continue
            try:
                audit_model_surface(redis, symbol, model)
            except Exception as e:
                logger.error("[vol-engine] Arbitrage check failed for %s", symbol, e, exc_info=True)
                _alert_throttled(
                    f"arbitrage:{symbol}",
                    f"⚠️ <b>Vol-engine</b>: arbitrage audit failing for {symbol} "
                    f"({type(e).__name__}: {e})",
                )


def update_heartbeat(redis, model_manager: ModelManager | None = None) -> None:
    mapping = {
        "name": "volatility-engine",
        "pid": str(os.getpid()),
        "status": "healthy",
        "last_heartbeat": str(time.time()),
        "version": BUILD_LABEL,
        "commit": GIT_COMMIT,
        "dirty": str(GIT_DIRTY),
    }
    # `models` makes a "healthy but serving nothing" state visible at a glance
    # (no accepted model yet is a real, expected state -- see design doc 0.6.1).
    if model_manager is not None:
        loaded = sorted(model_manager.models)
        mapping["models"] = ",".join(
            f"{s}@{model_manager.train_dates.get(s, 'unknown')}" for s in loaded
        ) or "none"
    redis.hset("service:registry:volatility-engine", mapping=mapping)
    redis.expire("service:registry:volatility-engine", 120)


def main():
    global _running
    import threading

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = RedisProxy(redis_url)
    try:
        redis.get("ping")
        logger.info("[vol-engine] Connected to Redis at %s", redis_url)
        logger.info("[vol-engine] v%s starting", BUILD_LABEL)
    except Exception as e:
        logger.error("[vol-engine] Cannot connect to Redis at %s: %s", redis_url, e, exc_info=True)
        sys.exit(1)

    from services.common.crash_handler import install_crash_handler
    install_crash_handler("volatility-engine")

    TELEGRAM_NOTIFICATIONS.is_production = os.getenv(constant.ENV_PRODUCTION, "0") == "1"

    model_manager = ModelManager()
    if not model_manager.models:
        logger.warning("[vol-engine] No models loaded at startup -- "
                        "run tools.pinn_volatility.training.run_training first")

    threads = [
        threading.Thread(target=inference_loop, args=(redis, model_manager), name="inference-loop", daemon=True),
        threading.Thread(target=arbitrage_monitor, args=(redis, model_manager), name="arbitrage-monitor", daemon=True),
    ]
    for t in threads:
        t.start()

    logger.info("[vol-engine] Started, %d worker threads running", len(threads))

    from lib.logging_util import refresh_level_from_redis

    while _running:
        update_heartbeat(redis, model_manager)
        refresh_level_from_redis(redis, "volatility-engine")
        time.sleep(30)

    logger.info("[vol-engine] Shutting down...")
    for t in threads:
        t.join(timeout=5)
    redis.hset("service:registry:volatility-engine", mapping={
        "status": "shutdown", "last_heartbeat": str(time.time()),
    })
    redis.close()


if __name__ == "__main__":
    main()
