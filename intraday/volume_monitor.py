import sys
import os
sys.path.append(os.getcwd())

import common.constants as constant
from common.helperFunctions import percentageChange

TIMES_VOLUME = 0
VOLUME_PRICE_THRESHOLD = 0

def set_volume_constants():
    global TIMES_VOLUME
    global VOLUME_PRICE_THRESHOLD
    if constant.mode.name == constant.Mode.INTRADAY.name:
        VOLUME_PRICE_THRESHOLD = 0.5   
        TIMES_VOLUME = 10
    else:
        VOLUME_PRICE_THRESHOLD = 5  
        TIMES_VOLUME = 3

def check_for_increase_in_volume_and_price(curr_vol, prev_vol, curr_vol_sma, curr_price, prev_price):

    if curr_vol_sma != 'NaN' and curr_vol > TIMES_VOLUME * prev_vol \
       and curr_vol > curr_vol_sma \
        and curr_price > prev_price \
            and  percentageChange(curr_price, prev_price) >  VOLUME_PRICE_THRESHOLD :
        return True
    return False

def check_for_increase_in_volume_and_decrease_in_price(curr_vol, prev_vol, curr_vol_sma, curr_price, prev_price):

    if curr_vol_sma != 'NaN' and curr_vol > TIMES_VOLUME * prev_vol \
       and curr_vol > curr_vol_sma \
        and curr_price < prev_price \
            and percentageChange(curr_price, prev_price) < (VOLUME_PRICE_THRESHOLD * -1):
        return True
    return False

def check_for_decrease_in_volume(curr_vol, prev_vol):
    if curr_vol < prev_vol:
        return True
    return False



        

        
    
