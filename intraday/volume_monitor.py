import sys
import os
sys.path.append(os.getcwd())

import common.constants as constants
from common.Stock import Stock


TIMES_VOLUME = 10

def check_for_increase_in_volume_and_price(curr_vol, prev_vol, curr_vol_sma, curr_price, prev_price):
    
    
    if curr_vol_sma != 'NaN' and curr_vol > TIMES_VOLUME * prev_vol \
       and curr_vol > curr_vol_sma \
        and curr_price > prev_price:
        return True
    return False

def check_for_increase_in_volume_and_decrease_in_price(curr_vol, prev_vol, curr_vol_sma, curr_price, prev_price):
    
    
    if curr_vol_sma != 'NaN' and curr_vol > TIMES_VOLUME * prev_vol \
       and curr_vol > curr_vol_sma \
        and curr_price < prev_price:
        return True
    return False

def check_for_decrease_in_volume(curr_vol, prev_vol):
    if curr_vol < prev_vol:
        return True
    return False



        

        
    
