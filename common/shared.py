from __future__ import annotations
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from zerodha.zerodha_connect import KiteConnect
    from zerodha.zerodha_analysis import ZerodhaTickerManager
    from common.token_registry import TokenRegistry
    from intelligence.signal_bus import SignalBus
    from intelligence.correlator import SignalCorrelator
    from intelligence.narrator import MarketNarrator

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
        self.token_registry: Optional[TokenRegistry] = None
        self.signal_bus: Optional[SignalBus] = None
        self.correlator: Optional[SignalCorrelator] = None
        self.narrator: Optional[MarketNarrator] = None
        # Feed health: wall-clock time of last equity tick received
        self.last_equity_tick_time: float = 0.0
        # LLM budget: flag to prevent repeated 80% alerts per day
        self.llm_budget_warned: bool = False
        # Options data source: "zerodha" (default) or "sensibull"
        self.options_source: str = "zerodha"
        self.sensibull_feed = None  # SensibullFeed instance when OPTIONS_SOURCE=sensibull
        # ── Debug counters (incremented in the intraday/positional loops) ──
        self.intraday_cycle_count: int = 0
        self.monitor_result_counts: dict = {"SUCCESS": 0, "NO_DATA": 0, "ERROR": 0}
        self.error_count: int = 0
        self.last_cycle_time: float = 0.0

app_ctx = AppContext()
ticker_52_week_high_list = []
ticker_52_week_low_list = []