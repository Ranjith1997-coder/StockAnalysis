# StockAnalysis - Comprehensive Design Document

> **Last Updated**: March 2026 (Market Holiday Gatekeeper & Warning System added)
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
21. [Key Design Patterns](#21-key-design-patterns)
22. [Data Flow Diagrams](#22-data-flow-diagrams)

---

## 1. System Overview

### What It Does
An automated stock market analysis system for the Indian market (NSE) that:
- Monitors stocks in **real-time during market hours** (intraday mode, 5-min intervals, 9:15 AM - 3:30 PM)
- Performs **end-of-day positional analysis** (daily data, ~2 years history)
- Tracks **live NIFTY/BANKNIFTY options per tick** via Zerodha WebSocket (zone-based subscription)
- Runs **8 specialized analyzers** across technical, volume, candlestick, options, futures, IV, PCR, max pain, and OI chain domains
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
|   |-- IVAnalyser.py                # Implied Volatility spike & trend
|   |-- PCRAnalyser.py               # Put-Call Ratio analysis (5 methods)
|   |-- MaxPainAnalyser.py           # Max Pain deviation, trend, alignment
|   |-- OIChainAnalyser.py           # OI-based S/R, buildup, walls, shifts
|   |-- Futures_Analyser.py          # Futures action, PVO, ORB with dynamic thresholds
|   |-- LiveOIAnalyser.py            # NEW: Real-time OI wall breach, PCR crossover, live max pain
|   |-- LiveStraddleAnalyser.py      # NEW: Straddle decay, IV skew flip, implied move boundary
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
|   |-- Stock.py                     # Core Stock data model (~68KB)
|   |-- shared.py                    # AppContext singleton, Mode enum, global state
|   |-- constants.py                 # All weights, thresholds, env vars, categories
|   |-- scoring.py                   # Score calculation, alignment bonus, should_notify()
|   |-- helperFunctions.py           # Utilities (percentage change, JSON I/O, time checks)
|   |-- logging_util.py              # Logger configuration
|   |-- token_registry.py            # TokenRegistry — O(1) tick routing, zone management
|
|-- notification/
|   |-- Notification.py              # Telegram message sender
|   |-- bot_listener.py              # Telegram bot with interactive commands (/help, /status, /ltp, /gainers, /losers, /watchlist, /holidays, /enctoken)
|
|-- nse/
|   |-- nse_derivative_data.py       # NSE API wrappers (futures, options, bhav copy, etc.)
|   |-- nse_utils.py                 # NSE request utilities, date helpers, market calendar
|
|-- zerodha/
|   |-- zerodha_connect.py           # Modified KiteConnect with enctoken auth
|   |-- zerodha_analysis.py          # ZerodhaTickerManager (WebSocket lifecycle)
|   |-- zerodha_ticker.py            # Custom KiteTicker (Twisted/Autobahn WebSocket)
|
|-- premarket/
|   |-- premarket_report.py          # Global cues, bonds, commodities, VIX, FII/DII, pre-open
|
|-- post_market_analysis/
|   |-- base.py                      # Abstract PostMarketSource
|   |-- registry.py                  # SOURCE_CLASSES list
|   |-- runner.py                    # Pipeline: fetch -> normalize -> analyze -> summarize
|   |-- summary.py                   # HTML formatters per source type
|   |-- sources/                     # Individual source implementations
|   |-- analyzer.py                  # Post-market data analyzer/dispatcher
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
|   |-- OptionDataCollection.py      # Legacy NSE option chain scraper
|   |-- OptionDatabaseStorage.py     # Legacy MySQL storage (incomplete)
|
|-- ml_pipeline/                     # ML prediction pipeline (XGBoost, LightGBM, RF, Ensemble)
|-- scripts/                         # Utility scripts
|-- configs/                         # Configuration files
|-- data/                            # Stock lists (final_derivatives_list.json, etc.)
|-- docs/                            # Documentation
|-- tests/                           # Test files
```

---

## 3. Core Data Model

### `common/Stock.py` - The Stock Class

The `Stock` class is the central data container (~68KB). Every stock/index tracked by the system is represented as a Stock instance.

#### Key Attributes

```python
class Stock:
    stock_symbol: str           # e.g., "RELIANCE", "NIFTY 50"
    stock_name: str
    ltp: float                  # Last traded price (from WebSocket ticks)
    priceData: pd.DataFrame     # OHLCV DataFrame (from yfinance)

    # Derivatives data
    zerodha_ctx: dict           # Zerodha option chain & futures data
    sensibull_ctx: dict         # Sensibull OI chain, PCR, max pain

    # Live WebSocket options data (NEW — populated by ZerodhaTickerManager per tick)
    options_live: dict          # {strike: {"CE": {ltp, oi, prev_oi, volume, buy_qty, sell_qty, ohlc, depth, ts}, "PE": {...}}}
    options_aggregate: dict     # Computed aggregates updated after each option tick:
                                #   total_ce_oi, total_pe_oi, live_pcr, atm_strike,
                                #   atm_straddle_premium, atm_iv_ce, atm_iv_pe, iv_skew,
                                #   max_oi_ce_strike, max_oi_pe_strike,
                                #   net_ce_oi_change, net_pe_oi_change, last_updated
    futures_live: dict          # {expiry: {ltp, oi, volume, ...}}

    # Candlestick pattern support
    current: dict               # Current candle OHLCV
    previous: dict              # Previous candle OHLCV
    previous_previous: dict     # Two candles back

    # Analysis results
    analysis: dict = {
        "BULLISH": {},          # signal_type -> namedtuple
        "BEARISH": {},
        "NEUTRAL": {},
        "NoOfTrends": 0         # Legacy counter
    }

    DERIVATIVE_DATA_LENGTH: int # Max stored derivative snapshots
```

#### Key Methods

| Method | Purpose |
|--------|---------|
| `set_analysis(sentiment, signal_type, data)` | Store analysis result (BULLISH/BEARISH/NEUTRAL) |
| `get_futures_and_options_data_from_nse_intraday()` | Fetch NSE derivatives data |
| `update_zerodha_data(tick)` | Process real-time index/equity tick from WebSocket (safe `.get()` defaults — index ticks lack volume/OI fields) |
| `update_option_tick(strike, option_type, tick)` | Store live option tick into `options_live[strike][CE\|PE]` |
| `update_futures_tick(expiry, tick)` | Store live futures tick into `futures_live[expiry]` |
| `recompute_options_aggregate(spot_price)` | Recompute PCR, ATM strike, straddle premium, OI walls from current `options_live` |
| IV calculation methods | Black-Scholes using `scipy.optimize.brentq` |

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
```

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
    global_indices_obj_dict: dict
    stockExpires: list            # F&O expiry dates
    mode: Mode                    # Current operating mode
    zd_ticker_manager             # ZerodhaTickerManager reference
    zd_kc                         # KiteConnect instance
    token_registry: TokenRegistry # Central token registry for O(1) tick routing
    signal_bus: SignalBus         # Cross-layer signal event bus (when ENABLE_INTELLIGENCE=1)
    correlator: SignalCorrelator  # Cross-layer confluence detector (when ENABLE_INTELLIGENCE=1)
    narrator: MarketNarrator      # LLM narrative generator (when ENABLE_NARRATOR=1)

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
| `analyse_bollinger_bands` | BOLLINGER_BANDS | period=20, std=2.0 | Momentum strategy (not mean-reversion) |
| `analyse_vwap` | VWAP | deviation threshold | VWAP deviation for intraday bias |
| `analyse_atr` | ATR | period=14 | ATR-based volatility assessment |
| `analyse_buy_sell_quantity` | BUY_SELL_QTY | - | Bid/ask quantity imbalance |
| `analyse_supertrend` | SUPERTREND | period=14, multiplier=2.5 | Supertrend indicator direction change |
| `analyse_rsi_divergence` | RSI_DIVERGENCE | - | Swing high/low detection with RSI divergence |
| `analyse_stochastic` | STOCHASTIC | K=5, D=5, upper=90, lower=30 | Stochastic oscillator with signal strength classification |
| `analyse_obv_divergence` | OBV_DIVERGENCE | - | OBV vs price divergence |
| `analyse_pivot_points` | PIVOT_POINTS | - | Classic pivot point S/R levels |

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

| Method | Signal Type | Description |
|--------|-------------|-------------|
| `analyse_iv_spike` | IV_SPIKE | IV percentage change exceeds threshold (from Zerodha ATM option) |
| `analyse_iv_trend` | IV_TREND | N consecutive days of rising/falling IV |

Both always classified as **NEUTRAL** (IV is directionally ambiguous).

### 7.5 PCRAnalyser (`analyser/PCRAnalyser.py`)

Uses Sensibull data for Put-Call Ratio analysis.

| Method | Signal Type | Logic |
|--------|-------------|-------|
| `analyse_pcr_extreme_zones` | PCR_EXTREME | Contrarian: PCR < 0.3 = Bullish, PCR > 1.5 = Bearish |
| `analyse_pcr_directional_bias` | PCR_BIAS | PCR < 0.5 = Bearish, PCR > 1.2 = Bullish |
| `analyse_pcr_trend` | PCR_TREND | 3-day consistent rising (Bullish) or falling (Bearish), >10% change |
| `analyse_pcr_divergence` | PCR_DIVERGENCE | Near vs far month PCR diff > 1.2 = NEUTRAL uncertainty signal |
| `analyse_pcr_reversal` | PCR_REVERSAL | Zone crossover or trend direction reversal (intraday only) |

### 7.6 MaxPainAnalyser (`analyser/MaxPainAnalyser.py`)

Uses Sensibull pre-calculated max pain data.

| Method | Signal Type | Logic |
|--------|-------------|-------|
| `analyse_max_pain_deviation` | MAX_PAIN | Price deviation from max pain strike. 12-day expiry proximity gate. MODERATE (2-5%) or STRONG (>5%) |
| `analyse_max_pain_trend` | MAX_PAIN_TREND | Converging (price moving toward max pain) or diverging analysis from historical snapshots |
| `analyse_max_pain_alignment` | MAX_PAIN_ALIGNMENT | Cross-validates max_pain_type vs pcr_type for signal confidence |

### 7.7 OIChainAnalyser (`analyser/OIChainAnalyser.py`, ~68KB)

Per-strike OI chain analysis from Sensibull.

| Method | Signal Type | Description |
|--------|-------------|-------------|
| OI-based support/resistance | OI_SUPPORT_RESISTANCE | Identifies S/R from OI concentration (proximity gate to current price) |
| OI buildup detection | OI_BUILDUP | Fresh writing detection with dominant ratio (call vs put) |
| OI wall detection | OI_WALL | Statistical outlier detection for massive OI at specific strikes |
| OI shift / position migration | OI_SHIFT | Tracks movement of OI concentration between strikes |
| Intraday OI trend | OI_TREND | Multi-snapshot PCR trend within the day |
| S/R level shift tracking | SR_SHIFT | Monitors when key S/R levels change |

### 7.8 FuturesAnalyser (`analyser/Futures_Analyser.py`)

Enhanced with dynamic thresholds and multi-timeframe analysis.

| Method | Signal Type | Description |
|--------|-------------|-------------|
| `analyse_futures_action` | FUTURES_ACTION | Detects: long_buildup, short_buildup, short_covering, long_unwinding |
| PVO patterns | PVO | Price-Volume-OI pattern detection |
| ORB breakout | ORB | Opening Range Breakout with OI + volume confirmation |

**Dynamic Thresholds**:
- ATR-based price threshold (adapts to volatility)
- OI-volatility-based OI threshold
- Multi-timeframe: 5-candle short-term + 15-candle medium-term analysis

**Signal Scoring System** (100-point internal score):
- OI component, Volume component, Trend component, Momentum component, Time component, Risk-reward component

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
        score_result.total_score >= MIN_NOTIFICATION_SCORE  # 75
        and score_result.confidence_pct >= 65               # 65% alignment gate
    )
```

### `common/constants.py` - ANALYSIS_WEIGHTS

Contains ~50 signal types with weights, e.g.:
```python
ANALYSIS_WEIGHTS = {
    "RSI": 15,
    "MACD": 12,
    "EMA_CROSSOVER": 10,
    "BOLLINGER_BANDS": 10,
    "SUPERTREND": 14,
    "FUTURES_ACTION": 18,
    "OI_WALL": 16,
    "PCR_EXTREME": 12,
    "MAX_PAIN": 10,
    # ... etc
}

NEUTRAL_EXCLUDE_FROM_SCORE = {"IV_SPIKE", "IV_TREND"}  # Don't count toward directional score
```

---

## 9. Intraday Monitor (Main Entry Point)

### `intraday/intraday_monitor.py` (~55KB)

This is the production entry point that orchestrates the entire system.

#### Registered Analyzers
```python
orchestrator.register([
    TechnicalAnalyser(),
    VolumeAnalyser(),
    CandleStickAnalyser(),
    IVAnalyser(),
    PCRAnalyser(),
    MaxPainAnalyser(),
    OIChainAnalyser(),
    FuturesAnalyser()
])
```

#### Production Daily Timeline

```
07:00 AM  - System starts, loads stock list from final_derivatives_list.json
09:07 AM  - Send global cues report (premarket)
09:08 AM  - Send pre-open session report
09:15 AM  - Market opens, intraday loop begins
            |
            +--> Every ~310 seconds:
            |      1. Download 5-min price data (yfinance)
            |      2. Fetch derivatives data (NSE + Zerodha + Sensibull)
            |      3. Run all intraday analyzer methods on each stock
            |      4. Score signals via scoring engine
            |      5. If should_notify() -> send Telegram alert
            |      6. Reset stock.analysis dict for next iteration
            |
03:30 PM  - Market closes, intraday loop stops
04:00 PM  - Run positional analysis (daily timeframe)
            |-- Download daily price data
            |-- Run all positional analyzer methods
            |-- Score and notify
            |-- Run post-market analysis pipeline
            |-- Send post-market summary reports
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

#### Zerodha WebSocket Integration
- WebSocket connects at startup for real-time tick data
- Ticks update `stock.ltp` and derivatives context
- Optional Telegram bot listener runs in a separate thread

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
|-- base.py       # Abstract PostMarketSource
|-- registry.py   # SOURCE_CLASSES list
|-- runner.py     # Pipeline orchestrator
|-- summary.py    # HTML formatters
|-- sources/      # Concrete source implementations
|-- analyzer.py   # Data analyzer/dispatcher
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
|-- zerodha_analysis.py   # WebSocket manager
|-- zerodha_ticker.py     # WebSocket client (Twisted/Autobahn)
```

### `zerodha_connect.py` - Modified KiteConnect

Supports **enctoken authentication** (in addition to standard api_key + access_token):
```python
# Authorization header: "enctoken {token}" instead of "token api_key:access_token"
self.enc_token used in headers
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

Runs inside `ZerodhaTickerManager` when `ENABLE_LIVE_OPTIONS=1`. Calls both live analysers and enforces cooldowns:

```python
class LiveOptionsEngine:
    COOLDOWNS = {
        "PCR_CROSSOVER_BULLISH/BEARISH": 600,   # 10 min
        "PCR_EXTREME_PE/CE":            900,    # 15 min
        "CE_WALL_BREACH/PE_WALL_BREACH": 900,   # 15 min
        "IV_EXPANDING/IV_COMPRESSING":  900,    # 15 min
        "RANGE_BOUNDARY":               1800,   # 30 min
        "SKEW_FLIP_BULLISH/BEARISH":    600,    # 10 min
        "PCR_SUSTAINED_BULLISH/BEARISH": 1200,  # 20 min
    }
```

### 7.9 LiveOIAnalyser (`analyser/LiveOIAnalyser.py`)

Real-time OI analysis using live WebSocket ticks (not polled Sensibull data):

| Method | Alert Type | Signal |
|--------|-----------|--------|
| `check_pcr_crossover` | `PCR_CROSSOVER_BULLISH/BEARISH` | PCR crosses 1.0 threshold |
| `check_pcr_extreme` | `PCR_EXTREME_PE/CE` | PCR > 1.3 (contrarian bullish) or < 0.7 (contrarian bearish) |
| `check_pcr_sustained` | `PCR_SUSTAINED_BULLISH/BEARISH` | PCR sustained above/below 1.0 for N ticks |
| `check_oi_wall_breach` | `CE_WALL_BREACH/PE_WALL_BREACH` | Max OI strike weakening ≥ 3% |
| `check_live_max_pain` | `LIVE_MAX_PAIN` | Live max pain deviates > 2% from spot |

### 7.10 LiveStraddleAnalyser (`analyser/LiveStraddleAnalyser.py`)

Real-time straddle and IV analysis:

| Method | Alert Type | Signal |
|--------|-----------|--------|
| `check_iv_change` | `IV_EXPANDING/IV_COMPRESSING` | Straddle changes ≥ 3% in 5 min with spot flat (±0.3%) |
| `check_implied_move_boundary` | `RANGE_BOUNDARY` | Spot used ≥ 80% of expected range (straddle × 0.68) |
| `check_skew_flip` | `SKEW_FLIP_BULLISH/BEARISH` | ATM CE/PE ratio crosses 1.0 |
| `check_straddle_decay_rate` | informational | Actual decay vs theoretical theta |

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
    |-- Runs all positional analysers on each stock/index
    |-- Emits positional signals to SignalBus
    |-- Restores mode = INTRADAY
    |
    v
Intraday loop begins (positional signals remain in correlator buffer for 6 hours)
```

Morning bias is skipped in POSITIONAL mode (EOD analysis already runs positional analysers).

#### Signal Emission Points

| Layer | Source | When |
|-------|--------|------|
| POSITIONAL | `intraday_monitor.compute_morning_bias()` | Once at startup |
| INTRADAY | `intraday_monitor.process_stock()` | Every 5-min cycle, per stock |
| LIVE | `LiveOptionsEngine.on_option_tick()` | Per tick (via LiveOI/LiveStraddle analysers) |

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
        # Routes to correct channel based on shared.app_ctx.mode:
        #   INTRADAY  -> INTRADAY_CHAT_ID
        #   POSITIONAL -> POSITIONAL_CHAT_ID
        # Only sends when is_production=True
        # Retries up to 3 times with exponential backoff (2s, 4s) on HTTP error,
        # Timeout, or ConnectionError. Timeout per attempt: 15s.

    def send_live_options_notification(message, parse_mode="HTML"):
        # Always routes to LIVE_OPTIONS_CHAT_ID (dedicated real-time channel)
        # Bypasses mode check — live options alerts are mode-independent
```

### Telegram Channels

| Channel | Env Var | Purpose |
|---------|---------|---------|
| Intraday | `INTRADAY_CHAT_ID` | 5-min stock/index analysis alerts |
| Positional | `POSITIONAL_CHAT_ID` | EOD swing trade alerts |
| Live Options | `LIVE_OPTIONS_CHAT_ID` | Real-time options tick signals (NEW) |

### `notification/bot_listener.py`

Telegram bot with interactive commands. Runs in a background thread via `init_telegram_bot()` using `python-telegram-bot` polling.

#### Commands

| Command | Description | Data source |
|---------|-------------|-------------|
| `/help` | List all available commands | Static |
| `/status` | System health snapshot: mode, WebSocket state, stock/index count, production flag, trading day | `shared.app_ctx`, `market_calendar` |
| `/ltp <SYMBOL>` | Last traded price + % change + previous close for any symbol (case-insensitive, searches indices, stocks, commodities, global indices) | `stock_token_obj_dict` |
| `/gainers` | Top 5 stocks by positive % change since previous close | `stock_token_obj_dict` |
| `/losers` | Top 5 stocks by negative % change since previous close | `stock_token_obj_dict` |
| `/watchlist` | Full subscription overview: indices, F&O stocks (with preview), commodities, global indices, Zerodha WebSocket status and token counts, options registry stats per index (zone breakdown + spot price), live futures LTP/OI, current & next expiry | `shared.app_ctx`, `token_registry`, `zerodha_ctx` |
| `/holidays` | Today's trading status + upcoming NSE holidays in the next 30 days | `market_calendar` |
| `/enctoken <token>` | Updates Zerodha enctoken in `.env`, reconnects WebSocket, subscribes options | `.env`, `ZerodhaTickerManager` |

#### Key helpers

```python
_find_stock_by_symbol(symbol)   # O(n) search across all 4 tracked dicts
_build_gainers_losers()         # Returns ([top5 gainers], [top5 losers]) sorted by % change
```

#### Telegram message size handling
`/watchlist` automatically splits messages that exceed Telegram's 4096-character limit into multiple sequential messages.

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

## 21. Key Design Patterns

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

### 13. Background Narrative Generation (Intelligence)
`MarketNarrator` uses a single-worker `ThreadPoolExecutor` to run LLM calls off the hot path. The raw alert fires instantly via Telegram; the LLM-generated narrative follows 1-3 seconds later without blocking the analysis pipeline. This pattern ensures the core system latency is unaffected by LLM response time.

---

## 22. Data Flow Diagrams

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
07:00  System startup, load config
09:07  Global cues report -> Telegram
09:08  Pre-open session report -> Telegram
09:15  +-- Intraday loop starts --------+
       |  Every 310s:                    |
       |    Fetch data -> Analyze ->     |
       |    Score -> Notify if needed    |
       +-- Loop until 15:30 ------------+
15:30  Intraday loop ends
16:00  Positional analysis (daily data)
       Post-market reports:
         - FII/DII flows
         - Sector performance
         - F&O participant OI
         - Index returns
       -> All sent to Telegram
```

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

| File | Size | Notes |
|------|------|-------|
| `common/Stock.py` | ~68KB | Core data model, many methods |
| `analyser/TechnicalAnalyser.py` | ~63KB | 12+ analysis methods |
| `analyser/OIChainAnalyser.py` | ~68KB | Complex OI chain logic |
| `intraday/intraday_monitor.py` | ~55KB | Main orchestration |
| `backtest/optimizer.py` | ~60KB | Search spaces for all methods |

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
