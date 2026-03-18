"""
LiveOptionsHistory
──────────────────
Lightweight in-memory time-series store for real-time options data.

Samples the options_aggregate + key options_live values every 60 seconds
and retains up to 375 entries (one full 6h 15min trading day).

Used by LiveOIAnalyser and LiveStraddleAnalyser to look back over minutes
instead of just the last few ticks.
"""

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class OptionsSnapshot:
    ts:               float         # Unix timestamp of this snapshot
    spot:             float         # Index spot price
    pcr:              float         # Put-Call Ratio (PE OI / CE OI)
    straddle:         float         # ATM straddle premium (CE + PE LTP)
    atm_strike:       float | None  # Strike closest to spot
    total_ce_oi:      int           # Sum of CE OI across all subscribed strikes
    total_pe_oi:      int           # Sum of PE OI across all subscribed strikes
    ce_wall:          float | None  # Strike with highest CE OI (resistance)
    pe_wall:          float | None  # Strike with highest PE OI (support)
    ce_wall_oi:       int           # OI at ce_wall strike
    pe_wall_oi:       int           # OI at pe_wall strike
    net_ce_oi_change: int           # Net CE OI change since last aggregate reset
    net_pe_oi_change: int           # Net PE OI change since last aggregate reset


class LiveOptionsHistory:
    """
    Per-symbol circular history buffer.

    Records one snapshot per SAMPLE_INTERVAL seconds.
    Retains at most MAX_SNAPSHOTS entries (≈ one trading day at 1-min samples).

    Query methods return plain lists — no pandas, no external deps.
    """

    MAX_SNAPSHOTS    = 375   # 375 min = full trading day (9:15 – 15:30 IST)
    SAMPLE_INTERVAL  = 60    # seconds between samples

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._buf: deque[OptionsSnapshot] = deque(maxlen=self.MAX_SNAPSHOTS)
        self._last_ts: float = 0.0

    # ── recording ────────────────────────────────────────────────────────────

    def record(self, agg: dict, options_live: dict, spot: float) -> bool:
        """
        Attempt to record a new snapshot.
        Returns True if a snapshot was saved, False if the interval hasn't elapsed.
        """
        now = time.time()
        if now - self._last_ts < self.SAMPLE_INTERVAL:
            return False

        ce_wall = agg.get("max_oi_ce_strike")
        pe_wall = agg.get("max_oi_pe_strike")

        snap = OptionsSnapshot(
            ts               = now,
            spot             = spot,
            pcr              = agg.get("live_pcr", 0.0),
            straddle         = agg.get("atm_straddle_premium", 0.0),
            atm_strike       = agg.get("atm_strike"),
            total_ce_oi      = agg.get("total_ce_oi", 0),
            total_pe_oi      = agg.get("total_pe_oi", 0),
            ce_wall          = ce_wall,
            pe_wall          = pe_wall,
            ce_wall_oi       = (options_live.get(ce_wall) or {}).get("CE", {}).get("oi", 0) if ce_wall else 0,
            pe_wall_oi       = (options_live.get(pe_wall) or {}).get("PE", {}).get("oi", 0) if pe_wall else 0,
            net_ce_oi_change = agg.get("net_ce_oi_change", 0),
            net_pe_oi_change = agg.get("net_pe_oi_change", 0),
        )
        self._buf.append(snap)
        self._last_ts = now
        return True

    # ── basic accessors ───────────────────────────────────────────────────────

    def size(self) -> int:
        return len(self._buf)

    def minutes_of_data(self) -> float:
        if len(self._buf) < 2:
            return 0.0
        return (self._buf[-1].ts - self._buf[0].ts) / 60.0

    def all(self) -> list[OptionsSnapshot]:
        return list(self._buf)

    def latest(self) -> OptionsSnapshot | None:
        return self._buf[-1] if self._buf else None

    def oldest(self) -> OptionsSnapshot | None:
        return self._buf[0] if self._buf else None

    # ── time-range queries ────────────────────────────────────────────────────

    def since(self, seconds: float) -> list[OptionsSnapshot]:
        """All snapshots from the last `seconds` seconds."""
        cutoff = time.time() - seconds
        return [s for s in self._buf if s.ts >= cutoff]

    def last_n(self, n: int) -> list[OptionsSnapshot]:
        """The most recent `n` snapshots."""
        buf = list(self._buf)
        return buf[-n:] if len(buf) >= n else buf

    # ── derived time-series ───────────────────────────────────────────────────

    def pcr_series(self, minutes: int) -> list[float]:
        """PCR values over the last `minutes` minutes (oldest first)."""
        return [s.pcr for s in self.since(minutes * 60) if s.pcr > 0]

    def straddle_series(self, minutes: int) -> list[tuple[float, float]]:
        """(straddle, spot) pairs over the last `minutes` minutes (oldest first)."""
        return [(s.straddle, s.spot) for s in self.since(minutes * 60) if s.straddle > 0]

    def oi_series(self, minutes: int) -> list[tuple[int, int]]:
        """(total_ce_oi, total_pe_oi) pairs over the last `minutes` minutes."""
        return [(s.total_ce_oi, s.total_pe_oi) for s in self.since(minutes * 60)]

    def wall_oi_trend(self, wall_type: str, minutes: int) -> tuple[int, int] | None:
        """
        Returns (oldest_oi, latest_oi) for the dominant OI wall over `minutes` minutes.
        Returns None if the wall strike changed during the window (wall migrated).

        wall_type: "CE" (resistance wall) or "PE" (support wall)
        """
        snaps = self.since(minutes * 60)
        if len(snaps) < 2:
            return None

        if wall_type == "CE":
            pairs = [(s.ce_wall, s.ce_wall_oi) for s in snaps if s.ce_wall]
        else:
            pairs = [(s.pe_wall, s.pe_wall_oi) for s in snaps if s.pe_wall]

        if len(pairs) < 2:
            return None

        # Wall must be the same strike throughout the window
        if pairs[0][0] != pairs[-1][0]:
            return None

        return pairs[0][1], pairs[-1][1]

    def pcr_trend_slope(self, minutes: int) -> float | None:
        """
        Simple linear slope of PCR over `minutes` minutes.
        Positive = PCR rising (bullish), Negative = PCR falling (bearish).
        Returns None if insufficient data.
        """
        series = self.pcr_series(minutes)
        if len(series) < 3:
            return None
        n = len(series)
        # Least-squares slope approximation using index as x
        x_mean = (n - 1) / 2.0
        y_mean = sum(series) / n
        num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(series))
        den = sum((i - x_mean) ** 2 for i in range(n))
        return num / den if den > 0 else None

    def ce_oi_change_pct(self, minutes: int) -> float | None:
        """% change in total CE OI over `minutes` minutes. Positive = buildup."""
        snaps = self.since(minutes * 60)
        if len(snaps) < 2 or snaps[0].total_ce_oi == 0:
            return None
        return (snaps[-1].total_ce_oi - snaps[0].total_ce_oi) / snaps[0].total_ce_oi * 100

    def pe_oi_change_pct(self, minutes: int) -> float | None:
        """% change in total PE OI over `minutes` minutes. Positive = buildup."""
        snaps = self.since(minutes * 60)
        if len(snaps) < 2 or snaps[0].total_pe_oi == 0:
            return None
        return (snaps[-1].total_pe_oi - snaps[0].total_pe_oi) / snaps[0].total_pe_oi * 100
