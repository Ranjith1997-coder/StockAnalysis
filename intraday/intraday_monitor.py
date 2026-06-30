import json
import os
import sys
import time as _time
import traceback
import html
import argparse
import uuid
from notification.Notification import TELEGRAM_NOTIFICATIONS
from datetime import datetime, time, timedelta
import common.constants as constant
import common.shared as shared
from common.Stock import Stock
from common.helperFunctions import *
from common.market_calendar import is_trading_day
from enum import Enum
from time import sleep
from analyser.Analyser import AnalyserOrchestrator
from analyser.Futures_Analyser import FuturesAnalyser
from analyser.VolumeAnalyser import VolumeAnalyser
from analyser.TechnicalAnalyser import TechnicalAnalyser
from analyser.candleStickPatternAnalyser import CandleStickAnalyser
from analyser.IVAnalyser import IVAnalyser
from analyser.PCRAnalyser import PCRAnalyser
from analyser.MaxPainAnalyser import MaxPainAnalyser
from analyser.OIChainAnalyser import OIChainAnalyser
from analyser.GEXAnalyser import GEXAnalyser
from analyser.PanicModeAnalyser import PanicModeAnalyser
from analyser.OptionSellerCompositeAnalyser import OptionSellerCompositeAnalyser
from common.logging_util import logger
from typing import List, Tuple, Optional
from zerodha.zerodha_analysis import ZerodhaTickerManager
from zerodha.live_options_engine import LiveOptionsEngine
from dotenv import load_dotenv
from notification.bot_listener import init_telegram_bot
import threading
from zerodha.zerodha_connect import KiteConnect
from post_market_analysis.runner import run_and_summarize
from premarket.premarket_report import run_global_cues_report, run_preopen_report
from urllib.parse import quote
from common.token_registry import TokenRegistry, TokenInfo, TokenType
from intelligence.signal_bus import SignalBus
from intelligence.correlator import SignalCorrelator, Confluence
from intelligence.signal import Signal, Direction, Layer, weight_to_strength
from zerodha.live_stock_engine import LiveStockEngine
from services.common.redis_proxy import RedisProxy
from services.common.stock_loader import (
    load_price_data_from_redis,
    load_sensibull_from_redis,
    load_zerodha_from_redis,
)
from services.common.cycle_subscriber import CycleSubscriber


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1: Global Exception Catcher — sends fatal tracebacks to Telegram
# ═══════════════════════════════════════════════════════════════════════════

def _crash_handler(exc_type, exc_value, exc_tb):
    """Last-resort handler for uncaught exceptions.

    Formats the traceback and fires a high-priority Telegram alert so the
    on-call engineer knows the process died, even at 2 AM.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return

    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    # Truncate to fit Telegram's 4096-char limit with room for the wrapper
    max_tb = 3500
    if len(tb_text) > max_tb:
        tb_text = tb_text[:max_tb] + "\n… (truncated)"

    # Also cap the one-liner summary (exc_value can be huge)
    exc_summary = f"{exc_type.__name__}: {exc_value}"
    if len(exc_summary) > 200:
        exc_summary = exc_summary[:200] + "…"

    # HTML-escape so repr strings like <urllib3.HTTPSConnection object> don't
    # break Telegram's HTML parser (status 400 "Unsupported start tag").
    message = (
        "🚨 <b>FATAL CRASH — Process Died</b>\n\n"
        f"<b>Exception:</b> <code>{html.escape(exc_summary)}</code>\n\n"
        f"<pre>{html.escape(tb_text)}</pre>"
    )

    try:
        TELEGRAM_NOTIFICATIONS.send_notification(message, parse_mode="HTML")
    except Exception:
        pass  # absolutely must not raise here

    logger.critical("Uncaught exception — crash handler fired", exc_info=(exc_type, exc_value, exc_tb))

sys.excepthook = _crash_handler


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2: Heartbeat — pings healthchecks.io at end of each analysis cycle
# ═══════════════════════════════════════════════════════════════════════════

def _ping_healthcheck():
    """Ping an external dead-man's switch (healthchecks.io) to signal liveness.

    Reads the URL from the HEALTHCHECK_URL env var.  If the variable is
    unset or the request fails, the trading loop is *never* disrupted.
    """
    url = os.getenv("HEALTHCHECK_URL")
    if not url:
        return
    try:
        import requests
        requests.get(url, timeout=5)
    except Exception:
        logger.debug("Healthcheck ping failed (non-fatal)")


# ═══════════════════════════════════════════════════════════════════════════
# Layer 3: Zombie Data Watchdog — detects stale live-options data
# ═══════════════════════════════════════════════════════════════════════════

_stale_alerts_sent = set()  # tracks symbols already alerted this session
_stale_alerts_lock  = threading.Lock()  # guards check-then-add on _stale_alerts_sent

def check_data_freshness(stock, stale_threshold_sec=120):
    """Check that live options data has been updated recently.

    Only fires during market hours on trading days.  If
    ``stock.options_aggregate['last_updated']`` is older than
    *stale_threshold_sec* seconds, sends a one-time Telegram warning.

    Returns:
        True  — data is fresh or check was skipped (outside market hours)
        False — stale data detected and alert sent
    """
    now = datetime.now()

    # Gate 1: only during market hours
    if not isNowInTimePeriod(time(9, 15), time(15, 30), now.time()):
        return True

    # Gate 2: only on trading days
    try:
        from common.market_calendar import is_trading_day
        if not is_trading_day(now.date()):
            return True
    except Exception:
        pass  # fail-open — assume trading day

    # Gate 3: check the timestamp
    options_agg = getattr(stock, "options_aggregate", None)
    if not options_agg:
        return True  # no options tracking for this stock — nothing to check

    last_updated = options_agg.get("last_updated")
    if not last_updated:
        return True  # field not populated yet (0.0 = never written)

    if isinstance(last_updated, (int, float)):
        age = now.timestamp() - last_updated
    elif hasattr(last_updated, "timestamp"):
        # datetime-like object
        age = now.timestamp() - last_updated.timestamp()
    else:
        return True  # unknown format — skip

    if age > stale_threshold_sec:
        symbol = getattr(stock, "stock_symbol", "UNKNOWN")
        with _stale_alerts_lock:
            if symbol in _stale_alerts_sent:
                return False
            _stale_alerts_sent.add(symbol)
        # Build and send the alert outside the lock so we never hold a lock over I/O
        msg = (
            f"⚠️ <b>STALE DATA — {symbol}</b>\n\n"
            f"options_aggregate last updated <b>{int(age)}s ago</b> "
            f"(threshold: {stale_threshold_sec}s).\n"
            f"WebSocket may have dropped silently."
        )
        try:
            TELEGRAM_NOTIFICATIONS.send_notification(msg, parse_mode="HTML")
        except Exception:
            pass
        logger.warning(f"Stale data detected for {symbol}: {int(age)}s old")
        return False

    return True

class Trend (Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


orchestrator : AnalyserOrchestrator =  None
PRODUCTION = False
DEV_NOTIFY = False      # True = send Telegram alerts even in dev mode (DEV_NOTIFY=1)
ENABLE_ZERODHA_DERIVATIVES = False
ENABLE_ZERODHA_API = False
ENABLE_TELEGRAM_BOT = False
ENABLE_POST_MARKET = False
ENABLE_LIVE_OPTIONS = False
LIVE_OPTIONS_ONLY   = False   # When True: skip all regular analysis, run live options engine only
ENABLE_INTELLIGENCE = False   # When True: SignalBus + Correlator + morning bias + LiveStockEngine
redis_proxy: Optional[RedisProxy] = None
cycle_subscriber: Optional[CycleSubscriber] = None

class MonitorResult(Enum):
    SUCCESS = 0
    NO_DATA = 1
    ERROR = 2

def process_monitor_results(results):
    for result, trend_found, message in results:
        shared.app_ctx.monitor_result_counts[result.name] = \
            shared.app_ctx.monitor_result_counts.get(result.name, 0) + 1
        if result == MonitorResult.NO_DATA:
            if message:
                logger.warning(message)
        elif result == MonitorResult.ERROR:
            shared.app_ctx.error_count += 1
            logger.error(f"Error during monitoring: {message}")
        elif trend_found:
            logger.info(f"Trend found: \n{message}")

def _register_base_tokens():
    """Register all equity, index, commodity, and global index tokens in the token registry."""
    registry = shared.app_ctx.token_registry
    if registry is None:
        return

    for token, stock in shared.app_ctx.stock_token_obj_dict.items():
        registry.register(TokenInfo(
            token=token, token_type=TokenType.EQUITY,
            parent_symbol=stock.stock_symbol, tradingsymbol=stock.stock_symbol
        ))
        registry.set_parent_object(stock.stock_symbol, stock)

    for token, index in shared.app_ctx.index_token_obj_dict.items():
        registry.register(TokenInfo(
            token=token, token_type=TokenType.INDEX,
            parent_symbol=index.stock_symbol, tradingsymbol=index.stock_symbol
        ))
        registry.set_parent_object(index.stock_symbol, index)

    for token, commodity in shared.app_ctx.commodity_token_obj_dict.items():
        registry.register(TokenInfo(
            token=token, token_type=TokenType.COMMODITY,
            parent_symbol=commodity.stock_symbol, tradingsymbol=commodity.stock_symbol
        ))
        registry.set_parent_object(commodity.stock_symbol, commodity)

    for token, gi in shared.app_ctx.global_indices_token_obj_dict.items():
        registry.register(TokenInfo(
            token=token, token_type=TokenType.GLOBAL_INDEX,
            parent_symbol=gi.stock_symbol, tradingsymbol=gi.stock_symbol
        ))
        registry.set_parent_object(gi.stock_symbol, gi)

    stats = registry.get_stats()
    logger.info(f"Token registry initialized: {stats}")


def _register_option_and_future_tokens(parent_symbol, options_df, futures_df, token_type_parent=TokenType.EQUITY):
    """Register option and futures instrument tokens for a symbol in the token registry."""
    registry = shared.app_ctx.token_registry
    if registry is None:
        return

    # Register option tokens
    if options_df is not None and not options_df.empty:
        for _, row in options_df.iterrows():
            registry.register(TokenInfo(
                token=int(row['instrument_token']),
                token_type=TokenType.OPTION,
                parent_symbol=parent_symbol,
                tradingsymbol=row['tradingsymbol'],
                strike=float(row['strike']),
                option_type=row['instrument_type'],  # CE or PE
                expiry=row['expiry'],
            ))

    # Register futures tokens
    if futures_df is not None and not futures_df.empty:
        for _, row in futures_df.iterrows():
            registry.register(TokenInfo(
                token=int(row['instrument_token']),
                token_type=TokenType.FUTURE,
                parent_symbol=parent_symbol,
                tradingsymbol=row['tradingsymbol'],
                expiry=row['expiry'],
            ))

    # Auto-detect strike gap for options
    if options_df is not None and not options_df.empty:
        strikes = sorted(options_df['strike'].unique())
        if len(strikes) >= 2:
            gap = strikes[1] - strikes[0]
            registry.set_strike_gap(parent_symbol, gap)
            logger.info(f"Strike gap for {parent_symbol}: {gap}")


def update_zerodha_option_chain(stockName = None, indexName = None):

    kc = KiteConnect(constant.DUMMY_API_KEY_ZERODHA)
    all_instruments_df = pd.DataFrame(kc.instruments())
    all_options_df = all_instruments_df[all_instruments_df['segment'].isin(['NFO-OPT', 'BFO-OPT'])]
    all_futures_df = all_instruments_df[all_instruments_df['segment'].isin(['NFO-FUT', 'BFO-FUT'])]
    count = 0
    for stock in shared.app_ctx.stock_token_obj_dict.values():
        if not PRODUCTION and constant.NO_OF_STOCKS != -1 and count >= constant.NO_OF_STOCKS:
            break
        if stockName and stock.stock_symbol != stockName:
            continue
        zerodha_ctx = stock.zerodha_ctx
        # Fetch options data for the stock
        stock_options = all_options_df[all_options_df['name'] == stock.stock_symbol]
        stock_options = stock_options[['instrument_token', 'tradingsymbol', 'expiry', 'strike', 'instrument_type']]
        # Fetch futures data for the stock
        stock_futures = all_futures_df[all_futures_df['name'] == stock.stock_symbol]
        stock_futures = stock_futures[['instrument_token', 'tradingsymbol', 'expiry', 'instrument_type']]

        expiry_dates = sorted(stock_options['expiry'].unique())
        if not expiry_dates:
            logger.warning(f"No expiry dates found for stock {stock.stock_symbol}")
            continue

        zerodha_ctx["option_chain"]["current"] = stock_options[stock_options['expiry'] == expiry_dates[0]]
        zerodha_ctx["futures_mdata"]["current"] = stock_futures[stock_futures['expiry'] == expiry_dates[0]]
        if len(expiry_dates) > 1:
            zerodha_ctx["option_chain"]["next"] = stock_options[stock_options['expiry'] == expiry_dates[1]]
            zerodha_ctx["futures_mdata"]["next"] = stock_futures[stock_futures['expiry'] == expiry_dates[1]]
        else:
            logger.info(f"stock {stock.stock_symbol} next expiry not available")
            zerodha_ctx["option_chain"]["next"] = pd.DataFrame()

        # Register option and futures tokens in the registry (current expiry only for live ticks)
        current_options = stock_options[stock_options['expiry'] == expiry_dates[0]]
        current_futures = stock_futures[stock_futures['expiry'] == expiry_dates[0]]
        _register_option_and_future_tokens(stock.stock_symbol, current_options, current_futures)

        logger.info(f"stock {stock.stock_symbol} zerodha_ctx updated")
        count += 1

    count = 0
    for index in  shared.app_ctx.index_token_obj_dict.values():
        if not PRODUCTION and constant.NO_OF_INDEX != -1 and count >= constant.NO_OF_INDEX:
            break
        if (index.stock_symbol in constant.INDEX_ANALYSIS_EXCLUDE) or (indexName and index.stock_symbol != indexName):
            continue

        zerodha_ctx = index.zerodha_ctx
        # Fetch options data for the index
        index_options = all_options_df[all_options_df['name'] == index.stock_symbol]
        index_options = index_options[['instrument_token', 'tradingsymbol', 'expiry', 'strike', 'instrument_type']]
        # Fetch futures data for the index
        index_futures = all_futures_df[all_futures_df['name'] == index.stock_symbol]
        index_futures = index_futures[['instrument_token', 'tradingsymbol', 'expiry', 'instrument_type']]

        expiry_dates = sorted(index_options['expiry'].unique())

        if not expiry_dates:
            logger.warning(f"No expiry dates found for index {index.stock_symbol}")
            continue

        # expiry_dates is already sorted; [0] = nearest weekly, [1] = next weekly
        # Previously this filtered to monthly only — now we keep weekly expiries
        # since weekly options have the highest liquidity and OI for intraday trading

        logger.info(f"Index {index.stock_symbol} available expiries: {expiry_dates}")
        logger.info(f"Index {index.stock_symbol} selected current expiry: {expiry_dates[0]}, "
                     f"options count: {len(index_options[index_options['expiry'] == expiry_dates[0]])}")

        zerodha_ctx["option_chain"]["current"] = index_options[index_options['expiry'] == expiry_dates[0]]
        if len(expiry_dates) > 1:
            zerodha_ctx["option_chain"]["next"] = index_options[index_options['expiry'] == expiry_dates[1]]
        else:
            logger.info(f"Index {index.stock_symbol} next expiry not available")
            zerodha_ctx["option_chain"]["next"] = pd.DataFrame()

        # Futures may have different expiry cadence than options (e.g. SENSEX: weekly options,
        # monthly futures) — use futures' own nearest expiry dates instead of option expiry dates.
        futures_expiry_dates = sorted(index_futures['expiry'].unique())
        if futures_expiry_dates:
            zerodha_ctx["futures_mdata"]["current"] = index_futures[index_futures['expiry'] == futures_expiry_dates[0]]
            zerodha_ctx["futures_mdata"]["next"] = index_futures[index_futures['expiry'] == futures_expiry_dates[1]] if len(futures_expiry_dates) > 1 else pd.DataFrame()
        else:
            zerodha_ctx["futures_mdata"]["current"] = pd.DataFrame()
            zerodha_ctx["futures_mdata"]["next"] = pd.DataFrame()
            logger.warning(f"No futures found for index {index.stock_symbol}")

        # Register option and futures tokens in the registry (current expiry only for live ticks)
        current_options = index_options[index_options['expiry'] == expiry_dates[0]]
        current_futures = index_futures[index_futures['expiry'] == expiry_dates[0]]
        _register_option_and_future_tokens(index.stock_symbol, current_options, current_futures, TokenType.INDEX)

        # Populate stockExpires from the first index that has valid expiry dates.
        # This seeds _get_current_expiry() used by futures tick routing.
        if not shared.app_ctx.stockExpires and expiry_dates:
            shared.app_ctx.stockExpires = [str(e) for e in expiry_dates]
            logger.info(f"stockExpires seeded from {index.stock_symbol}: {shared.app_ctx.stockExpires[:2]}")

        logger.info(f"Index {index.stock_symbol} zerodha_ctx updated")

    # Log final registry stats
    if shared.app_ctx.token_registry:
        stats = shared.app_ctx.token_registry.get_stats()
        logger.info(f"Token registry after option chain update: {stats}")


def create_stock_and_index_objects(stockName = None, indexName = None, commodityName = None, globalIndexName = None):
    def is_before_market_open():
        now = datetime.now()
        return now.weekday() < 5 and now.time() < time(9, 15)
    
    stock_list, index_list, commodity_list, global_indices_list = get_stock_objects_from_json()

    is_intraday = shared.app_ctx.mode and shared.app_ctx.mode.name == shared.Mode.INTRADAY.name

    count = 0
    yfinanceIndexSymbols = []
    for index in index_list:
        if not PRODUCTION and constant.NO_OF_INDEX != -1 and count >= constant.NO_OF_INDEX:
            break
        if indexName and index["tradingsymbol"] != indexName:
            continue
        
        yfinanceIndexSymbols.append(index["yfinancetradingsymbol"]) 

        ticker = Stock(index["name"], index["tradingsymbol"], yfinanceSymbol=index["yfinancetradingsymbol"], is_index=True)
        shared.app_ctx.index_token_obj_dict[index["instrument_token"]] = ticker
        shared.app_ctx.index_list.append(index["tradingsymbol"])
        count += 1

    count = 0
    yfinanceSymbols = []
    for stock in stock_list:
        if not PRODUCTION and constant.NO_OF_STOCKS != -1 and count >= constant.NO_OF_STOCKS:
            break
        if stockName and stock["tradingsymbol"] != stockName:
            continue
        yfinanceSymbols.append(stock["tradingsymbol"]+".NS") 
        ticker = Stock(stock["name"], stock["tradingsymbol"])
        shared.app_ctx.stock_token_obj_dict[stock["instrument_token"]] = ticker
        shared.app_ctx.stocks_list.append(stock["tradingsymbol"])
        count += 1
    
    # Create commodity objects
    count = 0
    yfinanceCommoditySymbols = []
    for commodity in commodity_list:
        if commodityName and commodity["tradingsymbol"] != commodityName:
            continue
        
        yfinanceCommoditySymbols.append(commodity["yfinancetradingsymbol"]) 

        ticker = Stock(commodity["name"], commodity["tradingsymbol"], yfinanceSymbol=commodity["yfinancetradingsymbol"], is_index=True)
        shared.app_ctx.commodity_token_obj_dict[commodity["instrument_token"]] = ticker
        shared.app_ctx.commodity_list.append(commodity["tradingsymbol"])
        count += 1

    # Create global indices objects
    count = 0
    yfinanceGlobalIndicesSymbols = []
    for global_index in global_indices_list:
        if globalIndexName and global_index["tradingsymbol"] != globalIndexName:
            continue
        
        yfinanceGlobalIndicesSymbols.append(global_index["yfinancetradingsymbol"]) 
        
        ticker = Stock(global_index["name"], global_index["tradingsymbol"], yfinanceSymbol=global_index["yfinancetradingsymbol"], is_index=True)
        shared.app_ctx.global_indices_token_obj_dict[global_index["instrument_token"]] = ticker
        shared.app_ctx.global_indices_list.append(global_index["tradingsymbol"])
        count += 1

    # Data is loaded from Redis by _load_initial_data_from_redis() after init.

def _re_emit_signals_from_analysis(stock, layer: Layer):
    """Re-emit signals from analysis dict to in-memory SignalBus.
    
    The analysis-engine worker's _emit_signals() no-ops because
    shared.app_ctx.signal_bus is None in the worker process. The monolith
    re-emits here so the Correlator/Narrator see the signals.
    """
    bus = shared.app_ctx.signal_bus
    if not bus:
        return
    for sentiment in ("BULLISH", "BEARISH"):
        direction = Direction[sentiment]
        for analysis_type in stock.analysis.get(sentiment, {}):
            weight = constant.ANALYSIS_WEIGHTS.get(
                analysis_type, constant.ANALYSIS_WEIGHTS.get("DEFAULT", 10)
            )
            bus.emit(Signal(
                symbol=stock.stock_symbol,
                direction=direction,
                source=analysis_type.lower(),
                layer=layer,
                strength=weight_to_strength(weight),
            ))


def _convert_stream_result(fields: dict, stock_obj) -> Tuple[MonitorResult, bool, Optional[str]]:
    """Convert a stream result dict to the standard tuple format + side effects.
    
    Side effects performed:
    - Updates stock_obj.analysis from result (for reporting/intelligence)
    - Adds to 52-week lists if applicable
    - Re-emits signals to in-memory SignalBus
    - Sends Telegram notification if trend found
    """
    result_str = fields.get("result", "ERROR")
    trend_found = fields.get("trend_found", "false").lower() == "true"
    message = fields.get("message", "")

    monitor_result = {
        "SUCCESS": MonitorResult.SUCCESS,
        "NO_DATA": MonitorResult.NO_DATA,
        "ERROR": MonitorResult.ERROR,
    }.get(result_str, MonitorResult.ERROR)

    analysis_json = fields.get("analysis_json", "{}")
    try:
        stock_obj.analysis = json.loads(analysis_json)
    except Exception:
        stock_obj.analysis = {"BULLISH": {}, "BEARISH": {}, "NEUTRAL": {}, "NoOfTrends": 0}

    is_52w_high = fields.get("is_52w_high", "false").lower() == "true"
    is_52w_low = fields.get("is_52w_low", "false").lower() == "true"
    if is_52w_high:
        shared.ticker_52_week_high_list.append(stock_obj)
    if is_52w_low:
        shared.ticker_52_week_low_list.append(stock_obj)

    if shared.app_ctx.signal_bus and trend_found:
        layer = (
            Layer.POSITIONAL
            if shared.app_ctx.mode == shared.Mode.POSITIONAL
            else Layer.INTRADAY
        )
        _re_emit_signals_from_analysis(stock_obj, layer)

    if trend_found and message:
        TELEGRAM_NOTIFICATIONS.send_notification(message, parse_mode="HTML")

    return (monitor_result, trend_found, message if trend_found else None)


def _time_to_next_5min_bar() -> int:
    """Seconds remaining until the next 5-minute bar boundary."""
    now = datetime.now()
    seconds_into_bar = now.second + (now.minute % 5) * 60
    return constant.INTRADAY_SLEEP_TIME - seconds_into_bar


def _analysis_collection_deadline(reporting_buffer: int = 15) -> float:
    """Compute the wall-clock deadline for collecting analysis results.

    Uses the time remaining until the next 5-min bar (intraday mode) or
    a generous fixed budget (positional mode, which runs once).

    Args:
        reporting_buffer: Seconds reserved for reporting + healthcheck after
                          collection completes.  Default 15s.

    Returns:
        Unix timestamp (time.time() + seconds) at which collection should stop.
    """
    if shared.app_ctx.mode == shared.Mode.POSITIONAL:
        return _time.time() + 180  # 3 min for positional (single run, no next-bar pressure)
    budget = _time_to_next_5min_bar() - reporting_buffer
    return _time.time() + max(60, budget)


def _publish_options_snapshot(index_objs: list) -> None:
    """Publish live options tick snapshot to Redis for GEX analyser in workers.

    For each index in LIVE_OPTIONS_INDICES, serialises the monolith's in-memory
    options_live (WS2 + Sensibull greeks) to a Redis hash so stateless analysis
    workers can run GEXAnalyser.  Uses DELETE + HSET to remove stale strikes
    from WS2 re-centering.
    """
    for idx in index_objs:
        if idx.stock_symbol not in constant.LIVE_OPTIONS_INDICES:
            continue
        ts = idx._tick_store
        if not ts.options_live:
            continue

        mapping = {}
        for strike, sides in ts.options_live.items():
            strike_key = str(float(strike))
            for opt_type in ("CE", "PE"):
                tick = sides.get(opt_type)
                if tick:
                    mapping[f"{strike_key}_{opt_type}"] = json.dumps(tick, default=str)

        if not mapping:
            continue

        key = f"data:options_live:{idx.stock_symbol}"
        redis_proxy.delete(key)
        redis_proxy.hset(key, mapping=mapping)

    n_indices = sum(1 for i in index_objs if i.stock_symbol in constant.LIVE_OPTIONS_INDICES)
    logger.debug(f"[stream] Published options_live snapshot for {n_indices} indices")


def _dispatch_and_collect_stream(
    stock_objs: list, index_objs: list
) -> List[Tuple[MonitorResult, bool, Optional[str]]]:
    """Dispatch analysis jobs to Redis Stream, collect results by cycle_id.

    Uses a dynamic deadline based on the next 5-min bar (intraday) or a
    generous fixed budget (positional).  Results from a different cycle_id
    (e.g. late stragglers from a previous cycle) are acked and discarded.
    """
    cycle_id = (
        f"{datetime.now().strftime('%Y-%m-%d')}-{shared.app_ctx.intraday_cycle_count}"
    )
    mode_str = (
        "positional"
        if shared.app_ctx.mode == shared.Mode.POSITIONAL
        else "intraday"
    )

    redis_proxy.hset("orchestrator:state", mapping={
        "mode": mode_str,
        "cycle_id": cycle_id,
        "last_cycle_time": str(_time.time()),
    })

    _publish_options_snapshot(index_objs)

    jobs = []
    for obj in index_objs + stock_objs:
        job_id = uuid.uuid4().hex[:8]
        jobs.append((job_id, obj))
        redis_proxy.xadd(constant.ANALYSIS_JOBS_STREAM, {
            "job_id": job_id,
            "cycle_id": cycle_id,
            "symbol": obj.stock_symbol,
            "is_index": str(obj.is_index).lower(),
            "mode": mode_str,
        }, maxlen=500)

    logger.info(f"[stream] Dispatched {len(jobs)} analysis jobs (cycle={cycle_id})")

    expected = len(jobs)
    job_ids = {jid for jid, _ in jobs}
    results_by_job = {}
    deadline = _analysis_collection_deadline()

    while len(results_by_job) < expected and _time.time() < deadline:
        remaining_ms = int((deadline - _time.time()) * 1000)
        block_ms = min(remaining_ms, 5000)
        if block_ms <= 0:
            break

        try:
            messages = redis_proxy.xreadgroup(
                constant.ANALYSIS_RESULTS_GROUP,
                "prod-1",
                {constant.ANALYSIS_RESULTS_STREAM: ">"},
                count=expected - len(results_by_job),
                block=block_ms,
            )
        except Exception as e:
            logger.error(f"[stream] xreadgroup error: {e}")
            sleep(1)
            continue

        if not messages:
            continue

        entries = messages[0][1] if isinstance(messages, list) and messages else []
        for msg_id, fields in entries:
            result_cycle = fields.get("cycle_id", "")
            if result_cycle != cycle_id:
                logger.debug(
                    f"[stream] Discarding stale result for {fields.get('symbol', '?')} "
                    f"(cycle={result_cycle}, current={cycle_id})"
                )
            else:
                jid = fields.get("job_id", "")
                if jid in job_ids:
                    results_by_job[jid] = fields
            try:
                redis_proxy.xack(constant.ANALYSIS_RESULTS_STREAM, constant.ANALYSIS_RESULTS_GROUP, msg_id)
            except Exception:
                pass

    results = []
    for job_id, obj in jobs:
        fields = results_by_job.get(job_id)
        if fields is None:
            logger.warning(f"[stream] No result for {obj.stock_symbol} (job={job_id}) — timeout")
            results.append((MonitorResult.ERROR, False, "stream_timeout"))
        else:
            results.append(_convert_stream_result(fields, obj))

    missing = expected - len(results_by_job)
    if missing > 0:
        logger.warning(f"[stream] {missing}/{expected} jobs timed out for cycle={cycle_id}")

    logger.info(f"[stream] Collected {len(results_by_job)}/{expected} results for cycle={cycle_id}")
    return results


def fetch_and_analyze_stocks() -> List[Tuple[MonitorResult, bool, Optional[str]]]:
    logger.info("Fetching and analyzing data for all stocks")

    stock_objs = list(shared.app_ctx.stock_token_obj_dict.values())
    index_objs = list(shared.app_ctx.index_token_obj_dict.values())
    commodity_objs = list(shared.app_ctx.commodity_token_obj_dict.values())
    global_indices_objs = list(shared.app_ctx.global_indices_token_obj_dict.values())

    load_price_data_from_redis(
        redis_proxy, stock_objs, index_objs,
        commodity_objs, global_indices_objs,
    )

    for obj in stock_objs + index_objs + commodity_objs + global_indices_objs:
        try:
            obj.update_latest_data()
        except Exception as e:
            logger.debug(f"[cycle] update_latest_data failed for {obj.stock_symbol}: {e}")

    n_with_ltp = sum(1 for o in index_objs if o.ltp is not None)
    logger.info(f"[cycle] update_latest_data: {n_with_ltp}/{len(index_objs)} indices have ltp")

    return _dispatch_and_collect_stream(stock_objs, index_objs)

def get_top_gainers_and_losers(stock_objs):
    """
    Returns the top 5 gainers and top 5 losers based on percentage change in stock prices.

    Args:
        stock_objs (list): List of Stock objects with price data.

    Returns:
        Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]: 
            - List of top 5 gainers as tuples of (stock symbol, percentage gain).
            - List of top 5 losers as tuples of (stock symbol, percentage loss).
    """
    gainers = []
    losers = []

    for _ , stock in stock_objs.items():
        try:
            if stock.ltp is None or stock.ltp_change_perc is None:
                continue
            if stock.ltp_change_perc > 0:
                gainers.append((stock.stock_symbol, stock.ltp_change_perc))
            else:
                losers.append((stock.stock_symbol, stock.ltp_change_perc))
        except Exception as e:
            logger.error(f"Error calculating percentage change for {stock.stock_symbol}: {e}")

    # Sort gainers and losers by percentage change
    gainers.sort(key=lambda x: x[1], reverse=True)
    losers.sort(key=lambda x: x[1])

    # Return top 5 gainers and top 5 losers
    return gainers[:5], losers[:5]

def generate_top_gainers_and_losers_report(gainers, losers):
    """
    Generates an HTML-formatted report with top gainers and top losers.
    """
    report = "\U0001F4C8 <b>Top Gainers</b>\n"
    for i, (stock, change_percent) in enumerate(gainers):
        report += f"  \U0001F7E2 {i+1}. <b>{stock}</b>: <code>{change_percent:+.2f}%</code>\n"

    report += "\n\U0001F4C9 <b>Top Losers</b>\n"
    for i, (stock, change_percent) in enumerate(losers):
        report += f"  \U0001F534 {i+1}. <b>{stock}</b>: <code>{change_percent:.2f}%</code>\n"

    return report

def report_top_gainers_and_losers():
    top_gainers , top_losers = get_top_gainers_and_losers(shared.app_ctx.stock_token_obj_dict)
    report = generate_top_gainers_and_losers_report(top_gainers, top_losers)
    TELEGRAM_NOTIFICATIONS.send_notification(report, parse_mode="HTML")
    logger.info(f"EOD Report\n {report}")
    return report

def report_index_data():
    logger.info("Reporting index data")
    report = "\U0001F3E6 <b>Index Report</b>\n"
    index_objs = list(shared.app_ctx.index_token_obj_dict.values())
    for index in index_objs:
        try:
            if index.ltp is None:
                continue
            if index.ltp_change_perc is not None:
                dot = "\U0001F7E2" if index.ltp_change_perc >= 0 else "\U0001F534"
                sign = "+" if index.ltp_change_perc >= 0 else ""
                report += f"  {dot} <b>{index.stock_symbol}</b>: <code>{index.ltp:.2f}</code> ({sign}{index.ltp_change_perc:.2f}%)\n"
            else:
                report += f"  <b>{index.stock_symbol}</b>: <code>{index.ltp:.2f}</code>\n"
        except Exception as e:
            logger.error(f"Error while getting index data for {index.stock_symbol}: {e}")
    TELEGRAM_NOTIFICATIONS.send_notification(report, parse_mode="HTML")
    logger.info(f"Index Report\n {report}")
    return report

def report_commodity_data():
    logger.info("Reporting commodity data")
    commodity_objs = list(shared.app_ctx.commodity_token_obj_dict.values())
    
    if not commodity_objs:
        return
    
    _COMMODITY_CURRENCY_SYMBOL = {"USDINR": "\u20B9"}
    _DEFAULT_CURRENCY_SYMBOL   = "$"

    report = "\U0001F6E2\uFE0F <b>Commodity Report</b>\n"
    for commodity in commodity_objs:
        try:
            if not commodity.is_price_data_empty():
                close_col = commodity.priceData['Close']

                # Handle MultiIndex columns from yfinance (e.g. Close > GOLD)
                if isinstance(close_col, pd.DataFrame):
                    close_col = close_col.iloc[:, 0]

                close_prices = close_col.dropna()
                if close_prices.empty:
                    logger.warning(f"No valid price data for {commodity.stock_symbol}")
                    continue

                current_price = close_prices.iloc[-1]

                if pd.isna(current_price) or not isinstance(current_price, (int, float)):
                    logger.warning(f"Invalid price for {commodity.stock_symbol}: {current_price} (type={type(current_price).__name__})")
                    continue

                prev_close = None
                if commodity.prevDayOHLCV and commodity.prevDayOHLCV.get("CLOSE") and pd.notna(commodity.prevDayOHLCV["CLOSE"]):
                    prev_close = commodity.prevDayOHLCV["CLOSE"]
                elif len(close_prices) >= 2:
                    prev_close = close_prices.iloc[-2]

                currency_sym = _COMMODITY_CURRENCY_SYMBOL.get(commodity.stock_symbol, _DEFAULT_CURRENCY_SYMBOL)
                if prev_close is not None and pd.notna(prev_close) and prev_close != 0:
                    change_percent = percentageChange(current_price, prev_close)
                    dot = "\U0001F7E2" if change_percent >= 0 else "\U0001F534"
                    report += f"  {dot} <b>{commodity.stock_symbol}</b>: <code>{currency_sym}{current_price:.2f}</code> ({change_percent:+.2f}%)\n"
                else:
                    report += f"  <b>{commodity.stock_symbol}</b>: <code>{currency_sym}{current_price:.2f}</code>\n"
        except Exception as e:
            logger.error(f"Error while getting commodity data for {commodity.stock_symbol}: {e}")
    
    TELEGRAM_NOTIFICATIONS.send_notification(report, parse_mode="HTML")
    logger.info(f"Commodity Report\n {report}")
    return report

def report_global_indices_data():
    logger.info("Reporting global indices data")
    global_indices_objs = list(shared.app_ctx.global_indices_token_obj_dict.values())
    
    if not global_indices_objs:
        return
    
    report = "\U0001F30D <b>Global Indices Report</b>\n"
    
    # Group by region
    usa_indices = []
    europe_indices = []
    asia_indices = []
    
    for global_index in global_indices_objs:
        symbol = global_index.stock_symbol
        if symbol in ["SPX", "DJI", "NASDAQ"]:
            usa_indices.append(global_index)
        elif symbol in ["FTSE", "DAX", "CAC40"]:
            europe_indices.append(global_index)
        else:
            asia_indices.append(global_index)
    
    region_icons = {"USA": "\U0001F1FA\U0001F1F8", "Europe": "\U0001F1EA\U0001F1FA", "Asia": "\U0001F30F"}

    def format_index_data(indices, region_name):
        icon = region_icons.get(region_name, "")
        region_report = f"\n{icon} <b>{region_name}</b>\n"
        for index in indices:
            try:
                if not index.is_price_data_empty():
                    close_col = index.priceData['Close']

                    # Handle MultiIndex columns from yfinance (e.g. Close > SPX)
                    if isinstance(close_col, pd.DataFrame):
                        close_col = close_col.iloc[:, 0]

                    close_prices = close_col.dropna()
                    if close_prices.empty:
                        logger.warning(f"No valid price data for {index.stock_symbol}")
                        continue
                    
                    current_price = close_prices.iloc[-1]
                    
                    if pd.isna(current_price) or not isinstance(current_price, (int, float)):
                        logger.warning(f"Invalid price for {index.stock_symbol}: {current_price} (type={type(current_price).__name__})")
                        continue
                    
                    prev_close = None
                    if index.prevDayOHLCV and index.prevDayOHLCV.get("CLOSE") and pd.notna(index.prevDayOHLCV["CLOSE"]):
                        prev_close = index.prevDayOHLCV["CLOSE"]
                    elif len(close_prices) >= 2:
                        prev_close = close_prices.iloc[-2]

                    if prev_close is not None and pd.notna(prev_close) and prev_close != 0:
                        change_percent = percentageChange(current_price, prev_close)
                        dot = "\U0001F7E2" if change_percent >= 0 else "\U0001F534"
                        region_report += f"  {dot} <b>{index.stock_symbol}</b>: <code>{current_price:.2f}</code> ({change_percent:+.2f}%)\n"
                    else:
                        region_report += f"  <b>{index.stock_symbol}</b>: <code>{current_price:.2f}</code>\n"
            except Exception as e:
                logger.error(f"Error while getting global index data for {index.stock_symbol}: {e}")
        return region_report
    
    if usa_indices:
        report += format_index_data(usa_indices, "USA")
    if europe_indices:
        report += format_index_data(europe_indices, "Europe")
    if asia_indices:
        report += format_index_data(asia_indices, "Asia")
    
    TELEGRAM_NOTIFICATIONS.send_notification(report, parse_mode="HTML")
    logger.info(f"Global Indices Report\n {report}")
    return report

def report_52_week_high_low(max_items: int = 40, clear_after: bool = False):
    """
    Report 52-week High / Low stocks.
    shared.ticker_52_week_high_list / low_list contain Stock objects.
    Uses stock.ltp and stock.ltp_change_perc directly (fallbacks if missing).
    """
    high_objs = shared.ticker_52_week_high_list
    low_objs  = shared.ticker_52_week_low_list

    if not high_objs and not low_objs:
        TELEGRAM_NOTIFICATIONS.send_notification("\U0001F4A5 <b>52W High/Low</b>: None today.", parse_mode="HTML")
        logger.info("52W High/Low: None today.")
        return

    def dedup(objs):
        seen = set()
        out = []
        for o in objs:
            if not o:
                continue
            sym = o.stock_symbol
            if sym not in seen:
                seen.add(sym)
                out.append(o)
        return out

    high_objs = dedup(high_objs)
    low_objs  = dedup(low_objs)

    def price_and_change(stk: 'Stock'):
        price = None
        chg = None
        try:
            price = float(stk.ltp) if stk.ltp is not None else None
        except Exception:
            price = None
        try:
            chg = float(stk.ltp_change_perc) if stk.ltp_change_perc is not None else None
        except Exception:
            chg = None

        # Fallbacks if missing
        if price is None and not stk.is_price_data_empty():
            try:
                price = float(stk.priceData['Close'].iloc[-1])
            except Exception:
                pass
        if chg is None and price is not None and stk.prevDayOHLCV and stk.prevDayOHLCV.get("CLOSE"):
            try:
                prev_close = float(stk.prevDayOHLCV["CLOSE"])
                if prev_close:
                    chg = percentageChange(price, prev_close)
            except Exception:
                pass
        return price, chg

    def build_section(title, icon, stocks, sort_desc=True):
        if not stocks:
            return f"{icon} <b>{title}</b> (0): None"
        rows = []
        for s in stocks:
            p, c = price_and_change(s)
            rows.append({"symbol": s.stock_symbol, "price": p, "chg": c})
        rows = [r for r in rows if r["price"] is not None]
        rows.sort(key=lambda r: (r["chg"] if r["chg"] is not None else (-1e9 if sort_desc else 1e9)),
                  reverse=sort_desc)

        display = rows[:max_items]
        extra = len(rows) - len(display)

        lines = [f"{icon} <b>{title}</b> ({len(rows)})"]
        for r in display:
            price_str = f"{r['price']:.2f}" if r['price'] is not None else "NA"
            if r['chg'] is not None:
                dot = "\U0001F7E2" if r['chg'] >= 0 else "\U0001F534"
                chg_str = f"{r['chg']:+.2f}%"
            else:
                dot = "\u26AA"
                chg_str = "NA"
            lines.append(f"  {dot} <b>{r['symbol']}</b>: <code>{price_str:>9}</code>  {chg_str}")
        if extra > 0:
            lines.append(f"  <i>... (+{extra} more)</i>")
        return "\n".join(lines)

    msg = (
        "\U0001F4A5 <b>52-Week High / Low</b>\n\n"
        + build_section("52W Highs", "\U0001F4C8", high_objs, sort_desc=True) + "\n\n"
        + build_section("52W Lows", "\U0001F4C9", low_objs, sort_desc=False)
    )

    TELEGRAM_NOTIFICATIONS.send_notification(msg, parse_mode="HTML")
    logger.info(msg)

    if clear_after:
        shared.ticker_52_week_high_list.clear()
        shared.ticker_52_week_low_list.clear()

    return msg

def _handle_confluence(confluence: Confluence):
    """Format and send a confluence alert to the live options Telegram channel."""
    layers_str = " + ".join(
        l.value.upper() for l in sorted(confluence.layers_involved, key=lambda l: l.value)
    )
    sources = "\n".join(
        f"  - {s.layer.value}: {s.source} ({s.strength.name})"
        for s in sorted(confluence.signals, key=lambda s: s.timestamp)
    )

    level = confluence.level
    caution = "\n  CAUTION: contradicting signals from other layers" if confluence.has_contradiction else ""

    msg = (
        f"{'[HIGH]' if level == 'HIGH' else '[MODERATE]'} "
        f"<b>{confluence.symbol} — {level} CONFLUENCE {confluence.direction.value}</b>\n\n"
        f"Layers: {layers_str}\n"
        f"Score: {confluence.score:.0f}\n\n"
        f"Signals:\n{sources}{caution}"
    )

    TELEGRAM_NOTIFICATIONS.send_live_options_notification(msg, parse_mode="HTML")
    logger.info(f"[Confluence] {confluence.symbol} {confluence.direction.value} "
                f"{level} ({confluence.layer_count} layers, score={confluence.score:.0f})")

    # Phase 2: async LLM narrative — HIGH confluences only (3+ layers aligned).
    # MODERATE (2-layer) confluences get the raw alert above but no LLM call,
    # which eliminates the bulk of morning-open notification flooding.
    if shared.app_ctx.narrator and confluence.level == "HIGH":
        shared.app_ctx.narrator.narrate_async(confluence)


def _init_signal_intelligence():
    """Set up the SignalBus and Correlator."""
    bus = SignalBus()
    correlator = SignalCorrelator(on_confluence=_handle_confluence)
    bus.subscribe(correlator.on_signal)

    shared.app_ctx.signal_bus = bus
    shared.app_ctx.correlator = correlator

    logger.info("Signal intelligence initialised (bus + correlator)")


_morning_bias_done = False


def _compute_daily_hv(stock) -> float | None:
    """
    Compute annualised Historical Volatility (%) from daily closes.
    Uses std(log_returns, 20-day window) × √252 × 100.

    Called once at morning bias while priceData still holds 1y daily bars.
    Returns HV as a percentage (e.g. 28.5), or None if insufficient data.
    """
    import numpy as np
    try:
        price_data = stock.priceData
        if price_data is None or price_data.empty:
            return None
        closes = price_data["Close"].dropna()
        # Need at least 21 rows for a 20-bar window (20 log returns)
        if len(closes) < 21:
            return None
        window = closes.iloc[-21:]
        log_returns = np.log(window / window.shift(1)).dropna()
        if len(log_returns) < 2:
            return None
        std = float(log_returns.std())
        if std == 0:
            return None
        return round(std * (252 ** 0.5) * 100, 2)
    except Exception as e:
        logger.warning(f"[HV] Failed to compute daily HV for {stock.stock_symbol}: {e}")
        return None


def _compute_daily_hv_all():
    """
    Compute and cache daily HV for all stocks and indices.
    Must be called while priceData still holds 1y daily bars (before the
    intraday loop overwrites it with 5m data).
    """
    all_stocks = list(shared.app_ctx.stock_token_obj_dict.values()) + [
        idx for idx in shared.app_ctx.index_token_obj_dict.values()
        if idx.stock_symbol not in constant.INDEX_ANALYSIS_EXCLUDE
    ]
    computed = 0
    for s in all_stocks:
        hv = _compute_daily_hv(s)
        s.daily_hv = hv
        if hv is not None:
            logger.debug(f"[HV] {s.stock_symbol} daily_hv={hv:.1f}%")
            computed += 1
        else:
            logger.debug(f"[HV] {s.stock_symbol} daily_hv=None (insufficient data)")
    logger.info(f"[HV] Daily HV cached for {computed}/{len(all_stocks)} symbols")


def compute_morning_bias():
    """
    Run positional analysers on daily data loaded by create_stock_and_index_objects().
    Emit results as POSITIONAL signals valid for the entire trading day.
    Only runs in INTRADAY mode — positional (8 PM) doesn't need morning bias.
    """
    global _morning_bias_done
    if _morning_bias_done:
        return
    _morning_bias_done = True

    # Skip for positional mode — no need for morning bias in the evening run
    if shared.app_ctx.mode and shared.app_ctx.mode.name == shared.Mode.POSITIONAL.name:
        logger.info("Morning bias skipped — positional mode")
        return

    bus = shared.app_ctx.signal_bus
    if not bus:
        return

    # Temporarily switch to POSITIONAL so analysers apply daily-data thresholds
    shared.app_ctx.mode = shared.Mode.POSITIONAL
    orchestrator.reset_all_constants()
    signals_emitted = []

    # Compute and cache daily HV for every stock and index while priceData
    # still holds 1y daily bars (before it is overwritten with 5m data in
    # the intraday loop). Stored on stock.daily_hv for use by IVAnalyser.
    _compute_daily_hv_all()

    # Read Sensibull data from Redis before running analysers.
    # compute_morning_bias() bypasses monitor(), so without this load
    # sensibull_ctx["current"]["stats"] stays None and IVAnalyser crashes.
    # Data-gateway's initial load already published positional-mode Sensibull data.

    try:
        # Indices
        for index in shared.app_ctx.index_token_obj_dict.values():
            if index.stock_symbol in constant.INDEX_ANALYSIS_EXCLUDE:
                continue
            if not load_sensibull_from_redis(redis_proxy, index):
                logger.warning(f"[MorningBias] No sensibull data in Redis for {index.stock_symbol}")
            orchestrator.run_all_positional(index, index=True)
            for sentiment in ("BULLISH", "BEARISH"):
                for analysis_type in index.analysis.get(sentiment, {}):
                    weight = constant.ANALYSIS_WEIGHTS.get(
                        analysis_type, constant.ANALYSIS_WEIGHTS.get("DEFAULT", 10)
                    )
                    signals_emitted.append(Signal(
                        symbol=index.stock_symbol,
                        direction=Direction[sentiment],
                        source=analysis_type.lower(),
                        layer=Layer.POSITIONAL,
                        strength=weight_to_strength(weight),
                    ))
            index.reset_analysis()

        # Equities
        for stock in shared.app_ctx.stock_token_obj_dict.values():
            if not load_sensibull_from_redis(redis_proxy, stock):
                logger.warning(f"[MorningBias] No sensibull data in Redis for {stock.stock_symbol}")
            orchestrator.run_all_positional(stock, index=False)
            for sentiment in ("BULLISH", "BEARISH"):
                for analysis_type in stock.analysis.get(sentiment, {}):
                    weight = constant.ANALYSIS_WEIGHTS.get(
                        analysis_type, constant.ANALYSIS_WEIGHTS.get("DEFAULT", 10)
                    )
                    signals_emitted.append(Signal(
                        symbol=stock.stock_symbol,
                        direction=Direction[sentiment],
                        source=analysis_type.lower(),
                        layer=Layer.POSITIONAL,
                        strength=weight_to_strength(weight),
                    ))
            stock.reset_analysis()
    finally:
        # Always restore INTRADAY mode
        shared.app_ctx.mode = shared.Mode.INTRADAY

    for sig in signals_emitted:
        bus.emit(sig)
    logger.info(f"Morning bias computed: {len(signals_emitted)} positional signals emitted to SignalBus")


def update_morning_bias():
    """Fast path: extract bias signals from 8 PM positional analysis results.

    Since the monolith is always running, stock.analysis is still populated
    from yesterday's 8 PM positional_analysis() run. Just extract the BULLISH/
    BEARISH results as POSITIONAL layer signals → SignalBus. Zero HTTP, zero
    re-computation. ~0.5s for 213 stocks.

    Slow path fallback: if analysis dict is empty (monolith restarted overnight),
    fall back to compute_morning_bias() which re-reads Redis and re-runs
    all positional analysers. ~120s with 43 HTTP calls.
    """
    bus = shared.app_ctx.signal_bus
    if not bus:
        return

    all_stocks = list(shared.app_ctx.stock_token_obj_dict.values()) + [
        idx for idx in shared.app_ctx.index_token_obj_dict.values()
        if idx.stock_symbol not in constant.INDEX_ANALYSIS_EXCLUDE
    ]

    has_analysis = any(
        s.analysis.get("BULLISH") or s.analysis.get("BEARISH")
        for s in all_stocks
    )

    if has_analysis:
        shared.app_ctx.mode = shared.Mode.POSITIONAL
        signals_emitted = []
        for stock in all_stocks:
            for sentiment in ("BULLISH", "BEARISH"):
                for analysis_type in stock.analysis.get(sentiment, {}):
                    weight = constant.ANALYSIS_WEIGHTS.get(
                        analysis_type, constant.ANALYSIS_WEIGHTS.get("DEFAULT", 10)
                    )
                    signals_emitted.append(Signal(
                        symbol=stock.stock_symbol,
                        direction=Direction[sentiment],
                        source=analysis_type.lower(),
                        layer=Layer.POSITIONAL,
                        strength=weight_to_strength(weight),
                    ))
        for sig in signals_emitted:
            bus.emit(sig)
        shared.app_ctx.mode = shared.Mode.INTRADAY
        logger.info(f"Morning bias (fast path): {len(signals_emitted)} signals from 8 PM analysis")
    else:
        logger.info("Morning bias (slow path): no 8 PM analysis found — recomputing from Redis")
        compute_morning_bias()


# ---------------------------------------------------------------------------
# Sensibull WS helpers — used when OPTIONS_SOURCE=sensibull
# ---------------------------------------------------------------------------

def _get_nearest_expiry_str(index_stock) -> str | None:
    """Return nearest expiry as 'YYYY-MM-DD' from already-populated zerodha_ctx."""
    try:
        chain_df = index_stock.zerodha_ctx.get("option_chain", {}).get("current")
        if chain_df is not None and not chain_df.empty:
            expiry = chain_df["expiry"].iloc[0]
            if hasattr(expiry, "strftime"):
                return expiry.strftime("%Y-%m-%d")
            return str(expiry)[:10]
    except Exception as exc:
        logger.warning(f"[Sensibull] could not resolve expiry for {index_stock.stock_symbol}: {exc}")
    return None


def _start_zerodha_ws() -> None:
    """Connect Zerodha dual WebSockets at 09:00 after auth refresh."""
    if not ENABLE_ZERODHA_API:
        logger.info("[ws] Zerodha API disabled — skipping WS connect")
        return
    tm = shared.app_ctx.zd_ticker_manager
    if tm is None:
        logger.warning("[ws] ZerodhaTickerManager not initialised")
        return
    if tm.connected:
        logger.info("[ws] Zerodha WS already connected")
        return
    enc_raw = os.getenv(constant.ENV_ZERODHA_ENC_TOKEN, "")
    if not enc_raw:
        logger.warning("[ws] ZERODHA_ENC_TOKEN not set — Zerodha WS not connected")
        return
    tm.update_enctoken(quote(enc_raw, safe=""))
    if tm.connect():
        logger.info("[ws] Zerodha WS1 (base) + WS2 (options) connected")
    else:
        logger.warning("[ws] Zerodha WS connect failed — use /enctoken via Telegram bot to retry")


def _stop_zerodha_ws() -> None:
    """Disconnect Zerodha WS after market close."""
    tm = shared.app_ctx.zd_ticker_manager
    if tm is None or not tm.connected:
        return
    try:
        tm.close_connection()
        tm.stop_tick_processor()
        logger.info("[ws] Zerodha WS1 + WS2 disconnected after market close")
    except Exception as e:
        logger.warning(f"[ws] Zerodha WS disconnect error: {e}")


def _start_sensibull_feed(live_options_engine, enrichment_only: bool = False) -> None:
    """
    Create one SensibullFeed per supported symbol and start them.
    The underlying token is read directly from index_token_obj_dict — the dict
    key IS the NSE/BSE instrument token, which Sensibull also uses as the
    underlying identifier. No separate token mapping is needed.
    Expiry is resolved from the zerodha_ctx already populated at startup.
    Feeds are stored in ``shared.app_ctx.sensibull_feed``.

    Args:
        enrichment_only: When True (OPTIONS_SOURCE=both), Sensibull only writes
                         Greeks/IV fields to existing Zerodha-subscribed strikes
                         and does NOT trigger on_aggregate_updated. Zerodha is
                         the authoritative tick source and engine trigger.
    """
    from fno.sensibull_feed import SensibullFeed
    from fno.sensibull_adapter import SensibullAdapter
    from notification.Notification import TELEGRAM_NOTIFICATIONS

    adapter = SensibullAdapter()
    feeds: list[SensibullFeed] = []
    started_symbols: list[str] = []

    for underlying_token, index_stock in shared.app_ctx.index_token_obj_dict.items():
        symbol = index_stock.stock_symbol
        if symbol not in constant.LIVE_OPTIONS_INDICES:
            continue

        expiry = _get_nearest_expiry_str(index_stock)
        if not expiry:
            # Allow manual override as a last resort
            expiry = os.getenv(f"SENSIBULL_EXPIRY_{symbol}", "")
        if not expiry:
            logger.error(
                f"[Sensibull] cannot resolve expiry for {symbol}. "
                f"Set SENSIBULL_EXPIRY_{symbol}=YYYY-MM-DD or ensure zerodha option chain is loaded."
            )
            continue

        subscriptions = [{"underlying": underlying_token, "expiry": expiry}]
        captured_stock = index_stock  # explicit closure capture

        def make_callback(stock):
            def _on_snapshot(token: int, data: dict) -> None:
                try:
                    adapter.apply(stock, data, live_options_engine,
                                  enrichment_only=enrichment_only)
                except Exception as exc:
                    logger.error(f"[Sensibull] snapshot error for {stock.stock_symbol}: {exc}")
            return _on_snapshot

        feed = SensibullFeed(subscriptions, on_snapshot=make_callback(captured_stock))
        feed.start()
        feeds.append(feed)
        started_symbols.append(symbol)
        logger.info(f"[Sensibull] feed started for {symbol} (underlying_token={underlying_token}, expiry={expiry}, enrichment_only={enrichment_only})")

    if feeds:
        shared.app_ctx.sensibull_feed = feeds
        mode_label = "Enrichment" if enrichment_only else "Primary"
        TELEGRAM_NOTIFICATIONS.send_live_options_notification(
            f"📡 <b>Sensibull Live Feed Started [{mode_label}]</b> — {', '.join(started_symbols)}"
        )
        logger.info(f"[Sensibull] {len(feeds)} feed(s) running [{mode_label}]: {started_symbols}")
    else:
        logger.error("[Sensibull] no feeds started — check symbol/expiry configuration")


def _stop_sensibull_feed() -> None:
    """Stop all Sensibull WebSocket feeds cleanly after market close."""
    feeds = shared.app_ctx.sensibull_feed
    if not feeds:
        return
    feeds_list = feeds if isinstance(feeds, list) else [feeds]
    for feed in feeds_list:
        try:
            feed.stop()
        except Exception as e:
            logger.warning(f"[Sensibull] stop error: {e}")
    shared.app_ctx.sensibull_feed = None
    logger.info(f"[Sensibull] {len(feeds_list)} feed(s) stopped")


def live_options_analysis():
    """
    Live options only mode — WebSocket + LiveOptionsEngine, no regular analysis.
    Auto-connects WebSocket using the encToken already in the environment.
    Runs until market close (or indefinitely in dev mode).
    """
    from notification.Notification import TELEGRAM_NOTIFICATIONS as TG
    from urllib.parse import quote as _quote

    tm = shared.app_ctx.zd_ticker_manager
    if tm is None:
        logger.error("ZerodhaTickerManager not initialised — cannot start live options analysis")
        return

    # Auto-connect if not already connected
    if not tm.connected:
        enc_raw = os.getenv(constant.ENV_ZERODHA_ENC_TOKEN, "")
        if enc_raw:
            tm.update_enctoken(_quote(enc_raw, safe=""))
            if tm.connect():
                logger.info("Live options WebSocket connected")
                from time import sleep as _sleep
                _sleep(3)   # wait for first index ticks
                if shared.app_ctx.options_source in ("sensibull", "both"):
                    _enrichment_only = (shared.app_ctx.options_source == "both")
                    _start_sensibull_feed(tm.live_options_engine, enrichment_only=_enrichment_only)
                else:
                    tm.subscribe_live_options(wait_for_ticks=True)
            else:
                logger.error("WebSocket connect failed — live options analysis aborted")
                return
        else:
            logger.error("ZERODHA_ENC_TOKEN not set — cannot auto-connect")
            return

    TG.send_live_options_notification("🟢 <b>Live Options Tracking Started</b>")
    logger.info("Live options analysis running — WebSocket handling alerts")

    while isNowInTimePeriod(time(9, 15), time(15, 30), datetime.now().time()) or not PRODUCTION:
        sleep(30)

    TG.send_live_options_notification("🔴 <b>Live Options Tracking Stopped — Market Closed</b>")
    logger.info("Live options analysis stopped — market closed")


def intraday_analysis(loop = True, loop_wait_time = 30, max_cycles = 0):
    """
    Run intraday analysis loop.

    Args:
        loop:           Dev mode only — False = single cycle then stop.
        loop_wait_time: Dev mode sleep between cycles (seconds).
                        -1 = use the same production wait logic (align to next 5-min bar).
                        Controlled by DEV_LOOP_WAIT_TIME env var.
        max_cycles:     Dev mode only — stop after this many cycles.
                        0 = unlimited. Controlled by DEV_MAX_CYCLES env var.
    """
    shared.app_ctx.mode = shared.Mode.INTRADAY

    # Sensibull feed is normally started at 09:00 in _run_daily_loop().
    # In dev mode (no _run_daily_loop), start it here as a fallback.
    _options_source = shared.app_ctx.options_source
    if (ENABLE_LIVE_OPTIONS
            and _options_source in ("sensibull", "both")
            and not shared.app_ctx.sensibull_feed):
        tm = shared.app_ctx.zd_ticker_manager
        if tm and tm.live_options_engine:
            _enrichment_only = (_options_source == "both")
            logger.info(f"[intraday] fallback — starting Sensibull feed (enrichment_only={_enrichment_only})")
            _start_sensibull_feed(tm.live_options_engine, enrichment_only=_enrichment_only)
        else:
            logger.warning("[intraday] Cannot start Sensibull feed — LiveOptionsEngine not initialised")

    logger.info("Market time open. Starting Intraday analysis")

    TELEGRAM_NOTIFICATIONS.send_notification("\U0001F4C8 <b>Intraday Analysis Started</b> \U0001F4C8", parse_mode="HTML")

    is_in_time_period = isNowInTimePeriod(time(9,15), time(15,30), datetime.now().time())
    cycle = 0
    _live_options_subscribed = False  # guard: subscribe option tokens once after first tick

    while(is_in_time_period or not PRODUCTION):
        cycle += 1
        shared.app_ctx.intraday_cycle_count = cycle
        shared.app_ctx.last_cycle_time = _time.time()
        logger.info("current iteration time : {}  cycle={}{}".format(
            datetime.now(), cycle,
            f"/{max_cycles}" if max_cycles > 0 else "",
        ))

        # Wait for data-gateway to publish current cycle's data
        if not _wait_for_cycle_ready():
            logger.warning("[cycle] Cycle signal timeout — using last cycle's data")

        try:
            results = fetch_and_analyze_stocks()
            process_monitor_results(results)
        except Exception as e:
            logger.error(f"Critical error in stock analysis: {e}")

        try:
            import psutil
            import os as _os
            _proc = psutil.Process(_os.getpid())
            _rss_mb = _proc.memory_info().rss / 1024 / 1024
            logger.info(f"[memory] cycle={cycle} RSS={_rss_mb:.1f} MB")
        except Exception:
            pass

        # After the first cycle, spot prices are available — subscribe Zerodha option tokens.
        # Runs once only; skipped if already subscribed or if Zerodha WS is not connected.
        if (not _live_options_subscribed
                and ENABLE_LIVE_OPTIONS
                and _options_source in ("zerodha", "both")):
            tm = shared.app_ctx.zd_ticker_manager
            if tm and tm.connected:
                logger.info("[intraday] subscribing Zerodha option tokens after first cycle")
                tm.subscribe_live_options(wait_for_ticks=False)
                _live_options_subscribed = True
            else:
                logger.debug("[intraday] Zerodha WS not connected — option subscription deferred")

        report_top_gainers_and_losers()
        report_index_data()
        report_commodity_data()
        report_global_indices_data()

        # Layer 3: Zombie data watchdog — check live options freshness
        if ENABLE_LIVE_OPTIONS:
            for idx_obj in shared.app_ctx.index_token_obj_dict.values():
                check_data_freshness(idx_obj)

        for stock in shared.app_ctx.stock_token_obj_dict:
            shared.app_ctx.stock_token_obj_dict[stock].reset_price_data()

        # Layer 2: Heartbeat — signal liveness to healthchecks.io
        _ping_healthcheck()

        if PRODUCTION:
            sleeptime = (constant.INTRADAY_SLEEP_TIME) - (datetime.now().second + ((datetime.now().minute % 5) * 60))
            logger.info("sleeping for {} sec".format(sleeptime))
            sleep(sleeptime)

            is_in_time_period = isNowInTimePeriod(time(9,15), time(15,30), datetime.now().time())
        else:
            if not loop:
                break
            if max_cycles > 0 and cycle >= max_cycles:
                logger.info(f"Dev mode: reached max_cycles={max_cycles}, stopping.")
                break
            if loop_wait_time == -1:
                # Mirror production wait: align to next 5-min bar
                sleeptime = (constant.INTRADAY_SLEEP_TIME) - (datetime.now().second + ((datetime.now().minute % 5) * 60))
                logger.info(f"Dev mode: production-style wait — sleeping {sleeptime}s to next 5-min bar")
                sleep(sleeptime)
            else:
                logger.info("Sleeping for {} sec in dev mode".format(loop_wait_time))
                sleep(loop_wait_time)
            is_in_time_period = True  # In dev mode, keep looping

    logger.info("Market time closed")


def positional_analysis():
    shared.app_ctx.mode = shared.Mode.POSITIONAL
    if PRODUCTION:
        if datetime.now().time() > time(16,0):
            logger.info("Market time closed")
        else:
            logger.info("Sleeping till 4:00 PM to start EOD analysis")
            now = datetime.now()
            new_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
            time_to_sleep = new_time - now
            logger.info("Sleeping for {} sec".format(time_to_sleep.total_seconds()))
            sleep(time_to_sleep.total_seconds())
    
    logger.info("EOD analysis Started")
    TELEGRAM_NOTIFICATIONS.send_notification("\U0001F4CA <b>EOD Analysis Started</b> \U0001F4CA", parse_mode="HTML")
    orchestrator.reset_all_constants()
    stock_alerts = []

    # Wait for data-gateway to publish current cycle's data
    if not _wait_for_cycle_ready():
        logger.warning("[cycle] Cycle signal timeout — using last cycle's data")

    try:
        results = fetch_and_analyze_stocks()
        process_monitor_results(results)
        # Collect alert messages for narrator
        for _, trend_found, message in results:
            if trend_found and message:
                stock_alerts.append(message)
    except Exception as e:
        logger.error(f"Critical error in stock analysis: {e}")

    movers_report = report_top_gainers_and_losers()
    index_report = report_index_data()
    commodity_report = report_commodity_data()
    global_report = report_global_indices_data()
    week52_report = report_52_week_high_low()

    # Post-market flows (sector, FII/DII)
    post_market_msgs = []
    if ENABLE_POST_MARKET:
        try:
            post_market_msgs = run_and_summarize() or []
            for msg in post_market_msgs:
                TELEGRAM_NOTIFICATIONS.send_notification(msg, parse_mode="HTML")
                logger.info(msg)
        except Exception as e:
            logger.error(f"Post-market pipeline failed: {e}")

    # LLM-powered EOD market briefing
    if shared.app_ctx.narrator:
        try:
            sector_report = ""
            fii_dii_report = ""
            for msg in post_market_msgs:
                if "FII" in msg or "DII" in msg or "Participant" in msg:
                    fii_dii_report += msg + "\n"
                else:
                    sector_report += msg + "\n"

            shared.app_ctx.narrator.narrate_positional({
                "stock_alerts": "\n\n".join(stock_alerts),
                "index_report": index_report or "",
                "commodity_report": commodity_report or "",
                "global_report": global_report or "",
                "week52_report": week52_report or "",
                "movers_summary": movers_report or "",
                "sector_report": sector_report,
                "fii_dii_report": fii_dii_report,
            })
        except Exception as e:
            logger.error(f"EOD narrative failed: {e}")

    for stock in shared.app_ctx.stock_token_obj_dict:
        shared.app_ctx.stock_token_obj_dict[stock].reset_price_data()

    logger.info("EOD analysis completed.")

def init():
    load_dotenv()
    global orchestrator
    global PRODUCTION
    global ENABLE_ZERODHA_DERIVATIVES
    global ENABLE_ZERODHA_API
    global ENABLE_TELEGRAM_BOT
    global ENABLE_POST_MARKET
    global ENABLE_LIVE_OPTIONS
    global LIVE_OPTIONS_ONLY
    global ENABLE_INTELLIGENCE
    global DEV_NOTIFY
    global redis_proxy
    global cycle_subscriber


    if os.getenv(constant.ENV_PRODUCTION, "0") == "1":
        logger.info("Running in production mode")
        PRODUCTION = True
        TELEGRAM_NOTIFICATIONS.is_production = True
    else:
        logger.info("Running in development mode")
        PRODUCTION = False
        if os.getenv(constant.ENV_DEV_NOTIFY, "0") == "1":
            DEV_NOTIFY = True
            TELEGRAM_NOTIFICATIONS.dev_notify = True
            logger.info("DEV_NOTIFY=1 — Telegram alerts enabled in dev mode")

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis_proxy = RedisProxy(redis_url)
    try:
        redis_proxy.get("ping")
        logger.info(f"[cycle] Connected to Redis at {redis_url}")
    except Exception as e:
        logger.error(f"[cycle] Cannot connect to Redis at {redis_url}: {e}")
        sys.exit(1)

    cycle_subscriber = CycleSubscriber(redis_proxy)
    cycle_subscriber.start()

    _start_auth_commands_consumer()

    try:
        redis_proxy.xgroup_create(
            constant.ANALYSIS_RESULTS_GROUP,
            constant.ANALYSIS_RESULTS_STREAM,
            mkstream=True,
        )
        logger.info(f"[stream] Created consumer group '{constant.ANALYSIS_RESULTS_GROUP}' on {constant.ANALYSIS_RESULTS_STREAM}")
    except Exception:
        pass  # Group already exists

    if os.getenv(constant.ENV_ENABLE_ZERODHA_DERIVATIVES, "0") == "1":
        logger.info("Zerodha Derivative analysis enabled")
        ENABLE_ZERODHA_DERIVATIVES = True
    else:
        logger.info("Zerodha Derivative analysis disabled")
        ENABLE_ZERODHA_DERIVATIVES = False
    
    if os.getenv(constant.ENV_ENABLE_ZERODHA_API, "0") == "1":
        logger.info(" Zerodha analysis enabled")
        ENABLE_ZERODHA_API = True
    else:
        logger.info(" Zerodha analysis disabled")
        ENABLE_ZERODHA_API = False
    
    if os.getenv(constant.ENV_ENABLE_TELEGRAM_BOT, "0") == "1":
        logger.info(" Telegram Bot enabled")
        ENABLE_TELEGRAM_BOT = True
    else:
        logger.info(" Telegram Bot disabled")
        ENABLE_TELEGRAM_BOT = False
    
    if os.getenv(constant.ENV_ENABLE_POST_MARKET, "0") == "1":
        ENABLE_POST_MARKET = True
    else:
        logger.info(" Post market analysis disabled")
        ENABLE_POST_MARKET = False

    if os.getenv(constant.ENV_ENABLE_LIVE_OPTIONS, "0") == "1":
        logger.info("Live options real-time analysis enabled")
        ENABLE_LIVE_OPTIONS = True
    else:
        logger.info("Live options real-time analysis disabled")
        ENABLE_LIVE_OPTIONS = False

    if os.getenv(constant.ENV_LIVE_OPTIONS_ONLY, "0") == "1":
        logger.info("LIVE OPTIONS ONLY mode — all regular analysis disabled")
        LIVE_OPTIONS_ONLY   = True
        ENABLE_LIVE_OPTIONS = True   # implies live options engine must be active
    else:
        LIVE_OPTIONS_ONLY = False

    if os.getenv(constant.ENV_ENABLE_INTELLIGENCE, "0") == "1":
        logger.info("Intelligence layer enabled (SignalBus + Correlator + morning bias)")
        ENABLE_INTELLIGENCE = True
    else:
        logger.info("Intelligence layer disabled")
        ENABLE_INTELLIGENCE = False

    if PRODUCTION:
        if datetime.now().time() < time(9,15) or isNowInTimePeriod(time(9,15), time(15,30), datetime.now().time()):
            shared.app_ctx.mode = shared.Mode.INTRADAY
        else:
            shared.app_ctx.mode = shared.Mode.POSITIONAL
    else:
        if os.getenv(constant.ENV_DEV_POSITIONAL, "0") == "1":
            logger.info("Positional analysis enabled")
            shared.app_ctx.mode = shared.Mode.POSITIONAL
        
        if os.getenv(constant.ENV_DEV_INTRADAY, "0") == "1":
            logger.info("Intraday analysis enabled")
            shared.app_ctx.mode = shared.Mode.INTRADAY
    
    if shared.app_ctx.mode == shared.Mode.INTRADAY:
        TELEGRAM_NOTIFICATIONS.is_intraday = True
    else:
        TELEGRAM_NOTIFICATIONS.is_intraday = False

    args = parse_arguments()

    # Initialize token registry
    shared.app_ctx.token_registry = TokenRegistry()

    # Initialize intelligence layer (SignalBus + Correlator)
    if ENABLE_INTELLIGENCE:
        _init_signal_intelligence()

        # Initialize LLM narrator (requires ENABLE_NARRATOR=1 + GEMINI_API_KEY)
        if os.getenv(constant.ENV_ENABLE_NARRATOR, "0") == "1":
            from intelligence.llm_client import GeminiClient
            from intelligence.context_builder import ContextBuilder
            from intelligence.narrator import MarketNarrator

            gemini = GeminiClient()
            if gemini.available:
                shared.app_ctx.narrator = MarketNarrator(gemini, ContextBuilder())
                logger.info("MarketNarrator initialised (Gemini Flash)")
            else:
                logger.warning("ENABLE_NARRATOR=1 but GEMINI_API_KEY not set — narrator disabled")

    create_stock_and_index_objects(args.stock, args.index)

    # Wait for data-gateway's initial cycle signal via stream catch-up
    cycle_subscriber.catch_up_on_startup(timeout=120)
    _load_initial_data_from_redis()

    # Register equity and index tokens in the registry
    _register_base_tokens()

    if ENABLE_ZERODHA_DERIVATIVES or LIVE_OPTIONS_ONLY:
        update_zerodha_option_chain(args.stock, args.index)
    orchestrator = AnalyserOrchestrator()
    if not LIVE_OPTIONS_ONLY:
        orchestrator.register(VolumeAnalyser())
        orchestrator.register(TechnicalAnalyser())
        orchestrator.register(CandleStickAnalyser())
        orchestrator.register(IVAnalyser())
        orchestrator.register(FuturesAnalyser())
        orchestrator.register(PCRAnalyser())
        orchestrator.register(MaxPainAnalyser())
        orchestrator.register(OIChainAnalyser())
        orchestrator.register(GEXAnalyser())        # After OIChainAnalyser; before composite
        orchestrator.register(PanicModeAnalyser())
        orchestrator.register(OptionSellerCompositeAnalyser())  # MUST be last -- reads PANIC_EXHAUSTION
    if ENABLE_ZERODHA_API:
        logger.info("Zerodha API enabled")
        userName = os.getenv(constant.ENV_ZERODHA_USERNAME)
        password = os.getenv(constant.ENV_ZERODHA_PASSWORD)
        encToken_raw = os.getenv(constant.ENV_ZERODHA_ENC_TOKEN)

        # URL-encode the encToken for ZerodhaTickerManager
        encToken_for_manager = quote(encToken_raw or "", safe="")
        shared.app_ctx.zd_ticker_manager = ZerodhaTickerManager(userName, password, encToken_for_manager)
        shared.app_ctx.zd_kc = KiteConnect(constant.DUMMY_API_KEY_ZERODHA, root="https://kite.zerodha.com/", enctoken=encToken_raw)

        if ENABLE_LIVE_OPTIONS:
            shared.app_ctx.zd_ticker_manager.live_options_engine = LiveOptionsEngine()
            logger.info("LiveOptionsEngine attached to ZerodhaTickerManager")
            options_source = os.getenv(constant.ENV_OPTIONS_SOURCE, "zerodha").lower()
            shared.app_ctx.options_source = options_source
            logger.info(f"OPTIONS_SOURCE={options_source}")

        # Attach LiveStockEngine for per-tick equity analysis (VWAP cross, ORB, etc.)
        if ENABLE_INTELLIGENCE and shared.app_ctx.signal_bus:
            shared.app_ctx.zd_ticker_manager.live_stock_engine = LiveStockEngine(shared.app_ctx.signal_bus)
            logger.info("LiveStockEngine attached to ZerodhaTickerManager")

        if LIVE_OPTIONS_ONLY:
            shared.app_ctx.zd_ticker_manager.index_only_mode = True
            logger.info("index_only_mode enabled — equity stocks excluded from WebSocket")

        # Auto-connect Zerodha WebSocket in dev mode.
        # In production mode, WS connects at 09:00 in _run_daily_loop()
        # after _refresh_zerodha_auth() so the enctoken is fresh.
        if not PRODUCTION and ENABLE_LIVE_OPTIONS and shared.app_ctx.options_source in ("zerodha", "both"):
            enc_raw = os.getenv(constant.ENV_ZERODHA_ENC_TOKEN, "")
            if enc_raw:
                logger.info("[init] Dev mode — auto-connecting Zerodha WebSocket")
                shared.app_ctx.zd_ticker_manager.update_enctoken(quote(enc_raw, safe=""))
                if shared.app_ctx.zd_ticker_manager.connect():
                    logger.info("[init] Zerodha WebSocket connected (dev mode)")
                else:
                    logger.warning("[init] Zerodha WebSocket auto-connect failed — use /enctoken via Telegram bot to retry")
            else:
                logger.warning("[init] ZERODHA_ENC_TOKEN not set — Zerodha WebSocket not connected")


def parse_arguments():
    parser = argparse.ArgumentParser(description="Stock Analysis Tool")
    parser.add_argument("--stock", type=str, help="Name of the stock to analyze (optional)")
    parser.add_argument("--index", type=str, help="Name of the index to analyze (optional)")
    parser.add_argument(
        "--premarket",
        action="store_true",
        help="Dev mode only: run the pre-market report (global cues + pre-open) and exit.",
    )
    return parser.parse_args()

# ═══════════════════════════════════════════════════════════════════════════
# Always-running loop helpers
# ═══════════════════════════════════════════════════════════════════════════

def _wait_until(hour: int, minute: int):
    """Sleep until specified time today. Returns immediately if already past."""
    while True:
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        sleep_sec = (target - now).total_seconds()
        if sleep_sec <= 0:
            return
        chunk = min(sleep_sec, 60)
        logger.debug(f"[schedule] Waiting until {hour:02d}:{minute:02d} ({sleep_sec:.0f}s remaining)")
        sleep(chunk)


def _sleep_until_midnight():
    """Sleep until midnight. Re-checks every 5 min (allows holiday detection)."""
    while True:
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        sleep_sec = (midnight - now).total_seconds()
        if sleep_sec <= 0:
            break
        sleep(min(sleep_sec, 300))
 

def _start_auth_commands_consumer():
    """Background thread consuming auth:commands stream for reactive enctoken refresh.

    The data-gateway publishes 'refresh_enctoken' commands when it gets 403/Bad Request
    from Zerodha. This consumer calls _refresh_zerodha_auth() and publishes the new
    token to Redis, which the data-gateway picks up via Pub/Sub.
    """
    import threading as _th
    from time import sleep as _sleep

    def _consume():
        stream = "auth:commands"
        group = "monolith"
        consumer = "auth-consumer-1"
        try:
            redis_proxy.xgroup_create(group, stream, mkstream=True)
        except Exception:
            pass
        logger.info("[auth] Started auth:commands consumer thread")
        while True:
            try:
                messages = redis_proxy.xreadgroup(group, consumer, {stream: ">"}, count=1, block=10000)
                if not messages:
                    continue
                entries = messages[0][1] if isinstance(messages, list) and messages else []
                for msg_id, fields in entries:
                    command = fields.get("command", "")
                    if command == "refresh_enctoken":
                        reason = fields.get("reason", "unknown")
                        logger.info(f"[auth] Received refresh_enctoken command (reason={reason})")
                        _refresh_zerodha_auth()
                    try:
                        redis_proxy.xack(stream, group, msg_id)
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"[auth] Consumer error: {e}")
                _sleep(5)

    t = _th.Thread(target=_consume, daemon=True, name="auth-commands-consumer")
    t.start()


def _refresh_zerodha_auth():
    """Refresh Zerodha enctoken via TOTP and publish to Redis for data-gateway."""
    if not ENABLE_ZERODHA_API:
        return
    try:
        from auth.auth_login import generate_enctoken
        success = generate_enctoken()
        if success:
            load_dotenv(override=True)
            encToken_raw = os.getenv(constant.ENV_ZERODHA_ENC_TOKEN)
            shared.app_ctx.zd_kc.update_enctoken(encToken_raw)

            # Publish enctoken to Redis for data-gateway
            now_ts = datetime.now().timestamp()
            redis_proxy.hset("auth:zerodha", mapping={
                "enctoken": encToken_raw,
                "issued_at": str(now_ts),
                "user_id": os.getenv("ZERODHA_USER", ""),
            })
            redis_proxy.publish("auth:enctoken_refreshed", f"issued_at={int(now_ts)}")
            logger.info("Zerodha auth refreshed — enctoken published to Redis")
        else:
            logger.error("Zerodha auth refresh failed")
    except Exception as e:
        logger.error(f"Zerodha auth refresh error: {e}")


def _run_daily_loop():
    """Always-running main loop. Self-schedules pre-market, intraday, positional.

    Production mode only — dev mode uses start_stock_analysis() as before.
    """
    global _morning_bias_done

    while True:
        # ── Holiday check ──
        if PRODUCTION:
            if not is_trading_day():
                today_str = datetime.now().date().strftime("%A, %d %b %Y")
                logger.info("Today (%s) is not an NSE trading day. Sleeping until midnight.", today_str)
                TELEGRAM_NOTIFICATIONS.is_intraday = False
                TELEGRAM_NOTIFICATIONS.send_notification(
                    "\U0001F4C5 <b>Market Holiday</b>\n\n"
                    f"Today is <b>{today_str}</b> and NSE is <b>closed</b>.\n"
                    "No analysis will run. Sleeping until next trading day.\U0001F3D6\uFE0F",
                    parse_mode="HTML",
                )
                _sleep_until_midnight()
                continue

        # ── Wait for pre-market time ──
        _wait_until(9, 0)
        logger.info("=== Pre-market phase ===")

        # Refresh auth (was separate systemd oneshot)
        _refresh_zerodha_auth()

        # ── 09:00 — Connect Zerodha dual WebSockets ──
        # WS1 (base) for equity + index ticks.
        # WS2 (options) for option ticks (no 500-instrument limit).
        # Both use the fresh enctoken from _refresh_zerodha_auth().
        _start_zerodha_ws()

        # ── 09:00 — Start Sensibull live option chain feed ──
        # 15-min buffer before market opens so connection issues are
        # discovered early and can retry before intraday starts.
        if ENABLE_LIVE_OPTIONS and shared.app_ctx.options_source in ("sensibull", "both"):
            tm = shared.app_ctx.zd_ticker_manager
            if tm and tm.live_options_engine and not shared.app_ctx.sensibull_feed:
                _enrichment_only = (shared.app_ctx.options_source == "both")
                _start_sensibull_feed(tm.live_options_engine, enrichment_only=_enrichment_only)

        TELEGRAM_NOTIFICATIONS.is_intraday = False
        TELEGRAM_NOTIFICATIONS.send_notification(
            "\U0001F4CB <b>Pre-Market Analysis Started</b> \U0001F4CB", parse_mode="HTML"
        )

        try:
            run_global_cues_report()
        except Exception as e:
            logger.error(f"Global cues report failed: {e}")

        # ── Pre-open report at 9:07 ──
        _wait_until(9, 7)
        try:
            run_preopen_report()
        except Exception as e:
            logger.error(f"Pre-open report failed: {e}")

        TELEGRAM_NOTIFICATIONS.is_intraday = True

        # ── Morning bias (fast path from 8 PM analysis) ──
        if ENABLE_INTELLIGENCE:
            try:
                update_morning_bias()
            except Exception as e:
                logger.error(f"Morning bias failed: {e}")

        # ── Intraday analysis (9:15 - 15:30) ──
        _wait_until(9, 15)
        logger.info("=== Intraday phase ===")
        intraday_analysis(
            max_cycles=int(os.getenv(constant.ENV_DEV_MAX_CYCLES, "0")),
            loop_wait_time=int(os.getenv(constant.ENV_DEV_LOOP_WAIT, "30")),
        )

        # ── Wait for positional (15:30 - 20:00) ──
        logger.info("=== Intraday complete. Waiting for 8 PM positional analysis ===")

        # Refresh enctoken before positional window — data-gateway's positional
        # fetch runs at 19:00 and needs a valid token (the 09:00 token has expired).
        _wait_until(18, 50)
        _refresh_zerodha_auth()

        _wait_until(20, 0)

        # ── Positional analysis (20:00) ──
        logger.info("=== Positional phase ===")
        positional_analysis()

        # ── Stop Sensibull feeds after market close (no longer needed) ──
        _stop_sensibull_feed()

        # ── Disconnect Zerodha WebSockets until tomorrow ──
        _stop_zerodha_ws()

        # ── Done for today ──
        _morning_bias_done = False
        logger.info("=== Daily cycle complete. Sleeping until midnight ===")
        _sleep_until_midnight()


def start_stock_analysis():
        logger.info("Running in development mode. No shutdown operation.")

        if ENABLE_INTELLIGENCE:
            try:
                compute_morning_bias()
            except Exception as e:
                logger.error(f"Morning bias computation failed: {e}")

        if LIVE_OPTIONS_ONLY:
            live_options_analysis()
        elif shared.app_ctx.mode and shared.app_ctx.mode.name == shared.Mode.INTRADAY.name:
            intraday_analysis(
                max_cycles=int(os.getenv(constant.ENV_DEV_MAX_CYCLES, "0")),
                loop_wait_time=int(os.getenv(constant.ENV_DEV_LOOP_WAIT, "30")),
            )
        elif shared.app_ctx.mode and shared.app_ctx.mode.name == shared.Mode.POSITIONAL.name:
            positional_analysis()

        _shutdown_background_services()
        # Stop the Telegram bot in dev mode too so the process exits cleanly
        if ENABLE_TELEGRAM_BOT:
            from notification.bot_listener import stop_telegram_bot
            stop_telegram_bot()

def _wait_for_cycle_ready(timeout: float = 120.0) -> bool:
    """Wait for the next data-gateway cycle signal via Pub/Sub subscriber.

    Returns True if signal received, False on timeout (stale data fallback).
    """
    if cycle_subscriber is None:
        return False
    return cycle_subscriber.wait_for_cycle(timeout=timeout)


def _load_initial_data_from_redis():
    """Load initial price + sensibull data from data-gateway's Redis hashes."""
    logger.info("[cycle] Loading initial data from Redis...")

    stock_objs = list(shared.app_ctx.stock_token_obj_dict.values())
    index_objs = list(shared.app_ctx.index_token_obj_dict.values())
    commodity_objs = list(shared.app_ctx.commodity_token_obj_dict.values())
    global_indices_objs = list(shared.app_ctx.global_indices_token_obj_dict.values())

    updated = load_price_data_from_redis(
        redis_proxy, stock_objs, index_objs,
        commodity_objs, global_indices_objs,
    )
    logger.info(f"[cycle] Loaded price data for {updated} symbols")

    sensibull_loaded = 0
    for stock in stock_objs + index_objs:
        if load_sensibull_from_redis(redis_proxy, stock):
            sensibull_loaded += 1

    logger.info(f"[cycle] Loaded sensibull data for {sensibull_loaded} symbols")


def _shutdown_background_services():
    """
    Shut down non-daemon background threads so the process exits cleanly in dev mode.
    """
    narrator = shared.app_ctx.narrator
    if narrator:
        try:
            narrator.shutdown()
            logger.debug("[shutdown] narrator executor stopped")
        except Exception as e:
            logger.warning(f"[shutdown] narrator shutdown error: {e}")

    _stop_sensibull_feed()
    _stop_zerodha_ws()

if __name__ =="__main__":

    # ── Dev-mode premarket-only shortcut ─────────────────────────────────────
    # Run with: python3 intraday_monitor.py --premarket
    # Skips all heavy init. Works without PRODUCTION=1.
    _pre_args = argparse.ArgumentParser(add_help=False)
    _pre_args.add_argument("--premarket", action="store_true")
    _known, _ = _pre_args.parse_known_args()
    if _known.premarket:
        load_dotenv()
        logger.info("--premarket flag detected: running pre-market report only.")
        TELEGRAM_NOTIFICATIONS.is_production = os.getenv(constant.ENV_PRODUCTION, "0") == "1"
        TELEGRAM_NOTIFICATIONS.is_intraday = False
        run_global_cues_report()
        run_preopen_report()
        logger.info("Pre-market report complete. Exiting.")
        sys.exit(0)
    # ─────────────────────────────────────────────────────────────────────────

    init()
    if PRODUCTION:
        # Always-running daily loop (self-schedules pre-market, intraday, positional)
        if ENABLE_TELEGRAM_BOT:
            thread = threading.Thread(target=_run_daily_loop)
            thread.start()
            init_telegram_bot()
        else:
            _run_daily_loop()
    else:
        # Dev mode — single run, exit after (unchanged behavior)
        if ENABLE_TELEGRAM_BOT:
            thread = threading.Thread(target=start_stock_analysis)
            thread.start()
            init_telegram_bot()
        else:
            start_stock_analysis()
    
    
