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
from datetime import date, datetime
from typing import Optional

from lib.logging_util import get_logger
logger = get_logger("zerodha")


class TickStore:
    """Thread-safe container for live WebSocket tick data."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Tick counters — incremented on each WS update, read by snapshot publisher
        self.tick_count = 0
        self.option_tick_count = 0

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
            "timestamp": 0.0,
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
            # OI change since session open (sum over strikes of oi - open_oi).
            # open_oi = first non-zero OI seen for the strike on the trading day
            # (or Sensibull's day-open baseline oi - oi_change_quantity).
            "net_ce_oi_change": 0,
            "net_pe_oi_change": 0,
            # Tick-to-tick OI change (sum of oi - prev_oi) — legacy semantics.
            "net_ce_oi_change_tick": 0,
            "net_pe_oi_change_tick": 0,
            # PCR/OI totals only include strikes with OI > 0. Peripheral strikes
            # are subscribed in MODE_QUOTE (no OI) so the effective window is
            # ~ATM±3%. pcr_window_pct = max |strike-spot|/spot*100 over the
            # strikes that actually contributed OI; pcr_strikes = their count.
            "pcr_window_pct": 0.0,
            "pcr_strikes": 0,
            # Expiry (YYYY-MM-DD) of the live chain — set at registration time.
            "expiry": None,
            "last_updated": 0.0,
            # Sensibull WS enrichment — populated when OPTIONS_SOURCE=sensibull;
            # remain at default (0.0 / None) in Zerodha mode.
            "atm_iv": 0.0,
            "atm_iv_percentile": 0.0,
            "atm_ivp_type": None,
            "max_pain_strike": None,
            "future_price": 0.0,
            # GEX (Gamma Exposure) — computed by GEXAnalyser every 5-min cycle.
            # Requires gamma from Sensibull; stays 0.0 in Zerodha-only mode.
            "gex_total": 0.0,       # Net GEX in ₹ crores (CE - PE)
            "gex_ce": 0.0,          # CE-side GEX contribution
            "gex_pe": 0.0,          # PE-side GEX contribution
            "gex_regime": None,     # "POSITIVE" | "NEGATIVE" | None (not yet computed)
            "gex_flip_level": None, # Strike where cumulative GEX crosses zero
            "gex_by_strike": {},    # {strike: net_gex} — used by GEX_WALL_BREACH next cycle
        }

        # Live futures tick data from WebSocket
        # { "current": {ltp, oi, volume, …}, "next": {…} }
        self.futures_live: dict = {}

        # Trading date for which per-strike ``open_oi`` baselines are valid.
        self._oi_session_date: Optional[date] = None

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
            self.tick_count += 1
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

            d["timestamp"] = time.time()
        logger.debug("[TickStore] equity #%d: price=%.2f", self.tick_count, d.get("last_price", 0))

    # ------------------------------------------------------------------
    # Options ticks
    # ------------------------------------------------------------------

    # Fields Sensibull may write in enrichment-only mode (Zerodha is the
    # authoritative writer of ltp/oi/prev_oi/volume/timestamp/bid/ask).
    ENRICHMENT_FIELDS = frozenset({"delta", "gamma", "theta", "vega", "iv", "iv_change"})

    def _roll_oi_session(self) -> None:
        """Reset per-strike ``open_oi`` baselines on a new trading day. Caller holds lock."""
        today = date.today()
        if self._oi_session_date == today:
            return
        if self._oi_session_date is not None:
            for sides in self.options_live.values():
                for entry in sides.values():
                    entry.pop("open_oi", None)
        self._oi_session_date = today

    @staticmethod
    def _best_price(depth: dict, side: str) -> Optional[float]:
        """Best (level-0) price from a Zerodha depth dict; None when absent/zero."""
        try:
            levels = depth.get(side) or []
            price = float(levels[0].get("price", 0) or 0) if levels else 0.0
        except (AttributeError, TypeError, ValueError, IndexError):
            return None
        return price if price > 0 else None

    def update_option_tick(self, strike: float, option_type: str, tick: dict, merge: bool = False) -> None:
        """Update live option data from a WebSocket tick.

        Args:
            merge: When True (enrichment-only mode), only writes the Greeks/IV
                   keys in ``ENRICHMENT_FIELDS`` — ltp/oi/prev_oi/volume/
                   timestamp/bid/ask are never touched.  The strike must already
                   exist in options_live — if not, the update is silently skipped
                   (Zerodha is the authoritative creator).  Used by Sensibull
                   whenever Zerodha is the options source.

        Per-strike fields written in full-writer mode:
            ltp, oi, prev_oi (tick-to-tick), open_oi (session baseline),
            has_oi (False for MODE_QUOTE ticks, which carry no OI), volume,
            buy_qty/sell_qty (total quantities), bid/ask (best prices from
            Zerodha depth level 0, or Sensibull best_buy/sell_price), timestamp.
        """
        with self._lock:
            if merge:
                # Enrichment-only: only update existing strikes (Zerodha-subscribed)
                existing = self.options_live.get(strike, {}).get(option_type)
                if existing is None:
                    logger.debug(f"[TickStore] Enrichment skip: strike {strike} {option_type} not in options_live (Zerodha not yet subscribed)")
                    return
                existing.update({k: v for k, v in tick.items() if k in self.ENRICHMENT_FIELDS})
                return

            self.option_tick_count += 1
            self._roll_oi_session()

            if strike not in self.options_live:
                self.options_live[strike] = {}

            entry = self.options_live[strike].get(option_type, {})
            entry["ltp"] = tick.get("last_price", 0)
            if "oi" in tick:
                oi = tick.get("oi") or 0
                entry["prev_oi"] = entry.get("oi", 0)
                entry["oi"] = oi
                entry["has_oi"] = True
                baseline = tick.get("open_oi")
                if baseline is not None:
                    entry["open_oi"] = baseline
                elif entry.get("open_oi") is None and oi > 0:
                    # First OI seen this trading day.  Zerodha ticks carry no
                    # prev-day close OI (oi_day_low/high are intraday extremes),
                    # so after a mid-session restart this is the restart-time OI.
                    entry["open_oi"] = oi
            else:
                # MODE_QUOTE tick: no OI in packet — keep last known OI, flag it.
                entry.setdefault("oi", 0)
                entry["prev_oi"] = entry["oi"]
                entry["has_oi"] = False
            entry["volume"] = tick.get("volume_traded", 0)
            entry["buy_qty"] = tick.get("total_buy_quantity", 0)
            entry["sell_qty"] = tick.get("total_sell_quantity", 0)
            entry["timestamp"] = tick.get("exchange_timestamp") or datetime.now()

            # Best bid/ask prices: explicit keys (Sensibull) win, else depth level 0.
            bid = tick.get("bid")
            ask = tick.get("ask")
            depth = tick.get("depth")
            if isinstance(depth, dict):
                if bid is None:
                    bid = self._best_price(depth, "buy")
                if ask is None:
                    ask = self._best_price(depth, "sell")
            if bid is not None:
                entry["bid"] = bid
            if ask is not None:
                entry["ask"] = ask

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
        logger.debug("[TickStore] option #%d %s strike=%.0f ltp=%.2f oi=%d",
                     self.option_tick_count, option_type, strike,
                     entry.get("ltp", 0), entry.get("oi", 0))

    def patch_options_aggregate(self, fields: dict) -> None:
        """Thread-safe patch of enrichment fields into options_aggregate."""
        with self._lock:
            self.options_aggregate.update(fields)

    def set_options_expiry(self, expiry: Optional[str]) -> None:
        """Record which expiry the live chain belongs to (published in options_agg)."""
        with self._lock:
            self.options_aggregate["expiry"] = expiry

    def recompute_options_aggregate(self, spot_price: Optional[float] = None) -> None:
        """Recompute aggregate metrics from options_live data.

        Only legs with OI > 0 contribute to totals/PCR/walls: MODE_QUOTE
        (peripheral) legs carry no OI and must not dilute or masquerade as a
        complete chain.  ``pcr_window_pct`` reports the effective window.

        ``net_*_oi_change`` = Σ(oi − open_oi) since session open (legs with a
        known baseline only); ``net_*_oi_change_tick`` = Σ(oi − prev_oi).
        """
        with self._lock:
            if not self.options_live:
                return

            total_ce_oi = total_pe_oi = 0
            net_ce_oi_change = net_pe_oi_change = 0
            net_ce_tick = net_pe_tick = 0
            max_ce_oi = max_pe_oi = 0
            max_ce_strike = max_pe_strike = None
            oi_strikes: set = set()

            for strike, data in self.options_live.items():
                ce = data.get("CE", {})
                pe = data.get("PE", {})

                ce_oi = ce.get("oi", 0) or 0
                pe_oi = pe.get("oi", 0) or 0
                if ce_oi > 0:
                    oi_strikes.add(strike)
                    total_ce_oi += ce_oi
                    net_ce_tick += ce_oi - (ce.get("prev_oi", 0) or 0)
                    if ce.get("open_oi") is not None:
                        net_ce_oi_change += ce_oi - ce["open_oi"]
                if pe_oi > 0:
                    oi_strikes.add(strike)
                    total_pe_oi += pe_oi
                    net_pe_tick += pe_oi - (pe.get("prev_oi", 0) or 0)
                    if pe.get("open_oi") is not None:
                        net_pe_oi_change += pe_oi - pe["open_oi"]

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
            agg["net_ce_oi_change_tick"] = net_ce_tick
            agg["net_pe_oi_change_tick"] = net_pe_tick
            agg["pcr_strikes"] = len(oi_strikes)
            if spot_price and spot_price > 0 and oi_strikes:
                agg["pcr_window_pct"] = round(
                    max(abs(s - spot_price) for s in oi_strikes) / spot_price * 100, 2
                )

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
        logger.debug("[TickStore] aggregate: strikes=%d pcr=%.3f atm=%s straddle=%.1f",
                     len(self.options_live), agg.get("live_pcr", 0),
                     agg.get("atm_strike"), agg.get("atm_straddle_premium", 0))

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

            # 5-level depth (full-mode ticks). Order-flow features need it.
            if "depth" in tick:
                entry["depth"] = tick["depth"]

            self.futures_live[expiry_key] = entry
        logger.debug("[TickStore] futures %s ltp=%.2f oi=%d", expiry_key,
                     entry.get("ltp", 0), entry.get("oi", 0))
