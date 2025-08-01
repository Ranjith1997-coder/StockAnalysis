import traceback
from analyser.Analyser import BaseAnalyzer
from common.Stock import Stock
import common.constants as constant
from common.logging_util import logger
from collections import namedtuple
import pandas as pd
from datetime import datetime
from common.helperFunctions import percentageChange

class TechnicalAnalyser(BaseAnalyzer):

    RSI_UPPER_THRESHOLD = 80
    RSI_LOWER_THRESHOLD = 20
    RSI_LOOKUP_PERIOD = 14
    ATR_THRESHOLD = 0.97
    VWAP_DEVIATION_PERCENTAGE = 2
    VWAP_DAYS = 10
    
    def __init__(self) -> None:
        self.analyserName = "Technical Analyser"
        super().__init__()
    
    def reset_constants(self):
        # if constant.mode.name == constant.Mode.INTRADAY.name:
        #     #add something later on this
        # else:
        #      #add something later on this
        logger.debug(f"Technical Analyser constants reset for mode {constant.mode.name}")
        logger.debug(f"RSI_UPPER_THRESHOLD = {TechnicalAnalyser.RSI_UPPER_THRESHOLD} , RSI_LOWER_THRESHOLD = {TechnicalAnalyser.RSI_LOWER_THRESHOLD}, ATR_THRESHOLD = {TechnicalAnalyser.ATR_THRESHOLD}")


    def compute_rsi(self, close_series):
        if len(close_series) < TechnicalAnalyser.RSI_LOOKUP_PERIOD + 1:
            raise ValueError(f"Need at least {TechnicalAnalyser.RSI_LOOKUP_PERIOD  + 1} data points to compute RSI")

        # change = close_series.diff()
        # up_series = change.mask(change < 0, 0.0)
        # down_series = -change.mask(change > 0, -0.0)

        # #@numba.jit
        # def rma(x, n):
        #     """Running moving average"""
        #     a = np.full_like(x, np.nan)
        #     # pdb.set_trace()
        #     a[n] = x[1:n+1].mean()
        #     for i in range(n+1, len(x)):
        #         a[i] = (a[i-1] * (n - 1) + x[i]) / n
        #     return a

        # avg_gain = rma(up_series.to_numpy(), TechnicalAnalyser.RSI_LOOKUP_PERIOD)
        # avg_loss = rma(down_series.to_numpy(), TechnicalAnalyser.RSI_LOOKUP_PERIOD)

        # rs = avg_gain / avg_loss
        # rsi = 100 - (100 / (1 + rs))
        # return rsi[-1]
        delta = close_series.diff().dropna()

        gains = delta.where(delta > 0, 0)
        losses = -delta.where(delta < 0, 0)

        # Use Wilder's smoothing method (EMA with adjust=False)
        avg_gain = gains.ewm(span=TechnicalAnalyser.RSI_LOOKUP_PERIOD, adjust=False).mean()
        avg_loss = losses.ewm(span=TechnicalAnalyser.RSI_LOOKUP_PERIOD, adjust=False).mean()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi.iloc[-1]
    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_rsi(self, stock: Stock):
        try : 
            logger.debug(f'Inside analyse_rsi for stock {stock.stock_symbol}')
            rsi_value = self.compute_rsi(stock.priceData["Close"].iloc[(-TechnicalAnalyser.RSI_LOOKUP_PERIOD * 5 ) -1:])
            RSIAnalysis = namedtuple("RSIAnalysis", ["value"])            

            if rsi_value > TechnicalAnalyser.RSI_UPPER_THRESHOLD: 
                stock.set_analysis("BEARISH", "RSI", RSIAnalysis(value=rsi_value))
                return True
            elif rsi_value < TechnicalAnalyser.RSI_LOWER_THRESHOLD:
                stock.set_analysis("BULLISH", "RSI", RSIAnalysis(value=rsi_value))
                return True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_rsi for stock {stock.stock_symbol}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    @BaseAnalyzer.both
    @BaseAnalyzer.index_both
    def analyse_rsi_crossover(self, stock: Stock):
        try : 
            logger.debug(f'Inside analyse_rsi_crossover for stock {stock.stock_symbol}')
            curr_rsi_value = self.compute_rsi(stock.priceData["Close"].iloc[(-TechnicalAnalyser.RSI_LOOKUP_PERIOD * 5 ) - 1:])
            prev_rsi_value = self.compute_rsi(stock.priceData["Close"].iloc[(-TechnicalAnalyser.RSI_LOOKUP_PERIOD * 5 ) - 2 : -1])
            RSICrossoverAnalysis = namedtuple("RSICrossoverAnalysis", ["curr_value", "prev_value"])
            if prev_rsi_value > TechnicalAnalyser.RSI_UPPER_THRESHOLD and curr_rsi_value < TechnicalAnalyser.RSI_UPPER_THRESHOLD: 
                stock.set_analysis("BEARISH", "rsi_crossover", RSICrossoverAnalysis(curr_value=curr_rsi_value, prev_value=prev_rsi_value))
                return True
            elif prev_rsi_value < TechnicalAnalyser.RSI_LOWER_THRESHOLD and curr_rsi_value > TechnicalAnalyser.RSI_LOWER_THRESHOLD: 
                stock.set_analysis("BULLISH", "rsi_crossover", RSICrossoverAnalysis(curr_value=curr_rsi_value, prev_value=prev_rsi_value))
                return True
            return False
        except Exception as e:
            logger.error(f"Error in analyse_rsi_crossover for stock {stock.stock_symbol}: {str(e)}")
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