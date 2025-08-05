import sys
import os
sys.path.append(os.getcwd())

from zerodha.zerodha_ticker import KiteTicker
from zerodha.zerodha_constants import ZERODHA_ENC_TOKEN, ZERODHA_USERNAME
import common.shared as shared

from common.Stock import Stock
from common.logging_util import logger
import pprint

kt : KiteTicker|None = None

def on_ticks(ws, ticks):
    # Callback to receive ticks.
    pprint.pprint(ticks)
    for tick in ticks:
        data = {
            "ltp" : tick["last_price"],
            'volume' : tick["last_price"],
            'buy_quantity' : tick["total_buy_quantity"],
            'sell_quantity' : tick["total_sell_quantity"]
        }
        
        

def on_connect(ws : KiteTicker, response):
    # Callback on successful connect.
    # Subscribe to a list of instrument_tokens (RELIANCE and ACC here).
    logger.info("Successfully connected. Response: {}".format(response))
    instrument_tokens = list(shared.stock_token_obj_dict.keys())

    ws.subscribe(instrument_tokens)
    ws.set_mode(ws.MODE_FULL, instrument_tokens)

def on_close(ws: KiteTicker, code, reason):
    # On connection close stop the event loop.
    # Reconnection will not happen after executing `ws.stop()`
    logger.info("Entering On close callback")
    ws.close()
    ws.stop()

def on_error(ws: KiteTicker, code, reason):
    print("code: {}".format(code))
    print("reason: {}".format(reason))


def zerodha_init():
    global kt

    kt = KiteTicker("kitefront", ZERODHA_USERNAME , ZERODHA_ENC_TOKEN, root="wss://ws.zerodha.com")
    kt.on_ticks = on_ticks
    kt.on_connect = on_connect
    kt.on_close = on_close
    kt.on_error = on_error

    kt.connect()
    logger.info("Zerodha initialized")

if __name__ == "__main__":
    
    ticker =[{
                "name": "HDFC Bank",
                "tradingsymbol": "HDFCBANK",
                "instrument_token": 128046084
            },
            {
                "name": "RELIANCE",
                "tradingsymbol": "RELIANCE",
                "instrument_token": 128046083
            },]
    
    for t in ticker:
        shared.stock_token_obj_dict[t["instrument_token"]] = Stock(t["name"], t["tradingsymbol"])
    zerodha_init()
    
    










