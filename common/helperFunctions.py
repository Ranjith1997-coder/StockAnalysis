import pandas as pd
import json
import os
import common.constants as constants

def percentageChange(x1, x2):
    return ((x1- x2)/x2)* 100

def zd_rate_of_change(series: pd.Series, period= 10):
    return percentageChange(series.iloc[-1], series.iloc[-1*period])

def get_stock_objects_from_json():
    STOCKS_JSON_FILE = os.getcwd() + "/" + constants.DERIVATIVE_LIST_FILENAME
    with open(STOCKS_JSON_FILE, "r") as file:
        stocks = json.load(file)
        commodity_list = stocks["data"].get("CommodityList", [])
        return stocks["data"]["UnderlyingList"], stocks["data"]["IndexList"], commodity_list

def get_stock_OHLCV_from_json():
    STOCKS_JSON_FILE = os.getcwd() + "/" + constants.STOCK_DATA_FILENAME
    with open(STOCKS_JSON_FILE, "r") as file:
        stocks = json.load(file)
        return stocks["data"]["UnderlyingList"]

def save_stock_objects_into_json(stockObjdict: dict):
    STOCKS_JSON_FILE = os.getcwd() + "/" + constants.STOCK_DATA_FILENAME
    with open(STOCKS_JSON_FILE, "w") as file:
        stock_list = []
        data = {"data": {"IndexList": [], "UnderlyingList": []}}
        for instrument_token, stock in stockObjdict.items():
            currdata = stock.current_equity_data
            stock_list.append({"tradingsymbol": stock.stock_symbol,
                               "OPEN" : currdata["Open"],
                               "CLOSE" : currdata["Close"],
                               "HIGH" : currdata["High"],
                               "LOW" : currdata["Low"],
                               "VOLUME" : currdata["Volume"]
                               })
        
        data["data"]["UnderlyingList"] = stock_list
        json.dump(data, file, indent=4)


def isNowInTimePeriod(startTime, endTime, nowTime): 
    if startTime < endTime: 
        return nowTime >= startTime and nowTime <= endTime 
    else: 
        #Over midnight: 
        return nowTime >= startTime or nowTime <= endTime 