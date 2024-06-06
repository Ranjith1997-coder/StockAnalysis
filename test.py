import sys
import os
sys.path.append(os.getcwd())

from common.push_notification import pushbullet_notif, telegram_notif
from common.Stock import Stock
import common.constants as constants
from datetime import datetime
import time
import multitasking 
from multiprocessing.pool import ThreadPool
import yfinance as yf


items = []
stock_obj_dict = {}


def generate_notif_message(stock):
    message = """Stock : {} \nTimestamp : {} \n""".format(constants.stocks[stock.stockName], stock.analysis["Timestamp"])
    
    if stock.analysis["BULLISH"]:
        bullish_trend = stock.analysis["BULLISH"]
        message += "BULLISH : \n"
        if "Volume" in bullish_trend.keys():
            message += """  volume increase : {:.2f} \n  price increase : {:.2f} \n """.format(bullish_trend["Volume"]["Volume_rate_percent"], bullish_trend["Volume"]["Price_inc_percent"])

        if "rsi" in bullish_trend.keys():
            message += """  rsi value : {:.2f} \n""".format(bullish_trend["rsi"]["value"])
    
    if stock.analysis["BEARISH"]:
        bearish_trend = stock.analysis["BEARISH"]
        message += "BEARISH : \n"
        if "Volume" in bearish_trend.keys():
            message += """  volume increase : {:.2f} \n  price decrease : {:.2f} \n """.format(bearish_trend["Volume"]["Volume_rate_percent"], bearish_trend["Volume"]["Price_dec_percent"])

        if "rsi" in bearish_trend.keys():
            message += """  rsi value : {:.2f} \n""".format(bearish_trend["rsi"]["value"])
    
    return message



if __name__ == '__main__':
    count = 0
    ticker = None
    for stock in constants.stocks:
        items.append(constants.stocks[stock]+".NS")
        ticker = Stock(stock, constants.stocks[stock]+".NS", constants.stocks[stock])
        count += 1
        if count == 1:
            break
    
    ticker.get_stock_price_data('2d','1m')
    ticker.compute_rsi()
    ticker.analysis ={"Timestamp" : time.strftime("%H:%M:%S", time.localtime()),
                        "BULLISH":{"Volume":{ "Volume_rate_percent" : 2, 
                                              "Price_inc_percent": 2},
                                    "rsi": {"value": 5}},
                        "BEARISH":{"Volume":{ "Volume_rate_percent" : 2, 
                                              "Price_dec_percent": 2},
                                    "rsi": {"value": 5}},
                        "NEUTRAL":{}}
    message = generate_notif_message(ticker)    

    telegram_notif(message)
    














