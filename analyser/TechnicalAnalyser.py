import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
from common.logging_util import logger
from collections import namedtuple
import pandas as pd
from datetime import datetime
from common.helperFunctions import percentageChange
import numpy as np
import common.shared as shared

class TechnicalAnalyser(BaseAnalyzer):

    RSI_UPPER_THRESHOLD = 80
    RSI_LOWER_THRESHOLD = 20
    RSI_LOOKUP_PERIOD = 14
    RSI_TREND_PERIODS = 3
    RSI_STRENGTH_THRESHOLD = 2
    RSI_MOMENTUM_THRESHOLD = 2

    VWAP_DEVIATION_PERCENTAGE = 2
    VWAP_DAYS = 10

    ATR_PERIOD = 14
    ATR_THRESHOLD = 3  # ATR multiplier for significance
    ATR_TREND_PERIODS = 3  # Number of periods to confirm trend

    BUY_SELL_QUANTITY = 2

    EMA_DIFF_THRESHOLD = 0      # minimum % separation after crossover
    EMA_MIN_SLOPE = 0    

    # Supertrend
    SUPERTREND_PERIOD = 10
    SUPERTREND_MULTIPLIER = 3

    # Stochastic Oscillator
    STOCHASTIC_K_PERIOD = 14
    STOCHASTIC_D_PERIOD = 3
    STOCHASTIC_UPPER = 80
    STOCHASTIC_LOWER = 20

    # RSI Divergence
    RSI_DIVERGENCE_LOOKBACK = 50
    RSI_DIVERGENCE_SWING_ORDER = 2

    # OBV (On-Balance Volume)
    OBV_EMA_PERIOD = 20

    
    def __init__(self) -> None:
        self.analyserName = "Technical Analyser"
        super().__init__()
    
    def reset_constants(self):
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            TechnicalAnalyser.FAST_EMA_PERIOD = 9
            TechnicalAnalyser.SLOW_EMA_PERIOD = 21
        else:
            TechnicalAnalyser.FAST_EMA_PERIOD = 50
            TechnicalAnalyser.SLOW_EMA_PERIOD = 200
        logger.debug(f"Technical Analyser constants reset for mode {shared.app_ctx.mode.name}")
        logger.debug(f"RSI_UPPER_THRESHOLD = {TechnicalAnalyser.RSI_UPPER_THRESHOLD} , RSI_LOWER_THRESHOLD = {TechnicalAnalyser.RSI_LOWER_THRESHOLD}, ATR_THRESHOLD = {TechnicalAnalyser.ATR_THRESHOLD}")

    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_rsi(self, stock: Stock):
        try : 
            def compute_rsi(close_series):
                if len(close_series) < TechnicalAnalyser.RSI_LOOKUP_PERIOD + 1:
                    raise ValueError(f"Need at least {TechnicalAnalyser.RSI_LOOKUP_PERIOD  + 1} data points to compute RSI")

                delta = close_series.diff().dropna()

                gains = delta.where(delta > 0, 0)
                losses = -delta.where(delta < 0, 0)

                # Use Wilder's smoothing method (EMA with adjust=False)
                avg_gain = gains.ewm(span=TechnicalAnalyser.RSI_LOOKUP_PERIOD, adjust=False).mean()
                avg_loss = losses.ewm(span=TechnicalAnalyser.RSI_LOOKUP_PERIOD, adjust=False).mean()

                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))

                return rsi
            logger.debug(f'Inside analyse_rsi for stock {stock.stock_symbol}')
            rsi_series = compute_rsi(stock.priceData["Close"].iloc[-100:])
            RSIAnalysis = namedtuple("RSIAnalysis", ["value", "previous_value", "trend",])

            current_rsi = rsi_series.iloc[-1]
            previous_rsi = rsi_series.iloc[-2]


            trend_found = False

            if current_rsi > TechnicalAnalyser.RSI_UPPER_THRESHOLD: 
                trend = "Overbought" if all(rsi > TechnicalAnalyser.RSI_UPPER_THRESHOLD for rsi in rsi_series.iloc[-self.RSI_TREND_PERIODS:]) else "Weakening"
                # strength = min(int((current_rsi - TechnicalAnalyser.RSI_UPPER_THRESHOLD) / self.RSI_STRENGTH_THRESHOLD), 5)
                # momentum = min(int((current_rsi - previous_rsi) / self.RSI_MOMENTUM_THRESHOLD), 5)
                
                # if trend == "Overbought" and strength >= 3 and momentum >= 2:
                if trend == "Overbought":
                    stock.set_analysis("BEARISH", "RSI", RSIAnalysis(
                        value=current_rsi,
                        previous_value=previous_rsi,
                        trend=trend
                    ))
                    trend_found = True
            elif current_rsi < TechnicalAnalyser.RSI_LOWER_THRESHOLD:
                trend = "Oversold" if all(rsi < TechnicalAnalyser.RSI_LOWER_THRESHOLD for rsi in rsi_series.iloc[-self.RSI_TREND_PERIODS:]) else "Strengthening"
                # strength = min(int((TechnicalAnalyser.RSI_LOWER_THRESHOLD - current_rsi) / self.RSI_STRENGTH_THRESHOLD), 5)
                # momentum = min(int((previous_rsi - current_rsi) / self.RSI_MOMENTUM_THRESHOLD), 5)
                
                # if trend == "Oversold" and strength >= 3 and momentum >= 2:
                if trend == "Oversold":
                    stock.set_analysis("BULLISH", "RSI", RSIAnalysis(
                        value=current_rsi,
                        previous_value=previous_rsi,
                        trend=trend
                    ))
                    trend_found = True
            
            RSICrossoverAnalysis = namedtuple("RSICrossoverAnalysis", ["curr_value", "prev_value"])
            if previous_rsi > TechnicalAnalyser.RSI_UPPER_THRESHOLD and current_rsi < TechnicalAnalyser.RSI_UPPER_THRESHOLD: 
                stock.set_analysis("BEARISH", "rsi_crossover", RSICrossoverAnalysis(curr_value=current_rsi, prev_value=previous_rsi))
                trend_found = True
            elif previous_rsi < TechnicalAnalyser.RSI_LOWER_THRESHOLD and current_rsi > TechnicalAnalyser.RSI_LOWER_THRESHOLD: 
                stock.set_analysis("BULLISH", "rsi_crossover", RSICrossoverAnalysis(curr_value=current_rsi, prev_value=previous_rsi))
                trend_found = True

            return trend_found
        except Exception as e:
            logger.error(f"Error in analyse_rsi for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_Bolinger_band(self, stock: Stock):
        try : 
            def compute_latest_bollinger_bands(series, window=20, num_std=2):
                """
                Compute latest Bollinger Bands values using only the last 'window' points.
                Returns: (sma, upper_band, lower_band)
                """
                if len(series) < window:
                    raise ValueError("Not enough data to compute Bollinger Bands.")

                recent = series[-window:]
                sma = recent.mean()
                std = recent.std()

                upper = sma + num_std * std
                lower = sma - num_std * std

                return sma, upper, lower
            
            logger.debug(f'Inside analyse_Bolinger_band for stock {stock.stock_symbol}')
            sma , upper_band, lower_band = compute_latest_bollinger_bands(stock.priceData['Close'])
            curr_data = stock.current_equity_data
            BBAnalysis = namedtuple("BBAnalysis", ["close", "upper_band", "lower_band"])
            if curr_data['Close'] > upper_band: 
                stock.set_analysis("BEARISH", "BollingerBand", BBAnalysis(close=curr_data['Close'], upper_band=upper_band, lower_band=lower_band))
                return True
            elif curr_data['Close'] < lower_band:
                stock.set_analysis("BULLISH", "BollingerBand", BBAnalysis(close=curr_data['Close'], upper_band=upper_band, lower_band=lower_band))
                return True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_Bolinger_band for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.positional
    @BaseAnalyzer.index_positional
    def analyse_is_52_week(self, stock: Stock):
        try : 
            logger.debug(f'Inside analyse_is_52_week for stock {stock.stock_symbol}')
            status = stock.check_52_week_status()
            if status == 1:
                stock.set_analysis("NEUTRAL", "52-week-high", True)
            elif status == -1:
                stock.set_analysis("NEUTRAL", "52-week-low", True)
            return False
        except Exception as e:
            logger.error(f"Error in analyse_is_52_week for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.intraday
    def analyse_vwap(self, stock: Stock):
        try : 
            def calculate_vwap(price_data: pd.DataFrame) -> pd.Series:
                """
                Calculate the Volume Weighted Average Price (VWAP) for a given price data.

                Args:
                    price_data (pd.DataFrame): DataFrame containing 'High', 'Low', 'Close', and 'Volume' columns.

                Returns:
                    pd.Series: A Series representing the VWAP for each row in the DataFrame.
                """
                # Calculate the typical price
                typical_price = (price_data['High'] + price_data['Low'] + price_data['Close']) / 3
                # Calculate the VWAP
                vwap = (typical_price * price_data['Volume']).cumsum() / price_data['Volume'].cumsum()
                return vwap
            logger.debug(f'Inside analyse_vwap for stock {stock.stock_symbol}')
            import pytz
            from datetime import datetime, timedelta
            ist = pytz.timezone('Asia/Kolkata')
            # yesterday = (datetime.now(ist) - timedelta(days=1)).date()
            # today_data = stock.priceData[stock.priceData.index.date == yesterday]
            today = datetime.now().date()
            today_data = stock.priceData[stock.priceData.index.date == today]
            vwap = calculate_vwap(today_data)

            latest_close = stock.priceData['Close'].iloc[-1]
            latest_vwap = vwap.iloc[-1]

            deviation = percentageChange(latest_close, latest_vwap)
            VwapAnalysis = namedtuple("VWAPAnalysis", ["close", "vwap", "vwap_days", "deviation"])
            
            if deviation > TechnicalAnalyser.VWAP_DEVIATION_PERCENTAGE:
                above_vwap_days = 1
                for i in range(len(today_data) - 2, -1, -1):  # Start from the second last day
                    if today_data['Close'].iloc[i] > vwap.iloc[i]:
                        above_vwap_days += 1
                    else:
                        break
                if above_vwap_days > TechnicalAnalyser.VWAP_DAYS:
                    stock.set_analysis("BEARISH", "vwap_deviation", VwapAnalysis(close=latest_close, vwap=latest_vwap, vwap_days=above_vwap_days, deviation=deviation))
                    return True
            elif deviation < (-1 * TechnicalAnalyser.VWAP_DEVIATION_PERCENTAGE):
                below_vwap_days = 1
                for i in range(len(today_data) - 2, -1, -1):  # Start from the second last day
                    if today_data['Close'].iloc[i] < vwap.iloc[i]:
                        below_vwap_days += 1
                    else:
                        break
                if below_vwap_days > TechnicalAnalyser.VWAP_DAYS:
                    stock.set_analysis("BULLISH", "vwap_deviation", VwapAnalysis(close=latest_close , vwap=latest_vwap, vwap_days=below_vwap_days, deviation=deviation))
                    return True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_vwap for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyze_macd(self, stock: Stock):
        def calculate_latest_macd(data, fast_period=12, slow_period=26, signal_period=9):
            """
            Calculate MACD, Signal line, and Histogram for the entire dataset.
    
            :param data: pandas DataFrame with a 'Close' column
            :param fast_period: The short-term EMA period (default: 12)
            :param slow_period: The long-term EMA period (default: 26)
            :param signal_period: The signal line EMA period (default: 9)
            :return: DataFrame with MACD, Signal, and Histogram columns
            """
            close = data['Close']
            
            # Calculate EMAs
            ema_fast = close.ewm(span=fast_period, adjust=False).mean()
            ema_slow = close.ewm(span=slow_period, adjust=False).mean()
            
            # Calculate MACD line
            macd_line = ema_fast - ema_slow
            
            # Calculate Signal line
            signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
            
            # Calculate MACD histogram
            macd_histogram = macd_line - signal_line
            
            return pd.DataFrame({
                'MACD': macd_line,
                'Signal': signal_line,
                'Histogram': macd_histogram
            })
        try:
            logger.debug(f'Analyzing MACD for stock {stock.stock_symbol}')
            # Get the stock's price data
            price_data = stock.priceData
            
            # Calculate latest MACD values
            macd_data = calculate_latest_macd(price_data)
            
            latest_macd = macd_data.iloc[-1]
            previous_macd = macd_data.iloc[-2]
            
            # 1. Minimum histogram value change
            MIN_HISTOGRAM_CHANGE = 0.1  # Adjust this value as needed
            histogram_change = abs(latest_macd['Histogram'] - previous_macd['Histogram'])

            # 2. Check for consecutive histogram changes
            CONSECUTIVE_CHANGES = 3
            histogram_values = macd_data['Histogram'].iloc[-CONSECUTIVE_CHANGES:]

            # 5. Trend strength
            TREND_STRENGTH_THRESHOLD = 0.02  # 2% change

            # Check for bullish crossover
            if (latest_macd['MACD'] > latest_macd['Signal'] and 
                previous_macd['MACD'] <= previous_macd['Signal'] and
                histogram_change > MIN_HISTOGRAM_CHANGE and
                histogram_values.is_monotonic_increasing and
                (latest_macd['MACD'] - latest_macd['Signal']) / latest_macd['Signal'] > TREND_STRENGTH_THRESHOLD):
                stock.set_analysis("BULLISH", "MACD", "Strong Bullish crossover")
                return True

            # Check for bearish crossover
            elif (latest_macd['MACD'] < latest_macd['Signal'] and 
                previous_macd['MACD'] >= previous_macd['Signal'] and
                histogram_change > MIN_HISTOGRAM_CHANGE and
                 histogram_values.is_monotonic_decreasing and
                (latest_macd['Signal'] - latest_macd['MACD']) / latest_macd['Signal'] > TREND_STRENGTH_THRESHOLD):
                
                stock.set_analysis("BEARISH", "MACD", "Strong Bearish crossover")
                return True

            return False
        except Exception as e:
            logger.error(f"Error in analyze_macd for stock {stock.stock_symbol}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
   

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_atr(self, stock: Stock):
        try:
            def calculate_atr(high, low, close, period=self.ATR_PERIOD):
                high_low = high - low
                high_close = np.abs(high - close.shift())
                low_close = np.abs(low - close.shift())
                ranges = pd.concat([high_low, high_close, low_close], axis=1)
                true_range = np.max(ranges, axis=1)
                return true_range.rolling(period).mean()
            logger.debug(f'Inside analyse_atr for stock {stock.stock_symbol}')
            # Calculate ATR
            atr = calculate_atr(stock.priceData['High'], stock.priceData['Low'], stock.priceData['Close'])
            
            # Get the latest price data
            latest_close = stock.priceData['Close'].iloc[-1]
            latest_atr = atr.iloc[-1]
            
            # Calculate the ATR percentage
            atr_percentage = (latest_atr / latest_close) * 100
            
            # Check for high volatility
            is_high_volatility = atr_percentage > self.ATR_THRESHOLD
            
            # Check for price breakout
            # upper_band = stock.priceData['Close'].rolling(self.ATR_TREND_PERIODS).mean() + (self.ATR_THRESHOLD * atr)
            # lower_band = stock.priceData['Close'].rolling(self.ATR_TREND_PERIODS).mean() - (self.ATR_THRESHOLD * atr)
            
            # is_upward_breakout = all(stock.priceData['Close'].iloc[-self.ATR_TREND_PERIODS:] > upper_band.iloc[-self.ATR_TREND_PERIODS:])
            # is_downward_breakout = all(stock.priceData['Close'].iloc[-self.ATR_TREND_PERIODS:] < lower_band.iloc[-self.ATR_TREND_PERIODS:])
            
            # Check for ATR expansion
            is_atr_expanding = all(atr.diff().iloc[-self.ATR_TREND_PERIODS:] > 0)
            
            ATRAnalysis = namedtuple("ATRAnalysis", ["atr_value", "atr_percentage", "volatility", "breakout"])

            if is_high_volatility  and is_atr_expanding:
                stock.set_analysis("NEUTRAL", "ATR", ATRAnalysis(
                    atr_value=latest_atr,
                    atr_percentage=atr_percentage,
                    volatility="High",
                    breakout="Upward"
                ))
                return True
            
            # if is_high_volatility  and is_atr_expanding:
            #     stock.set_analysis("BULLISH", "ATR", ATRAnalysis(
            #         atr_value=latest_atr,
            #         atr_percentage=atr_percentage,
            #         volatility="High",
            #         breakout="Upward"
            #     ))
            #     return True
            # elif is_high_volatility  and is_atr_expanding:
            #     stock.set_analysis("BEARISH", "ATR", ATRAnalysis(
            #         atr_value=latest_atr,
            #         atr_percentage=atr_percentage,
            #         volatility="High",
            #         breakout="Downward"
            #     ))
            #     return True
            
            return False
        except Exception as e:
            logger.error(f"Error in analyse_atr for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.intraday
    def analyze_buy_sell_quantity(self, stock:Stock):
        try:
            zerodha_data = stock.zerodha_data

            buy_quantity = zerodha_data.get("total_buy_quantity", 0)
            sell_quantity = zerodha_data.get("total_sell_quantity", 0)
            buySellAnalysis = namedtuple("buySellAnalysis", ["buy_quantity", "sell_quantity"])
            logger.debug(f"stock : {stock.stock_symbol}, buy_quantity: {buy_quantity}, sell_quantity: {sell_quantity}")

            if buy_quantity > self.BUY_SELL_QUANTITY * sell_quantity:
                stock.set_analysis("BULLISH", "BUY_SELL", buySellAnalysis(buy_quantity=buy_quantity, sell_quantity=sell_quantity))
                return True
            elif sell_quantity > self.BUY_SELL_QUANTITY * buy_quantity:
                stock.set_analysis("BEARISH", "BUY_SELL", buySellAnalysis(buy_quantity=buy_quantity, sell_quantity=sell_quantity))
                return True
            return False
        except Exception as e:
            logger.error(f"Error in analyze_buy_sell_quantity tick for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_ema_crossover(self, stock: Stock):
        """
        EMA crossover (works in intraday & positional):
          Bullish: FAST crosses above SLOW with positive slope + % diff threshold.
          Bearish: FAST crosses below SLOW with negative slope + % diff threshold.
        Stores result under key: EMA_CROSSOVER
        """
        try:
            logger.debug(f'Inside analyse_ema_crossover for stock {stock.stock_symbol}')
            close = stock.priceData['Close']
            if len(close) < max(TechnicalAnalyser.FAST_EMA_PERIOD, TechnicalAnalyser.SLOW_EMA_PERIOD) + 3:
                return False

            def tv_ema(series: pd.Series, length: int) -> pd.Series:
                if len(series) < length:
                    return series * float('nan')
                sma = series.iloc[:length].mean()
                alpha = 2 / (length + 1)
                out = [None] * (length - 1)
                prev = sma
                out.append(sma)
                for price in series.iloc[length:]:
                    prev = (price - prev) * alpha + prev
                    out.append(prev)
                return pd.Series(out, index=series.index)

            fast_ema = tv_ema(close, TechnicalAnalyser.FAST_EMA_PERIOD)
            slow_ema = tv_ema(close, TechnicalAnalyser.SLOW_EMA_PERIOD)
            # fast_ema = close.ewm(span=TechnicalAnalyser.FAST_EMA_PERIOD, adjust=False).mean()
            # slow_ema = close.ewm(span=TechnicalAnalyser.SLOW_EMA_PERIOD, adjust=False).mean()

            fast_now = fast_ema.iloc[-1]
            slow_now = slow_ema.iloc[-1]
            fast_prev = fast_ema.iloc[-2]
            slow_prev = slow_ema.iloc[-2]
            fast_prev2 = fast_ema.iloc[-3]
            slow_prev2 = slow_ema.iloc[-3]

            if any(pd.isna(x) for x in [fast_now, slow_now, fast_prev, slow_prev]):
                return False

            diff_pct = ((fast_now - slow_now) / slow_now) * 100 if slow_now else 0
            fast_slope = fast_now - fast_prev
            slow_slope = slow_now - slow_prev
            fast_slope_prev = fast_prev - fast_prev2
            slow_slope_prev = slow_prev - slow_prev2

            EMACross = namedtuple("EMACross", [
                "fast_ema", "slow_ema", "diff_pct",
                "fast_slope", "slow_slope",
                "fast_slope_prev", "slow_slope_prev",
                "direction"
            ])

            signal = None
            # Bullish
            if (fast_prev <= slow_prev and fast_now > slow_now and
                diff_pct > self.EMA_DIFF_THRESHOLD and
                fast_slope > self.EMA_MIN_SLOPE and slow_slope >= 0):
                signal = ("BULLISH", "bullish_crossover")
            # Bearish
            elif (fast_prev >= slow_prev and fast_now < slow_now and
                  diff_pct < -self.EMA_DIFF_THRESHOLD and
                  fast_slope < -self.EMA_MIN_SLOPE and slow_slope <= 0):
                signal = ("BEARISH", "bearish_crossover")

            if not signal:
                return False

            sentiment, direction = signal
            stock.set_analysis(
                sentiment,
                "EMA_CROSSOVER",
                EMACross(
                    fast_ema=fast_now,
                    slow_ema=slow_now,
                    diff_pct=diff_pct,
                    fast_slope=fast_slope,
                    slow_slope=slow_slope,
                    fast_slope_prev=fast_slope_prev,
                    slow_slope_prev=slow_slope_prev,
                    direction=direction
                )
            )
            return True

        except Exception as e:
            logger.error(f"Error in analyse_ema_crossover for stock {stock.stock_symbol}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_supertrend(self, stock: Stock):
        """
        Supertrend indicator using ATR-based trailing stop that flips between
        support and resistance.  Signal fires on direction change (reversal).
        Bullish: direction flips from DOWN to UP  (buy reversal)
        Bearish: direction flips from UP to DOWN  (sell reversal)
        """
        try:
            logger.debug(f'Inside analyse_supertrend for stock {stock.stock_symbol}')
            price_data = stock.priceData
            n = len(price_data)
            period = self.SUPERTREND_PERIOD
            multiplier = self.SUPERTREND_MULTIPLIER

            if n < period + 2:
                return False

            high = price_data['High'].values
            low = price_data['Low'].values
            close = price_data['Close'].values

            # True Range
            prev_close = np.roll(close, 1)
            prev_close[0] = close[0]
            tr = np.maximum(high - low,
                            np.maximum(np.abs(high - prev_close),
                                       np.abs(low - prev_close)))

            # ATR using Wilder's smoothing (RMA)
            atr = np.zeros(n)
            atr[period - 1] = np.mean(tr[:period])
            for i in range(period, n):
                atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

            hl2 = (high + low) / 2.0
            upper_basic = hl2 + multiplier * atr
            lower_basic = hl2 - multiplier * atr

            upper_band = np.zeros(n)
            lower_band = np.zeros(n)
            supertrend = np.zeros(n)
            direction = np.zeros(n, dtype=int)  # 1 = bullish, -1 = bearish

            # Initialise at first valid ATR bar
            upper_band[period - 1] = upper_basic[period - 1]
            lower_band[period - 1] = lower_basic[period - 1]
            supertrend[period - 1] = upper_basic[period - 1]
            direction[period - 1] = -1

            for i in range(period, n):
                # Adjust upper band
                if upper_basic[i] < upper_band[i - 1] or close[i - 1] > upper_band[i - 1]:
                    upper_band[i] = upper_basic[i]
                else:
                    upper_band[i] = upper_band[i - 1]

                # Adjust lower band
                if lower_basic[i] > lower_band[i - 1] or close[i - 1] < lower_band[i - 1]:
                    lower_band[i] = lower_basic[i]
                else:
                    lower_band[i] = lower_band[i - 1]

                # Direction and supertrend value
                if direction[i - 1] == -1:          # was bearish
                    if close[i] > upper_band[i]:
                        direction[i] = 1            # flip bullish
                        supertrend[i] = lower_band[i]
                    else:
                        direction[i] = -1
                        supertrend[i] = upper_band[i]
                else:                               # was bullish
                    if close[i] < lower_band[i]:
                        direction[i] = -1           # flip bearish
                        supertrend[i] = upper_band[i]
                    else:
                        direction[i] = 1
                        supertrend[i] = lower_band[i]

            curr_dir = direction[-1]
            prev_dir = direction[-2]
            curr_st = supertrend[-1]
            curr_close = close[-1]

            SupertrendAnalysis = namedtuple("SupertrendAnalysis", [
                "close", "supertrend_value", "direction", "signal"
            ])

            if prev_dir == -1 and curr_dir == 1:
                stock.set_analysis("BULLISH", "SUPERTREND", SupertrendAnalysis(
                    close=curr_close, supertrend_value=curr_st,
                    direction="UP", signal="buy_reversal"
                ))
                return True
            elif prev_dir == 1 and curr_dir == -1:
                stock.set_analysis("BEARISH", "SUPERTREND", SupertrendAnalysis(
                    close=curr_close, supertrend_value=curr_st,
                    direction="DOWN", signal="sell_reversal"
                ))
                return True

            return False
        except Exception as e:
            logger.error(f"Error in analyse_supertrend for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_rsi_divergence(self, stock: Stock):
        """
        Detects RSI divergence — a high-conviction reversal signal.
        Bearish divergence: price makes a higher high but RSI makes a lower high.
        Bullish divergence: price makes a lower low  but RSI makes a higher low.
        Swing points are identified in the price series and RSI is read at those bars.
        """
        try:
            logger.debug(f'Inside analyse_rsi_divergence for stock {stock.stock_symbol}')
            close = stock.priceData['Close']
            lookback = self.RSI_DIVERGENCE_LOOKBACK
            order = self.RSI_DIVERGENCE_SWING_ORDER

            if len(close) < lookback + self.RSI_LOOKUP_PERIOD + 1:
                return False

            # Compute RSI over the full series
            delta = close.diff().dropna()
            gains = delta.where(delta > 0, 0)
            losses = -delta.where(delta < 0, 0)
            avg_gain = gains.ewm(span=self.RSI_LOOKUP_PERIOD, adjust=False).mean()
            avg_loss = losses.ewm(span=self.RSI_LOOKUP_PERIOD, adjust=False).mean()
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

            # Work on the recent window (reset index for simple integer indexing)
            recent_close = close.iloc[-lookback:].reset_index(drop=True)
            recent_rsi = rsi.iloc[-lookback:].reset_index(drop=True)

            def find_swing_highs(series, swing_order):
                """Indices where value is strictly greater than `swing_order` neighbours each side."""
                highs = []
                for i in range(swing_order, len(series) - swing_order):
                    if (all(series.iloc[i] > series.iloc[i - j] for j in range(1, swing_order + 1)) and
                            all(series.iloc[i] > series.iloc[i + j] for j in range(1, swing_order + 1))):
                        highs.append(i)
                return highs

            def find_swing_lows(series, swing_order):
                """Indices where value is strictly less than `swing_order` neighbours each side."""
                lows = []
                for i in range(swing_order, len(series) - swing_order):
                    if (all(series.iloc[i] < series.iloc[i - j] for j in range(1, swing_order + 1)) and
                            all(series.iloc[i] < series.iloc[i + j] for j in range(1, swing_order + 1))):
                        lows.append(i)
                return lows

            RSIDivergenceAnalysis = namedtuple("RSIDivergenceAnalysis", [
                "divergence_type", "price_current", "price_previous",
                "rsi_current", "rsi_previous"
            ])

            # Bearish divergence: price higher-high, RSI lower-high
            price_highs = find_swing_highs(recent_close, order)
            if len(price_highs) >= 2:
                h1, h2 = price_highs[-2], price_highs[-1]
                if (recent_close.iloc[h2] > recent_close.iloc[h1] and
                        recent_rsi.iloc[h2] < recent_rsi.iloc[h1]):
                    stock.set_analysis("BEARISH", "RSI_DIVERGENCE", RSIDivergenceAnalysis(
                        divergence_type="bearish",
                        price_current=recent_close.iloc[h2],
                        price_previous=recent_close.iloc[h1],
                        rsi_current=recent_rsi.iloc[h2],
                        rsi_previous=recent_rsi.iloc[h1]
                    ))
                    return True

            # Bullish divergence: price lower-low, RSI higher-low
            price_lows = find_swing_lows(recent_close, order)
            if len(price_lows) >= 2:
                l1, l2 = price_lows[-2], price_lows[-1]
                if (recent_close.iloc[l2] < recent_close.iloc[l1] and
                        recent_rsi.iloc[l2] > recent_rsi.iloc[l1]):
                    stock.set_analysis("BULLISH", "RSI_DIVERGENCE", RSIDivergenceAnalysis(
                        divergence_type="bullish",
                        price_current=recent_close.iloc[l2],
                        price_previous=recent_close.iloc[l1],
                        rsi_current=recent_rsi.iloc[l2],
                        rsi_previous=recent_rsi.iloc[l1]
                    ))
                    return True

            return False
        except Exception as e:
            logger.error(f"Error in analyse_rsi_divergence for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_stochastic(self, stock: Stock):
        """
        Stochastic Oscillator (%K / %D).
        Bullish: %K crosses above %D while in oversold zone (<=20).
        Bearish: %K crosses below %D while in overbought zone (>=80).
        """
        try:
            logger.debug(f'Inside analyse_stochastic for stock {stock.stock_symbol}')
            price_data = stock.priceData
            k_period = self.STOCHASTIC_K_PERIOD
            d_period = self.STOCHASTIC_D_PERIOD

            if len(price_data) < k_period + d_period + 1:
                return False

            high = price_data['High']
            low = price_data['Low']
            close = price_data['Close']

            # %K = (Close - Lowest Low_n) / (Highest High_n - Lowest Low_n) * 100
            lowest_low = low.rolling(window=k_period).min()
            highest_high = high.rolling(window=k_period).max()
            denom = highest_high - lowest_low
            denom = denom.replace(0, np.nan)
            k_line = ((close - lowest_low) / denom) * 100

            # %D = SMA of %K
            d_line = k_line.rolling(window=d_period).mean()

            curr_k = k_line.iloc[-1]
            curr_d = d_line.iloc[-1]
            prev_k = k_line.iloc[-2]
            prev_d = d_line.iloc[-2]

            if any(pd.isna(x) for x in [curr_k, curr_d, prev_k, prev_d]):
                return False

            StochasticAnalysis = namedtuple("StochasticAnalysis", [
                "k_value", "d_value", "prev_k", "prev_d", "signal"
            ])

            # Bullish crossover in oversold zone
            if prev_k <= prev_d and curr_k > curr_d and prev_k <= self.STOCHASTIC_LOWER:
                stock.set_analysis("BULLISH", "STOCHASTIC", StochasticAnalysis(
                    k_value=curr_k, d_value=curr_d,
                    prev_k=prev_k, prev_d=prev_d,
                    signal="bullish_crossover_oversold"
                ))
                return True

            # Bearish crossover in overbought zone
            elif prev_k >= prev_d and curr_k < curr_d and prev_k >= self.STOCHASTIC_UPPER:
                stock.set_analysis("BEARISH", "STOCHASTIC", StochasticAnalysis(
                    k_value=curr_k, d_value=curr_d,
                    prev_k=prev_k, prev_d=prev_d,
                    signal="bearish_crossover_overbought"
                ))
                return True

            return False
        except Exception as e:
            logger.error(f"Error in analyse_stochastic for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_obv(self, stock: Stock):
        """
        On-Balance Volume (OBV) divergence detector.
        Compares price trend vs OBV trend over the lookback window.
        Bearish divergence: price rising but OBV falling  (distribution).
        Bullish divergence: price falling but OBV rising  (accumulation).
        """
        try:
            logger.debug(f'Inside analyse_obv for stock {stock.stock_symbol}')
            price_data = stock.priceData
            lookback = self.OBV_EMA_PERIOD

            if len(price_data) < lookback + 5:
                return False

            close = price_data['Close']
            volume = price_data['Volume']

            # Vectorised OBV calculation
            price_change = close.diff()
            signed_volume = np.where(price_change > 0, volume,
                                     np.where(price_change < 0, -volume, 0))
            obv = pd.Series(signed_volume, index=close.index, dtype=float).cumsum()

            # OBV EMA for trend smoothing
            obv_ema = obv.ewm(span=lookback, adjust=False).mean()

            # Split recent window into two halves and compare means
            recent_close = close.iloc[-lookback:]
            recent_obv = obv.iloc[-lookback:]
            half = lookback // 2

            price_first_half = recent_close.iloc[:half].mean()
            price_second_half = recent_close.iloc[half:].mean()
            obv_first_half = recent_obv.iloc[:half].mean()
            obv_second_half = recent_obv.iloc[half:].mean()

            # Require minimum 0.5 % price movement to filter noise
            price_change_pct = ((price_second_half - price_first_half) / price_first_half) * 100
            price_rising = price_change_pct > 0.5
            price_falling = price_change_pct < -0.5
            obv_rising = obv_second_half > obv_first_half
            obv_falling = obv_second_half < obv_first_half

            curr_obv = obv.iloc[-1]
            curr_obv_ema = obv_ema.iloc[-1]

            OBVAnalysis = namedtuple("OBVAnalysis", [
                "obv_current", "obv_ema", "divergence_type",
                "price_trend", "obv_trend"
            ])

            # Bearish divergence: price rising but OBV falling (distribution)
            if price_rising and obv_falling:
                stock.set_analysis("BEARISH", "OBV", OBVAnalysis(
                    obv_current=curr_obv, obv_ema=curr_obv_ema,
                    divergence_type="bearish_divergence",
                    price_trend="rising", obv_trend="falling"
                ))
                return True

            # Bullish divergence: price falling but OBV rising (accumulation)
            elif price_falling and obv_rising:
                stock.set_analysis("BULLISH", "OBV", OBVAnalysis(
                    obv_current=curr_obv, obv_ema=curr_obv_ema,
                    divergence_type="bullish_divergence",
                    price_trend="falling", obv_trend="rising"
                ))
                return True

            return False
        except Exception as e:
            logger.error(f"Error in analyse_obv for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_pivot_points(self, stock: Stock):
        """
        Classic Pivot Points (PP, R1-R3, S1-S3) from previous period OHLC.
        Signals fire when the current close crosses through a pivot level
        compared to the previous close:
          Bullish: price breaks UP through PP / R1 / R2  (resistance breakout)
          Bearish: price breaks DOWN through PP / S1 / S2  (support breakdown)
        """
        try:
            logger.debug(f'Inside analyse_pivot_points for stock {stock.stock_symbol}')

            # Previous period OHLC — use prevDayOHLCV for intraday, previous bar for positional
            if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
                if stock.prevDayOHLCV is None:
                    return False
                prev_high = stock.prevDayOHLCV['HIGH']
                prev_low = stock.prevDayOHLCV['LOW']
                prev_close = stock.prevDayOHLCV['CLOSE']
            else:
                if len(stock.priceData) < 3:
                    return False
                prev_data = stock.previous_equity_data
                prev_high = prev_data['High']
                prev_low = prev_data['Low']
                prev_close = prev_data['Close']

            # Classic Pivot Point formulae
            pp = (prev_high + prev_low + prev_close) / 3
            r1 = 2 * pp - prev_low
            s1 = 2 * pp - prev_high
            r2 = pp + (prev_high - prev_low)
            s2 = pp - (prev_high - prev_low)

            curr_close = stock.current_equity_data['Close']
            prev_close_val = stock.previous_equity_data['Close']

            PivotAnalysis = namedtuple("PivotAnalysis", [
                "close", "pivot", "level_name", "level_value", "signal"
            ])

            # --- Bullish breakouts (price crossing UP through a level) ---
            if prev_close_val < r2 and curr_close >= r2:
                stock.set_analysis("BULLISH", "PIVOT_POINTS", PivotAnalysis(
                    close=curr_close, pivot=pp, level_name="R2",
                    level_value=r2, signal="R2_breakout"
                ))
                return True
            elif prev_close_val < r1 and curr_close >= r1:
                stock.set_analysis("BULLISH", "PIVOT_POINTS", PivotAnalysis(
                    close=curr_close, pivot=pp, level_name="R1",
                    level_value=r1, signal="R1_breakout"
                ))
                return True
            elif prev_close_val < pp and curr_close >= pp:
                stock.set_analysis("BULLISH", "PIVOT_POINTS", PivotAnalysis(
                    close=curr_close, pivot=pp, level_name="PP",
                    level_value=pp, signal="PP_breakout"
                ))
                return True

            # --- Bearish breakdowns (price crossing DOWN through a level) ---
            if prev_close_val > s2 and curr_close <= s2:
                stock.set_analysis("BEARISH", "PIVOT_POINTS", PivotAnalysis(
                    close=curr_close, pivot=pp, level_name="S2",
                    level_value=s2, signal="S2_breakdown"
                ))
                return True
            elif prev_close_val > s1 and curr_close <= s1:
                stock.set_analysis("BEARISH", "PIVOT_POINTS", PivotAnalysis(
                    close=curr_close, pivot=pp, level_name="S1",
                    level_value=s1, signal="S1_breakdown"
                ))
                return True
            elif prev_close_val > pp and curr_close <= pp:
                stock.set_analysis("BEARISH", "PIVOT_POINTS", PivotAnalysis(
                    close=curr_close, pivot=pp, level_name="PP",
                    level_value=pp, signal="PP_breakdown"
                ))
                return True

            return False
        except Exception as e:
            logger.error(f"Error in analyse_pivot_points for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False