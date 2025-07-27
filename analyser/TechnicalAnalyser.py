import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
import common.constants as constant
from common.logging_util import logger
from collections import namedtuple

class TechnicalAnalyser(BaseAnalyzer):

    RSI_UPPER_THRESHOLD = 80
    RSI_LOWER_THRESHOLD = 20
    ATR_THRESHOLD = 0.97
    def __init__(self) -> None:
        self.analyserName = "Technical Analyser"
        super().__init__()
    
    def reset_constants(self):
        # if constant.mode.name == constant.Mode.INTRADAY.name:
        #     #add something later on this
        # else:
        #      #add something later on this
        logger.debug(f"Technical Analyser constants reset for mode {constant.mode.name}")
        logger.debug(f"RSI_UPPER_THRESHOLD = {TechnicalAnalyser.RSI_UPPER_THRESHOLD} , RSI_LOWER_THRESHOLD = {TechnicalAnalyser.RSI_LOWER_THRESHOLD}, ATR_THRESHOLD = {TechnicalAnalyser.ATR_THRESHOLD}")

    @BaseAnalyzer.both
    def analyse_rsi(self, stock: Stock):
        try : 
            logger.debug(f'Inside analyse_rsi for stock {stock.stock_symbol}')
            curr_data = stock.current_equity_data
            rsi_value = curr_data["rsi"]
            RSIAnalysis = namedtuple("RSIAnalysis", ["value"])
            if rsi_value > TechnicalAnalyser.RSI_UPPER_THRESHOLD: 
                stock.set_analysis("BEARISH", "RSI", RSIAnalysis(value=rsi_value))
                return True
            elif rsi_value < TechnicalAnalyser.RSI_LOWER_THRESHOLD:
                stock.set_analysis("BULLISH", "RSI", RSIAnalysis(value=rsi_value))
                return True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_rsi for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.both
    def analyse_rsi_crossover(self, stock: Stock):
        try : 
            logger.debug(f'Inside analyse_rsi_crossover for stock {stock.stock_symbol}')
            curr_data = stock.current_equity_data
            prev_data = stock.previous_equity_data
            curr_rsi_value = curr_data["rsi"]
            prev_rsi_value = prev_data["rsi"]
            RSICrossoverAnalysis = namedtuple("RSICrossoverAnalysis", ["value"])
            if prev_rsi_value > TechnicalAnalyser.RSI_UPPER_THRESHOLD and curr_rsi_value < TechnicalAnalyser.RSI_UPPER_THRESHOLD: 
                stock.set_analysis("BULLISH", "rsi_crossover", RSICrossoverAnalysis(value=curr_data["rsi"]))
                return True
            elif prev_rsi_value < TechnicalAnalyser.RSI_LOWER_THRESHOLD and curr_rsi_value > TechnicalAnalyser.RSI_LOWER_THRESHOLD: 
                stock.set_analysis("BEARISH", "rsi_crossover", RSICrossoverAnalysis(value=curr_data["rsi"]))
                return True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_rsi_crossover for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.both
    def analyse_Bolinger_band(self, stock: Stock):
        try : 
            logger.debug(f'Inside analyse_Bolinger_band for stock {stock.stock_symbol}')
            curr_data = stock.current_equity_data
            BBAnalysis = namedtuple("BBAnalysis", ["close", "upper_band", "lower_band"])
            if curr_data['Close'] > curr_data['BB_UPPER_BAND']: 
                stock.set_analysis("BEARISH", "BollingerBand", BBAnalysis(close=curr_data['Close'], upper_band=curr_data['BB_UPPER_BAND'], lower_band=curr_data['BB_LOWER_BAND']))
                return True
            elif curr_data['Close'] < curr_data['BB_LOWER_BAND']:
                stock.set_analysis("BULLISH", "BollingerBand", BBAnalysis(close=curr_data['Close'], upper_band=curr_data['BB_UPPER_BAND'], lower_band=curr_data['BB_LOWER_BAND']))
                return True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_Bolinger_band for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.positional
    def analyse_is_52_week(self, stock: Stock):
        try : 
            logger.debug(f'Inside analyse_is_52_week for stock {stock.stock_symbol}')
            status = stock.check_52_week_status()
            if status == 1:
                stock.set_analysis("NEUTRAL", "52-week-high", True)
            elif status == -1:
                stock.set_analysis("NEUTRAL", "52-week-low", True)
            return False
        except Exception as e:
            logger.error(f"Error in analyse_is_52_week for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False