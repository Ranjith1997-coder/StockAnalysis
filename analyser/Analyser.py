from common.logging_util import logger
import common.constants as constant

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

    def run_all_intraday(self, stock, index = False):
        logger.debug("Starting all analyses for stock {}".format(stock.stock_symbol))
        found_trend = False
        for analyser in self.analysers:
            if index:
                analyser.run_all_index_intraday_analyses(stock)
            else:
                analyser.run_all_intraday_analyses(stock)
        logger.debug("All analyses complete for stock {}".format(stock.stock_symbol))
        if stock.analysis["NoOfTrends"] >= constant.REQUIRED_TRENDS:
            found_trend = True
        return found_trend
    
    def run_all_positional(self, stock, index = False):
        logger.debug("Starting all analyses for stock {}".format(stock.stock_symbol))
        found_trend = False
        for analyser in self.analysers:
            if index:
                analyser.run_all_index_positional_analyses(stock)
            else:
                analyser.run_all_positional_analyses(stock)
        logger.debug("All analyses complete for stock {}".format(stock.stock_symbol))
        if stock.analysis["NoOfTrends"] >= constant.REQUIRED_TRENDS:
            found_trend = True
        return found_trend
    
    def generate_analysis_message(self, stock):
        # return message
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

        return "\n".join(message_parts)

