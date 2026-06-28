"""
Zerodha futures fetcher for the data-gateway.

Two-phase design:
  1. Instruments fetch (public API, no enctoken needed) — once at startup.
  2. Historical data fetch (needs enctoken) — every cycle.

The enctoken is published to Redis by the monolith after TOTP login at 09:00.
The data-gateway subscribes to auth:enctoken_refreshed Pub/Sub to get it.
"""

import datetime
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pandas as pd

from common.logging_util import logger
from zerodha.zerodha_connect import KiteConnect
from common import constants as constant


AUTH_HASH = "auth:zerodha"
AUTH_CHANNEL = "auth:enctoken_refreshed"
AUTH_COMMANDS_STREAM = "auth:commands"
ZERODHA_HASH_TEMPLATE = "data:zerodha:{symbol}"
FUTURES_WORKERS = 5


def fetch_instruments() -> dict:
    """Fetch the full instrument list from Zerodha (public API, no enctoken).

    Returns:
        dict: {symbol: {"current": DataFrame, "next": DataFrame}}
              Each DataFrame has columns: instrument_token, tradingsymbol, expiry
    """
    kc = KiteConnect(constant.DUMMY_API_KEY_ZERODHA)
    all_instruments = pd.DataFrame(kc.instruments())
    all_futures = all_instruments[
        all_instruments["segment"].isin(["NFO-FUT", "BFO-FUT"])
    ].copy()

    result: dict = {}
    for symbol in all_futures["name"].unique():
        sym_futures = all_futures[all_futures["name"] == symbol]
        expiry_dates = sorted(sym_futures["expiry"].unique())
        if not expiry_dates:
            continue

        current = sym_futures[sym_futures["expiry"] == expiry_dates[0]][
            ["instrument_token", "tradingsymbol", "expiry"]
        ].reset_index(drop=True)

        next_df = pd.DataFrame()
        if len(expiry_dates) > 1:
            next_df = sym_futures[sym_futures["expiry"] == expiry_dates[1]][
                ["instrument_token", "tradingsymbol", "expiry"]
            ].reset_index(drop=True)

        result[symbol] = {"current": current, "next": next_df}

    logger.info(f"[zerodha-fetcher] Loaded {len(result)} symbols from Zerodha instruments")
    return result


def _candles_to_df(hist_data: list) -> pd.DataFrame:
    """Convert raw historical data from kc.historical_data() to a DataFrame.

    Same format as FuturesFetcher._candles_to_df() but without spot_map
    (the data-gateway doesn't have priceData available).
    """
    rows = []
    for candle in hist_data:
        dt = (
            pd.Timestamp(candle["date"])
            .tz_convert("Asia/Kolkata")
            .replace(hour=5, minute=30, second=0)
        )
        rows.append({
            "date": dt,
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "volume": candle["volume"],
            "oi": candle.get("oi"),
            "underlying_price": candle["close"],
        })
    return pd.DataFrame(rows).set_index("date")


def _build_mdata_json(future_info: dict) -> str:
    """Build the futures_mdata_json for Redis.

    This is the same format expected by load_zerodha_from_redis():
    {"current": [{instrument_token, tradingsymbol, ...}], "next": [...]}
    """
    def _df_to_dict_list(df):
        if df is None or df.empty:
            return None
        return df.to_dict(orient="records")

    return json.dumps({
        "current": _df_to_dict_list(future_info.get("current")),
        "next": _df_to_dict_list(future_info.get("next")),
    }, default=str)


def _fetch_one_symbol(
    kc: KiteConnect,
    symbol: str,
    token: int,
    interval: str,
    from_date: str,
    to_date: str,
    is_next: bool,
    retries: int = 3,
) -> Optional[pd.DataFrame]:
    """Fetch historical data for a single instrument token."""
    for attempt in range(retries):
        try:
            hist_data = kc.historical_data(
                instrument_token=token,
                from_date=from_date,
                to_date=to_date,
                interval=interval,
                oi=True,
                continuous=False,
            )
            if hist_data:
                return _candles_to_df(hist_data)
            return None
        except Exception as e:
            err_str = str(e)
            if "Too many requests" in err_str:
                logger.debug(f"[zerodha-fetcher] Rate limit for {symbol}, sleeping 1s…")
                time.sleep(1)
            elif "403" in err_str or "TokenException" in err_str:
                raise  # enctoken expired — propagate to caller
            else:
                logger.error(f"[zerodha-fetcher] Error fetching {symbol} "
                             f"({'next' if is_next else 'current'}): {e}")
                if attempt == retries - 1:
                    return None
                time.sleep(1)
    return None


class ZerodhaFuturesManager:
    """Manages the enctoken lifecycle and parallel futures fetching.

    Must be initialised after the data-gateway's Redis connection is established.
    The enctoken arrives via Redis (published by the monolith after TOTP login).
    """

    def __init__(self, redis, futures_mdata: dict):
        self._redis = redis
        self._futures_mdata = futures_mdata
        self._kc: Optional[KiteConnect] = None
        self._has_enctoken = False
        self._lock = threading.Lock()

        # Check if monolith already published an enctoken
        enctoken = redis.hget(AUTH_HASH, "enctoken")
        if enctoken:
            self._init_kite(enctoken)
            logger.info("[zerodha-fetcher] Loaded enctoken from Redis")

        # Subscribe to future enctoken refreshes
        self._start_enctoken_subscriber()

    def _init_kite(self, enctoken: str):
        """Create or update KiteConnect with a new enctoken."""
        if self._kc is None:
            self._kc = KiteConnect(constant.DUMMY_API_KEY_ZERODHA, enctoken=enctoken)
        else:
            self._kc.update_enctoken(enctoken)
        self._has_enctoken = True
        logger.info("[zerodha-fetcher] KiteConnect enctoken updated")

    def _start_enctoken_subscriber(self):
        """Background thread listening for enctoken refresh Pub/Sub messages."""
        def _listen():
            ps = self._redis.pubsub()
            ps.subscribe(AUTH_CHANNEL)
            logger.info(f"[zerodha-fetcher] Subscribed to {AUTH_CHANNEL}")
            for message in ps.listen():
                if message["type"] == "message":
                    enctoken = self._redis.hget(AUTH_HASH, "enctoken")
                    if enctoken:
                        with self._lock:
                            self._init_kite(enctoken)
                        logger.info("[zerodha-fetcher] Enctoken refreshed via Pub/Sub")
        t = threading.Thread(target=_listen, daemon=True, name="enctoken-subscriber")
        t.start()

    def has_enctoken(self) -> bool:
        return self._has_enctoken

    def request_refresh(self, reason: str = "403"):
        """Publish a refresh request to the auth:commands stream.
        
        The monolith subscribes to this stream and runs _refresh_zerodha_auth()
        on demand, which publishes the new enctoken to auth:zerodha.
        """
        self._redis.xadd(AUTH_COMMANDS_STREAM, {
            "command": "refresh_enctoken",
            "reason": reason,
            "timestamp": str(time.time()),
        }, maxlen=100)
        logger.warning(f"[zerodha-fetcher] Requested enctoken refresh (reason={reason})")

    def fetch_and_publish(
        self,
        redis,
        symbols: list,
        mode: str,
    ) -> tuple[int, int]:
        """Fetch futures data for all symbols and publish to Redis.

        Args:
            symbols: List of symbol strings (stocks + indices).
            mode: "intraday" (current expiry only) or "positional" (both expiries).

        Returns:
            (ok_count, fail_count)
        """
        with self._lock:
            if not self._has_enctoken or self._kc is None:
                logger.warning("[zerodha-fetcher] No enctoken — cannot fetch futures")
                return 0, len(symbols)
            kc = self._kc
            has_enctoken = self._has_enctoken

        if not has_enctoken:
            return 0, len(symbols)

        if mode == "intraday":
            interval = "5minute"
            today_str = datetime.datetime.now().strftime("%Y-%m-%d")
            from_date = to_date = today_str
            is_next_expiry_required = False
        else:
            interval = "day"
            end_date = datetime.datetime.now()
            from_date = (end_date - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
            to_date = end_date.strftime("%Y-%m-%d")
            is_next_expiry_required = True

        ok_count = 0
        fail_count = 0

        def _process_one(symbol: str) -> tuple[str, bool]:
            nonlocal fail_count  # Actually can't use nonlocal in nested with ThreadPoolExecutor
                                   # We'll use the outer counters via the executor result
            future_info = self._futures_mdata.get(symbol)
            if future_info is None:
                return symbol, False

            current_df = future_info.get("current")
            next_df = future_info.get("next")

            if current_df is None or current_df.empty:
                return symbol, False

            current_token = int(current_df.iloc[0]["instrument_token"])

            try:
                current_result = _fetch_one_symbol(
                    kc, symbol, current_token, interval,
                    from_date, to_date, is_next=False,
                )
            except Exception as e:
                err_str = str(e)
                if "403" in err_str or "TokenException" in err_str:
                    self._has_enctoken = False
                    self.request_refresh(reason="403 on current expiry")
                logger.error(f"[zerodha-fetcher] {symbol} current expiry fetch failed: {e}")
                return symbol, False

            next_result = pd.DataFrame()
            if is_next_expiry_required and next_df is not None and not next_df.empty:
                next_token = int(next_df.iloc[0]["instrument_token"])
                try:
                    next_result = _fetch_one_symbol(
                        kc, symbol, next_token, interval,
                        from_date, to_date, is_next=True,
                    )
                    if next_result is None:
                        next_result = pd.DataFrame()
                except Exception as e:
                    err_str = str(e)
                    if "403" in err_str or "TokenException" in err_str:
                        self._has_enctoken = False
                        self.request_refresh(reason="403 on next expiry")
                    logger.warning(f"[zerodha-fetcher] {symbol} next expiry fetch failed: {e}")

            # Serialize and publish to Redis
            mapping = {}
            if current_result is not None and not current_result.empty:
                mapping["futures_data_current_json"] = current_result.to_json(
                    orient="split", date_format="iso"
                )
            else:
                mapping["futures_data_current_json"] = "{}"

            if next_result is not None and not next_result.empty:
                mapping["futures_data_next_json"] = next_result.to_json(
                    orient="split", date_format="iso"
                )
            else:
                mapping["futures_data_next_json"] = "{}"

            mapping["futures_mdata_json"] = _build_mdata_json(future_info)

            redis.hset(ZERODHA_HASH_TEMPLATE.format(symbol=symbol), mapping=mapping)
            return symbol, True

        with ThreadPoolExecutor(max_workers=FUTURES_WORKERS) as pool:
            futures = {pool.submit(_process_one, sym): sym for sym in symbols}
            for future in as_completed(futures, timeout=180):
                symbol = futures[future]
                try:
                    _, success = future.result()
                    if success:
                        ok_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    logger.error(f"[zerodha-fetcher] Unexpected error for {symbol}: {e}")
                    fail_count += 1

        logger.info(
            f"[zerodha-fetcher] Futures fetch complete: {ok_count} ok, "
            f"{fail_count} failed (mode={mode})"
        )
        return ok_count, fail_count
