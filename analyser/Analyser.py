from common.logging_util import logger
import common.constants as constant
from common.scoring import (
    calculate_score, should_notify, format_score_message,
    NotificationPriority, ScoreResult
)

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
        # return message
        score_result = stock.analysis.get("ScoreResult")
        
        # Priority emoji mapping
        priority_emoji = {
            NotificationPriority.LOW: "📊",
            NotificationPriority.MEDIUM: "📈",
            NotificationPriority.HIGH: "🔥",
            NotificationPriority.CRITICAL: "🚨"
        }
        
        # Build header with score info
        if include_score and score_result:
            emoji = priority_emoji.get(score_result.priority, "📊")
            message_parts = [
                f"{emoji} [{score_result.priority.value}] {stock.stock_symbol}: {stock.ltp:.2f} ({stock.ltp_change_perc:+.2f}%)",
                f"Score: {score_result.total_score} | {score_result.dominant_sentiment} ({score_result.confidence_pct}%)",
            ]
            if score_result.alignment_bonus > 0:
                message_parts.append(f"Alignment: {score_result.signal_alignment} (+{score_result.alignment_bonus} bonus)")
        else:
            message_parts = [
                f"Stock: {stock.stock_symbol}: {stock.ltp:.2f} {stock.ltp_change_perc:.2f}%",
            ]
        for trend in ['BULLISH', 'BEARISH']:
            if stock.analysis[trend]:
                message_parts.append(f"{trend}:")
                for analysis_type, data in stock.analysis[trend].items():
                    if analysis_type == 'Volume':
                        message_parts.append(f" Volume {trend.lower()}: {data.Volume_rate_percent:.2f}%")
                        message_parts.append(f" Price {trend.lower()}: {data.price_change_percent:.2f}%")
                    elif analysis_type == 'RSI':
                        message_parts.append(f" RSI value: {data.value:.2f}")
                    elif analysis_type == 'rsi_crossover':
                        message_parts.append(f" RSI crossover: pv:{data.prev_value:.2f}, cv:{data.curr_value:.2f} ")
                    elif analysis_type == 'BollingerBand':
                        def format_bollinger_band(data, trend):
                            close_price = f"{data.close:.2f}"
                            comparison = '<' if trend == 'BULLISH' else '>'
                            band_type = 'Lower' if trend == 'BULLISH' else 'Upper'
                            band_value = f"{data.lower_band:.2f}" if trend == 'BULLISH' else f"{data.upper_band:.2f}"
                            return f" Bollinger Band: Price({close_price}) {comparison} {band_type}_band ({band_value})"
                        message_parts.append(format_bollinger_band(data, trend))
                    elif analysis_type == 'Single_candle_stick_pattern':
                        message_parts.append(f" Single Candle stick Pattern: {data}")
                    elif analysis_type == 'Double_candle_stick_pattern':
                        message_parts.append(f" Double Candle stick Pattern: {data}")  
                    elif analysis_type == "Triple_candle_stick_pattern":
                        message_parts.append(f" Triple Candle stick Pattern: {data}")
                    elif analysis_type == 'FUTURE_ACTION':
                        message_parts.append(f" Futures action: {data.action}, p%:{data.price_percentage:.2f}, oi%:{data.oi_percentage:.2f}")
                    elif analysis_type == 'vwap_deviation':
                        message_parts.append(f" VWAP: Close({data.close:.2f}) {'<' if trend == 'BULLISH' else '>'} VWAP({data.vwap:.2f}) DEVIATION: {data.deviation:.2f}%")
                        message_parts.append(f"   Intervals {'below' if trend == 'BULLISH' else 'above'} VWAP: {data.vwap_days}")
                    elif analysis_type == 'MACD':
                        message_parts.append(f" MACD : {data}")
                    elif analysis_type == 'BUY_SELL':
                        message_parts.append(f" BUY_SELL : buy_quantity: {data.buy_quantity:.2f} {'>' if trend == 'BULLISH' else '<'} sell_quantity: {data.sell_quantity:.2f} ")
                    elif analysis_type == 'FUTURE_BREAKOUT_PATTERN':
                        message_parts.append(f" FUTURE_BREAKOUT_PATTERN : {data.pattern}")
                    elif analysis_type == 'EMA_CROSSOVER':
                        message_parts.append(f" EMA_CROSSOVER :{data.direction}, fast_ema: {data.fast_ema:.2f} {'>' if trend == 'BULLISH' else '<'} slow_ema: {data.slow_ema:.2f} ")
                    elif analysis_type == 'PCR_EXTREME':
                        message_parts.append(f" PCR_EXTREME : {data.zone} PCR={data.pcr_value:.3f} - {data.signal}")
                    elif analysis_type == 'PCR_BIAS':
                        message_parts.append(f" PCR_BIAS : {data.bias} PCR={data.total_pcr:.3f}")
                    elif analysis_type == 'PCR_TREND':
                        message_parts.append(f" PCR_TREND : {data.trend} PCR={data.pcr_current:.3f} Change={data.pcr_change_pct:.2f}%")
                    elif analysis_type == 'MAX_PAIN':
                        message_parts.append(f" MAX_PAIN : Price={data.current_price:.2f} MaxPain={data.max_pain_strike:.2f} Dev={data.deviation_pct:+.2f}% ({data.signal_strength})")
                        if data.pcr:
                            message_parts.append(f"   Expiry={data.expiry} PCR={data.pcr:.3f} Type={data.max_pain_type}")
                    elif analysis_type == 'MAX_PAIN_ALIGNMENT':
                        message_parts.append(f" MAX_PAIN_ALIGNMENT : {data.alignment} MaxPain={data.max_pain_type} PCR={data.pcr_type}")
                        message_parts.append(f"   {data.signal}")

        if stock.analysis['NEUTRAL']:
            message_parts.append("NEUTRAL:")
            for analysis_type, data in stock.analysis['NEUTRAL'].items():
                if analysis_type == '52-week-high':
                    message_parts.append("  Price at 52 WEEK HIGH")
                elif analysis_type == '52-week-low':
                    message_parts.append("  Price at 52 WEEK LOW")
                elif analysis_type == 'ATR':
                    message_parts.append(f" ATR : {data.atr_value:.2f} {data.atr_percentage:.2f}% ")
                elif analysis_type == 'IV_SPIKE':
                    if isinstance(data, list):
                        for iv_spike in data:
                            message_parts.append(f" IV_SPIKE : {iv_spike.expiry} {iv_spike.iv_change:.2f}% ")
                    else:
                        message_parts.append(f" IV_SPIKE : {data.expiry} {data.iv_change:.2f}% ")
                elif analysis_type == 'IV_TREND':
                    if isinstance(data, list):
                        for iv_trend in data:
                            message_parts.append(f" IV_TREND : {iv_trend.expiry} {iv_trend.trend} {iv_trend.iv_change_pct:.2f}%")
                    else:
                        message_parts.append(f" IV_TREND : {data.expiry} {data.trend} {data.iv_change_pct:.2f}%")
                elif analysis_type == 'FUTURE_PVO_PATTERN':
                    if isinstance(data, list):
                        for fut_data in data:
                            message_parts.append(f" FuturesPVOPattern : {fut_data.pattern} p:{fut_data.price_pct:.2f}%, v:{fut_data.vol_pct:.2f}%, oi:{fut_data.oi_pct:.2f}%")
                    else:
                        message_parts.append(f" FuturesPVOPattern : {data.pattern} p:{data.price_pct:.2f}%, v:{data.vol_pct:.2f}%, oi:{data.oi_pct:.2f}%")
                elif analysis_type == 'PCR_DIVERGENCE':
                    message_parts.append(f" PCR_DIVERGENCE : Near={data.near_month_pcr:.3f} Far={data.far_month_pcr:.3f} Div={data.divergence:.3f} - {data.signal}")
                elif analysis_type == 'MAX_PAIN_TREND':
                    message_parts.append(f" MAX_PAIN_TREND : {data.trend} Curr={data.curr_max_pain:.2f} Prev={data.prev_max_pain:.2f}")
                    message_parts.append(f"   Expiry={data.expiry} CurrDev={data.curr_deviation:+.2f}% PrevDev={data.prev_deviation:+.2f}%")
                elif analysis_type == 'MAX_PAIN_ALIGNMENT':
                    message_parts.append(f" MAX_PAIN_ALIGNMENT : {data.alignment} MaxPain={data.max_pain_type} PCR={data.pcr_type}")
                    message_parts.append(f"   {data.signal}")

        return "\n".join(message_parts)

