import yfinance as yf
from pandas import Series
from constants import stocks
from math import log,exp


def getReturnsForData(closeSeries):

    skip = 1
    prevdata = None

    dailyReturnlist = []
    for data in closeSeries:

        if skip == 1:
            skip = 0
            prevdata = data
            continue

        dailyReturnlist.append((log(data/prevdata))*100)
        prevdata = data

    return dailyReturnlist

if __name__ == "__main__":

    No_of_days_to_Expire = int(input("Number of days to expire : "))

    for stock in stocks:
        data = yf.download(stocks[stock] , period="3y")["Close"]
        # print(data.iloc[-1], data.__class__)
        # print(data[-9:])
        dailyReturns = Series(getReturnsForData(data))

        # print(dailyReturns,len(dailyReturns ) ,end='\n')
        mean = dailyReturns.mean()
        standardDeviation = dailyReturns.std()
        # print(mean)
        # print(standardDeviation)

        mean_at_expiry = mean * No_of_days_to_Expire
        std_at_expiry = standardDeviation * (pow(No_of_days_to_Expire,0.5))
        # print(mean_at_expiry)
        # print(std_at_expiry)

        lowerLimit_1SD = (exp((mean_at_expiry - std_at_expiry)/100)) * data.iloc[-1]
        upperLimit_1SD = (exp((mean_at_expiry + std_at_expiry)/100)) * data.iloc[-1]

        lowerLimit_2SD = (exp((mean_at_expiry - (2 * std_at_expiry))/100)) * data.iloc[-1]
        upperLimit_2SD = (exp((mean_at_expiry + (2 * std_at_expiry))/100)) * data.iloc[-1]

        print("******************************************************")
        print("Stock Name       : {}".format(stock))
        print("Current Price    : {}".format(data.iloc[-1]))
        print("Daily Volatility : {}".format(standardDeviation))
        print("Volatily after {} days: {}".format(No_of_days_to_Expire,std_at_expiry))
        print("Range for 1SD    : {} - {}".format(lowerLimit_1SD,upperLimit_1SD))
        print("Range for 2SD    : {} - {}".format(lowerLimit_2SD,upperLimit_2SD))
        print("******************************************************")




