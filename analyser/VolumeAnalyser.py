import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
from common.helperFunctions import percentageChange
from collections import namedtuple
import common.shared as shared


class VolumeAnalyser(BaseAnalyzer):
    TIMES_VOLUME = 0
    VOLUME_PRICE_THRESHOLD = 0
    VOLUME_MA_PERIOD = 0
    def __init__(self) -> None:
        self.analyserName = "Volume Analyser"
        super().__init__()
    
    def reset_constants(self):

        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            VolumeAnalyser.VOLUME_PRICE_THRESHOLD = 0.5   
            VolumeAnalyser.TIMES_VOLUME = 10
            VolumeAnalyser.VOLUME_MA_PERIOD = 20
        else:
            VolumeAnalyser.VOLUME_PRICE_THRESHOLD = 5  
            VolumeAnalyser.TIMES_VOLUME = 3
            VolumeAnalyser.VOLUME_MA_PERIOD = 50
        logger.debug(f"VolumeAnalyser constants reset for mode {shared.app_ctx.mode.name}")
        logger.debug(f"TIMES_VOLUME = {VolumeAnalyser.TIMES_VOLUME} ,VOLUME_PRICE_THRESHOLD = {VolumeAnalyser.VOLUME_PRICE_THRESHOLD}")

    @BaseAnalyzer.both
    def analyse_volume_and_price(self, stock: Stock):
        try : 
            logger.debug(f'Inside analyse_volume_and_price for stock {stock.stock_symbol}')
            curr_data = stock.current_equity_data
            prev_data = stock.previous_equity_data

            curr_vol = curr_data['Volume'] 
            prev_vol = prev_data["Volume"]
            if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
                curr_vol_sma = stock.priceData['Volume'].iloc[-VolumeAnalyser.VOLUME_MA_PERIOD:].ewm(span=VolumeAnalyser.VOLUME_MA_PERIOD, adjust=False).mean().iloc[-1]
            else:
                curr_vol_sma = stock.priceData['Volume'].iloc[-VolumeAnalyser.VOLUME_MA_PERIOD:].mean()
            curr_price = curr_data['Close']
            prev_price = prev_data['Close']
            VolumeAnalysis = namedtuple("VolumeAnalysis", ["Volume_rate_percent", "price_change_percent"])
            if curr_vol_sma != 'NaN' and curr_vol > VolumeAnalyser.TIMES_VOLUME * prev_vol \
            and curr_vol > curr_vol_sma \
                and curr_price > prev_price \
                    and  percentageChange(curr_price, prev_price) >  VolumeAnalyser.VOLUME_PRICE_THRESHOLD :
                vol_rate = ((curr_data['Volume'] - prev_data["Volume"])/prev_data["Volume"]) * 100
                price_inc = ((curr_data['Close'] - prev_data["Close"])/prev_data["Close"]) * 100
                stock.set_analysis("BULLISH", "Volume", VolumeAnalysis(Volume_rate_percent=vol_rate, price_change_percent=price_inc))
                return True
            elif curr_vol_sma != 'NaN' and curr_vol > VolumeAnalyser.TIMES_VOLUME * prev_vol \
            and curr_vol > curr_vol_sma \
                and curr_price < prev_price \
                    and percentageChange(curr_price, prev_price) < (VolumeAnalyser.VOLUME_PRICE_THRESHOLD * -1):
                vol_rate = ((curr_data['Volume'] - prev_data["Volume"])/prev_data["Volume"]) * 100
                price_inc = ((curr_data['Close'] - prev_data["Close"])/prev_data["Close"]) * 100
                stock.set_analysis("BEARISH", "Volume", VolumeAnalysis(Volume_rate_percent=vol_rate, price_change_percent=price_inc))
            return False
        except Exception as e:
            logger.error(f"Error in analyse_volume_and_price for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False


 