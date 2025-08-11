from enum import Enum
class Mode (Enum):
    INTRADAY = 1
    POSITIONAL = 2

class AppContext:
    def __init__(self):
        self.stock_token_obj_dict = {}
        self.stock_name_obj_dict = {}
        self.index_token_obj_dict = {}
        self.index_name_obj_dict = {}
        self.stocks_list = []
        self.index_list = []
        self.stockExpires = []
        self.mode = None
        self.zd_ticker_manager = None

app_ctx = AppContext()