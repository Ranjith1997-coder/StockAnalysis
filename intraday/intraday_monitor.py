import sys
import os
sys.path.append(os.getcwd())

from notification.Notification import TELEGRAM_NOTIFICATIONS
from datetime import datetime, time 
import concurrent.futures
import common.constants as constant
import common.shared as shared
from common.Stock import Stock
from common.helperFunctions import *
from enum import Enum
from time import sleep
from nse.nse_derivative_data import NSE_DATA_CLASS
from analyser.Analyser import AnalyserOrchestrator
from analyser.Futures_Analyser import FuturesAnalyser
from analyser.VolumeAnalyser import VolumeAnalyser
from analyser.TechnicalAnalyser import TechnicalAnalyser
from analyser.candleStickPatternAnalyser import CandleStickAnalyser
from common.logging_util import logger
from typing import List, Tuple, Optional
from enum import Enum
import yfinance as yf

class Trend (Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


thread_pool = None
orchestrator : AnalyserOrchestrator = None
PRODUCTION = False
SHUTDOWN_SYSTEM = False
ENABLE_DERIVATIVES = False

class MonitorResult(Enum):
    SUCCESS = 0
    NO_DATA = 1
    ERROR = 2

def monitor(stock: Stock) -> Tuple[MonitorResult, bool, Optional[str]]:
    """
    Monitor a stock for trends and generate analysis.
    
    Args:
        stock (Stock): The stock to monitor.
    
    Returns:
        Tuple[MonitorResult, bool, Optional[str]]: 
            - MonitorResult: The result of the monitoring process.
            - bool: Whether a trend was found.
            - Optional[str]: The analysis message if a trend was found, else None.
    """
    if stock.is_price_data_empty():
        return MonitorResult.NO_DATA, False, f"{stock.stock_symbol} data not available"
    
    try:
        stock.reset_analysis()

        analysis_type = "Positional" if constant.mode == constant.Mode.POSITIONAL else "Intraday"
        logger.debug(f"{analysis_type} analysis for {stock.stockName} started.")
        
        trend_found = (
            orchestrator.run_all_positional(stock)
            if constant.mode == constant.Mode.POSITIONAL
            else orchestrator.run_all_intraday(stock)
        )
        
        logger.debug(f"{analysis_type} analysis for {stock.stockName} completed.")
        
        if trend_found:
            logger.info(f"Trend found for {stock.stockName}")
            message = orchestrator.generate_analysis_message(stock)
            TELEGRAM_NOTIFICATIONS.send_notification(message)
            return MonitorResult.SUCCESS, True, message
        
        return MonitorResult.SUCCESS, False, None

    except Exception as e:
        logger.exception(f"Error occurred while monitoring {stock.stockName}")
        return MonitorResult.ERROR, False, str(e)

def process_monitor_results(results):
    for result, trend_found, message in results:
        if result == MonitorResult.NO_DATA:
            logger.warning(message)
        elif result == MonitorResult.ERROR:
            logger.error(f"Error during monitoring: {message}")
        elif trend_found:
            logger.info(f"Trend found: \n{message}")

def create_stock_objects():
    count = 0
    stock_list = get_stock_objects_from_json()

    yfinanceSymbols = [stock["tradingsymbol"]+".NS" for stock in stock_list]
   
    prevDaydata = yf.download(yfinanceSymbols, period="2D", interval="1d", group_by='ticker')
    logger.info(f"Price data fetched to update previous OHLCV")

    for stock in stock_list:
        if not PRODUCTION and constant.NO_OF_STOCKS != -1 and count >= constant.NO_OF_STOCKS:
            break
        ticker = Stock(stock["name"], stock["tradingsymbol"])
        stock_prev_OHLCV_df =  prevDaydata[stock["tradingsymbol"]+ ".NS"].iloc[-2]

        ticker.set_prev_day_ohlcv(stock_prev_OHLCV_df["Open"], stock_prev_OHLCV_df["Close"], 
                                  stock_prev_OHLCV_df["High"], stock_prev_OHLCV_df["Low"], 
                                  stock_prev_OHLCV_df["Volume"])
        shared.stock_token_obj_dict[stock["instrument_token"]] = ticker
        shared.stocks_list.append(stock["tradingsymbol"])
        count += 1

def fetch_price_data(stock_objs):
    symbols = [stock.stockSymbolYFinance for stock in stock_objs]
    if constant.mode.name == constant.Mode.POSITIONAL.name:
        data = yf.download(symbols, period="2y", interval="1d", group_by='ticker')
    else:
        data = yf.download(symbols, period="2d", interval="5m", group_by='ticker')
    
    for stock in stock_objs:
        try:
            # if len(symbols) > 1:
            #     stock_data = data[stock.stockSymbolYFinance]
            # else:
            #     stock_data = data
            stock_data = data[stock.stockSymbolYFinance]
            stock_data.index = stock_data.index.tz_convert('Asia/Kolkata')
            stock.priceData = stock_data
            stock.last_price_update = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            logger.debug(f"Price data fetched successfully for {stock.stockName}")
        except Exception as e:
            logger.error(f"Error fetching price data for {stock.stockName}: {e}")

def fetch_futures_data(stock):
    try:
        if constant.mode.name == constant.Mode.POSITIONAL.name:
            stock.get_futures_and_options_data_from_nse_positional()
        else:
            stock.get_futures_and_options_data_from_nse_intraday()
        logger.debug(f"Futures data fetched successfully for {stock.stockName}")
    except Exception as e:
        logger.error(f"Error fetching futures data for {stock.stockName}: {e}")


def process_stock(stock: Stock) -> Tuple[MonitorResult, bool, Optional[str]]:
    try:
        return monitor(stock)
    except Exception as exc:
        logger.error(f"Error processing {stock.stockName}: {exc}")
        return MonitorResult.ERROR, False, str(exc)

def fetch_and_analyze_stocks() -> List[Tuple[MonitorResult, bool, Optional[str]]]:
    logger.info("Fetching and analyzing data for all stocks")
    
    stock_objs = list(shared.stock_token_obj_dict.values())
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # Fetch price data for all stocks at once
        price_future = executor.submit(fetch_price_data, stock_objs)

        if ENABLE_DERIVATIVES:
            # Fetch futures data for each stock in parallel
            futures_futures = {executor.submit(fetch_futures_data, stock): stock for stock in stock_objs}
            
            # Wait for all data fetching to complete
            concurrent.futures.wait([price_future] + list(futures_futures.keys()))
            
            # Check for any errors in price data fetching
            try:
                price_future.result()
            except Exception as exc:
                logger.error(f"Error fetching price data for stocks: {exc}")
            
            # Check for any errors in futures data fetching
            for future in concurrent.futures.as_completed(futures_futures):
                stock = futures_futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.error(f"Error fetching futures data for {stock.stockName}: {exc}")
        else:
            # Wait for price data fetching to complete
            try:
                price_future.result()  # This will block until the price data fetching is complete
            except Exception as exc:
                logger.error(f"Error fetching price data for stocks: {exc}")
                return [(MonitorResult.ERROR, False, str(exc))]
            
        
        
        # Monitor and analyze all stocks
        monitor_futures = {executor.submit(process_stock, stock): stock for stock in stock_objs}
        results = []
        for future in concurrent.futures.as_completed(monitor_futures):
            stock = monitor_futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:
                logger.error(f"Unexpected error processing {stock.stockName}: {exc}")
                results.append((MonitorResult.ERROR, False, str(exc)))

    logger.info("Data fetching and analysis completed for all stocks")
    return results

def get_top_gainers_and_losers(stock_objs):
    """
    Returns the top 5 gainers and top 5 losers based on percentage change in stock prices.

    Args:
        stock_objs (list): List of Stock objects with price data.

    Returns:
        Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]: 
            - List of top 5 gainers as tuples of (stock symbol, percentage gain).
            - List of top 5 losers as tuples of (stock symbol, percentage loss).
    """
    gainers = []
    losers = []

    for _ , stock in stock_objs.items():
        try:
            # Assuming stock.priceData contains a DataFrame with 'Close' prices
            if not stock.is_price_data_empty() and stock.prevDayOHLCV is not None:
                current_close = stock.priceData['Close'].iloc[-1]
                # previous_close = stock.priceData['Close'].iloc[-2]
                previous_close = stock.prevDayOHLCV['CLOSE']
                change_percent = percentageChange(current_close, previous_close)

                # Check if change_percent is a valid number
                if not isinstance(change_percent, float) or change_percent != change_percent:  # NaN check
                    logger.warning(f"Invalid percentage change for {stock.stock_symbol}: {change_percent}")
                    continue
                
                if change_percent > 0:
                    gainers.append((stock.stock_symbol, change_percent))
                else:
                    losers.append((stock.stock_symbol, change_percent))
        except Exception as e:
            logger.error(f"Error calculating percentage change for {stock.stock_symbol}: {e}")

    # Sort gainers and losers by percentage change
    gainers.sort(key=lambda x: x[1], reverse=True)
    losers.sort(key=lambda x: x[1])

    # Return top 5 gainers and top 5 losers
    return gainers[:5], losers[:5]

def generate_top_gainers_and_losers_report(gainers, losers):
    """
    Generates a report with top gainers and top losers based on percentage change in stock prices.

    Args:
        gainers (list): List of tuples of (stock symbol, percentage gain).
        losers (list): List of tuples of (stock symbol, percentage loss).

    Returns:
        str: Report as a string.
    """
    report = "*********** Top Gainers ***********\n"
    for i, (stock, change_percent) in enumerate(gainers):
        report += f"{i+1}. {stock}: {change_percent:.2f}%\n"

    report += "\n*********** Top Losers ***********\n"
    for i, (stock, change_percent) in enumerate(losers):
        report += f"{i+1}. {stock}: {change_percent:.2f}%\n"

    return report

def report_top_gainers_and_losers():
    top_gainers , top_losers = get_top_gainers_and_losers(shared.stock_token_obj_dict)
    report = generate_top_gainers_and_losers_report(top_gainers, top_losers)
    TELEGRAM_NOTIFICATIONS.send_notification(report)
    logger.info(f"EOD Report\n {report}")

def intraday_analysis():
    constant.mode = constant.Mode.INTRADAY

    logger.info("Market time open. Starting Intraday analysis")

    TELEGRAM_NOTIFICATIONS.send_notification("*********** Intraday Analysis ***********")

    orchestrator.reset_all_constants()
    is_in_time_period = isNowInTimePeriod(time(9,15), time(15,30), datetime.now().time())

    while(is_in_time_period or not PRODUCTION):
        logger.info("current iteration time : {}".format(datetime.now()))

        try:
            results = fetch_and_analyze_stocks()
            process_monitor_results(results)
        except Exception as e:
            logger.error(f"Critical error in stock analysis: {e}")

        report_top_gainers_and_losers()

        for stock in shared.stock_token_obj_dict:
            shared.stock_token_obj_dict[stock].reset_price_data()

        if PRODUCTION:
            sleeptime = (constant.INTRADAY_SLEEP_TIME) - (datetime.now().second + ((datetime.now().minute % 5) * 60))
            logger.info("sleeping for {} sec".format(sleeptime))
            sleep(sleeptime)

            is_in_time_period = isNowInTimePeriod(time(9,15), time(15,30), datetime.now().time())
        else:
            is_in_time_period = False
            break

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
    try:
        results = fetch_and_analyze_stocks()
        process_monitor_results(results)
    except Exception as e:
        logger.error(f"Critical error in stock analysis: {e}")

    report_top_gainers_and_losers()

    for stock in shared.stock_token_obj_dict:
        shared.stock_token_obj_dict[stock].reset_price_data()

    logger.info("EOD analysis completed.")

def init():
    global thread_pool
    global orchestrator
    global PRODUCTION
    global SHUTDOWN_SYSTEM
    global ENABLE_DERIVATIVES

    if os.getenv(constant.ENV_PRODUCTION, "0") == "1":
        logger.info("Running in production mode")
        PRODUCTION = True
    else:
        logger.info("Running in development mode")
        PRODUCTION = False
    
    if os.getenv(constant.ENV_SHUTDOWN, "0") == "1":
        logger.info("Shutdown mode enabled")
        SHUTDOWN_SYSTEM = True
    else:
        logger.info("Shutdown mode disabled")
        SHUTDOWN_SYSTEM = False
    
    if os.getenv(constant.ENV_ENABLE_DERIVATIVES, "0") == "1":
        logger.info(" Derivative analysis enabled")
        ENABLE_DERIVATIVES = True
    else:
        logger.info(" Derivative analysis disabled")
        ENABLE_DERIVATIVES = False
    
    create_stock_objects()
    orchestrator = AnalyserOrchestrator()
    orchestrator.register(VolumeAnalyser())
    orchestrator.register(TechnicalAnalyser())
    orchestrator.register(CandleStickAnalyser())
    if ENABLE_DERIVATIVES:
        shared.stockExpires = NSE_DATA_CLASS.expiry_dates_future()
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
        TELEGRAM_NOTIFICATIONS.send_notification("*********** System shutdown ***********")
        # Shutdown system
        if SHUTDOWN_SYSTEM:
            from subprocess import run
            run(["/sbin/shutdown", "-h", "now"])
    else:
        logger.info("Running in development mode. No shutdown operation.")
        run_intraday = False
        run_positional = False
        if os.getenv(constant.ENV_DEV_POSITIONAL, "0") == "1":
            logger.debug("Intraday analysis enabled")
            run_positional = True
        if os.getenv(constant.ENV_DEV_INTRADAY, "0") == "1":
            logger.debug("Positional analysis enabled")
            run_intraday = True
        
        if not run_intraday and not run_positional:
            logger.info("No analysis enabled. Exiting.")
            exit(0)
        
        if run_intraday:
            intraday_analysis()
        if run_positional:
            positional_analysis()

    

    


    

    
    
