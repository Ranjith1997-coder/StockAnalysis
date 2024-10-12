import sys
import os
sys.path.append(os.getcwd())
from common.constants import mode,Mode

RSI_UPPER_THRESHOLD = 80
RSI_LOWER_THRESHOLD = 20
ATR_THRESHOLD = 95
if mode.name == Mode.INTRADAY.name:
    THREE_CONT_INC_OR_DEC_THRESHOLD = 1.5  
    TWO_CONT_INC_OR_DEC_THRESHOLD = 1    
    MARUBASU_THRESHOLD = 1.5
else:
    THREE_CONT_INC_OR_DEC_THRESHOLD = 5  
    TWO_CONT_INC_OR_DEC_THRESHOLD = 3   
    MARUBASU_THRESHOLD = 3 

def is_rsi_above_threshold(rsi_value):
    if rsi_value != 'NaN' and rsi_value > RSI_UPPER_THRESHOLD:
        return True
    return False

def is_rsi_below_threshold(rsi_value):
    if rsi_value != 'NaN' and rsi_value < RSI_LOWER_THRESHOLD:
        return True
    return False

def is_atr_rank_above_threshold(atr_rank):
    if atr_rank >= ATR_THRESHOLD:
        return True
    return False

def is_bullish_candle_stick_pattern(data):

    if mode.name == Mode.INTRADAY.name:

        if (data["CDL_3WHITESOLDIERS"] > 0.0 ):
            return (True , "3_white_solders")
        # elif (data["CDL_HARAMI"] > 0.0 ) :
        #     return (True , "Harami")
        elif (data["MARUBASU"] > MARUBASU_THRESHOLD ) :
            return (True , "Marubasu, rate: {}".format(data["MARUBASU"]) )
        # elif (data["CDL_ENGULFING"] > 0.0 ):
        #     return (True , "Engulf")
        elif (data["3_CONT_INC_OR_DEC"] > THREE_CONT_INC_OR_DEC_THRESHOLD):
            return (True , "3_cont_inc, rate:{}".format(data["3_CONT_INC_OR_DEC"]))
        elif (data["2_CONT_INC_OR_DEC"] > TWO_CONT_INC_OR_DEC_THRESHOLD):
            return (True , "2_cont_inc, rate:{}".format(data["2_CONT_INC_OR_DEC"]))
        else:
            return (False , "no_pattern")
    else:
        if (data["CDL_3WHITESOLDIERS"] > 0.0 ):
            return (True , "3_white_solders")
        elif (data["CDL_HARAMI"] > 0.0 ) :
            return (True , "Harami")
        # elif (data["CDL_MARUBOZU"] > 0.0 ) :
        #     return (True , "Marubasu")
        elif (data["CDL_ENGULFING"] > 0.0 ):
            return (True , "Engulf")
        elif (data["3_CONT_INC_OR_DEC"] > THREE_CONT_INC_OR_DEC_THRESHOLD):
            return (True , "3_cont_inc, rate:{}".format(data["3_CONT_INC_OR_DEC"]))
        elif (data["2_CONT_INC_OR_DEC"] > TWO_CONT_INC_OR_DEC_THRESHOLD):
            return (True , "2_cont_inc, rate:{}".format(data["2_CONT_INC_OR_DEC"]))
        else:
            return (False , "no_pattern")


def is_bearish_candle_stick_pattern(data):
    if mode.name == Mode.INTRADAY.name:

        if (data["CDL_3BLACKCROWS"] < 0.0 ):
            return (True , "3_black_crows")
        # elif (data["CDL_HARAMI"] < 0.0 ) :
        #     return (True , "Harami")
        elif (data["MARUBASU"] < (MARUBASU_THRESHOLD * -1) ) :
            return (True , "Marubasu, rate:{}".format(data["MARUBASU"]))
        # elif (data["CDL_ENGULFING"] < 0.0 ):
        #     return (True , "Engulf")
        elif (data["3_CONT_INC_OR_DEC"] < (THREE_CONT_INC_OR_DEC_THRESHOLD * -1) ):
            return (True , "3_cont_dec, rate:{}".format(data["3_CONT_INC_OR_DEC"]))
        elif (data["2_CONT_INC_OR_DEC"] < (TWO_CONT_INC_OR_DEC_THRESHOLD * -1) ):
            return (True , "2_cont_dec, rate:{}".format(data["2_CONT_INC_OR_DEC"]))
        else:
            return (False , "no_pattern")
    else:
        if (data["CDL_3BLACKCROWS"] < 0.0 ):
            return (True , "3_black_crows")
        elif (data["CDL_HARAMI"] < 0.0 ) :
            return (True , "Harami")
        # elif (data["CDL_MARUBOZU"] < 0.0 ) :
        #     return (True , "Marubasu")
        elif (data["CDL_ENGULFING"] < 0.0 ):
            return (True , "Engulf")
        elif (data["3_CONT_INC_OR_DEC"] < (THREE_CONT_INC_OR_DEC_THRESHOLD * -1) ):
            return (True , "3_cont_dec, rate:{}".format(data["3_CONT_INC_OR_DEC"]))
        elif (data["2_CONT_INC_OR_DEC"] < (TWO_CONT_INC_OR_DEC_THRESHOLD * -1) ):
            return (True , "2_cont_dec, rate:{}".format(data["2_CONT_INC_OR_DEC"]))
        else:
            return (False , "no_pattern")

