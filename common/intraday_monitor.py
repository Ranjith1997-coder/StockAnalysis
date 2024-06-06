import sys
import os
sys.path.append(os.getcwd())

from intraday.volume_monitor import *
from intraday.other_monitor import *
from push_notification import telegram_notif
from datetime import datetime
import time
from multiprocessing.pool import ThreadPool
import yfinance as yf

from enum import Enum

class Trend (Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

SLEEP_TIME = 61
VOL_SMA_WIN_SIZE = 20

thread_pool = None

stock_obj_dict = {}
stocks_list = []


def generate_notif_message(stock):
    message = """Stock : {} \nTimestamp : {} \n""".format(constants.stocks[stock.stockName], stock.analysis["Timestamp"])
    
    if stock.analysis["BULLISH"]:
        bullish_trend = stock.analysis["BULLISH"]
        message += "BULLISH : \n"
        if "Volume" in bullish_trend.keys():
            message += """  volume increase : {:.2f}% \n  price increase : {:.2f}% \n """.format(bullish_trend["Volume"]["Volume_rate_percent"], bullish_trend["Volume"]["Price_inc_percent"])

        if "rsi" in bullish_trend.keys():
            message += """  rsi value : {:.2f} \n""".format(bullish_trend["rsi"]["value"])
    
    if stock.analysis["BEARISH"]:
        bearish_trend = stock.analysis["BEARISH"]
        message += "BEARISH : \n"
        if "Volume" in bearish_trend.keys():
            message += """  volume increase : {:.2f}% \n  price decrease : {:.2f}% \n """.format(bearish_trend["Volume"]["Volume_rate_percent"], bearish_trend["Volume"]["Price_dec_percent"])

        if "rsi" in bearish_trend.keys():
            message += """  rsi value : {:.2f} \n""".format(bearish_trend["rsi"]["value"])
    
    if stock.analysis["NEUTRAL"]:
        neutral_trend = stock.analysis["NEUTRAL"]
        message += "NEUTRAL : \n"
        if "atr_rank" in neutral_trend.keys():
            message += """  atr_rank : {:.2f} \n""".format(neutral_trend["atr_rank"]["value"])


    
    return message

def monitor(stock):

        ticker = stock
        stock_name = ticker.stockName
        ticker.compute_sma_of_volume(VOL_SMA_WIN_SIZE)
        ticker.compute_rsi()
        ticker.compute_atr_rank()
        ticker.reset_analysis()
        curr_data = ticker.priceData.iloc[-2]
        prev_data = ticker.priceData.iloc[-3]
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
            ticker.analysis["BULLISH"] = {"rsi":{"value" : curr_data["rsi"]}}
            trend_found = True
        elif is_rsi_above_threshold(curr_data["rsi"]):
            ticker.analysis["BEARISH"] = {"rsi":{"value" : curr_data["rsi"]}}
            trend_found = True

        if is_atr_rank_above_threshold(curr_data["atr_rank"]):
            ticker.analysis["NEUTRAL"] = {"atr_rank":{"value" : curr_data["atr_rank"]}}
            trend_found = True
        
        if trend_found:
            ticker.analysis["Timestamp"] = curr_data.name
            message = generate_notif_message(ticker)
            telegram_notif(message)
            return (stock_name ,trend_found)   
        else:
            return (stock_name ,trend_found)   
        

def create_stock_objects():
    for stock in constants.stocks:
        ticker = Stock(stock, constants.stocks[stock]+".NS", constants.stocks[stock])
        stock_obj_dict[constants.stocks[stock]]= ticker
        stocks_list.append(constants.stocks[stock]+".NS")

def init():
    global thread_pool
    create_stock_objects()
    thread_pool = ThreadPool(processes=10)
    

if __name__ =="__main__":

    init()
    sleeptime = SLEEP_TIME - datetime.utcnow().second
    time.sleep(sleeptime)

    while True:
        print(time.strftime("%H:%M:%S", time.localtime()))

        # bulk_data = yf.download(stocks_list, period='2d', interval='1m')

        for stock in stock_obj_dict:
            stock_obj_dict[stock].get_stock_price_data('2d','1m')
        
        for result in thread_pool.map(monitor, list(stock_obj_dict.values())):
            print(result)
        
        for stock in stock_obj_dict:
            stock_obj_dict[stock].reset_price_data()

        sleeptime = SLEEP_TIME - datetime.utcnow().second
        print("sleeping for {} sec".format(sleeptime))
        time.sleep(sleeptime)

    

    
    
