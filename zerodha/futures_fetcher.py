"""
FuturesFetcher — Zerodha Kite futures historical data fetcher.

Extracted from common.Stock.get_futures_data_for_stock to separate HTTP /
network concerns from the Stock data model.

Usage::

    fetcher = FuturesFetcher(kite_connect=shared.app_ctx.zd_kc)
    fetcher.fetch(stock, mode="positional")
    fetcher.fetch(stock, mode="intraday", is_next_expiry_required=True)

The fetcher reads from and writes to ``stock.zerodha_ctx`` so the rest of the
codebase (Futures_Analyser, OI analysers) continues to access futures data via
``stock.zerodha_ctx["futures_data"]`` unchanged.
"""
from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING, Tuple

import pandas as pd

from common.logging_util import logger

if TYPE_CHECKING:
    from common.Stock import Stock


class FuturesFetcher:
    """
    Fetches futures OHLC + OI data from the Zerodha Kite API and stores
    the result in ``stock.zerodha_ctx["futures_data"]``.

    Args:
        kite_connect: An authenticated KiteConnect instance.
    """

    def __init__(self, kite_connect) -> None:
        self._kite = kite_connect

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(
        self,
        stock: "Stock",
        mode: str = "positional",
        is_next_expiry_required: bool = False,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Fetch futures data for *stock* and store it in
        ``stock.zerodha_ctx["futures_data"]``.

        Args:
            stock: Target Stock object.
            mode: ``"positional"`` (daily data, last 5 business days) or
                  ``"intraday"`` (5-min data, today only).
            is_next_expiry_required: When True, also fetch the next-expiry contract.

        Returns:
            Tuple of (futures_data_current, futures_data_next) DataFrames.

        Raises:
            ValueError: For an unknown mode string.
            Exception: Re-raises on unrecoverable API errors.
        """
        zerodha_ctx = stock.zerodha_ctx
        futures_mdata_current = zerodha_ctx["futures_mdata"]["current"]
        futures_mdata_next = zerodha_ctx["futures_mdata"]["next"]

        try:
            if mode == "positional":
                return self._fetch_positional(
                    stock, zerodha_ctx,
                    futures_mdata_current, futures_mdata_next,
                    is_next_expiry_required,
                )
            elif mode == "intraday":
                return self._fetch_intraday(
                    stock, zerodha_ctx,
                    futures_mdata_current, futures_mdata_next,
                    is_next_expiry_required,
                )
            else:
                raise ValueError("mode must be 'positional' or 'intraday'")

        except Exception as e:
            logger.error(f"FuturesFetcher.fetch failed for {stock.stock_symbol}: {e}")
            raise

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_positional(
        self, stock, zerodha_ctx,
        futures_mdata_current, futures_mdata_next,
        is_next_expiry_required,
    ):
        interval = "day"
        end_date = datetime.datetime.now()
        # 90-day lookback gives full single-contract life (~55 rows).
        # continuous=False keeps OI clean — no rollover artifacts.
        start_date = end_date - datetime.timedelta(days=90)
        # Rollover analysis always needs next expiry OI, so always fetch it
        # in positional mode regardless of the caller's flag.
        is_next_expiry_required = True

        def _fetch_with_retry(token):
            for attempt in range(3):
                try:
                    return self._kite.historical_data(
                        instrument_token=token,
                        from_date=start_date.strftime("%Y-%m-%d"),
                        to_date=end_date.strftime("%Y-%m-%d"),
                        interval=interval,
                        oi=True,
                        continuous=False,
                    )
                except Exception as e:
                    if "Too many requests" in str(e):
                        logger.warning(
                            f"Rate limit hit for {stock.stock_symbol}, sleeping 1s…"
                        )
                        time.sleep(1)
                    else:
                        logger.error(f"Error fetching futures data for token {token}: {e}")
                        raise
            logger.error(f"Failed to fetch futures data for token {token} after 3 attempts")
            raise Exception(f"Too many requests for futures token {token}")

        # Build a date → spot_price lookup from stock.priceData so each daily
        # futures row gets the correct underlying_price (fixes the basis=0 bug).
        spot_map: dict = {}
        if stock.priceData is not None and not stock.priceData.empty:
            for ts, row in stock.priceData.iterrows():
                day_key = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
                spot_map[day_key] = float(row.get("Close", row.get("close", 0)) or 0)

        futures_data_current = pd.DataFrame()
        if futures_mdata_current is not None:
            token = futures_mdata_current["instrument_token"].values[0]
            hist_data = _fetch_with_retry(token)
            if hist_data:
                futures_data_current = self._candles_to_df(hist_data, spot_map=spot_map)
                logger.info(
                    f"Futures data for {stock.stock_symbol} (current expiry) "
                    f"fetched for {len(futures_data_current)} rows."
                )

        futures_data_next = pd.DataFrame()
        if is_next_expiry_required and futures_mdata_next is not None:
            token_next = futures_mdata_next["instrument_token"].values[0]
            hist_data_next = _fetch_with_retry(token_next)
            if hist_data_next:
                futures_data_next = self._candles_to_df(hist_data_next, spot_map=spot_map)
                logger.info(
                    f"Futures data for {stock.stock_symbol} (next expiry) "
                    f"fetched for {len(futures_data_next)} rows."
                )

        zerodha_ctx["futures_data"]["current"] = futures_data_current
        zerodha_ctx["futures_data"]["next"] = futures_data_next
        return futures_data_current, futures_data_next

    def _fetch_intraday(
        self, stock, zerodha_ctx,
        futures_mdata_current, futures_mdata_next,
        is_next_expiry_required,
    ):
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        intraday_dates = [
            dt for dt in stock.priceData.index
            if dt.strftime("%Y-%m-%d") == today_str
        ]
        if len(intraday_dates) < 2:
            logger.info(f"Not enough intraday data for {stock.stock_symbol}")
            raise Exception(f"Not enough intraday data for {stock.stock_symbol}")

        dt = intraday_dates[-2]
        underlying_price = stock.priceData.loc[dt, "Close"]
        interval = "5minute"

        # ── Current expiry ────────────────────────────────────────────────
        futures_data_current = zerodha_ctx["futures_data"]["current"]
        if futures_mdata_current is not None and not futures_mdata_current.empty:
            token = futures_mdata_current["instrument_token"].values[0]
            hist_data = self._fetch_intraday_candle(
                stock, token, dt, interval
            )
            if hist_data:
                candle = next(
                    (c for c in hist_data
                     if pd.Timestamp(c["date"]).tz_convert("Asia/Kolkata") == dt),
                    None,
                )
                if candle:
                    row = self._candle_to_row(candle, dt, underlying_price)
                    new_df = pd.DataFrame([row]).set_index("date")
                    futures_data_current = (
                        pd.concat([futures_data_current, new_df])
                        if not futures_data_current.empty
                        else new_df
                    )
                    logger.info(
                        f"Futures data for {stock.stock_symbol} (current) at {dt}: "
                        f"O={row['open']:.2f} H={row['high']:.2f} L={row['low']:.2f} "
                        f"C={row['close']:.2f} vol={row['volume']:,} oi={row.get('oi', 0):,} "
                        f"spot={float(row.get('underlying_price') or 0):.2f}"
                    )

        # ── Next expiry ───────────────────────────────────────────────────
        futures_data_next = zerodha_ctx["futures_data"]["next"]
        if is_next_expiry_required and futures_mdata_next is not None:
            token_next = futures_mdata_next["instrument_token"].values[0]
            for ts in intraday_dates:
                existing = futures_data_next.index if not futures_data_next.empty else []
                if ts in existing:
                    continue
                hist_data_next = self._fetch_intraday_candle(
                    stock, token_next,
                    ts, interval,
                    dt_fmt="%Y-%m-%d %H:%M:%S",
                )
                if hist_data_next:
                    candle_next = next(
                        (c for c in hist_data_next
                         if pd.Timestamp(c["date"]).tz_convert("Asia/Kolkata") == ts),
                        None,
                    )
                    if candle_next:
                        row_next = self._candle_to_row(
                            candle_next, ts, candle_next["close"]
                        )
                        new_df = pd.DataFrame([row_next]).set_index("date")
                        futures_data_next = (
                            pd.concat([futures_data_next, new_df])
                            if not futures_data_next.empty
                            else new_df
                        )
                        logger.info(
                            f"Futures data for {stock.stock_symbol} (next) at {ts}: {row_next}"
                        )

        zerodha_ctx["futures_data"]["current"] = futures_data_current
        zerodha_ctx["futures_data"]["next"] = futures_data_next
        return futures_data_current, futures_data_next

    def _fetch_intraday_candle(self, stock, token, dt, interval,
                               dt_fmt: str = "%Y-%m-%d"):
        for attempt in range(3):
            try:
                return self._kite.historical_data(
                    instrument_token=token,
                    from_date=dt.strftime(dt_fmt),
                    to_date=dt.strftime(dt_fmt),
                    interval=interval,
                    oi=True,
                )
            except Exception as e:
                if "Too many requests" in str(e):
                    logger.warning(
                        f"Rate limit hit for {stock.stock_symbol}, sleeping 1s…"
                    )
                    time.sleep(1)
                else:
                    logger.error(
                        f"Error fetching intraday futures data for token {token} at {dt}: {e}"
                    )
                    raise
        logger.error(
            f"Failed to fetch intraday futures data for token {token} at {dt} after 3 attempts"
        )
        raise Exception(f"Too many requests for futures token {token} at {dt}")

    @staticmethod
    def _candles_to_df(hist_data: list, spot_map: dict | None = None) -> pd.DataFrame:
        rows = []
        for candle in hist_data:
            dt = (
                pd.Timestamp(candle["date"])
                .tz_convert("Asia/Kolkata")
                .replace(hour=5, minute=30, second=0)
            )
            day_key = dt.strftime("%Y-%m-%d")
            spot = (spot_map or {}).get(day_key) or 0
            # Fall back to futures close only when spot is unavailable (intraday
            # callers pass no spot_map; positional callers pass priceData map).
            underlying = spot if spot > 0 else candle["close"]
            rows.append({
                "date": dt,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "volume": candle["volume"],
                "oi": candle.get("oi"),
                "underlying_price": underlying,
            })
        return pd.DataFrame(rows).set_index("date")

    @staticmethod
    def _candle_to_row(candle: dict, dt, underlying_price) -> dict:
        return {
            "date": dt,
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "volume": candle["volume"],
            "oi": candle.get("oi"),
            "underlying_price": underlying_price,
        }
