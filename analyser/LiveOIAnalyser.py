from collections import deque
from common.logging_util import logger
from analyser.LiveAlertFormatter import F

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from analyser.LiveOptionsHistory import LiveOptionsHistory


class LiveOIAnalyser:
    """
    Real-time OI analysis using live WebSocket tick data from options_live / options_aggregate.

    Strategies:
      - PCR Crossover:     PCR crosses 1.0 → directional bias shift
      - PCR Extreme:       PCR enters extreme zone (<0.7 or >1.3) → contrarian signal
      - PCR Sustained:     PCR trending in one direction for ≥15 min → confirmed bias
      - OI Wall Breach:    Spot near max OI wall while wall OI falls → breakout setup
                           Uses 15-min history when available, falls back to 5-tick deque
    """

    PCR_CROSS_LEVEL      = 1.0    # Directional pivot
    PCR_EXTREME_PE       = 1.3    # Excess put buying  — contrarian bearish
    PCR_EXTREME_CE       = 0.7    # Excess call buying — contrarian bullish
    PCR_SUSTAINED_MIN    = 15     # Minutes of sustained PCR trend needed
    OI_WALL_PROXIMITY    = 0.6    # Alert when within 0.6 × strike_gap of a wall
    OI_WEAKENING_SHORT   = -0.03  # −3% over last 5 ticks (fast signal)
    OI_WEAKENING_HISTORY = -0.05  # −5% over last 15 min (confirmed signal)

    def __init__(self, symbol: str, strike_gap: float):
        self.symbol    = symbol
        self.strike_gap = strike_gap

        # PCR history — shared by crossover + extreme + sustained checks
        self._pcr_history: deque = deque(maxlen=10)

        # Short-term wall OI deques (fallback when history has < 3 min of data)
        self._last_ce_wall: float | None = None
        self._last_pe_wall: float | None = None
        self._ce_wall_oi_history: deque = deque(maxlen=5)
        self._pe_wall_oi_history: deque = deque(maxlen=5)

    # ── state update ──────────────────────────────────────────────────────────

    def update_pcr(self, pcr: float):
        if pcr > 0:
            self._pcr_history.append(pcr)

    # ── signal checks ─────────────────────────────────────────────────────────

    def check_pcr_crossover(self, agg: dict) -> tuple[str, str] | None:
        """
        Fires when PCR crosses 1.0.
        Always calls update_pcr() so internal state stays current.
        """
        pcr = agg.get("live_pcr", 0)
        self.update_pcr(pcr)

        if len(self._pcr_history) < 3:
            return None

        prev = self._pcr_history[-2]
        curr = self._pcr_history[-1]
        ce_oi = agg.get("total_ce_oi", 0)
        pe_oi = agg.get("total_pe_oi", 0)

        if prev < self.PCR_CROSS_LEVEL <= curr:
            msg = F.build(
                F.header(self.symbol, "PCR Crossover → BULLISH", "📈"),
                F.kv("PCR", f"{prev:.3f} → {curr:.3f}  (crossed above 1.0)"),
                F.kv_pair("CE OI", f"{ce_oi:,.0f}", "PE OI", f"{pe_oi:,.0f}"),
                F.signal("PE writers building. Bias shifts <b>UP</b>."),
            )
            return ("PCR_CROSSOVER_BULLISH", msg)

        if prev >= self.PCR_CROSS_LEVEL > curr:
            msg = F.build(
                F.header(self.symbol, "PCR Crossover → BEARISH", "📉"),
                F.kv("PCR", f"{prev:.3f} → {curr:.3f}  (dropped below 1.0)"),
                F.kv_pair("CE OI", f"{ce_oi:,.0f}", "PE OI", f"{pe_oi:,.0f}"),
                F.signal("CE writers building. Bias shifts <b>DOWN</b>."),
            )
            return ("PCR_CROSSOVER_BEARISH", msg)

        return None

    def check_pcr_extreme(self, agg: dict) -> tuple[str, str] | None:
        """
        Fires when PCR enters an extreme zone.
        Call after check_pcr_crossover() (which updates history).
        """
        if len(self._pcr_history) < 2:
            return None

        prev = self._pcr_history[-2]
        curr = self._pcr_history[-1]

        if prev < self.PCR_EXTREME_PE <= curr:
            msg = F.build(
                F.header(self.symbol, "PCR Extreme — PUT HEAVY", "⚠️"),
                F.kv("PCR", f"{curr:.3f}  (≥ {self.PCR_EXTREME_PE})"),
                F.signal("Excessive put buying. Contrarian signal: possible <b>bounce</b>."),
            )
            return ("PCR_EXTREME_PE", msg)

        if prev > self.PCR_EXTREME_CE >= curr:
            msg = F.build(
                F.header(self.symbol, "PCR Extreme — CALL HEAVY", "⚠️"),
                F.kv("PCR", f"{curr:.3f}  (≤ {self.PCR_EXTREME_CE})"),
                F.signal("Excessive call buying. Contrarian signal: possible <b>reversal down</b>."),
            )
            return ("PCR_EXTREME_CE", msg)

        return None

    def check_pcr_sustained_trend(self, history: "LiveOptionsHistory") -> tuple[str, str] | None:
        """
        NEW — requires history.
        Fires when PCR has been consistently rising or falling for ≥ PCR_SUSTAINED_MIN minutes.
        Confirms that the crossover / extreme signal is holding, not a noise spike.
        """
        if history.minutes_of_data() < self.PCR_SUSTAINED_MIN:
            return None

        slope = history.pcr_trend_slope(self.PCR_SUSTAINED_MIN)
        if slope is None:
            return None

        series = history.pcr_series(self.PCR_SUSTAINED_MIN)
        if len(series) < 3:
            return None

        first_pcr = series[0]
        last_pcr  = series[-1]
        change    = last_pcr - first_pcr

        # Meaningful sustained rise (≥ +0.10 PCR change over the window)
        if slope > 0 and change >= 0.10 and last_pcr >= self.PCR_CROSS_LEVEL:
            ce_oi_chg = history.ce_oi_change_pct(self.PCR_SUSTAINED_MIN)
            pe_oi_chg = history.pe_oi_change_pct(self.PCR_SUSTAINED_MIN)
            ce_str = f"{ce_oi_chg:+.1f}%" if ce_oi_chg is not None else "N/A"
            pe_str = f"{pe_oi_chg:+.1f}%" if pe_oi_chg is not None else "N/A"
            msg = F.build(
                F.header(self.symbol, f"PCR Sustained BULLISH Trend ({self.PCR_SUSTAINED_MIN} min)", "📈"),
                F.kv("PCR", f"{first_pcr:.3f} → {last_pcr:.3f}"),
                F.kv_pair("CE OI chg", ce_str, "PE OI chg", pe_str),
                F.signal("Consistent put writing. <b>Bullish bias confirmed.</b>"),
            )
            return ("PCR_SUSTAINED_BULLISH", msg)

        if slope < 0 and change <= -0.10 and last_pcr < self.PCR_CROSS_LEVEL:
            ce_oi_chg = history.ce_oi_change_pct(self.PCR_SUSTAINED_MIN)
            pe_oi_chg = history.pe_oi_change_pct(self.PCR_SUSTAINED_MIN)
            ce_str = f"{ce_oi_chg:+.1f}%" if ce_oi_chg is not None else "N/A"
            pe_str = f"{pe_oi_chg:+.1f}%" if pe_oi_chg is not None else "N/A"
            msg = F.build(
                F.header(self.symbol, f"PCR Sustained BEARISH Trend ({self.PCR_SUSTAINED_MIN} min)", "📉"),
                F.kv("PCR", f"{first_pcr:.3f} → {last_pcr:.3f}"),
                F.kv_pair("CE OI chg", ce_str, "PE OI chg", pe_str),
                F.signal("Consistent call writing. <b>Bearish bias confirmed.</b>"),
            )
            return ("PCR_SUSTAINED_BEARISH", msg)

        return None

    def check_oi_wall_breach(
        self,
        agg: dict,
        options_live: dict,
        spot: float,
        history: "LiveOptionsHistory | None" = None,
    ) -> tuple[str, str] | None:
        """
        Fires when spot is near an OI wall AND that wall is weakening.

        With history (≥ 5 min of data): uses 15-min wall OI trend — more reliable.
        Without history (early in session): falls back to 5-tick deque.
        """
        if spot <= 0:
            return None

        ce_wall: float | None = agg.get("max_oi_ce_strike")
        pe_wall: float | None = agg.get("max_oi_pe_strike")
        if not ce_wall or not pe_wall:
            return None

        threshold = self.strike_gap * self.OI_WALL_PROXIMITY

        # ── History-based detection (preferred) ──────────────────────────────
        if history and history.minutes_of_data() >= 5:
            return self._wall_breach_from_history(
                history, ce_wall, pe_wall, spot, threshold
            )

        # ── Fallback: short deque ─────────────────────────────────────────────
        ce_wall_oi = options_live.get(ce_wall, {}).get("CE", {}).get("oi", 0)
        pe_wall_oi = options_live.get(pe_wall, {}).get("PE", {}).get("oi", 0)

        if ce_wall != self._last_ce_wall:
            self._ce_wall_oi_history.clear()
            self._last_ce_wall = ce_wall
        if pe_wall != self._last_pe_wall:
            self._pe_wall_oi_history.clear()
            self._last_pe_wall = pe_wall

        self._ce_wall_oi_history.append(ce_wall_oi)
        self._pe_wall_oi_history.append(pe_wall_oi)

        if len(self._ce_wall_oi_history) < 3:
            return None

        return self._wall_breach_from_deque(
            ce_wall, pe_wall, spot, threshold
        )

    def _wall_breach_from_history(
        self, history, ce_wall, pe_wall, spot, threshold
    ) -> tuple[str, str] | None:
        """Use 15-min history for a confirmed wall-weakening signal."""
        dist_ce = ce_wall - spot
        if 0 < dist_ce <= threshold:
            trend = history.wall_oi_trend("CE", 15)
            if trend:
                old_oi, new_oi = trend
                if old_oi > 0:
                    chg_pct = (new_oi - old_oi) / old_oi
                    if chg_pct <= self.OI_WEAKENING_HISTORY:
                        msg = F.build(
                            F.header(self.symbol, "RESISTANCE WALL WEAKENING  (15-min confirmed)", "🚀"),
                            F.kv("CE wall", f"{ce_wall:.0f}  OI: {old_oi:,.0f} → {new_oi:,.0f}  ({chg_pct*100:.1f}%)"),
                            F.kv_pair("Spot", f"{spot:.2f}", "Distance", f"{dist_ce:.0f} pts"),
                            F.signal(f"Writers covering for 15 min. <b>Breakout above {ce_wall:.0f} high conviction.</b>"),
                        )
                        return ("CE_WALL_BREACH", msg)

        dist_pe = spot - pe_wall
        if 0 < dist_pe <= threshold:
            trend = history.wall_oi_trend("PE", 15)
            if trend:
                old_oi, new_oi = trend
                if old_oi > 0:
                    chg_pct = (new_oi - old_oi) / old_oi
                    if chg_pct <= self.OI_WEAKENING_HISTORY:
                        msg = F.build(
                            F.header(self.symbol, "SUPPORT WALL WEAKENING  (15-min confirmed)", "💥"),
                            F.kv("PE wall", f"{pe_wall:.0f}  OI: {old_oi:,.0f} → {new_oi:,.0f}  ({chg_pct*100:.1f}%)"),
                            F.kv_pair("Spot", f"{spot:.2f}", "Distance", f"{dist_pe:.0f} pts"),
                            F.signal(f"Put writers covering for 15 min. <b>Breakdown below {pe_wall:.0f} high conviction.</b>"),
                        )
                        return ("PE_WALL_BREACH", msg)

        return None

    def _wall_breach_from_deque(
        self, ce_wall, pe_wall, spot, threshold
    ) -> tuple[str, str] | None:
        """Fast fallback using the 5-tick deque (first few minutes of session)."""
        dist_ce = ce_wall - spot
        if 0 < dist_ce <= threshold and self._ce_wall_oi_history[0] > 0:
            chg_pct = (
                (self._ce_wall_oi_history[-1] - self._ce_wall_oi_history[0])
                / self._ce_wall_oi_history[0]
            )
            if chg_pct <= self.OI_WEAKENING_SHORT:
                msg = F.build(
                    F.header(self.symbol, "RESISTANCE WALL WEAKENING", "🚀"),
                    F.kv_pair("CE wall", f"{ce_wall:.0f}", "OI chg", f"{chg_pct*100:.1f}%"),
                    F.kv_pair("Spot", f"{spot:.2f}", "Distance", f"{dist_ce:.0f} pts"),
                    F.signal(f"Writers covering. <b>Breakout above {ce_wall:.0f} likely.</b>"),
                )
                return ("CE_WALL_BREACH", msg)

        dist_pe = spot - pe_wall
        if 0 < dist_pe <= threshold and self._pe_wall_oi_history[0] > 0:
            chg_pct = (
                (self._pe_wall_oi_history[-1] - self._pe_wall_oi_history[0])
                / self._pe_wall_oi_history[0]
            )
            if chg_pct <= self.OI_WEAKENING_SHORT:
                msg = F.build(
                    F.header(self.symbol, "SUPPORT WALL WEAKENING", "💥"),
                    F.kv_pair("PE wall", f"{pe_wall:.0f}", "OI chg", f"{chg_pct*100:.1f}%"),
                    F.kv_pair("Spot", f"{spot:.2f}", "Distance", f"{dist_pe:.0f} pts"),
                    F.signal(f"Put writers covering. <b>Breakdown below {pe_wall:.0f} likely.</b>"),
                )
                return ("PE_WALL_BREACH", msg)

        return None
