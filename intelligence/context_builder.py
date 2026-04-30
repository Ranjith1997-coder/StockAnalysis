"""
ContextBuilder — gathers a real-time market snapshot for LLM prompts.

Pulls data from Stock objects in shared.app_ctx to build a structured
MarketContext that the narrator injects into the prompt template.

Two build paths:
  build(symbol)        → MarketContext        (equities)
  build_index(symbol)  → IndexMarketContext   (NIFTY/BANKNIFTY — richer data)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Tuple

import common.shared as shared


@dataclass
class MarketContext:
    symbol: str
    spot: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    day_open: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    vwap: float | None = None

    # Options data
    pcr: float | None = None
    max_pain: float | None = None
    ce_oi_wall: float | None = None
    pe_oi_wall: float | None = None
    atm_strike: float | None = None
    atm_straddle: float | None = None
    total_ce_oi: float | None = None
    total_pe_oi: float | None = None

    # Volatility
    vix: float | None = None

    # Time
    expiry: str | None = None
    minutes_to_close: int | None = None

    def to_prompt_block(self) -> str:
        """Format as a readable block for the LLM prompt."""
        lines = [f"Symbol: {self.symbol}"]

        if self.spot:
            lines.append(f"Spot: {self.spot:.2f}")
        if self.day_high and self.day_low:
            lines.append(f"Day range: {self.day_low:.2f} - {self.day_high:.2f}")
        if self.day_open:
            lines.append(f"Day open: {self.day_open:.2f}")
        if self.prev_close:
            lines.append(f"Prev close: {self.prev_close:.2f}")
        if self.change_pct is not None:
            lines.append(f"Change: {self.change_pct:+.2f}%")
        if self.vwap:
            lines.append(f"VWAP: {self.vwap:.2f}")

        if self.vix is not None:
            label = "elevated" if self.vix > 16 else "low" if self.vix < 13 else "normal"
            lines.append(f"India VIX: {self.vix:.2f} ({label})")

        if self.pcr:
            lines.append(f"PCR: {self.pcr:.3f}")
        if self.max_pain:
            lines.append(f"Max pain: {self.max_pain:.0f}")
        if self.ce_oi_wall:
            lines.append(f"CE OI wall (resistance): {self.ce_oi_wall:.0f}")
        if self.pe_oi_wall:
            lines.append(f"PE OI wall (support): {self.pe_oi_wall:.0f}")
        if self.atm_strike:
            lines.append(f"ATM strike: {self.atm_strike:.0f}")
        if self.atm_straddle:
            lines.append(f"ATM straddle premium: {self.atm_straddle:.2f}")
        if self.total_ce_oi and self.total_pe_oi:
            lines.append(f"Total CE OI: {self.total_ce_oi:,.0f} | Total PE OI: {self.total_pe_oi:,.0f}")

        if self.expiry:
            lines.append(f"Weekly expiry: {self.expiry}")
        if self.minutes_to_close is not None:
            lines.append(f"Minutes to market close: {self.minutes_to_close}")

        return "\n".join(lines)


@dataclass
class IndexMarketContext(MarketContext):
    """
    Extended context for index confluences (NIFTY / BANKNIFTY).
    Adds volatility structure, OI flow, futures positioning, and
    previous day price levels — all absent from the base MarketContext.
    """

    # Previous day levels (support/resistance baseline)
    prev_day_high: float | None = None
    prev_day_low: float | None = None

    # Volatility structure
    atm_iv_ce: float | None = None       # ATM call implied volatility
    atm_iv_pe: float | None = None       # ATM put implied volatility
    iv_skew: float | None = None         # PE IV - CE IV (positive = fear/put buying)
    atm_iv_percentile: float | None = None  # Where current IV sits in 30-day range (0-100)

    # OI flow (is OI building or unwinding?)
    net_ce_oi_change: int | None = None  # +ve = CE writers adding, -ve = covering
    net_pe_oi_change: int | None = None  # +ve = PE writers adding, -ve = covering
    oi_change_type: str | None = None    # Sensibull classification: "Long Buildup" etc.
    volume_spike_type: str | None = None # "High", "Very High", "Normal" etc.

    # Top OI concentration strikes (resistance / support clusters)
    top_ce_strikes: list[Tuple[float, int]] = field(default_factory=list)  # [(strike, oi), ...]
    top_pe_strikes: list[Tuple[float, int]] = field(default_factory=list)

    # Futures positioning
    futures_ltp: float | None = None
    futures_oi: int | None = None
    futures_change_pct: float | None = None  # Futures % change vs prev close
    futures_buy_qty: int | None = None
    futures_sell_qty: int | None = None

    def to_prompt_block(self) -> str:
        """Extended prompt block: base fields + index-specific sections."""
        lines = [super().to_prompt_block()]

        # Previous day levels
        prev_day_lines = []
        if self.prev_day_high:
            prev_day_lines.append(f"  Prev day high: {self.prev_day_high:.2f}")
        if self.prev_day_low:
            prev_day_lines.append(f"  Prev day low: {self.prev_day_low:.2f}")
        if prev_day_lines:
            lines.append("\n--- Previous Day Levels ---")
            lines.extend(prev_day_lines)

        # Volatility structure
        iv_lines = []
        if self.atm_iv_ce is not None:
            iv_lines.append(f"  ATM CE IV: {self.atm_iv_ce:.2f}%")
        if self.atm_iv_pe is not None:
            iv_lines.append(f"  ATM PE IV: {self.atm_iv_pe:.2f}%")
        if self.iv_skew is not None:
            skew_bias = "put-heavy (bearish fear)" if self.iv_skew > 0.5 else \
                        "call-heavy (complacency)" if self.iv_skew < -0.5 else "neutral"
            iv_lines.append(f"  IV skew (PE-CE): {self.iv_skew:+.2f}% ({skew_bias})")
        if self.atm_iv_percentile is not None:
            iv_regime = "expensive — prefer selling" if self.atm_iv_percentile > 70 else \
                        "cheap — prefer buying" if self.atm_iv_percentile < 30 else "moderate"
            iv_lines.append(f"  IV percentile: {self.atm_iv_percentile:.0f} ({iv_regime})")
        if iv_lines:
            lines.append("\n--- Volatility Structure ---")
            lines.extend(iv_lines)

        # OI flow
        oi_lines = []
        if self.net_ce_oi_change is not None:
            direction = "building" if self.net_ce_oi_change > 0 else "unwinding"
            oi_lines.append(f"  CE OI change: {self.net_ce_oi_change:+,} ({direction})")
        if self.net_pe_oi_change is not None:
            direction = "building" if self.net_pe_oi_change > 0 else "unwinding"
            oi_lines.append(f"  PE OI change: {self.net_pe_oi_change:+,} ({direction})")
        if self.oi_change_type:
            oi_lines.append(f"  OI change type: {self.oi_change_type}")
        if self.volume_spike_type:
            oi_lines.append(f"  Volume spike: {self.volume_spike_type}")
        if oi_lines:
            lines.append("\n--- OI Flow ---")
            lines.extend(oi_lines)

        # Top OI concentration strikes
        if self.top_ce_strikes:
            strikes_str = " | ".join(
                f"{s:.0f} ({oi:,})" for s, oi in self.top_ce_strikes
            )
            lines.append(f"\n--- Top CE OI Walls (resistance) ---")
            lines.append(f"  {strikes_str}")
        if self.top_pe_strikes:
            strikes_str = " | ".join(
                f"{s:.0f} ({oi:,})" for s, oi in self.top_pe_strikes
            )
            lines.append(f"\n--- Top PE OI Walls (support) ---")
            lines.append(f"  {strikes_str}")

        # Futures positioning
        fut_lines = []
        if self.futures_ltp:
            fut_lines.append(f"  Futures LTP: {self.futures_ltp:.2f}")
        if self.futures_change_pct is not None:
            fut_lines.append(f"  Futures change: {self.futures_change_pct:+.2f}%")
        if self.futures_oi:
            fut_lines.append(f"  Futures OI: {self.futures_oi:,}")
        if self.futures_buy_qty and self.futures_sell_qty:
            total = self.futures_buy_qty + self.futures_sell_qty
            if total > 0:
                buy_pct = self.futures_buy_qty / total * 100
                bias = "buy-heavy" if buy_pct > 55 else "sell-heavy" if buy_pct < 45 else "balanced"
                fut_lines.append(
                    f"  Futures order flow: {bias} "
                    f"(buy {buy_pct:.0f}% / sell {100 - buy_pct:.0f}%)"
                )
        if fut_lines:
            lines.append("\n--- Futures Positioning ---")
            lines.extend(fut_lines)

        return "\n".join(lines)


class ContextBuilder:
    """Gathers live market data from Stock objects for LLM prompts."""

    def build(self, symbol: str) -> MarketContext:
        """Build equity context (base fields only)."""
        stock = self._find_stock(symbol)
        if stock is None:
            return MarketContext(symbol=symbol)

        zd = stock.zerodha_data
        agg = stock.options_aggregate

        spot = zd.get("last_price") or stock.ltp
        prev_close = (
            stock.prevDayOHLCV.get("CLOSE") if stock.prevDayOHLCV else None
        )
        change_pct = None
        if spot and prev_close and prev_close > 0:
            change_pct = ((spot - prev_close) / prev_close) * 100

        return MarketContext(
            symbol=symbol,
            spot=spot,
            day_high=zd.get("high"),
            day_low=zd.get("low"),
            day_open=zd.get("open"),
            prev_close=prev_close,
            change_pct=change_pct,
            vwap=zd.get("average_traded_price"),

            pcr=agg.get("live_pcr") or None,
            max_pain=self._get_max_pain(stock),
            ce_oi_wall=agg.get("max_oi_ce_strike"),
            pe_oi_wall=agg.get("max_oi_pe_strike"),
            atm_strike=agg.get("atm_strike"),
            atm_straddle=agg.get("atm_straddle_premium") or None,
            total_ce_oi=agg.get("total_ce_oi") or None,
            total_pe_oi=agg.get("total_pe_oi") or None,

            vix=self._get_vix(),
            expiry=self._get_expiry(stock),
            minutes_to_close=self._minutes_to_close(),
        )

    def build_index(self, symbol: str) -> IndexMarketContext:
        """
        Build enriched context for index confluences (NIFTY / BANKNIFTY).
        Adds volatility structure, OI flow, futures positioning, and
        previous day levels on top of the base MarketContext fields.
        """
        stock = self._find_stock(symbol)
        if stock is None:
            return IndexMarketContext(symbol=symbol)

        zd = stock.zerodha_data
        agg = stock.options_aggregate

        spot = zd.get("last_price") or stock.ltp
        prev_close = (
            stock.prevDayOHLCV.get("CLOSE") if stock.prevDayOHLCV else None
        )
        change_pct = None
        if spot and prev_close and prev_close > 0:
            change_pct = ((spot - prev_close) / prev_close) * 100

        return IndexMarketContext(
            # ── Base fields ────────────────────────────────────────────────
            symbol=symbol,
            spot=spot,
            day_high=zd.get("high"),
            day_low=zd.get("low"),
            day_open=zd.get("open"),
            prev_close=prev_close,
            change_pct=change_pct,
            vwap=zd.get("average_traded_price"),

            pcr=agg.get("live_pcr") or None,
            max_pain=self._get_max_pain(stock),
            ce_oi_wall=agg.get("max_oi_ce_strike"),
            pe_oi_wall=agg.get("max_oi_pe_strike"),
            atm_strike=agg.get("atm_strike"),
            atm_straddle=agg.get("atm_straddle_premium") or None,
            total_ce_oi=agg.get("total_ce_oi") or None,
            total_pe_oi=agg.get("total_pe_oi") or None,

            vix=self._get_vix(),
            expiry=self._get_expiry(stock),
            minutes_to_close=self._minutes_to_close(),

            # ── Previous day levels ────────────────────────────────────────
            prev_day_high=(
                stock.prevDayOHLCV.get("HIGH") if stock.prevDayOHLCV else None
            ),
            prev_day_low=(
                stock.prevDayOHLCV.get("LOW") if stock.prevDayOHLCV else None
            ),

            # ── Volatility structure ───────────────────────────────────────
            atm_iv_ce=agg.get("atm_iv_ce") or None,
            atm_iv_pe=agg.get("atm_iv_pe") or None,
            iv_skew=agg.get("iv_skew") or None,
            atm_iv_percentile=self._get_atm_iv_percentile(stock),

            # ── OI flow ────────────────────────────────────────────────────
            net_ce_oi_change=agg.get("net_ce_oi_change"),
            net_pe_oi_change=agg.get("net_pe_oi_change"),
            oi_change_type=self._get_sensibull_stat(stock, "oi_change_type"),
            volume_spike_type=self._get_sensibull_stat(stock, "volume_spike_type"),

            # ── Top OI concentration strikes ───────────────────────────────
            top_ce_strikes=self._get_top_oi_strikes(stock, "ce", n=3),
            top_pe_strikes=self._get_top_oi_strikes(stock, "pe", n=3),

            # ── Futures positioning ────────────────────────────────────────
            futures_ltp=self._get_futures_field(stock, "ltp"),
            futures_oi=self._get_futures_field(stock, "oi"),
            futures_change_pct=self._get_futures_change_pct(stock),
            futures_buy_qty=self._get_futures_field(stock, "buy_qty"),
            futures_sell_qty=self._get_futures_field(stock, "sell_qty"),
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _find_stock(self, symbol: str):
        for d in (shared.app_ctx.index_token_obj_dict,
                  shared.app_ctx.stock_token_obj_dict):
            for obj in d.values():
                if obj.stock_symbol == symbol:
                    return obj
        return None

    def _get_vix(self) -> float | None:
        for obj in shared.app_ctx.index_token_obj_dict.values():
            if obj.stock_symbol == "INDIA_VIX":
                return obj.zerodha_data.get("last_price") or obj.ltp
        return None

    def _get_max_pain(self, stock) -> float | None:
        try:
            stats = stock.sensibull_ctx.get("current", {}).get("stats")
            if stats and "max_pain" in stats:
                return stats["max_pain"]
        except (AttributeError, TypeError):
            pass
        return None

    def _get_expiry(self, stock) -> str | None:
        try:
            chain = stock.zerodha_ctx.get("option_chain", {}).get("current")
            if chain is not None and not chain.empty and "expiry" in chain.columns:
                return str(chain["expiry"].iloc[0])
        except (AttributeError, TypeError):
            pass
        return None

    def _minutes_to_close(self) -> int:
        now = datetime.now().time()
        close = time(15, 30)
        if now >= close:
            return 0
        now_mins = now.hour * 60 + now.minute
        close_mins = 15 * 60 + 30
        return close_mins - now_mins

    def _get_atm_iv_percentile(self, stock) -> float | None:
        """Read ATM IV percentile from Sensibull per-expiry stats."""
        try:
            stats = stock.sensibull_ctx.get("current", {}).get("stats", {})
            per_expiry = stats.get("per_expiry_map", {})
            if not per_expiry:
                return None
            # Use the nearest expiry (first key when sorted)
            nearest = sorted(per_expiry.keys())[0]
            return per_expiry[nearest].get("atm_iv_percentile")
        except (AttributeError, TypeError, IndexError):
            return None

    def _get_sensibull_stat(self, stock, key: str):
        """Read a field from Sensibull underlying_base_stats."""
        try:
            stats = stock.sensibull_ctx.get("current", {}).get("stats", {})
            return stats.get("underlying_base_stats", {}).get(key)
        except (AttributeError, TypeError):
            return None

    def _get_top_oi_strikes(self, stock, side: str, n: int = 3) -> list:
        """
        Return the top-n strikes by OI for the given side ('ce' or 'pe').
        Reads from sensibull_ctx['oi_chain']['per_strike_data'] first,
        falls back to options_live if Sensibull data is unavailable.
        Returns [(strike, oi), ...] sorted descending by OI.
        """
        try:
            oi_chain = stock.sensibull_ctx.get("oi_chain", {})
            per_strike = oi_chain.get("per_strike_data", {})
            if per_strike:
                oi_key = f"{side}_oi"
                pairs = [
                    (float(strike), data.get(oi_key, 0))
                    for strike, data in per_strike.items()
                    if data.get(oi_key, 0) > 0
                ]
                pairs.sort(key=lambda x: x[1], reverse=True)
                return pairs[:n]
        except (AttributeError, TypeError):
            pass

        # Fallback: options_live (live WebSocket data)
        try:
            oi_key = "oi"
            opt_side = side.upper()  # "CE" or "PE"
            pairs = [
                (float(strike), data.get(opt_side, {}).get(oi_key, 0))
                for strike, data in stock.options_live.items()
                if data.get(opt_side, {}).get(oi_key, 0) > 0
            ]
            pairs.sort(key=lambda x: x[1], reverse=True)
            return pairs[:n]
        except (AttributeError, TypeError):
            return []

    def _get_futures_field(self, stock, key: str):
        """Read a field from futures_live['current']."""
        try:
            return stock.futures_live.get("current", {}).get(key) or None
        except (AttributeError, TypeError):
            return None

    def _get_futures_change_pct(self, stock) -> float | None:
        """Compute futures % change vs previous close."""
        try:
            fut = stock.futures_live.get("current", {})
            ltp = fut.get("ltp")
            prev_close = fut.get("close")  # 'close' in futures_live = prev day close
            if ltp and prev_close and prev_close > 0:
                return ((ltp - prev_close) / prev_close) * 100
        except (AttributeError, TypeError):
            pass
        return None
