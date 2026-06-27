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
| **LIVE OPTIONS** | 9:15 AM – 3:30 PM (opt-in) | Per WebSocket tick (~ms) for NIFTY/BANKNIFTY/SENSEX options | Continuous |

Mode selection in production (`PRODUCTION=1`) is time-based. In dev mode, `DEV_INTRADAY=1` or `DEV_POSITIONAL=1` select manually.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Microservices (Phase 1)                       │
│                                                                  │
│  ┌──────────┐        ┌──────────────────┐                       │
│  │  Redis 7 │◄───────│  Monolith (until │                       │
│  │ (message │   LB   │  fully extracted)│                       │
│  │  broker) │        │ intraday_monitor │                       │
│  └────┬─────┘        │  .py             │                       │
│       │              └────────┬─────────┘                       │
│       │  notification:jobs    │ send_notification()             │
│       │  (stream)             │  ─→ Redis (primary)             │
│       │                       │   → HTTP (fallback)             │
│       ▼                       ▼                                 │
│  ┌───────────────────────────────┐                              │
│  │  notification-service         │  (EXTRACTED — Phase 1B)       │
│  │  services/notification-       │                              │
│  │  service/main.py              │                              │
│  │    └── Consumes Redis stream  │                              │
│  │    └── Telegram + Discord     │                              │
│  │    └── Retry + dead letter    │                              │
│  └───────────────────────────────┘                              │
│                                                                  │
│  Services to extract (planned):                                 │
│    □ data-gateway     — yfinance + Sensibull + Zerodha WS       │
│    □ analysis-engine  — 12 analysers + scoring                  │
│    □ orchestrator     — cycle coordination                      │
│    □ intelligence     — SignalBus + Correlator + LLM            │
│    □ bot-service      — Telegram bot commands                   │
└─────────────────────────────────────────────────────────────────┘

Current monolith flow:
intraday/intraday_monitor.py          ← Main entry point & orchestration
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
                   ├── PanicModeAnalyser        (active — MUST be last before composite)
                   └── OptionSellerCompositeAnalyser  (active — MUST be registered after PanicModeAnalyser)
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
| **PanicModeAnalyser** | `analyser/PanicModeAnalyser.py` | PANIC_MODE (≥4/6 conditions aligned), PANIC_EXHAUSTION (≥3/4 contrarian conditions) — reads all earlier analysers' output, **must precede OptionSellerCompositeAnalyser** |
| **OptionSellerCompositeAnalyser** | `analyser/OptionSellerCompositeAnalyser.py` | Three high-probability option-seller setups — GAMMA_TRAP (kill-switch: close shorts, directional breach confirmed), RANGE_BOUND_SETUP (Iron Condor / Strangle candidate: range-trapped + overpriced vol), SKEW_FADE_SETUP (directional credit spread: panic exhaustion at OI wall + reversal candle). All bypass score gate via `PRIORITY_OVERRIDE`. **Must be registered last** |
| **LiveOIAnalyser** | `analyser/LiveOIAnalyser.py` | Real-time: PCR crossover, PCR extreme, PCR sustained trend, OI wall breach |
| **LiveStraddleAnalyser** | `analyser/LiveStraddleAnalyser.py` | Real-time: IV expanding/compressing, implied move boundary, IV skew reversal |

---

## Live Options Engine (opt-in)

When `ENABLE_LIVE_OPTIONS=1`, the system subscribes to NIFTY, BANKNIFTY, and SENSEX weekly option chains via Zerodha WebSocket. SENSEX uses the BFO segment (BSE derivatives); NIFTY and BANKNIFTY use NFO (NSE derivatives). Zone-based subscription keeps token count manageable (94 tokens per index across 3 zones):

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
- **Notification gate**: `total_score >= MIN_NOTIFICATION_SCORE (110)` for intraday; `total_score >= MIN_NOTIFICATION_SCORE_POSITIONAL (150)` for EOD positional
- **Priority tiers**: LOW (≥35), MEDIUM (≥60), HIGH (≥90), CRITICAL (≥130)
- **Confidence**: `dominant_side_score / total_score × 100`
- **Winner-takes-all (composite setups)**: When `OptionSellerCompositeAnalyser` fires, it sets `PRIORITY_OVERRIDE` on `stock.analysis` and the orchestrator's `generate_analysis_message()` returns **only** the composite trade card (GAMMA_TRAP, RANGE_BOUND_SETUP, or SKEW_FADE_SETUP), suppressing all individual indicator output. This eliminates noise — the trader receives one clean, actionable card with no RSI/MACD/PCR clutter.
- **Positional price-move filter**: Stocks with `|ltp_change_perc| < 0.75%` are skipped entirely in positional mode to avoid mechanical signals (BASIS_EXPANDING, PCR noise) on flat days.

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
| `DEV_NOTIFY` | `1` = send Telegram alerts even in dev mode |

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
| `ZERODHA_TOTP_SECRET` | Base-32 TOTP secret for automated 2FA login (`auth/auth_login.py`) |
| `ZERODHA_ENC_TOKEN` | Enctoken for API auth (auto-refreshed by `auth/auth_login.py`; also updatable via `/enctoken` bot command) |

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

### Redis (required for notification service)

| Variable | Purpose |
|----------|---------|
| `REDIS_URL` | Redis connection string (default: `redis://localhost:6379`) |
| `NOTIFICATION_CHANNEL` | `telegram`, `discord`, or `both` |

### Per-Service Logging

All services log to `logs/{service_name}.log` with 10 MB rotating files (3 backups). Per-service log level override:

```bash
LOG_LEVEL=INFO NOTIFICATION_LOG_LEVEL=DEBUG make run-dev
```

| Env Var | Effect |
|----------|--------|
| `LOG_LEVEL` | Global default for all services (default: `INFO`) |
| `{SERVICE}_LOG_LEVEL` | Per-service override, e.g. `NOTIFICATION_LOG_LEVEL=DEBUG` |

---

## Automated Zerodha Authentication

`auth/auth_login.py` performs a fully automated TOTP-based Zerodha login and writes the fresh `ZERODHA_ENC_TOKEN` directly into `.env`, removing the need for manual token updates.

Requires `ZERODHA_USER`, `ZERODHA_PASS`, and `ZERODHA_TOTP_SECRET` in `.env`.

```bash
python auth/auth_login.py   # generates enctoken and writes it to .env
```

In production the `stockanalysis-auth.service` systemd unit (see `scripts/system_config`) runs this automatically at boot, **before** the intraday monitor starts. The `stockanalysis.service` and `stockanalysis-positional.service` both `Require=` the auth service, so a failed login halts the analysis service instead of running with an expired token.

`scripts/system_config` contains all four systemd unit files needed for a full EC2 deployment:

| Unit | Trigger | Purpose |
|------|---------|--------|
| `stockanalysis-auth.service` | At boot | TOTP login — writes fresh enctoken to `.env` |
| `stockanalysis.service` | Via timer | Intraday monitor during market hours |
| `stockanalysis.timer` | Mon–Fri 03:30 UTC (9:00 AM IST) | Triggers intraday service |
| `stockanalysis-positional.service` | Via timer | EOD positional analysis |
| `stockanalysis-positional.timer` | Mon–Fri 14:30 UTC (8:00 PM IST) | Triggers positional service |

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
# Setup
make venv                          # Create .venv/
make install                       # Install production dependencies
make install-dev                   # Install prod + dev/test tools
make install-deploy                # Install deploy tools (boto3, paramiko)
make env-check                     # Verify required .env variables are set

# Run
make run-prod                      # PRODUCTION=1 intraday monitor
make run-dev                       # DEV_INTRADAY=1 + notification service (auto start/stop)
make run-dev-positional            # DEV_POSITIONAL=1 EOD + notification service (auto start/stop)
make run-dev-stop                  # Stop notification service (manual cleanup)
make run-dev-stock-intraday STOCK=RELIANCE   # Single stock intraday
make run-dev-stock-positional STOCK=RELIANCE # Single stock positional
make run-dev-index-intraday INDEX=NIFTY      # Single index intraday
make run-dev-index-positional INDEX=NIFTY    # Single index positional
make run-premarket                 # Global cues + pre-open reports
make run-postmarket                # Post-market analysis pipeline
make deploy                        # git pull + restart service on EC2 via SSH
make service-stop                  # Start EC2 (if stopped) + stop service; exits early on holidays
make service-stop-force            # Same but bypasses holiday guard (dev use)

# Redis
make redis-install                 # Install Redis via Homebrew
make redis-start                   # Start Redis service
make redis-stop                    # Stop Redis service
make redis-status                  # Check Redis status + memory
make redis-config                  # Apply production config (128MB, no persistence)

# Notification Service
make run-notification              # Start notification service (foreground)
make svc-notify-test               # Send test notification to Redis stream
make svc-notification-check        # Check stream + consumer group
make svc-dead-letter               # View failed notifications

# Test
make test                          # Full test suite
make test-fast                     # Stop on first failure (-x)
make test-cov                      # With coverage report
make test-module MODULE=premarket  # Tests for a specific module

# Code quality
make lint                          # ruff check
make format                        # ruff format (auto-fix)
make typecheck                     # pyright type check

# Maintenance
make update-derivatives            # Refresh final_derivatives_list.json
make logs                          # Monolith log (legacy)
make logs-follow                   # Follow monolith log live
make logs-all                      # View last 20 lines of every service log
make logs-all-follow               # Follow all service logs live
make logs-svc SVC_LOG=data-gateway # View one service log
make logs-svc-follow SVC_LOG=...   # Follow one service log live
make logs-service                  # List all available service logs
make clean                         # Remove __pycache__, .pyc, pytest cache
make clean-all                     # clean + remove .venv

# Server (hacker@100.92.21.31)
make server-ssh                    # Open interactive SSH session
make server-logs                   # Tail last 50 lines on server
make server-logs-follow            # Live-follow service log on server
make server-status                 # Show stock_analysis.service status
make server-restart                # Restart stock_analysis.service
make server-pull                   # git pull on server repo
make server-df                     # Disk usage on server
make server-redis-status           # Redis status + memory on server
make server-redis-start            # Start Redis on server
make server-redis-config           # Apply Redis config on server
make server-notification-status    # Notification service status on server
make server-notification-logs      # Notification service logs on server
make server-notify-test            # Send test notification via server Redis
make server-svcs-status            # All StockAnalysis service statuses
make update-enctoken TOKEN=<tok>   # Update ZERODHA_ENC_TOKEN on server .env
```

---

## Project Structure

```
StockAnalysis/
├── analyser/          # All analyser classes (12 files incl. OptionSellerCompositeAnalyser)
├── auth/              # auth_login.py — automated TOTP-based Zerodha enctoken refresh
├── backtest/          # Backtesting framework + Optuna optimizer
├── common/            # Stock.py, shared.py, constants.py, scoring.py, token_registry.py
├── configs/           # custom_holidays.json, ml_config.yaml, redis.conf
├── data/              # final_derivatives_list.json, backtest results
├── docs/              # DESIGN.md, DATA_SCHEMA.md
├── fno/               # SensibullFetcher, sensibull_feed.py (OPTIONS_SOURCE=sensibull path)
├── intelligence/      # SignalBus, SignalCorrelator, MarketNarrator, GeminiClient
├── intraday/          # intraday_monitor.py — main entry point
├── ml_pipeline/       # ML prediction pipeline (XGBoost, LightGBM, RF, Ensemble)
├── notification/      # Telegram sender + bot commands (Command Router pattern)
├── nse/               # NSE API wrappers + market calendar helpers
├── plans/             # microservices_architecture.md — migration plan
├── post_market_analysis/  # FII/DII, sector perf, F&O OI, index returns pipeline
├── premarket/         # Global cues, bonds, commodities, pre-open report
├── scripts/           # deploy.py, service_stop.py (holiday-aware), system_config (systemd units)
├── sentiment/         # FinBERT news sentiment
├── services/          # Microservices (Phase 1 — extracted services)
│   ├── common/        # Shared infra: logging.py, redis_client.py, stock_proxy.py, health.py
│   ├── notification-service/  # Notification stream consumer (EXTRACTED — Phase 1B)
│   ├── data-gateway/  # yfinance + Sensibull + Zerodha WS (code ready, not deployed)
│   ├── coordinator/   # Orchestrator + intelligence + bot (compact mode, designed)
│   └── *-service/     # Future: orchestrator, analysis-engine, intelligence, bot, auth
├── tests/             # 951 tests across 41 files
├── zerodha/           # WebSocket lifecycle, TickStore, FuturesFetcher, LiveOptionsEngine
├── Makefile
├── .env.template
└── requirements.txt
```

### Key Files

| File | Purpose |
|------|---------|
| `intraday/intraday_monitor.py` | Main entry point, orchestration loop, observability layers |
| `common/Stock.py` | Core data model; delegates live ticks to TickStore |
| `common/constants.py` | ANALYSIS_WEIGHTS, priority thresholds, env var names, category sets |
| `common/shared.py` | AppContext singleton, Mode enum, global state |
| `common/scoring.py` | Score calculation, alignment bonus, should_notify() |
| `analyser/OptionSellerCompositeAnalyser.py` | Option-seller composite setups: GAMMA_TRAP, RANGE_BOUND_SETUP, SKEW_FADE_SETUP |
| `auth/auth_login.py` | Automated TOTP Zerodha login — writes fresh enctoken to `.env` |
| `scripts/system_config` | systemd unit files for EC2 deployment (auth + intraday + positional services + timers) |
| `analyser/Analyser.py` | BaseAnalyzer (decorator framework) + AnalyserOrchestrator |
| `zerodha/futures_fetcher.py` | FuturesFetcher — Kite historical futures data |
| `fno/sensibull_fetcher.py` | SensibullFetcher — Sensibull API (insights, OI chain, IV chart, OI history) |
| `notification/Notification.py` | Telegram message sender — routes through Redis by default, direct HTTP fallback |
| `notification/bot_listener.py` | Telegram bot entry point (Command Router) |
| `services/notification-service/main.py` | Notification stream consumer (EXTRACTED — Phase 1B) |
| `services/common/logging.py` | Per-service logger factory (`get_logger("service-name")`) |
| `services/common/stock_proxy.py` | Stock object ↔ Redis serialization |
| `configs/redis.conf` | Redis configuration (128MB maxmemory, no persistence) |

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
