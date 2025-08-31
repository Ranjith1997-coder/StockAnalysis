import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
import common.shared as shared
from common.helperFunctions import percentageChange
from collections import namedtuple

class IVAnalyser(BaseAnalyzer):
    IV_PERCENTAGE_CHANGE = 30
    def __init__(self) -> None:
        self.analyserName = "Volume Analyser"
        super().__init__()
    
    def reset_constants(self):
        IVAnalyser.IV_TREND_CONTINUATION_DAYS = 3
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            IVAnalyser.IV_PERCENTAGE_CHANGE = 10  
        else:
            IVAnalyser.IV_PERCENTAGE_CHANGE = 20

        logger.debug(f"IVAnalyser constants reset for mode {shared.app_ctx.mode.name}")
        logger.debug(f"IV_PERCENTAGE_CHANGE = {IVAnalyser.IV_PERCENTAGE_CHANGE}")

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_spike_in_ATM_IV(self, stock: Stock):
        try:
            def is_spike_in_atm_iv(stock:Stock, atm_chain, expiry="current") -> bool:

                # Sort the dates
                sorted_dates = sorted(atm_chain.keys())
                if len(sorted_dates) < 2:
                    return False  # Not enough data to compare

                # Get previous and current
                prev_date = sorted_dates[-2]
                curr_date = sorted_dates[-1]
                prev_row = atm_chain[prev_date]
                curr_row = atm_chain[curr_date]

                IV_SPIKE = namedtuple("IV_SPIKE", ["expiry", "iv_change",])
                # Check if both rows exist and have volume
                if curr_row is not None and curr_row.get("volume", 0) > 0 and prev_row is not None and prev_row.get("volume", 0) > 0:
                    curr_iv = curr_row.get("iv", None)
                    prev_iv = prev_row.get("iv", None)
                    if curr_iv is not None and prev_iv is not None and prev_iv != 0:
                        iv_change = percentageChange(curr_iv, prev_iv)
                        if abs(iv_change) >= IVAnalyser.IV_PERCENTAGE_CHANGE:
                            stock.set_analysis("NEUTRAL", "IV_SPIKE", IV_SPIKE(
                                                expiry=expiry,
                                                iv_change=iv_change
                                            ))
                            logger.info(f"IV spike detected for {stock.stock_symbol} on {curr_date} for expiry {expiry}: IV change = {iv_change:.2f}%, volume = {curr_row['volume']}")
                            return True
                return False
                 
            logger.debug(f'Inside analyse_spike_in_ATM_IV for stock {stock.stock_symbol}')
            atm_chain_current = stock.zerodha_ctx["atm_data"]["current"]
            atm_chain_next = stock.zerodha_ctx["atm_data"]["next"]
            res = False
            if is_spike_in_atm_iv(stock, atm_chain_current, "current"):
                res = True
            if is_spike_in_atm_iv(stock, atm_chain_next, "next"):
                res = True
            if res:
                logger.debug(f"IV spike detected for {stock.stock_symbol}")

            return res
        except Exception as e:
            logger.error(f"Error in analyse_spike_in_ATM_IV for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_trend_in_ATM_IV(self, stock: Stock):
        try:
            def get_iv_trend(atm_chain, expiry="current"):
                sorted_dates = sorted(atm_chain.keys())
                n = IVAnalyser.IV_TREND_CONTINUATION_DAYS
                if len(sorted_dates) < n:
                    return None  # Not enough data to check trend

                ivs = []
                for date in sorted_dates[-n:]:
                    row = atm_chain[date]
                    if row is not None and row.get("iv", None) is not None:
                        ivs.append(row["iv"])
                    else:
                        return None  # Missing IV data

                # Check for upward trend
                if all(ivs[i] < ivs[i+1] for i in range(n-1)):
                    return "UPWARD"
                # Check for downward trend
                elif all(ivs[i] > ivs[i+1] for i in range(n-1)):
                    return "DOWNWARD"
                else:
                    return None

            logger.debug(f'Inside analyse_trend_in_ATM_IV for stock {stock.stock_symbol}')
            atm_chain_current = stock.zerodha_ctx["atm_data"]["current"]
            atm_chain_next = stock.zerodha_ctx["atm_data"]["next"]

            IV_TREND = namedtuple("IV_TREND", ["expiry", "trend"])
            res = False

            trend_current = get_iv_trend(atm_chain_current, "current")
            if trend_current:
                stock.set_analysis("NEUTRAL", "IV_TREND", IV_TREND(expiry="current", trend=trend_current))
                logger.info(f"IV {trend_current.lower()} trend detected for {stock.stock_symbol} (current expiry) for last 3 days")
                res = True

            trend_next = get_iv_trend(atm_chain_next, "next")
            if trend_next:
                stock.set_analysis("NEUTRAL", "IV_TREND", IV_TREND(expiry="next", trend=trend_next))
                logger.info(f"IV {trend_next.lower()} trend detected for {stock.stock_symbol} (next expiry) for last 3 days")
                res = True

            return res
        except Exception as e:
            logger.error(f"Error in analyse_trend_in_ATM_IV for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

