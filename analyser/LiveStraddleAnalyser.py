import time
from collections import deque
from common.logging_util import logger
from analyser.LiveAlertFormatter import F

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from analyser.LiveOptionsHistory import LiveOptionsHistory


class LiveStraddleAnalyser:
    """
    Real-time straddle / IV analysis using live WebSocket tick data.

    Strategies:
      - IV Change Detection:   Straddle ±% with spot flat → IV expanding/compressing.
                               Uses history for 5-min comparison when available;
                               falls back to internal 60-s snapshot deque otherwise.
      - ATM Implied Move:      Spot consumed ≥ 75% of expected daily range → boundary alert.
                               Opening reference comes from history[0] (true open) when
                               available, else from first tick seen this session.
      - IV Skew Reversal:      CE/PE LTP ratio crosses parity at ATM → skew flip alert.
    """

    SNAPSHOT_INTERVAL_SEC  = 60      # Internal fallback snapshot frequency
    IV_EXPANDING_THRESHOLD = 0.04    # +4% straddle with flat spot = IV expanding
    IV_COMPRESS_THRESHOLD  = -0.05   # −5% straddle with flat spot = IV compressing
    SPOT_FLAT_PCT          = 0.001   # Spot must move < 0.1% to attribute change to IV
    RANGE_USED_PCT         = 0.75    # Alert when ≥ 75% of expected range is consumed
    SKEW_FLIP_BUFFER       = 0.05    # CE/PE ratio needs >1.05 / <0.95 before a flip counts

    def __init__(self, symbol: str):
        self.symbol = symbol

        # Internal fallback snapshot deque (used before history has enough data)
        self._snapshots: deque = deque(maxlen=12)      # (ts, straddle, spot)
        self._last_snapshot_ts: float = 0.0

        # Opening reference for implied-move range
        self._open_straddle: float = 0.0
        self._open_spot: float = 0.0

        # CE/PE ratio history for skew detection
        self._skew_ratio_history: deque = deque(maxlen=6)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _record_snapshot(self, straddle: float, spot: float):
        """Internal fallback — only used when no history object is passed."""
        now = time.time()
        if now - self._last_snapshot_ts >= self.SNAPSHOT_INTERVAL_SEC:
            self._snapshots.append((now, straddle, spot))
            self._last_snapshot_ts = now

        if self._open_straddle == 0.0 and straddle > 0 and spot > 0:
            self._open_straddle = straddle
            self._open_spot = spot

    def _open_reference(self, history: "LiveOptionsHistory | None") -> tuple[float, float]:
        """
        Return (open_straddle, open_spot).
        Prefers oldest history entry (true session open), falls back to first-tick cache.
        """
        if history:
            oldest = history.oldest()
            if oldest and oldest.straddle > 0 and oldest.spot > 0:
                return oldest.straddle, oldest.spot
        return self._open_straddle, self._open_spot

    # ── signal checks ─────────────────────────────────────────────────────────

    def check_iv_change(
        self,
        agg: dict,
        spot: float,
        history: "LiveOptionsHistory | None" = None,
    ) -> tuple[str, str] | None:
        """
        Compares straddle premium over a 5-min window with spot movement.
        Returns (alert_type, html_message) when IV has shifted with spot flat.

        With history (≥ 6 min of data): uses proper 5-min lookback.
        Without history: uses internal 60-s snapshot deque.
        """
        straddle = agg.get("atm_straddle_premium", 0)
        atm      = agg.get("atm_strike", "N/A")
        if straddle <= 0 or spot <= 0:
            return None

        if history and history.minutes_of_data() >= 6:
            return self._iv_change_from_history(history, straddle, atm, spot)

        # ── Fallback ──────────────────────────────────────────────────────────
        self._record_snapshot(straddle, spot)
        if len(self._snapshots) < 3:
            return None
        old_ts, old_straddle, old_spot = self._snapshots[-3]
        return self._iv_change_calc(straddle, old_straddle, spot, old_spot,
                                    time.time() - old_ts, atm)

    def _iv_change_from_history(
        self, history, straddle, atm, spot
    ) -> tuple[str, str] | None:
        series = history.straddle_series(5)    # last 5 minutes
        if len(series) < 2:
            return None
        old_straddle, old_spot = series[0]
        elapsed = 5 * 60  # approximate; good enough for display
        return self._iv_change_calc(straddle, old_straddle, spot, old_spot,
                                    elapsed, atm)

    def _iv_change_calc(
        self, straddle, old_straddle, spot, old_spot, elapsed_sec, atm
    ) -> tuple[str, str] | None:
        if old_straddle <= 0 or elapsed_sec < 30:
            return None
        straddle_chg_pct = (straddle - old_straddle) / old_straddle
        spot_chg_pct     = abs((spot - old_spot) / old_spot) if old_spot > 0 else 0
        if spot_chg_pct > self.SPOT_FLAT_PCT:
            return None

        elapsed_min = elapsed_sec / 60

        if straddle_chg_pct >= self.IV_EXPANDING_THRESHOLD:
            msg = F.build(
                F.header(self.symbol, "IV EXPANDING", "⚠️"),
                F.kv("ATM", f"{atm}  Straddle: {old_straddle:.0f} → {straddle:.0f}  ({straddle_chg_pct*100:+.1f}% in {elapsed_min:.0f} min)"),
                F.kv("Spot", f"{spot:.2f}  (flat)"),
                F.signal("Event risk building. <b>Avoid short premium.</b>"),
            )
            return ("IV_EXPANDING", msg)

        if straddle_chg_pct <= self.IV_COMPRESS_THRESHOLD:
            msg = F.build(
                F.header(self.symbol, "IV COMPRESSING", "📉"),
                F.kv("ATM", f"{atm}  Straddle: {old_straddle:.0f} → {straddle:.0f}  ({straddle_chg_pct*100:.1f}% in {elapsed_min:.0f} min)"),
                F.kv("Spot", f"{spot:.2f}  (flat)"),
                F.signal("Premium bleeding fast. <b>Sell side favored — theta &amp; IV crush active.</b>"),
            )
            return ("IV_COMPRESSING", msg)

        return None

    def check_implied_move_boundary(
        self,
        agg: dict,
        spot: float,
        history: "LiveOptionsHistory | None" = None,
    ) -> tuple[str, str] | None:
        """
        Expected daily range = ATM straddle × 0.68.
        Fires when spot has consumed ≥ 75% of that range from the session open.

        Opening reference: history[0] if available, else first-tick cache.
        """
        straddle = agg.get("atm_straddle_premium", 0)
        if straddle <= 0 or spot <= 0:
            return None

        # Ensure internal fallback cache is populated even when history is used
        self._record_snapshot(straddle, spot)

        open_straddle, open_spot = self._open_reference(history)
        if open_spot <= 0:
            return None

        expected_range = open_straddle * 0.68
        half_range     = expected_range / 2.0
        upper          = open_spot + half_range
        lower          = open_spot - half_range

        if spot >= open_spot:
            used_pct  = (spot - open_spot) / half_range
            remaining = upper - spot
            direction = "UP"
        else:
            used_pct  = (open_spot - spot) / half_range
            remaining = spot - lower
            direction = "DOWN"

        if used_pct >= self.RANGE_USED_PCT and remaining > 0:
            book_action  = "longs" if direction == "UP" else "shorts"
            source_note  = "from session open" if history and history.oldest() else "from first tick"
            msg = F.build(
                F.header(self.symbol, f"Near Range Boundary  ({source_note})", "📏"),
                F.kv_pair("Spot", f"{spot:.2f}", "Direction", f"<b>{direction}</b>"),
                F.kv("Range", f"{lower:.0f} – {upper:.0f}  (open straddle: {open_straddle:.0f})"),
                F.kv_pair("Used", f"{used_pct*100:.0f}%", "Remaining", f"{remaining:.0f} pts"),
                F.signal(f"Consider booking <b>{book_action}</b> or hedging."),
            )
            return ("RANGE_BOUNDARY", msg)

        return None

    def check_iv_skew_reversal(
        self,
        agg: dict,
        options_live: dict,
        spot: float,
    ) -> tuple[str, str] | None:
        """
        Tracks CE LTP / PE LTP ratio at ATM.
        Fires when ratio crosses parity with enough prior skew.
        (No history needed — skew reversals are fast events.)
        """
        atm = agg.get("atm_strike")
        if not atm or spot <= 0:
            return None

        atm_data = options_live.get(atm, {})
        ce_ltp   = atm_data.get("CE", {}).get("ltp", 0)
        pe_ltp   = atm_data.get("PE", {}).get("ltp", 0)
        if ce_ltp <= 0 or pe_ltp <= 0:
            return None

        ratio = ce_ltp / pe_ltp
        self._skew_ratio_history.append(ratio)

        if len(self._skew_ratio_history) < 3:
            return None

        prev = self._skew_ratio_history[-2]

        if prev > (1.0 + self.SKEW_FLIP_BUFFER) and ratio <= 1.0:
            msg = F.build(
                F.header(self.symbol, "IV Skew Flip — CALL→PUT", "🔄"),
                F.kv("ATM", f"{atm:.0f}  CE/PE ratio: {prev:.2f} → {ratio:.2f}"),
                F.kv_pair("CE LTP", f"{ce_ltp:.2f}", "PE LTP", f"{pe_ltp:.2f}"),
                F.signal("Market shifting to downside protection. <b>Bearish skew building.</b>"),
            )
            return ("SKEW_FLIP_BEARISH", msg)

        if prev < (1.0 - self.SKEW_FLIP_BUFFER) and ratio >= 1.0:
            msg = F.build(
                F.header(self.symbol, "IV Skew Flip — PUT→CALL", "🔄"),
                F.kv("ATM", f"{atm:.0f}  CE/PE ratio: {prev:.2f} → {ratio:.2f}"),
                F.kv_pair("CE LTP", f"{ce_ltp:.2f}", "PE LTP", f"{pe_ltp:.2f}"),
                F.signal("Market shifting to upside buying. <b>Bullish skew building.</b>"),
            )
            return ("SKEW_FLIP_BULLISH", msg)

        return None
