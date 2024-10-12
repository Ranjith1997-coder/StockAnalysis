import sys
import os
sys.path.append(os.getcwd())

from intraday.volume_monitor import *
from intraday.other_monitor import *
from common.push_notification import telegram_notif
from datetime import datetime
import time
from multiprocessing.pool import ThreadPool
from common.constants import mode, Mode , stocks
from common.shared import stock_token_obj_dict, stocks_list 
from common.Stock import Stock
from enum import Enum
import logging

logging.basicConfig(format="{levelname}:{name}:{message}", style="{")

class Trend (Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

SLEEP_TIME = 301
VOL_SMA_WIN_SIZE = 20

thread_pool = None

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
    
    if stock.analysis["BEARISH"]:
        bearish_trend = stock.analysis["BEARISH"]
        message += "BEARISH : \n"
        if "Volume" in bearish_trend.keys():
            message += """  volume increase : {:.2f}% \n  price decrease : {:.2f}% \n """.format(bearish_trend["Volume"]["Volume_rate_percent"], bearish_trend["Volume"]["Price_dec_percent"])

        if "rsi" in bearish_trend.keys():
            message += """  rsi value : {:.2f} \n""".format(bearish_trend["rsi"]["value"])
        
        if "Candle_stick_pattern" in bearish_trend.keys():
            message += """  candle stick Pattern : {} \n""".format(bearish_trend["Candle_stick_pattern"]["value"])
    
    if stock.analysis["NEUTRAL"]:
        neutral_trend = stock.analysis["NEUTRAL"]
        message += "NEUTRAL : \n"
        if "atr_rank" in neutral_trend.keys():
            message += """  atr_rank : {:.2f} \n""".format(neutral_trend["atr_rank"]["value"])

    return message

def monitor(stock):

        ticker = stock
        stock_name = ticker.stockName
        if ticker.is_price_data_empty():
            return (1, "{} data not available".format(stock_name))
        ticker.compute_sma_of_volume(VOL_SMA_WIN_SIZE)
        ticker.compute_rsi()
        ticker.compute_atr_rank()
        ticker.reset_analysis()
        ticker.compute_candle_stick_pattern()
        if mode.name == Mode.INTRADAY.name:
            curr_data = ticker.priceData.iloc[-2]
            prev_data = ticker.priceData.iloc[-3]
        else:
            curr_data = ticker.priceData.iloc[-1]
            prev_data = ticker.priceData.iloc[-2]

        trend_found = False

        if check_for_increase_in_volume_and_price(curr_data['Volume'], 
                                                prev_data["Volume"],
                                                curr_data['Vol_SMA_20'],
                                                curr_data['Close'],
                                                prev_data['Close']):
            
            vol_rate = ((curr_data['Volume'] - prev_data["Volume"])/prev_data["Volume"]) * 100
            price_inc = ((curr_data['Close'] - prev_data["Close"])/prev_data["Close"]) * 100
            ticker.analysis["BULLISH"] = {"Volume":{"Volume_rate_percent" : vol_rate, 
                                                   "Price_inc_percent": price_inc}}
            trend_found = True
        elif check_for_increase_in_volume_and_decrease_in_price(curr_data['Volume'], 
                                                prev_data["Volume"],
                                                curr_data['Vol_SMA_20'],
                                                curr_data['Close'],
                                                prev_data['Close']):
            vol_rate = ((curr_data['Volume'] - prev_data["Volume"])/prev_data["Volume"]) * 100
            price_inc = ((curr_data['Close'] - prev_data["Close"])/prev_data["Close"]) * 100
            ticker.analysis["BEARISH"] = {"Volume":{ "Volume_rate_percent" : vol_rate, 
                                                    "Price_dec_percent": price_inc}}
            trend_found = True

        if is_rsi_below_threshold(curr_data["rsi"]):
            ticker.analysis["BEARISH"] = {"rsi":{"value" : curr_data["rsi"]}}
            trend_found = True
        elif is_rsi_above_threshold(curr_data["rsi"]):
            ticker.analysis["BULLISH"] = {"rsi":{"value" : curr_data["rsi"]}}
            trend_found = True

        if is_atr_rank_above_threshold(curr_data["atr_rank"]):
            ticker.analysis["NEUTRAL"] = {"atr_rank":{"value" : curr_data["atr_rank"]}}
            trend_found = True
        
        pattern_found, pattern = is_bullish_candle_stick_pattern(curr_data)

        if pattern_found:
            ticker.analysis["BULLISH"] = {"Candle_stick_pattern":{"value" : pattern}}
            trend_found = True
        
        pattern_found, pattern = is_bearish_candle_stick_pattern(curr_data)

        if pattern_found:
            ticker.analysis["BEARISH"] = {"Candle_stick_pattern":{"value" : pattern}}
            trend_found = True
        
        if trend_found:
            ticker.analysis["Timestamp"] = curr_data.name
            message = generate_notif_message(ticker)
            telegram_notif(message)
            return (0, trend_found, message)   
        else:
            return (0, trend_found, None)
        
        
def create_stock_objects():
    for stock in stocks:
        ticker = Stock(stocks[stock]["name"], stocks[stock]["tradingsymbol"])
        stock_token_obj_dict[stocks[stock]["instrument_token"]] = ticker
        stocks_list.append(stocks[stock]["tradingsymbol"])

def init():
    global thread_pool
    create_stock_objects()
    thread_pool = ThreadPool(processes=10)
    

if __name__ =="__main__":

    init()
    # sleeptime = SLEEP_TIME - datetime.now().second

    if mode.name == Mode.INTRADAY.name:
        sleeptime = (SLEEP_TIME) - (datetime.now().second + ((datetime.now().minute % 5) * 60))
        print("sleeping for {} sec".format(sleeptime))
        time.sleep(sleeptime)

        while True:
            print(time.strftime("%H:%M:%S", time.localtime()))

            # bulk_data = yf.download(stocks_list, period='2d', interval='1m')

            for stock in stock_token_obj_dict:
                stock_token_obj_dict[stock].get_stock_price_data('5d','5m')
            
            for result in thread_pool.map(monitor, list(stock_token_obj_dict.values())):
                if result[0]:
                    print("Error : {}".format(result[1]))
                else:
                    if result[1]:
                        print(result[2])
            
            for stock in stock_token_obj_dict:
                stock_token_obj_dict[stock].reset_price_data()

            sleeptime = (SLEEP_TIME) - (datetime.now().second + ((datetime.now().minute % 5) * 60))
            print("sleeping for {} sec".format(sleeptime))
            time.sleep(sleeptime)
    else :

        for stock in stock_token_obj_dict:
            stock_token_obj_dict[stock].get_stock_price_data('1y','1d')
        
        for result in thread_pool.map(monitor, list(stock_token_obj_dict.values())):
            if result[0]:
                print("Error : {}".format(result[1]))
            else:
                if result[1]:
                    print(result[2])
        
        for stock in stock_token_obj_dict:
            stock_token_obj_dict[stock].reset_price_data()

    

    
    
