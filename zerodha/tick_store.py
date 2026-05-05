"""
TickStore — live WebSocket tick state for a single instrument.

Extracted from common.Stock to give Stock a single responsibility: holding
price/analysis data.  TickStore owns:

  * The raw zerodha tick snapshot (_zerodha_data) + its threading.Lock
  * Live options tick table (options_live) and aggregate metrics (options_aggregate)
  * Live futures tick table (futures_live)

All public methods mirror the names that previously lived on Stock so that the
Stock façade can delegate to them without any callers changing.
"""
from __future__ import annotations

import threading
import time
from typing import Optional


class TickStore:
    """Thread-safe container for live WebSocket tick data."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Raw equity / index tick snapshot
        self._zerodha_data: dict = {
            "volume_traded": 0,
            "last_price": 0,
            "open": 0,
            "high": 0,
            "close": 0,
            "low": 0,
            "change": 0,
            "average_traded_price": 0,
            "total_buy_quantity": 0,
            "total_sell_quantity": 0,
        }

        # Live options tick data from WebSocket (keyed by strike -> CE/PE)
        # { 24000: { "CE": {ltp, oi, prev_oi, volume, …}, "PE": {…} } }
        self.options_live: dict = {}

        # Aggregated metrics recomputed from options_live
        self.options_aggregate: dict = {
            "total_ce_oi": 0,
            "total_pe_oi": 0,
            "live_pcr": 0.0,
            "atm_strike": None,
            "atm_straddle_premium": 0.0,
            "atm_iv_ce": 0.0,
            "atm_iv_pe": 0.0,
            "iv_skew": 0.0,
            "max_oi_ce_strike": None,
            "max_oi_pe_strike": None,
            "net_ce_oi_change": 0,
            "net_pe_oi_change": 0,
            "last_updated": 0.0,
            # Sensibull WS enrichment — populated when OPTIONS_SOURCE=sensibull;
            # remain at default (0.0 / None) in Zerodha mode.
            "atm_iv": 0.0,
            "atm_iv_percentile": 0.0,
            "atm_ivp_type": None,
            "max_pain_strike": None,
            "future_price": 0.0,
        }

        # Live futures tick data from WebSocket
        # { "current": {ltp, oi, volume, …}, "next": {…} }
        self.futures_live: dict = {}

    # ------------------------------------------------------------------
    # Equity / index tick
    # ------------------------------------------------------------------

    @property
    def zerodha_data(self) -> dict:
        """Thread-safe snapshot of the current tick data."""
        with self._lock:
            return self._zerodha_data.copy()

    def update_zerodha_data(self, ticker_data: dict) -> None:
        """
        Thread-safe update of Zerodha tick data.

        Handles both equity ticks (184-byte full mode with volume/depth)
        and index ticks (28/32-byte quote/full mode with only OHLC).
        """
        with self._lock:
            d = self._zerodha_data
            d["last_price"] = ticker_data.get("last_price", d["last_price"])
            d["change"] = ticker_data.get("change", d["change"])

            ohlc = ticker_data.get("ohlc")
            if ohlc:
                d["open"] = ohlc.get("open", d["open"])
                d["high"] = ohlc.get("high", d["high"])
                d["close"] = ohlc.get("close", d["close"])
                d["low"] = ohlc.get("low", d["low"])

            # These fields are only present in equity/option ticks, not index ticks
            for field in ("volume_traded", "average_traded_price",
                          "total_buy_quantity", "total_sell_quantity"):
                if field in ticker_data:
                    d[field] = ticker_data[field]

    # ------------------------------------------------------------------
    # Options ticks
    # ------------------------------------------------------------------

    def update_option_tick(self, strike: float, option_type: str, tick: dict) -> None:
        """Update live option data from a WebSocket tick."""
        with self._lock:
            if strike not in self.options_live:
                self.options_live[strike] = {}

            entry = self.options_live[strike].get(option_type, {})
            entry["prev_oi"] = entry.get("oi", 0)
            entry["ltp"] = tick.get("last_price", 0)
            entry["oi"] = tick.get("oi", 0)
            entry["volume"] = tick.get("volume_traded", 0)
            entry["buy_qty"] = tick.get("total_buy_quantity", 0)
            entry["sell_qty"] = tick.get("total_sell_quantity", 0)
            entry["timestamp"] = tick.get("exchange_timestamp")

            # Greeks (populated by Sensibull path; absent in Zerodha ticks)
            if "delta" in tick:
                entry["delta"]     = tick["delta"]
                entry["gamma"]     = tick.get("gamma", 0.0)
                entry["theta"]     = tick.get("theta", 0.0)
                entry["vega"]      = tick.get("vega", 0.0)
                entry["iv"]        = tick.get("iv", 0.0)
                entry["iv_change"] = tick.get("iv_change", 0.0)

            if "ohlc" in tick:
                entry["open"] = tick["ohlc"].get("open", 0)
                entry["high"] = tick["ohlc"].get("high", 0)
                entry["low"] = tick["ohlc"].get("low", 0)
                entry["close"] = tick["ohlc"].get("close", 0)

            if "depth" in tick:
                entry["depth"] = tick["depth"]

            self.options_live[strike][option_type] = entry

    def recompute_options_aggregate(self, spot_price: Optional[float] = None) -> None:
        """Recompute aggregate metrics from options_live data."""
        with self._lock:
            if not self.options_live:
                return

            total_ce_oi = total_pe_oi = 0
            net_ce_oi_change = net_pe_oi_change = 0
            max_ce_oi = max_pe_oi = 0
            max_ce_strike = max_pe_strike = None

            for strike, data in self.options_live.items():
                ce = data.get("CE", {})
                pe = data.get("PE", {})

                ce_oi = ce.get("oi", 0)
                pe_oi = pe.get("oi", 0)
                total_ce_oi += ce_oi
                total_pe_oi += pe_oi
                net_ce_oi_change += ce_oi - ce.get("prev_oi", 0)
                net_pe_oi_change += pe_oi - pe.get("prev_oi", 0)

                if ce_oi > max_ce_oi:
                    max_ce_oi = ce_oi
                    max_ce_strike = strike
                if pe_oi > max_pe_oi:
                    max_pe_oi = pe_oi
                    max_pe_strike = strike

            agg = self.options_aggregate
            agg["total_ce_oi"] = total_ce_oi
            agg["total_pe_oi"] = total_pe_oi
            agg["live_pcr"] = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 0.0
            agg["max_oi_ce_strike"] = max_ce_strike
            agg["max_oi_pe_strike"] = max_pe_strike
            agg["net_ce_oi_change"] = net_ce_oi_change
            agg["net_pe_oi_change"] = net_pe_oi_change

            if spot_price and self.options_live:
                closest_strike = min(self.options_live.keys(),
                                     key=lambda s: abs(s - spot_price))
                agg["atm_strike"] = closest_strike
                atm_data = self.options_live.get(closest_strike, {})
                agg["atm_straddle_premium"] = (
                    atm_data.get("CE", {}).get("ltp", 0)
                    + atm_data.get("PE", {}).get("ltp", 0)
                )

            agg["last_updated"] = time.time()

    # ------------------------------------------------------------------
    # Futures ticks
    # ------------------------------------------------------------------

    def update_futures_tick(self, expiry_key: str, tick: dict) -> None:
        """Update live futures data from a WebSocket tick."""
        with self._lock:
            entry = self.futures_live.get(expiry_key, {})
            entry["prev_oi"] = entry.get("oi", 0)
            entry["ltp"] = tick.get("last_price", 0)
            entry["oi"] = tick.get("oi", 0)
            entry["volume"] = tick.get("volume_traded", 0)
            entry["buy_qty"] = tick.get("total_buy_quantity", 0)
            entry["sell_qty"] = tick.get("total_sell_quantity", 0)
            entry["change"] = tick.get("change", 0)
            entry["timestamp"] = tick.get("exchange_timestamp")

            if "ohlc" in tick:
                entry["open"] = tick["ohlc"].get("open", 0)
                entry["high"] = tick["ohlc"].get("high", 0)
                entry["low"] = tick["ohlc"].get("low", 0)
                entry["close"] = tick["ohlc"].get("close", 0)

            self.futures_live[expiry_key] = entry
