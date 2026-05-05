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
      - IV Skew Reversal:      IV skew (PE IV − CE IV) crosses zero → real skew flip alert.
                               Falls back to CE/PE LTP ratio when greeks absent (Zerodha path).
      - IV Trend:              ATM IV rising/falling consistently over ≥ 10 min (history-powered).
    """

    SNAPSHOT_INTERVAL_SEC  = 60      # Internal fallback snapshot frequency
    IV_EXPANDING_THRESHOLD = 0.04    # +4% straddle with flat spot = IV expanding
    IV_COMPRESS_THRESHOLD  = -0.05   # −5% straddle with flat spot = IV compressing
    SPOT_FLAT_PCT          = 0.001   # Spot must move < 0.1% to attribute change to IV
    RANGE_USED_PCT         = 0.75    # Alert when ≥ 75% of expected range is consumed
    # IV skew thresholds (skew = (PE IV − CE IV) × 100)
    SKEW_FLIP_BUFFER_IV    = 0.10    # IV skew must exceed ±0.10 pp before a flip is counted
    SKEW_FLIP_BUFFER_LTP   = 0.05    # Fallback LTP ratio buffer (Zerodha path)
    # IV trend thresholds
    IV_TREND_MINUTES       = 10      # Minimum minutes of history for IV trend check
    IV_TREND_RISE_PP       = 0.015   # ATM IV must rise ≥ 1.5 pp over window to alert
    IV_TREND_FALL_PP       = 0.010   # ATM IV must fall ≥ 1.0 pp over window to alert
    # Minimum straddle as % of spot — rejects partial ticks where only CE or PE has arrived.
    # A real ATM straddle is typically 0.5-3% of spot. Below 0.3% means one leg is missing.
    MIN_STRADDLE_SPOT_PCT  = 0.003

    def __init__(self, symbol: str):
        self.symbol = symbol

        # Internal fallback snapshot deque (used before history has enough data)
        self._snapshots: deque = deque(maxlen=12)      # (ts, straddle, spot)
        self._last_snapshot_ts: float = 0.0

        # Opening reference for implied-move range
        self._open_straddle: float = 0.0
        self._open_spot: float = 0.0

        # IV skew history — tracks iv_skew value (pp); fallback uses CE/PE LTP ratio
        self._skew_history: deque = deque(maxlen=6)    # float: iv_skew pp or LTP ratio

    # ── helpers ───────────────────────────────────────────────────────────────

    def _is_valid_straddle(self, straddle: float, spot: float) -> bool:
        """Reject partial straddle values where only one leg (CE or PE) has arrived."""
        if straddle <= 0 or spot <= 0:
            return False
        return (straddle / spot) >= self.MIN_STRADDLE_SPOT_PCT

    def _record_snapshot(self, straddle: float, spot: float):
        """Internal fallback — only used when no history object is passed."""
        if not self._is_valid_straddle(straddle, spot):
            return

        now = time.time()
        if now - self._last_snapshot_ts >= self.SNAPSHOT_INTERVAL_SEC:
            self._snapshots.append((now, straddle, spot))
            self._last_snapshot_ts = now

        if self._open_straddle == 0.0:
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
        if not self._is_valid_straddle(straddle, spot):
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
        # Reject if either straddle looks like a partial tick (one leg missing)
        if not self._is_valid_straddle(straddle, spot) or not self._is_valid_straddle(old_straddle, old_spot):
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
        Detects a flip in the IV skew direction.

        Sensibull path: uses agg["iv_skew"] = (PE IV − CE IV) × 100.
          Positive skew → puts more expensive (downside fear).
          Negative skew → calls more expensive (upside speculation).
          Fires when skew crosses zero after being on the other side.

        Zerodha fallback (no greeks): tracks CE/PE LTP ratio at ATM.
        """
        atm = agg.get("atm_strike")
        if not atm or spot <= 0:
            return None

        # ── Sensibull path: actual IV-based skew ─────────────────────────────
        ce_iv = agg.get("atm_iv_ce", 0.0) or 0.0
        pe_iv = agg.get("atm_iv_pe", 0.0) or 0.0
        if ce_iv > 0 and pe_iv > 0:
            iv_skew = agg.get("iv_skew", 0.0) or 0.0  # (pe_iv − ce_iv) × 100
            self._skew_history.append(iv_skew)

            if len(self._skew_history) < 3:
                return None

            prev_skew = self._skew_history[-2]
            buf = self.SKEW_FLIP_BUFFER_IV

            # Was bearish (put-skewed) → flipped to neutral/bullish
            if prev_skew > buf and iv_skew <= 0:
                msg = F.build(
                    F.header(self.symbol, "IV Skew Flip — PUT→NEUTRAL", "🔄"),
                    F.kv("ATM", f"{atm:.0f}  IV Skew: {prev_skew:+.2f} → {iv_skew:+.2f} pp"),
                    F.kv_pair("CE IV", f"{ce_iv:.1%}", "PE IV", f"{pe_iv:.1%}"),
                    F.signal("Put premium collapsing. <b>Downside fear easing — bullish tilt.</b>"),
                )
                return ("SKEW_FLIP_BULLISH", msg)

            # Was bullish/neutral → flipped to put-skewed (fear building)
            if prev_skew < -buf and iv_skew >= 0:
                msg = F.build(
                    F.header(self.symbol, "IV Skew Flip — CALL→PUT", "🔄"),
                    F.kv("ATM", f"{atm:.0f}  IV Skew: {prev_skew:+.2f} → {iv_skew:+.2f} pp"),
                    F.kv_pair("CE IV", f"{ce_iv:.1%}", "PE IV", f"{pe_iv:.1%}"),
                    F.signal("Put IV overtaking calls. <b>Downside protection being bought — bearish skew.</b>"),
                )
                return ("SKEW_FLIP_BEARISH", msg)

            return None

        # ── Zerodha fallback: CE/PE LTP ratio ────────────────────────────────
        atm_data = options_live.get(atm, {})
        ce_ltp   = atm_data.get("CE", {}).get("ltp", 0)
        pe_ltp   = atm_data.get("PE", {}).get("ltp", 0)
        if ce_ltp <= 0 or pe_ltp <= 0:
            return None

        ratio = ce_ltp / pe_ltp
        self._skew_history.append(ratio)

        if len(self._skew_history) < 3:
            return None

        prev = self._skew_history[-2]
        buf  = self.SKEW_FLIP_BUFFER_LTP

        if prev > (1.0 + buf) and ratio <= 1.0:
            msg = F.build(
                F.header(self.symbol, "Skew Flip — CALL→PUT", "🔄"),
                F.kv("ATM", f"{atm:.0f}  CE/PE LTP ratio: {prev:.2f} → {ratio:.2f}"),
                F.kv_pair("CE LTP", f"{ce_ltp:.2f}", "PE LTP", f"{pe_ltp:.2f}"),
                F.signal("Market shifting to downside protection. <b>Bearish skew building.</b>"),
            )
            return ("SKEW_FLIP_BEARISH", msg)

        if prev < (1.0 - buf) and ratio >= 1.0:
            msg = F.build(
                F.header(self.symbol, "Skew Flip — PUT→CALL", "🔄"),
                F.kv("ATM", f"{atm:.0f}  CE/PE LTP ratio: {prev:.2f} → {ratio:.2f}"),
                F.kv_pair("CE LTP", f"{ce_ltp:.2f}", "PE LTP", f"{pe_ltp:.2f}"),
                F.signal("Market shifting to upside buying. <b>Bullish skew building.</b>"),
            )
            return ("SKEW_FLIP_BULLISH", msg)

        return None

    def check_iv_trend(
        self,
        agg: dict,
        spot: float,
        history: "LiveOptionsHistory | None" = None,
    ) -> tuple[str, str] | None:
        """
        Detects a sustained ATM IV rise or fall over IV_TREND_MINUTES.
        Only fires when Sensibull greeks are present (atm_iv > 0 in history).
        Does NOT require spot to be flat — IV rising while spot moves is still notable.
        """
        if not history or history.minutes_of_data() < self.IV_TREND_MINUTES:
            return None

        series = history.atm_iv_series(self.IV_TREND_MINUTES)
        if len(series) < 3:
            return None

        slope = history.atm_iv_trend_slope(self.IV_TREND_MINUTES)
        if slope is None:
            return None

        first_iv = series[0]
        last_iv  = series[-1]
        change   = last_iv - first_iv   # absolute change in IV (e.g. 0.19 → 0.21 = +0.02)

        atm      = agg.get("atm_strike", "N/A")
        iv_skew  = agg.get("iv_skew", 0.0) or 0.0
        skew_str = f"Skew: {iv_skew:+.2f} pp" if iv_skew != 0 else ""

        if slope > 0 and change >= self.IV_TREND_RISE_PP:
            msg = F.build(
                F.header(self.symbol, f"IV Rising Trend ({self.IV_TREND_MINUTES} min)", "📈"),
                F.kv("ATM IV", f"{first_iv:.1%} → {last_iv:.1%}  ({change*100:+.1f} pp)"),
                F.kv_pair("ATM", f"{atm}", "Spot", f"{spot:.2f}  {skew_str}"),
                F.signal("Volatility building. <b>Event/directional move expected — avoid naked short premium.</b>"),
            )
            return ("IV_TREND_RISING", msg)

        if slope < 0 and change <= -self.IV_TREND_FALL_PP:
            msg = F.build(
                F.header(self.symbol, f"IV Falling Trend ({self.IV_TREND_MINUTES} min)", "📉"),
                F.kv("ATM IV", f"{first_iv:.1%} → {last_iv:.1%}  ({change*100:+.1f} pp)"),
                F.kv_pair("ATM", f"{atm}", "Spot", f"{spot:.2f}  {skew_str}"),
                F.signal("IV crush in progress. <b>Theta / sell-side favored — vega risk shrinking.</b>"),
            )
            return ("IV_TREND_FALLING", msg)

        return None
