import sys
import os
sys.path.append(os.getcwd())
from pprint import pprint

import logging
from kiteconnect import KiteTicker
from common.shared import stock_token_obj_dict 
from common.Stock import Stock
from datetime import datetime
import threading , time
from multiprocessing.pool import ThreadPool
from copy import deepcopy
from common.helperFunctions import get_stock_objects_from_json

logging.basicConfig(level=logging.INFO)

TESTING = True

stocks = {}

kws = None

ENC_TOKEN = "8ssCI0NEWHD4fYN%2F53tr3Ti4nWyFCHB9IsV%2B66k9%2FRzv%2FVsjmFKP2gbiyu695dsYWjKtQKN8mJI7RBF49s8hqyt5UCeFQ4N9Nn6xtXLR3h4AaAiqqpbfTw%3D%3D&uid=1733077009401"
USERNAME = "QR5450"

initial_condition = threading.Condition()
callback_count = 0
if TESTING:
    TESTING_DATA_COUNT = 10
    MAX_LENGTH_OF_CALLBACK = 1
    WAIT_TIME_SECONDS = 1
else:
    MAX_LENGTH_OF_CALLBACK = 100
    WAIT_TIME_SECONDS = 10


init_done = False
# Initialise

def monitor_zd_time_series_data(stock: Stock):
    logging.debug(f"Monitoring {stock.stockName}")

    if not stock.zd_data["data_count"] >= MAX_LENGTH_OF_CALLBACK:
        logging.info(f" {stock.stockName} did not have enough data. Data collected = {stock.zd_data["data_count"]}")
    
    with stock.zd_data_mutux:
        stock_data = deepcopy(stock.zd_data)


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
        
        logging.info("Monitor thread has started...")

        event = threading.Event()
        thread_pool = ThreadPool(processes=10)
        while not event.wait(WAIT_TIME_SECONDS):
            for result in thread_pool.map(monitor_zd_time_series_data, list(stock_token_obj_dict.values())):
                print(result)


def on_ticks(ws, ticks):
    # Callback to receive ticks.
    global callback_count
    print(ticks)
    for tick in ticks:
        data = {
            "ltp" : tick["last_price"],
            'volume' : tick["last_price"],
            'buy_quantity' : tick["total_buy_quantity"],
            'sell_quantity' : tick["total_sell_quantity"]
        }
        if TESTING:
            for _ in range(TESTING_DATA_COUNT):
                with stock_token_obj_dict[tick["instrument_token"]].zd_data_mutux:
                    stock_token_obj_dict[tick["instrument_token"]].zd_data["series_data"].append(data)
                    stock_token_obj_dict[tick["instrument_token"]].zd_data["last_updated_time_stamp"].append(datetime.now().replace(microsecond=0))
                    stock_token_obj_dict[tick["instrument_token"]].zd_data["open"] = tick["ohlc"]["open"]
                    stock_token_obj_dict[tick["instrument_token"]].zd_data["high"] = tick["ohlc"]["high"]
                    stock_token_obj_dict[tick["instrument_token"]].zd_data["low"] = tick["ohlc"]["low"]
                    stock_token_obj_dict[tick["instrument_token"]].zd_data["change"] = tick["change"]
                    stock_token_obj_dict[tick["instrument_token"]].zd_data["data_count"] += 1

                data["ltp"] += 1
                data["buy_quantity"] += 1
                data["sell_quantity"] += 1
                data["volume"] += 1 

                time.sleep(1)
        else:
            with stock_token_obj_dict[tick["instrument_token"]].zd_data_mutux:
                stock_token_obj_dict[tick["instrument_token"]].zd_data["series_data"].append(data)
                stock_token_obj_dict[tick["instrument_token"]].zd_data["last_updated_time_stamp"].append(datetime.now().replace(microsecond=0))
                stock_token_obj_dict[tick["instrument_token"]].zd_data["open"] = tick["ohlc"]["open"]
                stock_token_obj_dict[tick["instrument_token"]].zd_data["high"] = tick["ohlc"]["high"]
                stock_token_obj_dict[tick["instrument_token"]].zd_data["low"] = tick["ohlc"]["low"]
                stock_token_obj_dict[tick["instrument_token"]].zd_data["change"] = tick["change"]
                stock_token_obj_dict[tick["instrument_token"]].zd_data["data_count"] += 1
            
        # print( stock_token_obj_dict[tick["instrument_token"]].zd_data)
    callback_count += 1

    if  callback_count == MAX_LENGTH_OF_CALLBACK:
        with initial_condition:
            global init_done
            init_done = True
            logging.info("Collected {} of data. Notifying the monitor thread..".format(callback_count))
            initial_condition.notify()
        
        

def on_connect(ws, response):
    # Callback on successful connect.
    # Subscribe to a list of instrument_tokens (RELIANCE and ACC here).
    logging.info("Entering On connect callback")
    instrument_tokens = list(stock_token_obj_dict.keys())

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
    
    stocks = get_stock_objects_from_json()

    if not TESTING:
        for stock in stocks:
            ticker = Stock(stocks[stock]["name"], stocks[stock]["tradingsymbol"])
            stock_token_obj_dict[stocks[stock]["instrument_token"]] = ticker
    else:
        count = 0
        for stock in stocks:
            ticker = Stock(stocks[stock]["name"], stocks[stock]["tradingsymbol"])
            stock_token_obj_dict[stocks[stock]["instrument_token"]] = ticker
            count += 1

            if count == 2:
                break
    zerodha_init()
    # kws.connect(threaded=True)
    kws.connect()










