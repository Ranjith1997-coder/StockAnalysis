from common.logging_util import logger
import common.constants as constant
from common.scoring import (
    calculate_score, should_notify, format_score_message,
    NotificationPriority, ScoreResult
)
from analyser.MessageFormatter import MessageFormatter
from intelligence.signal import Signal, Direction, Layer, SignalStrength, weight_to_strength

class BaseAnalyzer():
    def __init__(self) -> None:
        self._intraday_methods = [
            getattr(self, name) for name in dir(self)
            if callable(getattr(self, name)) and getattr(getattr(self, name), "_is_intraday", False)
        ]
        self._positional_methods = [
            getattr(self, name) for name in dir(self)
            if callable(getattr(self, name)) and getattr(getattr(self, name), "_is_positional", False)
        ]
        self._intraday_index_methods = [
            getattr(self, name) for name in dir(self)
            if callable(getattr(self, name)) and getattr(getattr(self, name), "_is_index_intraday", False)
        ]
        self._positional_index_methods = [
            getattr(self, name) for name in dir(self)
            if callable(getattr(self, name)) and getattr(getattr(self, name), "_is_index_positional", False)
        ]
                
    @staticmethod
    def intraday(func):
        func._is_intraday = True
        return func

    @staticmethod
    def positional(func):
        func._is_positional = True
        return func

    @staticmethod
    def both(func):
        func._is_intraday = True
        func._is_positional = True
        return func
    
    @staticmethod
    def index_intraday(func):
        func._is_index_intraday = True
        return func

    @staticmethod
    def index_positional(func):
        func._is_index_positional = True
        return func

    @staticmethod
    def index_both(func):
        func._is_index_intraday = True
        func._is_index_positional = True
        return func

    
    def run_all_intraday_analyses(self, stock):
        found_trend = False
        for method in self._intraday_methods:
            found_trend |= method(stock)  # Call each method
        return found_trend
    
    def run_all_positional_analyses(self, stock):
        found_trend = False
        for method in self._positional_methods:
            found_trend |= method(stock)  # Call each method
        return found_trend
    
    def run_all_index_intraday_analyses(self, stock):
        found_trend = False
        for method in self._intraday_index_methods:
            found_trend |= method(stock)  # Call each method
        return found_trend
    
    def run_all_index_positional_analyses(self, stock):
        found_trend = False
        for method in self._positional_index_methods:
            found_trend |= method(stock)  # Call each method
        return found_trend



class AnalyserOrchestrator:
    def __init__(self):
        self.analysers = []

    def register(self, analyser: BaseAnalyzer):
        if not isinstance(analyser, BaseAnalyzer):
            raise TypeError("Analyser must inherit from BaseAnalyser")
        self.analysers.append(analyser)
    
    def reset_all_constants(self, is_index = False):
        for analyser in self.analysers:
            analyser.reset_constants()

    @staticmethod
    def _emit_signals(stock, layer: Layer):
        """Extract signals from stock.analysis and emit to SignalBus."""
        import common.shared as shared
        bus = shared.app_ctx.signal_bus
        if not bus:
            return
        for sentiment in ("BULLISH", "BEARISH"):
            direction = Direction[sentiment]
            for analysis_type in stock.analysis.get(sentiment, {}):
                weight = constant.ANALYSIS_WEIGHTS.get(
                    analysis_type, constant.ANALYSIS_WEIGHTS.get("DEFAULT", 10)
                )
                bus.emit(Signal(
                    symbol=stock.stock_symbol,
                    direction=direction,
                    source=analysis_type.lower(),
                    layer=layer,
                    strength=weight_to_strength(weight),
                ))

    def run_all_intraday(self, stock, index = False, use_scoring = True, min_priority = NotificationPriority.LOW):
        """
        Run all intraday analyses for a stock.
        
        Args:
            stock: Stock object to analyze
            index: Whether this is an index
            use_scoring: If True, use the new scoring system. If False, use legacy REQUIRED_TRENDS
            min_priority: Minimum priority level to consider as a valid trend (only used when use_scoring=True)
        
        Returns:
            Tuple of (found_trend: bool, score_result: ScoreResult or None)
        """
        logger.debug("Starting all analyses for stock {}".format(stock.stock_symbol))
        for analyser in self.analysers:
            if index:
                analyser.run_all_index_intraday_analyses(stock)
            else:
                analyser.run_all_intraday_analyses(stock)
        logger.debug("All analyses complete for stock {}".format(stock.stock_symbol))
        
        if use_scoring:
            should_send, score_result = should_notify(stock.analysis, min_priority)
            stock.analysis["ScoreResult"] = score_result
            logger.debug(f"Score for {stock.stock_symbol}: {score_result.total_score} ({score_result.priority.value})")
            # Emit to SignalBus if the score threshold is met OR a composite setup
            # bypassed the score gate via PRIORITY_OVERRIDE (composite setups have
            # weight=0 so their raw score never reaches MIN_NOTIFICATION_SCORE).
            if (score_result.total_score >= constant.MIN_NOTIFICATION_SCORE
                    or stock.analysis.get("PRIORITY_OVERRIDE") is not None):
                self._emit_signals(stock, Layer.INTRADAY)
            else:
                logger.debug(f"[SignalBus] Skipping intraday emit for {stock.stock_symbol} — score {score_result.total_score} < {constant.MIN_NOTIFICATION_SCORE}")
            return should_send, score_result
        else:
            # Legacy behavior
            found_trend = stock.analysis["NoOfTrends"] >= constant.REQUIRED_TRENDS
            return found_trend, None

    def run_all_positional(self, stock, index = False, use_scoring = True, min_priority = NotificationPriority.LOW):
        """
        Run all positional analyses for a stock.
        
        Args:
            stock: Stock object to analyze
            index: Whether this is an index
            use_scoring: If True, use the new scoring system. If False, use legacy REQUIRED_TRENDS
            min_priority: Minimum priority level to consider as a valid trend (only used when use_scoring=True)
        
        Returns:
            Tuple of (found_trend: bool, score_result: ScoreResult or None)
        """
        logger.debug("Starting all analyses for stock {}".format(stock.stock_symbol))
        for analyser in self.analysers:
            if index:
                analyser.run_all_index_positional_analyses(stock)
            else:
                analyser.run_all_positional_analyses(stock)
        logger.debug("All analyses complete for stock {}".format(stock.stock_symbol))
        
        if use_scoring:
            pos_threshold = constant.MIN_NOTIFICATION_SCORE_POSITIONAL
            should_send, score_result = should_notify(stock.analysis, min_priority, min_score=pos_threshold)
            stock.analysis["ScoreResult"] = score_result
            logger.debug(f"Score for {stock.stock_symbol}: {score_result.total_score} ({score_result.priority.value})")
            if score_result.total_score >= pos_threshold:
                self._emit_signals(stock, Layer.POSITIONAL)
            else:
                logger.debug(f"[SignalBus] Skipping positional emit for {stock.stock_symbol} — score {score_result.total_score} < {pos_threshold}")
            return should_send, score_result
        else:
            # Legacy behavior
            found_trend = stock.analysis["NoOfTrends"] >= constant.REQUIRED_TRENDS
            return found_trend, None

    def generate_analysis_message(self, stock, include_score=True):
        """
        Generate an HTML-formatted analysis message for Telegram.

        Mutual Exclusion (Winner-Takes-All) rule
        ─────────────────────────────────────────
        When OptionSellerCompositeAnalyser has written a PRIORITY_OVERRIDE, a composite
        trade setup has fired.  In that case we return ONLY the relevant trade card —
        the standard BULLISH/BEARISH/NEUTRAL indicator loops are skipped entirely.
        This eliminates noise: the trader receives one clean, actionable card with no
        RSI/MACD/PCR clutter underneath it.

        Priority of composite keys (highest urgency rendered first):
            GAMMA_TRAP        → CRITICAL  (kill-switch)
            SKEW_FADE_SETUP   → HIGH      (directional credit spread)
            RANGE_BOUND_SETUP → HIGH      (iron condor / strangle)

        If NO override is present the method falls through to the legacy loop which
        renders every individual indicator signal as before.
        """
        score_result = stock.analysis.get("ScoreResult")

        priority_emoji = {
            NotificationPriority.LOW:      "📊",
            NotificationPriority.MEDIUM:   "📈",
            NotificationPriority.HIGH:     "🔥",
            NotificationPriority.CRITICAL: "🚨",
        }

        # ── Shared header builder (used by both paths) ────────────────────────
        def _header() -> list[str]:
            if include_score and score_result:
                emoji   = priority_emoji.get(score_result.priority, "📊")
                chg_dot = "🟢" if (stock.ltp_change_perc or 0) >= 0 else "🔴"
                parts = [
                    f"{emoji} <b>[{score_result.priority.value}] {stock.stock_symbol}</b>: "
                    f"<code>{stock.ltp:.2f}</code> {chg_dot} ({stock.ltp_change_perc:+.2f}%)",
                ]
                # Composite path: omit the raw score line — the trade card is the signal.
                # Standard path (no override): keep the full score / alignment lines.
                if stock.analysis.get("PRIORITY_OVERRIDE") is None:
                    parts.append(
                        f"Score: <b>{score_result.total_score}</b> | "
                        f"<b>{score_result.dominant_sentiment}</b> ({score_result.confidence_pct}%)"
                    )
                    if score_result.alignment_bonus > 0:
                        parts.append(
                            f"Alignment: {score_result.signal_alignment} "
                            f"(+{score_result.alignment_bonus} bonus)"
                        )
                return parts
            return [
                f"📊 <b>{stock.stock_symbol}</b>: "
                f"<code>{stock.ltp:.2f}</code> ({stock.ltp_change_perc:.2f}%)",
            ]

        # ── Winner-Takes-All: composite path ─────────────────────────────────
        # Checked in priority order: Gamma Trap first (CRITICAL), then the HIGH setups.
        _COMPOSITE_KEYS = ("GAMMA_TRAP", "SKEW_FADE_SETUP", "RANGE_BOUND_SETUP")

        if stock.analysis.get("PRIORITY_OVERRIDE") is not None:
            neutral = stock.analysis.get("NEUTRAL", {})
            for composite_key in _COMPOSITE_KEYS:
                data = neutral.get(composite_key)
                if data is not None:
                    message_parts = _header()
                    message_parts.extend(
                        MessageFormatter.format(composite_key, data, "NEUTRAL")
                    )
                    return "\n".join(message_parts)
            # PRIORITY_OVERRIDE was set but no matching key found in NEUTRAL —
            # fall through to the standard path rather than returning an empty message.
            logger.warning(
                f"[generate_analysis_message] {stock.stock_symbol}: PRIORITY_OVERRIDE set "
                f"but no composite key found in NEUTRAL — falling back to standard render"
            )

        # ── Standard path: render all individual indicator signals ────────────
        message_parts = _header()

        for trend in ("BULLISH", "BEARISH"):
            if not stock.analysis[trend]:
                continue
            icon = "🟢" if trend == "BULLISH" else "🔴"
            message_parts.append(f"\n{icon} <b>{trend}</b>:")
            for analysis_type, data in stock.analysis[trend].items():
                message_parts.extend(MessageFormatter.format(analysis_type, data, trend))

        if stock.analysis["NEUTRAL"]:
            message_parts.append("\n⚪ <b>NEUTRAL</b>:")
            for analysis_type, data in stock.analysis["NEUTRAL"].items():
                # Skip internal composite infrastructure keys — not user-facing
                if analysis_type in ("GAMMA_TRAP_ACTIVE",):
                    continue
                message_parts.extend(MessageFormatter.format(analysis_type, data, "NEUTRAL"))

        return "\n".join(message_parts)

