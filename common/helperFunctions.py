import pandas as pd
import json
import os
import common.constants as constants

def percentageChange(x1, x2):
    return ((x1- x2)/x2)* 100

def get_stock_objects_from_json():
    STOCKS_JSON_FILE = os.getcwd() + "/" + constants.DERIVATIVE_LIST_FILENAME
    with open(STOCKS_JSON_FILE, "r") as file:
        stocks = json.load(file)
        commodity_list = stocks["data"].get("CommodityList", [])
        global_indices_list = stocks["data"].get("GlobalIndicesList", [])
        return stocks["data"]["UnderlyingList"], stocks["data"]["IndexList"], commodity_list, global_indices_list

def isNowInTimePeriod(startTime, endTime, nowTime): 
    if startTime < endTime: 
        return nowTime >= startTime and nowTime <= endTime 
    else: 
        #Over midnight: 
        return nowTime >= startTime or nowTime <= endTime 