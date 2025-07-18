import sys
import os
sys.path.append(os.getcwd())
import common.constants as constant

RSI_UPPER_THRESHOLD = 80
RSI_LOWER_THRESHOLD = 20
ATR_THRESHOLD = 0.97
THREE_CONT_INC_OR_DEC_THRESHOLD = 0
TWO_CONT_INC_OR_DEC_THRESHOLD = 0
MARUBASU_THRESHOLD = 0

def set_candle_stick_constants():
    global THREE_CONT_INC_OR_DEC_THRESHOLD
    global TWO_CONT_INC_OR_DEC_THRESHOLD 
    global MARUBASU_THRESHOLD
    if constant.mode.name == constant.Mode.INTRADAY.name:
        THREE_CONT_INC_OR_DEC_THRESHOLD = 1.5  
        TWO_CONT_INC_OR_DEC_THRESHOLD = 1    
        MARUBASU_THRESHOLD = 1.5
    else:
        THREE_CONT_INC_OR_DEC_THRESHOLD = 6  
        TWO_CONT_INC_OR_DEC_THRESHOLD = 4   
        MARUBASU_THRESHOLD = 3 

def is_rsi_above_threshold(rsi_value):
    # if rsi_value != 'NaN' and rsi_value > RSI_UPPER_THRESHOLD:
    if rsi_value > RSI_UPPER_THRESHOLD:
        return True
    return False

def is_rsi_below_threshold(rsi_value):
    # if rsi_value != 'NaN' and rsi_value < RSI_LOWER_THRESHOLD:
    if rsi_value < RSI_LOWER_THRESHOLD:
        return True
    return False

def is_atr_rank_above_threshold(atr_rank):
    if atr_rank >= ATR_THRESHOLD:
        return True
    return False

def is_price_at_upper_BB(close_price, upper_bb):
    if close_price >= upper_bb:
        return True
    return False

def is_price_at_lower_BB(close_price, lower_bb):
    if close_price <= lower_bb:
        return True
    return False

def is_bullish_candle_stick_pattern(data):

    if constant.mode.name == constant.Mode.INTRADAY.name:
        if (data["MARUBASU"].item() > MARUBASU_THRESHOLD ) :
            return (True , "Marubasu, rate: {:.2f}%".format(data["MARUBASU"].item()) )
        # if (data["CDL_3WHITESOLDIERS"] > 0.0 ):
        #     return (True , "3_white_solders")
        # elif (data["CDL_HARAMI"] > 0.0 ) :
        #     return (True , "Harami")
        # elif (data["CDL_ENGULFING"] > 0.0 ):
        #     return (True , "Engulf")
        elif (data["3_CONT_INC_OR_DEC"].item() > THREE_CONT_INC_OR_DEC_THRESHOLD):
            return (True , "3_cont_inc, rate:{:.2f}%".format(data["3_CONT_INC_OR_DEC"].item()))
        elif (data["2_CONT_INC_OR_DEC"].item() > TWO_CONT_INC_OR_DEC_THRESHOLD):
            return (True , "2_cont_inc, rate:{:.2f}%".format(data["2_CONT_INC_OR_DEC"].item()))
        else:
            return (False , "no_pattern")
    else:
        if (data["MARUBASU"].item() > MARUBASU_THRESHOLD ) :
            return (True , "Marubasu, rate: {:.2f}%".format(data["MARUBASU"].item()) )
        # if (data["CDL_3WHITESOLDIERS"] > 0.0 ):
        #     return (True , "3_white_solders")
        # elif (data["CDL_HARAMI"] > 0.0 ) :
        #     return (True , "Harami")
        # elif (data["CDL_MARUBOZU"] > 0.0 ) :
        #     return (True , "Marubasu")
        # elif (data["CDL_ENGULFING"] > 0.0 ):
        #     return (True , "Engulf")
        elif (data["3_CONT_INC_OR_DEC"].item() > THREE_CONT_INC_OR_DEC_THRESHOLD):
            return (True , "3_cont_inc, rate:{:.2f}%".format(data["3_CONT_INC_OR_DEC"].item()))
        elif (data["2_CONT_INC_OR_DEC"].item() > TWO_CONT_INC_OR_DEC_THRESHOLD):
            return (True , "2_cont_inc, rate:{:.2f}%".format(data["2_CONT_INC_OR_DEC"].item()))
        else:
            return (False , "no_pattern")


def is_bearish_candle_stick_pattern(data):
    if constant.mode.name == constant.Mode.INTRADAY.name:

        if (data["MARUBASU"].item() < (MARUBASU_THRESHOLD * -1) ) :
            return (True , "Marubasu, rate:{:.2f}%".format(data["MARUBASU"].item()))
        # if (data["CDL_3BLACKCROWS"] < 0.0 ):
        #     return (True , "3_black_crows")
        # elif (data["CDL_HARAMI"] < 0.0 ) :
        #     return (True , "Harami")
        # elif (data["CDL_ENGULFING"] < 0.0 ):
        #     return (True , "Engulf")
        elif (data["3_CONT_INC_OR_DEC"].item() < (THREE_CONT_INC_OR_DEC_THRESHOLD * -1) ):
            return (True , "3_cont_dec, rate:{:.2f}%".format(data["3_CONT_INC_OR_DEC"].item()))
        elif (data["2_CONT_INC_OR_DEC"].item() < (TWO_CONT_INC_OR_DEC_THRESHOLD * -1) ):
            return (True , "2_cont_dec, rate:{:.2f}%".format(data["2_CONT_INC_OR_DEC"].item()))
        else:
            return (False , "no_pattern")
    else:
        if (data["MARUBASU"].item() < (MARUBASU_THRESHOLD * -1) ) :
            return (True , "Marubasu, rate:{:.2f}%".format(data["MARUBASU"].item()))
        # if (data["CDL_3BLACKCROWS"] < 0.0 ):
        #     return (True , "3_black_crows")
        # elif (data["CDL_HARAMI"] < 0.0 ) :
        #     return (True , "Harami")
        # elif (data["CDL_MARUBOZU"] < 0.0 ) :
        #     return (True , "Marubasu")
        # elif (data["CDL_ENGULFING"] < 0.0 ):
        #     return (True , "Engulf")
        elif (data["3_CONT_INC_OR_DEC"].item() < (THREE_CONT_INC_OR_DEC_THRESHOLD * -1) ):
            return (True , "3_cont_dec, rate:{:.2f}%".format(data["3_CONT_INC_OR_DEC"].item()))
        elif (data["2_CONT_INC_OR_DEC"].item() < (TWO_CONT_INC_OR_DEC_THRESHOLD * -1) ):
            return (True , "2_cont_dec, rate:{:.2f}%".format(data["2_CONT_INC_OR_DEC"].item()))
        else:
            return (False , "no_pattern")

