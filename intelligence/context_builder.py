"""
ContextBuilder — gathers a real-time market snapshot for LLM prompts.

Pulls data from Stock objects in shared.app_ctx to build a structured
MarketContext that the narrator injects into the prompt template.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, time

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


class ContextBuilder:
    """Gathers live market data from Stock objects for LLM prompts."""

    def build(self, symbol: str) -> MarketContext:
        stock = self._find_stock(symbol)
        if stock is None:
            return MarketContext(symbol=symbol)

        zd = stock.zerodha_data
        agg = stock.options_aggregate

        spot = zd.get("last_price") or stock.ltp
        prev_close = stock.prev_day_close
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

    def _find_stock(self, symbol: str):
        """Look up the Stock object by symbol across all dicts."""
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
        """Extract max pain from sensibull or options aggregate."""
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
            if chain and hasattr(chain, "expiry"):
                return str(chain.expiry)
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
