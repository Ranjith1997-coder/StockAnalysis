
#ENVS
ENV_PRODUCTION = "PRODUCTION"
ENV_SHUTDOWN = "SHUTDOWN"
ENV_ENABLE_NSE_DERIVATIVES = "ENABLE_NSE_DERIVATIVES"
ENV_ENABLE_ZERODHA_DERIVATIVES = "ENABLE_ZERODHA_DERIVATIVES"
ENV_ENABLE_ZERODHA_API = "ENABLE_ZERODHA_API"
ENV_ENABLE_TELEGRAM_BOT = "ENABLE_TELEGRAM_BOT"
ENV_ENABLE_POST_MARKET = "ENABLE_POST_MARKET"


#DEV ENVIRONMENTS
ENV_DEV_INTRADAY = "DEV_INTRADAY"
ENV_DEV_POSITIONAL = "DEV_POSITIONAL"


#DEV_CONSTANTS
NO_OF_STOCKS = -1
NO_OF_INDEX = -1


#INTRADAY CONSTANTS
INTRADAY_SLEEP_TIME = 310


#NOTIFICATION CONSTANTS

TELEGRAM_INTRADAY_TOKEN = '8282998108:AAFXTZG2c7ltq6V6Aa1jzVU0m0rEGBdQyoc' 
TELEGRAM_INTRADAY_CHAT_ID = "1462841143"

TELEGRAM_POSITIONAL_TOKEN = "8418083942:AAGvrdcJWYncYYMiQaZlw2R0gJtGgnFCCbc"
TELEGRAM_POSITIONAL_CHAT_ID = "1462841143"

TELEGRAM_URL = 'https://api.telegram.org/bot'

#FILE NAMES
DERIVATIVE_LIST_FILENAME = "final_derivatives_list.json"
STOCK_DATA_FILENAME = "stock_data.json"


#ANALYSIS CONSTANTS
# Legacy - kept for backward compatibility
REQUIRED_TRENDS = 3

# Scoring-based notification system
# Each analysis type has a weight that contributes to the total score
ANALYSIS_WEIGHTS = {
    # Technical Indicators
    "RSI": 15,
    "rsi_crossover": 12,          # RSI crossing key levels
    "MACD": 15,
    "EMA_CROSSOVER": 12,
    "BollingerBand": 10,          # Price at Bollinger Bands
    "vwap_deviation": 12,         # VWAP deviation signal
    "ATR": 8,                     # Volatility measure
    "VOLUME": 10,
    "Volume": 10,                 # Alternative key
    "BUY_SELL": 10,               # Buy/Sell quantity imbalance
    
    # Candlestick Patterns
    "ThreeContInc": 12,
    "ThreeContDec": 12,
    "TwoContInc": 8,
    "TwoContDec": 8,
    "Marubozu": 10,
    "Single_candle_stick_pattern": 8,
    "Double_candle_stick_pattern": 10,
    "Triple_candle_stick_pattern": 12,
    
    # Options & Derivatives Analysis
    "MAX_PAIN": 15,
    "MAX_PAIN_TREND": 12,
    "MAX_PAIN_ALIGNMENT": 18,     # High weight when multiple signals align
    "PCR_EXTREME": 14,
    "PCR_BIAS": 10,
    "PCR_TREND": 12,
    "PCR_REVERSAL": 16,          # PCR zone crossover or trend reversal
    "PCR_DIVERGENCE": 14,
    "IV_SPIKE": 12,
    "IV_TREND": 10,
    
    # Futures Analysis
    "FUTURES_PREMIUM": 12,
    "OI_BUILDUP": 14,
    "FUTURE_ACTION": 14,          # Futures OI + Price action
    "FUTURE_BREAKOUT_PATTERN": 15,# ORB breakout with OI confirmation
    "FUTURE_PVO_PATTERN": 10,     # Price/Volume/OI patterns
    
    # Price Levels
    "52-week-high": 8,
    "52-week-low": 8,
    
    # Default weight for unlisted analysis types
    "DEFAULT": 10
}

# Notification priority thresholds
# Score determines which priority bucket the notification falls into
NOTIFICATION_PRIORITY = {
    "LOW": 20,       # Score >= 20: Low priority (informational)
    "MEDIUM": 35,    # Score >= 35: Medium priority (notable)
    "HIGH": 50,      # Score >= 50: High priority (actionable)
    "CRITICAL": 70   # Score >= 70: Critical (strong conviction signal)
}

# Minimum score required to send any notification
MIN_NOTIFICATION_SCORE = 45

# Bonus multipliers for signal alignment
SIGNAL_ALIGNMENT_BONUS = {
    "ALL_BULLISH": 1.3,    # All signals are bullish - 30% bonus
    "ALL_BEARISH": 1.3,    # All signals are bearish - 30% bonus  
    "MIXED": 1.0,          # Mixed signals - no bonus
    "CONFIRMATION": 1.5    # Technical + Options aligned - 50% bonus
}

# Analysis categories for alignment detection
TECHNICAL_ANALYSES = {"RSI", "MACD", "EMA_CROSSOVER", "ThreeContInc", "ThreeContDec", 
                      "TwoContInc", "TwoContDec", "Marubozu"}
OPTIONS_ANALYSES = {"MAX_PAIN", "MAX_PAIN_TREND", "MAX_PAIN_ALIGNMENT", 
                    "PCR_EXTREME", "PCR_BIAS", "PCR_TREND", "PCR_REVERSAL", "PCR_DIVERGENCE",
                    "IV_SPIKE", "IV_TREND"}
FUTURES_ANALYSES = {"FUTURES_PREMIUM", "OI_BUILDUP"}

# NEUTRAL signals that should NOT contribute to score
# These indicate uncertainty/mixed signals rather than actionable info
NEUTRAL_EXCLUDE_FROM_SCORE = {
    "MAX_PAIN_ALIGNMENT",   # When DIVERGENT - conflicting signals
    "MAX_PAIN_TREND",       # When DIVERGING - price moving away from max pain
    "PCR_DIVERGENCE",       # Term structure divergence - uncertainty
}

# NEUTRAL signals that SHOULD contribute to score (informational but valuable)
# Everything not in NEUTRAL_EXCLUDE_FROM_SCORE will score:
# - IV_SPIKE: Important volatility event
# - IV_TREND: Directional IV movement  
# - 52-week-high/low: Key price levels
# - ATR: Volatility measure
# - FUTURE_PVO_PATTERN: Price/Volume/OI patterns

#ZERODHA CONSTANTS
ENV_ZERODHA_USERNAME = "ZERODHA_USER"
ENV_ZERODHA_PASSWORD = "ZERODHA_PASS"
ENV_ZERODHA_ENC_TOKEN = "ZERODHA_ENC_TOKEN"
DUMMY_API_KEY_ZERODHA = "dummy_api_key"


NseOptionChainURL = "https://www.nseindia.com/option-chain"

# Opstra Data Collection Constants
OpstraURLs = {"TickerURL" : "https://opstra.definedge.com/api/tickers",
            "MonthlyExpiryURL" : "https://opstra.definedge.com/api/monthlies",
            "WeeklyExpiryURL" : "https://opstra.definedge.com/api/weeklies",
            "IVChartURL": "https://opstra.definedge.com/api/ivcharts/{}",
            "FII_DII_DATA_URL": "https://opstra.definedge.com/api/fiidiidata"}

# ---------- column lists-----------------






