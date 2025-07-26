import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
import common.constants as constant
from common.logging_util import logger

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
            rsi_value = curr_data["rsi"].item()
            if rsi_value > TechnicalAnalyser.RSI_UPPER_THRESHOLD: 
                stock.analysis["BEARISH"]["rsi"] = {"value" : curr_data["rsi"].item()}
                return True
            elif rsi_value < TechnicalAnalyser.RSI_LOWER_THRESHOLD:
                stock.analysis["BULLISH"]["rsi"] = {"value" : curr_data["rsi"].item()}
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
            curr_rsi_value = curr_data["rsi"].item()
            prev_rsi_value = prev_data["rsi"].item()

            if prev_rsi_value > TechnicalAnalyser.RSI_UPPER_THRESHOLD and curr_rsi_value < TechnicalAnalyser.RSI_UPPER_THRESHOLD: 
                stock.analysis["BEARISH"]["rsi_crossover"] = {"value" : curr_data["rsi"].item()}
                return True
            elif prev_rsi_value < TechnicalAnalyser.RSI_LOWER_THRESHOLD and curr_rsi_value > TechnicalAnalyser.RSI_LOWER_THRESHOLD: 
                stock.analysis["BULLISH"]["rsi_crossover"] = {"value" : curr_data["rsi"].item()}
                return True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_rsi for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.both
    def analyse_Bolinger_band(self, stock: Stock):
        try : 
            logger.debug(f'Inside analyse_Bolinger_band for stock {stock.stock_symbol}')
            curr_data = stock.current_equity_data
            if curr_data['Close'].item() > curr_data['BB_UPPER_BAND'].item(): 
                stock.analysis["BEARISH"]["BB"]  = { "close" : curr_data['Close'].item(),
                                                    "upper_band" : curr_data['BB_UPPER_BAND'].item()
                                                    }
                return True
            elif curr_data['Close'].item() < curr_data['BB_LOWER_BAND'].item():
                stock.analysis["BULLISH"]["BB"]  = { "close" : curr_data['Close'].item(),
                                                    "lower_band" : curr_data['BB_LOWER_BAND'].item()
                                                    }
                return True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_BB for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.positional
    def analyse_is_52_week(self, stock: Stock):
        try : 
            logger.debug(f'Inside analyse_is_52_week for stock {stock.stock_symbol}')
            status = stock.check_52_week_status()
            if status == 1:
                stock.analysis["NEUTRAL"]["52-week-high"] = True
            elif status == -1:
                stock.analysis["NEUTRAL"]["52-week-low"] = True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_is_52_week for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False