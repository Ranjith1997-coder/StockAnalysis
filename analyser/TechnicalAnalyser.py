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

    # Optimised for positional (profit_factor=1.20, 18 stocks, 2020-2024 train)
    RSI_UPPER_THRESHOLD = 85
    RSI_LOWER_THRESHOLD = 30
    RSI_LOOKUP_PERIOD = 14
    RSI_TREND_PERIODS = 5
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

    # Supertrend — optimised for positional (profit_factor=1.24, 18 stocks, 2020-2024 train)
    SUPERTREND_PERIOD = 14
    SUPERTREND_MULTIPLIER = 2.5

    # Stochastic Oscillator — optimised for positional (profit_factor=1.41 test, 18 stocks, 2020-2024 train)
    STOCHASTIC_K_PERIOD = 5
    STOCHASTIC_D_PERIOD = 5
    STOCHASTIC_UPPER = 90
    STOCHASTIC_LOWER = 30

    # RSI Divergence
    RSI_DIVERGENCE_LOOKBACK = 50
    RSI_DIVERGENCE_SWING_ORDER = 2

    # OBV (On-Balance Volume)
    OBV_EMA_PERIOD = 20

    # Bollinger Bands
    BB_WINDOW = 20
    BB_NUM_STD = 2.0

    # MACD
    MACD_FAST_PERIOD = 12
    MACD_SLOW_PERIOD = 26
    MACD_SIGNAL_PERIOD = 9

    
    def __init__(self) -> None:
        self.analyserName = "Technical Analyser"
        super().__init__()
    
    def reset_constants(self):
        if shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            TechnicalAnalyser.FAST_EMA_PERIOD = 9
            TechnicalAnalyser.SLOW_EMA_PERIOD = 21
            # Intraday RSI — original defaults (not optimised yet)
            TechnicalAnalyser.RSI_UPPER_THRESHOLD = 80
            TechnicalAnalyser.RSI_LOWER_THRESHOLD = 20
            TechnicalAnalyser.RSI_LOOKUP_PERIOD = 14
            TechnicalAnalyser.RSI_TREND_PERIODS = 3
            # Intraday Supertrend — original defaults (not optimised yet)
            TechnicalAnalyser.SUPERTREND_PERIOD = 10
            TechnicalAnalyser.SUPERTREND_MULTIPLIER = 3
            # Intraday Stochastic — original defaults (not optimised yet)
            TechnicalAnalyser.STOCHASTIC_K_PERIOD = 14
            TechnicalAnalyser.STOCHASTIC_D_PERIOD = 3
            TechnicalAnalyser.STOCHASTIC_UPPER = 80
            TechnicalAnalyser.STOCHASTIC_LOWER = 20
        else:
            TechnicalAnalyser.FAST_EMA_PERIOD = 50
            TechnicalAnalyser.SLOW_EMA_PERIOD = 200
            # Positional RSI — optimised (18 stocks, 2020-2024, profit_factor)
            TechnicalAnalyser.RSI_UPPER_THRESHOLD = 85
            TechnicalAnalyser.RSI_LOWER_THRESHOLD = 30
            TechnicalAnalyser.RSI_LOOKUP_PERIOD = 14
            TechnicalAnalyser.RSI_TREND_PERIODS = 5
            # Positional Supertrend — optimised (18 stocks, 2020-2024, profit_factor)
            TechnicalAnalyser.SUPERTREND_PERIOD = 14
            TechnicalAnalyser.SUPERTREND_MULTIPLIER = 2.5
            # Positional Stochastic — optimised (18 stocks, 2020-2024, profit_factor)
            TechnicalAnalyser.STOCHASTIC_K_PERIOD = 5
            TechnicalAnalyser.STOCHASTIC_D_PERIOD = 5
            TechnicalAnalyser.STOCHASTIC_UPPER = 90
            TechnicalAnalyser.STOCHASTIC_LOWER = 30
        logger.debug(f"Technical Analyser constants reset for mode {shared.app_ctx.mode.name}")
        logger.debug(f"RSI_UPPER_THRESHOLD = {TechnicalAnalyser.RSI_UPPER_THRESHOLD} , RSI_LOWER_THRESHOLD = {TechnicalAnalyser.RSI_LOWER_THRESHOLD}, ATR_THRESHOLD = {TechnicalAnalyser.ATR_THRESHOLD}")

    
    def _compute_rsi(self, close_series: pd.Series) -> pd.Series:
        """
        Shared RSI computation using Wilder's smoothing method.
        
        Args:
            close_series: Series of closing prices
            
        Returns:
            RSI series
        """
        if len(close_series) < self.RSI_LOOKUP_PERIOD + 1:
            raise ValueError(f"Need at least {self.RSI_LOOKUP_PERIOD + 1} data points to compute RSI")

        delta = close_series.diff().dropna()

        gains = delta.where(delta > 0, 0)
        losses = -delta.where(delta < 0, 0)

        # Use Wilder's smoothing method (EMA with adjust=False)
        avg_gain = gains.ewm(span=self.RSI_LOOKUP_PERIOD, adjust=False).mean()
        avg_loss = losses.ewm(span=self.RSI_LOOKUP_PERIOD, adjust=False).mean()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_rsi(self, stock: Stock):
        """
        RSI Overbought/Oversold strategy with trend filter.
        
        Logic:
        - RSI > Upper Threshold → BEARISH (expect mean reversion down)
        - RSI < Lower Threshold → BULLISH (expect mean reversion up)
        
        Trend Filter (optional, improves win rate):
        - Only signal if trend aligns with expected reversal direction
        - Uses EMA 20/50 crossover for trend detection
        - BULLISH signal: Only if trend is BEARISH or NEUTRAL (reversal expected)
        - BEARISH signal: Only if trend is BULLISH or NEUTRAL (reversal expected)
        """
        try : 
            logger.debug(f'Inside analyse_rsi for stock {stock.stock_symbol}')
            close = stock.priceData["Close"]
            
            # Need enough data for RSI + EMA trend filter
            if len(close) < 100:
                return False
            
            rsi_series = self._compute_rsi(close.iloc[-100:])
            
            # Calculate trend using EMA 20/50
            ema_20 = close.ewm(span=20, adjust=False).mean()
            ema_50 = close.ewm(span=50, adjust=False).mean()
            
            ema_20_curr = ema_20.iloc[-1]
            ema_50_curr = ema_50.iloc[-1]
            ema_20_prev = ema_20.iloc[-2]
            ema_50_prev = ema_50.iloc[-2]
            
            # Determine trend
            if ema_20_curr > ema_50_curr and ema_20_prev > ema_50_prev:
                trend = "BULLISH"
            elif ema_20_curr < ema_50_curr and ema_20_prev < ema_50_prev:
                trend = "BEARISH"
            else:
                trend = "NEUTRAL"
            
            current_rsi = rsi_series.iloc[-1]
            previous_rsi = rsi_series.iloc[-2]

            trend_found = False

            if current_rsi > TechnicalAnalyser.RSI_UPPER_THRESHOLD: 
                # Count how many candles RSI has been in overbought zone
                overbought_candles = 0
                for i in range(len(rsi_series) - 1, -1, -1):
                    if rsi_series.iloc[i] > TechnicalAnalyser.RSI_UPPER_THRESHOLD:
                        overbought_candles += 1
                    else:
                        break
                
                rsi_trend = "Overbought" if overbought_candles >= self.RSI_TREND_PERIODS else "Weakening"
                
                # Only signal BEARISH if price trend is BULLISH or NEUTRAL (reversal expected)
                # Skip signal if already in BEARISH trend (RSI overbought in downtrend = strong momentum)
                if rsi_trend == "Overbought" and trend in ("BULLISH", "NEUTRAL"):
                    RSIAnalysis = namedtuple("RSIAnalysis", [
                        "value", "previous_value", "trend", "price_trend", "zone_candles"
                    ])
                    stock.set_analysis("BEARISH", "RSI", RSIAnalysis(
                        value=current_rsi,
                        previous_value=previous_rsi,
                        trend=rsi_trend,
                        price_trend=trend,
                        zone_candles=overbought_candles
                    ))
                    trend_found = True
            elif current_rsi < TechnicalAnalyser.RSI_LOWER_THRESHOLD:
                # Count how many candles RSI has been in oversold zone
                oversold_candles = 0
                for i in range(len(rsi_series) - 1, -1, -1):
                    if rsi_series.iloc[i] < TechnicalAnalyser.RSI_LOWER_THRESHOLD:
                        oversold_candles += 1
                    else:
                        break
                
                rsi_trend = "Oversold" if oversold_candles >= self.RSI_TREND_PERIODS else "Strengthening"
                
                # Only signal BULLISH if price trend is BEARISH or NEUTRAL (reversal expected)
                # Skip signal if already in BULLISH trend (RSI oversold in uptrend = strong momentum)
                if rsi_trend == "Oversold" and trend in ("BEARISH", "NEUTRAL"):
                    RSIAnalysis = namedtuple("RSIAnalysis", [
                        "value", "previous_value", "trend", "price_trend", "zone_candles"
                    ])
                    stock.set_analysis("BULLISH", "RSI", RSIAnalysis(
                        value=current_rsi,
                        previous_value=previous_rsi,
                        trend=rsi_trend,
                        price_trend=trend,
                        zone_candles=oversold_candles
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
        """
        Enhanced Bollinger Band analysis with momentum approach and filters.
        
        Filters applied:
        1. Confirmation filter: Price must stay outside band for 2+ consecutive candles
        2. Trend filter: Signal must align with the dominant trend (EMA-based)
        
        Momentum Strategy:
        - Price > Upper Band + Uptrend → BULLISH (momentum continuation)
        - Price < Lower Band + Downtrend → BEARISH (momentum breakdown)
        """
        try : 
            def compute_bollinger_bands(series, window=20, num_std=2):
                """
                Compute Bollinger Bands for the entire series.
                Returns: DataFrame with sma, upper_band, lower_band columns
                """
                if len(series) < window:
                    return None, None, None
                
                sma = series.rolling(window=window).mean()
                std = series.rolling(window=window).std()
                
                upper = sma + num_std * std
                lower = sma - num_std * std
                
                return sma, upper, lower
            
            def get_trend(close_series, fast_period=20, slow_period=50):
                """
                Determine the dominant trend using EMA crossover.
                Returns: 'BULLISH', 'BEARISH', or 'NEUTRAL'
                """
                if len(close_series) < slow_period:
                    return 'NEUTRAL'
                
                fast_ema = close_series.ewm(span=fast_period, adjust=False).mean()
                slow_ema = close_series.ewm(span=slow_period, adjust=False).mean()
                
                fast_latest = fast_ema.iloc[-1]
                slow_latest = slow_ema.iloc[-1]
                
                # Check trend strength (percentage difference)
                pct_diff = ((fast_latest - slow_latest) / slow_latest) * 100
                
                if pct_diff > 0.5:  # Fast EMA is 0.5% above slow EMA
                    return 'BULLISH'
                elif pct_diff < -0.5:  # Fast EMA is 0.5% below slow EMA
                    return 'BEARISH'
                else:
                    return 'NEUTRAL'
            
            logger.debug(f'Inside analyse_Bolinger_band for stock {stock.stock_symbol}')
            
            close_series = stock.priceData['Close']
            
            # Compute Bollinger Bands for entire series
            sma_series, upper_series, lower_series = compute_bollinger_bands(
                close_series, 
                window=TechnicalAnalyser.BB_WINDOW, 
                num_std=TechnicalAnalyser.BB_NUM_STD
            )
            
            if sma_series is None or len(sma_series) < 3:
                return False
            
            curr_data = stock.current_equity_data
            curr_close = curr_data['Close']
            
            # Get latest band values
            upper_band = upper_series.iloc[-1]
            lower_band = lower_series.iloc[-1]
            sma = sma_series.iloc[-1]
            
            # Get previous candle values for confirmation filter
            prev_close = close_series.iloc[-2]
            prev_upper = upper_series.iloc[-2]
            prev_lower = lower_series.iloc[-2]
            
            BBAnalysis = namedtuple("BBAnalysis", [
                "close", "upper_band", "lower_band", "sma", 
                "signal_type", "trend", "confirmation_candles"
            ])
            
            # Determine dominant trend
            trend = get_trend(close_series)
            
            # ==========================================
            # FILTER 1: Confirmation Filter
            # Price must stay outside band for 2+ consecutive candles
            # ==========================================
            
            # Check for bullish breakout (price above upper band)
            above_upper_curr = curr_close > upper_band
            above_upper_prev = prev_close > prev_upper
            
            # Check for bearish breakdown (price below lower band)
            below_lower_curr = curr_close < lower_band
            below_lower_prev = prev_close < prev_lower
            
            confirmation_candles = 0
            signal_type = None
            sentiment = None
            
            # Bullish signal: Price above upper band for 2+ candles
            if above_upper_curr and above_upper_prev:
                confirmation_candles = 2
                # Check for more confirmation candles
                for i in range(3, min(6, len(close_series) + 1)):
                    if close_series.iloc[-i] > upper_series.iloc[-i]:
                        confirmation_candles += 1
                    else:
                        break
                
                # ==========================================
                # FILTER 2: Trend Filter
                # Only signal if trend aligns (BULLISH or NEUTRAL trend)
                # ==========================================
                if trend in ('BULLISH', 'NEUTRAL'):
                    signal_type = "momentum_breakout_above"
                    sentiment = "BULLISH"
                else:
                    # Trend is BEARISH - don't signal against the trend
                    logger.debug(f"BB bullish signal filtered: trend is {trend}")
                    return False
            
            # Bearish signal: Price below lower band for 2+ candles
            elif below_lower_curr and below_lower_prev:
                confirmation_candles = 2
                # Check for more confirmation candles
                for i in range(3, min(6, len(close_series) + 1)):
                    if close_series.iloc[-i] < lower_series.iloc[-i]:
                        confirmation_candles += 1
                    else:
                        break
                
                # ==========================================
                # FILTER 2: Trend Filter
                # Only signal if trend aligns (BEARISH or NEUTRAL trend)
                # ==========================================
                if trend in ('BEARISH', 'NEUTRAL'):
                    signal_type = "momentum_breakout_below"
                    sentiment = "BEARISH"
                else:
                    # Trend is BULLISH - don't signal against the trend
                    logger.debug(f"BB bearish signal filtered: trend is {trend}")
                    return False
            
            # No signal if confirmation filter not met
            if signal_type is None:
                return False
            
            # Set analysis with enhanced data
            stock.set_analysis(sentiment, "BollingerBand", BBAnalysis(
                close=curr_close, 
                upper_band=upper_band, 
                lower_band=lower_band,
                sma=sma,
                signal_type=signal_type,
                trend=trend,
                confirmation_candles=confirmation_candles
            ))
            
            band_name = 'upper' if 'above' in signal_type else 'lower'
            band_value = upper_band if 'above' in signal_type else lower_band
            logger.info(f"BB {signal_type} for {stock.stock_symbol}: "
                       f"close={curr_close:.2f}, band={band_name}={band_value:.2f}, "
                       f"trend={trend}, confirmation={confirmation_candles}candles")
            return True
            
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
            macd_data = calculate_latest_macd(price_data, fast_period=TechnicalAnalyser.MACD_FAST_PERIOD, slow_period=TechnicalAnalyser.MACD_SLOW_PERIOD, signal_period=TechnicalAnalyser.MACD_SIGNAL_PERIOD)
            
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
        Detects RSI divergence with enhanced filters for higher conviction signals.
        
        Bearish divergence: price makes a higher high but RSI makes a lower high.
        Bullish divergence: price makes a lower low but RSI makes a higher low.
        
        Enhanced Filters:
        1. RSI Zone: Divergence only valid in overbought/oversold zones
        2. Trend Filter: Only signal if trend is weakening (not in strong trend)
        3. Minimum RSI Difference: At least 3 points difference between swings
        4. Larger Swing Order: 3 candles for more significant swings
        """
        try:
            logger.debug(f'Inside analyse_rsi_divergence for stock {stock.stock_symbol}')
            close = stock.priceData['Close']
            
            # Enhanced parameters
            lookback = 50  # Balanced lookback
            order = 3  # Increased from 2 for significant swings
            min_rsi_diff = 3  # Minimum RSI difference for valid divergence
            rsi_overbought = 75  # Relaxed zone for bearish divergence
            rsi_oversold = 25  # Relaxed zone for bullish divergence

            if len(close) < lookback + self.RSI_LOOKUP_PERIOD + 1:
                return False

            # Compute RSI using shared method
            rsi = self._compute_rsi(close)

            # Work on the recent window (reset index for simple integer indexing)
            recent_close = close.iloc[-lookback:].reset_index(drop=True)
            recent_rsi = rsi.iloc[-lookback:].reset_index(drop=True)
            
            # Calculate trend using EMA
            ema_20 = close.ewm(span=20, adjust=False).mean()
            ema_50 = close.ewm(span=50, adjust=False).mean()
            
            # Determine trend - check if EMAs are converging (trend weakening)
            ema_diff_curr = abs(ema_20.iloc[-1] - ema_50.iloc[-1])
            ema_diff_prev = abs(ema_20.iloc[-5] - ema_50.iloc[-5])
            trend_weakening = ema_diff_curr < ema_diff_prev
            
            # Determine trend direction
            if ema_20.iloc[-1] > ema_50.iloc[-1]:
                trend = "BULLISH"
            elif ema_20.iloc[-1] < ema_50.iloc[-1]:
                trend = "BEARISH"
            else:
                trend = "NEUTRAL"

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
                "rsi_current", "rsi_previous", "trend", "trend_weakening"
            ])

            # Bearish divergence: price higher-high, RSI lower-high
            # Enhanced: Only in overbought zone + trend weakening
            price_highs = find_swing_highs(recent_close, order)
            if len(price_highs) >= 2:
                h1, h2 = price_highs[-2], price_highs[-1]
                rsi_curr = recent_rsi.iloc[h2]
                rsi_prev = recent_rsi.iloc[h1]
                rsi_diff = rsi_prev - rsi_curr  # Should be positive for lower high
                
                # Enhanced filters:
                # 1. Price makes higher high
                # 2. RSI makes lower high (with minimum difference)
                # 3. Current RSI in overbought zone (relaxed)
                # 4. Trend is weakening OR not strongly bullish
                if (recent_close.iloc[h2] > recent_close.iloc[h1] and
                        rsi_diff >= min_rsi_diff and
                        rsi_curr >= rsi_overbought and
                        (trend_weakening or trend != "BULLISH")):
                    stock.set_analysis("BEARISH", "RSI_DIVERGENCE", RSIDivergenceAnalysis(
                        divergence_type="bearish",
                        price_current=recent_close.iloc[h2],
                        price_previous=recent_close.iloc[h1],
                        rsi_current=rsi_curr,
                        rsi_previous=rsi_prev,
                        trend=trend,
                        trend_weakening=trend_weakening
                    ))
                    return True

            # Bullish divergence: price lower-low, RSI higher-low
            # Enhanced: Only in oversold zone + trend weakening
            price_lows = find_swing_lows(recent_close, order)
            if len(price_lows) >= 2:
                l1, l2 = price_lows[-2], price_lows[-1]
                rsi_curr = recent_rsi.iloc[l2]
                rsi_prev = recent_rsi.iloc[l1]
                rsi_diff = rsi_curr - rsi_prev  # Should be positive for higher low
                
                # Enhanced filters:
                # 1. Price makes lower low
                # 2. RSI makes higher low (with minimum difference)
                # 3. Current RSI in oversold zone (relaxed)
                # 4. Trend is weakening OR not strongly bearish
                if (recent_close.iloc[l2] < recent_close.iloc[l1] and
                        rsi_diff >= min_rsi_diff and
                        rsi_curr <= rsi_oversold and
                        (trend_weakening or trend != "BEARISH")):
                    stock.set_analysis("BULLISH", "RSI_DIVERGENCE", RSIDivergenceAnalysis(
                        divergence_type="bullish",
                        price_current=recent_close.iloc[l2],
                        price_previous=recent_close.iloc[l1],
                        rsi_current=rsi_curr,
                        rsi_previous=rsi_prev,
                        trend=trend,
                        trend_weakening=trend_weakening
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
        
        Note: This strategy works best WITHOUT trend filter.
        Stochastic is a mean reversion indicator that benefits from more signals.
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