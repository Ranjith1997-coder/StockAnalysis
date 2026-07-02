"""
yfinance fetcher — downloads price data for all tracked symbols and publishes to Redis.

Replicates the logic from intraday/intraday_monitor.py:fetch_price_data()
and create_stock_and_index_objects(), but publishes results to Redis
instead of writing directly to Stock objects.
"""

from __future__ import annotations

import time
import logging
import datetime
import yfinance as yf
import pandas as pd
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.common.redis_proxy import RedisProxy

from services.common.logging import get_logger
logger = get_logger("data-gateway")
from common.helperFunctions import get_stock_objects_from_json
from services.common.stock_proxy import StockProxy


def _get_prev_day_row(df: pd.DataFrame):
    """Get the previous completed trading day's OHLCV row.

    If the last bar is today (partial / in-progress), use iloc[-2] (yesterday).
    Otherwise (market closed, last bar is the most recent completed day), use
    iloc[-1] — that *is* yesterday's complete bar.

    This fixes the bug where iloc[-2] returned 2-days-ago when the refresh
    ran before market open (today's partial bar doesn't exist yet).
    """
    if df.empty:
        return None
    today = datetime.date.today()
    last_ts = df.index[-1]
    last_date = last_ts.date() if hasattr(last_ts, "date") else None
    if last_date == today and len(df) >= 2:
        return df.iloc[-2]
    return df.iloc[-1]


def fetch_initial_daily_data(redis_proxy: "RedisProxy", is_intraday: bool):
    """
    Fetch prev-day OHLCV + daily price data for all symbols.
    Called once at data-gateway startup.

    Publishes:
        - data:price:{symbol} → prevDayOHLCV_json, daily_hv, priceData_json (daily data)
    """
    stock_list, index_list, commodity_list, global_indices_list = get_stock_objects_from_json()

    _fetch_index_initial(redis_proxy, index_list, is_intraday)
    _fetch_stock_initial(redis_proxy, stock_list, is_intraday)
    _fetch_commodity_initial(redis_proxy, commodity_list, is_intraday)
    _fetch_global_indices_initial(redis_proxy, global_indices_list, is_intraday)


def refresh_prev_day_ohlcv(redis_proxy: "RedisProxy"):
    """Refresh prevDayOHLCV for all symbols using latest 1d data.

    Called once per trading day (after market open) to ensure prevDayOHLCV
    reflects the most recent completed trading day, not the day the data-gateway
    was started.  Uses a 5-day 1d download and takes iloc[-2] as previous day
    (iloc[-1] may be today's partial bar).
    """
    stock_list, index_list, commodity_list, global_indices_list = get_stock_objects_from_json()
    _refresh_group_prev_day(redis_proxy, index_list, "index")
    _refresh_group_prev_day(redis_proxy, stock_list, "stock")
    _refresh_group_prev_day(redis_proxy, commodity_list, "commodity")
    _refresh_group_prev_day(redis_proxy, global_indices_list, "global_index")
    logger.info("[yfinance] prevDayOHLCV refresh complete for all symbols")


def _refresh_group_prev_day(redis_proxy, obj_list, group_name):
    """Fetch 5d 1d data for a group and update prevDayOHLCV_json in Redis."""
    if not obj_list:
        return

    if group_name == "stock":
        symbols = [o["tradingsymbol"] + ".NS" for o in obj_list]
        key_fn = lambda o: o["tradingsymbol"]
        yf_fn = lambda o: o["tradingsymbol"] + ".NS"
    elif group_name == "index":
        symbols = [o["yfinancetradingsymbol"] for o in obj_list]
        key_fn = lambda o: o["tradingsymbol"]
        yf_fn = lambda o: o["yfinancetradingsymbol"]
    else:
        symbols = [o.get("yfinancetradingsymbol", o["tradingsymbol"]) for o in obj_list]
        key_fn = lambda o: o["tradingsymbol"]
        yf_fn = lambda o: o.get("yfinancetradingsymbol", o["tradingsymbol"])

    try:
        data = yf.download(symbols, period="5d", interval="1d", group_by="ticker",
                           auto_adjust=True, progress=False)
    except Exception as e:
        logger.error(f"[yfinance] prevDay refresh download failed ({group_name}): {e}")
        return

    updated = 0
    for obj in obj_list:
        symbol = yf_fn(obj)
        name = key_fn(obj)
        try:
            if len(symbols) == 1:
                sym_data = data
            else:
                sym_data = data[symbol]

            if sym_data.empty or len(sym_data) < 2:
                continue

            ohlcv_row = _get_prev_day_row(sym_data)
            if ohlcv_row is None:
                continue
            prev_day = {
                "OPEN": float(ohlcv_row["Open"]),
                "HIGH": float(ohlcv_row["High"]),
                "LOW": float(ohlcv_row["Low"]),
                "CLOSE": float(ohlcv_row["Close"]),
                "VOLUME": float(ohlcv_row["Volume"]),
            }
            redis_proxy.hset(f"data:price:{name}",
                             mapping={"prevDayOHLCV_json": __import__("json").dumps(prev_day, default=str)})
            updated += 1
        except (KeyError, IndexError, Exception) as e:
            logger.debug(f"[yfinance] prevDay refresh skip {name}: {e}")

    logger.info(f"[yfinance] prevDayOHLCV refreshed for {updated}/{len(obj_list)} {group_name}s")


def _fetch_index_initial(redis_proxy, index_list, is_intraday):
    symbols = [idx["yfinancetradingsymbol"] for idx in index_list]
    if not symbols:
        return

    period = "1y" if is_intraday else "5D"
    logger.info(f"[yfinance] Fetching initial daily data for {len(symbols)} indices ({period})")

    try:
        data = yf.download(symbols, period=period, interval="1d", group_by="ticker", auto_adjust=True, progress=False)
        for idx in index_list:
            symbol = idx["yfinancetradingsymbol"]
            name = idx["tradingsymbol"]
            try:
                if len(symbols) == 1:
                    idx_data = data
                else:
                    idx_data = data[symbol]

                if idx_data.empty or len(idx_data) < 2:
                    logger.warning(f"[yfinance] Insufficient data for {name} ({symbol}), skipping")
                    continue

                ohlcv_row = _get_prev_day_row(idx_data)
                if ohlcv_row is None:
                    continue
                prev_day = {
                    "OPEN": float(ohlcv_row["Open"]),
                    "HIGH": float(ohlcv_row["High"]),
                    "LOW": float(ohlcv_row["Low"]),
                    "CLOSE": float(ohlcv_row["Close"]),
                    "VOLUME": float(ohlcv_row["Volume"]),
                }

                idx_data.index = idx_data.index.tz_localize("UTC").tz_convert("Asia/Kolkata") if idx_data.index.tz is None else idx_data.index.tz_convert("Asia/Kolkata")
                idx_data = idx_data.dropna(how="all")

                import numpy as np
                returns = idx_data["Close"].pct_change().dropna()
                daily_hv = float(returns.std() * (252 ** 0.5) * 100) if len(returns) > 1 else None

                mapping = {
                    "priceData_json": idx_data.to_json(orient="split", date_format="iso"),
                    "prevDayOHLCV_json": __import__("json").dumps(prev_day, default=str),
                    "ltp": "",
                    "ltp_change_perc": "",
                    "daily_hv": str(daily_hv) if daily_hv else "",
                }
                redis_proxy.hset(f"data:price:{name}", mapping=mapping)
                logger.info(f"[yfinance] Published initial data for index {name} ({len(idx_data)} rows)")

            except (KeyError, IndexError) as e:
                logger.warning(f"[yfinance] Failed to get initial data for {name}: {e}")
    except Exception as e:
        logger.error(f"[yfinance] Error fetching initial index data: {e}")


def _fetch_stock_initial(redis_proxy, stock_list, is_intraday):
    symbols = [stk["tradingsymbol"] + ".NS" for stk in stock_list]
    if not symbols:
        return

    period = "1y" if is_intraday else "5D"
    logger.info(f"[yfinance] Fetching initial daily data for {len(symbols)} stocks ({period})")

    try:
        data = yf.download(symbols, period=period, interval="1d", group_by="ticker", auto_adjust=True, progress=False)
        for stk in stock_list:
            symbol = stk["tradingsymbol"] + ".NS"
            name = stk["tradingsymbol"]
            try:
                if len(symbols) == 1:
                    stk_data = data
                else:
                    stk_data = data[symbol]

                if stk_data.empty or len(stk_data) < 2:
                    logger.warning(f"[yfinance] Insufficient data for {name}, skipping")
                    continue

                ohlcv_row = _get_prev_day_row(stk_data)
                if ohlcv_row is None:
                    continue
                prev_day = {
                    "OPEN": float(ohlcv_row["Open"]),
                    "HIGH": float(ohlcv_row["High"]),
                    "LOW": float(ohlcv_row["Low"]),
                    "CLOSE": float(ohlcv_row["Close"]),
                    "VOLUME": float(ohlcv_row["Volume"]),
                }

                stk_data.index = stk_data.index.tz_localize("UTC").tz_convert("Asia/Kolkata") if stk_data.index.tz is None else stk_data.index.tz_convert("Asia/Kolkata")
                stk_data = stk_data.dropna(how="all")

                returns = stk_data["Close"].pct_change().dropna()
                daily_hv = float(returns.std() * (252 ** 0.5) * 100) if len(returns) > 1 else None

                mapping = {
                    "priceData_json": stk_data.to_json(orient="split", date_format="iso"),
                    "prevDayOHLCV_json": __import__("json").dumps(prev_day, default=str),
                    "ltp": "",
                    "ltp_change_perc": "",
                    "daily_hv": str(daily_hv) if daily_hv else "",
                }
                redis_proxy.hset(f"data:price:{name}", mapping=mapping)
                logger.info(f"[yfinance] Published initial data for stock {name} ({len(stk_data)} rows)")

            except (KeyError, IndexError) as e:
                logger.warning(f"[yfinance] Failed to get initial data for {name}: {e}")
    except Exception as e:
        logger.error(f"[yfinance] Error fetching initial stock data: {e}")


def _fetch_commodity_initial(redis_proxy, commodity_list, is_intraday):
    symbols = [c.get("yfinancetradingsymbol", c["tradingsymbol"]) for c in commodity_list]
    if not symbols:
        return
    period = "1y" if is_intraday else "5D"
    logger.info(f"[yfinance] Fetching initial daily data for {len(symbols)} commodities ({period})")
    try:
        data = yf.download(symbols, period=period, interval="1d", group_by="ticker", auto_adjust=True, progress=False)
        for c in commodity_list:
            name = c["tradingsymbol"]
            yf_sym = c.get("yfinancetradingsymbol", name)
            try:
                if len(symbols) == 1:
                    sym_data = data
                else:
                    sym_data = data[yf_sym]
                if sym_data.empty or len(sym_data) < 2:
                    logger.warning(f"[yfinance] Insufficient data for commodity {name}, skipping")
                    continue
                ohlcv_row = _get_prev_day_row(sym_data)
                if ohlcv_row is None:
                    continue
                prev_day = {
                    "OPEN": float(ohlcv_row["Open"]),
                    "HIGH": float(ohlcv_row["High"]),
                    "LOW": float(ohlcv_row["Low"]),
                    "CLOSE": float(ohlcv_row["Close"]),
                    "VOLUME": float(ohlcv_row["Volume"]),
                }
                sym_data.index = (
                    sym_data.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
                    if sym_data.index.tz is None
                    else sym_data.index.tz_convert("Asia/Kolkata")
                )
                sym_data = sym_data.dropna(how="all")
                mapping = {
                    "priceData_json": sym_data.to_json(orient="split", date_format="iso"),
                    "prevDayOHLCV_json": __import__("json").dumps(prev_day, default=str),
                    "ltp": "",
                    "ltp_change_perc": "",
                    "daily_hv": "",
                }
                redis_proxy.hset(f"data:price:{name}", mapping=mapping)
                logger.info(f"[yfinance] Published initial data for commodity {name} ({len(sym_data)} rows)")
            except (KeyError, IndexError) as e:
                logger.warning(f"[yfinance] Failed to get initial data for commodity {name}: {e}")
    except Exception as e:
        logger.error(f"[yfinance] Error fetching initial commodity data: {e}")


def _fetch_global_indices_initial(redis_proxy, global_indices_list, is_intraday):
    symbols = [g.get("yfinancetradingsymbol", g["tradingsymbol"]) for g in global_indices_list]
    if not symbols:
        return
    period = "1y" if is_intraday else "5D"
    logger.info(f"[yfinance] Fetching initial daily data for {len(symbols)} global indices ({period})")
    try:
        data = yf.download(symbols, period=period, interval="1d", group_by="ticker", auto_adjust=True, progress=False)
        for g in global_indices_list:
            name = g["tradingsymbol"]
            yf_sym = g.get("yfinancetradingsymbol", name)
            try:
                if len(symbols) == 1:
                    sym_data = data
                else:
                    sym_data = data[yf_sym]
                if sym_data.empty or len(sym_data) < 2:
                    logger.warning(f"[yfinance] Insufficient data for global index {name}, skipping")
                    continue
                ohlcv_row = _get_prev_day_row(sym_data)
                if ohlcv_row is None:
                    continue
                prev_day = {
                    "OPEN": float(ohlcv_row["Open"]),
                    "HIGH": float(ohlcv_row["High"]),
                    "LOW": float(ohlcv_row["Low"]),
                    "CLOSE": float(ohlcv_row["Close"]),
                    "VOLUME": float(ohlcv_row["Volume"]),
                }
                sym_data.index = (
                    sym_data.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
                    if sym_data.index.tz is None
                    else sym_data.index.tz_convert("Asia/Kolkata")
                )
                sym_data = sym_data.dropna(how="all")
                mapping = {
                    "priceData_json": sym_data.to_json(orient="split", date_format="iso"),
                    "prevDayOHLCV_json": __import__("json").dumps(prev_day, default=str),
                    "ltp": "",
                    "ltp_change_perc": "",
                    "daily_hv": "",
                }
                redis_proxy.hset(f"data:price:{name}", mapping=mapping)
                logger.info(f"[yfinance] Published initial data for global index {name} ({len(sym_data)} rows)")
            except (KeyError, IndexError) as e:
                logger.warning(f"[yfinance] Failed to get initial data for global index {name}: {e}")
    except Exception as e:
        logger.error(f"[yfinance] Error fetching initial global indices data: {e}")


def fetch_cycle_data(redis_proxy: "RedisProxy", stock_symbols: list[str], index_symbols: list[str],
                     commodity_symbols: list[str] = None, global_indices_symbols: list[str] = None,
                     yf_to_key_map: dict[str, str] = None, mode: str = None):
    """
    Fetch intraday 5-min or positional daily data for the current cycle.
    Publishes updated priceData to Redis.

    Args:
        stock_symbols: list of yfinance symbols for F&O stocks
        index_symbols: list of yfinance symbols for indices
        commodity_symbols: list of yfinance symbols for commodities
        global_indices_symbols: list of yfinance symbols for global indices
        yf_to_key_map: optional dict mapping yfinance symbol -> Redis key (tradingsymbol)
        mode: "intraday" or "positional" — overrides env var detection.
              If None, falls back to env vars (legacy behaviour).
    """
    if mode is not None:
        is_positional = mode == "positional"
    else:
        import os
        from dotenv import load_dotenv
        load_dotenv()
        is_prod = os.getenv("PRODUCTION", "0") == "1"
        is_dev_intraday = os.getenv("DEV_INTRADAY", "0") == "1"
        is_dev_positional = os.getenv("DEV_POSITIONAL", "0") == "1"
        is_positional = is_dev_positional or (is_prod and not is_dev_intraday)

    period = "2y" if is_positional else "5d"
    interval = "1d" if is_positional else "5m"

    t0 = time.time()

    if stock_symbols:
        _download_group(redis_proxy, stock_symbols, period, interval, "stock", yf_to_key_map)
    if index_symbols:
        _download_group(redis_proxy, index_symbols, period, interval, "index", yf_to_key_map)
    if commodity_symbols:
        _download_group(redis_proxy, commodity_symbols, period, interval, "commodity", yf_to_key_map)
    if global_indices_symbols:
        _download_group(redis_proxy, global_indices_symbols, period, interval, "global_index", yf_to_key_map)

    elapsed = time.time() - t0
    logger.info(f"[yfinance] Cycle fetch complete: {elapsed:.1f}s ({len(stock_symbols)} stocks, {len(index_symbols)} indices)")


def _download_group(redis_proxy, symbols: list[str], period: str, interval: str, group_name: str,
                     yf_to_key_map: dict[str, str] = None):
    if not symbols:
        return

    try:
        data = yf.download(symbols, period=period, interval=interval, group_by="ticker", auto_adjust=True, progress=False)

        for symbol in symbols:
            try:
                if len(symbols) == 1:
                    sym_data = data
                else:
                    sym_data = data[symbol]
            except KeyError:
                logger.warning(f"[yfinance] {symbol} not found in yfinance download ({group_name})")
                continue

            if sym_data.empty:
                continue

            # Convert timezone
            try:
                if sym_data.index.tz is None:
                    sym_data.index = sym_data.index.tz_localize("UTC").tz_convert("Asia/Kolkata")
                else:
                    sym_data.index = sym_data.index.tz_convert("Asia/Kolkata")
            except Exception:
                pass

            sym_data = sym_data.dropna(how="all")
            expected = {"Open", "High", "Low", "Close", "Volume"}
            actual = set(sym_data.columns.tolist())
            if not expected.issubset(actual):
                logger.warning(f"[yfinance] {symbol}: unexpected columns {sym_data.columns.tolist()}")
                continue

            # Derive Redis key: use yf_to_key_map if available, otherwise strip .NS suffix
            if yf_to_key_map and symbol in yf_to_key_map:
                stock_key = yf_to_key_map[symbol]
            else:
                stock_key = symbol.replace(".NS", "")
            mapping = {
                "priceData_json": sym_data.to_json(orient="split", date_format="iso"),
                "last_price_update": str(pd.Timestamp.now(tz="Asia/Kolkata")),
            }
            redis_proxy.hset(f"data:price:{stock_key}", mapping=mapping)

    except Exception as e:
        logger.error(f"[yfinance] Error fetching {group_name} data: {e}")
