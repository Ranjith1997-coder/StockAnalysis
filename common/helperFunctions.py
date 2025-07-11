import datetime as dt  
import pandas as pd
import json

STOCKS_JSON_FILE = "/Users/rkumark/Ranjith/StockAnalysis/final_derivatives_list.json"

def percentageChange(x1, x2):
    return ((x1- x2)/x2)* 100

def zd_rate_of_change(series: pd.Series, period= 10):
    return percentageChange(series.iloc[-1], series.iloc[-1*period])

def get_stock_objects_from_json():
    with open(STOCKS_JSON_FILE, "r") as file:
        stocks = json.load(file)
        return stocks


def isNowInTimePeriod(startTime, endTime, nowTime): 
    if startTime < endTime: 
        return nowTime >= startTime and nowTime <= endTime 
    else: 
        #Over midnight: 
        return nowTime >= startTime or nowTime <= endTime 