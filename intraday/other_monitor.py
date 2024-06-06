

RSI_UPPER_THRESHOLD = 80
RSI_LOWER_THRESHOLD = 20
ATR_THRESHOLD = 95

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