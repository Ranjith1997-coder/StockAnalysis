from common.logging_util import logger
import common.constants as constant
from common.scoring import (
    calculate_score, should_notify, format_score_message,
    NotificationPriority, ScoreResult
)
from analyser.MessageFormatter import MessageFormatter

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
            should_send, score_result = should_notify(stock.analysis, min_priority)
            stock.analysis["ScoreResult"] = score_result
            logger.debug(f"Score for {stock.stock_symbol}: {score_result.total_score} ({score_result.priority.value})")
            return should_send, score_result
        else:
            # Legacy behavior
            found_trend = stock.analysis["NoOfTrends"] >= constant.REQUIRED_TRENDS
            return found_trend, None
    
    def generate_analysis_message(self, stock, include_score=True):
        """Generate an HTML-formatted analysis message for Telegram."""
        score_result = stock.analysis.get("ScoreResult")

        priority_emoji = {
            NotificationPriority.LOW:      "📊",
            NotificationPriority.MEDIUM:   "📈",
            NotificationPriority.HIGH:     "🔥",
            NotificationPriority.CRITICAL: "🚨",
        }

        # ── Header ────────────────────────────────────────────────────────────
        if include_score and score_result:
            emoji   = priority_emoji.get(score_result.priority, "📊")
            chg_dot = "🟢" if stock.ltp_change_perc >= 0 else "🔴"
            message_parts = [
                f"{emoji} <b>[{score_result.priority.value}] {stock.stock_symbol}</b>: "
                f"<code>{stock.ltp:.2f}</code> {chg_dot} ({stock.ltp_change_perc:+.2f}%)",
                f"Score: <b>{score_result.total_score}</b> | "
                f"<b>{score_result.dominant_sentiment}</b> ({score_result.confidence_pct}%)",
            ]
            if score_result.alignment_bonus > 0:
                message_parts.append(
                    f"Alignment: {score_result.signal_alignment} "
                    f"(+{score_result.alignment_bonus} bonus)")
        else:
            message_parts = [
                f"📊 <b>{stock.stock_symbol}</b>: "
                f"<code>{stock.ltp:.2f}</code> ({stock.ltp_change_perc:.2f}%)",
            ]

        # ── Directional trends ────────────────────────────────────────────────
        for trend in ("BULLISH", "BEARISH"):
            if not stock.analysis[trend]:
                continue
            icon = "🟢" if trend == "BULLISH" else "🔴"
            message_parts.append(f"\n{icon} <b>{trend}</b>:")
            for analysis_type, data in stock.analysis[trend].items():
                message_parts.extend(MessageFormatter.format(analysis_type, data, trend))

        # ── Neutral signals ───────────────────────────────────────────────────
        if stock.analysis["NEUTRAL"]:
            message_parts.append("\n⚪ <b>NEUTRAL</b>:")
            for analysis_type, data in stock.analysis["NEUTRAL"].items():
                message_parts.extend(MessageFormatter.format(analysis_type, data, "NEUTRAL"))

        return "\n".join(message_parts)

