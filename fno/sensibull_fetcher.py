"""
SensibullFetcher — Sensibull API data fetcher.

Extracted from common.Stock.fetch_sensibull_data and fetch_sensibull_oi_chain
to separate HTTP / network concerns from the Stock data model.

Usage::

    fetcher = SensibullFetcher()
    fetcher.fetch_data(stock, mode="positional")
    fetcher.fetch_oi_chain(stock, mode="intraday")

Both methods read from and write to ``stock.sensibull_ctx`` so analysers
that access ``stock.sensibull_ctx`` directly continue to work unchanged.
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Optional
from urllib.parse import quote

import requests

from common.logging_util import logger

if TYPE_CHECKING:
    from common.Stock import Stock


class SensibullFetcher:
    """
    Fetches Sensibull insights + OI-chain data and stores results in
    ``stock.sensibull_ctx``.

    No constructor arguments — Sensibull uses unauthenticated HTTP.
    """

    _INSIGHTS_URL = (
        "https://oxide.sensibull.com/v1/compute/cache/insights/stock_info"
        "?tradingsymbol={symbol}"
    )
    _OI_CHAIN_URL  = "https://oxide.sensibull.com/v1/compute/1/oi_graphs/oi_chart"
    _IV_CHART_URL  = "https://oxide.sensibull.com/v1/compute/iv_chart/{symbol}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_data(self, stock: "Stock", mode: str = "positional") -> Optional[object]:
        """
        Fetch stock insights from Sensibull and store them in
        ``stock.sensibull_ctx``.

        Args:
            stock: Target Stock object.
            mode: ``"positional"`` (keep last 30 rows) or ``"intraday"`` (keep 5 days).

        Returns:
            Updated historical DataFrame, or None on failure.
        """
        try:
            encoded = quote(stock.stock_symbol, safe="")
            url = self._INSIGHTS_URL.format(symbol=encoded)
            logger.info(f"Fetching Sensibull data for {stock.stock_symbol} from {url}")

            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            if not (data.get("success") and "payload" in data):
                logger.warning(
                    f"Sensibull API returned unsuccessful response for {stock.stock_symbol}"
                )
                return None

            payload = data["payload"]
            timestamp = datetime.datetime.now()

            ctx = stock.sensibull_ctx
            ctx["last_fetch_time"] = timestamp
            ctx["current"]["underlying_info"] = payload.get("underlying_info")
            ctx["current"]["stats"] = payload.get("stats")
            ctx["current"]["per_expiry_map"] = (
                payload.get("stats", {}).get("per_expiry_map")
            )
            ctx["current"]["nse_stats"] = payload.get("nse_stats")

            stats = payload.get("stats", {})
            base_stats = stats.get("underlying_base_stats", {})
            per_expiry_map = stats.get("per_expiry_map", {})

            historical_row: dict = {
                "timestamp": timestamp,
                "volume_spike": base_stats.get("volume_spike"),
                "volume_spike_type": base_stats.get("volume_spike_type"),
                "future_oi_change": base_stats.get("future_oi_change"),
                "oi_change_type": base_stats.get("oi_change_type"),
                "total_pcr": base_stats.get("total_pcr"),
            }

            for expiry, expiry_data in per_expiry_map.items():
                sfx = expiry.replace("-", "")
                historical_row[f"future_price_{sfx}"] = expiry_data.get("future_price")
                historical_row[f"future_change_pct_{sfx}"] = expiry_data.get("future_change_percent")
                historical_row[f"atm_strike_{sfx}"] = expiry_data.get("atm_strike")
                historical_row[f"atm_iv_{sfx}"] = expiry_data.get("atm_iv")
                historical_row[f"atm_iv_change_{sfx}"] = expiry_data.get("atm_iv_change")
                historical_row[f"atm_iv_percentile_{sfx}"] = expiry_data.get("atm_iv_percentile")
                historical_row[f"atm_ivp_type_{sfx}"] = expiry_data.get("atm_ivp_type")
                historical_row[f"max_pain_{sfx}"] = expiry_data.get("max_pain_strike")
                historical_row[f"max_pain_type_{sfx}"] = expiry_data.get("max_pain_type")
                historical_row[f"pcr_{sfx}"] = expiry_data.get("pcr")
                historical_row[f"pcr_type_{sfx}"] = expiry_data.get("pcr_type")
                historical_row[f"lot_size_{sfx}"] = expiry_data.get("lot_size")

            import pandas as pd

            new_row_df = pd.DataFrame([historical_row])
            existing = ctx["historical_data"]

            if mode == "positional":
                updated_df = (
                    pd.concat([existing, new_row_df], ignore_index=True).tail(30)
                    if not existing.empty
                    else new_row_df
                )
            elif mode == "intraday":
                five_days_ago = timestamp - datetime.timedelta(days=5)
                if not existing.empty:
                    updated_df = pd.concat([existing, new_row_df], ignore_index=True)
                    updated_df = updated_df[updated_df["timestamp"] >= five_days_ago]
                else:
                    updated_df = new_row_df
            else:
                raise ValueError("mode must be 'positional' or 'intraday'")

            ctx["historical_data"] = updated_df
            logger.info(
                f"Sensibull data stored for {stock.stock_symbol}. "
                f"Historical rows: {len(updated_df)}"
            )
            return updated_df

        except requests.exceptions.Timeout:
            logger.error(f"Timeout fetching Sensibull data for {stock.stock_symbol}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error fetching Sensibull data for {stock.stock_symbol}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching Sensibull data for {stock.stock_symbol}: {e}")
        return None

    def fetch_oi_chain(self, stock: "Stock", mode: str = "positional") -> Optional[dict]:
        """
        Fetch per-strike OI chain from Sensibull and store it in
        ``stock.sensibull_ctx``.

        Requires ``fetch_data()`` to have been called first so that
        ``sensibull_ctx["current"]["per_expiry_map"]`` is populated.

        Args:
            stock: Target Stock object.
            mode: ``"positional"`` (single snapshot) or ``"intraday"`` (append to history).

        Returns:
            OI chain snapshot dict, or None on failure.
        """
        try:
            expiries_body = self._build_oi_chain_expiry_body(stock)
            if not expiries_body:
                logger.warning(
                    f"Cannot fetch OI chain for {stock.stock_symbol}: "
                    "no expiry data available. Call fetch_data() first."
                )
                return None

            request_body = {
                "underlying": stock.stock_symbol,
                "expiries": expiries_body,
                "atm_strike_selection": "twenty",
                "input_min_strike": None,
                "input_max_strike": None,
                "auto_update": "full_day",
                "show_prev_oi": True,
            }

            logger.info(
                f"Fetching Sensibull OI chain for {stock.stock_symbol} ({mode})"
            )
            response = requests.post(self._OI_CHAIN_URL, json=request_body, timeout=15)
            response.raise_for_status()
            data = response.json()

            if not (data.get("success") and "payload" in data):
                logger.warning(
                    f"Sensibull OI chain API unsuccessful for {stock.stock_symbol}"
                )
                return None

            payload = data["payload"]
            enabled_expiry = self._find_enabled_expiry(payload)
            timestamp = datetime.datetime.now()

            oi_chain = {
                "timestamp": timestamp,
                "date": payload.get(
                    "input", {}
                ).get("date", timestamp.strftime("%Y-%m-%d")),
                "expiry": enabled_expiry,
                "underlying_symbol": stock.stock_symbol,
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

            ctx = stock.sensibull_ctx
            ctx["oi_chain"] = oi_chain

            if mode == "intraday":
                history = ctx["oi_chain_history"]
                history.append(oi_chain)
                if len(history) > 15:
                    ctx["oi_chain_history"] = history[-15:]
                logger.info(
                    f"OI chain fetched for {stock.stock_symbol} (intraday): "
                    f"PCR={oi_chain['pcr']}, history={len(ctx['oi_chain_history'])}/15"
                )
            else:
                ctx["oi_chain_history"] = [oi_chain]
                logger.info(
                    f"OI chain fetched for {stock.stock_symbol} (positional): "
                    f"PCR={oi_chain['pcr']}"
                )

            return oi_chain

        except requests.exceptions.Timeout:
            logger.error(f"Timeout fetching Sensibull OI chain for {stock.stock_symbol}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error fetching OI chain for {stock.stock_symbol}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching OI chain for {stock.stock_symbol}: {e}")
        return None

    def fetch_iv_chart(self, stock: "Stock") -> Optional[object]:
        """
        Fetch daily ATM IV history from Sensibull iv_chart API and store it in
        ``stock.sensibull_ctx["iv_chart_history"]``.

        The iv_chart API returns 2 years of daily IV closes. Values are already
        in percentage form (e.g. 13.5 = 13.5%) — no decimal conversion needed.

        This is used by IVAnalyser.analyse_trend_in_ATM_IV in positional mode,
        where sensibull_ctx["historical_data"] only has 1 intraday snapshot.

        Skips the fetch if iv_chart_history is already populated (fetch once per day).

        Returns:
            DataFrame with columns [date, iv_close, price_close], or None on failure.
        """
        import pandas as pd

        ctx = stock.sensibull_ctx
        existing = ctx.get("iv_chart_history")
        if existing is not None and not existing.empty:
            logger.debug(
                f"[IV_CHART] {stock.stock_symbol} — already fetched "
                f"({len(existing)} rows), skipping"
            )
            return existing

        try:
            encoded = quote(stock.stock_symbol, safe="")
            url = self._IV_CHART_URL.format(symbol=encoded)
            logger.info(f"[IV_CHART] Fetching IV chart for {stock.stock_symbol} from {url}")

            response = requests.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()

            if not (data.get("success") and "payload" in data):
                logger.warning(
                    f"[IV_CHART] Sensibull iv_chart API returned unsuccessful response "
                    f"for {stock.stock_symbol}"
                )
                return None

            iv_ohlc = data["payload"].get("iv_ohlc_data", {})
            if not iv_ohlc:
                logger.warning(f"[IV_CHART] Empty iv_ohlc_data for {stock.stock_symbol}")
                return None

            rows = []
            for date_str, entry in iv_ohlc.items():
                iv_close    = entry.get("iv")
                price_close = entry.get("close")
                if iv_close is None:
                    continue
                rows.append({
                    "date":        date_str,
                    "iv_close":    float(iv_close),
                    "price_close": float(price_close) if price_close is not None else None,
                })

            if not rows:
                logger.warning(f"[IV_CHART] No valid rows parsed for {stock.stock_symbol}")
                return None

            df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
            ctx["iv_chart_history"] = df
            logger.info(
                f"[IV_CHART] {stock.stock_symbol} — stored {len(df)} daily IV rows "
                f"({df['date'].iloc[0]} → {df['date'].iloc[-1]})"
            )
            return df

        except requests.exceptions.Timeout:
            logger.error(f"[IV_CHART] Timeout fetching iv_chart for {stock.stock_symbol}")
        except requests.exceptions.RequestException as e:
            logger.error(f"[IV_CHART] Request error fetching iv_chart for {stock.stock_symbol}: {e}")
        except Exception as e:
            logger.error(f"[IV_CHART] Unexpected error for {stock.stock_symbol}: {e}")
        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_oi_chain_expiry_body(stock: "Stock") -> Optional[dict]:
        per_expiry_map = (
            stock.sensibull_ctx.get("current", {}).get("per_expiry_map")
        )
        if not per_expiry_map:
            return None
        sorted_expiries = sorted(per_expiry_map.keys())
        if not sorted_expiries:
            return None
        nearest = sorted_expiries[0]
        return {
            exp: {"is_weekly": False, "is_enabled": exp == nearest}
            for exp in sorted_expiries
        }

    @staticmethod
    def _find_enabled_expiry(payload: dict) -> Optional[str]:
        for exp_date, exp_info in (
            payload.get("input", {}).get("expiries", {}).items()
        ):
            if exp_info.get("is_enabled", False):
                return exp_date
        return None
