from requests import get
from common.constants import OpstraURLs
import pandas as pd

def getMonthlyExpiry():

    res = get(OpstraURLs["OpstraMonthlyExpiryURL"])

    res = list(map(lambda x : x[1:-1],(res.text[1:-2].split(','))))
    # print(res)

    return res

def getweeklyExpiry():

    res = get(OpstraURLs["OpstraWeeklyExpiryURL"])
    res = list(map(lambda x: x[1:-1], (res.text[1:-2].split(','))))
    # print(res)
    return res

def getTickerList():
    res = get(OpstraURLs["OpstraTickerURL"])
    return res


def getIVChartData(ticker):
    res = get(OpstraURLs["IVChartURL"].format(ticker)).json()
    return (pd.DataFrame(res["events"]),pd.DataFrame(res["ivchart"]))

def get_FII_DII_Data():
    res = get(OpstraURLs["FII_DII_DATA_URL"]).json()
    return (res["daily"])



if __name__ == '__main__':
    print(getIVChartData("COALINDIA"))




