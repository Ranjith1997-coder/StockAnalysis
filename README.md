# StockAnalysis

## Project Overview

StockAnalysis is an automated analysis system for Indian equity and derivatives markets (NSE). It targets NIFTY weekly options traders using live Zerodha WebSocket data for real-time tick-level analysis, Sensibull for OI chain and PCR data, and yfinance for historical price data.

---

## Operating Modes

The system runs in two primary modes, orchestrated from `intraday/intraday_monitor.py`:

| Mode | When | Data Interval | Loop |
|------|------|---------------|------|
| **INTRADAY** | 9:15 AM – 3:30 PM | 5-min candles (yfinance) + live WebSocket ticks (Zerodha) | Every ~310 seconds |
| **POSITIONAL** | After 4:00 PM (EOD) | Daily candles — 90-day daily for futures, 2-year daily for equity | Single run |
| **LIVE OPTIONS** | 9:15 AM – 3:30 PM (opt-in) | Per WebSocket tick (~ms) for NIFTY/BANKNIFTY options | Continuous |

Mode selection in production (`PRODUCTION=1`) is time-based. In dev mode, `DEV_INTRADAY=1` or `DEV_POSITIONAL=1` select manually.

---

## Architecture

```
intraday/intraday_monitor.py          ← Main entry point & orchestrator
         |
         ├── create_stock_and_index_objects()   yfinance daily data download
         ├── update_zerodha_option_chain()       Kite instruments API → zerodha_ctx
         ├── FuturesFetcher.fetch()              Kite historical API → futures_data
         ├── SensibullFetcher.fetch_data()       Sensibull insights → sensibull_ctx
         ├── SensibullFetcher.fetch_oi_chain()   Sensibull OI chain → sensibull_ctx
         ├── SensibullFetcher.fetch_iv_chart()   (positional only) daily IV history
         ├── SensibullFetcher.fetch_oi_history() (positional only) daily OI/PCR history
         └── AnalyserOrchestrator.run_all_*()    Runs registered analysers
                   |
                   ├── VolumeAnalyser           (active)
                   ├── TechnicalAnalyser        (active)
                   ├── CandleStickAnalyser      (active)
                   ├── IVAnalyser               (active)
                   ├── FuturesAnalyser          (active)
                   ├── PCRAnalyser              (active)
                   ├── MaxPainAnalyser          (active)
                   ├── OIChainAnalyser          (active)
                   └── PanicModeAnalyser        (active — MUST be last)
```

---

## Data Sources

| Source | What is fetched | Module |
|--------|-----------------|--------|
| **Zerodha Kite API** (historical) | Futures OHLCV + OI (daily 90-day, 5-min intraday) | `zerodha/futures_fetcher.py` |
| **Zerodha WebSocket** | Live equity/index ticks, option chain ticks, futures ticks | `zerodha/zerodha_analysis.py` |
| **Sensibull** | OI chain per-strike, PCR, ATM IV, max pain, IV chart history, daily OI/PCR history | `fno/sensibull_fetcher.py` |
| **yfinance** | Equity/index OHLCV — 1y daily at startup, 5-min intraday during market hours, 2y daily for positional | `intraday/intraday_monitor.py` |

---

## Analyser Modules

| Analyser | File | What it detects |
|----------|------|-----------------|
| **FuturesAnalyser** | `analyser/Futures_Analyser.py` | Long/short buildup, short covering, long unwinding; PVO patterns; ORB breakout; positional OI trend; cost of carry / backwardation; rollover pressure; intraday OI buildup from open |
| **TechnicalAnalyser** | `analyser/TechnicalAnalyser.py` | RSI, MACD, EMA crossover, Bollinger Bands, VWAP, ATR, Supertrend, RSI divergence, Stochastic, Pivot Points |
| **VolumeAnalyser** | `analyser/VolumeAnalyser.py` | Volume breakout (2x MA + price confirm), OBV divergence, volume climax (3x spike) |
| **CandleStickAnalyser** | `analyser/candleStickPatternAnalyser.py` | Marubozu, Hammer/Shooting Star, Engulfing/Piercing/Dark Cloud, Morning/Evening Star, continuation patterns |
| **IVAnalyser** | `analyser/IVAnalyser.py` | IV spike, IV trend, IV rank (IVP percentile), IV vs historical volatility |
| **PCRAnalyser** | `analyser/PCRAnalyser.py` | PCR extreme zones (contrarian), PCR directional bias, PCR trend (5-day), PCR intraday trend, PCR reversal, positional PCR reversal |
| **MaxPainAnalyser** | `analyser/MaxPainAnalyser.py` | Max pain deviation (moderate/strong), max pain trend (converging/diverging), max pain alignment with PCR |
| **OIChainAnalyser** | `analyser/OIChainAnalyser.py` | OI support/resistance, OI buildup, OI wall (statistical outlier), OI shift, intraday OI trend, intraday S/R shift, OI capitulation (positional), OI wall migration (positional), positional OI trend, OI acceleration |
| **PanicModeAnalyser** | `analyser/PanicModeAnalyser.py` | PANIC_MODE (≥4/6 conditions aligned), PANIC_EXHAUSTION (≥3/4 contrarian conditions) — reads all earlier analysers' output, **must be last** |
| **LiveOIAnalyser** | `analyser/LiveOIAnalyser.py` | Real-time: PCR crossover, PCR extreme, PCR sustained trend, OI wall breach |
| **LiveStraddleAnalyser** | `analyser/LiveStraddleAnalyser.py` | Real-time: IV expanding/compressing, implied move boundary, IV skew reversal |

---

## Live Options Engine (opt-in)

When `ENABLE_LIVE_OPTIONS=1`, the system subscribes to NIFTY and BANKNIFTY weekly option chains via Zerodha WebSocket. Zone-based subscription keeps token count manageable (94 tokens per index across 3 zones):

| Zone | Distance from ATM | WebSocket mode |
|------|--------------------|----------------|
| CORE | ±1% | FULL (OI + depth) |
| ACTIVE | 1–3% | FULL |
| PERIPHERAL | 3–5% | QUOTE |

Dynamic re-centering fires when spot moves ≥ 50 pts (1 strike gap): unsubscribes far strikes, subscribes new ones.

Live alerts go to a dedicated `LIVE_OPTIONS_CHAT_ID` Telegram channel with per-type cooldowns (10–30 min) to prevent flooding.

---

## Signal Scoring System

Every cycle, each fired signal contributes a weight to a total score:

```
total_score = base_score + alignment_bonus
```

- **Base score**: sum of `ANALYSIS_WEIGHTS[signal_type]` for all BULLISH or BEARISH signals
- **Alignment bonus**: applied when signals span multiple categories (TECHNICAL + OPTIONS = 1.5× on base)
- **Notification gate**: `total_score >= MIN_NOTIFICATION_SCORE (110)` to send Telegram alert
- **Priority tiers**: LOW (≥35), MEDIUM (≥60), HIGH (≥90), CRITICAL (≥130)
- **Confidence**: `dominant_side_score / total_score × 100`

---

## Intelligence Layer (opt-in)

When `ENABLE_INTELLIGENCE=1`:
- **SignalBus**: thread-safe pub/sub event bus — all analysers emit `Signal` objects
- **SignalCorrelator**: detects cross-layer confluence (LIVE + INTRADAY + POSITIONAL alignment)
- **Morning bias**: runs positional analysers at startup on 1y daily data, seeds correlator buffer
- **LiveStockEngine**: per-tick VWAP cross, bid/ask imbalance, ORB, day high/low break signals

When `ENABLE_NARRATOR=1` (requires `GEMINI_API_KEY`):
- **MarketNarrator**: LLM-powered trade thesis via Google Gemini Flash
- Triggered only on HIGH confluences (3+ layers aligned) with 30-min per-symbol cooldown
- Also generates EOD positional briefing after ~4 PM positional run

---

## Configuration (.env)

### Core

| Variable | Purpose |
|----------|---------|
| `PRODUCTION` | `1` = production mode (time-based mode selection), `0` = dev |
| `SHUTDOWN` | `1` = shutdown system after EOD analysis |
| `DEV_INTRADAY` | `1` = dev intraday mode |
| `DEV_POSITIONAL` | `1` = dev positional mode |
| `NO_OF_STOCKS` | Max stocks to analyze (`-1` = all) |
| `NO_OF_INDEX` | Max indices to analyze (`-1` = all) |
| `DEV_MAX_CYCLES` | Max intraday loop cycles in dev mode (`0` = unlimited) |
| `DEV_LOOP_WAIT_TIME` | Seconds between dev cycles (`-1` = use production wait) |

### Telegram

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_INTRADAY_TOKEN` | Bot token for intraday channel |
| `TELEGRAM_INTRADAY_CHAT_ID` | Chat ID for intraday alerts |
| `TELEGRAM_POSITIONAL_TOKEN` | Bot token for positional/EOD channel |
| `TELEGRAM_POSITIONAL_CHAT_ID` | Chat ID for positional/EOD alerts |
| `TELEGRAM_LIVE_OPTIONS_TOKEN` | Bot token for live options channel |
| `TELEGRAM_LIVE_OPTIONS_CHAT_ID` | Chat ID for real-time options alerts |

### Zerodha

| Variable | Purpose |
|----------|---------|
| `ZERODHA_USER` | Zerodha user ID |
| `ZERODHA_PASS` | Zerodha password |
| `ZERODHA_ENC_TOKEN` | Enctoken for API auth (expires — refresh via `/enctoken` bot command) |

### Feature Flags

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_ZERODHA_API` | `0` | Enable WebSocket connection + enctoken auth |
| `ENABLE_ZERODHA_DERIVATIVES` | `0` | Enable Zerodha Kite historical futures data fetch |
| `ENABLE_TELEGRAM_BOT` | `0` | Enable interactive Telegram bot |
| `ENABLE_POST_MARKET` | `0` | Enable post-market analysis pipeline |
| `ENABLE_LIVE_OPTIONS` | `0` | Enable real-time option chain tracking |
| `LIVE_OPTIONS_ONLY` | `0` | Skip all regular analysis — WebSocket live options only |
| `ENABLE_INTELLIGENCE` | `0` | Enable SignalBus + Correlator + morning bias |
| `ENABLE_NARRATOR` | `0` | Enable LLM trade narratives (requires `GEMINI_API_KEY`) |
| `OPTIONS_SOURCE` | `zerodha` | `zerodha` or `sensibull` for live option tick source |
| `HEALTHCHECK_URL` | (empty) | Dead-man's switch ping URL (e.g., healthchecks.io) |

---

## How to Run

### Installation

```bash
git clone https://github.com/yourusername/StockAnalysis.git
cd StockAnalysis
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.template .env   # fill in your credentials
```

### Run

```bash
# Intraday (dev mode)
# Set DEV_INTRADAY=1, PRODUCTION=0 in .env
python intraday/intraday_monitor.py

# Positional EOD (dev mode)
# Set DEV_POSITIONAL=1, PRODUCTION=0 in .env
python intraday/intraday_monitor.py

# Production (auto time-based mode selection)
# Set PRODUCTION=1 in .env
python intraday/intraday_monitor.py

# Analyze specific stock or index
python intraday/intraday_monitor.py --stock RELIANCE
python intraday/intraday_monitor.py --index NIFTY
```

### Makefile targets

```bash
make venv              # Create .venv/
make install           # Install production dependencies
make run-prod          # PRODUCTION=1 intraday monitor
make run-dev           # PRODUCTION=0 intraday monitor (safe)
make run-postmarket    # Post-market analysis pipeline
make deploy            # git pull + restart service on EC2 via SSH
make service-stop      # Start EC2 (if stopped) + stop service; exits early on holidays
make test              # Full test suite
make lint              # ruff check
make logs-follow       # Follow logs/monitor.log live
```

---

## Project Structure

```
StockAnalysis/
├── analyser/          # All analyser classes (11 files)
├── backtest/          # Backtesting framework + Optuna optimizer
├── common/            # Stock.py, shared.py, constants.py, scoring.py, token_registry.py
├── configs/           # custom_holidays.json, ml_config.yaml
├── data/              # final_derivatives_list.json, backtest results
├── docs/              # DESIGN.md, DATA_SCHEMA.md
├── fno/               # SensibullFetcher, sensibull_feed.py (OPTIONS_SOURCE=sensibull path)
├── intelligence/      # SignalBus, SignalCorrelator, MarketNarrator, GeminiClient
├── intraday/          # intraday_monitor.py — main entry point
├── ml_pipeline/       # ML prediction pipeline (XGBoost, LightGBM, RF, Ensemble)
├── notification/      # Telegram sender + bot commands (Command Router pattern)
├── nse/               # NSE API wrappers + market calendar helpers
├── post_market_analysis/  # FII/DII, sector perf, F&O OI, index returns pipeline
├── premarket/         # Global cues, bonds, commodities, pre-open report
├── scripts/           # deploy.py, service_stop.py (holiday-aware)
├── sentiment/         # FinBERT news sentiment
├── tests/             # 951 tests across 41 files
├── zerodha/           # WebSocket lifecycle, TickStore, FuturesFetcher, LiveOptionsEngine
├── Makefile
└── .env.template
```

### Key Files

| File | Purpose |
|------|---------|
| `intraday/intraday_monitor.py` | Main entry point, orchestration loop, observability layers |
| `common/Stock.py` | Core data model; delegates live ticks to TickStore |
| `common/constants.py` | ANALYSIS_WEIGHTS, priority thresholds, env var names, category sets |
| `common/shared.py` | AppContext singleton, Mode enum, global state |
| `common/scoring.py` | Score calculation, alignment bonus, should_notify() |
| `analyser/Analyser.py` | BaseAnalyzer (decorator framework) + AnalyserOrchestrator |
| `zerodha/futures_fetcher.py` | FuturesFetcher — Kite historical futures data |
| `fno/sensibull_fetcher.py` | SensibullFetcher — Sensibull API (insights, OI chain, IV chart, OI history) |
| `notification/Notification.py` | Telegram message sender (3 channels, retry logic) |
| `notification/bot_listener.py` | Telegram bot entry point (Command Router) |

---

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/help` | All available commands |
| `/status` | System Health Dashboard — feed lag, RAM, LLM budget |
| `/ltp <SYMBOL>` | Last traded price + % change |
| `/gainers` | Top 5 gainers by % change |
| `/losers` | Top 5 losers by % change |
| `/watchlist` | WebSocket subscription overview, options zones, futures LTP/OI |
| `/holidays` | Today's status + upcoming NSE holidays (next 30 days) |
| `/straddle <SYMBOL>` | Live ATM straddle: premium, ±1SD range, CE/PE LTPs, PCR |
| `/walls <SYMBOL>` | OI walls: CE/PE max-OI strikes, tick delta, session delta, Iron Condor zone |
| `/enctoken <token>` | Update Zerodha enctoken in `.env` + reconnect WebSocket |

---

## Post-Market Analysis

After EOD positional analysis, the pipeline fetches and formats:
- FII/DII cash and derivatives flows (last 5 days)
- Sector performance (top 5 gaining/losing)
- F&O participant OI breakdown (Client/DII/FII/Pro)
- NSE index returns

All are sent as HTML-formatted Telegram messages to the positional channel.

---

## Observability

Three fail-silent layers injected in `intraday_monitor.py`:

1. **Crash handler** (`sys.excepthook`): sends fatal tracebacks to Telegram with `html.escape()`
2. **Heartbeat** (`HEALTHCHECK_URL`): pings dead-man's switch at end of every analysis cycle
3. **Zombie watchdog**: detects stale WebSocket options data (>120s), sends one-time alert per symbol

---

## Disclaimer

This tool is for educational and informational purposes only. It is not financial advice. The authors are not responsible for any financial losses.
