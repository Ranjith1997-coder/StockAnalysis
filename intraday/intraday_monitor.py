import sys
import os
sys.path.append(os.getcwd())

from notification.Notification import TELEGRAM_NOTIFICATIONS
from datetime import datetime, time 
from multiprocessing.pool import ThreadPool
import common.constants as constant
import common.shared as shared
from common.Stock import Stock
from common.helperFunctions import get_stock_objects_from_json, isNowInTimePeriod
from enum import Enum
from time import sleep
from nse.nse_derivative_data import NSE_DATA_CLASS
from analyser.Analyser import AnalyserOrchestrator
from analyser.Futures_Analyser import FuturesAnalyser
from analyser.VolumeAnalyser import VolumeAnalyser
from analyser.TechnicalAnalyser import TechnicalAnalyser
from analyser.CandleStickPatternAnalyser import CandleStickAnalyser
from common.logging_util import logger

class Trend (Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


VOL_SMA_WIN_SIZE = 20

thread_pool = None
orchestrator : AnalyserOrchestrator = None
PRODUCTION = False
SHUTDOWN_SYSTEM = False

def monitor(stock: Stock):
        ticker = stock
        stock_name = ticker.stockName
        if ticker.is_price_data_empty():
            return (1, "{} data not available".format(stock_name))
        
        try: 
            ticker.compute_sma_of_volume(VOL_SMA_WIN_SIZE)
            ticker.compute_rsi()
            # ticker.compute_atr_rank()
            ticker.reset_analysis()
            ticker.compute_bollinger_band()
            # ticker.compute_candle_stick_pattern()
            if constant.mode.name == constant.Mode.INTRADAY.name:
                curr_data = ticker.priceData.iloc[-2]
                prev_data = ticker.priceData.iloc[-3]
            else:
                curr_data = ticker.priceData.iloc[-1]
                prev_data = ticker.priceData.iloc[-2]

            trend_found = False

            if constant.mode.name == constant.Mode.POSITIONAL.name:
                logger.debug("Positional analysis for {} stated.".format(ticker.stockName))
                trend_found = orchestrator.run_all_positional(stock)
                logger.debug("Positional analysis for {} completed.".format(ticker.stockName))
            else : 
                logger.debug("Intraday analysis for {} stated.".format(ticker.stockName))
                trend_found = orchestrator.run_all_intraday(stock)
                logger.debug("Intraday analysis for {} completed.".format(ticker.stockName))
            
            if trend_found:
                logger.info("Trend found for {} ".format(ticker.stockName))
                ticker.analysis["Timestamp"] = curr_data.name
                message = orchestrator.generate_analysis_message(ticker)
                TELEGRAM_NOTIFICATIONS.send_notification(message)
                return (0, trend_found, message)   
            else:
                return (0, trend_found, None)
        except Exception as e : 
            logger.error("Error occured while monitoring {}. \n Exception : {}".format(ticker.stockName, e))
        

def create_stock_objects(stock_list : list):
    count = 0

    for stock in stock_list:
        if not PRODUCTION and constant.NO_OF_STOCKS != -1 and count >= constant.NO_OF_STOCKS:
            break
        ticker = Stock(stock["name"], stock["tradingsymbol"])
        shared.stock_token_obj_dict[stock["instrument_token"]] = ticker
        shared.stocks_list.append(stock["tradingsymbol"])
        count += 1


def intraday_analysis():
    constant.mode = constant.Mode.INTRADAY

    logger.info("Market time open. Starting Intraday analysis")

    TELEGRAM_NOTIFICATIONS.send_notification("*********** Intraday Analysis ***********")

    orchestrator.reset_all_constants()
    is_in_time_period = isNowInTimePeriod(time(9,15), time(15,30), datetime.now().time())

    while(is_in_time_period):
        logger.info("current iteration time : {}".format(datetime.now()))

        for stock in shared.stock_token_obj_dict:
            shared.stock_token_obj_dict[stock].get_stock_price_data('2d','5m')
            shared.stock_token_obj_dict[stock].get_futures_and_options_data_from_nse_intraday()
        
        for result in thread_pool.map(monitor, list(shared.stock_token_obj_dict.values())):
            if result[0]:
                print("Error : {}".format(result[1]))
            else:
                if result[1]:
                    print(result[2])
        
        for stock in shared.stock_token_obj_dict:
            shared.stock_token_obj_dict[stock].reset_price_data()

        sleeptime = (constant.INTRADAY_SLEEP_TIME) - (datetime.now().second + ((datetime.now().minute % 5) * 60))
        logger.info("sleeping for {} sec".format(sleeptime))
        sleep(sleeptime)

        is_in_time_period = isNowInTimePeriod(time(9,15), time(15,30), datetime.now().time())

    logger.info("Market time closed")


def positional_analysis():
    constant.mode = constant.Mode.POSITIONAL
    if PRODUCTION:
        if datetime.now().time() > time(16,0):
            logger.info("Market time closed")
        else:
            logger.info("Sleeping till 4:00 PM to start EOD analysis")
            now = datetime.now()
            new_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
            time_to_sleep = new_time - now
            logger.info("Sleeping for {} sec".format(time_to_sleep.total_seconds()))
            sleep(time_to_sleep.total_seconds())
    
    logger.info("EOD analysis Started")
    TELEGRAM_NOTIFICATIONS.send_notification("*********** EOD Analysis ***********")
    orchestrator.reset_all_constants()
    for stock in shared.stock_token_obj_dict:
        shared.stock_token_obj_dict[stock].get_stock_price_data('2y','1d')
        shared.stock_token_obj_dict[stock].get_futures_and_options_data_from_nse_positional()

    logger.info("Data fetched for all stocks")
    for result in thread_pool.map(monitor, list(shared.stock_token_obj_dict.values())):
        if result[0]:
            print("Error : {}".format(result[1]))
        else:
            if result[1]:
                print(result[2])
    
    for stock in shared.stock_token_obj_dict:
        shared.stock_token_obj_dict[stock].reset_price_data()

    logger.info("EOD analysis completed.")

def init():
    global thread_pool
    global orchestrator
    global PRODUCTION
    global SHUTDOWN_SYSTEM

    if os.getenv(constant.ENV_PRODUCTION, "False") == "True":
        logger.info("Running in production mode")
        PRODUCTION = True
    else:
        logger.info("Running in development mode")
        PRODUCTION = False
    
    if os.getenv(constant.ENV_SHUTDOWN, "False") == "True":
        logger.info("Shutdown mode enabled")
        SHUTDOWN_SYSTEM = True
    else:
        logger.info("Shutdown mode disabled")
        SHUTDOWN_SYSTEM = False
    
    data = get_stock_objects_from_json()
    shared.stockExpires = NSE_DATA_CLASS.expiry_dates_future()
    stock_list = data["data"]["UnderlyingList"]
    create_stock_objects(stock_list)
    thread_pool = ThreadPool(processes=10)

    orchestrator = AnalyserOrchestrator()
    orchestrator.register(FuturesAnalyser())
    orchestrator.register(VolumeAnalyser())
    orchestrator.register(TechnicalAnalyser())
    orchestrator.register(CandleStickAnalyser())
    

if __name__ =="__main__":

    init()

    if PRODUCTION:
        if datetime.now().time() < time(9,15):
            now = datetime.now()
            new_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
            time_to_sleep = new_time - now
            logger.info("Sleeping for {} sec to 9:15 AM".format(time_to_sleep.total_seconds()))
            sleep(time_to_sleep.total_seconds())

        is_in_time_period = isNowInTimePeriod(time(9,15), time(15,30), datetime.now().time())

        if is_in_time_period:
            intraday_analysis()
        else:
            positional_analysis()
        
        logger.info("shutting down the system.")
        TELEGRAM_NOTIFICATIONS.send_notification("*********** System shutdown ***********")
        # Shutdown system
        if SHUTDOWN_SYSTEM:
            from subprocess import run
            run(["/sbin/shutdown", "-h", "now"])
    else:
        logger.info("Running in development mode. No shutdown operation.")
        run_intraday = False
        run_positional = True
        if os.getenv(constant.ENV_DEV_POSITIONAL, "False") == "True":
            logger.debug("Intraday analysis enabled")
            run_positional = True
        if os.getenv(constant.ENV_DEV_INTRADAY, "False") == "True":
            logger.debug("Positional analysis enabled")
            run_intraday = True
        
        if not run_intraday and not run_positional:
            logger.info("No analysis enabled. Exiting.")
            exit(0)
        
        if run_intraday:
            intraday_analysis()
        if run_positional:
            positional_analysis()

    

    


    

    
    
