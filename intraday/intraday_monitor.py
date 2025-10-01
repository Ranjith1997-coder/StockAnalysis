import os
import argparse 
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
from analyser.IVAnalyser import IVAnalyser
from common.logging_util import logger
from typing import List, Tuple, Optional
from enum import Enum
import yfinance as yf
from zerodha.zerodha_analysis import ZerodhaTickerManager
from dotenv import load_dotenv
from notification.bot_listener import init_telegram_bot
import threading
from zerodha.zerodha_connect import KiteConnect
from post_market_analysis.runner import run_and_summarize

class Trend (Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


thread_pool = None
orchestrator : AnalyserOrchestrator =  None
PRODUCTION = False
SHUTDOWN_SYSTEM = False
ENABLE_NSE_DERIVATIVES = False
ENABLE_ZERODHA_DERIVATIVES = False
ENABLE_ZERODHA_API = False
ENABLE_TELEGRAM_BOT = False
ENABLE_POST_MARKET = False

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
        stock.update_latest_data()

        analysis_type = "positional" if shared.app_ctx.mode == shared.Mode.POSITIONAL else "intraday"
        logger.debug(f"{analysis_type} analysis for {stock.stockName} started.")
        
        if ENABLE_ZERODHA_DERIVATIVES:
            try:
                # Fetch zerodha derivatives data
                stock.get_atm_data_for_stock(mode=analysis_type)
                stock.get_futures_data_for_stock(mode=analysis_type)
                logger.debug(f"Zerodha derivatives data fetched successfully for {stock.stockName}")
            except Exception as e:
                logger.error(f"Error fetching zerodha derivatives data for {stock.stockName}: {e}")
        else:
            logger.debug("Zerodha derivatives data not enabled for {stock.stockName}")

        if stock.is_index:
            trend_found = (
                orchestrator.run_all_positional(stock, index=True)
                if shared.app_ctx.mode == shared.Mode.POSITIONAL
                else orchestrator.run_all_intraday(stock, index=True)
            )
        else: 
            trend_found = (
                orchestrator.run_all_positional(stock)
                if shared.app_ctx.mode == shared.Mode.POSITIONAL
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

def update_zerodha_option_chain(stockName = None, indexName = None):
    
    kc = KiteConnect(constant.DUMMY_API_KEY_ZERODHA)
    all_instruments_df = pd.DataFrame(kc.instruments())
    all_options_df= all_instruments_df[(all_instruments_df['segment'] == 'NFO-OPT')]
    all_futures_df= all_instruments_df[(all_instruments_df['segment'] == 'NFO-FUT')]
    count = 0
    for stock in shared.app_ctx.stock_token_obj_dict.values():
        if not PRODUCTION and constant.NO_OF_STOCKS != -1 and count >= constant.NO_OF_STOCKS:
            break
        if stockName and stock.stock_symbol != stockName:
            continue
        zerodha_ctx = stock.zerodha_ctx
        # Fetch options data for the stock
        stock_options = all_options_df[all_options_df['name'] == stock.stock_symbol]
        stock_options = stock_options[['instrument_token', 'tradingsymbol', 'expiry', 'strike', 'instrument_type']]
        # Fetch futures data for the stock
        stock_futures = all_futures_df[all_futures_df['name'] == stock.stock_symbol]
        stock_futures = stock_futures[['instrument_token', 'tradingsymbol', 'expiry', 'instrument_type']]

        expiry_dates = sorted(stock_options['expiry'].unique())
        zerodha_ctx["option_chain"]["current"] = stock_options[stock_options['expiry'] == expiry_dates[0]]
        zerodha_ctx["futures_mdata"]["current"] = stock_futures[stock_futures['expiry'] == expiry_dates[0]]
        if len(expiry_dates) > 1:
            zerodha_ctx["option_chain"]["next"] = stock_options[stock_options['expiry'] == expiry_dates[1]] 
            zerodha_ctx["futures_mdata"]["next"] = stock_futures[stock_futures['expiry'] == expiry_dates[1]]
        else:
            logger.info(f"stock {stock.stock_symbol} next expiry not available")
            zerodha_ctx["option_chain"]["next"] = pd.DataFrame()
        
        logger.info(f"stock {stock.stock_symbol} zerodha_ctx updated")
        count += 1
    
    count = 0
    for index in  shared.app_ctx.index_token_obj_dict.values():
        if not PRODUCTION and constant.NO_OF_INDEX != -1 and count >= constant.NO_OF_INDEX:
            break
        if (index.stock_symbol == "INDIA_VIX") or  (indexName and index.stock_symbol != indexName) :
            continue
        
        zerodha_ctx = index.zerodha_ctx
        # Fetch options data for the index
        index_options = all_options_df[all_options_df['name'] == index.stock_symbol]
        index_options = index_options[['instrument_token', 'tradingsymbol', 'expiry', 'strike', 'instrument_type']]
        # Fetch futures data for the index
        index_futures = all_futures_df[all_futures_df['name'] == index.stock_symbol]
        index_futures = index_futures[['instrument_token', 'tradingsymbol', 'expiry', 'instrument_type']]

        expiry_dates = sorted(index_options['expiry'].unique())

        if "NIFTY" ==  index.stock_symbol:
            expiry_df = pd.DataFrame({'expiry': expiry_dates})
            expiry_df['year'] = expiry_df['expiry'].apply(lambda x: x.year)
            expiry_df['month'] = expiry_df['expiry'].apply(lambda x: x.month)

            # Only keep the last expiry for each month (monthly expiry)
            monthly_expiry_dates = expiry_df.groupby(['year', 'month'])['expiry'].max().sort_values().tolist()

            expiry_dates = monthly_expiry_dates
        
        zerodha_ctx["option_chain"]["current"] = index_options[index_options['expiry'] == expiry_dates[0]]
        zerodha_ctx["futures_mdata"]["current"] = index_futures[index_futures['expiry'] == expiry_dates[0]]
        if len(expiry_dates) > 1:
            zerodha_ctx["option_chain"]["next"] = index_options[index_options['expiry'] == expiry_dates[1]]
            zerodha_ctx["futures_mdata"]["next"] = index_futures[index_futures['expiry'] == expiry_dates[1]]
        else:
            logger.info(f"Index {index.stock_symbol} next expiry not available")
            zerodha_ctx["option_chain"]["next"] = pd.DataFrame()
        
        logger.info(f"Index {index.stock_symbol} zerodha_ctx updated")


def create_stock_and_index_objects(stockName = None, indexName = None ):
    def is_before_market_open():
        now = datetime.now()
        return now.weekday() < 5 and now.time() < time(9, 15)
    
    stock_list, index_list= get_stock_objects_from_json()

    count = 0
    yfinanceIndexSymbols = []
    for index in index_list:
        if not PRODUCTION and constant.NO_OF_INDEX != -1 and count >= constant.NO_OF_INDEX:
            break
        if indexName and index["tradingsymbol"] != indexName:
            continue
        
        yfinanceIndexSymbols.append(index["yfinancetradingsymbol"]) 

        ticker = Stock(index["name"], index["tradingsymbol"], yfinanceSymbol=index["yfinancetradingsymbol"], is_index=True)
        shared.app_ctx.index_token_obj_dict[index["instrument_token"]] = ticker
        shared.app_ctx.index_list.append(index["tradingsymbol"])
        count += 1

    if yfinanceIndexSymbols:
        prevDayIndexData = yf.download(yfinanceIndexSymbols, period="2D", interval="1d", group_by='ticker')
        logger.debug(f"Price data fetched to update previous OHLCV for indices")
        for index in  shared.app_ctx.index_token_obj_dict.values():
            if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
                if is_before_market_open():
                    stock_prev_OHLCV_df = prevDayIndexData[index.stockSymbolYFinance].iloc[-1]
                    logger.debug(f"Using second-to-last day's data for {index.stock_symbol} (before market open)")
                else:
                    stock_prev_OHLCV_df = prevDayIndexData[index.stockSymbolYFinance].iloc[-2]
                    logger.debug(f"Using last day's data for {index.stock_symbol} (after market open)")
            else:
                stock_prev_OHLCV_df = prevDayIndexData[index.stockSymbolYFinance].iloc[-2]
                logger.debug(f"Using second-to-last day's data for {index.stock_symbol}")

            index.set_prev_day_ohlcv(stock_prev_OHLCV_df["Open"], stock_prev_OHLCV_df["Close"], 
                                        stock_prev_OHLCV_df["High"], stock_prev_OHLCV_df["Low"], 
                                        stock_prev_OHLCV_df["Volume"])
        

    count = 0
    yfinanceSymbols = []
    for stock in stock_list:
        if not PRODUCTION and constant.NO_OF_STOCKS != -1 and count >= constant.NO_OF_STOCKS:
            break
        if stockName and stock["tradingsymbol"] != stockName:
            continue
        yfinanceSymbols.append(stock["tradingsymbol"]+".NS") 
        ticker = Stock(stock["name"], stock["tradingsymbol"])
        shared.app_ctx.stock_token_obj_dict[stock["instrument_token"]] = ticker
        shared.app_ctx.stocks_list.append(stock["tradingsymbol"])
        count += 1
    
    if yfinanceSymbols:
        prevDaydata = yf.download(yfinanceSymbols, period="2D", interval="1d", group_by='ticker')
        logger.debug(f"Price data fetched to update previous OHLCV for stocks")
        for stock in shared.app_ctx.stock_token_obj_dict.values():
            if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
                if is_before_market_open():
                    stock_prev_OHLCV_df = prevDaydata[stock.stockSymbolYFinance].iloc[-1]
                    logger.debug(f"Using second-to-last day's data for {stock.stock_symbol} (before market open)")
                else:
                    stock_prev_OHLCV_df = prevDaydata[stock.stockSymbolYFinance].iloc[-2]
                    logger.debug(f"Using last day's data for {stock.stock_symbol} (after market open)")
            else:
                stock_prev_OHLCV_df = prevDaydata[stock.stockSymbolYFinance].iloc[-2]
                logger.debug(f"Using second-to-last day's data for {stock.stock_symbol}")

            stock.set_prev_day_ohlcv(stock_prev_OHLCV_df["Open"], stock_prev_OHLCV_df["Close"], 
                                        stock_prev_OHLCV_df["High"], stock_prev_OHLCV_df["Low"], 
                                        stock_prev_OHLCV_df["Volume"])
    

def fetch_price_data(stock_objs, index_objs):
    
    def update_price_data(ticker : Stock, data):
        try:
            if shared.app_ctx.mode.name == shared.Mode.POSITIONAL.name:
                data.index = data.index.tz_localize('UTC').tz_convert('Asia/Kolkata')
            else:
                data.index = data.index.tz_convert('Asia/Kolkata')

            ticker.priceData = data
            ticker.last_price_update = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            logger.debug(f"Price data fetched successfully for {ticker.stockName}")
        except Exception as e:
            logger.error(f"Error fetching price data for {ticker.stockName}: {e}")
    
    stockSymbols = [stock.stockSymbolYFinance for stock in stock_objs]
    indexSymbols = [index.stockSymbolYFinance for index in index_objs]

    period = "2y" if shared.app_ctx.mode.name == shared.Mode.POSITIONAL.name else "2D"
    interval = "1d" if shared.app_ctx.mode.name == shared.Mode.POSITIONAL.name else "5m"

    if stockSymbols:
        stockData = yf.download(stockSymbols, period=period, interval=interval, group_by='ticker')
        for stock in stock_objs:
            stock_data = stockData[stock.stockSymbolYFinance]
            update_price_data(stock, stock_data)
    
    if indexSymbols:
        indexData = yf.download(indexSymbols, period=period, interval=interval, group_by='ticker')
        for index in index_objs:
            index_data = indexData[index.stockSymbolYFinance]
            update_price_data(index, index_data)

def fetch_futures_data(stock):
    try:
        if shared.app_ctx.mode.name == shared.Mode.POSITIONAL.name:
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
    
    stock_objs = list(shared.app_ctx.stock_token_obj_dict.values())
    index_objs = list(shared.app_ctx.index_token_obj_dict.values())
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # Fetch price data for all stocks at once
        price_future = executor.submit(fetch_price_data, stock_objs, index_objs)

        if ENABLE_NSE_DERIVATIVES:
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
        
        # Monitor and analyze all index
        results = []
        monitor_futures = {executor.submit(process_stock, index): index for index in index_objs}
        orchestrator.reset_all_constants(is_index=True)
        for future in concurrent.futures.as_completed(monitor_futures):
            index = monitor_futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:
                logger.error(f"Unexpected error processing {index.stockName}: {exc}")
                results.append((MonitorResult.ERROR, False, str(exc)))
        
        # Monitor and analyze all stocks

        monitor_futures = {executor.submit(process_stock, stock): stock for stock in stock_objs}
        orchestrator.reset_all_constants(is_index=False)
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
                    logger.warning(f"Invalid percentage change for {stock.stock_symbol}: {change_percent} current close {current_close}, previous close {previous_close}")
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
    top_gainers , top_losers = get_top_gainers_and_losers(shared.app_ctx.stock_token_obj_dict)
    report = generate_top_gainers_and_losers_report(top_gainers, top_losers)
    TELEGRAM_NOTIFICATIONS.send_notification(report)
    logger.info(f"EOD Report\n {report}")

def report_index_data():
    logger.info("Reporting index data")
    report = "*********** Index Report ***********\n"
    index_objs = list(shared.app_ctx.index_token_obj_dict.values())
    for index in index_objs:
        try:
            report += f"  {index.stock_symbol}: {index.ltp:.2f} {index.ltp_change_perc:.2f}%\n"
        except Exception as e:
            logger.error(f"Error while getting index data for {index}: {e}")
    TELEGRAM_NOTIFICATIONS.send_notification(report)
    logger.info(f"Index Report\n {report}")

def report_52_week_high_low(max_items: int = 40, clear_after: bool = False):
    """
    Report 52-week High / Low stocks.
    shared.ticker_52_week_high_list / low_list contain Stock objects.
    Uses stock.ltp and stock.ltp_change_perc directly (fallbacks if missing).
    """
    high_objs = shared.ticker_52_week_high_list
    low_objs  = shared.ticker_52_week_low_list

    if not high_objs and not low_objs:
        TELEGRAM_NOTIFICATIONS.send_notification("52W High/Low: None today.")
        logger.info("52W High/Low: None today.")
        return

    def dedup(objs):
        seen = set()
        out = []
        for o in objs:
            if not o:
                continue
            sym = o.stock_symbol
            if sym not in seen:
                seen.add(sym)
                out.append(o)
        return out

    high_objs = dedup(high_objs)
    low_objs  = dedup(low_objs)

    def price_and_change(stk: 'Stock'):
        price = None
        chg = None
        try:
            price = float(stk.ltp) if stk.ltp is not None else None
        except Exception:
            price = None
        try:
            chg = float(stk.ltp_change_perc) if stk.ltp_change_perc is not None else None
        except Exception:
            chg = None

        # Fallbacks if missing
        if price is None and not stk.is_price_data_empty():
            try:
                price = float(stk.priceData['Close'].iloc[-1])
            except Exception:
                pass
        if chg is None and price is not None and stk.prevDayOHLCV and stk.prevDayOHLCV.get("CLOSE"):
            try:
                prev_close = float(stk.prevDayOHLCV["CLOSE"])
                if prev_close:
                    chg = percentageChange(price, prev_close)
            except Exception:
                pass
        return price, chg

    def build_section(title, stocks, sort_desc=True):
        if not stocks:
            return f"{title} (0): None"
        rows = []
        for s in stocks:
            p, c = price_and_change(s)
            rows.append({"symbol": s.stock_symbol, "price": p, "chg": c})
        # Filter rows with price
        rows = [r for r in rows if r["price"] is not None]
        # Sort highs by % change desc, lows asc
        rows.sort(key=lambda r: (r["chg"] if r["chg"] is not None else (-1e9 if sort_desc else 1e9)),
                  reverse=sort_desc)

        display = rows[:max_items]
        extra = len(rows) - len(display)

        sym_w = max(6, *(len(r["symbol"]) for r in display)) if display else 6
        header = f"{title} ({len(rows)})"
        lines = [
            header,
            f"{'Symbol'.ljust(sym_w)}  {'Price':>9}  {'%Chg':>7}",
            f"{'-'*sym_w}  {'-'*9}  {'-'*7}"
        ]
        for r in display:
            price_str = f"{r['price']:.2f}" if r['price'] is not None else "NA"
            chg_str = f"{r['chg']:+.2f}%" if r['chg'] is not None else "NA"
            lines.append(f"{r['symbol'].ljust(sym_w)}  {price_str:>9}  {chg_str:>7}")
        if extra > 0:
            lines.append(f"... (+{extra} more)")
        return "\n".join(lines)

    msg = (
        "*********** 52-Week High / Low ***********\n"
        + build_section("52W Highs", high_objs, sort_desc=True) + "\n\n"
        + build_section("52W Lows", low_objs, sort_desc=False)
    )

    TELEGRAM_NOTIFICATIONS.send_notification(msg)
    logger.info(msg)

    if clear_after:
        shared.ticker_52_week_high_list.clear()
        shared.ticker_52_week_low_list.clear()

def intraday_analysis(loop = True, loop_wait_time = 30):
    shared.app_ctx.mode = shared.Mode.INTRADAY

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
        report_index_data()

        for stock in shared.app_ctx.stock_token_obj_dict:
            shared.app_ctx.stock_token_obj_dict[stock].reset_price_data()

        if PRODUCTION:
            sleeptime = (constant.INTRADAY_SLEEP_TIME) - (datetime.now().second + ((datetime.now().minute % 5) * 60))
            logger.info("sleeping for {} sec".format(sleeptime))
            sleep(sleeptime)

            is_in_time_period = isNowInTimePeriod(time(9,15), time(15,30), datetime.now().time())
        else:
            if not loop:
                break
            logger.info("Sleeping for {} sec in dev mode".format(loop_wait_time))
            sleep(loop_wait_time)
            is_in_time_period = True  # In dev mode, keep looping

    logger.info("Market time closed")


def positional_analysis():
    shared.app_ctx.mode = shared.Mode.POSITIONAL
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
    report_index_data()
    report_52_week_high_low()

    # Post-market flows
    if ENABLE_POST_MARKET:
        try:
            post_market_msg_list = run_and_summarize()
            if post_market_msg_list:
                for msg in post_market_msg_list:
                    TELEGRAM_NOTIFICATIONS.send_notification(msg)
                    logger.info(msg)
        except Exception as e:
            logger.error(f"Post-market pipeline failed: {e}")

    for stock in shared.app_ctx.stock_token_obj_dict:
        shared.app_ctx.stock_token_obj_dict[stock].reset_price_data()

    logger.info("EOD analysis completed.")

def init():
    load_dotenv()
    global thread_pool
    global orchestrator
    global PRODUCTION
    global SHUTDOWN_SYSTEM
    global ENABLE_NSE_DERIVATIVES
    global ENABLE_ZERODHA_DERIVATIVES
    global ENABLE_ZERODHA_API
    global ENABLE_TELEGRAM_BOT
    global ENABLE_POST_MARKET
    

    if os.getenv(constant.ENV_PRODUCTION, "0") == "1":
        logger.info("Running in production mode")
        PRODUCTION = True
        TELEGRAM_NOTIFICATIONS.is_production = True
    else:
        logger.info("Running in development mode")
        PRODUCTION = False
    
    if os.getenv(constant.ENV_SHUTDOWN, "0") == "1":
        logger.info("Shutdown mode enabled")
        SHUTDOWN_SYSTEM = True
    else:
        logger.info("Shutdown mode disabled")
        SHUTDOWN_SYSTEM = False
    
    if os.getenv(constant.ENV_ENABLE_NSE_DERIVATIVES, "0") == "1":
        logger.info(" Derivative analysis enabled")
        ENABLE_NSE_DERIVATIVES = True
    else:
        logger.info(" Derivative analysis disabled")
        ENABLE_NSE_DERIVATIVES = False
    
    if os.getenv(constant.ENV_ENABLE_ZERODHA_DERIVATIVES, "0") == "1":
        logger.info("Zerodha Derivative analysis enabled")
        ENABLE_ZERODHA_DERIVATIVES = True
    else:
        logger.info("Zerodha Derivative analysis disabled")
        ENABLE_ZERODHA_DERIVATIVES = False
    
    if os.getenv(constant.ENV_ENABLE_ZERODHA_API, "0") == "1":
        logger.info(" Zerodha analysis enabled")
        ENABLE_ZERODHA_API = True
    else:
        logger.info(" Zerodha analysis disabled")
        ENABLE_ZERODHA_API = False
    
    if os.getenv(constant.ENV_ENABLE_TELEGRAM_BOT, "0") == "1":
        logger.info(" Telegram Bot enabled")
        ENABLE_TELEGRAM_BOT = True
    else:
        logger.info(" Telegram Bot disabled")
        ENABLE_TELEGRAM_BOT = False
    
    if os.getenv(constant.ENV_ENABLE_POST_MARKET, "0") == "1":
        ENABLE_POST_MARKET = True
    else:
        logger.info(" Post market analysis disabled")
        ENABLE_POST_MARKET = False
    
    if PRODUCTION:
        if datetime.now().time() < time(9,15) or isNowInTimePeriod(time(9,15), time(15,30), datetime.now().time()):
            shared.app_ctx.mode = shared.Mode.INTRADAY
        else:
            shared.app_ctx.mode = shared.Mode.POSITIONAL
    else:
        if os.getenv(constant.ENV_DEV_POSITIONAL, "0") == "1":
            logger.info("Positional analysis enabled")
            shared.app_ctx.mode = shared.Mode.POSITIONAL
        
        if os.getenv(constant.ENV_DEV_INTRADAY, "0") == "1":
            logger.info("Intraday analysis enabled")
            shared.app_ctx.mode = shared.Mode.INTRADAY
    

    args = parse_arguments()
    
    create_stock_and_index_objects(args.stock, args.index)
    if ENABLE_ZERODHA_DERIVATIVES:
        update_zerodha_option_chain(args.stock, args.index)
    orchestrator = AnalyserOrchestrator()
    orchestrator.register(VolumeAnalyser())
    orchestrator.register(TechnicalAnalyser())
    orchestrator.register(CandleStickAnalyser())
    orchestrator.register(IVAnalyser())
    orchestrator.register(FuturesAnalyser())
    if ENABLE_NSE_DERIVATIVES:
        shared.app_ctx.stockExpires = NSE_DATA_CLASS.expiry_dates_future()        
    
    if ENABLE_ZERODHA_API:
        logger.info("Zerodha API enabled")
        userName = os.getenv(constant.ENV_ZERODHA_USERNAME)
        password = os.getenv(constant.ENV_ZERODHA_PASSWORD)
        encToken = os.getenv(constant.ENV_ZERODHA_ENC_TOKEN)
        shared.app_ctx.zd_ticker_manager = ZerodhaTickerManager(userName, password, encToken)
        shared.app_ctx.zd_kc = KiteConnect(constant.DUMMY_API_KEY_ZERODHA, root="https://kite.zerodha.com/", enctoken=encToken)


def parse_arguments():
    parser = argparse.ArgumentParser(description="Stock Analysis Tool")
    parser.add_argument("--stock", type=str, help="Name of the stock to analyze (optional)")
    parser.add_argument("--index", type=str, help="Name of the index to analyze (optional)")
    return parser.parse_args()

def start_stock_analysis():

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
        
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            intraday_analysis()
        if shared.app_ctx.mode.name == shared.Mode.POSITIONAL.name:
            positional_analysis()

if __name__ =="__main__":

    init()
    if ENABLE_TELEGRAM_BOT:
        thread = threading.Thread(target=start_stock_analysis)
        thread.start()
        init_telegram_bot()
    else:
        start_stock_analysis()

    

    


    

    
    
