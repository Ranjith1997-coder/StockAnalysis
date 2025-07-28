import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
import common.constants as constant
from common.helperFunctions import percentageChange
from common.logging_util import logger
from collections import namedtuple

class FuturesAnalyser(BaseAnalyzer):
    FUTURE_OI_INCREASE_PERCENTAGE = 0
    FUTURE_PRICE_CHANGE_PERCENTAGE = 0

    def __init__(self) -> None:
        self.analyserName = "Futures Analyser"
        super().__init__()
    
    def reset_constants(self):
        if constant.mode.name == constant.Mode.INTRADAY.name:
            FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE = 7
            FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE = 0.5
        else :
            FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE = 30
            FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE = 4
        logger.debug(f"FuturesAnalyser constants reset for mode {constant.mode.name}")
        logger.debug(f'FUTURE_OI_INCREASE_PERCENTAGE: {FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE}, FUTURE_PRICE_CHANGE_PERCENTAGE: {FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE}')

    @BaseAnalyzer.intraday
    def analyse_intraday_check_future_action(self, stock: Stock):
        try:
            logger.debug("Inside analyse_intraday_check_future_action method for stock {}".format(stock.stock_symbol))

            futures_data_curr_expiry = stock.derivativesData["futuresData"]["currExpiry"]
            futures_data_next_expiry = stock.derivativesData["futuresData"]["nextExpiry"]

            if futures_data_curr_expiry is None or futures_data_next_expiry is None:
                logger.warning(f"No futures data found for stock {stock.stock_symbol}")
                return False

            if len(futures_data_curr_expiry) <= 1 or len(futures_data_next_expiry) <= 1 :
                return False
            

            prev_oi = futures_data_curr_expiry.iloc[-2]['OPEN_INT'] if futures_data_next_expiry is None else futures_data_curr_expiry.iloc[-2]['OPEN_INT'] + futures_data_next_expiry.iloc[-2]['OPEN_INT']
            curr_oi = futures_data_curr_expiry.iloc[-1]['OPEN_INT'] if futures_data_next_expiry is None else futures_data_curr_expiry.iloc[-1]['OPEN_INT'] + futures_data_next_expiry.iloc[-1]['OPEN_INT']
            prev_price = futures_data_curr_expiry.iloc[-2]['LAST_TRADED_PRICE'] if futures_data_next_expiry is None else (futures_data_curr_expiry.iloc[-2]['LAST_TRADED_PRICE'] + futures_data_next_expiry.iloc[-2]['LAST_TRADED_PRICE']) / 2
            curr_price = futures_data_curr_expiry.iloc[-1]['LAST_TRADED_PRICE'] if futures_data_next_expiry is None else (futures_data_curr_expiry.iloc[-1]['LAST_TRADED_PRICE'] + futures_data_next_expiry.iloc[-1]['LAST_TRADED_PRICE']) / 2
            price_percentage = percentageChange(curr_price, prev_price) 
            oi_percentage = percentageChange(curr_oi, prev_oi)

            FutureActionAnalysis = namedtuple('FutureActionAnalysis', ['action', 'price_percentage', 'oi_percentage' ])

            if  price_percentage > FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE and \
                oi_percentage > FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE:
                stock.set_analysis("BULLISH", "future_action", FutureActionAnalysis("long_buildup", price_percentage, oi_percentage))
                return True
            elif price_percentage < (-1 * FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE) and \
                oi_percentage > FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE:
                stock.set_analysis("BEARISH", "future_action", FutureActionAnalysis("short_buildup", price_percentage, oi_percentage))
                return True
            elif price_percentage >  FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE and \
                oi_percentage < (-1 * FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE):
                stock.set_analysis("BULLISH", "future_action", FutureActionAnalysis("short_covering", price_percentage, oi_percentage))
                return True
            elif price_percentage <  (-1 * FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE) and \
                oi_percentage < (-1 * FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE):
                stock.set_analysis("BEARISH", "future_action", FutureActionAnalysis("long_unwinding", price_percentage, oi_percentage))
                return True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_intraday_check_future_action for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False    
    @BaseAnalyzer.positional
    def analyse_positional_check_future_action(self, stock: Stock):
        try:
            logger.debug("Inside analyse_positional_check_future_action method for stock {}".format(stock.stock_symbol))

            futures_data_curr_expiry = stock.derivativesData["futuresData"]["currExpiry"]
            futures_data_next_expiry = stock.derivativesData["futuresData"]["nextExpiry"]

            if len(futures_data_next_expiry) <= 1 or len(futures_data_curr_expiry) <= 1 :
                return False

            curr_expiry_oi_change_pct = (futures_data_curr_expiry.iloc[0]["CHANGE_IN_OI"]/futures_data_curr_expiry.iloc[1]["OPEN_INT"]) * 100
            next_expiry_oi_change_pct = (futures_data_next_expiry.iloc[0]["CHANGE_IN_OI"]/futures_data_next_expiry.iloc[1]["OPEN_INT"]) * 100
            avg_oi_change_pct = (curr_expiry_oi_change_pct + next_expiry_oi_change_pct) / 2

            curr_price = (futures_data_curr_expiry.iloc[0]['SETTLE_PRICE'] + futures_data_next_expiry.iloc[0]['SETTLE_PRICE']) / 2
            prev_price = (futures_data_curr_expiry.iloc[1]['SETTLE_PRICE'] + futures_data_next_expiry.iloc[1]['SETTLE_PRICE']) / 2
            price_percentage = percentageChange(curr_price, prev_price) 

            FutureActionAnalysis = namedtuple('FutureActionAnalysis', ['action', 'price_percentage', 'oi_percentage' ])

            if  price_percentage > FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE and \
                avg_oi_change_pct > FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE:
                stock.set_analysis("BULLISH", "future_action", FutureActionAnalysis("long_buildup", price_percentage, avg_oi_change_pct))
                return True
            elif price_percentage < (-1 * FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE) and \
                avg_oi_change_pct > FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE:
                stock.set_analysis("BEARISH", "future_action", FutureActionAnalysis("short_buildup", price_percentage, avg_oi_change_pct))
                return True
            elif price_percentage >  FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE and \
                avg_oi_change_pct < (-1 * FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE):
                stock.set_analysis("BULLISH", "future_action", FutureActionAnalysis("short_covering", price_percentage, avg_oi_change_pct))
                return True
            elif price_percentage <  (-1 * FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE) and \
                avg_oi_change_pct < (-1 * FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE):
                stock.set_analysis("BEARISH", "future_action", FutureActionAnalysis("long_unwinding", price_percentage, avg_oi_change_pct))
                return True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_positional_check_future_action for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")

        
    


        


        




