import pandas as pd
import json
import os
import common.constants as constants
from lib.logging_util import get_logger
logger = get_logger("common")

def percentageChange(x1, x2):
    try:
        return ((x1 - x2) / x2) * 100
    except ZeroDivisionError:
        logger.debug("[helper] percentageChange division by zero: x1=%s x2=%s", x1, x2)
        return 0.0
    except Exception as e:
        logger.debug("[helper] percentageChange failed: x1=%s x2=%s: %s", x1, x2, e)
        return 0.0

def get_stock_objects_from_json():
    STOCKS_JSON_FILE = os.getcwd() + "/" + constants.DERIVATIVE_LIST_FILENAME
    try:
        with open(STOCKS_JSON_FILE, "r") as file:
            stocks = json.load(file)
    except FileNotFoundError:
        logger.error("[helper] Derivatives list not found: %s", STOCKS_JSON_FILE, exc_info=True)
        raise
    except json.JSONDecodeError:
        logger.error("[helper] Invalid JSON in %s", STOCKS_JSON_FILE, exc_info=True)
        raise
    commodity_list = stocks["data"].get("CommodityList", [])
    global_indices_list = stocks["data"].get("GlobalIndicesList", [])
    return stocks["data"]["UnderlyingList"], stocks["data"]["IndexList"], commodity_list, global_indices_list

def isNowInTimePeriod(startTime, endTime, nowTime): 
    if startTime < endTime: 
        return nowTime >= startTime and nowTime <= endTime 
    else: 
        #Over midnight: 
        return nowTime >= startTime or nowTime <= endTime 