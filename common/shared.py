from enum import Enum
class Mode (Enum):
    INTRADAY = 1
    POSITIONAL = 2

class AppContext:
    def __init__(self):
        self.stock_token_obj_dict = {}
        self.index_token_obj_dict = {}
        self.ticker_token_obj_dict_zerodha = {}
        self.stocks_list = []
        self.index_list = []
        self.stockExpires = []
        self.mode = None
        self.zd_ticker_manager = None
        self.zd_kc = None

app_ctx = AppContext()
ticker_52_week_high_list = []
ticker_52_week_low_list = []