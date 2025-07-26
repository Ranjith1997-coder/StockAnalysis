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
    

