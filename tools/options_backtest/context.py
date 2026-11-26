"""Rebuild the Redis view the analysis worker and paper-trading engine read.

Every captured snapshot is published as the same hashes production writes:
``data:options_live``, ``data:options_agg``, ``data:tick``, ``data:price``,
``data:zerodha`` and ``data:sensibull``. Rolling history (OI-chain snapshots,
ATM IV, price bars) is kept so the intraday analysers see the same depth of
context they had live.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pandas as pd

from services.common.serialization import dataframe_to_json, safe_json_dumps
from services.data_gateway.sensibull_fetcher import _compute_max_pain

from .loader import Snapshot
from .replay_redis import ReplayRedis

DEFAULT_LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 35, "SENSEX": 20,
    "FINNIFTY": 65, "MIDCPNIFTY": 120, "BANKEX": 30,
}
OI_HISTORY_LEN = 15


def _i(value) -> int:
    """NaN-safe int (real capture frames have NaN for missing numeric fields)."""
    number = _f(value)
    return int(number) if number is not None else 0


def _f(value) -> float | None:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class ReplayContext:
    symbol: str
    lot_size: int
    redis: ReplayRedis = field(default_factory=ReplayRedis)
    _bars: list[dict] = field(default_factory=list)
    _futures: list[dict] = field(default_factory=list)
    _iv_history: list[dict] = field(default_factory=list)
    _oi_history: list[dict] = field(default_factory=list)
    _first_spot: float | None = None
    _prev_spot: float | None = None

    @classmethod
    def for_symbol(cls, symbol: str, lot_size: int | None = None) -> "ReplayContext":
        return cls(symbol, lot_size or DEFAULT_LOT_SIZES.get(symbol, 75))

    # ── per snapshot ─────────────────────────────────────────────────────

    def update(self, snapshot: Snapshot, *, analysis_cycle: bool = False) -> None:
        frame = snapshot.frame
        spot = snapshot.spot
        if self._first_spot is None:
            self._first_spot = spot
        expiry = snapshot.expiry

        live: dict[str, str] = {}
        per_strike: dict[str, dict] = {}
        atm_ivs: list[float] = []
        atm = snapshot.atm_strike
        for row in frame.itertuples(index=False):
            strike = _f(row.strike)
            side = str(row.side)
            if strike is None:
                continue
            tick = {
                "ltp": _f(row.ltp), "bid": _f(row.bid), "ask": _f(row.ask),
                "buy_qty": _i(row.bid_qty), "sell_qty": _i(row.ask_qty),
                "oi": _i(row.oi), "open_oi": _i(row.open_oi),
                "prev_oi": _i(row.prev_oi), "has_oi": bool(row.has_oi),
                "volume": _i(row.volume), "iv": _f(row.iv),
                "delta": _f(row.delta), "gamma": _f(row.gamma),
                "theta": _f(row.theta), "vega": _f(row.vega),
                "iv_change": _f(row.iv_change),
                "timestamp": snapshot.ts.isoformat(),
            }
            live[f"{float(strike)}_{side}"] = json.dumps(tick)
            bucket = per_strike.setdefault(f"{float(strike):.2f}", {
                "call_oi": 0, "put_oi": 0, "prev_call_oi": 0, "prev_put_oi": 0,
            })
            if side == "CE":
                bucket["call_oi"] = tick["oi"]
                bucket["prev_call_oi"] = tick["open_oi"]
            else:
                bucket["put_oi"] = tick["oi"]
                bucket["prev_put_oi"] = tick["open_oi"]
            if atm is not None and strike == atm and tick["iv"]:
                atm_ivs.append(tick["iv"])

        call_oi = sum(v["call_oi"] for v in per_strike.values())
        put_oi = sum(v["put_oi"] for v in per_strike.values())
        call_change = sum(v["call_oi"] - v["prev_call_oi"] for v in per_strike.values())
        put_change = sum(v["put_oi"] - v["prev_put_oi"] for v in per_strike.values())
        chain_atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else None
        # Prefer the captured vendor ATM IV / percentile; the chain-derived
        # average is the fallback for days recorded before the schema change.
        atm_iv = _f(snapshot.ctx.get("atm_iv_near")) or chain_atm_iv
        ts_epoch = snapshot.ts.timestamp()

        self.redis.hset(f"data:options_live:{self.symbol}", mapping=live)
        self.redis.hset(f"data:options_agg:{self.symbol}", mapping={
            "last_updated": ts_epoch,
            "expiry": expiry,
            "atm_strike": atm or "",
            "atm_straddle_premium": snapshot.ctx.get("atm_straddle") or "",
            "atm_iv": atm_iv or "",
            "live_pcr": snapshot.ctx.get("live_pcr") or "",
            "pcr_window_pct": snapshot.ctx.get("pcr_window_pct") or "",
            "max_pain_strike": snapshot.ctx.get("max_pain_strike") or "",
            "future_price": snapshot.ctx.get("future_price") or "",
            "total_ce_oi": call_oi,
            "total_pe_oi": put_oi,
            "net_ce_oi_change": call_change,
            "net_pe_oi_change": put_change,
            "option_tick_count": snapshot.ctx.get("option_tick_count") or "",
            "tick_count": snapshot.ctx.get("option_tick_count") or "",
        })
        self.redis.hset(f"data:tick:{self.symbol}", mapping={
            "last_price": spot,
            "open": self._first_spot or spot,
            "high": max(spot, self._prev_spot or spot),
            "low": min(spot, self._prev_spot or spot),
            "close": spot,
            "timestamp": ts_epoch,
            "volume_traded": 0,
        })

        self._bars.append({
            "date": snapshot.ts,
            "Open": self._prev_spot or spot,
            "High": max(spot, self._prev_spot or spot),
            "Low": min(spot, self._prev_spot or spot),
            "Close": spot,
            "Volume": 0,
        })
        self._prev_spot = spot
        bars = pd.DataFrame(self._bars).set_index("date")

        future_price = _f(snapshot.ctx.get("future_price")) or spot
        fut_oi = _i(snapshot.ctx.get("fut_oi"))
        self._futures.append({
            "date": snapshot.ts, "open": future_price, "high": future_price,
            "low": future_price, "close": future_price,
            "volume": _i(snapshot.ctx.get("fut_volume")),
            "oi": fut_oi, "underlying_price": spot,
        })
        fut_df = pd.DataFrame(self._futures).set_index("date")

        self.redis.hset(f"data:price:{self.symbol}", mapping={
            "priceData_json": dataframe_to_json(bars),
            "ltp": spot,
            "ltp_change_perc": (spot - (self._first_spot or spot))
            / (self._first_spot or spot) * 100,
            "prevDayOHLCV_json": safe_json_dumps({
                "OPEN": self._first_spot, "HIGH": spot, "LOW": spot,
                "CLOSE": self._first_spot,
            }),
            "daily_hv": 18.0,
        })
        self.redis.hset(f"data:zerodha:{self.symbol}", mapping={
            "futures_data_current_json": dataframe_to_json(fut_df),
            "futures_data_next_json": "{}",
            "futures_mdata_json": safe_json_dumps({
                "current": {"expiry": expiry, "instrument_token": 1},
                "next": None,
            }),
        })

        if analysis_cycle:
            self._iv_history.append({
                "timestamp": snapshot.ts,
                f"atm_iv_{expiry.replace('-', '')}": atm_iv,
                "spot": spot,
            })
            self._oi_history.append({
                "timestamp": snapshot.ts.isoformat(),
                "date": snapshot.ts.date().isoformat(),
                "expiry": expiry,
                "prev_ltp": self._first_spot,
                "current_ltp": spot,
                "date_ltp": self._first_spot,
                "atm_strike": atm,
                "total_call_oi": call_oi,
                "total_put_oi": put_oi,
                "total_call_oi_change": call_change,
                "total_put_oi_change": put_change,
                "pcr": (put_oi / call_oi) if call_oi else None,
                "per_strike_data": per_strike,
                "strike_list": sorted(float(k) for k in per_strike),
                "min_strike": min((float(k) for k in per_strike), default=None),
                "max_strike": max((float(k) for k in per_strike), default=None),
                "underlying_token": 1,
            })
            self._oi_history = self._oi_history[-OI_HISTORY_LEN:]

        self.redis.hset(f"data:sensibull:{self.symbol}", mapping={
            "last_fetch_time": snapshot.ts.isoformat(),
            "current_json": safe_json_dumps(self._current_payload(
                snapshot, atm, atm_iv, call_oi, put_oi, future_price)),
            "historical_data_json": dataframe_to_json(pd.DataFrame(self._iv_history)),
            "oi_chain_json": safe_json_dumps(self._oi_history[-1] if self._oi_history else None),
            "oi_chain_history_json": safe_json_dumps(self._oi_history),
            "iv_chart_history_json": "{}",
            "oi_history_json": "{}",
        })

    # ── payload builders ─────────────────────────────────────────────────

    def _current_payload(self, snapshot: Snapshot, atm, atm_iv, call_oi, put_oi,
                         future_price) -> dict:
        expiry = snapshot.expiry
        pcr = (put_oi / call_oi) if call_oi else None
        max_pain = _f(snapshot.ctx.get("max_pain_strike"))
        percentile = _f(snapshot.ctx.get("atm_iv_percentile"))
        per_expiry = {expiry: {
            "atm_iv_percentile": percentile if percentile is not None else 50.0,
            "atm_strike": atm,
            "atm_iv": atm_iv,
            "atm_iv_change": 0.0,
            "atm_ivp_type": "MODERATE",
            "max_pain_strike": max_pain,
            "max_pain_type": self._max_pain_type(snapshot.spot, max_pain),
            "pcr": pcr,
            "pcr_type": self._pcr_type(pcr),
            "future_price": future_price,
            "future_change_percent": 0.0,
            "lot_size": self.lot_size,
            "days_to_expiry": int(snapshot.ctx.get("dte") or 0),
        }}
        next_expiry = str(snapshot.ctx.get("next_expiry") or "")[:10]
        next_iv = _f(snapshot.ctx.get("atm_iv_next"))
        if next_expiry and next_iv is not None:
            far = dict(per_expiry[expiry])
            far.update({
                "atm_iv": next_iv,
                "days_to_expiry": int(snapshot.ctx.get("dte") or 0) + 7,
            })
            per_expiry[next_expiry] = far
        return {
            # fetch_expiry() reads the map at the top level; the analysers read
            # it under "stats" (as the fetched payload is shaped). Publish both.
            "per_expiry_map": per_expiry,
            "underlying_info": {"symbol": self.symbol, "lot_size": self.lot_size},
            "stats": {
                "underlying_base_stats": {
                    "total_pcr": pcr,
                    "volume_spike": 1.0,
                    "volume_spike_type": "NORMAL",
                    "future_oi_change": _f(snapshot.ctx.get("fut_oi")),
                    "oi_change_type": "NEUTRAL",
                },
                "per_expiry_map": per_expiry,
            },
            "nse_stats": {},
        }

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

    # ── helpers ──────────────────────────────────────────────────────────

    @property
    def first_spot(self) -> float | None:
        return self._first_spot

    @property
    def current_spot(self) -> float | None:
        return self._prev_spot

    def max_pain_from_chain(self, snapshot: Snapshot) -> float | None:
        frame = snapshot.frame
        per_strike: dict[str, dict] = {}
        for row in frame.itertuples(index=False):
            strike = _f(row.strike)
            if strike is None:
                continue
            bucket = per_strike.setdefault(f"{strike:.2f}", {"call_oi": 0, "put_oi": 0})
            if str(row.side) == "CE":
                bucket["call_oi"] = _i(row.oi)
            else:
                bucket["put_oi"] = _i(row.oi)
        return _compute_max_pain(per_strike, snapshot.spot)
