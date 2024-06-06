import sys
import os
sys.path.append(os.getcwd())


from common.Stock import Stock
import common.constants as constants
from  intraday.volume_monitor import check_for_increase_in_volume_and_price, check_for_decrease_in_volume


def back_test_volume_monitor(buy_function, sell_function):
    
    total_count = 0
    total_positive_returns = 0
    total_negative_returns = 0

    for stock in constants.stocks:
        ticker = Stock(stock, constants.stocks[stock]+".NS", constants.stocks[stock])
        ticker.get_stock_price_data('5d', '1m')
        ticker.compute_sma_of_volume(20)
        prev_data = None
        shares_bought = False
        count = 0
        buy_price = 0
        sell_price = 0
        positive_return = 0
        negative_return = 0
        for index , row in ticker.priceData.iterrows():
            if type(prev_data) == type(None):
                prev_data = row
                continue
            else:
                if not shares_bought :
                    if buy_function(row['Volume'], 
                                prev_data["Volume"],
                                row['Vol_SMA_20'],
                                row['Close'],
                                prev_data['Close']):
                        buy_price = row['Close']
                        shares_bought = True
                        print("*****************************************************")
                        print("Stock Name : {} \n buy Timestamp : {} \n buy_price = {}".format(ticker.stockName, str(index), str(buy_price)))
                else:
                    if shares_bought and sell_function(row['Volume'], 
                                prev_data["Volume"]):
                        sell_price = row['Close']
                        returns = ((sell_price - buy_price) / buy_price) * 100
                        if returns > 0:
                            positive_return += 1
                        else :
                            negative_return += 1

                        shares_bought = False
                        print(" sell price : {}\n sell timestamp = {} \n returns = {}". format(row["Close"],str(index), returns))
            prev_data = row
        
        print("*****************************************************")
        print("Summary : \n\t total count = {} \n\t Positive return = {} \n\t negative return = {} \n\t ".format(positive_return+negative_return, positive_return, negative_return))            
        print("-----------------------------------------------------")
        total_positive_returns += positive_return
        total_negative_returns += negative_return
        total_count += positive_return + negative_return

    print("-----------------------------------------------------")
    print("back test Summary : \n\t total count = {} \n\t Positive return = {} \n\t negative return = {} \n\t ".format(total_count, total_positive_returns, total_negative_returns))

# back_test_volume_monitor(check_for_increase_in_volume_and_price, check_for_decrease_in_volume)

stock = "ACC_Ltd"

ticker = Stock(stock, constants.stocks[stock]+".NS", constants.stocks[stock])
ticker.get_stock_price_data('2d', '1m')
ticker.compute_sma_of_volume(20)

curr_data = curr_data = ticker.priceData.iloc[-2]


print()
