"""
SensibullAdapter — translates a decoded Sensibull WS option-chain snapshot
into the same TickStore structures that the Zerodha tick path produces.

Two modes (selected by services/market_data/main.py from OPTIONS_SOURCE):

Full-writer mode (enrichment_only=False, OPTIONS_SOURCE=sensibull only):
  options_live[strike][CE/PE]:
    last_price          → "last_price"
    oi                  → "oi"           (open_oi = oi − oi_change_quantity)
    volume              → "volume_traded"
    best_buy_price      → "bid"          (price — NOT a quantity)
    best_sell_price     → "ask"
    best_buy_quantity / best_sell_quantity → buy_qty / sell_qty (0 if absent)
  Aggregates are recomputed with the *index* spot (Zerodha WS1 index tick)
  when available; ``future_price`` is used only as a last-resort fallback.

Enrichment-only mode (enrichment_only=True — whenever Zerodha is the options
source, i.e. OPTIONS_SOURCE=zerodha or both):
  * Only Greeks/IV (delta/gamma/theta/vega/iv/iv_change) are merged into
    existing Zerodha-created strikes (TickStore merge=True).
  * ltp/oi/prev_oi/volume/timestamp/bid/ask are never written.
  * Aggregate enrichment fields (atm_iv, atm_iv_percentile, atm_ivp_type,
    max_pain_strike, future_price, atm_iv_ce/pe, iv_skew) are patched.
  * recompute_options_aggregate / on_aggregate_updated are NOT called —
    Zerodha is the authoritative trigger, and futures price is never used
    as spot.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from lib.logging_util import get_logger
logger = get_logger("fno")

if TYPE_CHECKING:
    from common.Stock import Stock
    from lib.zerodha.live_options_engine import LiveOptionsEngine

# Sensibull key → internal side key
_SIDE_MAP = {"call": "CE", "put": "PE"}


class SensibullAdapter:
    """
    Stateful adapter that converts Sensibull WS snapshots into TickStore updates.

    One instance per process is sufficient; it keeps a ``_prev_oi_cache``
    so that ``prev_oi`` (needed by ``net_oi_change`` computation) can be
    derived across successive snapshots.
    """

    def __init__(self) -> None:
        # { symbol: { strike: { "CE": int, "PE": int } } }
        self._prev_oi_cache: dict[str, dict[float, dict[str, int]]] = {}

    # ── public API ────────────────────────────────────────────────────────────

    def apply(
        self,
        index_stock: "Stock",
        data: dict,
        live_options_engine: "LiveOptionsEngine",
        enrichment_only: bool = False,
    ) -> None:
        """
        Process one Sensibull chain snapshot.

        Normal mode (enrichment_only=False):
          1. Update ``options_live`` for every strike/side (full tick).
          2. Call ``recompute_options_aggregate`` (index spot, futures fallback).
          3. Patch Sensibull-only enrichment fields into ``options_aggregate``.
          4. Call ``live_options_engine.on_aggregate_updated``.

        Enrichment-only mode (enrichment_only=True — Zerodha is options source):
          1. Write only Greeks/IV fields to existing strikes (merge=True —
             skips far-OTM strikes not yet written by Zerodha).
          2. Patch Sensibull-only aggregate fields (atm_iv, ivp, max_pain…).
          3. Does NOT call recompute_options_aggregate or on_aggregate_updated —
             Zerodha is the authoritative tick source and engine trigger.
        """
        symbol = index_stock.stock_symbol
        chain: dict = data.get("chain", {})
        future_price: float = data.get("future_price") or 0.0

        if not chain:
            logger.debug(f"[SensibullAdapter] empty chain for {symbol}, skipping")
            return

        sym_cache = self._prev_oi_cache.setdefault(symbol, {})

        # Collect per-strike IV from greeks while iterating the chain.
        # Sensibull provides one IV per strike (not separate CE/PE IVs).
        # { strike_float: iv_decimal }
        strike_iv: dict[float, float] = {}

        # ── 1. Push per-leg ticks ─────────────────────────────────────────────
        for strike_str, strike_data in chain.items():
            try:
                strike = float(strike_str)
            except ValueError:
                continue

            strike_cache = sym_cache.setdefault(strike, {})

            # Greeks and iv_change are per-strike (not per-leg) in Sensibull.
            # call_delta is the CE delta; put delta = call_delta - 1.
            greeks: dict = strike_data.get("greeks", {})
            call_delta: float    = greeks.get("call_delta", 0.0) or 0.0
            gamma: float         = greeks.get("gamma", 0.0) or 0.0
            theta: float         = greeks.get("theta", 0.0) or 0.0
            vega: float          = greeks.get("vega", 0.0) or 0.0
            strike_iv_val: float = float(greeks.get("iv") or 0.0)
            iv_change: float     = float(strike_data.get("iv_change") or 0.0)

            # Populate strike_iv for ATM IV derivation later
            if strike_iv_val:
                strike_iv[strike] = strike_iv_val

            # Snapshot prev_oi BEFORE updating the cache so _patch_prev_oi
            # receives the *old* values, not the current ones.
            prev_oi_snapshot: dict[str, int] = {}

            for sb_side, internal_side in _SIDE_MAP.items():
                leg: dict | None = strike_data.get(sb_side)
                if not leg:
                    continue

                current_oi: int = leg.get("oi") or 0
                oi_change_qty: int = leg.get("oi_change_quantity") or 0

                # Derive prev_oi: first call uses oi_change_quantity from Sensibull
                # (absolute change since day-open); subsequent calls use cache
                if internal_side in strike_cache:
                    prev_oi = strike_cache[internal_side]
                else:
                    prev_oi = current_oi - oi_change_qty

                prev_oi_snapshot[internal_side] = prev_oi  # capture BEFORE overwrite
                strike_cache[internal_side] = current_oi

                delta = call_delta if internal_side == "CE" else (call_delta - 1.0)

                if enrichment_only:
                    # Only write Greeks/IV — never touch ltp/oi/prev_oi/volume/
                    # timestamp/bid/ask.  merge=True silently skips strikes not
                    # yet in options_live (outside Zerodha's subscription zone).
                    greek_tick = {
                        "delta":     delta,
                        "gamma":     gamma,
                        "theta":     theta,
                        "vega":      vega,
                        "iv":        strike_iv_val,
                        "iv_change": iv_change,
                    }
                    index_stock.update_option_tick(strike, internal_side, greek_tick, merge=True)
                else:
                    tick = {
                        "last_price":           leg.get("last_price") or 0,
                        "oi":                   current_oi,
                        # Day-open OI baseline (Sensibull oi_change_quantity is
                        # the absolute change since day open).
                        "open_oi":              current_oi - oi_change_qty,
                        "volume_traded":        leg.get("volume") or 0,
                        "total_buy_quantity":   leg.get("best_buy_quantity") or 0,
                        "total_sell_quantity":  leg.get("best_sell_quantity") or 0,
                        "bid":      leg.get("best_buy_price") or 0,
                        "ask":      leg.get("best_sell_price") or 0,
                        "delta":    delta,
                        "gamma":    gamma,
                        "theta":    theta,
                        "vega":     vega,
                        "iv":       strike_iv_val,
                        "iv_change": iv_change,
                    }
                    index_stock.update_option_tick(strike, internal_side, tick)

            # Full-writer mode only: patch prev_oi using the snapshot taken
            # before cache update (first call → day-open baseline).  Never in
            # enrichment mode — Zerodha owns prev_oi there.
            if not enrichment_only:
                _patch_prev_oi(index_stock, strike, prev_oi_snapshot)

        # ── 2. Recompute aggregate (PCR, ATM strike, straddle, OI walls) ──────
        # Skipped in enrichment_only mode — Zerodha owns the aggregate trigger.
        # Use the real index spot; futures price only as a fallback.
        engine_spot = 0.0
        if not enrichment_only:
            engine_spot = _index_spot(index_stock) or (future_price if future_price > 0 else 0.0)
            index_stock.recompute_options_aggregate(engine_spot or None)

        # ── 3. Patch Sensibull-only enrichment fields ─────────────────────────
        # Always patched regardless of mode — Zerodha never writes these fields.
        ts = index_stock._tick_store
        patch: dict = {
            "atm_iv":            data.get("atm_iv", 0.0) or 0.0,
            "atm_iv_percentile": data.get("atm_iv_percentile", 0.0) or 0.0,
            "atm_ivp_type":      data.get("atm_ivp_type"),
            "max_pain_strike":   data.get("max_pain_strike"),
            "future_price":      future_price,
        }
        agg = ts.options_aggregate

        # ── 3a. Populate atm_iv_ce, atm_iv_pe, iv_skew from per-strike IVs ───
        # Sensibull gives one IV per strike in greeks.iv.  We use the nearest
        # OTM call (first strike > ATM) as atm_iv_ce and nearest OTM put
        # (first strike < ATM) as atm_iv_pe.
        # iv_skew = (pe_iv - ce_iv) * 100  — positive means put-buying/fear.
        atm_strike = agg.get("atm_strike")
        if atm_strike and strike_iv:
            sorted_strikes = sorted(strike_iv)
            atm_idx = None
            for i, s in enumerate(sorted_strikes):
                if s >= atm_strike:
                    atm_idx = i
                    break
            if atm_idx is not None:
                # nearest OTM call: atm_idx or atm_idx+1 (first >= ATM)
                ce_strike = sorted_strikes[atm_idx] if sorted_strikes[atm_idx] >= atm_strike else None
                if ce_strike is None and atm_idx + 1 < len(sorted_strikes):
                    ce_strike = sorted_strikes[atm_idx + 1]
                # nearest OTM put: first strike < ATM
                pe_strike = sorted_strikes[atm_idx - 1] if atm_idx > 0 else None

                ce_iv = strike_iv.get(ce_strike, 0.0) if ce_strike else 0.0
                pe_iv = strike_iv.get(pe_strike, 0.0) if pe_strike else 0.0

                patch["atm_iv_ce"] = ce_iv
                patch["atm_iv_pe"] = pe_iv
                # skew in percentage-point terms (matching context_builder convention)
                if ce_iv > 0 and pe_iv > 0:
                    patch["iv_skew"] = (pe_iv - ce_iv) * 100

        ts.patch_options_aggregate(patch)

        # ── 4. Notify the live options engine ─────────────────────────────────
        # Skipped in enrichment_only mode — Zerodha ticks are the authoritative
        # engine trigger; Sensibull is a passive enrichment source only.
        if live_options_engine and not enrichment_only:
            live_options_engine.on_aggregate_updated(index_stock, engine_spot)

        logger.debug(
            f"[SensibullAdapter] {symbol} {'enriched' if enrichment_only else 'updated'} — "
            f"strikes={len(chain)}, future_price={future_price}, "
            f"atm_iv={agg.get('atm_iv', 0):.3f}, ivp={agg.get('atm_iv_percentile', 0):.1f}"
        )


# ── helpers ───────────────────────────────────────────────────────────────────

def _index_spot(index_stock: "Stock") -> float:
    """Underlying index spot from the Zerodha WS1 index tick (0.0 if absent)."""
    try:
        px = index_stock._tick_store._zerodha_data.get("last_price") or 0.0
        if not px:
            px = getattr(index_stock, "ltp", 0.0) or 0.0
        return float(px) if px and px > 0 else 0.0
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _patch_prev_oi(
    index_stock: "Stock",
    strike: float,
    strike_cache: dict[str, int],
) -> None:
    """
    TickStore.update_option_tick sets prev_oi = entry.get("oi", 0) from the
    *previous* call.  After the first snapshot the cache holds the true previous
    OI value, so we write it back directly into options_live.

    This is safe because options_live is a plain dict and the lock is not held
    outside of TickStore methods — we call this immediately after update_option_tick
    in the same thread.
    """
    live = index_stock._tick_store.options_live.get(strike)
    if not live:
        return
    for side, prev_oi in strike_cache.items():
        entry = live.get(side)
        if entry:
            entry["prev_oi"] = prev_oi
