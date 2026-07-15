# StockAnalysis

## Project Overview

StockAnalysis is an automated analysis system for Indian equity and derivatives markets (NSE). It targets NIFTY weekly options traders using live Zerodha WebSocket data for real-time tick-level analysis, Sensibull for OI chain and PCR data, and yfinance for historical price data. The system runs as 8 always-on microservices (monolith, data-gateway, market-data, analysis-engine, notification-service, resource-monitor, auth-service, Redis) with Redis as the sole shared-state and message-broker dependency.

---

---

## Operating Modes

The monolith (`intraday/intraday_monitor.py`) runs 24/7 as an always-on systemd service. It self-schedules the daily flow internally — no timers, no restarts:

| Phase | Time (IST) | Description |
|-------|-----------|-------------|
| **IDLE** | 00:00 – 09:00 | Overnight — all services idle, Redis keeps yesterday's positional data |
| **PRE-MARKET** | 09:00 – 09:15 | Global cues report (sector, FII/DII, F&O OI, NSE indices) + NSE pre-open at 09:07 + morning bias |
| **INTRADAY** | 09:15 – 15:30 | Analysis loop every ~310s — reads fresh data from Redis (published by data-gateway) |
| **WAITING** | 15:30 – 20:00 | Idle — data-gateway fetches positional data at 19:00 |
| **POSITIONAL** | 20:00 – ~20:30 | EOD analysis — reads 2y daily data + positional Sensibull from Redis |
| **IDLE** | 20:30 – 24:00 | Overnight — analysis + priceData retained in memory for next morning's bias |

Mode selection in production (`PRODUCTION=1`) is time-based via `_run_daily_loop()`. In dev mode, `DEV_INTRADAY=1` or `DEV_POSITIONAL=1` select manually via `start_stock_analysis()`.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                  Always-Running Microservices (Phase 1–4)                   │
│                                                                              │
│  ┌──────────┐    ┌──────────────────┐    ┌───────────────────────────┐      │
│  │  Redis 7 │◄───│  Data Gateway    │    │  Monolith                 │      │
│  │ (message │    │  (24/7)          │    │  (24/7 — self-scheduling) │      │
│  │  broker) │    │                  │    │                           │      │
│  │          │    │  09:15-15:30:    │    │  09:00: pre-market        │      │
│  │ + shared │    │    yfinance 5m   │    │  09:07: NSE pre-open      │      │
│  │   state  │    │    + Sensibull   │    │  09:10: morning bias      │      │
│  │          │    │    (parallel)    │    │  09:15: intraday loop     │      │
│  │          │    │    → data:price:* │    │  15:30: idle              │      │
│  │          │    │    → data:sensibull:* │  20:00: positional analysis│     │
│  │          │    │  + prevDayOHLCV  │    │                           │      │
│  │          │    │    daily refresh │    │  Reads from Redis:        │      │
│  │          │    │  19:00: positional│   │    data:price:*            │      │
│  │          │    │    (2y daily)    │    │    data:sensibull:*        │      │
│  │          │    │  Otherwise: idle │    │    data:zerodha:*         │      │
│  │          │    └────────┬─────────┘    └───────────┬───────────────┘      │
│  │          │             │                          │                       │
│  │          │  data:cycle_stream         notification:jobs                   │
│  │          │  data:cycle_ready          (Redis stream)                      │
│  │          │  (Pub/Sub)                                                    │
│  │          │                          │                                    │
│  │          │  ┌───────────────────┐   │  ┌──────────────────────────┐      │
│  │          │  │ Market Data (24/7)│   │  │ Notification Service(24/7)│     │
│  │          │  │ services/market_  │   │  │ services/notification-    │     │
│  │          │  │   data/main.py    │   │  │   service/main.py         │     │
│  │          │  │  WS1: equity/idx  │   │  │  Consumes notification:   │     │
│  │          │  │  WS2: option ticks│   │  │    jobs stream            │     │
│  │          │  │  Sensibull WS     │   │  │  Telegram + Discord       │     │
│  │          │  │  → data:tick:*    │   │  │  3x retry + dead letter   │     │
│  │          │  │  → data:options_  │   │  │  → alerts_delivered/      │     │
│  │          │  │    agg:*          │   │  │    alerts_failed counters │     │
│  │          │  │  → signal:channel │   │  └──────────────────────────┘     │
│  │          │  │    (Pub/Sub)      │   │                                    │
│  │          │  │  + heartbeat →    │   │  ┌──────────────────────────┐      │
│  │          │  │   stats:system    │   │  │ Resource Monitor (24/7)  │      │
│  │          │  │   stats:stock:*   │   │  │ services/resource_       │      │
│  │          │  └───────────────────┘   │  │   monitor/main.py        │      │
│  │          │                          │  │  Polls system, per-      │      │
│  │          │  ┌───────────────────┐   │  │  service & Redis metrics │      │
│  │          │  │ stats:stock:*     │   │  │  every 30s → sys:latest:*│      │
│  │          │  │ stats:system      │   │  │    sys:ts:* time-series  │      │
│  │          │  │ stats:daily:*     │   │  │  Proactive alerts        │      │
│  │          │  │ (metrics/counters)│   │  │  → notification:jobs     │      │
│  │          │  └───────────────────┘   │  └──────────────────────────┘      │
│  └──────────┘                      │                                    │
│                                    │  ┌──────────────────────────┐      │
│                                    │  │ Analysis Engine (24/7)    │     │
│                                    │  │ services/analysis_engine/ │     │
│                                    │  │  Consumes data:cycle_     │     │
│                                    │  │    stream                 │     │
│                                    │  │  12 analysers + scoring   │     │
│                                    │  │  → analysis_count,        │     │
│                                    │  │    trends_found, errors   │     │
│                                    │  │    per-stock + system     │     │
│                                    │  └──────────────────────────┘     │
│                              ┌────────────────────────────────────┐    │
│                              │  Redis streams + Pub/Sub + sys:*  │    │
│                              └────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘

Data flow:
  data-gateway  → Redis hashes (data:price:*, data:sensibull:*, data:zerodha:*)
                  → Redis stream (data:cycle_stream) + Pub/Sub (data:cycle_ready)
  market-data   → WS1 (equity/index) + WS2 (option ticks) + Sensibull WS
                  → Redis hashes (data:tick:*, data:options_agg:*)
                  → Pub/Sub (signal:channel for live alerts)
                  → Heartbeat: stats:system + per-stock tick counters (every 30s)
  analysis-engine → Consumes data:cycle_stream → runs 12 analysers + scoring
                  → Writes analysis metrics (stats:stock:*, stats:system)
  resource-monitor → Polls psutil every 30s → sys:latest:* (system, per-service, Redis)
                  → sys:ts:* time-series (24h ZSET), sys:daily:* rollups (30d)
                  → Fires proactive alerts → notification:jobs
  monolith      → reads Redis hashes (sub-millisecond)
                  → writes notification:jobs stream
                  → alert counters: alerts_trend, alerts_confluence, etc.
  notification  → reads notification:jobs → sends Telegram/Discord
                  → increments alerts_delivered / alerts_failed
  metrics       → /debugstats bot command reads stats:system + stats:stock:*
  sysstats      → /sysstats bot command reads sys:latest:* + sys:ts:*
```

---

## Data Sources

| Source | What is fetched | Module | Who fetches |
|--------|-----------------|--------|-------------|
| **Zerodha Kite API** (historical) | Futures OHLCV + OI (daily 90-day, 5-min intraday) | `services/data_gateway/zerodha_fetcher.py` | **Data-gateway** (via `ZerodhaFuturesManager`) |
| **Zerodha WebSocket** | Live equity/index ticks, option chain ticks, futures ticks | `zerodha/zerodha_analysis.py` | Monolith (inline) |
| **Sensibull** (authenticated) | OI chain per-strike, PCR, ATM IV, max pain, IV chart history, daily OI/PCR history | `services/data_gateway/sensibull_fetcher.py` | **Data-gateway** (parallel, 10 workers; cookies from `auth:sensibull` Redis hash) |
| **Sensibull** (WebSocket) | Live option Greeks/IV for NIFTY/BANKNIFTY/SENSEX | `fno/sensibull_feed.py` | **Market-data** (wsrelay, free public feed) |
| **yfinance** | Equity/index/commodity/global OHLCV — 1y daily at startup, 5-min intraday, 2y daily for positional | `services/data_gateway/yfinance_fetcher.py` | **Data-gateway** |
| **NSE** | Holiday calendar, pre-open data, FII/DII flows, sector performance | `premarket/premarket_report.py`, `post_market_analysis/` | Monolith (self-contained HTTP) |

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
- **Morning bias**: extracts positional bias signals from 8 PM analysis results (fast path — zero HTTP, ~0.5s). Falls back to full recompute from Redis if monolith restarted overnight.
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
| `PRODUCTION` | `1` = production mode (always-running daily loop), `0` = dev |
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
| `ZERODHA_ENC_TOKEN` | Enctoken for API auth (auto-refreshed by auth-service; also updatable via `/enctoken` bot command) |

### Sensibull

| Variable | Purpose |
|----------|---------|
| `SENSIBULL_ACCESS_TOKEN` | Platform access token cookie (auto-refreshed by auth-service via Zerodha OAuth; fallback if Redis unavailable) |
| `SENSIBULL_CLIENT_INFO` | JWT client info cookie (auto-refreshed by auth-service; fallback if Redis unavailable) |

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

### Redis (required — all services use Redis)

| Variable | Purpose |
|----------|---------|
| `REDIS_URL` | Redis connection string (default: `redis://localhost:6379`) |
| `NOTIFICATION_CHANNEL` | `telegram`, `discord`, or `both` |
| `SENSIBULL_WORKERS` | Parallel Sensibull fetch workers (default: `10`) |

### Logging (unified across all services)

All services use `services/common/logging.py` via `get_logger("service-name")`. The monolith's `common/logging_util.py` is a thin shim that delegates to the same factory — all 44+ modules that import it get the unified logger transparently.

Log files (10 MB rotating, 3 backups each):

| File | Service |
|------|---------|
| `logs/monolith.log` | Monolith (intraday + positional) |
| `logs/data-gateway.log` | Data gateway |
| `logs/market-data.log` | Market-data (WebSocket ingestion) |
| `logs/analysis-engine.log` | Analysis engine (12 analysers + scoring) |
| `logs/notification-service.log` | Notification service |
| `logs/resource-monitor.log` | Resource monitor (system metrics + alerts) |
| `logs/cycle-subscriber.log` | Cycle subscriber (internal) |

Format: `28 13:26:31 | WARNING | SA.monolith | intraday_monitor.py:1234 | message`

Per-service log level override:

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

In production, the **auth-service** (`services/auth_service/main.py`) handles authentication automatically:
- **Scheduled refresh**: TOTP login at 09:00 and 18:50 IST every trading day
- **Reactive refresh**: listens to `auth:commands` Redis stream — other services can publish `refresh_enctoken` when they get 403/Bad Request from Zerodha
- **Redis-first enctoken**: all services read enctoken from `auth:zerodha` Redis hash (published by auth-service), with `.env` fallback for cold starts
- **Pub/Sub notification**: `auth:enctoken_refreshed` channel — services like data-gateway and market-data subscribe to update their KiteConnect instances in real-time

### Zerodha API Endpoint (important)

Zerodha deprecated enctoken-based access on `api.kite.trade` (returns "Bad Request"). The enctoken now only works on `kite.zerodha.com/oms/`. The `KiteConnect` class (`zerodha/zerodha_connect.py`) uses:
- `_default_root_uri = "https://kite.zerodha.com/oms"` — for all authenticated REST calls (historical_data, profile, quote)
- `_public_root_uri = "https://api.kite.trade"` — for the public `instruments()` endpoint (no auth required, not available on /oms)

### Automated Sensibull Authentication

Sensibull locked down their REST APIs — they now require `access_token` + `client_info` cookies for all endpoints. The **auth-service** automates the Sensibull login alongside the Zerodha refresh:

**Flow** (runs automatically after Zerodha TOTP login):
1. Uses the Zerodha session to hit Sensibull's OAuth login endpoint: `GET /pluto/auth/web/session/b/u/kite/platform/login`
2. Follows the OAuth redirect chain (auto-approves since Zerodha session is active) → gets `request_token`
3. POSTs `request_token` to Sensibull's generate endpoint: `POST /pluto/auth/web/session/b/u/kite/platform/generate`
4. Returns `access_token` + `client_info` cookies (expires ~15 hours)
5. Publishes to Redis hash `auth:sensibull` + Pub/Sub channel `auth:sensibull_refreshed`
6. Also writes to `.env` for restart persistence

The data-gateway reads Sensibull cookies from Redis first (auto-refreshed), falling back to env vars. Supports reactive refresh via `auth:commands` stream command `refresh_sensibull`.

**Sensibull API endpoints** (all require cookies + `Origin: https://web.sensibull.com` header):
- Stock info: `GET /v1/compute/cache/stock_info?tradingsymbol={symbol}` (renamed from `/cache/insights/stock_info`)
- OI chain: `POST /v1/compute/1/oi_graphs/oi_chart`
- IV chart: `GET /v1/compute/iv_chart/{symbol}`
- OI history: `POST /v1/compute/compute_intraday`

If the stock_info endpoint is unavailable, the fetcher falls back to reconstructing insights data from OI chain + IV chart (computes max pain from per-strike OI, IV percentile from 2-year IV history).

### systemd Services (always-running)

All services run 24/7 with `Restart=always`. `scripts/system_config` contains the unit definitions:

| Unit | Runs | Purpose |
|------|------|---------|
| `redis-server.service` | 24/7 (apt-managed) | Redis message broker (128MB, no persistence) |
| `stockanalysis-notification.service` | 24/7 | Notification stream consumer → Telegram/Discord |
| `stockanalysis-data-gateway.service` | 24/7 (self-scheduling) | yfinance + Sensibull → Redis hashes + cycle signals |
| `stockanalysis-market-data.service` | 24/7 | WebSocket ingestion (WS1 equity/index, WS2 options, Sensibull WS) → Redis snapshots + Pub/Sub signals |
| `stockanalysis-analysis-engine.service` | 24/7 | Consumes data:cycle_stream → runs 12 analysers + scoring → writes analysis metrics |
| `stockanalysis-resource-monitor.service` | 24/7 | Polls psutil + Redis metrics every 30s → sys:latest:* + sys:ts:* + proactive alerts → notification:jobs |
| `stockanalysis-auth.service` | 24/7 (self-scheduling) | Zerodha TOTP login (09:00 + 18:50) + Sensibull OAuth auto-login + reactive refresh via auth:commands stream |
| `stockanalysis.service` | 24/7 (self-scheduling) | Monolith — pre-market, intraday, positional analysis |

No timers. All services self-schedule. The auth-service handles both Zerodha enctoken and Sensibull access_token lifecycle.

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

# Data Gateway
make run-data-gateway            # Start data-gateway (foreground)
make run-data-gateway-dev        # Start data-gateway (dev intraday mode)
make svc-data-gateway-check      # Check data-gateway Redis registry + data keys
make svc-data-gateway-logs       # Tail data-gateway log

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
make logs                          # Monolith log (monolith.log)
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
make server-data-gateway-status    # Data-gateway status on server
make server-data-gateway-start     # Start data-gateway on server
make server-data-gateway-stop      # Stop data-gateway on server
make server-data-gateway-logs      # Data-gateway logs on server
make server-svcs-status            # All StockAnalysis service statuses
make timers-disable                # Disable old timers, enable always-on services
make server-enable-always-on       # Restart monolith as always-on service
make update-enctoken TOKEN=<tok>   # Update ZERODHA_ENC_TOKEN on server .env
```

---

## Project Structure

```
StockAnalysis/
├── analyser/          # All analyser classes (12 files incl. GEXAnalyser, OptionSellerCompositeAnalyser)
├── auth/              # auth_login.py — automated TOTP-based Zerodha enctoken refresh
├── backtest/          # Backtesting framework + Optuna optimizer
├── common/            # Stock.py, shared.py, constants.py, scoring.py, token_registry.py
├── configs/           # custom_holidays.json, ml_config.yaml, redis.conf
├── data/              # final_derivatives_list.json, backtest results
├── docs/              # DESIGN.md, DATA_SCHEMA.md, ANALYSER_DATA_SOURCES.md
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
├── services/          # Microservices (Phase 1–3 — extracted services)
│   ├── common/        # Shared infra: logging.py, redis_proxy.py, stock_loader.py, cycle_subscriber.py, stock_proxy.py, metrics.py, rate_limiter.py
│   ├── notification-service/  # Notification stream consumer (EXTRACTED)
│   ├── data_gateway/  # yfinance + Sensibull fetcher → Redis (EXTRACTED — Phase 1)
│   ├── market_data/   # WebSocket ingestion → Redis snapshots + Pub/Sub (EXTRACTED — Phase 2)
│   ├── analysis_engine/  # Stream consumer: 12 analysers + scoring (EXTRACTED — Phase 3)
│   ├── resource_monitor/ # System + per-service + Redis metrics collector (30s poll)
│   ├── auth_service/   # Zerodha enctoken + Sensibull OAuth lifecycle: scheduled TOTP login + reactive refresh (EXTRACTED)
│   └── coordinator/   # Orchestrator + intelligence + bot (compact mode, designed)
├── tests/             # 1251 tests across 69 files
├── zerodha/           # WebSocket lifecycle, TickStore, FuturesFetcher, LiveOptionsEngine
├── Makefile
├── .env.template
└── requirements.txt
```

### Key Files

| File | Purpose |
|------|---------|
| `intraday/intraday_monitor.py` | Main entry point — always-running daily loop, orchestration, observability |
| `services/data_gateway/main.py` | Data gateway — self-scheduling yfinance + Sensibull fetcher → Redis (24/7) |
| `services/data_gateway/sensibull_fetcher.py` | Parallel Sensibull fetcher (10 workers) → Redis hashes |
| `services/data_gateway/yfinance_fetcher.py` | yfinance fetcher → Redis hashes (initial + intraday + positional + prevDayOHLCV daily refresh) |
| `services/market_data/main.py` | Market-data service — WS1 (equity/index) + WS2 (options) + Sensibull WS ingestion → Redis snapshots (24/7) |
| `services/market_data/snapshot_publisher.py` | Publishes `data:tick:*` and `data:options_agg:*` Redis hashes at 1s interval |
| `services/market_data/signal_publisher.py` | Pub/Sub signal bus for live options alerts (`signal:channel`) |
| `services/analysis_engine/main.py` | Analysis-engine entry point — consumes data:cycle_stream, dispatches worker pool |
| `services/analysis_engine/worker.py` | Per-stock worker: loads from Redis, runs 12 analysers + scoring, writes metrics |
| `services/notification-service/main.py` | Notification stream consumer → Telegram/Discord (24/7) |
| `services/resource_monitor/main.py` | Resource monitor — polls psutil + Redis every 30s, stores sys:latest:* + sys:ts:* time-series, fires proactive alerts |
| `services/common/metrics.py` | Per-stock + system-wide counters in Redis (`stats:stock:*`, `stats:system`, `stats:daily:*`) — fail-safe |
| `services/common/cycle_subscriber.py` | Redis Pub/Sub + stream subscriber for cycle sync (monolith ↔ data-gateway) |
| `services/common/stock_loader.py` | Sync Stock object reconstruction from Redis hashes |
| `services/common/logging.py` | Unified per-service logger factory (`get_logger("service-name")`) |
| `services/common/redis_proxy.py` | Redis client wrapper (hset, hgetall, xadd, xreadgroup, publish, pubsub) |
| `common/logging_util.py` | Thin shim → delegates to `services/common/logging.py` (44 modules import this) |
| `common/Stock.py` | Core data model; delegates live ticks to TickStore |
| `common/constants.py` | ANALYSIS_WEIGHTS, priority thresholds, env var names, category sets |
| `common/shared.py` | AppContext singleton, Mode enum, global state |
| `common/scoring.py` | Score calculation, alignment bonus, should_notify() |
| `analyser/OptionSellerCompositeAnalyser.py` | Option-seller composite setups: GAMMA_TRAP, RANGE_BOUND_SETUP, SKEW_FADE_SETUP |
| `auth/auth_login.py` | Automated TOTP Zerodha login — called by auth-service at 09:00 + 18:50 |
| `services/auth_service/main.py` | Auth-service — Zerodha TOTP login + Sensibull OAuth auto-login + reactive refresh via auth:commands stream |
| `notification/commands/sysstats.py` | `/sysstats` bot command — live dashboard, 24h sparklines, Redis deep dive |
| `scripts/system_config` | systemd unit files (auth + notification + data-gateway + market-data + analysis-engine + resource-monitor + monolith — all always-on) |
| `analyser/Analyser.py` | BaseAnalyzer (decorator framework) + AnalyserOrchestrator |
| `zerodha/zerodha_connect.py` | Modified KiteConnect — enctoken auth, dual root URI (/oms for authenticated, api.kite.trade for public instruments) |
| `zerodha/futures_fetcher.py` | FuturesFetcher — Kite historical futures data (used by data-gateway's ZerodhaFuturesManager) |
| `services/data_gateway/zerodha_fetcher.py` | ZerodhaFuturesManager — fetches futures data via Zerodha REST → publishes to Redis (data:zerodha:*) |
| `services/data_gateway/sensibull_fetcher.py` | Parallel Sensibull fetcher (10 workers) — stock_info + OI chain + IV chart + OI history → Redis hashes (authenticated via auth:sensibull cookies) |
| `notification/Notification.py` | Telegram message sender — routes through Redis stream |
| `notification/bot_listener.py` | Telegram bot entry point (Command Router) |
| `notification/commands/stats.py` | `/debugstats` command — system dashboard + per-stock + all-stocks sorted views |
| `configs/redis.conf` | Redis configuration (128MB maxmemory, no persistence) |

---

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/help` | All available commands |
| `/status` | System Health Dashboard — feed lag, RAM, LLM budget |
| `/debugstats` | System + per-stock metrics dashboard (tick rate, analysis runs, alert breakdowns) |
| `/debugstats <SYMBOL>` | Per-stock deep dive: tick count, option ticks, analysis count, alert breakdown |
| `/debugstats all [ticks\|errors\|stale\|nodata]` | All stocks sorted by selected metric |
| `/ltp <SYMBOL>` | Last traded price + % change |
| `/gainers` | Top 5 gainers by % change |
| `/losers` | Top 5 losers by % change |
| `/sysstats` | System resource dashboard — CPU (per-core), RAM, services, Redis health |
| `/sysstats history` | 24h sparklines (CPU, RAM, Redis mem) + 7-day trend table |
| `/sysstats redis` | Redis deep dive — memory, clients, ops/s, hit rate, slowlog |
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

### Crash & Health Layers (fail-silent)

Three fail-silent layers injected in `intraday_monitor.py`:

1. **Crash handler** (`sys.excepthook`): sends fatal tracebacks to Telegram with `html.escape()`
2. **Heartbeat** (`HEALTHCHECK_URL`): pings dead-man's switch at end of every analysis cycle
3. **Zombie watchdog**: detects stale WebSocket options data (>120s), sends one-time alert per symbol

### Metrics & Counters System

A lightweight per-stock + system-wide counters system stored in Redis, providing real-time visibility into every stage of the pipeline. All functions are **fail-safe** — Redis unavailability only logs at DEBUG, never crashes business logic.

**Module:** `services/common/metrics.py`

**Redis key structure:**

| Key | Type | TTL | Contents |
|-----|------|-----|----------|
| `stats:stock:{symbol}` | HASH | persistent | Per-stock counters: tick_count, option_tick_count, analysis_count, last_analysis_result/duration/time, trends_found, analysis_errors, alerts_trend/confluence/live_options/narrative/stale_data/attempted/delivered/failed |
| `stats:system` | HASH | persistent | System-wide: total_ticks, tick_rate, total_jobs_dispatched/completed, analysis_runs, result_success/no_data/error_count, alerts_attempted/delivered/failed, trends_found, total_confluences, stale_stocks_count, ws2_reconnects, snapshot_age_s |
| `stats:daily:{YYYY-MM-DD}` | HASH | 30-day TTL | Daily rollup: alerts_attempted/delivered/failed, analysis_runs, trends_found |

**Instrumentation points:**

| Service | What it records |
|---------|----------------|
| market-data | Per-stock tick_count + option_tick_count (batch-updated in 30s heartbeat), system tick_rate/total_ticks/ws2_reconnects/snapshot_age_s |
| analysis-engine | Per-stock analysis_count, last_analysis_result/duration/time, trends_found, analysis_errors; system result_success/no_data/error_count, analysis_runs |
| monolith (intraday) | Per-stock alerts_trend, alerts_confluence, alerts_stale_data; system total_jobs_dispatched/completed, trends_found, total_confluences, stale_stocks_count |
| monolith (live options) | Per-stock alerts_live_options |
| monolith (narrator) | Per-stock alerts_narrative |
| notification-service | Per-stock + system + daily alerts_delivered / alerts_failed |
| Notification.py | Per-stock + system + daily alerts_attempted (producer-side, when queuing to Redis stream) |

**Dual counting strategy:** `alerts_attempted` (producer-side) counts queued alerts. `alerts_delivered` + `alerts_failed` (consumer-side) count actual outcomes. `alerts_attempted - alerts_delivered - alerts_failed` = alerts stuck in stream.

**`/debugstats` bot command** (`notification/commands/stats.py`): Restricted to debug chat. Three views:
- System dashboard: tick pipeline, analysis pipeline, alerts & signals, auth
- Per-stock deep dive: data, analysis, alerts breakdown, derivatives
- All stocks sorted: by tick_count, analysis_errors, last_analysis_time, or NO_DATA filter

---

## Disclaimer

This tool is for educational and informational purposes only. It is not financial advice. The authors are not responsible for any financial losses.
