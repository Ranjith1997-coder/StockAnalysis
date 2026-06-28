"""
Sensibull data fetcher — fetches Sensibull insights + OI chain and publishes to Redis.

Ported from fno/sensibull_fetcher.py but publishes results to Redis hashes
instead of writing to Stock.sensibull_ctx directly.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import os
import threading
import time
from urllib.parse import quote

import requests
import pandas as pd

from services.common.logging import get_logger
logger = get_logger("data-gateway")
from services.common.stock_proxy import StockProxy


SENSIBULL_BASE = "https://oxide.sensibull.com/v1/compute"
INDEX_ANALYSIS_EXCLUDE = {"INDIA_VIX", "FINNIFTY"}


def fetch_sensibull_data(symbol: str, mode: str = "intraday") -> dict | None:
    """
    Fetch Sensibull insights for a single symbol.

    Returns:
        dict with keys: underlying_info, stats, per_expiry_map, nse_stats,
        historical_row (for building historical_data)
    """
    try:
        encoded = quote(symbol, safe="")
        url = f"{SENSIBULL_BASE}/cache/insights/stock_info?tradingsymbol={encoded}"
        response = requests.get(url, timeout=(5, 10))
        response.raise_for_status()
        data = response.json()

        if not (data.get("success") and "payload" in data):
            logger.warning(f"[Sensibull] Unsuccessful response for {symbol}")
            return None

        payload = data["payload"]
        stats = payload.get("stats", {})
        base_stats = stats.get("underlying_base_stats", {})
        per_expiry_map = stats.get("per_expiry_map", {})

        result = {
            "underlying_info": payload.get("underlying_info"),
            "stats": stats,
            "per_expiry_map": per_expiry_map,
            "nse_stats": payload.get("nse_stats"),
        }

        return result

    except requests.exceptions.Timeout:
        logger.error(f"[Sensibull] Timeout fetching data for {symbol}")
    except requests.exceptions.RequestException as e:
        logger.error(f"[Sensibull] Request error for {symbol}: {e}")
    except Exception as e:
        logger.error(f"[Sensibull] Error for {symbol}: {e}")
    return None


def fetch_sensibull_oi_chain(symbol: str, per_expiry_map: dict, mode: str = "intraday") -> dict | None:
    """
    Fetch Sensibull OI chain for a single symbol.

    Args:
        symbol: stock/index symbol
        per_expiry_map: from fetch_sensibull_data() output
        mode: "intraday" or "positional"

    Returns:
        OI chain dict with per_strike_data
    """
    try:
        sorted_expiries = sorted(per_expiry_map.keys())
        if not sorted_expiries:
            logger.warning(f"[Sensibull] No expiry data for {symbol}, skipping OI chain")
            return None

        nearest = sorted_expiries[0]
        expiries_body = {
            exp: {"is_weekly": False, "is_enabled": exp == nearest}
            for exp in sorted_expiries
        }

        body = {
            "underlying": symbol,
            "expiries": expiries_body,
            "atm_strike_selection": "twenty",
            "input_min_strike": None,
            "input_max_strike": None,
            "auto_update": "full_day",
            "show_prev_oi": True,
        }

        url = f"{SENSIBULL_BASE}/1/oi_graphs/oi_chart"
        response = requests.post(url, json=body, timeout=(5, 15))
        response.raise_for_status()
        data = response.json()

        if not (data.get("success") and "payload" in data):
            logger.warning(f"[Sensibull] OI chain unsuccessful for {symbol}")
            return None

        payload = data["payload"]
        timestamp = datetime.datetime.now()

        enabled_expiry = None
        for exp_date, exp_info in payload.get("input", {}).get("expiries", {}).items():
            if exp_info.get("is_enabled", False):
                enabled_expiry = exp_date
                break

        oi_chain = {
            "timestamp": timestamp.isoformat(),
            "date": payload.get("input", {}).get("date", timestamp.strftime("%Y-%m-%d")),
            "expiry": enabled_expiry,
            "underlying_symbol": symbol,
            "prev_ltp": payload.get("prev_ltp"),
            "current_ltp": payload.get("current_ltp") or payload.get("date_ltp"),
            "date_ltp": payload.get("date_ltp"),
            "atm_strike": payload.get("atm_strike"),
            "total_call_oi": payload.get("total_call_oi", 0),
            "total_put_oi": payload.get("total_put_oi", 0),
            "total_call_oi_change": payload.get("total_call_oi_change", 0),
            "total_put_oi_change": payload.get("total_put_oi_change", 0),
            "pcr": payload.get("pcr"),
            "per_strike_data": payload.get("per_strike_data", {}),
            "strike_list": payload.get("strike_list", []),
            "min_strike": payload.get("min_strike"),
            "max_strike": payload.get("max_strike"),
            "underlying_token": payload.get("underlying_token"),
        }

        return oi_chain

    except requests.exceptions.Timeout:
        logger.error(f"[Sensibull] Timeout fetching OI chain for {symbol}")
    except requests.exceptions.RequestException as e:
        logger.error(f"[Sensibull] Request error for OI chain {symbol}: {e}")
    except Exception as e:
        logger.error(f"[Sensibull] Error fetching OI chain for {symbol}: {e}")
    return None


def build_historical_row(symbol: str, sensibull_data: dict) -> dict:
    """Build a single historical_data row from the Sensibull insights payload."""
    stats = sensibull_data.get("stats", {})
    base_stats = stats.get("underlying_base_stats", {})
    per_expiry_map = stats.get("per_expiry_map", {})

    row = {
        "timestamp": datetime.datetime.now(),
        "volume_spike": base_stats.get("volume_spike"),
        "volume_spike_type": base_stats.get("volume_spike_type"),
        "future_oi_change": base_stats.get("future_oi_change"),
        "oi_change_type": base_stats.get("oi_change_type"),
        "total_pcr": base_stats.get("total_pcr"),
    }

    for expiry, expiry_data in per_expiry_map.items():
        sfx = expiry.replace("-", "")
        row[f"future_price_{sfx}"] = expiry_data.get("future_price")
        row[f"future_change_pct_{sfx}"] = expiry_data.get("future_change_percent")
        row[f"atm_strike_{sfx}"] = expiry_data.get("atm_strike")
        row[f"atm_iv_{sfx}"] = expiry_data.get("atm_iv")
        row[f"atm_iv_change_{sfx}"] = expiry_data.get("atm_iv_change")
        row[f"atm_iv_percentile_{sfx}"] = expiry_data.get("atm_iv_percentile")
        row[f"atm_ivp_type_{sfx}"] = expiry_data.get("atm_ivp_type")
        row[f"max_pain_{sfx}"] = expiry_data.get("max_pain_strike")
        row[f"max_pain_type_{sfx}"] = expiry_data.get("max_pain_type")
        row[f"pcr_{sfx}"] = expiry_data.get("pcr")
        row[f"pcr_type_{sfx}"] = expiry_data.get("pcr_type")
        row[f"lot_size_{sfx}"] = expiry_data.get("lot_size")

    return row


def publish_to_redis(redis_proxy, symbol: str, current_data: dict | None,
                     oi_chain: dict | None, existing_ctx: dict | None, mode: str = "intraday"):
    """
    Publish Sensibull data to Redis hashes.

    Args:
        redis_proxy: RedisProxy instance
        symbol: stock/index symbol
        current_data: sensibull insights result dict (or None)
        oi_chain: OI chain result dict (or None)
        existing_ctx: existing sensibull_ctx from Redis (or empty dict)
        mode: "intraday" or "positional"
    """
    ctx = existing_ctx or {}
    if current_data:
        ctx["current"] = current_data
        ctx["last_fetch_time"] = str(datetime.datetime.now())

        # Build and append historical row
        new_row = build_historical_row(symbol, current_data)
        new_row_df = pd.DataFrame([new_row])

        existing_hist = ctx.get("historical_data", pd.DataFrame())
        if isinstance(existing_hist, str):
            existing_hist = pd.DataFrame()

        if not existing_hist.empty:
            updated_hist = pd.concat([existing_hist, new_row_df], ignore_index=True)
            if mode == "positional":
                ctx["historical_data"] = updated_hist.tail(30)
            else:
                five_days_ago = datetime.datetime.now() - datetime.timedelta(days=5)
                ctx["historical_data"] = updated_hist[updated_hist["timestamp"] >= five_days_ago]
        else:
            ctx["historical_data"] = new_row_df

    if oi_chain:
        ctx["oi_chain"] = oi_chain
        history = ctx.get("oi_chain_history", [])
        history.append(oi_chain)
        if len(history) > 15:
            history = history[-15:]
        ctx["oi_chain_history"] = history

    # Serialize to Redis
    mapping = {
        "last_fetch_time": str(ctx.get("last_fetch_time", "")),
        "current_json": json.dumps(ctx.get("current", {}), default=str),
        "historical_data_json": ctx.get("historical_data", pd.DataFrame()).to_json(orient="split", date_format="iso") if isinstance(ctx.get("historical_data"), pd.DataFrame) else "{}",
        "oi_chain_json": json.dumps(ctx.get("oi_chain"), default=str) if ctx.get("oi_chain") else "null",
        "oi_chain_history_json": json.dumps(ctx.get("oi_chain_history", []), default=str),
        "iv_chart_history_json": ctx.get("iv_chart_history", pd.DataFrame()).to_json(orient="split", date_format="iso") if isinstance(ctx.get("iv_chart_history"), pd.DataFrame) else "{}",
        "oi_history_json": ctx.get("oi_history", pd.DataFrame()).to_json(orient="split", date_format="iso") if isinstance(ctx.get("oi_history"), pd.DataFrame) else "{}",
    }

    redis_proxy.hset(f"data:sensibull:{symbol}", mapping=mapping)
    logger.info(f"[Sensibull] Published data for {symbol} (current: {current_data is not None}, oi_chain: {oi_chain is not None})")


def fetch_and_publish_cycle(redis_proxy, stock_symbols: list[str], index_symbols: list[str],
                            mode: str = "intraday"):
    """
    Fetch Sensibull data + OI chain for all symbols in a single cycle.
    Runs sequentially per symbol to avoid overwhelming the free API.

    Args:
        redis_proxy: RedisProxy instance
        stock_symbols: list of stock symbols (e.g. ["RELIANCE", "TCS"])
        index_symbols: list of index symbols (e.g. ["NIFTY", "BANKNIFTY"])
        mode: "intraday" or "positional"
    """
    all_symbols = stock_symbols + index_symbols
    successes = 0
    failures = 0

    for symbol in all_symbols:
        if symbol in INDEX_ANALYSIS_EXCLUDE:
            continue

        try:
            # Fetch existing ctx from Redis
            existing_raw = redis_proxy.hgetall(f"data:sensibull:{symbol}")
            existing_ctx = _deserialize_sensibull_ctx(existing_raw)

            # Step 1: Fetch insights
            current_data = fetch_sensibull_data(symbol, mode)
            if current_data is None:
                failures += 1
                continue

            per_expiry_map = current_data.get("per_expiry_map", {})

            # Step 2: Fetch OI chain
            oi_chain = fetch_sensibull_oi_chain(symbol, per_expiry_map, mode)

            # Step 3: Publish to Redis
            publish_to_redis(redis_proxy, symbol, current_data, oi_chain, existing_ctx, mode)
            successes += 1

        except Exception as e:
            logger.error(f"[Sensibull] Error in cycle for {symbol}: {e}")
            failures += 1

    logger.info(f"[Sensibull] Cycle complete: {successes} success, {failures} failure")


SENSIBULL_WORKERS = int(os.environ.get("SENSIBULL_WORKERS", "10"))


def fetch_and_publish_cycle_parallel(redis_proxy, stock_symbols: list[str], index_symbols: list[str],
                                      mode: str = "intraday") -> tuple[int, int]:
    """
    Parallel version of fetch_and_publish_cycle.

    Uses a thread pool to fetch Sensibull data concurrently, reducing cycle
    time from ~106s (sequential) to ~12s (10 workers).

    Args:
        redis_proxy: RedisProxy instance
        stock_symbols: list of stock symbols
        index_symbols: list of index symbols
        mode: "intraday" or "positional"

    Returns:
        tuple of (success_count, failure_count)
    """
    all_symbols = stock_symbols + index_symbols
    all_symbols = [s for s in all_symbols if s not in INDEX_ANALYSIS_EXCLUDE]

    success_count = [0]
    failure_count = [0]
    lock = threading.Lock()

    def _fetch_one(symbol: str):
        try:
            existing_raw = redis_proxy.hgetall(f"data:sensibull:{symbol}")
            existing_ctx = _deserialize_sensibull_ctx(existing_raw)

            current_data = fetch_sensibull_data(symbol, mode)
            if current_data is None:
                with lock:
                    failure_count[0] += 1
                return

            per_expiry_map = current_data.get("per_expiry_map", {})
            oi_chain = fetch_sensibull_oi_chain(symbol, per_expiry_map, mode)

            publish_to_redis(redis_proxy, symbol, current_data, oi_chain, existing_ctx, mode)
            with lock:
                success_count[0] += 1

        except Exception as e:
            logger.error(f"[Sensibull] Error in cycle for {symbol}: {e}")
            with lock:
                failure_count[0] += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=SENSIBULL_WORKERS) as pool:
        futures = [pool.submit(_fetch_one, symbol) for symbol in all_symbols]
        for future in concurrent.futures.as_completed(futures):
            future.result()

    logger.info(f"[Sensibull] Parallel cycle complete: {success_count[0]} success, {failure_count[0]} failure")
    return success_count[0], failure_count[0]


def _deserialize_sensibull_ctx(raw: dict) -> dict:
    """Convert raw Redis hgetall result back to a sensibull_ctx dict."""
    ctx = {}
    if not raw:
        return ctx

    current_json = raw.get("current_json", "{}")
    ctx["current"] = json.loads(current_json) if current_json != "{}" else {}
    ctx["last_fetch_time"] = raw.get("last_fetch_time")

    hist_json = raw.get("historical_data_json", "{}")
    ctx["historical_data"] = pd.read_json(hist_json, orient="split") if hist_json != "{}" else pd.DataFrame()

    oi_chain_raw = raw.get("oi_chain_json", "null")
    ctx["oi_chain"] = json.loads(oi_chain_raw) if oi_chain_raw != "null" else None

    hist_list_raw = raw.get("oi_chain_history_json", "[]")
    ctx["oi_chain_history"] = json.loads(hist_list_raw) if hist_list_raw != "[]" else []

    iv_chart_raw = raw.get("iv_chart_history_json", "{}")
    ctx["iv_chart_history"] = pd.read_json(iv_chart_raw, orient="split") if iv_chart_raw != "{}" else pd.DataFrame()

    oi_hist_raw = raw.get("oi_history_json", "{}")
    ctx["oi_history"] = pd.read_json(oi_hist_raw, orient="split") if oi_hist_raw != "{}" else pd.DataFrame()

    return ctx
