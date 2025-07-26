import sys
import os
sys.path.append(os.getcwd())

from intraday.volume_monitor import *
from intraday.other_monitor import *
from common.push_notification import telegram_notif
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

def generate_notif_message(stock):
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
            ticker.compute_candle_stick_pattern()
            if constant.mode.name == constant.Mode.INTRADAY.name:
                curr_data = ticker.priceData.iloc[-2]
                prev_data = ticker.priceData.iloc[-3]
            else:
                curr_data = ticker.priceData.iloc[-1]
                prev_data = ticker.priceData.iloc[-2]

            trend_found = False
        
        # Volume and price increase/decrease indicator
            if check_for_increase_in_volume_and_price(curr_data['Volume'].item(), 
                                                    prev_data["Volume"].item(),
                                                    curr_data['Vol_SMA_20'].item(),
                                                    curr_data['Close'].item(),
                                                    prev_data['Close'].item()):
                
                vol_rate = ((curr_data['Volume'].item() - prev_data["Volume"].item())/prev_data["Volume"].item()) * 100
                price_inc = ((curr_data['Close'].item() - prev_data["Close"].item())/prev_data["Close"].item()) * 100
                ticker.analysis["BULLISH"]["Volume"] = {"Volume_rate_percent" : vol_rate, 
                                                    "Price_inc_percent": price_inc}
                trend_found = True
            elif check_for_increase_in_volume_and_decrease_in_price(curr_data['Volume'].item(), 
                                                    prev_data["Volume"].item(),
                                                    curr_data['Vol_SMA_20'].item(),
                                                    curr_data['Close'].item(),
                                                    prev_data['Close'].item()):
                vol_rate = ((curr_data['Volume'].item() - prev_data["Volume"].item())/prev_data["Volume"].item()) * 100
                price_inc = ((curr_data['Close'].item() - prev_data["Close"].item())/prev_data["Close"].item()) * 100
                ticker.analysis["BEARISH"]["Volume"] = { "Volume_rate_percent" : vol_rate, 
                                                        "Price_dec_percent": price_inc}
                trend_found = True

        # RSI Indicator
            if is_rsi_below_threshold(curr_data["rsi"].item()):
                ticker.analysis["BEARISH"]["rsi"] = {"value" : curr_data["rsi"].item()}
                trend_found = True
            elif is_rsi_above_threshold(curr_data["rsi"].item()):
                ticker.analysis["BULLISH"]["rsi"] = {"value" : curr_data["rsi"].item()}
                trend_found = True

        # #ATR Indicator 
        #     if is_atr_rank_above_threshold(curr_data["atr_rank"].item()):
        #         ticker.analysis["NEUTRAL"]["atr_rank"] = {"value" : curr_data["atr_rank"].item()}
        #         trend_found = True
        
        # Candle Stick Pattern.
            pattern_found, pattern = is_bullish_candle_stick_pattern(curr_data)

            if pattern_found:
                ticker.analysis["BULLISH"]["Candle_stick_pattern"] = {"value" : pattern}
                trend_found = True
            
            pattern_found, pattern = is_bearish_candle_stick_pattern(curr_data)

            if pattern_found:
                ticker.analysis["BEARISH"]["Candle_stick_pattern"]  = {"value" : pattern}
                trend_found = True
        
        # Bollinger band.
            if is_price_at_upper_BB(curr_data['Close'].item(), curr_data['BB_UPPER_BAND'].item()):
                ticker.analysis["BEARISH"]["BB"]  = { "close" : curr_data['Close'].item(),
                                                    "upper_band" : curr_data['BB_UPPER_BAND'].item()}
            elif is_price_at_lower_BB(curr_data['Close'].item(), curr_data['BB_LOWER_BAND'].item()):
                ticker.analysis["BULLISH"]["BB"]  = { "close" : curr_data['Close'].item(),
                                                    "lower_band" : curr_data['BB_LOWER_BAND'].item()}
        
        # 52 week status 
            if constant.mode.name == constant.Mode.POSITIONAL.name:
                status = ticker.check_52_week_status()
                if status == 1:
                    ticker.analysis["NEUTRAL"]["52-week-high"] = True
                    trend_found = True
                elif status == -1:
                    ticker.analysis["NEUTRAL"]["52-week-low"] = True
                    trend_found = True
            

            if constant.mode.name == constant.Mode.POSITIONAL.name:
                trend_found |= orchestrator.run_all_positional(stock)
            else : 
                trend_found |= orchestrator.run_all_intraday(stock)
            
            if trend_found:
                ticker.analysis["Timestamp"] = curr_data.name
                message = generate_notif_message(ticker)
                telegram_notif(message)
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

    telegram_notif("*********** Intraday Analysis ***********")
    set_candle_stick_constants()
    set_volume_constants()
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
    telegram_notif("*********** EOD Analysis ***********")
    set_candle_stick_constants()
    set_volume_constants()
    orchestrator.reset_all_constants()
    for stock in shared.stock_token_obj_dict:
        shared.stock_token_obj_dict[stock].get_stock_price_data('2y','1d')
        shared.stock_token_obj_dict[stock].get_futures_and_options_data_from_nse_positional()

    logger.info("Data fetched and analysis started.")
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
        telegram_notif("*********** System shutdown ***********")
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

    

    


    

    
    
