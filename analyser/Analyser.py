from common.logging_util import logger

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
            found_trend |= analyser.run_all_intraday_analyses(stock)
        logger.debug("All analyses complete for stock {}".format(stock.stock_symbol))
        return found_trend
    
    def run_all_positional(self, stock):
        logger.debug("Starting all analyses for stock {}".format(stock.stock_symbol))
        found_trend = False
        for analyser in self.analysers:
            found_trend |= analyser.run_all_positional_analyses(stock)
        logger.debug("All analyses complete for stock {}".format(stock.stock_symbol))
        return found_trend
    
    def generate_analysis_message(stock):
        message = """Stock : {} \nTimestamp : {} \n""".format(stock.stock_symbol, stock.analysis["Timestamp"])
        
        if stock.analysis["BULLISH"]:
            bullish_trend = stock.analysis["BULLISH"]
            message += "BULLISH : \n"
            if "Volume" in bullish_trend.keys():
                message += """  volume increase : {:.2f}% \n  price increase : {:.2f}% \n """.format(bullish_trend["Volume"]["Volume_rate_percent"], bullish_trend["Volume"]["Price_inc_percent"])

            if "rsi" in bullish_trend.keys():
                message += """  rsi value : {:.2f} \n""".format(bullish_trend["rsi"]["value"])
            
            if "Candle_stick_pattern" in bullish_trend.keys():
                message += """  candle stick Pattern : {} \n""".format(bullish_trend["Candle_stick_pattern"]["value"])
            
            if "BB" in bullish_trend.keys():
                message += """  Bollinger Band : Price({:.2f}) < Lower_band ({:.2f}) \n """.format(bullish_trend["BB"]['close'], bullish_trend["BB"]['lower_band'])
            
            if stock.analysis["BULLISH"]:
                if "future_action" in bullish_trend.keys():
                    message += """  Futures_action : {} \n""".format(bullish_trend["future_action"]["action"])
        
        if stock.analysis["BEARISH"]:
            bearish_trend = stock.analysis["BEARISH"]
            message += "BEARISH : \n"
            if "Volume" in bearish_trend.keys():
                message += """  volume increase : {:.2f}% \n  price decrease : {:.2f}% \n """.format(bearish_trend["Volume"]["Volume_rate_percent"], bearish_trend["Volume"]["Price_dec_percent"])

            if "rsi" in bearish_trend.keys():
                message += """  rsi value : {:.2f} \n""".format(bearish_trend["rsi"]["value"])
            
            if "Candle_stick_pattern" in bearish_trend.keys():
                message += """  candle stick Pattern : {} \n""".format(bearish_trend["Candle_stick_pattern"]["value"])
            
            if "BB" in bearish_trend.keys():
                message += """  Bollinger Band : Price({:.2f}) > Upper_band ({:.2f})  \n """.format(bearish_trend["BB"]['close'], bearish_trend["BB"]['upper_band'])
            
            if "future_action" in bearish_trend.keys():
                    message += """  Futures_action : {} \n""".format(bearish_trend["future_action"]["action"])
        
        if stock.analysis["NEUTRAL"]:
            neutral_trend = stock.analysis["NEUTRAL"]
            message += "NEUTRAL : \n"
            if "atr_rank" in neutral_trend.keys():
                message += """  atr_rank : {:.2f} \n""".format(neutral_trend["atr_rank"]["value"])
            if "52-week-high" in neutral_trend.keys():
                message += """  Price at 52 WEEK HIGH \n"""
            if "52-week-low" in neutral_trend.keys():
                message += """  Price at 52 WEEK LOW \n"""

        return message

