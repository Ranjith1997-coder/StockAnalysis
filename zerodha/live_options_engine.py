"""
LiveOptionsEngine
─────────────────
Coordinator for real-time options analysis.
Called after every options_aggregate recomputation (≈ every 1 s per symbol).

Runs LiveOIAnalyser + LiveStraddleAnalyser checks, throttles alerts by cooldown,
and dispatches to the dedicated live-options Telegram channel.
"""

import time
import threading

from common.logging_util import logger
from common.Stock import Stock
from analyser.LiveOIAnalyser import LiveOIAnalyser
from analyser.LiveStraddleAnalyser import LiveStraddleAnalyser
from analyser.LiveOptionsHistory import LiveOptionsHistory
from notification.Notification import TELEGRAM_NOTIFICATIONS
from common.constants import LIVE_OPTIONS_INDICES


class LiveOptionsEngine:
    """
    Per-tick options analysis engine.

    Usage:
        engine = LiveOptionsEngine()
        # After options_aggregate is recomputed:
        engine.on_aggregate_updated(stock_obj, spot_price)
    """

    # Cooldown (seconds) between consecutive alerts of the same type per symbol.
    # Prevents flooding the Telegram channel during fast-moving markets.
    COOLDOWNS: dict[str, int] = {
        "PCR_CROSSOVER_BULLISH": 600,   # 10 min
        "PCR_CROSSOVER_BEARISH": 600,
        "PCR_EXTREME_PE":        900,   # 15 min
        "PCR_EXTREME_CE":        900,
        "CE_WALL_BREACH":        900,
        "PE_WALL_BREACH":        900,
        "IV_EXPANDING":          900,
        "IV_COMPRESSING":        900,
        "RANGE_BOUNDARY":        1800,  # 30 min
        "SKEW_FLIP_BULLISH":        600,
        "SKEW_FLIP_BEARISH":        600,
        "PCR_SUSTAINED_BULLISH":   1200,  # 20 min — slow-burn trend signal
        "PCR_SUSTAINED_BEARISH":   1200,
    }

    def __init__(self):
        # Per-symbol analyser instances (created lazily)
        self._oi_analysers:       dict[str, LiveOIAnalyser]       = {}
        self._straddle_analysers: dict[str, LiveStraddleAnalyser] = {}

        # Per-symbol history stores (375 snapshots = 1 trading day at 1-min intervals)
        self._histories: dict[str, LiveOptionsHistory] = {}

        # (symbol, alert_type) → last alert timestamp
        self._last_alert: dict[tuple[str, str], float] = {}

        self._lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────────

    def on_aggregate_updated(self, stock: Stock, spot: float):
        """
        Entry point — called from ZerodhaTickerManager after every aggregate recompute.
        Thread-safe: the tick processor thread calls this.
        """
        if spot <= 0:
            return

        symbol = stock.stock_symbol
        if symbol not in LIVE_OPTIONS_INDICES:
            return
        agg         = stock.options_aggregate
        options_live = stock.options_live

        with self._lock:
            try:
                history = self._get_history(symbol)
                history.record(agg, options_live, spot)
                self._run_oi_checks(symbol, agg, options_live, spot, history)
                self._run_straddle_checks(symbol, agg, options_live, spot, history)
            except Exception as exc:
                logger.error(f"[LiveOptionsEngine] {symbol}: {exc}")

    # ── internal ─────────────────────────────────────────────────────────────

    def _oi_analyser(self, symbol: str, strike_gap: float) -> LiveOIAnalyser:
        if symbol not in self._oi_analysers:
            self._oi_analysers[symbol] = LiveOIAnalyser(symbol, strike_gap)
        return self._oi_analysers[symbol]

    def _straddle_analyser(self, symbol: str) -> LiveStraddleAnalyser:
        if symbol not in self._straddle_analysers:
            self._straddle_analysers[symbol] = LiveStraddleAnalyser(symbol)
        return self._straddle_analysers[symbol]

    def _get_history(self, symbol: str) -> LiveOptionsHistory:
        if symbol not in self._histories:
            self._histories[symbol] = LiveOptionsHistory(symbol)
        return self._histories[symbol]

    def get_history(self, symbol: str) -> LiveOptionsHistory | None:
        """Public accessor — returns None if symbol has no history yet."""
        return self._histories.get(symbol)

    def _throttled(self, symbol: str, alert_type: str) -> bool:
        cooldown = self.COOLDOWNS.get(alert_type, 600)
        last     = self._last_alert.get((symbol, alert_type), 0.0)
        return (time.time() - last) < cooldown

    def _fire(self, symbol: str, alert_type: str, msg: str):
        self._last_alert[(symbol, alert_type)] = time.time()
        TELEGRAM_NOTIFICATIONS.send_live_options_notification(msg)
        logger.info(f"[LiveOptions] {symbol} {alert_type}: {msg[:60]}…")

    def _get_strike_gap(self, symbol: str) -> float:
        """Resolve strike gap from token registry if available, else default."""
        try:
            import common.shared as shared
            if shared.app_ctx.token_registry:
                return shared.app_ctx.token_registry.get_strike_gap(symbol)
        except Exception:
            pass
        return 50.0  # NIFTY default

    def _run_oi_checks(
        self, symbol: str, agg: dict, options_live: dict,
        spot: float, history: LiveOptionsHistory
    ):
        strike_gap = self._get_strike_gap(symbol)
        analyser   = self._oi_analyser(symbol, strike_gap)

        # PCR crossover — always call (updates internal PCR deque)
        result = analyser.check_pcr_crossover(agg)
        if result:
            alert_type, msg = result
            if not self._throttled(symbol, alert_type):
                self._fire(symbol, alert_type, msg)

        # PCR extreme — reads same pcr_history; call after crossover
        result = analyser.check_pcr_extreme(agg)
        if result:
            alert_type, msg = result
            if not self._throttled(symbol, alert_type):
                self._fire(symbol, alert_type, msg)

        # PCR sustained trend — history-powered, fires once per 20 min
        result = analyser.check_pcr_sustained_trend(history)
        if result:
            alert_type, msg = result
            if not self._throttled(symbol, alert_type):
                self._fire(symbol, alert_type, msg)

        # OI wall breach — history-assisted when ≥ 5 min of data available
        result = analyser.check_oi_wall_breach(agg, options_live, spot, history)
        if result:
            alert_type, msg = result
            if not self._throttled(symbol, alert_type):
                self._fire(symbol, alert_type, msg)

    def _run_straddle_checks(
        self, symbol: str, agg: dict, options_live: dict,
        spot: float, history: LiveOptionsHistory
    ):
        analyser = self._straddle_analyser(symbol)

        # IV change — history-assisted when ≥ 6 min of data available
        result = analyser.check_iv_change(agg, spot, history)
        if result:
            alert_type, msg = result
            if not self._throttled(symbol, alert_type):
                self._fire(symbol, alert_type, msg)

        # Implied move boundary — uses history[0] as true open reference
        result = analyser.check_implied_move_boundary(agg, spot, history)
        if result:
            alert_type, msg = result
            if not self._throttled(symbol, alert_type):
                self._fire(symbol, alert_type, msg)

        # IV skew reversal — tick-level, no history needed
        result = analyser.check_iv_skew_reversal(agg, options_live, spot)
        if result:
            alert_type, msg = result
            if not self._throttled(symbol, alert_type):
                self._fire(symbol, alert_type, msg)
