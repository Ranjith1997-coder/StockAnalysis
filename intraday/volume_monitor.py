import sys
import os
sys.path.append(os.getcwd())

from common.constants import mode,Mode
from common.helperFunctions import percentageChange

if mode.name == Mode.INTRADAY.name:
    VOLUME_PRICE_THRESHOLD = 0.5   
    MARUBASU_THRESHOLD = 1.5
else:
    THREE_CONT_INC_OR_DEC_THRESHOLD = 5  
    MARUBASU_THRESHOLD = 3 


TIMES_VOLUME = 10

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



        

        
    
