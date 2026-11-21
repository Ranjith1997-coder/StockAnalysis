"""
Paper Trading — SPAN Margin Calculator

Real SPAN margin calculation via Zerodha's public margin-calculator API, plus
the instruments cache and scrip-code derivation it depends on. See
docs/PAPER_TRADING_DESIGN.md section 4 for the full spec.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional

import requests

from lib.logging_util import get_logger
logger = get_logger("paper-trading")
from services.paper_trading.models import (
    INSTRUMENTS_CACHE_TTL_SECONDS,
    INSTRUMENTS_FETCHED_AT_KEY,
    instruments_key,
)

API_URL = "https://zerodha.com/margin-calculator/SPAN"
CACHE_TTL_SECONDS = 300           # 5 minutes
MIN_CALL_INTERVAL_SECONDS = 0.5   # rate-limit courtesy delay between live calls
REQUEST_TIMEOUT = 5
# Min gap between Zerodha instrument refresh attempts (stale cache or miss).
INSTRUMENTS_REFRESH_MIN_INTERVAL_SECONDS = 300


def derive_scrip(tradingsymbol: str, strike: float, option_type: str) -> str:
    """Strip strike + option_type suffix from tradingsymbol to get the SPAN scrip code.

    e.g. "NIFTY2672124050CE" -> "NIFTY26721", "NIFTY26JUL24050CE" -> "NIFTY26JUL".

    The regex fallback below is defensive-only: the sole call site
    (_build_form_data) always passes strike/option_type from the same
    instrument entry the tradingsymbol came from, so the suffix always
    matches there. If ever called with a mismatched strike, digits alone
    can't disambiguate the expiry/strike boundary, so the fallback
    deliberately degrades to just the alpha prefix (e.g. "NIFTY") rather
    than guess -- that's not a valid scrip on its own, so the SPAN API
    safely rejects it (empty response -> None -> skip trade) instead of
    silently using a wrong-but-plausible-looking scrip.
    """
    suffix = str(int(strike)) + option_type  # "24050CE"
    if tradingsymbol.endswith(suffix):
        return tradingsymbol[: -len(suffix)]
    match = re.match(r"^(.+?)(\d+)(CE|PE)$", tradingsymbol)
    if match:
        return match.group(1)
    return tradingsymbol


@dataclass(frozen=True)
class InstrumentEntry:
    strike: float
    option_type: str        # "CE" | "PE"
    tradingsymbol: str
    lot_size: int
    exchange: str            # "NFO" | "BFO"


class SpanLegRequest(NamedTuple):
    """One leg of a (possibly multi-leg) SPAN margin request."""
    symbol: str
    expiry: str              # ISO date, e.g. "2026-07-21"
    strike: float
    option_type: str          # "CE" | "PE"
    qty: int
    trade: str                 # "sell" | "buy"


def _expiry_str(expiry) -> str:
    """Instruments from KiteConnect.instruments() parse expiry into a date object."""
    if hasattr(expiry, "isoformat"):
        return expiry.isoformat()
    return str(expiry)


def build_instruments_cache(raw_instruments: list[dict], symbols: list[str]) -> dict:
    """
    Build the nested instruments cache from KiteConnect.instruments() rows.

    Returns: {symbol: {expiry: [InstrumentEntry, ...]}}
    """
    cache: dict = {}
    symbol_set = set(symbols)
    for row in raw_instruments:
        name = row.get("name")
        if name not in symbol_set:
            continue
        instrument_type = row.get("instrument_type")
        if instrument_type not in ("CE", "PE"):
            continue
        expiry = _expiry_str(row.get("expiry"))
        entry = InstrumentEntry(
            strike=float(row["strike"]),
            option_type=instrument_type,
            tradingsymbol=row["tradingsymbol"],
            lot_size=int(row["lot_size"]),
            exchange=row["exchange"],
        )
        cache.setdefault(name, {}).setdefault(expiry, []).append(entry)
    return cache


def serialize_instruments_cache(cache: dict) -> dict[str, dict]:
    """Per-symbol Redis hash mapping: {expiry: json list of entries} (paper:instruments:{symbol})."""
    result: dict[str, dict] = {}
    for symbol, by_expiry in cache.items():
        result[symbol] = {
            expiry: json.dumps([entry.__dict__ for entry in entries])
            for expiry, entries in by_expiry.items()
        }
    return result


def deserialize_instruments_cache(mapping_by_symbol: dict[str, dict]) -> dict:
    """Inverse of serialize_instruments_cache()."""
    cache: dict = {}
    for symbol, by_expiry in mapping_by_symbol.items():
        cache[symbol] = {
            expiry: [InstrumentEntry(**e) for e in json.loads(raw)]
            for expiry, raw in by_expiry.items()
        }
    return cache


def save_instruments_cache(redis, cache: dict, ttl_seconds: int = INSTRUMENTS_CACHE_TTL_SECONDS) -> None:
    """Replace each symbol's hash (drops expired expiries) and apply the TTL
    so a stale copy can never be served forever."""
    for symbol, mapping in serialize_instruments_cache(cache).items():
        if mapping:
            key = instruments_key(symbol)
            redis.delete(key)
            redis.hset(key, mapping=mapping)
            redis.expire(key, ttl_seconds)
    redis.set_with_ttl(INSTRUMENTS_FETCHED_AT_KEY, str(time.time()), ex=ttl_seconds)


def load_instruments_fetched_at(redis) -> float:
    """Epoch seconds the Redis instruments copy was fetched (0.0 if unknown)."""
    try:
        return float(redis.get(INSTRUMENTS_FETCHED_AT_KEY) or 0)
    except (TypeError, ValueError):
        return 0.0


def load_instruments_cache(redis, symbols: list[str]) -> dict:
    cache: dict = {}
    for symbol in symbols:
        mapping = redis.hgetall(instruments_key(symbol))
        if mapping:
            cache.update(deserialize_instruments_cache({symbol: mapping}))
    return cache


class SpanCalculator:
    """Real SPAN margin calculation via Zerodha's public margin-calculator API."""

    def __init__(self, instruments: dict, refresher: Optional[Callable[[], Optional[dict]]] = None,
                 loaded_at: Optional[float] = None, ttl_seconds: float = INSTRUMENTS_CACHE_TTL_SECONDS):
        self._instruments = instruments   # {symbol: {expiry: [InstrumentEntry, ...]}}
        self._cache: dict[str, float] = {}
        self._cache_times: dict[str, float] = {}
        self._last_call_time = 0.0
        self._refresher = refresher       # fetches a fresh cache from Zerodha (None = disabled)
        self._loaded_at = loaded_at if loaded_at is not None else time.time()
        self._ttl_seconds = ttl_seconds
        self._last_refresh_attempt = 0.0

    def set_instruments(self, instruments: dict, loaded_at: Optional[float] = None) -> None:
        self._instruments = instruments
        self._loaded_at = loaded_at if loaded_at is not None else time.time()

    def maybe_refresh(self, symbol: Optional[str] = None, expiry: Optional[str] = None,
                      now: Optional[float] = None) -> bool:
        """Refresh from Zerodha when the cache is older than the TTL or the
        requested symbol/expiry is missing (new weekly listed, lot size
        revision).  Rate-limited; returns True if instruments were replaced."""
        if self._refresher is None:
            return False
        now = now if now is not None else time.time()
        stale = (now - self._loaded_at) > self._ttl_seconds
        missing = bool(symbol and expiry and not self._instruments.get(symbol, {}).get(expiry))
        if not (stale or missing):
            return False
        if now - self._last_refresh_attempt < INSTRUMENTS_REFRESH_MIN_INTERVAL_SECONDS:
            logger.debug(f"[SpanCalculator] {symbol} | SOURCE expiry={expiry} stale={stale} "
                         f"missing={missing} — refresh rate-limited")
            return False
        self._last_refresh_attempt = now
        fresh = self._refresher()
        if not fresh:
            logger.warning(f"[SpanCalculator] Instruments refresh failed (stale={stale} missing={missing})")
            return False
        self.set_instruments(fresh, loaded_at=now)
        logger.info(f"[SpanCalculator] Instruments refreshed (stale={stale} missing={symbol}/{expiry})")
        return True

    def get_lot_size(self, symbol: str, expiry: str) -> Optional[int]:
        """All options for a given symbol+expiry share the same lot size."""
        self.maybe_refresh(symbol, expiry)
        entries = self._instruments.get(symbol, {}).get(expiry, [])
        return entries[0].lot_size if entries else None

    def get_scrip(self, symbol: str, expiry: str) -> Optional[str]:
        """All legs of a position share (symbol, expiry) so they share one scrip code."""
        entries = self._instruments.get(symbol, {}).get(expiry, [])
        if not entries:
            return None
        entry = entries[0]
        return derive_scrip(entry.tradingsymbol, entry.strike, entry.option_type)

    def _resolve_leg(self, leg: SpanLegRequest) -> Optional[InstrumentEntry]:
        by_expiry = self._instruments.get(leg.symbol, {})
        entries = by_expiry.get(leg.expiry, [])
        for entry in entries:
            if entry.strike == leg.strike and entry.option_type == leg.option_type:
                return entry
        return None

    def _make_cache_key(self, legs: list[SpanLegRequest]) -> str:
        parts = sorted(
            f"{leg.symbol}:{leg.expiry}:{leg.strike}:{leg.option_type}:{leg.qty}:{leg.trade}"
            for leg in legs
        )
        return "|".join(parts)

    def _is_cached(self, cache_key: str) -> bool:
        cached_at = self._cache_times.get(cache_key)
        if cached_at is None:
            return False
        return (time.time() - cached_at) < CACHE_TTL_SECONDS

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_call_time
        if elapsed < MIN_CALL_INTERVAL_SECONDS:
            time.sleep(MIN_CALL_INTERVAL_SECONDS - elapsed)

    def _build_form_data(self, legs: list[SpanLegRequest]) -> Optional[list[tuple[str, str]]]:
        """Repeated-array form data. Returns None if any leg can't be resolved to an instrument."""
        data: list[tuple[str, str]] = [("action", "calculate")]
        for leg in legs:
            entry = self._resolve_leg(leg)
            if entry is None:
                logger.warning(
                    f"[SpanCalculator] Could not resolve instrument for "
                    f"{leg.symbol} {leg.expiry} {leg.strike}{leg.option_type}"
                )
                return None
            scrip = derive_scrip(entry.tradingsymbol, entry.strike, entry.option_type)
            data.append(("exchange[]", entry.exchange))
            data.append(("product[]", "OPT"))
            data.append(("scrip[]", scrip))
            data.append(("option_type[]", leg.option_type))
            data.append(("strike_price[]", str(int(leg.strike))))
            data.append(("qty[]", str(leg.qty)))
            data.append(("trade[]", leg.trade))
        return data

    def calculate_margin(self, legs: list[SpanLegRequest]) -> Optional[float]:
        """Calculate total SPAN margin for a (possibly multi-leg) position.

        Returns total margin in rupees, or None if the API is unreachable,
        an instrument couldn't be resolved, or the scrip is invalid.
        """
        if not legs:
            return None

        cache_key = self._make_cache_key(legs)
        if self._is_cached(cache_key):
            return self._cache[cache_key]

        form_data = self._build_form_data(legs)
        if form_data is None:
            return None

        self._throttle()
        try:
            resp = requests.post(
                API_URL,
                data=form_data,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://zerodha.com/margin-calculator/SPAN/",
                },
                timeout=REQUEST_TIMEOUT,
            )
            self._last_call_time = time.time()
            result = resp.json()
        except Exception as e:
            logger.error(f"[SpanCalculator] SPAN API call failed: {e}")
            return None

        total = result.get("total", {})
        if isinstance(total, list) or not total:
            logger.warning("[SpanCalculator] SPAN API returned empty/invalid scrip response")
            return None

        try:
            margin = float(total["total"])
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"[SpanCalculator] Unexpected SPAN response shape: {e}")
            return None

        self._cache[cache_key] = margin
        self._cache_times[cache_key] = time.time()
        return margin
