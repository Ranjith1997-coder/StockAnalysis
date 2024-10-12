import sys
import os
sys.path.append(os.getcwd())
from pprint import pprint

import logging
from kiteconnect import KiteTicker
from common.shared import stock_token_obj_dict 
import common.constants as constants
from common.Stock import Stock
from datetime import datetime
from common.push_notification import telegram_notif
import threading 

logging.basicConfig(level=logging.INFO)

TESTING = True

kws = None

ENC_TOKEN = "nImo9AHJms%2FilCpP2%2Fb22uUzDZjA%2BxNSiZI4WVsjQIflmHJ%2BRt%2FeMffx82xzRnj8U7Jd%2Fg2iXbYIN01RNKdwdQ05XDQoqe%2BxrBSVuE%2FM7iGmrkofXSbh7w%3D%3D"
USERNAME = "QR5450"

initial_condition = threading.Condition()
init_done = False
# Initialise

class Monitor_Thread(threading.Thread): 
    def __init__(self, wait_time): 
        threading.Thread.__init__(self) 
        self.wait_time = wait_time
 
        # helper function to execute the threads
    def run(self): 
        with initial_condition:
            while not init_done:
                logging.info("Monitor thread is waiting...")
                initial_condition.wait()



def on_ticks(ws, ticks):
    # Callback to receive ticks.
    for tick in ticks:
        data = {
            "ltp" : tick["last_price"],
            'volume' : tick["last_price"],
            'buy_quantity' : tick["total_buy_quantity"],
            'sell_quantity' : tick["total_sell_quantity"]
        }
        stock_token_obj_dict[tick["instrument_token"]].zd_data["series_data"].append(data)
        stock_token_obj_dict[tick["instrument_token"]].zd_data["last_updated_time_stamp"].append(datetime.now().replace(microsecond=0))
        stock_token_obj_dict[tick["instrument_token"]].zd_data["open"] = tick["ohlc"]["open"]
        stock_token_obj_dict[tick["instrument_token"]].zd_data["high"] = tick["ohlc"]["high"]
        stock_token_obj_dict[tick["instrument_token"]].zd_data["low"] = tick["ohlc"]["low"]
        stock_token_obj_dict[tick["instrument_token"]].zd_data["change"] = tick["change"]
        print( stock_token_obj_dict[tick["instrument_token"]].zd_data)
        
    # print(type(ticks))
        

def on_connect(ws, response):
    # Callback on successful connect.
    # Subscribe to a list of instrument_tokens (RELIANCE and ACC here).
    logging.info("Entering On connect callback")
    instrument_tokens = list(stock_token_obj_dict.keys())

    if TESTING:
        #single
        ws.subscribe([instrument_tokens[0],instrument_tokens[1]])
        ws.set_mode(ws.MODE_FULL, [instrument_tokens[0],instrument_tokens[1]])
    else:
        ws.subscribe(instrument_tokens)
        ws.set_mode(ws.MODE_FULL, instrument_tokens)

def on_close(ws, code, reason):
    # On connection close stop the event loop.
    # Reconnection will not happen after executing `ws.stop()`
    logging.info("Entering On close callback")
    ws.close()
    ws.stop()

def on_error(ws, code, reason):
    print("code: {}".format(code))
    print("reason: {}".format(reason))


def initialize_monitor_thread():

    mon_thread = Monitor_Thread(5)
    mon_thread.start()



def zerodha_init():
    global kws

    kws = KiteTicker("kitefront", USERNAME , ENC_TOKEN)
    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.on_close = on_close
    kws.on_error = on_error

    initialize_monitor_thread()

    logging.info("Zerodha Init done")

if __name__ == "__main__":

    for stock in constants.stocks:
        ticker = Stock(constants.stocks[stock]["name"], constants.stocks[stock]["tradingsymbol"])
        stock_token_obj_dict[constants.stocks[stock]["instrument_token"]] = ticker
    zerodha_init()
    # kws.connect(threaded=True)
    kws.connect()










