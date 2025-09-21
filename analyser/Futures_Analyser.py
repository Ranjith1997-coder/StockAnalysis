import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.helperFunctions import percentageChange
from common.logging_util import logger
from collections import namedtuple
import common.shared as shared

class FuturesAnalyser(BaseAnalyzer):
    FUTURE_OI_INCREASE_PERCENTAGE = 0
    FUTURE_PRICE_CHANGE_PERCENTAGE = 0

    def __init__(self) -> None:
        self.analyserName = "Futures Analyser"
        super().__init__()
    
    def reset_constants(self):
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE = 1.5
            FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE = 0.5
        else :
            FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE = 10
            FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE = 2
        logger.debug(f"FuturesAnalyser constants reset for mode {shared.app_ctx.mode.name}")
        logger.debug(f'FUTURE_OI_INCREASE_PERCENTAGE: {FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE}, FUTURE_PRICE_CHANGE_PERCENTAGE: {FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE}')

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_intraday_check_future_action(self, stock: Stock):
        try:
            logger.debug("Inside analyse_intraday_check_future_action method for stock {}".format(stock.stock_symbol))

            def get_future_action(futures_data, price_col="close", oi_col="oi", expiry='current'):
                
                """
                Determines futures action based on price and OI percentage change.
                Expects futures_data as a DataFrame with columns: price_col, oi_col.
                Returns a namedtuple with action, price_percentage, oi_percentage.
                """
                FutureActionAnalysis = namedtuple('FutureActionAnalysis', ['expiry', 'action', 'price_percentage', 'oi_percentage'])

                if len(futures_data) < 2:
                    logger.warning(f"Insufficient data for futures analysis for stock: {stock.stock_symbol} and expiry: {expiry}. Skipping action determination.")
                    return False

                prev_oi = futures_data.iloc[-2][oi_col]
                curr_oi = futures_data.iloc[-1][oi_col]
                prev_price = futures_data.iloc[-2][price_col]
                curr_price = futures_data.iloc[-1][price_col]

                price_percentage = percentageChange(curr_price, prev_price)
                oi_percentage = percentageChange(curr_oi, prev_oi)

                if price_percentage > FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE and \
                oi_percentage > FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE:
                    stock.set_analysis("BULLISH", "FUTURE_ACTION", FutureActionAnalysis(expiry,"long_buildup", price_percentage, oi_percentage))
                    return True
                elif price_percentage < (-1 * FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE) and \
                    oi_percentage > FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE:
                    stock.set_analysis("BEARISH", "FUTURE_ACTION", FutureActionAnalysis(expiry,"short_buildup", price_percentage, oi_percentage))
                    return True
                elif price_percentage > FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE and \
                    oi_percentage < (-1 * FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE):
                    stock.set_analysis("BULLISH", "FUTURE_ACTION",  FutureActionAnalysis(expiry, "short_covering", price_percentage, oi_percentage))
                    return True
                elif price_percentage < (-1 * FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE) and \
                    oi_percentage < (-1 * FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE):
                    stock.set_analysis("BEARISH", "FUTURE_ACTION",  FutureActionAnalysis(expiry,"long_unwinding", price_percentage, oi_percentage))
                    return True
                return False

            zerodha_ctx = stock.zerodha_ctx

            futures_data_curr = zerodha_ctx["futures_data"]["current"]
            futures_data_next = zerodha_ctx["futures_data"]["next"]
            res = False
            if get_future_action(futures_data_curr, expiry='current'):
                logger.info(f"Futures action detected for {stock.stock_symbol} for current expiry")
                res = True
            
            # if get_future_action(futures_data_next, expiry='next'):
            #     logger.info(f"Futures action detected for {stock.stock_symbol} for next expiry")
            #     res = True

            if res:
                logger.debug(f"Futures action detected for {stock.stock_symbol}")
            
            return res
            
        except Exception as e:
            logger.error(f"Error in analyse_intraday_check_future_action for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False    


        
    


        


        




