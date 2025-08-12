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
    
    
    def __init__(self) -> None:
        self.analyserName = "Technical Analyser"
        super().__init__()
    
    def reset_constants(self):
        # if constant.mode.name == constant.Mode.INTRADAY.name:
        #     #add something later on this
        # else:
        #      #add something later on this
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