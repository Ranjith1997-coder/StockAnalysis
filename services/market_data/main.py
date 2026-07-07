"""
Market-Data Service — WebSocket tick ingestion + Redis snapshot publisher.

Owns all live WebSocket connections (Zerodha WS1/WS2, Sensibull WS),
the TickStore state, live engines (LiveOptionsEngine, LiveStockEngine),
and the TokenRegistry. Publishes 1-second snapshots to Redis hashes
so the monolith (bot commands, narrator) and analysis-engine workers
can read live tick data without holding WS connections.

Lifecycle:
  1. Load instruments from Zerodha (kc.instruments — public API)
  2. Build Stock objects + TokenRegistry (same as monolith init)
  3. Wait for enctoken from Redis (published by monolith at 09:00)
  4. Connect WS1 (equity/index) + WS2 (options)
  5. Start Sensibull feeds (greeks enrichment)
  6. Start snapshot publisher (1-second Redis writes)
  7. Health heartbeat loop

Enctoken refresh: consumes `auth:commands` stream — when the monolith
publishes a `refresh_enctoken` command (triggered by data-gateway 403
or 18:50 proactive refresh), this service picks up the new token from
Redis and reconnects WS.
"""
from __future__ import annotations

import json
import os
import signal as sig
import sys
import threading
import time
from datetime import datetime
from urllib.parse import quote

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

import common.constants as constant
import common.shared as shared
from common.Stock import Stock
from common.helperFunctions import get_stock_objects_from_json
from common.logging_util import logger
from common.token_registry import (
    TokenInfo,
    TokenType,
    TokenRegistry,
)
from services.common.redis_proxy import RedisProxy
from services.market_data.snapshot_publisher import SnapshotPublisher
from services.market_data.signal_publisher import RedisSignalBus
from services.common.metrics import incr_stock, set_stock, incr_system, set_system

# ── Lazy imports (heavy deps) ──────────────────────────────────────────────
from zerodha.zerodha_analysis import ZerodhaTickerManager
from zerodha.zerodha_connect import KiteConnect
from zerodha.live_options_engine import LiveOptionsEngine
from zerodha.live_stock_engine import LiveStockEngine
from fno.sensibull_feed import SensibullFeed
from fno.sensibull_adapter import SensibullAdapter
from notification.Notification import TELEGRAM_NOTIFICATIONS


AUTH_HASH = "auth:zerodha"
AUTH_CHANNEL = "auth:enctoken_refreshed"
AUTH_COMMANDS_STREAM = "auth:commands"

_running = True


def signal_handler(signum, frame):
    global _running
    logger.info(f"[market-data] Received signal {signum}, shutting down...")
    _running = False


# ── Stock object + registry setup ──────────────────────────────────────────

def _build_stock_objects():
    """Build Stock objects from final_derivatives_list.json (same as monolith)."""
    stock_list, index_list, commodity_list, global_indices_list = get_stock_objects_from_json()

    for index in index_list:
        ticker = Stock(
            index["name"], index["tradingsymbol"],
            yfinanceSymbol=index.get("yfinancetradingsymbol"),
            is_index=True,
        )
        shared.app_ctx.index_token_obj_dict[index["instrument_token"]] = ticker

    for stock in stock_list:
        ticker = Stock(stock["name"], stock["tradingsymbol"])
        shared.app_ctx.stock_token_obj_dict[stock["instrument_token"]] = ticker

    for commodity in commodity_list:
        ticker = Stock(
            commodity["name"], commodity["tradingsymbol"],
            yfinanceSymbol=commodity.get("yfinancetradingsymbol"),
            is_index=True,
        )
        shared.app_ctx.commodity_token_obj_dict[commodity["instrument_token"]] = ticker

    for gi in global_indices_list:
        ticker = Stock(
            gi["name"], gi["tradingsymbol"],
            yfinanceSymbol=gi.get("yfinancetradingsymbol"),
            is_index=True,
        )
        shared.app_ctx.global_indices_token_obj_dict[gi["instrument_token"]] = ticker

    logger.info(
        f"[market-data] Built {len(shared.app_ctx.stock_token_obj_dict)} stocks, "
        f"{len(shared.app_ctx.index_token_obj_dict)} indices, "
        f"{len(shared.app_ctx.commodity_token_obj_dict)} commodities, "
        f"{len(shared.app_ctx.global_indices_token_obj_dict)} global indices"
    )


def _register_base_tokens(registry: TokenRegistry):
    """Register all equity/index/commodity/global-index tokens."""
    for token, stock in shared.app_ctx.stock_token_obj_dict.items():
        registry.register(TokenInfo(
            token=token, token_type=TokenType.EQUITY,
            parent_symbol=stock.stock_symbol, tradingsymbol=stock.stock_symbol,
        ))
        registry.set_parent_object(stock.stock_symbol, stock)

    for token, index in shared.app_ctx.index_token_obj_dict.items():
        registry.register(TokenInfo(
            token=token, token_type=TokenType.INDEX,
            parent_symbol=index.stock_symbol, tradingsymbol=index.stock_symbol,
        ))
        registry.set_parent_object(index.stock_symbol, index)

    for token, commodity in shared.app_ctx.commodity_token_obj_dict.items():
        registry.register(TokenInfo(
            token=token, token_type=TokenType.COMMODITY,
            parent_symbol=commodity.stock_symbol, tradingsymbol=commodity.stock_symbol,
        ))
        registry.set_parent_object(commodity.stock_symbol, commodity)

    for token, gi in shared.app_ctx.global_indices_token_obj_dict.items():
        registry.register(TokenInfo(
            token=token, token_type=TokenType.GLOBAL_INDEX,
            parent_symbol=gi.stock_symbol, tradingsymbol=gi.stock_symbol,
        ))
        registry.set_parent_object(gi.stock_symbol, gi)

    logger.info(f"[market-data] Token registry: {registry.get_stats()}")


def _register_option_tokens(registry: TokenRegistry, all_options_df, all_futures_df):
    """Register option + futures tokens for current expiry (same as monolith's update_zerodha_option_chain)."""
    for stock in shared.app_ctx.stock_token_obj_dict.values():
        _register_one_symbol(registry, stock, all_options_df, all_futures_df, TokenType.EQUITY)

    for index in shared.app_ctx.index_token_obj_dict.values():
        if index.stock_symbol in constant.INDEX_ANALYSIS_EXCLUDE:
            continue
        _register_one_symbol(registry, index, all_options_df, all_futures_df, TokenType.INDEX)

    logger.info(f"[market-data] Token registry after options: {registry.get_stats()}")


def _register_one_symbol(registry, stock, all_options_df, all_futures_df, parent_type):
    symbol = stock.stock_symbol
    options = all_options_df[all_options_df["name"] == symbol]
    options = options[["instrument_token", "tradingsymbol", "expiry", "strike", "instrument_type"]]

    expiry_dates = sorted(options["expiry"].unique())
    if not expiry_dates:
        return

    current_options = options[options["expiry"] == expiry_dates[0]]

    zerodha_ctx = stock.zerodha_ctx
    zerodha_ctx["option_chain"]["current"] = current_options
    if len(expiry_dates) > 1:
        zerodha_ctx["option_chain"]["next"] = options[options["expiry"] == expiry_dates[1]]
    else:
        zerodha_ctx["option_chain"]["next"] = pd.DataFrame()

    futures = all_futures_df[all_futures_df["name"] == symbol]
    futures = futures[["instrument_token", "tradingsymbol", "expiry", "instrument_type"]]
    futures_expiry = sorted(futures["expiry"].unique())
    if futures_expiry:
        zerodha_ctx["futures_mdata"]["current"] = futures[futures["expiry"] == futures_expiry[0]]
        zerodha_ctx["futures_mdata"]["next"] = (
            futures[futures["expiry"] == futures_expiry[1]]
            if len(futures_expiry) > 1 else pd.DataFrame()
        )

    for _, row in current_options.iterrows():
        registry.register(TokenInfo(
            token=int(row["instrument_token"]),
            token_type=TokenType.OPTION,
            parent_symbol=symbol,
            tradingsymbol=row["tradingsymbol"],
            strike=float(row["strike"]),
            option_type=row["instrument_type"],
        ))

    current_futures = futures[futures["expiry"] == futures_expiry[0]] if futures_expiry else pd.DataFrame()
    for _, row in current_futures.iterrows():
        registry.register(TokenInfo(
            token=int(row["instrument_token"]),
            token_type=TokenType.FUTURE,
            parent_symbol=symbol,
            tradingsymbol=row["tradingsymbol"],
            expiry=str(row["expiry"]),
        ))

    if not shared.app_ctx.stockExpires and expiry_dates:
        shared.app_ctx.stockExpires = [str(e) for e in expiry_dates]


# ── Enctoken handling ──────────────────────────────────────────────────────

def _wait_for_enctoken(redis: RedisProxy, timeout: int = 600) -> str | None:
    """Wait for enctoken to appear in Redis (published by monolith at 09:00)."""
    deadline = time.time() + timeout
    while time.time() < deadline and _running:
        enctoken = redis.hget(AUTH_HASH, "enctoken")
        if enctoken:
            logger.info("[market-data] Enctoken found in Redis")
            return enctoken
        time.sleep(2)
    logger.error("[market-data] Timeout waiting for enctoken")
    return None


def _start_enctoken_subscriber(redis: RedisProxy, tm: ZerodhaTickerManager):
    """Background thread: listen for enctoken refresh via Pub/Sub."""
    def _listen():
        ps = redis.pubsub()
        ps.subscribe(AUTH_CHANNEL)
        logger.info(f"[market-data] Subscribed to {AUTH_CHANNEL}")
        for message in ps.listen():
            if not _running:
                break
            if message["type"] == "message":
                enctoken = redis.hget(AUTH_HASH, "enctoken")
                if enctoken:
                    try:
                        tm.update_enctoken(quote(enctoken, safe=""))
                        logger.info("[market-data] Enctoken updated via Pub/Sub")
                    except Exception as e:
                        logger.error(f"[market-data] Enctoken update failed: {e}")

    t = threading.Thread(target=_listen, daemon=True, name="enctoken-subscriber")
    t.start()


def _start_auth_commands_consumer(redis: RedisProxy, tm: ZerodhaTickerManager):
    """Consume auth:commands stream for reactive enctoken refresh.

    When the data-gateway gets 403, it publishes a refresh_enctoken command.
    The monolith does the TOTP login and publishes the new token to Redis.
    This service picks it up via the Pub/Sub subscriber above.
    This consumer is a backup in case Pub/Sub is missed.
    """
    group = "market-data"
    consumer = "md-auth-1"
    try:
        redis.xgroup_create(group, AUTH_COMMANDS_STREAM, mkstream=True)
    except Exception:
        pass

    def _consume():
        while _running:
            try:
                messages = redis.xreadgroup(
                    group, consumer, {AUTH_COMMANDS_STREAM: ">"},
                    count=1, block=10000,
                )
                if not messages:
                    continue
                entries = messages[0][1] if isinstance(messages, list) and messages else []
                for msg_id, fields in entries:
                    command = fields.get("command", "")
                    if command == "refresh_enctoken":
                        enctoken = redis.hget(AUTH_HASH, "enctoken")
                        if enctoken:
                            try:
                                tm.update_enctoken(quote(enctoken, safe=""))
                                logger.info("[market-data] Enctoken refreshed via auth:commands")
                            except Exception as e:
                                logger.error(f"[market-data] Enctoken refresh failed: {e}")
                    try:
                        redis.xack(AUTH_COMMANDS_STREAM, group, msg_id)
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"[market-data] auth:commands consumer error: {e}")
                time.sleep(5)

    t = threading.Thread(target=_consume, daemon=True, name="auth-commands-consumer")
    t.start()


# ── Sensibull feed startup ─────────────────────────────────────────────────

def _start_sensibull_feeds(tm: ZerodhaTickerManager, enrichment_only: bool):
    """Start Sensibull WS feeds for LIVE_OPTIONS_INDICES."""
    adapter = SensibullAdapter()
    feeds = []
    started = []

    for underlying_token, index_stock in shared.app_ctx.index_token_obj_dict.items():
        symbol = index_stock.stock_symbol
        if symbol not in constant.LIVE_OPTIONS_INDICES:
            continue

        chain_df = index_stock.zerodha_ctx.get("option_chain", {}).get("current")
        if chain_df is None or chain_df.empty:
            logger.warning(f"[market-data] No option chain for {symbol}, skipping Sensibull")
            continue
        expiry_val = chain_df["expiry"].iloc[0]
        expiry = expiry_val.strftime("%Y-%m-%d") if hasattr(expiry_val, "strftime") else str(expiry_val)[:10]

        subscriptions = [{"underlying": underlying_token, "expiry": expiry}]
        captured = index_stock

        def make_callback(stock):
            def _on_snapshot(token, data):
                try:
                    adapter.apply(stock, data, tm.live_options_engine,
                                  enrichment_only=enrichment_only)
                except Exception as exc:
                    logger.error(f"[market-data] Sensibull snapshot error for {stock.stock_symbol}: {exc}")
            return _on_snapshot

        feed = SensibullFeed(subscriptions, on_snapshot=make_callback(captured))
        feed.start()
        feeds.append(feed)
        started.append(symbol)
        logger.info(f"[market-data] Sensibull feed started for {symbol} (expiry={expiry})")

    shared.app_ctx.sensibull_feed = feeds
    if feeds:
        mode_label = "Enrichment" if enrichment_only else "Primary"
        logger.info(f"[market-data] {len(feeds)} Sensibull feed(s) running [{mode_label}]: {started}")


# ── Health heartbeat ───────────────────────────────────────────────────────

_prev_total_ticks = 0

def _update_heartbeat(redis: RedisProxy, tm: ZerodhaTickerManager,
                      publisher: SnapshotPublisher | None = None):
    global _prev_total_ticks
    ws1_subs = 0
    ws2_subs = 0
    ws2_reconnects = 0
    try:
        if tm._kt_base:
            ws1_subs = len(tm._kt_base.subscribed_tokens)
        if tm._kt_options:
            ws2_subs = len(tm._kt_options.subscribed_tokens)
            ws2_reconnects = getattr(tm._kt_options, "reconnect_attempts", 0)
    except Exception:
        pass

    sensibull_count = 0
    if shared.app_ctx.sensibull_feed:
        sensibull_count = len(shared.app_ctx.sensibull_feed)

    redis.hset("service:registry:market-data", mapping={
        "name": "market-data",
        "pid": str(os.getpid()),
        "status": "healthy",
        "last_heartbeat": str(time.time()),
        "ws1_connected": str(tm.connected),
        "ws2_connected": str(tm.options_connected),
        "ws1_subs": str(ws1_subs),
        "ws2_subs": str(ws2_subs),
        "ws2_reconnects": str(ws2_reconnects),
        "sensibull_feeds": str(sensibull_count),
        "last_equity_tick": str(shared.app_ctx.last_equity_tick_time),
        "tick_count": str(tm._tick_count),
    })
    redis.expire("service:registry:market-data", 120)

    # ── System stats (stats:system) ─────────────────────────────────────
    total_ticks = tm._tick_count
    tick_rate = (total_ticks - _prev_total_ticks) / 30.0
    _prev_total_ticks = total_ticks

    incr_system("total_ticks", total_ticks)  # absolute value each cycle
    set_system(
        total_ticks=str(total_ticks),
        tick_rate=f"{tick_rate:.1f}",
        ws2_reconnects=str(ws2_reconnects),
        snapshot_age_s=str(
            int(time.time() - publisher.last_publish_time)
            if publisher and publisher.last_publish_time else 0
        ),
    )

    # ── Per-stock tick counters (batch via set_stock) ───────────────────
    for _, stock in shared.app_ctx.stock_token_obj_dict.items():
        ts = stock._tick_store
        if ts.tick_count > 0:
            set_stock(stock.stock_symbol,
                tick_count=str(ts.tick_count),
            )

    for _, index_obj in shared.app_ctx.index_token_obj_dict.items():
        ts = index_obj._tick_store
        if ts.tick_count > 0 or ts.option_tick_count > 0:
            set_stock(index_obj.stock_symbol,
                tick_count=str(ts.tick_count),
                option_tick_count=str(ts.option_tick_count),
            )


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    global _running

    sig.signal(sig.SIGTERM, signal_handler)
    sig.signal(sig.SIGINT, signal_handler)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = RedisProxy(redis_url)
    try:
        redis.get("ping")
        logger.info(f"[market-data] Connected to Redis at {redis_url}")
    except Exception as e:
        logger.error(f"[market-data] Cannot connect to Redis: {e}")
        sys.exit(1)

    from services.common.crash_handler import install_crash_handler
    install_crash_handler("market-data")

    # 1. Build Stock objects
    _build_stock_objects()

    # 2. Init token registry
    registry = TokenRegistry()
    shared.app_ctx.token_registry = registry

    # 3. Load instruments + register tokens
    logger.info("[market-data] Loading Zerodha instruments...")
    kc = KiteConnect(constant.DUMMY_API_KEY_ZERODHA)
    all_instruments = pd.DataFrame(kc.instruments())
    all_options = all_instruments[all_instruments["segment"].isin(["NFO-OPT", "BFO-OPT"])]
    all_futures = all_instruments[all_instruments["segment"].isin(["NFO-FUT", "BFO-FUT"])]

    _register_base_tokens(registry)
    _register_option_tokens(registry, all_options, all_futures)

    # 4. Wait for enctoken
    enctoken = _wait_for_enctoken(redis)
    if not enctoken:
        logger.error("[market-data] No enctoken — exiting")
        sys.exit(1)

    enc_quoted = quote(enctoken, safe="")

    # 5. Create ZerodhaTickerManager
    userName = os.getenv(constant.ENV_ZERODHA_USERNAME, "")
    password = os.getenv(constant.ENV_ZERODHA_PASSWORD, "")
    tm = ZerodhaTickerManager(userName, password, enc_quoted)
    shared.app_ctx.zd_ticker_manager = tm
    shared.app_ctx.zd_kc = KiteConnect(
        constant.DUMMY_API_KEY_ZERODHA,
        root="https://kite.zerodha.com/",
        enctoken=enctoken,
    )

    # 6. Create live engines
    options_source = os.getenv(constant.ENV_OPTIONS_SOURCE, "zerodha").lower()
    shared.app_ctx.options_source = options_source
    logger.info(f"[market-data] OPTIONS_SOURCE={options_source}")

    tm.live_options_engine = LiveOptionsEngine()
    logger.info("[market-data] LiveOptionsEngine attached")

    signal_bus = RedisSignalBus(redis)
    shared.app_ctx.signal_bus = signal_bus
    tm.live_stock_engine = LiveStockEngine(signal_bus)
    logger.info("[market-data] LiveStockEngine attached (RedisSignalBus)")

    # 7. Connect WS
    if tm.connect():
        logger.info("[market-data] Zerodha WS1 + WS2 connected")
    else:
        logger.error("[market-data] WS connect failed — will retry on enctoken refresh")

    # 8. Subscribe option tokens after first equity ticks arrive
    def _subscribe_options_later():
        time.sleep(5)
        if tm.connected and _running:
            try:
                tm.subscribe_live_options(wait_for_ticks=False)
                logger.info("[market-data] Option tokens subscribed")
            except Exception as e:
                logger.error(f"[market-data] Option subscription failed: {e}")

    threading.Thread(target=_subscribe_options_later, daemon=True, name="opt-subscriber").start()

    # 9. Start Sensibull feeds
    enrichment_only = options_source == "both"
    _start_sensibull_feeds(tm, enrichment_only=enrichment_only)

    # 10. Start snapshot publisher
    stock_objs = list(shared.app_ctx.stock_token_obj_dict.values())
    index_objs = list(shared.app_ctx.index_token_obj_dict.values())
    publisher = SnapshotPublisher(redis, stock_objs, index_objs)
    publisher.start()

    # 11. Enctoken subscribers
    _start_enctoken_subscriber(redis, tm)
    _start_auth_commands_consumer(redis, tm)

    # 12. Health heartbeat loop
    logger.info("[market-data] Service started — entering heartbeat loop")
    heartbeat_counter = 0

    while _running:
        try:
            _update_heartbeat(redis, tm, publisher=publisher)
        except Exception as e:
            logger.error(f"[market-data] Heartbeat error: {e}")

        time.sleep(30)
        heartbeat_counter += 1

    # ── Shutdown ────────────────────────────────────────────────────────────
    logger.info("[market-data] Shutting down...")
    publisher.stop()

    try:
        tm.close_connection()
        tm.stop_tick_processor()
    except Exception:
        pass

    if shared.app_ctx.sensibull_feed:
        for feed in shared.app_ctx.sensibull_feed:
            try:
                feed.stop()
            except Exception:
                pass

    redis.hset("service:registry:market-data", mapping={
        "status": "shutdown",
        "last_heartbeat": str(time.time()),
    })
    redis.expire("service:registry:market-data", 120)
    redis.close()
    logger.info("[market-data] Shutdown complete")


if __name__ == "__main__":
    main()
