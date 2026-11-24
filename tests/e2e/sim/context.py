"""Publishes simulator state to Redis in production shapes (T5.5).

The analysis engine reads ``data:price``, ``data:zerodha`` and ``data:sensibull``;
the feed already writes ticks/agg/live through the real publisher. This module
fills the remaining keys so ``worker.process_job`` runs on real inputs, and the
Sensibull context is derived from the *same* chain the feed emitted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from services.common.serialization import dataframe_to_json, safe_json_dumps
from services.data_gateway.sensibull_fetcher import (
    _compute_iv_percentile,
    _compute_max_pain,
    publish_to_redis,
)

from .config import Scenario
from .market import MarketState


@dataclass
class BarSeries:
    """Minute-ish OHLCV accumulated per symbol from simulated ticks."""

    rows: dict[str, list[dict]] = field(default_factory=dict)

    def add_option_bar(self, symbol: str, now: datetime, price: float,
                       previous: float | None, volume: int) -> None:
        series = self.rows.setdefault(symbol, [])
        open_price = previous if previous is not None else price
        series.append({
            "date": now,
            "Open": round(open_price, 2),
            "High": round(max(open_price, price), 2),
            "Low": round(min(open_price, price), 2),
            "Close": round(price, 2),
            "Volume": volume,
        })

    def frame(self, symbol: str) -> pd.DataFrame:
        rows = self.rows.get(symbol, [])
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame["date"])
        return frame.set_index("date")

    def futures_frame(self, symbol: str, spot: float, oi: int) -> pd.DataFrame:
        frame = self.frame(symbol)
        if frame.empty:
            return frame
        frame = frame.rename(columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        }).copy()
        frame["oi"] = oi
        frame["underlying_price"] = spot
        return frame


class ContextPublisher:
    """Builds and publishes price / zerodha / sensibull hashes each cycle."""

    def __init__(self, redis, scenario: Scenario, feed) -> None:
        self._redis = redis
        self._scenario = scenario
        self._feed = feed
        self._ctx: dict[str, dict] = {}
        self._iv_chart = {
            symbol: self._make_iv_chart(symbol) for symbol in scenario.symbols
        }

    # ── price + zerodha ──────────────────────────────────────────────────

    def publish_price(self, symbol: str, bars: BarSeries, state: MarketState) -> None:
        frame = bars.frame(symbol)
        open_spot = self._feed.generator.open_spot[symbol]
        self._redis.hset(f"data:price:{symbol}", mapping={
            "priceData_json": dataframe_to_json(frame),
            "ltp": str(state.spot[symbol]),
            "ltp_change_perc": str((state.spot[symbol] - open_spot) / open_spot * 100),
            "prevDayOHLCV_json": safe_json_dumps({
                "OPEN": open_spot,
                "HIGH": max(state.spot[symbol], open_spot),
                "LOW": min(state.spot[symbol], open_spot),
                "CLOSE": open_spot,
            }),
            "daily_hv": "18.5",
        })

    def publish_zerodha(self, symbol: str, bars: BarSeries, state: MarketState) -> None:
        futures = bars.futures_frame(symbol, state.spot[symbol],
                                     oi=int(3_000_000 + state.elapsed))
        expiry = self._scenario.expiry[symbol].isoformat()
        self._redis.hset(f"data:zerodha:{symbol}", mapping={
            "futures_data_current_json": dataframe_to_json(futures),
            "futures_data_next_json": "{}",
            "futures_mdata_json": safe_json_dumps({
                "current": {"expiry": expiry, "instrument_token": 1,
                            "lot_size": self._scenario.lot(symbol)},
                "next": None,
            }),
        })

    # ── sensibull ────────────────────────────────────────────────────────

    def publish_sensibull(self, symbol: str, state: MarketState) -> None:
        current = self._build_current_data(symbol, state)
        chain = self._build_oi_chain(symbol, state, current)
        existing = self._ctx.setdefault(symbol, {})
        publish_to_redis(
            self._redis, symbol, current, chain, existing,
            mode="intraday", iv_chart=self._iv_chart[symbol],
        )

    def _build_current_data(self, symbol: str, state: MarketState) -> dict:
        stock = self._feed.stocks[symbol]
        expiry = self._scenario.expiry[symbol].isoformat()
        agg = stock._tick_store.options_aggregate
        per_strike = self._per_strike_data(symbol)
        spot = state.spot[symbol]
        call_oi = sum(int(d["call_oi"] or 0) for d in per_strike.values()) or 0
        put_oi = sum(int(d["put_oi"] or 0) for d in per_strike.values()) or 0
        pcr = round(put_oi / call_oi, 4) if call_oi else None
        max_pain = _compute_max_pain(per_strike, spot)

        near_iv = round(state.atm_iv[symbol] * 100, 2)
        entry = {
            "atm_strike": agg.get("atm_strike") or self._feed.atm_strike(symbol, spot),
            "atm_iv": near_iv,
            "atm_iv_change": 0.2,
            "atm_iv_percentile": self._scenario.atm_iv_percentile,
            "atm_ivp_type": "MODERATE",
            "max_pain_strike": max_pain,
            "max_pain_type": self._max_pain_type(spot, max_pain),
            "pcr": pcr,
            "pcr_type": self._pcr_type(pcr),
            "future_price": round(state.fut[symbol], 2),
            "future_change_percent": round(
                (state.fut[symbol] - self._feed.generator.open_spot[symbol])
                / self._feed.generator.open_spot[symbol] * 100, 3),
            "lot_size": self._scenario.lot(symbol),
            "days_to_expiry": max((self._scenario.expiry[symbol] - self._scenario.date).days, 0),
        }
        far_expiry = (self._scenario.expiry[symbol] + timedelta(days=7)).isoformat()
        far_entry = dict(entry)
        far_entry.update({
            "atm_iv": round(near_iv + self._scenario.term_slope_pct_per_week, 2),
            "atm_iv_change": 0.0,
            "atm_iv_percentile": self._scenario.atm_iv_percentile,
            "days_to_expiry": entry["days_to_expiry"] + 7,
        })
        return {
            "underlying_info": {"symbol": symbol,
                                "lot_size": self._scenario.lot(symbol)},
            "stats": {
                "underlying_base_stats": {
                    "volume_spike": 1.4,
                    "volume_spike_type": "HIGH",
                    "future_oi_change": 1.2,
                    "oi_change_type": "BUILDUP",
                    "total_pcr": pcr,
                },
                "per_expiry_map": {expiry: entry, far_expiry: far_entry},
            },
            "nse_stats": {"advances": 30, "declines": 20},
        }

    def _build_oi_chain(self, symbol: str, state: MarketState, current: dict) -> dict:
        per_strike = self._per_strike_data(symbol)
        spot = state.spot[symbol]
        call_oi = sum(int(d["call_oi"] or 0) for d in per_strike.values())
        put_oi = sum(int(d["put_oi"] or 0) for d in per_strike.values())
        call_change = sum(int(d["call_oi"] or 0) - int(d["prev_call_oi"] or 0)
                          for d in per_strike.values())
        put_change = sum(int(d["put_oi"] or 0) - int(d["prev_put_oi"] or 0)
                         for d in per_strike.values())
        strikes = sorted(float(k) for k in per_strike)
        expiry = self._scenario.expiry[symbol].isoformat()
        prev_spot = self._feed.generator.open_spot[symbol]
        return {
            "timestamp": state.t.isoformat(),
            "date": self._scenario.date.isoformat(),
            "expiry": expiry,
            "underlying_symbol": symbol,
            "prev_ltp": round(prev_spot, 2),
            "current_ltp": round(spot, 2),
            "date_ltp": round(prev_spot, 2),
            "atm_strike": self._feed.atm_strike(symbol, spot),
            "total_call_oi": call_oi,
            "total_put_oi": put_oi,
            "total_call_oi_change": call_change,
            "total_put_oi_change": put_change,
            "pcr": round(put_oi / call_oi, 4) if call_oi else None,
            "per_strike_data": per_strike,
            "strike_list": strikes,
            "min_strike": strikes[0] if strikes else None,
            "max_strike": strikes[-1] if strikes else None,
            "underlying_token": 1,
        }

    def _per_strike_data(self, symbol: str) -> dict:
        live = self._feed.stocks[symbol]._tick_store.options_live
        data: dict[str, dict] = {}
        for strike, sides in live.items():
            row = {
                "call_oi": int((sides.get("CE") or {}).get("oi") or 0),
                "put_oi": int((sides.get("PE") or {}).get("oi") or 0),
                "prev_call_oi": int((sides.get("CE") or {}).get("open_oi")
                                    or (sides.get("CE") or {}).get("oi") or 0),
                "prev_put_oi": int((sides.get("PE") or {}).get("open_oi")
                                   or (sides.get("PE") or {}).get("oi") or 0),
            }
            if row["call_oi"] or row["put_oi"]:
                data[f"{float(strike):.2f}"] = row
        return data

    # ── static helpers ───────────────────────────────────────────────────

    def _make_iv_chart(self, symbol: str) -> pd.DataFrame:
        base = self._scenario.iv_atm * 100
        rows = []
        for i in range(252):
            iv = base + 3.0 * ((i % 20) / 20.0 - 0.5) + (i % 7) * 0.1
            rows.append({"date": (date(2025, 1, 1) + timedelta(days=i)).isoformat(),
                         "iv_close": round(iv, 2),
                         "price_close": round(self._scenario.spot[symbol], 2)})
        return pd.DataFrame(rows)

    @staticmethod
    def _max_pain_type(spot: float, max_pain: float | None) -> str:
        if max_pain is None:
            return "Neutral"
        if spot > max_pain * 1.001:
            return "Bullish"
        if spot < max_pain * 0.999:
            return "Bearish"
        return "Neutral"

    @staticmethod
    def _pcr_type(pcr: float | None) -> str:
        if pcr is None:
            return "Neutral"
        if pcr > 1.2:
            return "Bullish"
        if pcr < 0.7:
            return "Bearish"
        return "Neutral"


def iv_percentile_of_chart(frame: pd.DataFrame) -> tuple[float | None, str | None]:
    """Exposed for tests: the same computation the fetcher uses."""
    return _compute_iv_percentile(frame)
