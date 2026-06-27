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
from common.helperFunctions import get_stock_objects_from_json
from services.common.redis_proxy import RedisProxy
from services.data_gateway.yfinance_fetcher import fetch_initial_daily_data, fetch_cycle_data
from services.data_gateway.sensibull_fetcher import fetch_and_publish_cycle, INDEX_ANALYSIS_EXCLUDE


# Configuration
CYCLE_SLEEP = 310  # seconds between intraday cycles (same as monolith)
DEV_LOOP_WAIT_TIME = int(os.environ.get("DEV_LOOP_WAIT_TIME", "30"))

# Global state
_running = True


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
                          stock_list: list, index_list: list) -> tuple[list[str], list[str], list[str], list[str]]:
    """Convert tradingsymbols to yfinance symbols."""
    # Build lookup maps
    stock_yf_map = {s["tradingsymbol"]: s["tradingsymbol"] + ".NS" for s in stock_list}
    index_yf_map = {i["tradingsymbol"]: i["yfinancetradingsymbol"] for i in index_list}
    commodity_yf_map = {c["tradingsymbol"]: c.get("yfinance_symbol", c["tradingsymbol"]) for c in commodity_list}
    global_yf_map = {g["tradingsymbol"]: g.get("yfinance_symbol", g["tradingsymbol"]) for g in global_indices_list}

    yf_stocks = [stock_yf_map.get(s) for s in stock_symbols if stock_yf_map.get(s)]
    yf_indices = [index_yf_map.get(i) for i in index_symbols if index_yf_map.get(i)]
    yf_commodities = [commodity_yf_map.get(c) for c in commodity_symbols if commodity_yf_map.get(c)]
    yf_globals = [global_yf_map.get(g) for g in global_indices_symbols if global_yf_map.get(g)]

    return yf_stocks, yf_indices, yf_commodities, yf_globals


def main():
    global _running

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
        stock_list, index_list
    )

    logger.info(f"[data-gateway] yfinance symbols: {len(yf_stocks)} stocks, {len(yf_indices)} indices, "
                f"{len(yf_commodities)} commodities, {len(yf_globals)} global indices")

    # ── Initial data load ──────────────────────────────────────────────────
    logger.info("[data-gateway] Starting initial data load...")
    try:
        fetch_initial_daily_data(redis, is_intraday)
        logger.info("[data-gateway] Initial daily data loaded")
    except Exception as e:
        logger.error(f"[data-gateway] Initial data load failed: {e}")
        logger.error(traceback.format_exc())

    # Initial Sensibull fetch
    try:
        logger.info("[data-gateway] Starting initial Sensibull data fetch...")
        fetch_and_publish_cycle(redis, stock_symbols, index_symbols,
                                mode="intraday" if is_intraday else "positional")
        logger.info("[data-gateway] Initial Sensibull data loaded")
    except Exception as e:
        logger.error(f"[data-gateway] Initial Sensibull fetch failed: {e}")
        logger.error(traceback.format_exc())

    # Mark healthy
    redis.hset("service:registry:data-gateway", mapping={
        "name": "data-gateway",
        "pid": str(os.getpid()),
        "status": "healthy",
        "started_at": str(time.time()),
    })
    logger.info("[data-gateway] Initial load complete. Entering main loop.")

    # ── Main loop ──────────────────────────────────────────────────────────
    cycle_count = 0
    while _running:
        cycle_start = time.time()
        cycle_count += 1
        logger.info(f"[data-gateway] Cycle {cycle_count} starting...")

        try:
            # Step 1: Fetch yfinance price data
            fetch_cycle_data(redis, yf_stocks, yf_indices, yf_commodities, yf_globals)
        except Exception as e:
            logger.error(f"[data-gateway] yfinance cycle fetch failed: {e}")

        try:
            # Step 2: Fetch Sensibull data (insights + OI chain)
            fetch_and_publish_cycle(redis, stock_symbols, index_symbols,
                                    mode="intraday" if is_intraday else "positional")
        except Exception as e:
            logger.error(f"[data-gateway] Sensibull cycle fetch failed: {e}")

        # Update heartbeat
        cycle_elapsed = time.time() - cycle_start
        redis.hset("service:registry:data-gateway", mapping={
            "name": "data-gateway",
            "pid": str(os.getpid()),
            "status": "healthy",
            "last_heartbeat": str(time.time()),
            "stats_json": json.dumps({
                "cycle_count": cycle_count,
                "cycle_elapsed": round(cycle_elapsed, 1),
                "stocks": len(stock_symbols),
                "indices": len(index_symbols),
            }, default=str),
        })

        logger.info(f"[data-gateway] Cycle {cycle_count} complete in {cycle_elapsed:.1f}s")

        # Sleep until next cycle
        if _running:
            if is_dev_intraday and not is_prod:
                sleep_time = DEV_LOOP_WAIT_TIME
            else:
                # Align to next 5-min bar (same logic as monolith)
                now = time.localtime()
                seconds_elapsed = now.tm_sec + (now.tm_min % 5) * 60
                sleep_time = CYCLE_SLEEP - seconds_elapsed
                if sleep_time <= 0:
                    sleep_time = CYCLE_SLEEP

            logger.debug(f"[data-gateway] Sleeping {sleep_time}s until next cycle")
            # Sleep in 1s chunks so SIGTERM is responsive
            for _ in range(int(sleep_time)):
                if not _running:
                    break
                time.sleep(1)

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
