from enum import Enum
class Mode (Enum):
    INTRADAY = 1
    POSITIONAL = 2

mode = None

#ENVS
ENV_PRODUCTION = "PRODUCTION"
ENV_SHUTDOWN = "SHUTDOWN"
ENV_ENABLE_DERIVATIVES = "ENABLE_DERIVATIVES"


#DEV ENVIRONMENTS
ENV_DEV_INTRADAY = "DEV_INTRADAY"
ENV_DEV_POSITIONAL = "DEV_POSITIONAL"


#DEV_CONSTANTS
NO_OF_STOCKS = -1
NO_OF_INDEX = -1


#INTRADAY CONSTANTS
INTRADAY_SLEEP_TIME = 301


#NOTIFICATION CONSTANTS

TELEGRAM_TOKEN = '7042349293:AAGW0-OzOwfvbKdkuM6G40UfcXIHcs_YJwk' 
TELEGRAM_CHAT_ID = "1462841143"
TELEGRAM_URL = 'https://api.telegram.org/bot'

#FILE NAMES
DERIVATIVE_LIST_FILENAME = "final_derivatives_list.json"
STOCK_DATA_FILENAME = "stock_data.json"


#ANALYSIS CONSTANTS
REQUIRED_TRENDS = 2


NseOptionChainURL = "https://www.nseindia.com/option-chain"

# Opstra Data Collection Constants
OpstraURLs = {"TickerURL" : "https://opstra.definedge.com/api/tickers",
            "MonthlyExpiryURL" : "https://opstra.definedge.com/api/monthlies",
            "WeeklyExpiryURL" : "https://opstra.definedge.com/api/weeklies",
            "IVChartURL": "https://opstra.definedge.com/api/ivcharts/{}",
            "FII_DII_DATA_URL": "https://opstra.definedge.com/api/fiidiidata"}

# ---------- column lists-----------------






