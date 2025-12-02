
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
REQUIRED_TRENDS = 3

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






