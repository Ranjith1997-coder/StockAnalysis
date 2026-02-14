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
        """Generate an HTML-formatted analysis message for Telegram."""
        score_result = stock.analysis.get("ScoreResult")

        # Priority emoji mapping
        priority_emoji = {
            NotificationPriority.LOW: "\U0001F4CA",
            NotificationPriority.MEDIUM: "\U0001F4C8",
            NotificationPriority.HIGH: "\U0001F525",
            NotificationPriority.CRITICAL: "\U0001F6A8"
        }

        # Build header with score info
        if include_score and score_result:
            emoji = priority_emoji.get(score_result.priority, "\U0001F4CA")
            chg_dot = "\U0001F7E2" if stock.ltp_change_perc >= 0 else "\U0001F534"
            message_parts = [
                f"{emoji} <b>[{score_result.priority.value}] {stock.stock_symbol}</b>: "
                f"<code>{stock.ltp:.2f}</code> {chg_dot} ({stock.ltp_change_perc:+.2f}%)",
                f"Score: <b>{score_result.total_score}</b> | "
                f"<b>{score_result.dominant_sentiment}</b> ({score_result.confidence_pct}%)",
            ]
            if score_result.alignment_bonus > 0:
                message_parts.append(
                    f"Alignment: {score_result.signal_alignment} (+{score_result.alignment_bonus} bonus)")
        else:
            message_parts = [
                f"\U0001F4CA <b>{stock.stock_symbol}</b>: "
                f"<code>{stock.ltp:.2f}</code> ({stock.ltp_change_perc:.2f}%)",
            ]

        for trend in ['BULLISH', 'BEARISH']:
            if stock.analysis[trend]:
                trend_icon = "\U0001F7E2" if trend == 'BULLISH' else "\U0001F534"
                message_parts.append(f"\n{trend_icon} <b>{trend}</b>:")
                for analysis_type, data in stock.analysis[trend].items():
                    if analysis_type == 'Volume':
                        message_parts.append(f"  Volume {trend.lower()}: <code>{data.Volume_rate_percent:.2f}%</code>")
                        message_parts.append(f"  Price {trend.lower()}: <code>{data.price_change_percent:.2f}%</code>")
                    elif analysis_type == 'RSI':
                        message_parts.append(f"  RSI: <code>{data.value:.2f}</code>")
                    elif analysis_type == 'rsi_crossover':
                        message_parts.append(f"  RSI crossover: <code>{data.prev_value:.2f} \u2192 {data.curr_value:.2f}</code>")
                    elif analysis_type == 'BollingerBand':
                        comparison = '&lt;' if trend == 'BULLISH' else '&gt;'
                        band_type = 'Lower' if trend == 'BULLISH' else 'Upper'
                        band_value = f"{data.lower_band:.2f}" if trend == 'BULLISH' else f"{data.upper_band:.2f}"
                        message_parts.append(f"  BB: Price(<code>{data.close:.2f}</code>) {comparison} {band_type}(<code>{band_value}</code>)")
                    elif analysis_type == 'Single_candle_stick_pattern':
                        entries = data if isinstance(data, list) else [data]
                        for d in entries:
                            message_parts.append(f"  Candle (1): <i>{d}</i>")
                    elif analysis_type == 'Double_candle_stick_pattern':
                        entries = data if isinstance(data, list) else [data]
                        for d in entries:
                            message_parts.append(f"  Candle (2): <i>{d}</i>")
                    elif analysis_type == "Triple_candle_stick_pattern":
                        entries = data if isinstance(data, list) else [data]
                        for d in entries:
                            message_parts.append(f"  Candle (3): <i>{d}</i>")
                    elif analysis_type == 'FUTURE_ACTION':
                        message_parts.append(f"  Futures: <b>{data.action}</b> p%:<code>{data.price_percentage:.2f}</code> oi%:<code>{data.oi_percentage:.2f}</code>")
                    elif analysis_type == 'vwap_deviation':
                        cmp = '&lt;' if trend == 'BULLISH' else '&gt;'
                        side = 'below' if trend == 'BULLISH' else 'above'
                        message_parts.append(f"  VWAP: <code>{data.close:.2f}</code> {cmp} <code>{data.vwap:.2f}</code> Dev:<code>{data.deviation:.2f}%</code>")
                        message_parts.append(f"    Intervals {side}: {data.vwap_days}")
                    elif analysis_type == 'MACD':
                        message_parts.append(f"  MACD: <i>{data}</i>")
                    elif analysis_type == 'BUY_SELL':
                        cmp = '&gt;' if trend == 'BULLISH' else '&lt;'
                        message_parts.append(f"  BuySell: Buy <code>{data.buy_quantity:.0f}</code> {cmp} Sell <code>{data.sell_quantity:.0f}</code>")
                    elif analysis_type == 'FUTURE_BREAKOUT_PATTERN':
                        message_parts.append(f"  Futures Breakout: <b>{data.pattern}</b>")
                    elif analysis_type == 'EMA_CROSSOVER':
                        cmp = '&gt;' if trend == 'BULLISH' else '&lt;'
                        message_parts.append(f"  EMA: <b>{data.direction}</b> fast:<code>{data.fast_ema:.2f}</code> {cmp} slow:<code>{data.slow_ema:.2f}</code>")
                    elif analysis_type == 'SUPERTREND':
                        arrow = '\u2191' if trend == 'BULLISH' else '\u2193'
                        message_parts.append(f"  Supertrend: {arrow} ST=<code>{data.supertrend_value:.2f}</code> Price=<code>{data.close:.2f}</code> | <i>{data.signal}</i>")
                    elif analysis_type == 'RSI_DIVERGENCE':
                        message_parts.append(f"  RSI Div: <b>{data.divergence_type}</b> P:<code>{data.price_previous:.2f}\u2192{data.price_current:.2f}</code> RSI:<code>{data.rsi_previous:.1f}\u2192{data.rsi_current:.1f}</code>")
                    elif analysis_type == 'STOCHASTIC':
                        message_parts.append(f"  Stoch: %K=<code>{data.k_value:.1f}</code> %D=<code>{data.d_value:.1f}</code> | <i>{data.signal}</i>")
                    elif analysis_type == 'OBV':
                        message_parts.append(f"  OBV: <b>{data.divergence_type}</b> Price={data.price_trend} OBV={data.obv_trend}")
                    elif analysis_type == 'PIVOT_POINTS':
                        message_parts.append(f"  Pivot: <b>{data.signal}</b> Price=<code>{data.close:.2f}</code> {data.level_name}=<code>{data.level_value:.2f}</code> PP=<code>{data.pivot:.2f}</code>")
                    elif analysis_type == 'PCR_EXTREME':
                        message_parts.append(f"  PCR Extreme: <b>{data.zone}</b> PCR=<code>{data.pcr_value:.3f}</code> - <i>{data.signal}</i>")
                    elif analysis_type == 'PCR_BIAS':
                        message_parts.append(f"  PCR Bias: <b>{data.bias}</b> PCR=<code>{data.total_pcr:.3f}</code>")
                    elif analysis_type == 'PCR_TREND':
                        message_parts.append(f"  PCR Trend: <b>{data.trend}</b> PCR=<code>{data.pcr_current:.3f}</code> \u0394=<code>{data.pcr_change_pct:.2f}%</code>")
                    elif analysis_type == 'PCR_REVERSAL':
                        message_parts.append(f"  PCR Reversal: <b>{data.reversal_type}</b> {data.previous_zone}\u2192{data.current_zone}")
                        message_parts.append(f"    PCR: <code>{data.previous_pcr:.3f}</code> \u2192 <code>{data.current_pcr:.3f}</code> | <i>{data.signal}</i>")
                    elif analysis_type == 'MAX_PAIN':
                        message_parts.append(f"  MaxPain: Price=<code>{data.current_price:.2f}</code> MP=<code>{data.max_pain_strike:.2f}</code> Dev=<code>{data.deviation_pct:+.2f}%</code> ({data.signal_strength})")
                        if data.pcr:
                            message_parts.append(f"    Exp={data.expiry} PCR=<code>{data.pcr:.3f}</code> Type={data.max_pain_type}")
                    elif analysis_type == 'MAX_PAIN_ALIGNMENT':
                        message_parts.append(f"  MP Align: <b>{data.alignment}</b> MP={data.max_pain_type} PCR={data.pcr_type}")
                        message_parts.append(f"    <i>{data.signal}</i>")
                    elif analysis_type == 'OI_SUPPORT_RESISTANCE':
                        message_parts.append(f"  OI S/R: S=<code>{data.support_strike:.0f}</code>(OI:{data.support_oi:,.0f}) R=<code>{data.resistance_strike:.0f}</code>(OI:{data.resistance_oi:,.0f})")
                        message_parts.append(f"    Price=<code>{data.current_price:.2f}</code> | <i>{data.signal}</i>")
                    elif analysis_type == 'OI_BUILDUP':
                        ratio_val = data.call_put_oi_change_ratio
                        ratio_str = f"{ratio_val:.1f}x" if ratio_val != float('inf') else "\u221E"
                        message_parts.append(f"  OI Buildup: <b>{data.buildup_type}</b> Call\u0394=<code>{data.total_call_oi_change:+,.0f}</code> Put\u0394=<code>{data.total_put_oi_change:+,.0f}</code> Ratio=<code>{ratio_str}</code>")
                        message_parts.append(f"    <i>{data.signal}</i>")
                    elif analysis_type == 'OI_WALL':
                        message_parts.append(f"  OI Wall: <b>{data.wall_type}</b>")
                        message_parts.append(f"    <i>{data.signal}</i>")
                    elif analysis_type == 'OI_SHIFT':
                        call_center_str = f"{data.call_oi_center:.0f}" if data.call_oi_center else "N/A"
                        put_center_str = f"{data.put_oi_center:.0f}" if data.put_oi_center else "N/A"
                        message_parts.append(f"  OI Shift: CallCenter=<code>{call_center_str}</code> PutCenter=<code>{put_center_str}</code>")
                        message_parts.append(f"    NewCall=<code>{data.total_new_call_oi:,.0f}</code> NewPut=<code>{data.total_new_put_oi:,.0f}</code>")
                        message_parts.append(f"    <i>{data.signal}</i>")
                    elif analysis_type == 'OI_INTRADAY_TREND':
                        message_parts.append(f"  OI Trend: Call={data.call_oi_trend}(<code>{data.call_oi_change_pct:+.1f}%</code>) Put={data.put_oi_trend}(<code>{data.put_oi_change_pct:+.1f}%</code>) PCR={data.pcr_trend}(<code>{data.first_pcr:.2f}\u2192{data.last_pcr:.2f}</code>)")
                        message_parts.append(f"    [{data.snapshots_used} snaps] <i>{data.signal}</i>")
                    elif analysis_type == 'OI_SR_SHIFT':
                        message_parts.append(f"  OI S/R Shift: R:<code>{data.first_resistance:.0f}\u2192{data.last_resistance:.0f}</code> S:<code>{data.first_support:.0f}\u2192{data.last_support:.0f}</code>")
                        message_parts.append(f"    [{data.snapshots_used} snaps] <i>{data.signal}</i>")

        if stock.analysis['NEUTRAL']:
            message_parts.append(f"\n\u26AA <b>NEUTRAL</b>:")
            for analysis_type, data in stock.analysis['NEUTRAL'].items():
                if analysis_type == '52-week-high':
                    message_parts.append("  \U0001F4A5 Price at <b>52 WEEK HIGH</b>")
                elif analysis_type == '52-week-low':
                    message_parts.append("  \U0001F4A5 Price at <b>52 WEEK LOW</b>")
                elif analysis_type == 'ATR':
                    message_parts.append(f"  ATR: <code>{data.atr_value:.2f}</code> (<code>{data.atr_percentage:.2f}%</code>)")
                elif analysis_type == 'IV_SPIKE':
                    if isinstance(data, list):
                        for iv_spike in data:
                            message_parts.append(f"  IV Spike: {iv_spike.expiry} <code>{iv_spike.iv_change:.2f}%</code>")
                    else:
                        message_parts.append(f"  IV Spike: {data.expiry} <code>{data.iv_change:.2f}%</code>")
                elif analysis_type == 'IV_TREND':
                    if isinstance(data, list):
                        for iv_trend in data:
                            message_parts.append(f"  IV Trend: {iv_trend.expiry} <b>{iv_trend.trend}</b> <code>{iv_trend.iv_change_pct:.2f}%</code>")
                    else:
                        message_parts.append(f"  IV Trend: {data.expiry} <b>{data.trend}</b> <code>{data.iv_change_pct:.2f}%</code>")
                elif analysis_type == 'FUTURE_PVO_PATTERN':
                    if isinstance(data, list):
                        for fut_data in data:
                            message_parts.append(f"  PVO: <b>{fut_data.pattern}</b> p:<code>{fut_data.price_pct:.2f}%</code> v:<code>{fut_data.vol_pct:.2f}%</code> oi:<code>{fut_data.oi_pct:.2f}%</code>")
                    else:
                        message_parts.append(f"  PVO: <b>{data.pattern}</b> p:<code>{data.price_pct:.2f}%</code> v:<code>{data.vol_pct:.2f}%</code> oi:<code>{data.oi_pct:.2f}%</code>")
                elif analysis_type == 'PCR_DIVERGENCE':
                    message_parts.append(f"  PCR Div: Near=<code>{data.near_month_pcr:.3f}</code> Far=<code>{data.far_month_pcr:.3f}</code> Div=<code>{data.divergence:.3f}</code> - <i>{data.signal}</i>")
                elif analysis_type == 'MAX_PAIN_TREND':
                    message_parts.append(f"  MP Trend: <b>{data.trend}</b> Curr=<code>{data.curr_max_pain:.2f}</code> Prev=<code>{data.prev_max_pain:.2f}</code>")
                    message_parts.append(f"    Exp={data.expiry} CurrDev=<code>{data.curr_deviation:+.2f}%</code> PrevDev=<code>{data.prev_deviation:+.2f}%</code>")
                elif analysis_type == 'MAX_PAIN_ALIGNMENT':
                    message_parts.append(f"  MP Align: <b>{data.alignment}</b> MP={data.max_pain_type} PCR={data.pcr_type}")
                    message_parts.append(f"    <i>{data.signal}</i>")
                elif analysis_type == 'OI_SUPPORT_RESISTANCE':
                    message_parts.append(f"  OI S/R: Range={data.oi_range} | S=<code>{data.support_strike:.0f}</code> R=<code>{data.resistance_strike:.0f}</code>")
                elif analysis_type == 'OI_INTRADAY_TREND':
                    message_parts.append(f"  OI Trend: Call={data.call_oi_trend} Put={data.put_oi_trend} PCR={data.pcr_trend}")
                    message_parts.append(f"    <i>{data.signal}</i>")
                elif analysis_type == 'OI_SR_SHIFT':
                    message_parts.append(f"  OI S/R Shift: R:<code>{data.first_resistance:.0f}\u2192{data.last_resistance:.0f}</code> S:<code>{data.first_support:.0f}\u2192{data.last_support:.0f}</code>")
                    message_parts.append(f"    <i>{data.signal}</i>")

        return "\n".join(message_parts)

