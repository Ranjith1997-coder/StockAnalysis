import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.helperFunctions import percentageChange
from common.logging_util import logger
from collections import namedtuple
import common.shared as shared

class FuturesAnalyser(BaseAnalyzer):
    FUTURE_OI_INCREASE_PERCENTAGE = 0
    FUTURE_PRICE_CHANGE_PERCENTAGE = 0

    def __init__(self) -> None:
        self.analyserName = "Futures Analyser"
        super().__init__()
    
    def reset_constants(self):
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE = 0.5
            FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE = 0.5
            FuturesAnalyser.ORB_CANDLES = 3
        else :
            FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE = 10
            FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE = 2
        logger.debug(f"FuturesAnalyser constants reset for mode {shared.app_ctx.mode.name}")
        logger.debug(f'FUTURE_OI_INCREASE_PERCENTAGE: {FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE}, FUTURE_PRICE_CHANGE_PERCENTAGE: {FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE}')

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_intraday_check_future_action(self, stock: Stock):
        try:
            logger.debug("Inside analyse_intraday_check_future_action method for stock {}".format(stock.stock_symbol))

            def get_future_action(futures_data, price_col="close", oi_col="oi", expiry='current'):
                
                """
                Determines futures action based on price and OI percentage change.
                Expects futures_data as a DataFrame with columns: price_col, oi_col.
                Returns a namedtuple with action, price_percentage, oi_percentage.
                """
                FutureActionAnalysis = namedtuple('FutureActionAnalysis', ['expiry', 'action', 'price_percentage', 'oi_percentage'])

                if len(futures_data) < 2:
                    logger.warning(f"Insufficient data for futures analysis for stock: {stock.stock_symbol} and expiry: {expiry}. Skipping action determination.")
                    return False

                prev_oi = futures_data.iloc[-2][oi_col]
                curr_oi = futures_data.iloc[-1][oi_col]
                prev_price = futures_data.iloc[-2][price_col]
                curr_price = futures_data.iloc[-1][price_col]

                price_percentage = percentageChange(curr_price, prev_price)
                oi_percentage = percentageChange(curr_oi, prev_oi)

                if price_percentage > FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE and \
                oi_percentage > FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE:
                    stock.set_analysis("BULLISH", "FUTURE_ACTION", FutureActionAnalysis(expiry,"long_buildup", price_percentage, oi_percentage))
                    return True
                elif price_percentage < (-1 * FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE) and \
                    oi_percentage > FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE:
                    stock.set_analysis("BEARISH", "FUTURE_ACTION", FutureActionAnalysis(expiry,"short_buildup", price_percentage, oi_percentage))
                    return True
                elif price_percentage > FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE and \
                    oi_percentage < (-1 * FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE):
                    stock.set_analysis("BULLISH", "FUTURE_ACTION",  FutureActionAnalysis(expiry, "short_covering", price_percentage, oi_percentage))
                    return True
                elif price_percentage < (-1 * FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE) and \
                    oi_percentage < (-1 * FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE):
                    stock.set_analysis("BEARISH", "FUTURE_ACTION",  FutureActionAnalysis(expiry,"long_unwinding", price_percentage, oi_percentage))
                    return True
                return False

            zerodha_ctx = stock.zerodha_ctx

            futures_data_curr = zerodha_ctx["futures_data"]["current"]
            futures_data_next = zerodha_ctx["futures_data"]["next"]
            res = False
            if get_future_action(futures_data_curr, expiry='current'):
                logger.info(f"Futures action detected for {stock.stock_symbol} for current expiry")
                res = True
            
            # if get_future_action(futures_data_next, expiry='next'):
            #     logger.info(f"Futures action detected for {stock.stock_symbol} for next expiry")
            #     res = True

            if res:
                logger.debug(f"Futures action detected for {stock.stock_symbol}")
            
            return res
            
        except Exception as e:
            logger.error(f"Error in analyse_intraday_check_future_action for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False    

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_intraday_price_volume_oi_pattern(self, stock: Stock):
        """
        Patterns:
          price_up_vol_oi_flat      -> price up while participation not building
          price_down_vol_oi_flat    -> price down while participation not building
          price_flat_vol_oi_incr    -> possible absorption / pre-breakout buildup
        Uses last two futures candles (current expiry).
        """
        try:
            zerodha_ctx = stock.zerodha_ctx
            fut_curr = zerodha_ctx["futures_data"]["current"]
            if fut_curr is None or fut_curr.empty or len(fut_curr) < 2:
                return False

            prev = fut_curr.iloc[-2]
            curr = fut_curr.iloc[-1]

            prev_close = prev.get("close"); curr_close = curr.get("close")
            prev_vol = prev.get("volume", 0); curr_vol = curr.get("volume", 0)
            prev_oi = prev.get("oi", 0); curr_oi = curr.get("oi", 0)
            if None in (prev_close, curr_close):
                return False

            price_pct = percentageChange(curr_close, prev_close)
            vol_pct = percentageChange(curr_vol, prev_vol) if prev_vol else 0
            oi_pct = percentageChange(curr_oi, prev_oi) if prev_oi else 0

            PRICE_THRESH = FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE 
            INCR_THRESH  = FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE 
            FLAT_PRICE_THRESH = max(PRICE_THRESH * 0.3, 0.15)          # tighter band for “flat price”
            FLAT_VOL_OI_THRESH = max(INCR_THRESH * 0.3, 0.2)          # flat def for vol & oi

            price_up   = price_pct > PRICE_THRESH
            price_down = price_pct < -PRICE_THRESH
            price_flat = abs(price_pct) <= FLAT_PRICE_THRESH

            vol_flat = abs(vol_pct) <= FLAT_VOL_OI_THRESH
            oi_flat  = abs(oi_pct) <= FLAT_VOL_OI_THRESH
            vol_incr = vol_pct > INCR_THRESH
            oi_incr  = oi_pct > INCR_THRESH
            vol_dec = vol_pct < -INCR_THRESH
            oi_dec  = oi_pct < -INCR_THRESH

            PatternTuple = namedtuple("FuturesPVOPattern",
                                      ["pattern", "price_pct", "vol_pct", "oi_pct", "expiry"])

            pattern = None
            sentiment = "NEUTRAL"

            if price_up and vol_flat and oi_flat:
                pattern = "price_up_vol_oi_flat"
                sentiment = "NEUTRAL"
            elif price_down and vol_flat and oi_flat:
                pattern = "price_down_vol_oi_flat"
                sentiment = "NEUTRAL"
            elif price_flat and vol_incr and oi_incr:
                pattern = "price_flat_vol_oi_incr"
                sentiment = "NEUTRAL"
            elif price_flat and vol_dec and oi_dec:
                pattern = "price_flat_vol_oi_dec"
                sentiment = "NEUTRAL"

            if not pattern:
                return False

            stock.set_analysis(sentiment,
                               "FUTURE_PVO_PATTERN",
                               PatternTuple(pattern, price_pct, vol_pct, oi_pct, "current"))
            logger.info(f"{pattern} detected for {stock.stock_symbol}: "
                        f"price {price_pct:.2f}%, vol {vol_pct:.2f}%, oi {oi_pct:.2f}%")
            return True

        except Exception as e:
            logger.error(f"Error in analyse_intraday_price_volume_oi_pattern for {stock.stock_symbol}: {e}")
            logger.error(traceback.format_exc())
            return False
        
    @BaseAnalyzer.index_intraday
    @BaseAnalyzer.intraday
    def analyse_intraday_breakout_oi_confirmation(self, stock: Stock):
        """
        Detect opening range breakout (ORB) with OI + Volume confirmation.
        Patterns:
          orb_breakout_up_oi_confirmed
          orb_breakout_down_oi_confirmed
          orb_breakout_up_no_confirm
          orb_breakout_down_no_confirm
        Conditions:
          - Need at least ORB_CANDLES + 2 candles.
          - ORB = first ORB_CANDLES highs/lows.
          - Breakout buffer applied to avoid marginal ticks.
          - OI confirmation: OI % change > OI_CONFIRM_PCT and Volume > VOL_FACTOR * rolling avg(vol, VOL_ROLL_N).
        """
        try:
            zerodha_ctx = stock.zerodha_ctx
            fut_curr = zerodha_ctx["futures_data"]["current"]
            if fut_curr is None or fut_curr.empty:
                return False

            # Ensure sorted by index (time)
            fut_curr = fut_curr.sort_index()

            # Intraday subset (assumes only today present; if not, filter by date)
            last_dt = fut_curr.index[-1]
            today_str = last_dt.strftime("%Y-%m-%d")
            fut_today = fut_curr[[d.strftime("%Y-%m-%d") == today_str for d in fut_curr.index]]

            ORB_CANDLES = FuturesAnalyser.ORB_CANDLES          # first 3 x 5m = 15m opening range
            BREAKOUT_BUFFER_PCT = 0.05             # 0.05% buffer above/below ORB
            OI_CONFIRM_PCT = max(FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE, 0.5)
            VOL_ROLL_N = 5
            VOL_FACTOR = 1.2

            if len(fut_today) < ORB_CANDLES + 2:
                return False

            orb_slice = fut_today.iloc[:ORB_CANDLES]
            orb_high = orb_slice['high'].max()
            orb_low = orb_slice['low'].min()

            last = fut_today.iloc[-1]
            prev = fut_today.iloc[-2]

            last_close = last['close']; prev_close = prev['close']
            last_oi = last.get('oi', 0); prev_oi = prev.get('oi', 0)
            last_vol = last.get('volume', 0)

            if any(v is None for v in [last_close, prev_close, last_oi, prev_oi]):
                return False

            # Percentage changes
            price_from_orb_high_pct = ((last_close - orb_high) / orb_high) * 100
            price_from_orb_low_pct = ((orb_low - last_close) / orb_low) * 100
            oi_pct = percentageChange(last_oi, prev_oi)

            # Rolling volume average (exclude last bar)
            vol_series = fut_today['volume']
            if len(vol_series) < VOL_ROLL_N + 1:
                vol_avg = vol_series.iloc[:-1].mean()
            else:
                vol_avg = vol_series.iloc[-(VOL_ROLL_N+1):-1].mean()
            vol_confirm = last_vol > VOL_FACTOR * vol_avg if vol_avg > 0 else False

            buffer_high_level = orb_high * (1 + BREAKOUT_BUFFER_PCT / 100)
            buffer_low_level = orb_low * (1 - BREAKOUT_BUFFER_PCT / 100)

            breakout_up = last_close > buffer_high_level
            breakout_down = last_close < buffer_low_level

            oi_confirm = oi_pct > OI_CONFIRM_PCT
            pattern = None
            sentiment = "NEUTRAL"

            if breakout_up:
                if oi_confirm and vol_confirm:
                    pattern = "orb_breakout_up_oi_confirmed"
                    sentiment = "BULLISH"
                # else:
                #     pattern = "orb_breakout_up_no_confirm"
            elif breakout_down:
                if oi_confirm and vol_confirm:
                    pattern = "orb_breakout_down_oi_confirmed"
                    sentiment = "BEARISH"
                # else:
                #     pattern = "orb_breakout_down_no_confirm"

            if not pattern:
                return False

            BreakoutTuple = namedtuple("FuturesBreakoutPattern", [
                "pattern",
                "orb_high",
                "orb_low",
                "last_close",
                "oi_pct",
                "vol",
                "vol_avg",
                "oi_confirm",
                "vol_confirm",
                "expiry"
            ])

            stock.set_analysis(
                sentiment,
                "FUTURE_BREAKOUT_PATTERN",
                BreakoutTuple(
                    pattern=pattern,
                    orb_high=orb_high,
                    orb_low=orb_low,
                    last_close=last_close,
                    oi_pct=oi_pct,
                    vol=last_vol,
                    vol_avg=vol_avg,
                    oi_confirm=oi_confirm,
                    vol_confirm=vol_confirm,
                    expiry="current"
                )
            )

            logger.info(
                f"{pattern} {stock.stock_symbol}: close={last_close:.2f} "
                f"orbH={orb_high:.2f} orbL={orb_low:.2f} "
                f"OI%={oi_pct:.2f} vol={last_vol} avgVol={vol_avg:.0f} "
                f"OIconf={oi_confirm} VOLconf={vol_confirm}"
            )
            return True

        except Exception as e:
            logger.error(f"Error in analyse_intraday_breakout_oi_confirmation for {stock.stock_symbol}: {e}")
            logger.error(traceback.format_exc())
            return False




        


        




