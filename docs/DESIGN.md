# StockAnalysis - Comprehensive Design Document

> **Last Updated**: July 2026 (Phase 1–4 complete: data-gateway, notification-service, market-data, analysis-engine, resource-monitor, auth-service all extracted as always-running microservices; monolith is always-running with self-scheduling daily loop; cycle sync via Redis Pub/Sub + stream; parallel Sensibull fetch (10 workers); unified logging via `services/common/logging.py`; per-stock + system-wide metrics/counters in Redis; `/debugstats` bot command; resource monitor with system/per-service/Redis metrics every 30s, time-series storage, proactive alerts, `/sysstats` bot command; prevDayOHLCV daily refresh with pre-open-safe `_get_prev_day_row()` helper + Zerodha fallback for NaN closes; service versioning via git SHA in Redis registry + `/version` bot command; auth-service handles scheduled TOTP login (09:00 + 18:50) + reactive refresh via auth:commands stream + automated Sensibull OAuth login; monolith reads enctoken from Redis via Pub/Sub subscriber; Zerodha REST endpoint changed from api.kite.trade to kite.zerodha.com/oms; Sensibull REST APIs now require cookie auth (access_token + client_info) — auto-refreshed by auth-service; Sensibull stock_info endpoint renamed from /cache/insights/stock_info to /cache/stock_info with fallback to OI chain + IV chart reconstruction; intraday futures fetch enabled for all symbols (was gated behind positional mode only); no systemd timers — all services self-schedule)
> **Purpose**: Complete architectural reference for the Indian Stock Market Analysis System targeting NSE (National Stock Exchange) equities. This document captures the entire codebase: architecture, data flows, module interactions, signal processing, scoring, notifications, and deployment details.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Project Structure](#2-project-structure)
3. [Core Data Model](#3-core-data-model)
4. [Application Context & Shared State](#4-application-context--shared-state)
5. [Data Pipeline & Sources](#5-data-pipeline--sources)
6. [Analyzer Framework](#6-analyzer-framework)
7. [Individual Analyzers](#7-individual-analyzers)
8. [Scoring & Notification System](#8-scoring--notification-system)
9. [Intraday Monitor (Main Entry Point)](#9-intraday-monitor-main-entry-point)
10. [Pre-Market Reports](#10-pre-market-reports)
11. [Post-Market Analysis](#11-post-market-analysis)
12. [Zerodha Integration](#12-zerodha-integration)
13. [Live Options Tracking](#13-live-options-tracking)
14. [Intelligence Layer](#14-intelligence-layer)
15. [Backtesting Framework](#15-backtesting-framework)
16. [Sentiment Analysis](#16-sentiment-analysis)
17. [Notification & Telegram](#17-notification--telegram)
18. [ML Pipeline](#18-ml-pipeline)
19. [Configuration & Constants](#19-configuration--constants)
20. [Market Holiday Gatekeeper & Warning System](#20-market-holiday-gatekeeper--warning-system)
21. [Observability System](#21-observability-system)
22. [Key Design Patterns](#22-key-design-patterns)
23. [Data Flow Diagrams](#23-data-flow-diagrams)
24. [Test Suite](#24-test-suite)

---

## 1. System Overview

### What It Does
An automated stock market analysis system for the Indian market (NSE) that:
- Monitors stocks in **real-time during market hours** (intraday mode, 5-min intervals, 9:15 AM - 3:30 PM)
- Performs **end-of-day positional analysis** (daily data, ~2 years history)
- Tracks **live NIFTY, BANKNIFTY, and SENSEX options per tick** via Zerodha WebSocket (zone-based subscription; SENSEX uses BFO segment)
- Runs **9 specialized analyzers** across technical, volume, candlestick, options, futures, IV, PCR, max pain, and OI chain domains, plus **1 composite analyser** (`OptionSellerCompositeAnalyser`) for high-probability option-seller setups
- Runs **2 live options analyzers** (LiveOIAnalyser, LiveStraddleAnalyser) on real-time option ticks
- Scores signals with a **weighted scoring engine** and sends alerts to **Telegram channels**
- Generates **pre-market** and **post-market** summary reports
- Detects **cross-layer signal confluence** (live + intraday + positional alignment) via SignalCorrelator
- Generates **LLM-powered trade narratives** and EOD market briefings via Google Gemini Flash
- Supports **backtesting** with Optuna-based parameter optimization
- Includes **sentiment analysis** via FinBERT on news headlines

### Three-Mode Operation

| Aspect | Intraday | Positional/EOD | Live Options |
|--------|----------|----------------|--------------|
| Data interval | 5 minutes | 1 day | Per tick (~ms) |
| History depth | ~60 days (yfinance) | ~2 years | 375 min (intraday) |
| Market hours | 9:15 AM - 3:30 PM | After 4:00 PM | 9:15 AM - 3:30 PM |
| Loop interval | Every ~310 seconds | Single run | Continuous WebSocket |
| Telegram channel | Intraday channel | Positional channel | Live Options channel |
| Thresholds | Tighter (faster signals) | Wider (confirmation-based) | Cooldown-gated |
| Enabled by | Default | Default | `ENABLE_LIVE_OPTIONS=1` |

---

## 2. Project Structure

```
StockAnalysis/
|-- intraday/
|   |-- intraday_monitor.py          # Main entry point & orchestration loop
|
|-- analyser/
|   |-- Analyser.py                  # BaseAnalyzer (decorator framework) + AnalyserOrchestrator
|   |-- TechnicalAnalyser.py         # RSI, MACD, EMA, Bollinger, VWAP, Supertrend, etc.
|   |-- VolumeAnalyser.py            # Volume breakout, OBV divergence, volume climax
|   |-- candleStickPatternAnalyser.py # Single/double/triple candle patterns
|   |-- IVAnalyser.py                # IV spike, trend, rank (IVP), IV vs HV comparison
|   |-- PCRAnalyser.py               # Put-Call Ratio analysis (5 methods)
|   |-- MaxPainAnalyser.py           # Max Pain deviation, trend, alignment
|   |-- OIChainAnalyser.py           # OI-based S/R, buildup, walls, shifts, intraday trend
|   |-- Futures_Analyser.py          # Futures action, PVO, ORB with dynamic thresholds
|   |-- LiveOIAnalyser.py            # Real-time OI wall breach, PCR crossover, PCR sustained
|   |-- LiveStraddleAnalyser.py      # Straddle IV change, IV skew reversal, implied move boundary
|   |-- LiveOptionsHistory.py        # In-memory per-symbol time-series (375 snapshots/day)
|   |-- LiveAlertFormatter.py        # HTML builder for real-time Telegram alerts (F singleton)
|   |-- MessageFormatter.py          # Registry-based formatter for batch analysis alerts
|   |-- PanicModeAnalyser.py         # Composite analyser — panic detection & exhaustion (9th, must precede OptionSellerCompositeAnalyser)
|   |-- OptionSellerCompositeAnalyser.py  # Option-seller composite setups (10th, MUST be last)
|                                    # Emits GAMMA_TRAP / RANGE_BOUND_SETUP / SKEW_FADE_SETUP
|                                    # Sets PRIORITY_OVERRIDE; bypasses score gate via winner-takes-all dispatch
|
|-- intelligence/
|   |-- signal.py                    # Signal dataclass, Direction, Layer, SignalStrength enums
|   |-- signal_bus.py                # Thread-safe pub/sub event bus for cross-layer signals
|   |-- correlator.py                # SignalCorrelator — time-windowed cross-layer confluence
|   |-- context_builder.py           # ContextBuilder — gathers live market data for LLM prompts
|   |-- narrator.py                  # MarketNarrator — LLM-powered trade thesis & EOD briefing
|   |-- llm_client.py                # GeminiClient — Google Gemini Flash API wrapper
|
|-- common/
|   |-- Stock.py                     # Core Stock data model; delegates live ticks to TickStore
|   |-- shared.py                    # AppContext singleton, Mode enum, global state
|   |-- constants.py                 # All weights, thresholds, env vars, categories
|   |-- scoring.py                   # Score calculation, alignment bonus, should_notify()
|   |-- helperFunctions.py           # Utilities (percentage change, JSON I/O, time checks)
|   |-- logging_util.py              # Logger configuration (monolith, legacy)
|   |-- token_registry.py            # TokenRegistry — O(1) tick routing, zone management

|-- services/                        # Microservices (Phase 1–3 extraction)
|   |-- common/
|   |   |-- logging.py               # Per-service logger: get_logger("service-name")
|   |   |-- redis_proxy.py           # Sync Redis wrapper (hset, hgetall, xadd, xreadgroup, publish, pubsub)
|   |   |-- serialization.py         # DataFrame/dict JSON serialization
|   |   |-- crash_handler.py         # Shared crash handler — install_crash_handler(service_name)
|   |   |-- version.py               # Git SHA + dirty flag captured at import time (SERVICE_VERSION, GIT_COMMIT, GIT_DIRTY)
|   |   |-- stock_proxy.py           # Stock object ↔ Redis serialization (async)
|   |   |-- stock_loader.py          # Sync Stock reconstruction from Redis hashes (monolith)
|   |   |-- cycle_subscriber.py      # Pub/Sub + stream subscriber for cycle sync (monolith ↔ data-gateway)
|   |   |-- metrics.py               # Per-stock + system-wide counters in Redis (fail-safe)
|   |   |-- rate_limiter.py          # Redis-based rate limiter
|   |-- notification-service/        # EXTRACTED — always-running stream consumer
|   |   |-- main.py                  # Stream consumer: reads notification:jobs, sends Telegram/Discord
|   |-- data_gateway/                # EXTRACTED — always-running, self-scheduling data fetcher
|   |   |-- main.py                  # Self-scheduling loop (market calendar + clock)
|   |   |-- yfinance_fetcher.py      # yfinance data fetcher → Redis hashes (initial + intraday + positional + prevDayOHLCV daily refresh)
|   |   |-- sensibull_fetcher.py     # Parallel Sensibull REST fetcher (10 workers) → Redis hashes
|   |-- market_data/                 # EXTRACTED — always-running WebSocket ingestion
|   |   |-- main.py                  # WS1 (equity/index) + WS2 (options) + Sensibull WS → Redis snapshots
|   |   |-- snapshot_publisher.py    # Publishes data:tick:* and data:options_agg:* hashes at 1s interval
|   |   |-- signal_publisher.py      # Pub/Sub signal bus for live options alerts (signal:channel)
|   |-- analysis_engine/             # EXTRACTED — always-running stream consumer
|   |   |-- main.py                  # Consumes data:cycle_stream, dispatches worker pool
|   |   |-- worker.py                # Per-stock worker: 12 analysers + scoring → metrics
|   |-- resource_monitor/            # EXTRACTED — always-running system metrics collector
|   |   |-- main.py                  # Polls psutil + Redis every 30s, stores sys:latest:* + sys:ts:* time-series, fires proactive alerts
|   |-- auth_service/                # EXTRACTED — always-running Zerodha enctoken + Sensibull OAuth lifecycle manager
|   |   |-- main.py                  # Self-scheduling TOTP login (09:00 + 18:50) + Sensibull OAuth auto-login + reactive auth:commands consumer → Redis publish
|   |-- coordinator/                 # Designed — orchestrator + intelligence + bot merged
|   |-- orchestrator/                # Designed — standalone orchestrator
|   |-- intelligence-service/        # Designed — SignalBus + Correlator + Narrator
|   |-- bot-service/                 # Designed — Telegram bot commands

|-- notification/
|   |-- Notification.py              # Telegram message sender (routes through Redis stream)
|   |-- bot_listener.py              # Thin entry point: register_all() + LLM budget alert job
|   |-- commands/
|   |   |-- __init__.py              # register_all(application) — iterates all command modules
|   |   |-- _helpers.py             # find_stock_by_symbol(), build_gainers_losers()
|   |   |-- _guard.py               # guard decorator + debug_chat_only() helper
|   |   |-- account.py              # /start, /enctoken
|   |   |-- market.py               # /ltp, /gainers, /losers, /watchlist, /holidays, /straddle, /walls
|   |   |-- system.py               # /help, /status (System Health Dashboard), job_llm_budget_alert
|   |   |-- stats.py                # /debugstats — system + per-stock metrics dashboard
|   |   |-- debug.py                # Debug commands
|   |   |-- debug_inspect.py        # Debug inspection commands
|
|-- nse/
|   |-- nse_derivative_data.py       # NSE API wrappers (futures, options, bhav copy, etc.)
|   |-- nse_utils.py                 # NSE request utilities, date helpers, market calendar
|
|-- zerodha/
|   |-- zerodha_connect.py           # Modified KiteConnect with enctoken auth
|   |-- zerodha_analysis.py          # ZerodhaTickerManager (WebSocket lifecycle)
|   |-- zerodha_ticker.py            # Custom KiteTicker (Twisted/Autobahn WebSocket)
|   |-- tick_store.py                # TickStore — thread-safe live tick state (extracted from Stock)
|   |-- futures_fetcher.py           # FuturesFetcher — Zerodha Kite futures data fetcher (extracted from Stock)
|   |-- live_options_engine.py       # LiveOptionsEngine — per-tick options analysis coordinator
|   |-- live_stock_engine.py         # LiveStockEngine — per-tick equity signals (VWAP, ORB, imbalance)
|   |-- zerodha_exceptions.py        # Custom exceptions for Zerodha API errors
|
|-- premarket/
|   |-- premarket_report.py          # Global cues, bonds, commodities, VIX, FII/DII, pre-open
|
|-- post_market_analysis/
|   |-- base.py                      # Abstract PostMarketSource
|   |-- registry.py                  # SOURCE_CLASSES list
|   |-- runner.py                    # Pipeline: fetch -> normalize -> analyze -> summarize
|   |-- summary.py                   # HTML formatters per source type
|   |-- analysis.py                  # Post-market data analyzer/dispatcher
|   |-- fii_dii.py                   # FiiDiiActivitySource implementation
|   |-- fo_participant_oi.py         # FoParticipantOISource implementation
|   |-- index_returns.py             # IndexReturnsSource implementation
|   |-- sector_performance.py        # SectorPerformanceSource implementation
|
|-- backtest/
|   |-- backtest.py                  # Trade, BacktestResult, Backtester classes
|   |-- optimizer.py                 # Optuna-based ThresholdOptimizer
|   |-- *_backtest.py                # Individual strategy backtests
|
|-- sentiment/
|   |-- news_sentiment_manager.py    # FinBERT + Google News RSS
|
|-- fno/
|   |-- sensibull_fetcher.py         # SensibullFetcher — Sensibull API fetcher (extracted from Stock)
|   |-- OptionWriteStandardDeviation.py  # Legacy standard deviation writer
|
|-- auth/
|   |-- auth_login.py               # Automated TOTP Zerodha login — writes fresh enctoken to .env
|                                    # Requires ZERODHA_USER, ZERODHA_PASS, ZERODHA_TOTP_SECRET
|                                    # Called by auth-service at 09:00 + 18:50 IST via _do_refresh()
|
|-- ml_pipeline/                     # ML prediction pipeline (XGBoost, LightGBM, RF, Ensemble)
|-- scripts/
|   |-- deploy.py                    # SSH deploy: git pull + restart service on EC2
|   |-- service_stop.py              # Start EC2 (if stopped) + stop stock_analysis.service
|                                    # Holiday guard: exits early on non-trading days unless --force
|                                    # SSH retry-poll replaces fixed 15s sleep
|   |-- system_config               # systemd unit files for always-running deployment:
|                                    #   stockanalysis-notification.service  (24/7 stream consumer)
|                                    #   stockanalysis-data-gateway.service   (24/7 self-scheduling fetcher)
|                                    #   stockanalysis-market-data.service    (24/7 WebSocket ingestion)
|                                    #   stockanalysis-analysis-engine.service(24/7 stream consumer + 12 analysers)
|                                    #   stockanalysis-resource-monitor.service(24/7 metrics collector, 10% CPU quota)
|                                    #   stockanalysis-auth.service           (24/7 enctoken + Sensibull OAuth lifecycle, 10% CPU quota)
|                                    #   stockanalysis.service                 (24/7 monolith — Restart=always)
|                                    # No timers — all services self-schedule
|-- configs/                         # Configuration files
|-- data/                            # Stock lists (final_derivatives_list.json, etc.)
|-- docs/                            # Documentation
|-- tests/                           # Test suite (1251 tests, 69 files across 8 subdirectories)
|   |-- analyser/                    # 17 test files covering all 11 analyser classes
|   |-- common/                      # 6 test files (Stock, scoring, token_registry, market_calendar, etc.)
|   |-- services/                    # 8 test files (auth_service, rate_limiter, stock_loader, cycle_subscriber, version, prevday_fallback, analysis_worker, crash_handler/observability)
|   |-- zerodha/                     # 5 test files (ZerodhaTickerManager, LiveOptionsEngine, LiveStockEngine, etc.)
|   |-- notification/                # 2 test files (Notification, bot_listener)
|   |-- post_market_analysis/        # 9 test files (all sources, runner, summary, analysis)
|   |-- premarket/                   # 5 test files (fetching, formatting, helpers, parsing, runner)
|   |-- test_observability.py        # 22 tests for crash handler, heartbeat, zombie watchdog
```

---

## 3. Core Data Model

### `common/Stock.py` - The Stock Class

The `Stock` class is the central data container. Every stock/index tracked by the system is represented as a Stock instance. Live tick state (WebSocket data, options ticks, futures ticks) is delegated to an internal `TickStore` instance (`_tick_store`), keeping `Stock` focused on price/analysis data.

#### Key Attributes

```python
class Stock:
    stock_symbol: str           # e.g., "RELIANCE", "NIFTY 50"
    stock_name: str
    ltp: float                  # Last traded price (from WebSocket ticks)
    ltp_change_perc: float      # Percentage change from previous close
    priceData: pd.DataFrame     # OHLCV DataFrame (from yfinance)

    # Live tick state — backed by TickStore (delegate)
    _tick_store: TickStore      # Single-responsibility container for live WebSocket state

    # Derivatives data
    zerodha_ctx: dict           # Zerodha option chain & futures data
    sensibull_ctx: dict         # Sensibull OI chain, PCR, max pain

    # Live WebSocket options data — Properties that delegate to TickStore:
    options_live: dict          # {strike: {"CE": {ltp, oi, prev_oi, volume, ...}, "PE": {...}}}
    options_aggregate: dict     # Computed aggregates:
                                #   total_ce_oi, total_pe_oi, live_pcr, atm_strike,
                                #   atm_straddle_premium, atm_iv_ce, atm_iv_pe, iv_skew,
                                #   max_oi_ce_strike, max_oi_pe_strike,
                                #   net_ce_oi_change, net_pe_oi_change, last_updated
    futures_live: dict          # {expiry: {ltp, oi, volume, ...}}
    zerodha_data: dict          # Thread-safe equity/index tick snapshot (via property)

    # Candlestick pattern support
    current: dict               # Current candle OHLCV
    previous: dict              # Previous candle OHLCV
    previous_previous: dict     # Two candles back

    # Analysis results
    analysis: dict = {
        "BULLISH": {},          # signal_type -> namedtuple
        "BEARISH": {},
        "NEUTRAL": {},
        "NoOfTrends": 0,        # Legacy counter (still maintained)
        "ScoreResult": None,    # Populated after scoring
    }

    DERIVATIVE_DATA_LENGTH: int # Max stored derivative snapshots
```

#### Key Methods

| Method | Purpose |
|--------|---------|
| `set_analysis(sentiment, signal_type, data)` | Store analysis result (BULLISH/BEARISH/NEUTRAL) |
| `update_zerodha_data(tick)` | Delegate to TickStore — thread-safe equity/index tick update |
| `update_option_tick(strike, option_type, tick)` | Delegate to TickStore — store live option tick |
| `update_futures_tick(expiry_key, tick)` | Delegate to TickStore — store live futures tick |
| `recompute_options_aggregate(spot_price)` | Delegate to TickStore — recompute PCR, ATM strike, straddle premium, OI walls |
| `update_latest_data()` | Recompute `ltp` and `ltp_change_perc` from latest priceData + prevDayOHLCV |
| IV calculation methods | Black-Scholes using `scipy.optimize.brentq` |

#### Deprecated Shims (backward compatibility)

The following Stock methods are **deprecated** and emit `DeprecationWarning`. They delegate to the extracted fetcher classes:

| Deprecated Method | Replacement |
|------------------|-------------|
| `get_futures_data_for_stock(mode, is_next_expiry_required)` | `FuturesFetcher(kite_connect).fetch(stock, mode)` |
| `fetch_sensibull_data(mode)` | `SensibullFetcher().fetch_data(stock, mode)` |
| `fetch_sensibull_oi_chain(mode)` | `SensibullFetcher().fetch_oi_chain(stock, mode)` |

#### Analysis Dict Structure
When an analyzer fires a signal:
```python
stock.set_analysis("BULLISH", "RSI", RSIAnalysis(
    rsi_value=28.5,
    zone="oversold",
    duration=3
))
# Result: stock.analysis["BULLISH"]["RSI"] = RSIAnalysis(...)
# stock.analysis["NoOfTrends"] += 1
# Multiple signals for same type are stored as a list
```

### `zerodha/tick_store.py` - TickStore

Extracted from `Stock` to give Stock a single responsibility. `TickStore` owns all live WebSocket tick state:

```python
class TickStore:
    _lock: threading.Lock        # Protects _zerodha_data from concurrent writes
    _zerodha_data: dict          # {volume_traded, last_price, open, high, close, low, change,
                                 #  average_traded_price, total_buy_quantity, total_sell_quantity}
    options_live: dict           # {strike: {"CE": {...}, "PE": {...}}}
    options_aggregate: dict      # Aggregated metrics, updated by recompute_options_aggregate()
    futures_live: dict           # {"current": {...}, "next": {...}}

    # Methods mirror Stock's old public interface — Stock façade delegates to these
    def update_zerodha_data(ticker_data)       # Thread-safe; handles 28/32-byte index ticks
    def update_option_tick(strike, opt_type, tick)
    def update_futures_tick(expiry_key, tick)
    def recompute_options_aggregate(spot_price)
    @property zerodha_data -> dict             # Thread-safe copy
```

**Why extracted:** Index ticks (segment 9, 28/32 bytes) lack `volume_traded`, OI, and depth fields. `TickStore.update_zerodha_data()` uses `.get()` with safe defaults. Centralizing this in TickStore keeps all tick-format quirks in one place.

---

## 4. Application Context & Shared State

### `common/shared.py`

```python
class Mode(Enum):
    INTRADAY = "INTRADAY"
    POSITIONAL = "POSITIONAL"

class AppContext:
    stock_token_obj_dict: dict    # {instrument_token: Stock} for equities
    index_token_obj_dict: dict    # {instrument_token: Stock} for indices
    commodity_token_obj_dict: dict
    global_indices_token_obj_dict: dict
    stocks_list: list             # list of stock trading symbols
    index_list: list              # list of index trading symbols
    commodity_list: list
    global_indices_list: list
    stockExpires: list            # F&O expiry dates (seeded from first index with valid expiries)
    mode: Mode                    # Current operating mode
    zd_ticker_manager             # ZerodhaTickerManager reference
    zd_kc                         # KiteConnect instance
    token_registry: TokenRegistry # Central token registry for O(1) tick routing
    signal_bus: SignalBus         # Cross-layer signal event bus (when ENABLE_INTELLIGENCE=1)
    correlator: SignalCorrelator  # Cross-layer confluence detector (when ENABLE_INTELLIGENCE=1)
    narrator: MarketNarrator      # LLM narrative generator (when ENABLE_NARRATOR=1)
    last_equity_tick_time: float  # epoch of last equity WebSocket tick; 0.0 = no tick yet
                                  # Set by ZerodhaTickerManager.on_ticks(); used by /status
    llm_budget_warned: bool       # True once 80% LLM budget alert fires today;
                                  # prevents duplicate alerts; reset at midnight via callback
    options_source: str           # "zerodha" (default) or "sensibull" — live option tick source
    sensibull_feed                # SensibullFeed instance when OPTIONS_SOURCE=sensibull

# Global singleton
app_ctx = AppContext()

# Also global: 52-week high/low lists
```

All modules import `common.shared` and reference `shared.app_ctx` to access the global stock dictionaries, mode, and Zerodha connections.

### `common/helperFunctions.py`

| Function | Purpose |
|----------|---------|
| `get_stock_objects_from_json()` | Reads `final_derivatives_list.json`, returns `(UnderlyingList, IndexList, CommodityList, GlobalIndicesList)` of Stock objects |
| `save_stock_objects_into_json()` | Persists stock list back to JSON |
| `percentageChange(old, new)` | Standard percentage change calculation |
| `isNowInTimePeriod(start, end)` | Check if current time is within a range |

---

## 5. Data Pipeline & Sources

### Data Sources Overview

| Source | Data Type | Module | Usage |
|--------|-----------|--------|-------|
| **Yahoo Finance** (`yfinance`) | OHLCV price data | `intraday_monitor.py` | Primary price data for all stocks |
| **NSE APIs** | Futures, options, bhav copy, FII stats | `nse/nse_derivative_data.py` | Derivatives data, expiry dates, participant OI |
| **Zerodha/KiteConnect** | Option chains, futures, real-time ticks | `zerodha/` | Real-time WebSocket ticks, historical data, option chain |
| **Sensibull** | OI chain, PCR, max pain | Fetched via Stock methods | OI chain analysis, PCR signals, max pain levels |
| **StockEdge API** | FII/DII daily flows | `premarket/premarket_report.py` | Pre-market FII/DII cash + derivatives flows |
| **Google News RSS** | News headlines | `sentiment/news_sentiment_manager.py` | Sentiment analysis via FinBERT |

### NSE Data Module (`nse/nse_derivative_data.py`)

`NSE_DATA_CLASS` provides static methods:

| Method | Data |
|--------|------|
| Live futures data | Intraday futures quotes |
| Historical futures data | Positional (paginated 90-day chunks) |
| Option chain data | Full option chain per expiry |
| Bhav copy | End-of-day settlement data |
| Participant-wise OI | Client/DII/FII/Pro positions |
| FII derivatives statistics | FII futures + options activity |
| F&O ban list | Securities in F&O ban |
| Expiry dates | Current + upcoming expiries |

### NSE Utilities (`nse/nse_utils.py`)

- `nse_urlfetch()`: Session-based HTTP with cookie handling for NSE's anti-bot measures
- Date validation/derivation helpers
- `floor_to_5_min()`: Round timestamps to 5-min boundaries
- NSE calendar integration via `pandas_market_calendars`

---

## 6. Analyzer Framework

### `analyser/Analyser.py`

#### BaseAnalyzer - Decorator Pattern

The framework uses **decorators to register methods** for specific modes and instrument types:

```python
class BaseAnalyzer:
    # Mode decorators
    @staticmethod
    def intraday(func):      # Only run in intraday mode
    @staticmethod
    def positional(func):    # Only run in positional mode
    @staticmethod
    def both(func):          # Run in both modes

    # Instrument type decorators
    @staticmethod
    def index_intraday(func):    # Run for indices in intraday
    @staticmethod
    def index_positional(func):  # Run for indices in positional
    @staticmethod
    def index_both(func):        # Run for indices in both modes
```

Each decorator sets flags on the method:
- `_is_intraday`, `_is_positional` for mode selection
- `_is_index_intraday`, `_is_index_positional` for index-specific methods

**`__init__` auto-discovery**: On initialization, BaseAnalyzer scans all methods on the subclass, collecting those with decorator flags into lists (`self.intraday_methods`, `self.positional_methods`, etc.).

**`reset_constants()`**: Each analyzer overrides this to adjust thresholds per mode (tighter for intraday, wider for positional).

#### AnalyserOrchestrator

Manages the list of all registered analyzers:

```python
class AnalyserOrchestrator:
    analyzers: list[BaseAnalyzer]

    def run_all_intraday(self, stock):
        # For each analyzer, call all intraday_methods on the stock
        # Then call scoring.should_notify() to determine if alert needed

    def run_all_positional(self, stock):
        # Same for positional methods

    def generate_analysis_message(self, stock):
        # Create detailed HTML-formatted Telegram message
        # with emoji indicators per signal type
```

---

## 7. Individual Analyzers

### 7.1 TechnicalAnalyser (`analyser/TechnicalAnalyser.py`, ~63KB)

The largest analyzer with backtested-optimized parameters.

| Method | Signal Type | Key Parameters | Description |
|--------|-------------|----------------|-------------|
| `analyse_rsi` | RSI | upper=85, lower=30 | RSI with zone duration tracking (consecutive bars in overbought/oversold) |
| `analyse_macd` | MACD | fast=12, slow=26, signal=9 | MACD crossover with histogram confirmation |
| `analyse_ema_crossover` | EMA_CROSSOVER | short=9, long=21 | EMA crossover with ADX trend strength gate |
| `analyse_Bolinger_band` | BOLLINGER_BANDS | period=20, std=2.0 | Momentum strategy (not mean-reversion). **Note:** exact method name has capital B — `analyse_Bolinger_band` |
| `analyse_vwap` | VWAP | deviation threshold | VWAP deviation for intraday bias |
| `analyse_atr` | ATR | period=14 | ATR-based volatility assessment |
| `analyse_supertrend` | SUPERTREND | period=14, multiplier=2.5 | Supertrend indicator direction change |
| `analyse_rsi_divergence` | RSI_DIVERGENCE | — | Swing high/low detection with RSI divergence |
| `analyse_stochastic` | STOCHASTIC | K=5, D=5, upper=90, lower=30 | Stochastic oscillator with signal strength classification |
| `analyse_pivot_points` | PIVOT_POINTS | — | Classic pivot point S/R levels |

### 7.2 VolumeAnalyser (`analyser/VolumeAnalyser.py`)

| Method | Signal Type | Description |
|--------|-------------|-------------|
| `analyse_volume_breakout` | VOLUME_BREAKOUT | 2x volume MA with price confirmation + volume trend validation |
| `analyse_obv_divergence` | OBV_DIVERGENCE | Price making LL + OBV making HL (or vice versa) with trend weakening filter |
| `analyse_volume_climax` | VOLUME_CLIMAX | 3x volume spike + close position reversal (exhaustion detection) |

### 7.3 CandleStickPatternAnalyser (`analyser/candleStickPatternAnalyser.py`)

Six pattern detection methods organized by candle count and pattern type:

| Method | Patterns Detected | Requirements |
|--------|-------------------|--------------|
| Single momentum | Marubozu | Strong body, minimal wicks |
| Single reversal | Hammer, Shooting Star | Trend context validation required |
| Double reversal | Engulfing, Piercing, Dark Cloud Cover | Trend context + `ENGULFING_MIN_BODY_RATIO=1.5` |
| Double continuation | 2 consecutive same-direction candles | - |
| Triple reversal | Morning Star, Evening Star | Trend context + `STAR_MAX_BODY_RATIO=0.15` (positional) |
| Triple continuation | 3 consecutive same-direction candles | - |

All reversal patterns call `_get_trend_context()` to validate that the pattern appears in the correct trend direction.

### 7.4 IVAnalyser (`analyser/IVAnalyser.py`)

**Note on method names:** The public methods use the original exact names from the source file.

| Method | Signal Type | Description |
|--------|-------------|-------------|
| `analyse_spike_in_ATM_IV` | IV_SPIKE | IV percentage change exceeds threshold (intraday: 5%, positional: 20%) from Zerodha ATM option `atm_iv_change` field; falls back to comparing last 2 `historical_data` rows |
| `analyse_trend_in_ATM_IV` | IV_TREND | N consecutive rows (default 3) of rising/falling ATM IV from Sensibull `historical_data` |
| `analyse_iv_rank` | IV_RANK / IV_RANK_EXTREME | IVP percentile-based rank. IVP<20 → IV_RANK LOW; IVP>70 → IV_RANK HIGH; IVP>85 → IV_RANK_EXTREME HIGH; IVP<10 → IV_RANK_EXTREME LOW |
| `analyse_iv_vs_hv` | IV_VS_HV | Ratio of current ATM IV to realized historical volatility (HV). Intraday: 50×5m bars, positional: 20-day HV. Ratios: ≥1.3/1.2=ELEVATED, ≥1.6/1.5=EXPENSIVE, ≥2.0=EXTREME |

All IV signals are always classified as **NEUTRAL** (IV is directionally ambiguous). `IV_SPIKE` and `IV_TREND` are excluded from directional score via `NEUTRAL_EXCLUDE_FROM_SCORE`.

### 7.5 PCRAnalyser (`analyser/PCRAnalyser.py`)

Uses Sensibull data for Put-Call Ratio analysis. Data sources differ by method:
- `historical_data` (rolling intraday snapshots) → intraday methods
- `oi_history` (daily rows from compute_intraday 1D) → positional trend/reversal
- `oi_chain` (latest OI chain snapshot) → extreme/bias methods

| Method | Signal Type | Mode | Logic |
|--------|-------------|------|-------|
| `analyse_pcr_extreme_zones` | PCR_EXTREME | @both | Contrarian: PCR < 0.3 = Bullish, PCR > 1.5 = Bearish |
| `analyse_pcr_directional_bias` | PCR_BIAS | @both | PCR < 0.5 = Bearish, PCR > 1.2 = Bullish (WEAK/MODERATE/STRONG tiers) |
| `analyse_pcr_trend` | PCR_TREND | @positional | 5-day window from `oi_history`; OI must change >= 8% and >= 0.08 absolute |
| `analyse_pcr_intraday_trend` | PCR_INTRADAY_TREND | @intraday | Multi-snapshot PCR trend from `oi_chain_history`; min 3 snapshots, >= 5% change |
| `analyse_pcr_divergence` | PCR_DIVERGENCE | @both | Near vs far expiry PCR diff > 0.35 |
| `analyse_pcr_reversal` | PCR_REVERSAL | @intraday | Zone crossover or trend direction reversal; min 4 snapshots |
| `analyse_pcr_pos_reversal` | PCR_POS_REVERSAL | @positional | Multi-day PCR reversal from `oi_history`; min 6 daily rows, 3-day average comparison |

### 7.6 MaxPainAnalyser (`analyser/MaxPainAnalyser.py`)

Uses Sensibull pre-calculated max pain data.

| Method | Signal Type | Logic |
|--------|-------------|-------|
| `analyse_max_pain_deviation` | MAX_PAIN | Price deviation from max pain strike. 12-day expiry proximity gate. MODERATE (2-5%) or STRONG (>5%) |
| `analyse_max_pain_trend` | MAX_PAIN_TREND | Converging (price moving toward max pain) or diverging analysis from historical snapshots |
| `analyse_max_pain_alignment` | MAX_PAIN_ALIGNMENT | Cross-validates max_pain_type vs pcr_type for signal confidence |

### 7.7 OIChainAnalyser (`analyser/OIChainAnalyser.py`, ~68KB)

Per-strike OI chain analysis from Sensibull. Uses `sensibull_ctx["oi_chain"]` and `oi_chain_history` for intraday methods; `prev_call_oi` / `prev_put_oi` in `per_strike_data` for positional overnight-comparison methods.

| Method | Signal Type | Mode | Description |
|--------|-------------|------|-------------|
| `analyse_oi_support_resistance` | OI_SUPPORT_RESISTANCE | @both | Identifies S/R from OI concentration; requires proximity ≤1.0% (intraday) / 1.5% (positional); strike OI ≥ 1.5× average |
| `analyse_oi_buildup` | OI_BUILDUP | @both | Fresh writing detection; call/put OI ratio ≥ 2.5×; min 3 significant strikes; total OI change ≥ 3% (intraday) / 5% (positional) |
| `analyse_oi_wall` | OI_WALL | @both | Statistical outlier: OI > mean + 1.8×std (intraday) / 2.0×std (positional); within 3%/5% of price |
| `analyse_oi_shift` | OI_SHIFT | @both | Tracks movement of OI concentration; 4×/5× imbalance ratio required |
| `analyse_intraday_oi_trend` | OI_INTRADAY_TREND | @intraday | Multi-snapshot PCR trend; min 5 snapshots; PCR change ≥ 8%; single-side OI change ≥ 5% |
| `analyse_intraday_oi_sr_shift` | OI_SR_SHIFT | @intraday | S/R level migration; min 5 snapshots; must shift ≥ 2 strike widths |
| `analyse_oi_capitulation` | OI_CAPITULATION | @positional | Institutional OI unwinding; uses `prev_call_oi`/`prev_put_oi`; strike OI drop ≥ 30% AND ≥ 50K contracts; min 2 qualifying strikes; ≥ 8% of that side's total OI; within ±8% of spot |
| `analyse_oi_wall_migration` | OI_WALL_MIGRATION | @positional | Overnight wall migration; detects ≥ 1 strike width movement using `prev_*_oi` for yesterday's wall vs today's; guarded against expiry-day total OI drop >80% |
| `analyse_positional_oi_trend` | OI_POSITIONAL_TREND | @positional | Multi-day call/put OI build-up from `oi_history`; look-back 5 days; one side must grow ≥ 15%; leading side must exceed lagging by ≥ 10% |
| `analyse_oi_acceleration` | OI_ACCELERATION | @positional | Sudden 2×+ jump in daily OI writing velocity from `oi_history`; recent 3-day velocity ≥ 2× prior 3-day; recent mean daily change ≥ 2M contracts; prior base ≥ 500K |

### 7.8 FuturesAnalyser (`analyser/Futures_Analyser.py`)

Enhanced with dynamic thresholds, multi-timeframe analysis, and new positional/intraday methods.

| Method | Signal Type | Mode | Description |
|--------|-------------|------|-------------|
| `analyse_intraday_check_future_action` | FUTURE_ACTION | @both | Detects long_buildup, short_buildup, short_covering, long_unwinding from `futures_data["current"]` and `"next"` DataFrames |
| `analyse_intraday_price_volume_oi_pattern` | FUTURE_PVO_PATTERN | @both | Price-Volume-OI pattern detection (10 patterns including directional and divergence) |
| `analyse_intraday_breakout_oi_confirmation` | FUTURE_BREAKOUT_PATTERN | @intraday | Opening Range Breakout (ORB candles 3–5) with OI + volume confirmation; fires once per direction per session |
| `analyse_positional_oi_trend` | FUTURE_OI_TREND | @positional | Multi-day OI buildup/unwinding trend using 10-day and 20-day windows on the 55-row positional dataset |
| `analyse_positional_cost_of_carry` | FUTURE_COST_OF_CARRY | @both | Basis (futures-spot); BACKWARDATION (<-0.05%) valid in both modes; HIGH_COST_OF_CARRY (ann CoC >15%) and BASIS_EXPANDING positional only |
| `analyse_positional_rollover_pressure` | FUTURE_ROLLOVER | @positional | curr/next OI ratio detection; ROLLOVER_ACTIVE (<2x), ROLLOVER_STARTING (2–4x + falling trend) |
| `analyse_intraday_oi_buildup_from_open` | FUTURE_OI_FROM_OPEN | @intraday | Compares current OI vs session-open OI (threshold: ≥1.5%); session-open OI cached in `_session_open_oi` |

**Session State Flags** (class-level, reset on mode change):

| Flag | Purpose |
|------|---------|
| `_orb_fired_up` | Prevents ORB upside from re-firing every cycle |
| `_orb_fired_down` | Prevents ORB downside from re-firing every cycle |
| `_orb_open_time_warned` | Logs only once if first bar is not at 09:15 |
| `_session_open_oi` | Cached session-open OI for OI-from-open analysis |
| `_session_date` | Date string to detect new session and reset `_session_open_oi` |
| `_last_reset_mode` | Prevents repeated `reset_constants()` calls in same mode |

**Dynamic Thresholds** (computed per stock per call):
- ATR-based price threshold: `max(ATR% × 0.5, MIN_PRICE_THRESHOLD)`
- OI-volatility-based threshold: `max(oi_std × 1.5, MIN_OI_THRESHOLD)`, with:
  - Intraday: floored at `base × 0.5`, no cap
  - Positional: floored at `base`, capped at `base × 3` (prevents overloose std suppressing real moves)
- Startup noise filter: OI rows below 5% of contract max are skipped before computing std

**6-Component Signal Score** (0–100 internal score, determines FUTURE_SIGNAL_SCORE_* weight used):

| Component | Max pts | Source |
|-----------|---------|--------|
| OI confirmation | 20 | OI change > threshold |
| Volume confirmation | 20 | Volume > 1.2× rolling average |
| Multi-timeframe alignment | 15 | Short (6 candles) + medium (18 candles) trend aligned |
| Momentum | 15 | RSI-like momentum + ROC |
| Time filter | 10 | Market open (0), lunch (5), close (7.5), prime hours (10) |
| Risk/reward | 20 | ORB range vs distance past level; or price+OI magnitude |

**Mode thresholds** (set by `reset_constants()`):

| Parameter | Intraday | Positional |
|-----------|----------|------------|
| `FUTURE_PRICE_CHANGE_PERCENTAGE` | 0.15% | 1.0% |
| `FUTURE_OI_INCREASE_PERCENTAGE` | 0.10% | 3.0% |
| `ORB_CANDLES` | 3 | 3 (unused) |
| `SHORT_TERM_CANDLES` | 6 (30 min) | 5 (1 week) |
| `MEDIUM_TERM_CANDLES` | 18 (90 min) | 15 (3 weeks) |

---

## 8. Scoring & Notification System

### `common/scoring.py`

The scoring engine replaces the legacy `NoOfTrends` count with a weighted, multi-factor system.

#### Score Calculation Flow

```
stock.analysis dict
    |
    v
calculate_score(analysis)
    |
    |--> For each signal in BULLISH/BEARISH/NEUTRAL:
    |       weight = ANALYSIS_WEIGHTS[signal_type]
    |       Apply diminishing returns (30% per duplicate type)
    |       Sum into base_score per sentiment
    |
    |--> calculate_alignment_bonus()
    |       Cross-category confirmation:
    |         TECHNICAL + OPTIONS = 1.5x bonus
    |         TECHNICAL + FUTURES = 1.3x bonus
    |         OPTIONS + FUTURES = 1.4x bonus
    |
    |--> total_score = base_score + alignment_bonus
    |
    v
ScoreResult(
    total_score,
    base_score,
    alignment_bonus,
    priority,           # LOW/MEDIUM/HIGH/CRITICAL
    dominant_sentiment, # BULLISH or BEARISH
    confidence_pct      # % alignment of signals
)
```

#### Signal Categories

```python
TECHNICAL_ANALYSES = {
    "RSI", "MACD", "EMA_CROSSOVER", "BOLLINGER_BANDS",
    "VWAP", "SUPERTREND", "RSI_DIVERGENCE", "STOCHASTIC",
    "OBV_DIVERGENCE", "PIVOT_POINTS", "VOLUME_BREAKOUT",
    "VOLUME_CLIMAX", "CANDLESTICK_*", ...
}

OPTIONS_ANALYSES = {
    "PCR_EXTREME", "PCR_BIAS", "PCR_TREND", "PCR_REVERSAL",
    "MAX_PAIN", "MAX_PAIN_TREND", "MAX_PAIN_ALIGNMENT",
    "OI_SUPPORT_RESISTANCE", "OI_BUILDUP", "OI_WALL",
    "OI_SHIFT", "OI_TREND", "IV_SPIKE", "IV_TREND", ...
}

FUTURES_ANALYSES = {
    "FUTURES_ACTION", "PVO", "ORB", ...
}
```

#### Priority Tiers

| Priority | Score Threshold | Meaning |
|----------|----------------|---------|
| LOW | >= 35 | Weak signal cluster |
| MEDIUM | >= 60 | Moderate confirmation |
| HIGH | >= 90 | Strong multi-factor confirmation |
| CRITICAL | >= 130 | Very strong aligned signals |

#### Notification Gate

```python
def should_notify(score_result):
    return (
        score_result.total_score >= MIN_NOTIFICATION_SCORE  # 110 intraday
        and score_result.confidence_pct >= 65               # 65% alignment gate
    )
# Positional mode uses MIN_NOTIFICATION_SCORE_POSITIONAL = 150
# (EOD runs on 50+ stocks; near-expiry week inflates scores mechanically;
#  150 requires genuine cross-category alignment: futures + options + technical)
```

#### Composite Setup Override (Winner-Takes-All)

`OptionSellerCompositeAnalyser` runs last and cross-reads `stock.analysis` populated by all 9 preceding analysers. When a composite setup fires it:

1. Writes `stock.analysis["NEUTRAL"]["GAMMA_TRAP"]` / `RANGE_BOUND_SETUP` / `SKEW_FADE_SETUP` namedtuple.
2. Sets `stock.analysis["PRIORITY_OVERRIDE"] = NotificationPriority.CRITICAL/HIGH` — **score gate is bypassed**.
3. `generate_analysis_message()` detects `PRIORITY_OVERRIDE` and returns **only** the composite trade card; all individual RSI/MACD/PCR/etc. cards are suppressed.

Priority of composite keys (highest urgency rendered first):
- `GAMMA_TRAP` → CRITICAL (kill-switch: close short positions, directional breach)
- `SKEW_FADE_SETUP` → HIGH (directional credit spread: fade a panic at an OI wall)
- `RANGE_BOUND_SETUP` → HIGH (Iron Condor / Strangle: range-trapped + overpriced vol)

Weights in `ANALYSIS_WEIGHTS` for all three are intentionally `0` so they never inflate batch scores.

### `common/constants.py` - ANALYSIS_WEIGHTS

Contains ~60 signal types with weights. See `DATA_SCHEMA.md` Section 12 for the full dict. Key values:

```python
ANALYSIS_WEIGHTS = {
    # Technical
    "RSI": 15, "MACD": 15, "SUPERTREND": 15, "RSI_DIVERGENCE": 18,
    # Candlestick (backtest-optimised)
    "Double_candle_stick_pattern": 18,  # Engulfing — most reliable
    "Single_candle_stick_pattern": 6,   # Marubozu — reduced, not reliable
    # Futures
    "FUTURE_ACTION_LONG_BUILDUP": 16, "FUTURE_BREAKOUT_MTF_ALIGNED": 20,
    "FUTURE_OI_TREND": 16, "FUTURE_OI_FROM_OPEN": 15,
    # OI Chain
    "OI_ACCELERATION": 17, "OI_CAPITULATION": 16, "OI_POSITIONAL_TREND": 16,
    # Panic composite
    "PANIC_MODE": 22, "PANIC_EXHAUSTION": 25,
    # Composite option-seller setups — weight=0: bypass score gate via PRIORITY_OVERRIDE
    "GAMMA_TRAP": 0, "RANGE_BOUND_SETUP": 0, "SKEW_FADE_SETUP": 0,
}

NEUTRAL_EXCLUDE_FROM_SCORE = {
    "MAX_PAIN_ALIGNMENT",    # When DIVERGENT
    "MAX_PAIN_TREND",        # When DIVERGING
    "OI_SUPPORT_RESISTANCE", # Informational S/R
    "OI_SR_SHIFT",           # Informational range
    "FUTURE_ROLLOVER",       # Context only
    "RANGE_BOUND_SETUP",     # Composite — score bypassed via PRIORITY_OVERRIDE
    "SKEW_FADE_SETUP",
    "GAMMA_TRAP",
    "GAMMA_TRAP_ACTIVE",     # Boolean suppression flag set by Gamma Trap
}

MIN_NOTIFICATION_SCORE = 110              # intraday default
MIN_NOTIFICATION_SCORE_POSITIONAL = 150   # EOD positional (higher bar for 50+ stock run)
```

---

## 9. Intraday Monitor (Main Entry Point)

### `intraday/intraday_monitor.py` (~55KB)

This is the production entry point that orchestrates the entire system.

#### Registered Analyzers (current state)

All 10 analysers are active. Registration order matters — `PanicModeAnalyser` must run before `OptionSellerCompositeAnalyser` because the composite analyser reads `PANIC_EXHAUSTION` from `stock.analysis`.

```python
orchestrator.register(VolumeAnalyser())
orchestrator.register(TechnicalAnalyser())
orchestrator.register(CandleStickAnalyser())
orchestrator.register(IVAnalyser())
orchestrator.register(FuturesAnalyser())
orchestrator.register(PCRAnalyser())
orchestrator.register(MaxPainAnalyser())
orchestrator.register(OIChainAnalyser())
orchestrator.register(PanicModeAnalyser())                  # 9th — reads all preceding
orchestrator.register(OptionSellerCompositeAnalyser())      # 10th — MUST be last
```

| Order | Analyser | Signal categories | Mode |
|---|---|---|---|
| 1 | VolumeAnalyser | VOLUME, VOLUME_BREAKOUT, OBV_DIVERGENCE, VOLUME_CLIMAX | both |
| 2 | TechnicalAnalyser | RSI, MACD, EMA_CROSSOVER, SUPERTREND, RSI_DIVERGENCE, STOCHASTIC, BOLLINGERBAND, VWAP, PIVOT_POINTS | both |
| 3 | CandleStickAnalyser | Single/Double/Triple candle patterns | both |
| 4 | IVAnalyser | IV_SPIKE, IV_TREND, IV_RANK, IV_RANK_EXTREME, IV_PREMIUM | both |
| 5 | FuturesAnalyser | FUTURE_ACTION, FUTURE_PVO_PATTERN, FUTURE_BREAKOUT_PATTERN, FUTURE_OI_TREND, FUTURE_COST_OF_CARRY, FUTURE_ROLLOVER, FUTURE_OI_FROM_OPEN | both/positional/intraday |
| 6 | PCRAnalyser | PCR_EXTREME, PCR_BIAS, PCR_TREND, PCR_INTRADAY_TREND, PCR_REVERSAL, PCR_POS_REVERSAL, PCR_DIVERGENCE | both |
| 7 | MaxPainAnalyser | MAX_PAIN, MAX_PAIN_TREND, MAX_PAIN_ALIGNMENT | both |
| 8 | OIChainAnalyser | OI_SUPPORT_RESISTANCE, OI_BUILDUP, OI_WALL, OI_SHIFT, OI_INTRADAY_TREND, OI_SR_SHIFT, OI_CAPITULATION, OI_WALL_MIGRATION, OI_POSITIONAL_TREND, OI_ACCELERATION | both |
| 9 | PanicModeAnalyser | PANIC_MODE, PANIC_EXHAUSTION | both — reads all 8 above |
| 10 | OptionSellerCompositeAnalyser | GAMMA_TRAP, RANGE_BOUND_SETUP, SKEW_FADE_SETUP | both — reads all 9 above; sets PRIORITY_OVERRIDE |

#### Production Daily Timeline

The monolith runs 24/7 as an always-on systemd service (`Restart=always`). It self-schedules the entire daily flow via `_run_daily_loop()`:

```
00:00     - Monolith idle (overnight). Redis keeps yesterday's positional data.
            data-gateway idle. notification-service draining stream.

09:00 AM  - Pre-market phase:
              auth-service: scheduled TOTP login → publishes enctoken to Redis auth:zerodha
              auth-service: also runs Sensibull OAuth login → publishes access_token to Redis auth:sensibull
              monolith: reads enctoken from Redis (auth:zerodha hash) on startup
              data-gateway: prevDayOHLCV daily refresh (all stocks/indices/commodities/global)
                → uses _get_prev_day_row() helper: checks if last bar date == today
                  (before open: iloc[-1] = yesterday's bar; during market: iloc[-2] = yesterday)
              run_global_cues_report() — sector, FII/DII, F&O OI, NSE indices
09:07 AM  - run_preopen_report() — NSE pre-open session
09:10 AM  - update_morning_bias() — extracts positional signals from 8 PM results
              (fast path: ~0.5s, zero HTTP; falls back to full recompute if restarted)
09:15 AM  - Intraday loop begins (data-gateway starts fetching 5m + Sensibull)
            |
            +--> Every ~310 seconds:
            |      1. _wait_for_cycle_ready() — blocks for data-gateway Pub/Sub signal
            |      2. load_price_data_from_redis() — sub-millisecond HGETALL
            |      3. monitor() per stock (20 ThreadPoolExecutor workers):
            |         a. load_sensibull_from_redis() — OI chain, PCR, max pain
            |         b. load_zerodha_from_redis() — futures data (or FuturesFetcher fallback)
            |         c. orchestrator.run_all_intraday(stock)
            |         d. Score signals via scoring engine
            |         e. If should_notify() -> XADD notification:jobs stream
            |      4. Reports: top gainers/losers, index, commodity, global indices
            |
03:30 PM  - Market closes, intraday loop stops
            Data-gateway switches to idle (positional fetch at 19:00)
07:00 PM  - Data-gateway: single positional fetch (2y daily + Sensibull positional)
            → publishes cycle_ready
08:00 PM  - Positional analysis:
            |-- _wait_for_cycle_ready() — data-gateway already published at 19:00
            |-- load_price_data_from_redis() — 2y daily bars
            |-- Run all positional analyzer methods
            |-- Score and notify
            |-- Reports: movers, index, commodity, global, 52W high/low
            |-- Run post-market analysis pipeline (sector, FII/DII, F&O OI)
            |-- LLM narrator: EOD market briefing
            |-- stock.analysis + stock.priceData RETAINED in memory (not reset)
08:30 PM  - Monolith idle. Redis keeps positional data for tomorrow's morning bias.
```

#### CLI Arguments
```
--stock       Run for specific stock(s)
--index       Run for index(es)
--commodity   Run for commodity
--global-index Run for global indices
```

#### Parallel Execution
Uses `ThreadPoolExecutor` to analyze multiple stocks concurrently:
```python
with ThreadPoolExecutor(max_workers=N) as executor:
    futures = {executor.submit(analyze_stock, stock): stock
               for stock in stocks}
```

#### Data Fetching (Data-Gateway → Redis → Monolith)

All price and Sensibull data is fetched by the **data-gateway** service and published to Redis hashes. The monolith reads from Redis (sub-millisecond) — no direct HTTP calls to yfinance or Sensibull in the monolith:

```python
# Data-gateway fetches (parallel, 10 workers for Sensibull):
#   yf.download(5m bars) → HSET data:price:{SYMBOL}
#   Sensibull insights + OI chain → HSET data:sensibull:{SYMBOL}
#   PUBLISH data:cycle_ready  (Pub/Sub — instant notification to monolith)

# Monolith reads from Redis:
load_price_data_from_redis(redis_proxy, stock_objs, index_objs, ...)
load_sensibull_from_redis(redis_proxy, stock)

# Futures data — fetched by data-gateway's ZerodhaFuturesManager → Redis
# Monolith reads from Redis:
load_price_data_from_redis(redis_proxy, stock_objs, index_objs, ...)
load_sensibull_from_redis(redis_proxy, stock)
load_zerodha_from_redis(redis_proxy, stock)  # futures data from data:data_gateway → data:zerodha:*
```

#### LiveStockEngine Integration

When `ENABLE_INTELLIGENCE=1`, a `LiveStockEngine` is injected into the `ZerodhaTickerManager` for per-tick equity signal generation:

```python
if shared.app_ctx.signal_bus:
    zd_ticker_manager.live_stock_engine = LiveStockEngine(shared.app_ctx.signal_bus)
```

Signals: VWAP cross, bid/ask imbalance, Opening Range Breakout (ORB), day high/low break.

#### Zerodha WebSocket Integration
- WebSocket connects at startup for real-time tick data
- Ticks update `stock.ltp` and derivatives context
- Optional Telegram bot listener runs in a separate thread

#### Observability — 3-Layer System

Three lightweight reliability layers are injected at module load time (before any class definitions) and inside the intraday loop. See [Section 21](#21-observability-system) for full detail.

| Layer | Function | Trigger |
|-------|----------|---------|
| 1 — Crash Handler | `_crash_handler()` + `sys.excepthook` | Any uncaught exception |
| 2 — Heartbeat | `_ping_healthcheck()` | End of every `intraday_analysis()` loop iteration |
| 3 — Zombie Watchdog | `check_data_freshness(stock)` | Per-stock inside loop, gated by `ENABLE_LIVE_OPTIONS` |

---

## 10. Pre-Market Reports

### `premarket/premarket_report.py`

`PreMarketReport` class generates a comprehensive morning briefing:

#### Data Collected
1. **Global Indices**: US (S&P 500, NASDAQ, Dow), Europe (FTSE, DAX), Asia (Nikkei, Hang Seng, Shanghai) - single `yf.download()` call
2. **Bond Yields**: US 10Y, 2Y, India 10Y - yield curve inversion detection
3. **Commodities**: Gold, Silver, Crude Oil, Natural Gas
4. **Currencies**: USD/INR, DXY (Dollar Index)
5. **India VIX**: Regime classification (Low/Normal/Elevated/High/Extreme)
6. **FII/DII Flows**: From StockEdge API (parallel fetch)
7. **NSE Pre-Open Session**: 9:00-9:08 AM pre-open auction data

#### Actionable Signals Generated
- Crude oil surge (>2% move)
- Rupee weakening (>0.5% depreciation)
- 10Y yield threshold breaches
- VIX regime changes
- Bond yield curve inversion warning

#### Holiday Warning Banner (NEW)
`PreMarketReport._build_holiday_warning_banner()` is called automatically inside
`generate_global_report()` and, if any NSE trading holidays are detected in the
next 7 days, prepends a high-visibility HTML banner to the morning Telegram
message warning options traders about accelerated Theta (Θ) decay.

- Delegates to `common.market_calendar.get_upcoming_holidays(days_ahead=7)`
- Empty list → sentinel `""` returned; normal report sent unchanged
- Holidays found → banner with formatted dates + Theta decay guidance prepended

#### Fail-Fast Gatekeeper (NEW)
`run_global_cues_report()` checks `is_trading_day()` before fetching any data.
On a market holiday in production mode it calls `sys.exit(0)` immediately.

---

## 11. Post-Market Analysis

### Architecture (Registry Pattern)

```
post_market_analysis/
|-- base.py                 # Abstract PostMarketSource
|-- registry.py             # SOURCE_CLASSES list
|-- runner.py               # Pipeline orchestrator
|-- summary.py              # HTML formatters
|-- analysis.py             # Data analyzer/dispatcher
|-- fii_dii.py              # FiiDiiActivitySource
|-- fo_participant_oi.py    # FoParticipantOISource
|-- index_returns.py        # IndexReturnsSource
|-- sector_performance.py   # SectorPerformanceSource
```

### Pipeline Flow
```
runner.py: load_sources()
    |
    v
For each source in SOURCE_CLASSES:
    source.run()  -->  fetch_raw()  -->  normalize()
    |
    v
analyzer.dispatch(normalized_data)
    |
    v
summary.build()  -->  Formatted Telegram messages
```

### Registered Sources

| Source | Data |
|--------|------|
| `SectorPerformanceSource` | NSE sector indices performance |
| `FiiDiiActivitySource` | FII/DII cash + derivatives flows |
| `FoParticipantOISource` | Client/DII/FII/Pro OI breakdown |
| `IndexReturnsSource` | All NSE index returns |

### Summary Formatters (`summary.py`)

Each source has a dedicated HTML formatter for Telegram:

| Formatter | Output |
|-----------|--------|
| `FiiDiiSummaryFormatter` | 5-day table with cash/derivatives flows, green/red dots |
| `SectorSummaryFormatter` | Top 5 gaining/losing sectors with market cap |
| `FoParticipantOISummaryFormatter` | Client/DII/FII/Pro net OI table |
| `IndexReturnsSummaryFormatter` | Top 10 gaining/losing indices |

All formatters enforce a 3900-character Telegram message limit.

---

## 12. Zerodha Integration

### Architecture

```
zerodha/
|-- zerodha_connect.py    # REST API (modified KiteConnect)
|-- zerodha_analysis.py   # WebSocket manager (ZerodhaTickerManager)
|-- zerodha_ticker.py     # WebSocket client (Twisted/Autobahn)
|-- tick_store.py         # TickStore — live tick state container (extracted from Stock)
|-- futures_fetcher.py    # FuturesFetcher — Kite historical futures fetcher (extracted from Stock)
|-- live_options_engine.py # LiveOptionsEngine — per-tick options analysis coordinator
|-- live_stock_engine.py  # LiveStockEngine — per-tick equity signals (VWAP, ORB, imbalance)
|-- zerodha_exceptions.py # Custom API exception types
```

### `zerodha/futures_fetcher.py` - FuturesFetcher

Extracted from `Stock.get_futures_data_for_stock()` to separate HTTP/network concerns from the data model.

```python
class FuturesFetcher:
    def __init__(self, kite_connect): ...

    def fetch(stock, mode="positional", is_next_expiry_required=False) -> Tuple[DataFrame, DataFrame]:
        # mode="positional": daily OHLCV+OI, 90-day lookback (continuous=False, no rollover artifacts)
        #                    always fetches next expiry too (for rollover analysis)
        #                    ~55 clean rows after 5% startup noise filter
        # mode="intraday":   5-min OHLCV+OI, today only — appends 1 row per cycle
        #                    accumulates through the session in futures_data["current"]
        # Both modes populate underlying_price from stock.priceData daily closes (spot_map)
        # Reads/writes stock.zerodha_ctx["futures_data"]["current"] and "next" DataFrames
```

### `zerodha/live_stock_engine.py` - LiveStockEngine

Per-tick equity analysis for VWAP, ORB, and bid/ask imbalance signals. Attached when `ENABLE_INTELLIGENCE=1`:

```python
class LiveStockEngine:
    IMBALANCE_BULLISH = 2.5    # buy_qty / sell_qty > 2.5
    IMBALANCE_BEARISH = 0.4    # buy_qty / sell_qty < 0.4
    SIGNAL_COOLDOWN = 300      # 5 min between same signal per symbol
    TICK_INTERVAL = 5          # throttle: max once per 5s per symbol

    def on_tick(stock):
        # Emits Signal objects to SignalBus (Layer.LIVE)
        # Signal types: VWAP_CROSS, BID_ASK_IMBALANCE, ORB_BREAKOUT, DAY_HIGH_LOW_BREAK
```

### `fno/sensibull_fetcher.py` - SensibullFetcher (legacy / dev mode)

Extracted from `Stock.fetch_sensibull_data()` and `fetch_sensibull_oi_chain()`. Used in dev mode only — production uses `services/data_gateway/sensibull_fetcher.py`.

### `services/data_gateway/sensibull_fetcher.py` - Parallel Sensibull Fetcher (production)

Fetches Sensibull data for all symbols in parallel (10 workers) and publishes to Redis hashes (`data:sensibull:{symbol}`). **Requires authenticated cookies** (access_token + client_info) since Sensibull locked down their REST APIs in July 2026.

**Cookie auth**: Reads `access_token` + `client_info` from Redis hash `auth:sensibull` (auto-refreshed by auth-service) first, falls back to env vars `SENSIBULL_ACCESS_TOKEN` / `SENSIBULL_CLIENT_INFO`. All API calls include `Origin: https://web.sensibull.com` + `Referer` headers.

**4 API endpoints**:
- `stock_info`: `GET /v1/compute/cache/stock_info?tradingsymbol={symbol}` — per-expiry ATM IV, IV percentile, max pain, PCR, volume spike, future price (renamed from `/cache/insights/stock_info`)
- `oi_chart`: `POST /v1/compute/1/oi_graphs/oi_chart` — per-strike OI (call_oi, put_oi, prev_call_oi, prev_put_oi) + PCR + ATM strike
- `iv_chart`: `GET /v1/compute/iv_chart/{symbol}` — 2-year daily ATM IV history
- `compute_intraday`: `POST /v1/compute/compute_intraday` — ~181 daily OI/PCR/max_pain history rows

**Fallback**: If `stock_info` returns 404 (endpoint unavailable), reconstructs insights from OI chain (auto-discovers expiries with empty map) + IV chart. Computes max_pain from per-strike OI, IV percentile from 2-year IV history.

```python
class SensibullFetcher:
    def fetch_data(stock, mode="positional") -> Optional[DataFrame]:
        # Fetches insights (underlying_info, stats, per_expiry_map, nse_stats)
        # Stores in stock.sensibull_ctx["current"] and "historical_data"
        # historical_data: rolling 30 rows (positional) or 5-day window (intraday)

    def fetch_oi_chain(stock, mode="positional") -> Optional[dict]:
        # Fetches per-strike OI chain (requires fetch_data() first for expiry selection)
        # Stores in stock.sensibull_ctx["oi_chain"] and "oi_chain_history"
        # oi_chain_history: max 15 snapshots (intraday), single entry (positional)

    def fetch_iv_chart(stock) -> Optional[DataFrame]:
        # Fetches 2yr daily ATM IV history (fetch-once guard per day)
        # Stores in stock.sensibull_ctx["iv_chart_history"] [date, iv_close, price_close]
        # Called in positional mode only

    def fetch_oi_history(stock) -> Optional[DataFrame]:
        # Fetches ~181 daily OI rows from compute_intraday 1D (fetch-once guard per day)
        # Requires fetch_data() first for expiry selection
        # Stores in stock.sensibull_ctx["oi_history"]
        # [date, spot, call_oi, put_oi, futures_oi, call_oi_change, put_oi_change,
        #  future_oi_change, pcr, max_pain]
        # Used by PCRAnalyser (pcr_trend, pcr_pos_reversal) and OIChainAnalyser (positional methods)
        # Called in positional mode only
```

### `zerodha_connect.py` - Modified KiteConnect

Supports **enctoken authentication** (in addition to standard api_key + access_token):
```python
# Authorization header: "enctoken {token}" instead of "token api_key:access_token"
self.enc_token used in headers
```

**Dual root URI** (as of July 2026 — Zerodha deprecated enctoken on api.kite.trade):
```python
_default_root_uri = "https://kite.zerodha.com/oms"     # authenticated REST calls (historical_data, profile, quote)
_public_root_uri = "https://api.kite.trade"            # public instruments() endpoint (no auth, not on /oms)
```

Full REST API coverage: orders, positions, holdings, historical data, quotes, GTT, mutual funds.

### `zerodha_analysis.py` - ZerodhaTickerManager

Manages the WebSocket connection lifecycle:

```python
class ZerodhaTickerManager:
    def on_connect(ws, response):
        # Subscribe equity + index + commodity base tokens
        # When ENABLE_LIVE_OPTIONS: skip equity tokens (index-only mode)
        ws.subscribe(list(all_tokens))
        ws.set_mode(ws.MODE_QUOTE, list(all_tokens))

    def _route_tick(tick):
        # O(1) lookup via TokenRegistry
        info = token_registry.lookup(token)
        if info.type == TokenType.OPTION:   _process_option_tick(tick, info)
        elif info.type == TokenType.INDEX:  _process_equity_tick(tick, info)
        elif info.type == TokenType.EQUITY: _process_equity_tick(tick, info)
        elif info.type == TokenType.FUTURE: _process_future_tick(tick, info)

    def subscribe_options_for_symbol(symbol, spot):
        # Zone-based option subscription via TokenRegistry
        # CORE (ATM ±1%): MODE_FULL, ACTIVE (1-3%): MODE_FULL, PERIPHERAL (3-5%): MODE_QUOTE
```

Features:
- Tick queue with dedicated processor thread
- Auto-reconnection with exponential backoff
- Typed tick routing via `TokenRegistry` (replaces flat dict lookup)
- `LiveOptionsEngine` integration for per-tick signal analysis
- Dynamic re-centering: unsubscribes far strikes, subscribes new ones when spot moves ≥ 50 pts

### `common/token_registry.py` - TokenRegistry (NEW)

Central O(1) routing registry mapping `instrument_token → TokenInfo`:

```python
class TokenType(Enum):
    EQUITY, INDEX, OPTION, FUTURE, COMMODITY, GLOBAL_INDEX

class OptionZone(Enum):
    CORE        # ATM ± 1%   — 18 tokens, MODE_FULL
    ACTIVE      # 1–3% away  — 36 tokens, MODE_FULL
    PERIPHERAL  # 3–5% away  — 40 tokens, MODE_QUOTE

@dataclass
class TokenInfo:
    token: int
    type: TokenType
    symbol: str           # parent symbol e.g. "NIFTY"
    strike: float         # options only
    option_type: str      # "CE" or "PE"
    expiry: date
    zone: OptionZone

class TokenRegistry:
    def register(token, info): ...
    def lookup(token) -> TokenInfo: ...      # O(1)
    def calculate_zones(symbol, spot): ...   # assigns CORE/ACTIVE/PERIPHERAL
    def initial_subscribe_options(symbol, spot) -> dict[zone, list[token]]: ...
    def recenter_and_get_subscription_changes(symbol, new_spot): ...
    def round_to_strike(spot, gap) -> float: ...  # math.isfinite() guarded
```

**Zone-to-WebSocket mode mapping:**
```python
ZONE_TO_WS_MODE = {
    OptionZone.CORE:       MODE_FULL,   # OI + Volume + Depth
    OptionZone.ACTIVE:     MODE_FULL,   # OI + Volume + Depth
    OptionZone.PERIPHERAL: MODE_QUOTE,  # Volume only
}
```

**Index tick format note:** Index ticks (segment 9) are only 28/32 bytes — they carry `last_price`, `ohlc`, `change` but **no** `volume_traded`, `total_buy_quantity`, `total_sell_quantity`, or `oi`. `update_zerodha_data()` uses `.get()` with safe defaults to handle this.

### `zerodha_ticker.py` - Custom KiteTicker

Built on **Twisted/Autobahn** WebSocket framework:
- Binary message parsing for tick data (LTP/Quote/Full packet modes)
- 8-byte LTP, 28/32-byte index quote/full, 44-byte equity quote, 184-byte equity full
- Handles NSE's binary tick format
- Reconnection logic built-in
- **500 instruments per connection limit** (Kite web endpoint)

---

## 13. Live Options Tracking

### Overview

When `ENABLE_LIVE_OPTIONS=1`, the system subscribes to NIFTY/BANKNIFTY weekly option chains via Zerodha WebSocket and runs continuous real-time analysis. This is a **separate mode** from the 5-min intraday analyser — it operates at tick speed (milliseconds).

### Architecture

```
Zerodha WebSocket tick
        |
        v
ZerodhaTickerManager._route_tick()
        |
        +-- TokenType.INDEX  → update spot price
        +-- TokenType.OPTION → stock.update_option_tick()
        |                      stock.recompute_options_aggregate()  [throttled 1s]
        |                      LiveOptionsEngine.on_option_tick()
        +-- TokenType.FUTURE → stock.update_futures_tick()
```

### Zone-Based Subscription

```
NIFTY spot: 23150
CORE     (ATM ± 1%):  22920–23380  →  18 tokens  MODE_FULL
ACTIVE   (1–3% away): 22650–22900  →  36 tokens  MODE_FULL
                       23400–23650
PERIPHERAL (3–5% away): 21990–22600 → 40 tokens  MODE_QUOTE
                          23700–24300
────────────────────────────────────────────────
Total: 94 option tokens per index (+ 6 index base tokens)
```

Dynamic re-centering fires when spot moves ≥ 50 pts (1 strike gap):
- Unsubscribes strikes that moved outside 5% zone
- Subscribes new strikes that entered the zone
- Promotes PERIPHERAL → ACTIVE → CORE based on new spot

### LiveOptionsEngine

Lives in `zerodha/live_options_engine.py`. Runs when `ENABLE_LIVE_OPTIONS=1`. Calls both live analysers and enforces cooldowns. Per-symbol `LiveOIAnalyser`, `LiveStraddleAnalyser`, and `LiveOptionsHistory` instances are created lazily:

```python
class LiveOptionsEngine:
    COOLDOWNS: dict[str, int] = {
        "PCR_CROSSOVER_BULLISH/BEARISH": 600,   # 10 min
        "PCR_EXTREME_PE/CE":            900,    # 15 min
        "CE_WALL_BREACH/PE_WALL_BREACH": 900,   # 15 min
        "IV_EXPANDING/IV_COMPRESSING":  900,    # 15 min
        "RANGE_BOUNDARY":               1800,   # 30 min
        "SKEW_FLIP_BULLISH/BEARISH":    600,    # 10 min
        "PCR_SUSTAINED_BULLISH/BEARISH": 1200,  # 20 min
    }

    def on_aggregate_updated(stock, spot):
        # Entry point — called after every options_aggregate recompute (≈1s per symbol)
```

### `analyser/LiveOptionsHistory.py` - LiveOptionsHistory

In-memory circular time-series store for real-time options data. One instance per tracked index.

```python
class LiveOptionsHistory:
    MAX_SNAPSHOTS = 375        # 375 min = full trading day at 1-min samples
    SAMPLE_INTERVAL = 60       # seconds between recorded snapshots

    def record(agg, options_live, spot) -> bool
    def straddle_series(minutes) -> list[OptionsSnapshot]
    def pcr_series(minutes) -> list[float]
```

Each `OptionsSnapshot` captures: `ts, spot, pcr, straddle, atm_strike, total_ce_oi, total_pe_oi, ce_wall, pe_wall, ce_wall_oi, pe_wall_oi, net_ce_oi_change, net_pe_oi_change`.

### `analyser/LiveAlertFormatter.py` - F singleton

Consistent HTML builder for real-time Telegram alerts:

```python
# Usage: from analyser.LiveAlertFormatter import F
F.header(symbol, title, emoji) -> str    # "[HH:MM:SS] SYMBOL — Title"
F.kv(label, value)             -> str    # "  Label: <code>value</code>"
F.kv_pair(l1, v1, l2, v2)     -> str    # "  L1: <code>v1</code>  |  L2: <code>v2</code>"
F.kv_bold(label, value)        -> str    # "  Label: <b>value</b>"
F.signal(text)                 -> str    # "→ text"
F.note(text)                   -> str    # "  <i>text</i>"
F.build(*lines)                -> str    # Joins non-empty lines with \n
```

### `analyser/MessageFormatter.py` - MessageFormatter

Registry-based formatter for batch analysis Telegram alerts. Each analysis type registers a formatter function via `@MessageFormatter.register(*types)`. Provides a generic fallback so no alert is ever silently dropped.

```python
MessageFormatter.format(analysis_type, data, trend) -> list[str]
MessageFormatter.registered_types() -> list[str]
```

### 7.9 LiveOIAnalyser (`analyser/LiveOIAnalyser.py`)

Real-time OI analysis using live WebSocket ticks (not polled Sensibull data):

| Method | Alert Type | Signal |
|--------|-----------|--------|
| `check_pcr_crossover(agg)` | `PCR_CROSSOVER_BULLISH/BEARISH` | PCR crosses 1.0 threshold |
| `check_pcr_extreme(agg)` | `PCR_EXTREME_PE/CE` | PCR > 1.3 (contrarian bullish) or < 0.7 (contrarian bearish) |
| `check_pcr_sustained_trend(history)` | `PCR_SUSTAINED_BULLISH/BEARISH` | PCR sustained above/below 1.0 for N minutes via `LiveOptionsHistory` |
| `check_oi_wall_breach(agg, options_live)` | `CE_WALL_BREACH/PE_WALL_BREACH` | Max OI strike weakening ≥ 3% |

### 7.10 LiveStraddleAnalyser (`analyser/LiveStraddleAnalyser.py`)

Real-time straddle and IV analysis. Each method receives `(agg, options_live, spot)` from `LiveOptionsEngine`.

| Method | Alert Type | Signal |
|--------|-----------|--------|
| `check_iv_change(agg, history)` | `IV_EXPANDING/IV_COMPRESSING` | Straddle changes ≥ 3% in 5 min with spot flat (±0.3%) |
| `check_implied_move_boundary(agg, spot)` | `RANGE_BOUNDARY` | Spot used ≥ 80% of expected range (straddle × 0.68); returns `"RANGE_BOUNDARY"` signal type |
| `check_iv_skew_reversal(agg, options_live, spot)` | `SKEW_FLIP_BULLISH/BEARISH` | ATM CE/PE IV ratio crosses 1.0 |

### 7.11 PanicModeAnalyser (`analyser/PanicModeAnalyser.py`)

Composite analyser that cross-reads `stock.analysis` already populated by all earlier analysers. **Must be registered last** in `AnalyserOrchestrator`.

Two signals:

**`PANIC_MODE`** — active panic: ≥ 4/6 conditions in same direction
**`PANIC_EXHAUSTION`** — move burning out: ≥ 3/4 contrarian conditions

**PANIC_MODE conditions (6):**

| # | Condition | How Checked |
|---|-----------|-------------|
| C1 | Price momentum | `ltp_change_perc` ≥ threshold (intraday: 1.5%, positional: 3.0%) |
| C2 | IV expanding | `IV_SPIKE` or `IV_TREND(UPWARD)` in NEUTRAL |
| C3 | OI confirming | intraday: `OI_INTRADAY_TREND`; positional: `OI_BUILDUP` or `OI_SHIFT` |
| C4 | Futures confirm | `FUTURE_ACTION_*` or `FUTURE_SIGNAL_SCORE_MEDIUM+` in same direction |
| C5 | Volume surge | `VOLUME_BREAKOUT` or `VOLUME_CLIMAX` in panic direction |
| C6 | PCR confirming | `PCR_BIAS`, `PCR_EXTREME`, or `PCR_TREND` in panic direction |

**PANIC_EXHAUSTION conditions (4):**

| # | Condition | How Checked |
|---|-----------|-------------|
| E1 | IV at extreme | `IV_RANK_EXTREME` NEUTRAL with `ivp_type=VERY_HIGH` or IVP > 80 |
| E2 | Contrarian PCR | `PCR_EXTREME` or `PCR_REVERSAL` in opposite direction |
| E3 | Volume climax | `VOLUME_CLIMAX` in panic direction (exhaustion candle) |
| E4 | OI wall holding | `OI_WALL` (contrarian) or NEUTRAL `OI_SUPPORT_RESISTANCE` |

Result namedtuple `PANIC_MODE`: `(direction, price_change_pct, conditions_met, conditions_count, mode, signal)`
Result namedtuple `PANIC_EXHAUSTION`: `(panic_direction, conditions_met, conditions_count, iv_percentile, mode, signal)`

### Expiry Selection

- **Weekly expiry**: nearest Thursday (or Tuesday/Wednesday when Thursday is a holiday)
- Only the **current weekly expiry** is subscribed for live ticks
- Monthly expiry OI is much larger but weekly expiry is what matters for intraday trading
- Zerodha instruments API returns all active contracts; `expiry_dates[0]` (earliest) = current weekly

### Telegram Channel

Live options alerts go to a **dedicated channel** (`LIVE_OPTIONS_CHAT_ID`) separate from the intraday channel to avoid flooding. All 7 alert types include a timestamp `[HH:MM:SS]` in the header.

---

## 14. Intelligence Layer

The intelligence layer adds cross-layer signal correlation and LLM-powered trade narrative generation on top of the existing analysis pipeline. It is fully opt-in via environment variables.

### Architecture Overview

```
intelligence/
|-- signal.py           # Signal, Direction, Layer, SignalStrength
|-- signal_bus.py       # SignalBus (pub/sub)
|-- correlator.py       # SignalCorrelator + Confluence
|-- context_builder.py  # ContextBuilder + MarketContext
|-- narrator.py         # MarketNarrator (LLM trade thesis)
|-- llm_client.py       # GeminiClient (Gemini Flash API)
```

### Phase 1: Signal Correlation (`ENABLE_INTELLIGENCE=1`)

#### Signal — Standard Format

Every analyser (live, intraday, positional) emits `Signal` objects through the `SignalBus`:

```python
@dataclass(frozen=True)
class Signal:
    symbol: str                  # "NIFTY", "RELIANCE"
    direction: Direction         # BULLISH / BEARISH / NEUTRAL
    source: str                  # "rsi_divergence", "pcr_crossover"
    layer: Layer                 # LIVE / INTRADAY / POSITIONAL
    strength: SignalStrength     # WEAK / MODERATE / STRONG
    timestamp: float
    context: dict                # Extra metadata (RSI value, PCR level, etc.)
```

**Direction**: `BULLISH`, `BEARISH`, `NEUTRAL`
**Layer**: `LIVE` (per-tick), `INTRADAY` (5-min), `POSITIONAL` (daily/morning bias)
**SignalStrength**: Derived from analysis weight — `WEAK` (<10), `MODERATE` (10-15), `STRONG` (>=16)

#### SignalBus — Thread-Safe Event Bus

Synchronous pub/sub bus. All analysers call `bus.emit(signal)` and the correlator subscribes:

```python
bus = SignalBus()
bus.subscribe(correlator.on_signal)
bus.emit(Signal(...))
```

Thread-safe via `threading.Lock`. Tracks `total_emitted` count for monitoring.

#### SignalCorrelator — Cross-Layer Confluence

Buffers signals per symbol. When a new signal arrives, checks whether signals from OTHER layers align in the same direction within configurable time windows:

| Layer | Window | Rationale |
|-------|--------|-----------|
| LIVE | 5 min | Per-tick signals expire quickly |
| INTRADAY | 15 min | 5-min cycle signals stay relevant for ~3 cycles |
| POSITIONAL | 6 hours | Morning bias covers the full trading day |

**Confluence levels:**
- **MODERATE**: 2 layers aligned (e.g., LIVE + INTRADAY)
- **HIGH**: 3 layers aligned (LIVE + INTRADAY + POSITIONAL)
- **CAUTION flag**: Added when opposing signals exist from other layers

**Scoring:**
```
base = sum(signal.strength.value)   # 1/2/3 per signal
+ (layers - 1) × 5                  # layer bonus
+ 3 if LIVE layer present           # timeliness bonus
- 3 if contradicting signals exist  # dampener
```

**Cooldown:** 10 minutes between firing the same confluence for a symbol+direction.

#### Morning Bias Flow

At startup (before the intraday loop), the system runs positional analysers on daily data to establish directional context:

```
System startup
    |
    v
create_stock_and_index_objects()  [downloads period="1y" daily data]
    |
    v
compute_morning_bias()
    |-- Temporarily sets mode = POSITIONAL
    |-- orchestrator.reset_all_constants()   [sets POSITIONAL-mode thresholds on all analysers]
    |-- Runs all positional analysers on each stock/index
    |-- Emits positional signals to SignalBus (score-gated: score >= MIN_NOTIFICATION_SCORE)
    |-- Restores mode = INTRADAY
    |
    v
Intraday loop begins (positional signals remain in correlator buffer for 6 hours)
```

Morning bias is skipped in POSITIONAL mode (EOD analysis already runs positional analysers).

**Why `reset_all_constants()` is required:** Several analysers (e.g. `IVAnalyser`) define mode-dependent class attributes (`IV_TREND_CONTINUATION_DAYS`, `IV_TREND_PERCENTAGE_CHANGE`) only inside `reset_constants()`. Without calling it before analysis, those attributes do not exist on the class and any analyser method that references them raises `AttributeError`. The normal intraday loop calls `reset_all_constants()` before each cycle; morning bias must do the same after switching mode to POSITIONAL.

#### Signal Emission Points

| Layer | Source | When | Score Gate |
|-------|--------|------|------------|
| POSITIONAL | `intraday_monitor.compute_morning_bias()` | Once at startup | `score >= MIN_NOTIFICATION_SCORE (110)` |
| INTRADAY | `AnalyserOrchestrator.run_all_intraday()` | Every 5-min cycle, per stock | `score >= MIN_NOTIFICATION_SCORE (110)` |
| LIVE | `LiveOptionsEngine.on_option_tick()` | Per tick (via LiveOI/LiveStraddle analysers) | None — always emitted |

**Score gate rationale:** All analysers for a stock run sequentially to completion on the same candle. `_emit_signals()` is only called if `score_result.total_score >= MIN_NOTIFICATION_SCORE (110)`. Stocks that fire only 1-2 weak indicators never reach the correlator. LIVE signals bypass the gate because they originate from per-tick WebSocket data and have no batch score context — latency is the priority.

**Emission is already effectively batched for INTRADAY/POSITIONAL:** all analysers finish before `_emit_signals()` is called, so there is no intra-cycle race condition between individual indicators (RSI vs MACD etc.). The "race condition" concern only applies to LIVE signals, which are genuinely independent per-tick.

### Phase 2: Market Narrative Generator (`ENABLE_NARRATOR=1`)

Adds LLM-powered trade thesis generation using Google Gemini Flash. Requires `GEMINI_API_KEY`.

#### GeminiClient (`intelligence/llm_client.py`)

```python
class GeminiClient(LLMClient):
    MODEL = "gemini-2.5-flash"
    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
    TIMEOUT = 15          # seconds
    MAX_OUTPUT_TOKENS = 6000
    DAILY_TOKEN_LIMIT = 900_000   # 10% buffer from 1M free tier
```

Features:
- Daily token tracking with midnight reset (thread-safe)
- Automatic daily limit enforcement (skips calls when budget exhausted)
- Logs finish reason (`STOP` vs `MAX_TOKENS`) and token usage per call
- Typical usage: ~20-30 calls/day x 1.3K tokens = ~35K tokens (3.5% of daily budget)

#### ContextBuilder (`intelligence/context_builder.py`)

Gathers real-time market data from Stock objects for LLM prompts:

```python
@dataclass
class MarketContext:
    symbol, spot, day_high, day_low, day_open, prev_close, change_pct, vwap
    pcr, max_pain, ce_oi_wall, pe_oi_wall, atm_strike, atm_straddle
    total_ce_oi, total_pe_oi
    vix, expiry, minutes_to_close
```

`to_prompt_block()` formats the context as readable text for the LLM prompt.

#### MarketNarrator (`intelligence/narrator.py`)

Two operating modes:

**1. Real-Time Confluence Narrative** (`narrate_async(confluence)`)
- Triggered when SignalCorrelator detects a cross-layer confluence
- Runs in a background `ThreadPoolExecutor(max_workers=1)` — non-blocking
- Builds a prompt with confluence signals + live market context
- Sends LLM response to the Live Options Telegram channel

System prompt focuses on:
- Weekly options on NIFTY/BANKNIFTY
- Specific strike, entry, SL, target recommendations
- VIX-aware strategy (VIX > 18: sell premium, VIX < 13: buy OTM)
- Time decay awareness (avoid new positions < 60 min to close)
- Response capped at 200 words

**Flood Control (anti-flooding guards added March 2026):**

Before routing a confluence to `narrate_async()`, two gates are applied:

1. **Level gate** (in `_handle_confluence`, `intraday_monitor.py`): Only `HIGH` confluences (3+ layers aligned) trigger the narrator. `MODERATE` confluences (2 layers) still send the raw alert but skip the LLM call. This eliminates ~90% of morning-open LLM calls.

2. **Per-symbol cooldown** (`MarketNarrator.NARRATE_SYMBOL_COOLDOWN = 1800`): The same symbol cannot be narrated more than once per 30 minutes, regardless of direction. Cooldown is tracked in `_last_narrated: dict[str, float]` (symbol → epoch). Skipped calls are logged at DEBUG level with remaining cooldown seconds.

```python
# Guard in _handle_confluence (intraday_monitor.py)
if shared.app_ctx.narrator and confluence.level == "HIGH":
    shared.app_ctx.narrator.narrate_async(confluence)

# Guard in narrate_async (narrator.py)
now = time.time()
if now - self._last_narrated.get(symbol, 0) < NARRATE_SYMBOL_COOLDOWN:
    return  # skip — logged at DEBUG
self._last_narrated[symbol] = now
```

**2. Positional EOD Briefing** (`narrate_positional(report_data)`)
- Called after all positional analysis completes (~4 PM)
- Receives all report data: index, global, commodities, FII/DII, sectors, 52W, stock alerts, movers
- Strips HTML tags from Telegram messages before sending to LLM
- Sends LLM response to the Positional Telegram channel

System prompt requires:
- **Primary trade**: Always NIFTY/BANKNIFTY weekly options (specific strike, entry, SL, target, expiry)
- OI walls determine strikes: call walls = resistance, put walls = support
- FII/DII flow interpretation
- VIX regime-based strategy selection

Response template structure:
```
MARKET OVERVIEW:         [4-5 lines: direction, global, FII/DII, VIX, bias]
SECTOR THEMES:           [strong/weak sectors linked to stock alerts]
INDEX OPTIONS TRADE:     [primary NIFTY/BANKNIFTY weekly options trade]
STOCK SETUPS:            [2 equity trade ideas from alerts]
52-WEEK LOW WATCHLIST:   [capitulation vs value trap analysis]
RISKS & CONTRADICTIONS:  [mixed signals, key levels, event risk]
```

### Intelligence Data Flow

```
                    ┌─────────────────────┐
                    │   POSITIONAL Layer   │
                    │  (morning bias, EOD) │
                    └──────────┬──────────┘
                               │ Signal
    ┌──────────────────┐       │       ┌──────────────┐
    │   INTRADAY Layer │       │       │   LIVE Layer  │
    │  (5-min cycle)   │       │       │  (per tick)   │
    └────────┬─────────┘       │       └──────┬───────┘
             │ Signal          │              │ Signal
             v                 v              v
         ┌───────────────────────────────────────┐
         │              SignalBus                 │
         │         (thread-safe pub/sub)          │
         └───────────────────┬───────────────────┘
                             │
                             v
         ┌───────────────────────────────────────┐
         │          SignalCorrelator              │
         │  (time-windowed cross-layer check)    │
         └───────────────────┬───────────────────┘
                             │ Confluence detected
                             v
         ┌───────────────────────────────────────┐
         │          MarketNarrator               │
         │  (LLM trade thesis via Gemini Flash)  │
         └───────────────────┬───────────────────┘
                             │
                             v
         ┌───────────────────────────────────────┐
         │        Telegram Notification          │
         │   (Live Options / Positional channel) │
         └───────────────────────────────────────┘
```

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_INTELLIGENCE` | `0` | Toggle SignalBus + Correlator + morning bias |
| `ENABLE_NARRATOR` | `0` | Toggle LLM narratives (requires `GEMINI_API_KEY`) |
| `GEMINI_API_KEY` | (empty) | Google AI Studio API key for Gemini Flash |
| `HEALTHCHECK_URL` | (empty) | Dead-man's switch ping URL (e.g., healthchecks.io). If set, pinged at end of every analysis cycle. Unset = no-op. |

Both features are fully opt-in. When disabled, the system operates exactly as before.

---

## 15. Backtesting Framework

### `backtest/backtest.py`

#### Trade Class
```python
class Trade:
    entry_date, entry_price
    exit_date, exit_price
    direction: "LONG" | "SHORT"
    pnl, pnl_pct
    stop_loss, target
    holding_period
```

#### BacktestResult
```python
class BacktestResult:
    total_trades, winning_trades, losing_trades
    win_rate
    total_pnl, avg_pnl
    profit_factor          # gross_profit / gross_loss
    sharpe_ratio
    max_drawdown, max_drawdown_pct
    expectancy             # avg win * win_rate - avg loss * loss_rate
    avg_holding_period
```

#### Backtester
```python
class Backtester:
    # Configurable:
    position_size, stop_loss_pct, target_pct
    # Multi-stock support
    # Train/test split
    # 350-day data buffer for indicator warmup

    def run(self, stock, analyzer_method):
        # Walk-forward simulation:
        # For each bar, call analyzer on expanding window
        # If signal fires, create Trade
        # Track P&L, drawdown, etc.
```

### `backtest/optimizer.py` (~60KB)

#### Optuna-Based ThresholdOptimizer

```python
class ThresholdOptimizer:
    # Pre-defined search spaces for EVERY analyzer method
    # e.g., RSI: rsi_upper in [70,90], rsi_lower in [10,40]

    def optimize(self, method_name, n_trials=100):
        study = optuna.create_study(direction="maximize")
        study.optimize(objective, n_trials)
        # objective = Sharpe ratio or profit factor

    def objective(self, trial):
        # Sample parameters from search space
        # Set on analyzer class
        # Run backtest on TRAIN data
        # Return metric (Sharpe/profit_factor)

    # After optimization: validate on TEST data
```

Multi-stock aggregation: optimizes parameters across multiple stocks simultaneously to avoid overfitting.

---

## 16. Sentiment Analysis

### `sentiment/news_sentiment_manager.py`

#### FinBERT Model
Uses `yiyanghkust/finbert-tone` (HuggingFace) for financial sentiment classification.

```python
class FinBertSentiment:
    model = AutoModelForSequenceClassification("yiyanghkust/finbert-tone")
    tokenizer = AutoTokenizer("yiyanghkust/finbert-tone")

    def predict(self, text) -> (label, score):
        # Returns: ("positive"/"negative"/"neutral", confidence)
```

#### News Pipeline
```python
class NewsSentimentManager:
    def fetch_news(self, stock_symbol):
        # Google News RSS feed for stock
        # Deduplicate with MD5 hashes

    def process_sentiment(self, stock):
        # Fetch headlines -> FinBERT predict each
        # Aggregate: avg sentiment score
        # Thread-safe with threading.Lock
```

---

## 17. Notification & Telegram

### `notification/Notification.py`

```python
class TELEGRAM_NOTIFICATIONS:
    def send_notification(message, parse_mode="HTML"):
        # ── Redis stream dispatch ─────────────────────────────────────
        # 1. XADD notification:jobs {chat_type, message, parse_mode, ...}
        #    → Consumed by notification-service (separate process, 24/7)
        # 2. Fallback: direct HTTP to Telegram/Discord if Redis unavailable
        #
        # Routes to correct channel based on chat_type:
        #   "intraday"  -> INTRADAY_CHAT_ID
        #   "positional" -> POSITIONAL_CHAT_ID
        # Retries up to 3 times with exponential backoff on HTTP error.

    def send_live_options_notification(message, parse_mode="HTML"):
        # Same Redis primary path with chat_type="live_options"
        # Direct HTTP fallback when Redis unavailable
```

### Telegram Channels

| Channel | Env Var | Purpose |
|---------|---------|---------|
| Intraday | `INTRADAY_CHAT_ID` | 5-min stock/index analysis alerts |
| Positional | `POSITIONAL_CHAT_ID` | EOD swing trade alerts |
| Live Options | `LIVE_OPTIONS_CHAT_ID` | Real-time options tick signals (NEW) |

### Command Router (`notification/commands/`)

The bot was refactored from a 430-line monolith into a **Command Router** pattern. `bot_listener.py` is now a thin ~30-line entry point; all command logic lives in the `commands/` subpackage.

```
notification/
  bot_listener.py       # init_telegram_bot() → register_all(); schedules job_llm_budget_alert
  commands/
    __init__.py         # register_all(app): iterates [account, market, system, debug, stats] for HANDLERS
    _helpers.py         # find_stock_by_symbol(), build_gainers_losers()
    _guard.py           # guard decorator + debug_chat_only() helper
    account.py          # /start, /enctoken + _subscribe_registered_options
    market.py           # /ltp, /gainers, /losers, /watchlist, /holidays, /straddle, /walls
    system.py           # /help, /status, job_llm_budget_alert (background job)
    stats.py            # /debugstats — system + per-stock metrics dashboard
    sysstats.py         # /sysstats — system resource dashboard, history, Redis deep dive
    debug.py            # Debug commands
    debug_inspect.py    # Debug inspection commands
```

**Adding a new command:** create `commands/mymodule.py` with `HANDLERS = [("cmd", handler_fn)]`. `register_all()` picks it up automatically — no changes to `bot_listener.py` needed.

`bot_listener.py` re-exports all old public symbols for backward compatibility with existing tests.

### Bot Commands

| Command | Module | Description |
|---------|--------|-------------|
| `/help` | system | All available commands |
| `/status` | system | **System Health Dashboard** — feed lag, RAM, LLM budget |
| `/version` | system | Service versions + git commit SHA + dirty flag (debug chat only) |
| `/debugstats` | stats | System + per-stock metrics dashboard (tick rate, analysis runs, alert breakdowns) |
| `/debugstats <SYMBOL>` | stats | Per-stock deep dive: tick count, option ticks, analysis count, alert breakdown |
| `/debugstats all [ticks\|errors\|stale\|nodata]` | stats | All stocks sorted by selected metric |
| `/ltp <SYMBOL>` | market | Last traded price + % change (all 4 dicts, case-insensitive) |
| `/gainers` | market | Top 5 gainers by % change since previous close |
| `/losers` | market | Top 5 losers by % change since previous close |
| `/watchlist` | market | Full subscription overview: WebSocket state, options zones, futures LTP/OI |
| `/holidays` | market | Today's trading status + upcoming NSE holidays (next 30 days) |
| `/straddle <SYMBOL>` | market | Live ATM straddle: premium, ±1SD range, CE/PE LTPs, PCR (NIFTY/BANKNIFTY only) |
| `/walls <SYMBOL>` | market | OI walls: CE/PE max-OI strikes, tick delta, session delta, gaps, Iron Condor zone |
| `/sysstats` | sysstats | System resource dashboard — CPU (per-core), RAM, services, Redis health |
| `/sysstats history` | sysstats | 24h sparklines (CPU, RAM, Redis mem) + 7-day trend table |
| `/sysstats redis` | sysstats | Redis deep dive — memory, clients, ops/s, hit rate, slowlog |
| `/version` | system | Service versions + git commit SHA + dirty flag per service (debug chat only) |
| `/enctoken <token>` | account | Update Zerodha enctoken in `.env` + reconnect WebSocket |
| `/start` | account | Register for notifications |

### `/status` — System Health Dashboard

`notification/commands/system.py` assembles three sections in a single message:

**1. Feed Health**
- **Equity feed**: time since last WebSocket tick (`app_ctx.last_equity_tick_time`). 🟢 <30s · 🟡 30-120s · 🔴 >120s. Shows "outside market hours" between 15:30–09:15 IST.
- **Options feed**: `options_aggregate["last_updated"]` per tracked index. Same thresholds.

**2. RAM Health** (via `psutil`)
- Process RSS + system total RAM + usage %. 🟢 <60% · 🟡 60–80% · 🔴 >80%.

**3. LLM Budget**
- `GeminiClient._daily_tokens / DAILY_TOKEN_LIMIT` (900 K tokens/day).
- Background job `job_llm_budget_alert` runs every 15 min. Fires a Telegram alert **once per day** when usage crosses 80% (`app_ctx.llm_budget_warned` prevents repeats; reset at midnight).

### `/straddle <SYMBOL>` (NIFTY / BANKNIFTY only)

Reads `options_aggregate` + `options_live[atm_strike]` from `TickStore`:
- Spot, ATM strike, straddle premium
- ±1SD expected daily move (`straddle × 0.68`)
- CE and PE individual LTPs
- PCR + staleness warning if feed lag > 30 s

### `/walls <SYMBOL>` (NIFTY / BANKNIFTY only)

Reads `max_oi_ce_strike` / `max_oi_pe_strike` from `options_aggregate`:

| Row | Source | Meaning |
|-----|--------|---------|
| OI | `options_live[strike][type]["oi"]` | Current open interest |
| Tick delta | `oi − prev_oi` | Change in last WebSocket update |
| Session delta | `LiveOptionsHistory.wall_oi_trend(type, 375)` | OI built/unwound since open |

Session delta is suppressed if the wall strike migrated during the session. Also shows gap (pts + %) from spot to each wall and an Iron Condor zone suggestion.

#### Telegram message size handling
`/watchlist` automatically splits messages exceeding Telegram's 4096-character limit into sequential messages.

### Message Format (from `AnalyserOrchestrator.generate_analysis_message()`)

HTML-formatted Telegram message with:
- Stock name + current price
- Score + priority tier
- Dominant sentiment with confidence percentage
- Per-signal breakdown with emoji indicators (green/red circles)
- Cross-category confirmation notes

---

## 18. ML Pipeline

### `ml_pipeline/`

A full ML prediction pipeline (separate from the main analysis system):

| Stage | Description |
|-------|-------------|
| Data collection | Historical price + derivatives data |
| Feature engineering | Technical indicators, OI features, volume features |
| Model training | XGBoost, LightGBM, Random Forest, Ensemble |
| Evaluation | Classification metrics, backtested returns |

---

## 19. Configuration & Constants

### `common/constants.py`

#### Environment Variables
All sensitive config via env vars:
- `TELEGRAM_TOKEN`, `INTRADAY_CHAT_ID`, `POSITIONAL_CHAT_ID`
- `LIVE_OPTIONS_CHAT_ID` — dedicated Telegram channel for real-time options alerts (NEW)
- `ZERODHA_API_KEY`, `ZERODHA_ENC_TOKEN`
- `IS_PRODUCTION` flag
- `ENABLE_LIVE_OPTIONS` — set to `1` to activate live options tracking mode (NEW)
  - When set: subscribes NIFTY/BANKNIFTY weekly option chains via WebSocket
  - Runs `LiveOIAnalyser` + `LiveStraddleAnalyser` on every option tick
  - Sends alerts to `LIVE_OPTIONS_CHAT_ID`
  - Skips commodity token subscriptions (index-only WebSocket mode)
- `LIVE_OPTIONS_ONLY` — set to `1` alongside `ENABLE_LIVE_OPTIONS` to disable all regular intraday/positional analysers and run WebSocket-only
- `ENABLE_INTELLIGENCE` — set to `1` to activate SignalBus, SignalCorrelator, morning bias, and LiveStockEngine
- `ENABLE_NARRATOR` — set to `1` to activate LLM-powered trade narratives (requires `GEMINI_API_KEY`)
- `GEMINI_API_KEY` — Google AI Studio API key for Gemini Flash model

#### Key Constants

```python
# Scoring
MIN_NOTIFICATION_SCORE = 75
CONFIDENCE_GATE = 65  # % alignment required

# Priority thresholds
PRIORITY_LOW = 35
PRIORITY_MEDIUM = 60
PRIORITY_HIGH = 90
PRIORITY_CRITICAL = 130

# Cross-category bonuses
TECH_OPTIONS_BONUS = 1.5
TECH_FUTURES_BONUS = 1.3
OPTIONS_FUTURES_BONUS = 1.4

# Diminishing returns
DUPLICATE_SIGNAL_DECAY = 0.30  # 30% per additional same-type signal
```

#### Stock Lists
`data/final_derivatives_list.json`: Master list of tracked stocks/indices/commodities with instrument tokens.

---

## 20. Market Holiday Gatekeeper & Warning System

### Overview

A three-component feature that prevents wasted compute/API calls on market
holidays and proactively warns options traders about Theta decay risk.

### Component 1 — `common/market_calendar.py` (Single Source of Truth)

| Item | Detail |
|------|--------|
| **Library** | `pandas_market_calendars` — `'XNSE'` calendar |
| **Custom overlay** | `configs/custom_holidays.json` (JSON array of ISO-8601 strings) for ad-hoc state holidays not yet in the library |
| **Caching** | `@lru_cache(maxsize=1)` on both the calendar instance and JSON parse — zero re-reads within a process lifetime |
| **Fail-open** | If the library throws an unexpected error, `is_trading_day()` returns `True` so the system never silently blocks on a real trading day |

```python
# Public API
from common.market_calendar import is_trading_day, get_upcoming_holidays

is_trading_day()                     # -> bool (today)
is_trading_day(date(2026, 4, 14))    # -> False (holiday)
get_upcoming_holidays(days_ahead=7)  # -> [date(2026, 4, 14), ...]
```

**Custom holidays file** (`configs/custom_holidays.json`):
```json
["2026-04-14", "2026-10-02"]
```

### Component 2 — Fail-Fast Gatekeepers

Both entry points guard themselves at the **very first line of `__main__`**
(or the public runner function), before any heavy object initialisation:

| File | Guard location | Action on holiday |
|------|---------------|-------------------|
| `intraday/intraday_monitor.py` | `if __name__ == "__main__"` block | `sys.exit(0)` |
| `premarket/premarket_report.py` | `run_global_cues_report()` | `sys.exit(0)` |

Guards are **production-only** (`PRODUCTION=1`). Dev mode is always pass-through.

### Component 3 — Telegram Holiday Warning Banner

Generated by `PreMarketReport._build_holiday_warning_banner()` and automatically
prepended to the morning global-cues Telegram message:

- No holidays found → empty string → report sent unmodified
- Holidays found → high-visibility banner with dates + Theta decay guidance
- Weekends excluded — only named NSE market holidays are listed

---

## 21. Observability System

### Overview

Three lightweight reliability layers are injected into `intraday/intraday_monitor.py`. All layers are **fail-silent** — they never raise exceptions or disrupt the trading loop. Designed for an AWS t2.micro instance (1 GB RAM) where silent failures are common.

---

### Layer 1 — Global Exception Catcher

**Function:** `_crash_handler(exc_type, exc_value, exc_tb)`  
**Activation:** `sys.excepthook = _crash_handler` at module load time

Intercepts every uncaught exception before the process dies and fires a Telegram alert with the formatted traceback:

```python
def _crash_handler(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(...)   # pass-through — don't trap Ctrl-C
        return
    tb_text  = "".join(traceback.format_exception(...))
    tb_text  = truncate(tb_text, 3500)          # fits Telegram 4096-char limit
    exc_summary = truncate(str(exc_value), 200)
    # html.escape() both fields — prevents Telegram 400 errors from
    # repr strings like <urllib3.HTTPSConnection object at 0x...>
    message = f"🚨 FATAL CRASH ... <pre>{html.escape(tb_text)}</pre>"
    TELEGRAM_NOTIFICATIONS.send_notification(message, parse_mode="HTML")
```

**Key properties:**
- `html.escape()` applied to both `exc_summary` and `tb_text` — prevents Telegram HTTP 400 (`Bad Request: can't parse entities: Unsupported start tag`) when tracebacks contain angle-bracket repr strings (e.g., `<urllib3.connection.HTTPSConnection object at 0x7f...>`)
- `KeyboardInterrupt` is forwarded to `sys.__excepthook__` so `Ctrl+C` behaves normally
- Entire body wrapped in `try/except` — will never itself raise
- Logs at `CRITICAL` level regardless of Telegram success

---

### Layer 2 — Heartbeat (Dead-Man's Switch)

**Function:** `_ping_healthcheck()`  
**Activation:** Called at the **bottom of every `intraday_analysis()` loop iteration**, after all analysis and before `sleep()`

Pings an external heartbeat service to confirm the process is alive:

```python
def _ping_healthcheck():
    url = os.getenv("HEALTHCHECK_URL")
    if not url:
        return           # no-op if env var not set
    try:
        import requests  # local import — keeps module-level imports clean
        requests.get(url, timeout=5)
    except Exception:
        logger.debug("Healthcheck ping failed (non-fatal)")
```

**Setup:** Create a free check at [healthchecks.io](https://healthchecks.io) and set `HEALTHCHECK_URL` in `.env`. If the ping is not received within the configured period (e.g., 15 minutes), healthchecks.io sends an alert via email / Telegram / Slack.

**Key properties:**
- Completely optional — unset `HEALTHCHECK_URL` and the function is a no-op
- `requests` is imported locally to avoid adding it to module-level scope
- Any exception (Timeout, ConnectionError, etc.) is silently swallowed

---

### Layer 3 — Zombie Data Watchdog

**Function:** `check_data_freshness(stock, stale_threshold_sec=120)`  
**Activation:** Called per-index-object inside `intraday_analysis()` loop, gated by `ENABLE_LIVE_OPTIONS`

Detects silently-stalled WebSocket feeds — a common failure mode where the WebSocket connection appears open but stops delivering ticks:

```python
_stale_alerts_sent = set()   # per-session dedup: never re-alert same symbol

def check_data_freshness(stock, stale_threshold_sec=120):
    # Gate 1: only during market hours (09:15–15:30)
    # Gate 2: only on trading days (via market_calendar, fail-open)
    # Gate 3: options_aggregate must exist and have last_updated
    age = now.timestamp() - last_updated   # supports epoch float OR datetime
    if age > stale_threshold_sec:
        if symbol not in _stale_alerts_sent:
            _stale_alerts_sent.add(symbol)
            TELEGRAM_NOTIFICATIONS.send_notification("⚠️ STALE DATA...", ...)
    return age <= stale_threshold_sec
```

**Key properties:**
- All three gates must pass before any staleness check occurs
- **One-time alert per symbol per session** — `_stale_alerts_sent` set prevents repeated flooding when data stays stale
- Supports both `int/float` epoch timestamps and `datetime`-like objects (duck-typed via `hasattr(x, "timestamp")`)
- Fail-open on `is_trading_day()` import errors — assumes trading day so real stale data is never silently missed
- Returns `True` (fresh / skipped) or `False` (stale, alert sent)

---

### Test Coverage

`tests/test_observability.py` — **22 tests, 0 network calls**

| Class | Count | What's tested |
|-------|-------|---------------|
| `TestCrashHandler` | 7 | Telegram alert sent, `parse_mode="HTML"`, `KeyboardInterrupt` bypass, long traceback truncation, Telegram failure survival, `sys.excepthook` installed, angle-bracket HTML escaping (`&lt;urllib3`) |
| `TestHeartbeat` | 5 | Pings when URL set, no-op when unset, suppresses `Timeout` / `ConnectionError` / generic exceptions |
| `TestZombieDataWatchdog` | 10 | Fresh data, stale alert+dedup, different symbols both alert, outside market hours, holiday skip, no `options_aggregate`, no `last_updated`, custom threshold, `datetime`-typed `last_updated` |

---

### Service Versioning & Deployment Verification

**Problem:** With 6 always-running services across 2+ deployment points, there was no way to verify which git commit was running on the server. The deploy script did a blind `git pull` + `systemctl restart` without recording the resulting HEAD. The bot could not report versions. If a deploy failed silently (partial pull, wrong branch), the running code could be stale with no visible signal.

**Solution:** A central version module captures the git commit SHA at import time. Every service writes `version` + `commit` fields to its Redis registry hash on every heartbeat. A `/version` bot command reads all registry hashes and reports the running version of each service.

#### `services/common/version.py` — Central Version Source

```python
SERVICE_VERSION = "1.0.0"           # Semantic version (manual bump per release)
GIT_COMMIT      = "1c4b128"         # Short SHA from `git rev-parse --short HEAD`
GIT_DIRTY       = True/False        # True if working tree has uncommitted changes
BUILD_LABEL     = "1.0.0+1c4b128"   # Composite: version + commit (+dirty flag)
```

Captured once at import time via `subprocess.run(["git", "rev-parse", "--short", "HEAD"])`. If git is unavailable (e.g. production without git installed), fields default to `"unknown"`. The `GIT_DIRTY` flag is set by checking `git status --porcelain` — if any untracked/modified files exist, the commit is marked dirty, indicating the running code may differ from the committed SHA.

**Fail-silent:** If `subprocess.run` fails (git not installed, not a git repo, HEAD detached), all fields are set to `"unknown"` and `GIT_DIRTY = False`. The module never raises.

#### Integration Points

| Service | Where version is written | Frequency |
|---------|--------------------------|-----------|
| data-gateway | `service:registry:data-gateway` hash → `version`, `commit` fields | Every heartbeat (~60s) |
| market-data | `service:registry:market-data` hash → `version`, `commit` fields | Every heartbeat (30s) |
| notification-service | `service:registry:notification-service` hash → `version`, `commit` fields | Every heartbeat (~30s) |
| analysis-engine | `service:registry:analysis-engine:{worker}` hash → `version`, `commit` fields | Every heartbeat |
| resource-monitor | `service:registry:resource-monitor` hash → `version`, `commit` fields | Every heartbeat (30s) |
| monolith | `service:registry:monolith` hash → `version`, `commit` fields | Every cycle (~310s) |

Every service also logs its version at startup:
```
[data-gateway] v1.0.0+1c4b128 starting
```

#### `/version` Bot Command

`notification/commands/system.py` — reads all `service:registry:*` hashes from Redis and displays:

```
🏷️ Service Versions

  📦 monolith              v1.0.0+1c4b128   ✅ clean   up 3h 42m
  📦 data-gateway          v1.0.0+1c4b128   ✅ clean   up 3h 41m
  📦 market-data           v1.0.0+1c4b128   ⚠️ dirty   up 3h 41m
  📦 analysis-engine       v1.0.0+1c4b128   ✅ clean   up 3h 41m
  📦 notification-service  v1.0.0+1c4b128   ✅ clean   up 3h 41m
  📦 resource-monitor      v1.0.0+1c4b128   ✅ clean   up 3h 41m

Git HEAD: 1c4b128
```

- **✅ clean**: `GIT_DIRTY = False` — running code matches the committed SHA
- **⚠️ dirty**: `GIT_DIRTY = True` — working tree has uncommitted changes; running code may differ from SHA
- **🔴 stale**: `last_heartbeat` older than 2× TTL (240s) — service may be down
- **⚪ no data**: registry hash missing or empty — service never started or Redis was flushed

#### Deploy Script Enhancement

`scripts/deploy.py` captures `git rev-parse --short HEAD` after `git pull` and logs it:
```
Deployed commit: 1c4b128
```

This provides a local record of what was deployed, even if the SSH session output is lost.

#### Design Decisions

- **Git SHA, not semantic version alone**: The commit SHA is the source of truth. `SERVICE_VERSION` is a convenience label that must be manually bumped. The SHA is captured automatically and is always accurate.
- **Dirty flag**: Catches the common failure mode of editing files directly on the server (hotfix) without committing. The `/version` output immediately shows `⚠️ dirty` so the operator knows the running code doesn't match any commit.
- **Per-heartbeat, not per-startup**: Writing version on every heartbeat ensures the registry hash always has the version, even if the service was restarted and the initial write was missed. The TTL (120s) would expire a startup-only write.
- **No build step**: Version is captured at import time, not injected by CI. This works with the existing `git pull + systemctl restart` deploy flow without requiring Docker, CI, or build scripts.
- **`/version` restricted to debug chat**: Same `debug_chat_only()` guard as `/sysstats` and `/debugstats` — version info reveals deployment infrastructure.

---

### Metrics & Counters System

A lightweight per-stock + system-wide counters system stored in Redis, providing real-time visibility into every stage of the pipeline. All functions are **fail-safe** — Redis unavailability only logs at DEBUG, never crashes business logic.

**Module:** `services/common/metrics.py`

#### Redis Key Structure

| Key | Type | TTL | Contents |
|-----|------|-----|----------|
| `stats:stock:{symbol}` | HASH | persistent | Per-stock counters |
| `stats:system` | HASH | persistent | System-wide counters |
| `stats:daily:{YYYY-MM-DD}` | HASH | 30-day TTL | Daily rollup |
| `sys:latest:system` | HASH | 120s TTL | System snapshot: CPU per-core, RAM, swap, disk, load, net, uptime |
| `sys:latest:{service}` | HASH | 120s TTL | Per-service snapshot: CPU%, RSS, threads, fds, uptime, affinity |
| `sys:latest:redis` | HASH | 120s TTL | Redis snapshot: used/peak mem, clients, ops/s, hit rate, keys, slowlog |
| `sys:ts:{metric}` | ZSET | 25h TTL | Time-series: score=timestamp, member=value (24h retention) |
| `sys:daily:{YYYY-MM-DD}` | HASH | 30-day TTL | Daily max/avg rollups: cpu, ram, disk, redis_mem |

#### Per-Stock Fields (`stats:stock:{symbol}`)

| Field | Written by | Description |
|-------|-----------|-------------|
| `tick_count` | market-data (heartbeat) | Total equity/index ticks received |
| `option_tick_count` | market-data (heartbeat) | Total option ticks received |
| `analysis_count` | analysis-engine | Number of analysis cycles completed |
| `last_analysis_result` | analysis-engine | SUCCESS / NO_DATA / ERROR / SKIPPED |
| `last_analysis_duration_ms` | analysis-engine | Last analysis duration in milliseconds |
| `last_analysis_time` | analysis-engine | Epoch timestamp of last analysis |
| `trends_found` | analysis-engine | Total trends detected for this stock |
| `analysis_errors` | analysis-engine | Number of analysis errors |
| `alerts_trend` | monolith (intraday) | Trend alert count |
| `alerts_confluence` | monolith (intraday) | Confluence alert count |
| `alerts_stale_data` | monolith (intraday) | Stale data alert count |
| `alerts_live_options` | monolith (live options) | Live options alert count |
| `alerts_narrative` | monolith (narrator) | LLM narrative alert count |
| `alerts_attempted` | Notification.py | Alerts queued to Redis stream (producer-side) |
| `alerts_delivered` | notification-service | Alerts successfully delivered |
| `alerts_failed` | notification-service | Alerts that failed (dead-lettered) |

#### System Fields (`stats:system`)

| Field | Written by | Description |
|-------|-----------|-------------|
| `total_ticks` | market-data (heartbeat) | Total ticks across all symbols |
| `tick_rate` | market-data (heartbeat) | Ticks per second |
| `ws2_reconnects` | market-data (heartbeat) | WS2 reconnection count |
| `snapshot_age_s` | market-data (heartbeat) | Seconds since last snapshot publish |
| `total_jobs_dispatched` | monolith (intraday) | Total analysis jobs dispatched |
| `total_jobs_completed` | monolith (intraday) | Total analysis jobs completed |
| `analysis_runs` | analysis-engine | Total analysis cycles across all stocks |
| `result_success_count` | analysis-engine | Successful analysis results |
| `result_no_data_count` | analysis-engine | NO_DATA results |
| `result_error_count` | analysis-engine | Error results |
| `trends_found` | monolith (intraday) | Total trends found system-wide |
| `total_confluences` | monolith (intraday) | Total confluence signals detected |
| `stale_stocks_count` | monolith (intraday) | Number of stocks with stale data |
| `alerts_attempted` | Notification.py | Total alerts queued |
| `alerts_delivered` | notification-service | Total alerts delivered |
| `alerts_failed` | notification-service | Total alerts failed |
| `last_updated` | metrics module | Epoch timestamp of last system update |

#### Daily Rollup Fields (`stats:daily:{YYYY-MM-DD}`)

| Field | Description |
|-------|-------------|
| `alerts_attempted` | Total alerts queued today |
| `alerts_delivered` | Total alerts delivered today |
| `alerts_failed` | Total alerts failed today |
| `analysis_runs` | Total analysis runs today |
| `trends_found` | Total trends found today |

#### Design Decisions

- **Dual counting strategy:** `alerts_attempted` (producer-side, in `_notify_via_redis`) counts queued alerts. `alerts_delivered` + `alerts_failed` (consumer-side, in notification-service) count actual outcomes. `alerts_attempted - alerts_delivered - alerts_failed` = alerts stuck in stream.
- **Per-stock tick counters batch-updated in 30s heartbeat**, not in 1s snapshot loop — avoids doubling Redis write traffic. `tick_count` and `option_tick_count` are added as fields to existing `data:tick:*` and `data:options_agg:*` hashes (zero extra Redis calls during snapshot), then batch-written to `stats:stock:*` every 30s.
- **`stats:daily:{date}` keys have 30-day TTL** set on first write via pipeline.
- **Lazy Redis connection** with `ping()` check — if Redis is unavailable, all write/read functions silently return None/empty dict.

#### `/debugstats` Bot Command

`notification/commands/stats.py` — restricted to debug chat via `guard` + `debug_chat_only`. Three views:

1. **System dashboard** (`/debugstats`): tick pipeline (rate, snapshots, WS2 reconnects), analysis pipeline (cycle, jobs dispatched/completed, pending, avg duration, results breakdown), alerts & signals (attempted, delivered, failed, trends, confluences, stale stocks), auth (enctoken refreshes).
2. **Per-stock deep dive** (`/debugstats RELIANCE`): data (tick count, option ticks, last tick age, PCR), analysis (count, last result, duration, time, trends, errors), alerts (trend, confluence, live options, narratives, stale, attempted/delivered/failed), derivatives (GEX regime).
3. **All stocks sorted** (`/debugstats all [ticks|errors|stale|nodata]`): table of all stocks sorted by selected metric, showing ticks, analysis count, trends, alerts, result, errors.

---

## 22. Key Design Patterns

### 1. Decorator-Based Method Registration
Analyzers use decorators (`@intraday`, `@both`, `@index_both`) to self-register methods. The orchestrator discovers them automatically at init time. This makes adding new analysis methods trivial - just decorate and implement.

### 2. Singleton AppContext
`shared.app_ctx` provides global access to stock dictionaries, mode, and Zerodha connections. All modules share state through this singleton.

### 3. Registry Pattern (Post-Market)
`SOURCE_CLASSES` list in `registry.py` makes it easy to add new post-market data sources. Just implement `PostMarketSource` abstract class and register.

### 4. Template Method (PostMarketSource)
`base.py` defines `run()` which calls `fetch_raw()` -> `normalize()`. Subclasses implement the specific fetch and normalize logic.

### 5. Named Tuples for Analysis Results
Every signal stores its data as a `namedtuple`, providing structured, immutable analysis results that are easy to format for messages.

### 6. Observer Pattern (WebSocket Ticks)
Zerodha ticks flow through: `KiteTicker` -> `ZerodhaTickerManager.on_ticks` -> `analyze_tick()` -> `stock.update_zerodha_data()`. Each stock object is updated as ticks arrive.

### 7. Strategy Pattern (Analyzers)
Each analyzer is an independent strategy. The orchestrator iterates through all of them uniformly. New analyzers can be added without modifying existing code.

### 8. Walk-Forward Simulation (Backtesting)
The backtester uses expanding windows to simulate real-world conditions where you only have data up to the current bar.

### 9. Token Registry (Live Options)
`TokenRegistry` provides O(1) instrument_token → TokenInfo lookup, replacing per-tick dict scans. All option/futures/equity/index tokens are pre-registered at startup. The registry also owns zone assignment (CORE/ACTIVE/PERIPHERAL) and re-centering logic.

### 10. Zone-Based Subscription (Live Options)
Instead of subscribing all 514 NIFTY option tokens, only 94 tokens in 3 zones around the current ATM are subscribed. Zone membership and WebSocket mode (FULL/QUOTE) are determined by distance from spot price. When spot moves ≥ 1 strike gap, zones are recalculated and subscription changes are applied.

### 11. Cooldown Gate (Live Options)
`LiveOptionsEngine` maintains a `_last_alert` dict keyed by `(symbol, alert_type)`. Each alert type has a configurable cooldown (10–30 min). This prevents alert flooding in choppy markets while ensuring signals are not missed — the first occurrence always fires.

### 12. Cross-Layer Signal Correlation (Intelligence)
The `SignalBus` + `SignalCorrelator` pattern decouples signal producers (analysers) from consumers (correlator, narrator). Analysers emit standardized `Signal` objects without knowing what will consume them. The correlator buffers signals with per-layer time windows and detects alignment across 2+ layers as a `Confluence`. This pub/sub design makes it trivial to add new signal sources or consumers.

**Score-gated emission (INTRADAY/POSITIONAL only):** `_emit_signals()` is only called when `score_result.total_score >= MIN_NOTIFICATION_SCORE (75)`. This ensures the correlator only receives signals from stocks with genuine multi-indicator conviction — the same bar used for Telegram alerts. Stocks that fire only 1-2 weak indicators are silently dropped at the bus boundary. LIVE layer signals bypass this gate since they have no batch score context and require minimum latency.

**Hybrid emission model:** INTRADAY and POSITIONAL signals are effectively batched — all analysers run sequentially to completion before `_emit_signals()` fires, eliminating intra-cycle race conditions. LIVE signals are streamed individually per tick, where latency is the priority. The correlator's time-window design naturally reconciles these two regimes.

### 13. Background Narrative Generation (Intelligence)
`MarketNarrator` uses a single-worker `ThreadPoolExecutor` to run LLM calls off the hot path. The raw alert fires instantly via Telegram; the LLM-generated narrative follows 1-3 seconds later without blocking the analysis pipeline. This pattern ensures the core system latency is unaffected by LLM response time.

### 14. Dual-Gate Flood Control (Narrator)
The narrator uses two independent guards before queuing an LLM call: a **level gate** (`level == "HIGH"`) in `_handle_confluence` and a **per-symbol cooldown** in `narrate_async`. Separating the gates at different call sites means each can be tuned or disabled independently without touching the other. See [Section 21](#21-observability-system) for implementation details.

### 15. Fail-Silent Observability Helpers
`_crash_handler`, `_ping_healthcheck`, and `check_data_freshness` are designed on the principle that observability code must **never** affect the system it observes. All three catch all exceptions internally and log at DEBUG/WARNING rather than propagating. This "fail-silent" pattern is enforced in tests by injecting `side_effect=Exception` into Telegram/requests mocks and asserting that the production call still returns normally.

### 16. Single-Responsibility Extraction (Stock Decoupling)
`TickStore`, `FuturesFetcher`, and `SensibullFetcher` were extracted from `Stock` to apply the Single Responsibility Principle. `Stock` now owns price data and analysis results only. Live tick state is in `TickStore`. HTTP fetching is in the dedicated fetcher classes. `Stock` provides deprecated shim methods for backward compatibility.

### 17. Composite Analyser Pattern (PanicModeAnalyser)
`PanicModeAnalyser` is a **meta-analyser**: instead of reading raw market data, it reads the `stock.analysis` dict populated by all earlier analysers. This makes it a cross-signal correlator that can detect confluent panic conditions without duplicating any individual signal logic. The constraint that it **must run last** is enforced in the `orchestrator.register()` call order.

### 18. Registry-Based Alert Formatting (MessageFormatter)
`MessageFormatter` uses a `@register(*analysis_types)` decorator pattern to map analysis type strings to HTML formatter functions. This decouples formatting from analysis logic, allows incremental addition of new analysis types without touching existing code, and provides a non-silent fallback for any unregistered types.

---

## 23. Data Flow Diagrams

### Intraday Analysis Flow

```
                    +-----------------+
                    |   Stock List    |
                    | (JSON config)   |
                    +-------+---------+
                            |
                            v
                    +-------+---------+
                    | intraday_monitor|
                    |  (main loop)    |
                    +-------+---------+
                            |
              +-------------+-------------+
              |             |             |
              v             v             v
        +---------+   +---------+   +-----------+
        | yfinance|   |Zerodha  |   | Sensibull |
        | (OHLCV) |   |(ticks,  |   | (OI,PCR,  |
        |         |   | options)|   |  MaxPain)  |
        +---------+   +---------+   +-----------+
              |             |             |
              +-------------+-------------+
                            |
                            v
                    +-------+---------+
                    |   Stock Object  |
                    | (priceData,     |
                    |  zerodha_ctx,   |
                    |  sensibull_ctx) |
                    +-------+---------+
                            |
                            v
              +-------------+-------------+
              |             |             |
              v             v             v
        +---------+   +---------+   +---------+
        |Technical|   | Volume  |   |Candle-  |
        |Analyser |   |Analyser |   |stick    |  ... (8 analyzers)
        +---------+   +---------+   +---------+
              |             |             |
              +-------------+-------------+
                            |
                            v
                    +-------+---------+
                    | stock.analysis  |
                    | {BULLISH:{...}, |
                    |  BEARISH:{...}} |
                    +-------+---------+
                            |
                            v
                    +-------+---------+
                    | scoring.py      |
                    | calculate_score |
                    | should_notify   |
                    +-------+---------+
                            |
                     (if score >= 75
                      and confidence
                      >= 65%)
                            |
                            v
                    +-------+---------+
                    | Telegram Alert  |
                    | (HTML formatted)|
                    +-----------------+
```

### Signal Scoring Flow

```
stock.analysis["BULLISH"] = {
    "RSI": RSIAnalysis(...),           weight=15
    "SUPERTREND": SupertrendData(...), weight=14
    "OI_WALL": OIWallData(...),        weight=16
}                                       base = 45

stock.analysis["BEARISH"] = {
    "MACD": MACDAnalysis(...),         weight=12
}                                       base = 12

Dominant = BULLISH (45 > 12)
Confidence = 45/(45+12) = 78.9%

Cross-category check:
  RSI (TECHNICAL) + OI_WALL (OPTIONS) = 1.5x bonus
  alignment_bonus = 45 * 0.5 = 22.5

total_score = 45 + 22.5 = 67.5
priority = MEDIUM (>= 60)

should_notify? 67.5 < 75 => NO (below MIN_NOTIFICATION_SCORE)
```

### Live Options Data Flow

```
Zerodha WebSocket (wss://ws.zerodha.com)
  |
  | binary tick packets (8/28/32/44/184 bytes)
  v
KiteTicker.on_ticks() → tick_queue.put()
  |
  v
ZerodhaTickerManager.process_ticks()  [dedicated thread]
  |
  v
_route_tick(tick)
  |
  +--[TokenType.INDEX]-->  stock.update_zerodha_data(tick)
  |                        (updates spot price — 28/32 byte format)
  |
  +--[TokenType.OPTION]--> stock.update_option_tick(strike, ce/pe, tick)
  |                        stock.recompute_options_aggregate(spot)   [throttled 1s]
  |                             |
  |                             v
  |                        options_aggregate updated:
  |                          total_ce_oi, total_pe_oi, live_pcr,
  |                          atm_strike, atm_straddle_premium,
  |                          max_oi_ce_strike, max_oi_pe_strike
  |                             |
  |                             v
  |                        LiveOptionsEngine.on_option_tick(symbol, agg, options_live, spot)
  |                             |
  |                    +--------+--------+
  |                    |                 |
  |                    v                 v
  |             LiveOIAnalyser     LiveStraddleAnalyser
  |             check_pcr_*()      check_iv_change()
  |             check_oi_wall()    check_implied_move()
  |             check_max_pain()   check_skew_flip()
  |                    |                 |
  |                    +--------+--------+
  |                             |
  |                    [cooldown gate]
  |                             |
  |                             v
  |                    TELEGRAM_NOTIFICATIONS
  |                    .send_live_options_notification()
  |                             |
  |                             v
  |                    #live-options Telegram channel
  |
  +--[dynamic re-centering]-->  when spot moves ≥ 50 pts:
                                unsubscribe far strikes
                                subscribe new strikes
                                reassign CORE/ACTIVE/PERIPHERAL zones
```

### Pre-Market to Post-Market Timeline

```
00:00  Monolith idle (overnight). Redis keeps yesterday's positional data.
         data-gateway idle. notification-service draining stream.
         market-data: WS connected, receiving ticks (if any pre-open).
         analysis-engine: idle, waiting for cycle stream.

09:00  Monolith: _refresh_zerodha_auth() — TOTP login
       Data-gateway: prevDayOHLCV daily refresh (all 211 stocks + 6 indices +
         6 commodities + 10 global indices) using _get_prev_day_row() helper
09:00  Global cues report → Telegram (intraday channel)
09:07  Pre-open session report → Telegram
09:10  Morning bias (positional signals from yesterday's 8 PM results)
09:15  +-- Intraday loop starts --------+
       |  data-gateway: fetches 5m + Sensibull → Redis + cycle_ready
       |  market-data: WS1 (equity/index) + WS2 (options) + Sensibull WS
       |    → data:tick:* + data:options_agg:* (1s snapshots)
       |    → heartbeat: stats:system + stats:stock:* (every 30s)
       |  analysis-engine: consumes data:cycle_stream → 12 analysers + scoring
       |    → stats:stock:* (analysis_count, result, duration, trends)
       |  Monolith: orchestrates cycle, reads Redis, dispatches alerts
       |    → notification:jobs stream (with alerts_attempted counter)
       |  notification-service: consumes stream → Telegram/Discord
       |    → alerts_delivered / alerts_failed counters
       |  Every ~310s cycle
       +-- Loop until 15:30 ------------+
15:30  Intraday loop ends
       Data-gateway switches to idle (positional fetch at 19:00)
19:00  Data-gateway: single positional fetch (2y daily + Sensibull positional)
         → publishes cycle_ready
20:00  Positional analysis:
         Run all positional analyzer methods
         Score and notify
         Post-market reports: FII/DII, sectors, F&O OI, index returns
         LLM narrator: EOD market briefing
20:30  Monolith idle. Redis keeps positional data for tomorrow's morning bias.
```

---

## 24. Test Suite

### Overview

The project has a comprehensive test suite with **1127+ tests across 45 files** organized in 6 subdirectories. All tests run in-process with zero network calls (all external dependencies are mocked).

```
tests/
|-- conftest.py                          # Root-level fixtures
|-- test_observability.py                # 22 tests: crash handler, heartbeat, zombie watchdog
|
|-- services/                            # 1 test file — microservice tests
|   |-- test_resource_monitor.py         # 24 tests: collector, storage, alerts, sparkline, sysstats views
|   |-- test_crash_handler.py            # 6 tests: shared crash handler module
|   |-- test_prevday_fallback.py         # 12 tests: yfinance NaN detection + Zerodha fallback
|   |-- test_version.py                  # Tests: version module git SHA capture, dirty flag, unknown fallback
|
|-- analyser/                            # 17 test files — all analyser classes
|   |-- conftest.py                      # Shared fixtures (mock_stock, make_price_df, etc.)
|   |-- test_analyser_base.py            # BaseAnalyzer decorator registration + AnalyserOrchestrator
|   |-- test_technical_analyser.py       # RSI, ADX, EMA crossover, Bollinger, Supertrend, Stochastic
|   |-- test_volume_analyser.py          # Volume breakout, OBV divergence, volume climax
|   |-- test_candle_stick_pattern_analyser.py  # All 6 candlestick pattern groups
|   |-- test_iv_analyser.py              # IV spike, trend, rank, IV vs HV
|   |-- test_pcr_analyser.py             # 5 PCR methods
|   |-- test_max_pain_analyser.py        # Max pain deviation, trend
|   |-- test_oi_chain_analyser.py        # 5 OI chain methods
|   |-- test_panic_mode_analyser.py      # PANIC_MODE and PANIC_EXHAUSTION
|   |-- test_futures_analyser.py         # ATR, dynamic thresholds, futures action
|   |-- test_live_oi_analyser.py         # PCR crossover, extreme, sustained, wall breach
|   |-- test_live_straddle_analyser.py   # IV change, implied move boundary, skew reversal
|   |-- test_live_alert_formatter.py     # F singleton HTML builder
|   |-- test_live_options_history.py     # OptionsSnapshot recording, querying
|   |-- test_message_formatter.py        # Registry registration, format, fallback
|
|-- common/                              # 6 test files
|   |-- test_stock.py                    # Stock construction, set_analysis, TickStore delegation
|   |-- test_scoring.py                  # Score calculation, alignment bonus, should_notify
|   |-- test_token_registry.py           # TokenRegistry, TokenType, OptionZone, zone assignment
|   |-- test_market_calendar.py          # is_trading_day, get_upcoming_holidays, custom_holidays
|   |-- test_shared.py                   # AppContext, Mode enum
|   |-- test_helper_functions.py         # percentageChange, isNowInTimePeriod, etc.
|
|-- zerodha/                             # 5 test files
|   |-- test_zerodha_analysis.py         # ZerodhaTickerManager WebSocket lifecycle
|   |-- test_live_options_engine.py      # LiveOptionsEngine cooldowns, dispatch
|   |-- test_live_stock_engine.py        # LiveStockEngine VWAP, ORB, imbalance signals
|   |-- test_kite_connect.py             # Modified KiteConnect enctoken auth
|   |-- test_exceptions.py              # Custom exception types
|
|-- notification/                        # 2 test files
|   |-- test_notification.py             # TELEGRAM_NOTIFICATIONS send, retry, routing
|   |-- test_bot_listener.py             # All Telegram bot commands (/status, /ltp, etc.)
|
|-- post_market_analysis/                # 9 test files
|   |-- test_base.py, test_registry.py, test_runner.py, test_summary.py
|   |-- test_fii_dii.py, test_fo_participant_oi.py, test_index_returns.py
|   |-- test_sector_performance.py, test_analysis.py
|
|-- premarket/                           # 5 test files
|   |-- test_fetching.py, test_formatting.py, test_helpers.py
|   |-- test_parsing.py, test_report_runner.py
```

### Test Conventions

| Convention | Detail |
|------------|--------|
| Framework | `pytest` 9.0.2 |
| Mocking | `unittest.mock.patch`, `MagicMock` — all HTTP, Zerodha API, and Telegram calls mocked |
| AppContext patching | `patch("common.shared.app_ctx", mock_obj)` covers all analyser mode checks |
| Fixtures | `conftest.py` at each level provides `mock_stock`, `make_price_df`, `make_sensibull_ctx`, etc. |
| No network | Zero network calls; `yfinance`, `requests`, and all WebSocket code mocked |
| Run command | `PYTHONPATH=$(pwd) .venv/bin/python -m pytest tests/ -q` |

### Key Testing Facts (Accuracy Notes)

These method names and behaviors were discovered/verified during test creation:

| Item | Actual (verified) | DESIGN.md had |
|------|------------------|---------------|
| TechnicalAnalyser Bollinger | `analyse_Bolinger_band` (capital B, lowercase olinger) | `analyse_bollinger_bands` |
| OIChainAnalyser intraday trend | `analyse_intraday_oi_trend` | described as OI_TREND |
| LiveStraddleAnalyser skew check | `check_iv_skew_reversal(agg, options_live, spot)` | `check_skew_flip` |
| LiveOIAnalyser PCR sustained | `check_pcr_sustained_trend(history)` | `check_pcr_sustained` |
| `check_implied_move_boundary` | returns `"RANGE_BOUNDARY"` signal | — |
| FuturesAnalyser data source | `stock.zerodha_ctx["futures_data"]["current"]` DataFrame | `stock.future_data` |
| MaxPainAnalyser far-expiry gate | Parses actual expiry date string vs `date.today()` | — |
| PCR divergence data path | `underlying_base_stats["per_expiry_pcr"]` dict | `per_expiry_map` |

---

## Appendix A: Analyzer Method Registration Quick Reference

To add a new analysis method to any analyzer:

```python
class MyAnalyzer(BaseAnalyzer):

    @BaseAnalyzer.both          # Run in both intraday and positional
    @BaseAnalyzer.index_both    # Also run for indices
    def analyse_my_signal(self, stock: Stock):
        # Your analysis logic
        # ...
        if signal_detected:
            MySignal = namedtuple("MySignal", ["field1", "field2"])
            stock.set_analysis("BULLISH", "MY_SIGNAL", MySignal(...))
            return True
        return False
```

Then add to `common/constants.py`:
```python
ANALYSIS_WEIGHTS["MY_SIGNAL"] = 12  # weight
# Add to appropriate category set (TECHNICAL/OPTIONS/FUTURES)
```

## Appendix B: Key File Sizes (for context budget awareness)

| File | Notes |
|------|-------|
| `common/Stock.py` | Core data model; delegates live ticks to TickStore |
| `analyser/TechnicalAnalyser.py` | ~63KB — 12+ analysis methods |
| `analyser/OIChainAnalyser.py` | ~68KB — complex OI chain logic |
| `intraday/intraday_monitor.py` | ~55KB — main orchestration |
| `backtest/optimizer.py` | ~60KB — Optuna search spaces for all methods |
| `analyser/PanicModeAnalyser.py` | Composite analyser — reads all other analysers' output |

## Appendix C: External Dependencies

| Package | Usage |
|---------|-------|
| `yfinance` | Historical + intraday OHLCV data |
| `pandas` | All data manipulation |
| `numpy` | Numerical calculations |
| `scipy` | Black-Scholes IV solver (brentq) |
| `optuna` | Bayesian hyperparameter optimization |
| `transformers` | FinBERT sentiment model |
| `twisted` / `autobahn` | WebSocket client for Zerodha |
| `pandas_market_calendars` | NSE market calendar |
| `python-telegram-bot` | Telegram bot + notifications |
| `requests` | HTTP calls to NSE, StockEdge, Sensibull, Gemini API |
| `feedparser` | Google News RSS parsing |
