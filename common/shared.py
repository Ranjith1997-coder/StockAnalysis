from __future__ import annotations
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from zerodha.zerodha_connect import KiteConnect
    from zerodha.zerodha_analysis import ZerodhaTickerManager

class Mode (Enum):
    INTRADAY = 1
    POSITIONAL = 2

class AppContext:
    def __init__(self):
        self.stock_token_obj_dict = {}
        self.index_token_obj_dict = {}
        self.commodity_token_obj_dict = {}
        self.global_indices_token_obj_dict = {}
        self.ticker_token_obj_dict_zerodha = {}
        self.stocks_list = []
        self.index_list = []
        self.commodity_list = []
        self.global_indices_list = []
        self.stockExpires = []
        self.mode: Optional[Mode] = None
        self.zd_ticker_manager: Optional[ZerodhaTickerManager] = None
        self.zd_kc: Optional[KiteConnect] = None

app_ctx = AppContext()
ticker_52_week_high_list = []
ticker_52_week_low_list = []