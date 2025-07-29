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


class AnalyserOrchestrator:
    def __init__(self):
        self.analysers = []

    def register(self, analyser: BaseAnalyzer):
        if not isinstance(analyser, BaseAnalyzer):
            raise TypeError("Analyser must inherit from BaseAnalyser")
        self.analysers.append(analyser)
    
    def reset_all_constants(self):
        for analyser in self.analysers:
            analyser.reset_constants()

    def run_all_intraday(self, stock):
        logger.debug("Starting all analyses for stock {}".format(stock.stock_symbol))
        found_trend = False
        for analyser in self.analysers:
            analyser.run_all_intraday_analyses(stock)
        logger.debug("All analyses complete for stock {}".format(stock.stock_symbol))
        if stock.analysis["NoOfTrends"] >= constant.REQUIRED_TRENDS:
            found_trend = True
        return found_trend
    
    def run_all_positional(self, stock):
        logger.debug("Starting all analyses for stock {}".format(stock.stock_symbol))
        found_trend = False
        for analyser in self.analysers:
            analyser.run_all_positional_analyses(stock)
        logger.debug("All analyses complete for stock {}".format(stock.stock_symbol))
        if stock.analysis["NoOfTrends"] >= constant.REQUIRED_TRENDS:
            found_trend = True
        return found_trend
    
    def generate_analysis_message(self, stock):
        # return message
        message_parts = [
        f"Stock: {stock.stock_symbol}",
        ]

        for trend in ['BULLISH', 'BEARISH']:
            if stock.analysis[trend]:
                message_parts.append(f"{trend}:")
                for analysis_type, data in stock.analysis[trend].items():
                    if analysis_type == 'Volume':
                        message_parts.append(f"  Volume {trend.lower()}: {data.Volume_rate_percent:.2f}%")
                        message_parts.append(f"  Price {trend.lower()}: {data.price_change_percent:.2f}%")
                    elif analysis_type == 'RSI':
                        message_parts.append(f"  RSI value: {data.value:.2f}")
                    elif analysis_type == 'rsi_crossover':
                        message_parts.append(f"  RSI crossover: pv:{data.prev_value:.2f}, cv:{data.curr_value:.2f} ")
                    elif analysis_type == 'BollingerBand':
                        def format_bollinger_band(data, trend):
                            close_price = f"{data.close:.2f}"
                            comparison = '<' if trend == 'BULLISH' else '>'
                            band_type = 'Lower' if trend == 'BULLISH' else 'Upper'
                            band_value = f"{data.lower_band:.2f}" if trend == 'BULLISH' else f"{data.upper_band:.2f}"
                            return f"  Bollinger Band: Price({close_price}) {comparison} {band_type}_band ({band_value})"
                        message_parts.append(format_bollinger_band(data, trend))
                    elif analysis_type == 'Single_candle_stick_pattern':
                        message_parts.append(f" Single Candle stick Pattern: {data}")
                    elif analysis_type == 'Double_candle_stick_pattern':
                        message_parts.append(f" Double Candle stick Pattern: {data}")  
                    elif analysis_type == "Triple_candle_stick_pattern":
                        message_parts.append(f" Triple Candle stick Pattern: {data}")
                    elif analysis_type == 'future_action':
                        message_parts.append(f"  Futures action: {data.action}, p% = {data.price_percentage:.2f}, oi% = {data.oi_percentage:.2f}")

        if stock.analysis['NEUTRAL']:
            message_parts.append("NEUTRAL:")
            for analysis_type, data in stock.analysis['NEUTRAL'].items():
                if analysis_type == '52-week-high':
                    message_parts.append("  Price at 52 WEEK HIGH")
                elif analysis_type == '52-week-low':
                    message_parts.append("  Price at 52 WEEK LOW")

        return "\n".join(message_parts)

