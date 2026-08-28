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

from lib.logging_util import get_logger
logger = get_logger("volatility-engine")
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
ARBITRAGE_CHECK_INTERVAL_SECONDS = 30
ARBITRAGE_AUDIT_POINTS = 2000

_running = True


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


def inference_loop(redis, model_manager: ModelManager) -> None:
    while _running:
        now = datetime.now()
        if not is_market_hours(now):
            time.sleep(30)
            continue

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
            except Exception:
                logger.error("[vol-engine] Error evaluating %s", symbol, exc_info=True)

        time.sleep(INFERENCE_INTERVAL_SECONDS)


def arbitrage_monitor(redis, model_manager: ModelManager) -> None:
    """Independent, periodic no-arbitrage check on each loaded model's own
    surface -- catches model degradation live (training converged badly but
    still slipped past the offline gate), distinct from that offline gate."""
    while _running:
        time.sleep(ARBITRAGE_CHECK_INTERVAL_SECONDS)
        for symbol in model_manager.symbols:
            model = model_manager.get_model(symbol)
            if model is None:
                continue
            try:
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
            except Exception:
                logger.error("[vol-engine] Arbitrage check failed for %s", symbol, exc_info=True)


def update_heartbeat(redis) -> None:
    redis.hset("service:registry:volatility-engine", mapping={
        "name": "volatility-engine",
        "pid": str(os.getpid()),
        "status": "healthy",
        "last_heartbeat": str(time.time()),
        "version": BUILD_LABEL,
        "commit": GIT_COMMIT,
        "dirty": str(GIT_DIRTY),
    })
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
        update_heartbeat(redis)
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
