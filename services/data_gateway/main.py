"""
Data Gateway — StockAnalysis data ingestion service.

Fetches data from all external sources (yfinance, Sensibull) and publishes
to Redis. Other services (analysis-engine, orchestrator, bot) read from Redis
instead of making direct HTTP calls.

Phase 1A:
    - yfinance historical + intraday price data → Redis HSET
    - Sensibull insights + OI chain → Redis HSET

Phase 1B:
    - Zerodha WebSocket + REST → Redis Pub/Sub + HSET
    - Sensibull WebSocket feed → Redis Pub/Sub

Usage:
    python -m services.data_gateway.main [--dev-intraday] [--dev-positional]
"""

from __future__ import annotations

import datetime
import os
import sys
import time
import json
import signal
import argparse
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from dotenv import load_dotenv
load_dotenv()

from services.common.logging import get_logger
logger = get_logger("data-gateway")
from datetime import time as _time
from common.helperFunctions import get_stock_objects_from_json, isNowInTimePeriod
from common.market_calendar import is_trading_day
from common import constants as constant
from services.common.redis_proxy import RedisProxy
from services.data_gateway.yfinance_fetcher import fetch_initial_daily_data, fetch_cycle_data
from services.data_gateway.sensibull_fetcher import (
    fetch_and_publish_cycle_parallel,
    INDEX_ANALYSIS_EXCLUDE,
)
from services.data_gateway.zerodha_fetcher import (
    fetch_instruments,
    ZerodhaFuturesManager,
)

CYCLE_STREAM = "data:cycle_stream"
CYCLE_CHANNEL = "data:cycle_ready"


# Configuration
CYCLE_SLEEP = 310         # seconds between intraday cycles (same as monolith)
DEV_LOOP_WAIT_TIME = int(os.environ.get("DEV_LOOP_WAIT_TIME", "30"))
MARKET_OPEN = _time(9, 15)
MARKET_CLOSE = _time(15, 30)
IDLE_SLEEP = 300          # seconds between idle heartbeats (5 min)
POSITIONAL_FETCH_START = _time(19, 0)
POSITIONAL_FETCH_END = _time(19, 30)

# Global state
_running = True
_positional_done_date = None  # date string when positional fetch completed (prevents redundant cycles)


def signal_handler(signum, frame):
    global _running
    logger.info(f"[data-gateway] Received signal {signum}, shutting down...")
    _running = False


def get_symbol_lists() -> tuple[list[str], list[str], list[str], list[str]]:
    """
    Load stock objects from JSON and return symbol lists.

    Returns:
        tuple of (stock_symbols, index_symbols, commodity_symbols, global_indices_symbols)
    """
    stock_list, index_list, commodity_list, global_indices_list = get_stock_objects_from_json()

    stock_symbols = [s["tradingsymbol"] for s in stock_list if s["tradingsymbol"] not in INDEX_ANALYSIS_EXCLUDE]
    index_symbols = [i["tradingsymbol"] for i in index_list if i["tradingsymbol"] not in INDEX_ANALYSIS_EXCLUDE]
    commodity_symbols = [c["tradingsymbol"] for c in commodity_list]
    global_indices_symbols = [g["tradingsymbol"] for g in global_indices_list]

    logger.info(f"[data-gateway] Loaded {len(stock_symbols)} stocks, {len(index_symbols)} indices, "
                f"{len(commodity_symbols)} commodities, {len(global_indices_symbols)} global indices")
    return stock_symbols, index_symbols, commodity_symbols, global_indices_symbols


def get_yfinance_symbols(stock_symbols: list[str], index_symbols: list[str],
                          commodity_symbols: list[str], global_indices_symbols: list[str],
                          stock_list: list, index_list: list,
                          commodity_list: list = None, global_indices_list: list = None) -> tuple[list[str], list[str], list[str], list[str]]:
    """Convert tradingsymbols to yfinance symbols."""
    if commodity_list is None:
        commodity_list = []
    if global_indices_list is None:
        global_indices_list = []
    stock_yf_map = {s["tradingsymbol"]: s["tradingsymbol"] + ".NS" for s in stock_list}
    index_yf_map = {i["tradingsymbol"]: i["yfinancetradingsymbol"] for i in index_list}
    commodity_yf_map = {c["tradingsymbol"]: c.get("yfinancetradingsymbol", c["tradingsymbol"]) for c in commodity_list}
    global_yf_map = {g["tradingsymbol"]: g.get("yfinancetradingsymbol", g["tradingsymbol"]) for g in global_indices_list}

    yf_stocks = [stock_yf_map.get(s) for s in stock_symbols if stock_yf_map.get(s)]
    yf_indices = [index_yf_map.get(i) for i in index_symbols if index_yf_map.get(i)]
    yf_commodities = [commodity_yf_map.get(c) for c in commodity_symbols if commodity_yf_map.get(c)]
    yf_globals = [global_yf_map.get(g) for g in global_indices_symbols if global_yf_map.get(g)]

    return yf_stocks, yf_indices, yf_commodities, yf_globals


# ═══════════════════════════════════════════════════════════════════════════
# Scheduling — self-determines whether to fetch data based on market calendar
# ═══════════════════════════════════════════════════════════════════════════

FetchDecision = tuple[str, str]
# Returns (action, mode) where:
#   action: "fetch" | "skip" | "sleep_until_open" | "idle"
#   mode:   "intraday" | "positional" | ""


def _determine_fetch_action(is_prod: bool) -> FetchDecision:
    """
    Determine whether the data-gateway should fetch data this cycle.

    Returns:
        (action, mode):
            "fetch", "intraday"        → fetch 5m bars, repeat every 5 min
            "fetch", "positional"      → fetch 2y daily once (19:00-19:30 window)
            "sleep_until_open", ""     → sleep until market opens at 9:15
            "idle", ""                 → idle (weekend, holiday, or past 19:30)
    """
    if not is_prod:
        return "fetch", "intraday"

    if not is_trading_day():
        return "idle", ""

    now = datetime.datetime.now().time()

    if now < MARKET_OPEN:
        return "sleep_until_open", ""

    if isNowInTimePeriod(MARKET_OPEN, MARKET_CLOSE, now):
        return "fetch", "intraday"

    if POSITIONAL_FETCH_START <= now <= POSITIONAL_FETCH_END:
        return "fetch", "positional"

    return "idle", ""


def _update_beat(redis, cycle_count: int, status: str, **extra):
    """Update the data-gateway heartbeat in Redis."""
    mapping = {
        "name": "data-gateway",
        "pid": str(os.getpid()),
        "status": status,
        "last_heartbeat": str(time.time()),
        "cycle_count": str(cycle_count),
    }
    mapping.update(extra)
    redis.hset("service:registry:data-gateway", mapping=mapping)


def _sleep_seconds(seconds: int):
    """Sleep in 1s chunks so SIGTERM is responsive."""
    for _ in range(seconds):
        if not _running:
            break
        time.sleep(1)


def main():
    global _running, _positional_done_date

    parser = argparse.ArgumentParser(description="StockAnalysis Data Gateway")
    parser.add_argument("--dev-intraday", action="store_true", help="Dev intraday mode")
    parser.add_argument("--dev-positional", action="store_true", help="Dev positional mode")
    args = parser.parse_args()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Determine mode
    is_prod = os.getenv("PRODUCTION", "0") == "1"
    is_dev_intraday = args.dev_intraday or os.getenv("DEV_INTRADAY", "0") == "1"
    is_dev_positional = args.dev_positional or os.getenv("DEV_POSITIONAL", "0") == "1"
    is_intraday = is_dev_intraday or (is_prod and not is_dev_positional)

    logger.info(f"[data-gateway] Starting in {'intraday' if is_intraday else 'positional'} mode")
    logger.info(f"[data-gateway] PRODUCTION={is_prod}, DEV_INTRADAY={is_dev_intraday}, DEV_POSITIONAL={is_dev_positional}")

    # Connect to Redis
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = RedisProxy(redis_url)
    try:
        redis.get("ping")  # Test connection
        logger.info(f"[data-gateway] Connected to Redis at {redis_url}")
    except Exception as e:
        logger.error(f"[data-gateway] Cannot connect to Redis at {redis_url}: {e}")
        sys.exit(1)

    # Write initial heartbeat
    redis.hset("service:registry:data-gateway", mapping={
        "name": "data-gateway",
        "pid": str(os.getpid()),
        "status": "starting",
        "started_at": str(time.time()),
    })

    # Load symbol lists
    stock_symbols, index_symbols, commodity_symbols, global_indices_symbols = get_symbol_lists()
    stock_list, index_list, commodity_list, global_indices_list = get_stock_objects_from_json()
    yf_stocks, yf_indices, yf_commodities, yf_globals = get_yfinance_symbols(
        stock_symbols, index_symbols, commodity_symbols, global_indices_symbols,
        stock_list, index_list, commodity_list, global_indices_list,
    )

    logger.info(f"[data-gateway] yfinance symbols: {len(yf_stocks)} stocks, {len(yf_indices)} indices, "
                f"{len(yf_commodities)} commodities, {len(yf_globals)} global indices")

    # Initialise Zerodha futures manager (fetches instruments via public API)
    all_futures_mdata = fetch_instruments()
    zerodha_mgr = ZerodhaFuturesManager(redis, all_futures_mdata)
    has_enc = "available" if zerodha_mgr.has_enctoken() else "pending (monolith TOTP at 09:00)"
    logger.info(f"[data-gateway] ZerodhaFuturesManager ready — enctoken={has_enc}")

    # Build yfinance symbol → tradingsymbol map for cycle data key resolution
    yf_to_key_map = {}
    for s in stock_list:
        yf_sym = s["tradingsymbol"] + ".NS"
        yf_to_key_map[yf_sym] = s["tradingsymbol"]
    for idx in index_list:
        yf_sym = idx["yfinancetradingsymbol"]
        yf_to_key_map[yf_sym] = idx["tradingsymbol"]
    for c in commodity_list:
        yf_sym = c.get("yfinancetradingsymbol", c["tradingsymbol"])
        yf_to_key_map[yf_sym] = c["tradingsymbol"]
    for g in global_indices_list:
        yf_sym = g.get("yfinancetradingsymbol", g["tradingsymbol"])
        yf_to_key_map[yf_sym] = g["tradingsymbol"]

    # ── Initial data load ──────────────────────────────────────────────────
    logger.info("[data-gateway] Starting initial data load...")
    try:
        fetch_initial_daily_data(redis, is_intraday)
        logger.info("[data-gateway] Initial daily data loaded")
    except Exception as e:
        logger.error(f"[data-gateway] Initial data load failed: {e}")
        logger.error(traceback.format_exc())

    # Initial Sensibull fetch (parallel)
    try:
        logger.info("[data-gateway] Starting initial Sensibull data fetch (10 workers)...")
        sens_ok, sens_fail = fetch_and_publish_cycle_parallel(
            redis, stock_symbols, index_symbols,
            mode="intraday" if is_intraday else "positional",
        )
        logger.info(f"[data-gateway] Initial Sensibull data loaded: {sens_ok} ok, {sens_fail} failed")
    except Exception as e:
        logger.error(f"[data-gateway] Initial Sensibull fetch failed: {e}")
        logger.error(traceback.format_exc())

    # Publish initial cycle signal for monolith's catch_up_on_startup()
    initial_ts = datetime.datetime.now().isoformat()
    redis.xadd(CYCLE_STREAM, {
        "cycle": "0",
        "timestamp": initial_ts,
        "mode": "initial_load",
        "price_symbols": str(len(stock_symbols) + len(index_symbols) + len(commodity_symbols) + len(global_indices_symbols)),
        "sensibull_symbols": str(sens_ok),
        "failures": str(sens_fail),
        "elapsed": "startup",
    }, maxlen=100)
    redis.publish(CYCLE_CHANNEL, f"cycle=0,ts={initial_ts}")

    # Mark healthy
    _update_beat(redis, 0, "healthy")
    logger.info("[data-gateway] Initial load complete. Entering main loop.")

    # ── Main loop ──────────────────────────────────────────────────────────
    cycle_count = 0

    while _running:
        cycle_count += 1

        # ── Determine whether to fetch data this cycle ──────────────────
        if is_prod:
            action, mode = _determine_fetch_action(is_prod)
        else:
            action = "fetch"
            mode = "intraday" if is_intraday else "positional"

        if action == "idle":
            _update_beat(redis, cycle_count, "idle", status_detail="idle")
            logger.debug(f"[data-gateway] Cycle {cycle_count}: idle")
            _sleep_seconds(IDLE_SLEEP)
            continue

        if action == "sleep_until_open":
            now = datetime.datetime.now()
            market_open_dt = now.replace(hour=9, minute=15, second=0, microsecond=0)
            sleep_sec = int((market_open_dt - now).total_seconds())
            if sleep_sec > 0:
                logger.info(f"[data-gateway] Cycle {cycle_count}: sleeping {sleep_sec}s until market open")
                _sleep_seconds(min(sleep_sec, 300))
                continue

        if mode == "positional" and _positional_done_date == str(datetime.date.today()):
            logger.debug(f"[data-gateway] Cycle {cycle_count}: positional already done today — idle")
            _update_beat(redis, cycle_count, "idle", status_detail="positional_done")
            _sleep_seconds(IDLE_SLEEP)
            continue

        # ── Fetch data ──────────────────────────────────────────────────
        cycle_start = time.time()
        logger.info(f"[data-gateway] Cycle {cycle_count}: fetching {mode} data...")

        price_ok = True
        try:
            fetch_cycle_data(redis, yf_stocks, yf_indices, yf_commodities, yf_globals,
                             yf_to_key_map=yf_to_key_map, mode=mode)
        except Exception as e:
            logger.error(f"[data-gateway] yfinance cycle fetch failed: {e}")
            price_ok = False

        sensibull_ok = 0
        sensibull_fail = 0
        try:
            sensibull_ok, sensibull_fail = fetch_and_publish_cycle_parallel(
                redis, stock_symbols, index_symbols,
                mode=mode,
            )
        except Exception as e:
            logger.error(f"[data-gateway] Sensibull cycle fetch failed: {e}")
            sensibull_fail = len(stock_symbols) + len(index_symbols)

        # ── Zerodha futures data ──────────────────────────────────────────────
        futures_ok = 0
        futures_fail = 0
        if zerodha_mgr.has_enctoken():
            try:
                futures_ok, futures_fail = zerodha_mgr.fetch_and_publish(
                    redis,
                    stock_symbols + index_symbols,
                    mode=mode,
                )
            except Exception as e:
                logger.error(f"[data-gateway] Zerodha futures fetch failed: {e}")
                futures_fail = len(stock_symbols) + len(index_symbols)
        else:
            logger.debug("[data-gateway] No enctoken yet — skipping futures fetch")

        # ── Publish cycle signal ────────────────────────────────────────────
        cycle_elapsed = time.time() - cycle_start
        price_count = len(stock_symbols) + len(index_symbols) + len(commodity_symbols) + len(global_indices_symbols)

        cycle_fields = {
            "cycle": str(cycle_count),
            "timestamp": str(datetime.datetime.now()),
            "mode": mode,
            "price_symbols": str(price_count if price_ok else 0),
            "sensibull_symbols": str(sensibull_ok),
            "failures": str(sensibull_fail),
            "futures_ok": str(futures_ok),
            "futures_fail": str(futures_fail),
            "elapsed": str(round(cycle_elapsed, 1)),
        }
        redis.xadd(CYCLE_STREAM, cycle_fields, maxlen=100)
        redis.publish(CYCLE_CHANNEL,
                      f"cycle={cycle_count},ts={datetime.datetime.now().isoformat()}")

        _update_beat(redis, cycle_count, "healthy",
                     stats_json=json.dumps({
                         "cycle_count": cycle_count,
                         "cycle_elapsed": round(cycle_elapsed, 1),
                         "mode": mode,
                         "stocks": len(stock_symbols),
                         "indices": len(index_symbols),
                     }, default=str))

        logger.info(f"[data-gateway] Cycle {cycle_count} complete in {cycle_elapsed:.1f}s")

        # ── Sleep until next cycle ──────────────────────────────────────
        if mode == "positional":
            _positional_done_date = str(datetime.date.today())
            logger.info("[data-gateway] Positional fetch complete — idle until tomorrow")

        if not _running:
            break

        if is_dev_intraday and not is_prod:
            sleep_time = DEV_LOOP_WAIT_TIME
        elif mode == "intraday":
            now_t = time.localtime()
            seconds_elapsed = now_t.tm_sec + (now_t.tm_min % 5) * 60
            sleep_time = CYCLE_SLEEP - seconds_elapsed
            if sleep_time <= 0:
                sleep_time = CYCLE_SLEEP
        else:
            sleep_time = IDLE_SLEEP

        logger.debug(f"[data-gateway] Sleeping {sleep_time}s until next cycle")
        _sleep_seconds(int(sleep_time))

    # Clean shutdown
    logger.info("[data-gateway] Shutting down...")
    redis.hset("service:registry:data-gateway", mapping={
        "name": "data-gateway",
        "pid": str(os.getpid()),
        "status": "shutdown",
    })
    redis.close()
    logger.info("[data-gateway] Shutdown complete")


if __name__ == "__main__":
    main()
