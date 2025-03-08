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
import pandas as pd
import pandas_ta as ta

stock = {
    "ABB": {
            "instrument_token": 3329,
            "tradingsymbol": "ABB",
            "name": "ABB INDIA",
            "instrument_type": "EQ"
      },
}

ticket = Stock(stock["ABB"]["name"], stock["ABB"]["tradingsymbol"])
ticket.get_stock_price_data('5d','5m')














