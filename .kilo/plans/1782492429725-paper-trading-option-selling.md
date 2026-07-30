# Paper Trading System — Option Selling (Intraday + Positional) + PINN Volatility Surface

> **NOTE**: This plan has been superseded by the comprehensive design document at `docs/PAPER_TRADING_DESIGN.md`.
> That document includes a full bottleneck analysis, SPAN margin calculator design, corrected data sources,
> cost model, and updated implementation plan. Refer to it for implementation.
> This file is kept for historical context only.

## Context

The StockAnalysis system already generates high-quality option-seller signals:

1. **OptionSellerCompositeAnalyser** (`analyser/OptionSellerCompositeAnalyser.py`) produces three setups:
   - `RANGE_BOUND_SETUP` — Iron Condor / Strangle candidate with `put_wall_strike`, `call_wall_strike`, `setup_type`, `iv_percentile`, `max_pain_dev_pct`
   - `SKEW_FADE_SETUP` — Directional credit spread with `fade_direction`, `sr_level`, `exhaustion_confidence`
   - `GAMMA_TRAP` — Kill-switch warning with `direction`, `breach_signal`, `volume_signal`

2. **SignalCorrelator** (`intelligence/correlator.py`) detects cross-layer confluence (LIVE + INTRADAY + POSITIONAL) with `Confluence.symbol`, `.direction`, `.score`, `.level` (HIGH/MODERATE)

3. **Analysis results** flow through `analysis:results` Redis stream with full `analysis_json` containing all setup namedtuples

4. **Live option LTP** available for NIFTY, BANKNIFTY, SENSEX in `data:options_live:{symbol}` Redis hash (1-second refresh from market-data service's `SnapshotPublisher`). Each per-strike tick contains `ltp`, `oi`, `volume`, `iv`, `gamma`, `delta`, `theta`, `vega` (Greeks from Sensibull enrichment).

5. **Option aggregate** in `data:options_agg:{symbol}` — ATM strike, PCR, walls, GEX regime, max pain

6. **PINN Volatility Surface** (new): A Physics-Informed Neural Network learns the arbitrage-free implied volatility surface from historical per-strike option data. At inference, it computes fair-value total variance (μ) and predictive variance (σ²_pred) for every live option contract. When the market-implied total variance exceeds μ + 1.28√σ²_pred (90th percentile boundary), the contract is statistically overpriced — a mispricing signal that feeds into the paper trader as a third signal source.

All signals and pricing data already exist in Redis. The paper trader is a pure consumer — no new data producers needed (except the vol-surface service which persists existing live data for training).

## Decisions

| Decision | Choice |
|----------|--------|
| Signal sources | Three: OptionSeller setups + cross-layer Confluence + PINN vol-surface mispricing; GAMMA_TRAP = square-off trigger |
| Strategies | Full suite: Iron Condor, Strangle, Bull/Bear Credit Spread, Naked CE/PE, Mispriced Short (credit spread or naked based on z-score) |
| Universe | NIFTY, BANKNIFTY, SENSEX only — real-time MTM from `data:options_live` |
| Architecture | Two new microservices: `services/paper_trading/` + `services/vol_surface/` (collector + inference) + systemd units |
| Capital | ₹10,00,000 virtual, 2-5% margin per trade, max 8 concurrent positions |
| Exit rules | Multi-rule: 200% SL, 50% profit target, theta decay exit, GAMMA_TRAP instant close, 15:15 intraday square-off |
| Intraday vs Positional | Separate rules: intraday = current expiry + 15:15 square-off; positional = hold up to 3 days, roll to next expiry |
| Strike selection | Signal-derived: OI walls for Iron Condor, SR level for credit spreads, 1-2 strikes OTM for naked, PINN z-score for mispriced shorts |
| PINN training data | Live data collection: persist `data:options_live` + `data:tick` + India VIX to Parquet every 30s. Accumulate 2-4 weeks, then train. Retrain weekly. |
| PINN training location | Server CPU-only (4-core, 8GB RAM). ~100K params, 20-40 min per training run. Run during off-market hours. |
| PINN inference | New microservice `services/vol_surface/` — loads frozen weights, runs inference every 3s, publishes to `vol:mispricing` stream |
| PINN-paper trader integration | Unified: PINN mispricing is a third signal source in the same signal router, same position manager, same exit engine, same bot commands |

## Architecture

```
┌─────────────────┐     analysis:results stream     ┌──────────────────────┐
│ analysis-engine  │─────────────────────────────────▶│                      │
└─────────────────┘                                  │  paper-trading-service│
                                                     │                      │
┌─────────────────┐     intelligence:signals stream  │  ┌──────────────┐   │
│ market-data      │─────────────────────────────────▶│  │ Signal Router │   │
└─────────────────┘                                  │  │  (3 sources)  │   │
                                                     │  └──────┬───────┘   │
┌─────────────────┐     vol:mispricing stream        │         │           │
│ vol-surface-svc  │─────────────────────────────────▶│  ┌──────▼───────┐   │
│ (PINN inference)  │                                 │  │  Engine Core  │   │
└─────────────────┘                                  │  │  (positions,  │   │
       │                                            │  │   MTM, exits) │   │
       │ reads data:options_live                    │  └──────┬───────┘   │
       │ reads data:tick                             │         │           │
       ▼                                            │  ┌──────▼───────┐   │
┌─────────────────┐     data:options_live:{sym}      │  │  Redis Store  │   │
│ market-data      │──── 1s snapshot ────────────────▶│  │ (positions,   │   │
│ (SnapshotPublish)│     data:options_agg:{sym}      │  │  trades, P&L)│   │
└─────────────────┘     data:tick:{sym}              │  └──────────────┘   │
                                                     └──────────────────────┘
┌─────────────────┐     notification:dispatch        ▲
│ notification-svc │◀────────────────────────────────│
└─────────────────┘                                  │ reads paper:* Redis
                                                     │
┌─────────────────┐     paper:commands stream        │
│ monolith (bot)   │─────────────────────────────────▶│
└─────────────────┘                                  │
                                                     │
┌──────────────────────────────────────────┐         │
│ vol-surface-svc (services/vol_surface/)   │         │
│                                          │         │
│  Thread 1: Data Collector (30s)          │─────────┘
│    reads data:options_live + data:tick   │  writes Parquet
│    writes data/vol_surface/{sym}/{date}  │  data/vol_surface/
│                                          │
│  Thread 2: PINN Inference (3s)           │
│    loads frozen .pt weights              │
│    computes μ + σ²_pred per strike       │
│    publishes vol:mispricing stream       │
│                                          │
│  Offline: train.py (weekend cron)        │
│    reads Parquet → trains PINN           │
│    exports data/models/vol_surface/*.pt  │
└──────────────────────────────────────────┘
```

### Data Flow

1. **Entry signals** (three sources): Paper trader joins `analysis:results`, `intelligence:signals`, and `vol:mispricing` consumer groups.
   - `analysis:results`: Parse `analysis_json` for `RANGE_BOUND_SETUP`, `SKEW_FADE_SETUP` in `NEUTRAL` bucket.
   - `intelligence:signals`: Run local `SignalCorrelator` on cross-layer signals → confluence events.
   - `vol:mispricing`: PINN inference service publishes overpriced option contracts (z-score > 1.28).

2. **GAMMA_TRAP**: Detected from `analysis_json` `NEUTRAL.GAMMA_TRAP` — triggers immediate square-off of all open positions on that symbol.

3. **Mark-to-market**: Every 3 seconds, reads `data:options_live:{symbol}` and `data:tick:{symbol}` to compute current premium for each open position's legs.

4. **Exits**: Checked on each MTM cycle — stop loss, profit target, theta decay, time-based square-off.

5. **Persistence**: All positions, trades, and daily P&L stored in Redis hashes under `paper:*` namespace.

6. **Bot commands**: New `notification/commands/paper.py` module with `/paper_positions`, `/paper_pnl`, `/paper_trades`, `/paper_close`, `/paper_config`, `/paper_vol` (PINN status). Commands read directly from Redis (no stream needed — same pattern as `/debugstats`).

7. **PINN data collection** (parallel): vol-surface service's collector thread reads `data:options_live` + `data:tick` every 30s during market hours, writes to `data/vol_surface/{symbol}/{date}.parquet` for training.

8. **PINN inference** (parallel): vol-surface service's inference thread loads frozen weights, runs every 3s on live option ticks, publishes mispricing signals to `vol:mispricing` stream.

## Implementation Plan

### Dependencies

Add to `ml_pipeline/requirements.txt` (uncomment torch) or install directly:
```
torch>=2.2.0        # CPU-only — PyTorch for PINN model
pyarrow>=18.0.0     # Parquet I/O (already in ml_pipeline/requirements.txt)
```

The `sentiment/news_sentiment_manager.py` already imports `torch`, so it may already be installed on the server. Verify with `python -c "import torch; print(torch.__version__)"`. If not installed: `pip install torch --index-url https://download.pytorch.org/whl/cpu` (CPU-only build, ~200MB).

### Task 1: Core Data Models (`services/paper_trading/models.py`)

Dataclasses for the paper trading domain:

```python
@dataclass
class OptionLeg:
    strike: float
    option_type: str        # "CE" | "PE"
    side: str               # "SELL" | "BUY"
    lots: int
    entry_premium: float    # per unit
    current_premium: float  # live MTM
    entry_timestamp: float

@dataclass
class PaperPosition:
    position_id: str        # UUID
    symbol: str             # "NIFTY", "BANKNIFTY", "SENSEX"
    strategy: str           # "IRON_CONDOR" | "STRANGLE" | "CREDIT_SPREAD" | "NAKED_CE" | "NAKED_PE" | "MISPRICED_SHORT_CE" | "MISPRICED_SHORT_PE"
    mode: str               # "intraday" | "positional"
    direction: str          # "NEUTRAL" | "BULLISH" | "BEARISH"
    legs: list[OptionLeg]
    expiry: str             # ISO date
    entry_timestamp: float
    entry_credit: float     # net premium received (sum of sells - sum of buys)
    margin_blocked: float
    status: str             # "OPEN" | "CLOSED"
    exit_timestamp: float | None
    exit_premium: float | None
    pnl: float | None
    exit_reason: str | None  # "STOP_LOSS" | "TARGET" | "THETA_DECAY" | "GAMMA_TRAP" | "SQUARE_OFF" | "EXPIRY"
    signal_source: str       # "RANGE_BOUND_SETUP" | "SKEW_FADE_SETUP" | "CONFLUENCE" | "MISPRICED_OPTION"
    signal_score: float | None  # confluence score or PINN z-score
    signal_context: dict     # snapshot of trigger data

@dataclass
class PaperAccount:
    capital: float           # ₹10,00,000
    realized_pnl: float
    unrealized_pnl: float
    margin_used: float
    available_margin: float
    open_positions: int
    day_start_capital: float
    max_drawdown: float
```

Redis keys:
- `paper:account` — hash (capital, realized_pnl, margin_used, day_start_capital, max_drawdown)
- `paper:positions:open` — hash {position_id: json}
- `paper:positions:closed:{date}` — hash {position_id: json} (per-day archive)
- `paper:trades` — stream (append-only, maxlen=5000)
- `paper:daily_pnl:{date}` — hash (realized, unrealized, total, trades_count, wins, losses)
- `paper:config` — hash (max_positions, max_margin_pct, sl_multiplier, target_pct, theta_exit_pct, theta_exit_time)

### Task 2: Signal Router (`services/paper_trading/signal_router.py`)

Consumes three Redis streams. Parses signals into trade signals:

**From `analysis:results`** (join consumer group `paper-trader`):
- Parse `analysis_json` → check `NEUTRAL.RANGE_BOUND_SETUP` → emit `EntrySignal(strategy=IRON_CONDOR/STRANGLE, ...)`
- Parse `analysis_json` → check `NEUTRAL.SKEW_FADE_SETUP` → emit `EntrySignal(strategy=CREDIT_SPREAD, direction=fade_direction, ...)`
- Parse `analysis_json` → check `NEUTRAL.GAMMA_TRAP` → emit `ExitSignal(reason=GAMMA_TRAP, symbol=...)`
- Skip if `trend_found=false` and no setups present (fast path — avoids JSON parse for most results)

**From `intelligence:signals`** (join consumer group `paper-trader-signals`):
- Run a local `SignalCorrelator` instance (reuse `intelligence/correlator.py`)
- On confluence callback → emit `EntrySignal(strategy=NAKED_CE/NAKED_PE, direction=confluence.direction, score=confluence.score, ...)`
- Only HIGH confluences (3 layers) trigger naked selling; MODERATE (2 layers) trigger credit spreads

**From `vol:mispricing`** (join consumer group `paper-trader-vol`):
- Each message contains: symbol, strike, option_type (CE/PE), z_score, w_market, mu, sigma_pred, spot, timestamp
- Sort by z_score descending, take top 2 per symbol per cycle (avoid flooding)
- z_score > 1.28 (90th percentile) → emit `EntrySignal(strategy=MISPRICED_SHORT_CE or MISPRICED_SHORT_PE, strike=signal.strike, z_score=signal.z_score)`
- z_score > 2.0 (extreme mispricing) → flag for naked short (no protection leg)
- Cooldown: 10 min per (symbol, strike) pair — prevents re-entry on same contract

**Entry filters:**
- Cooldown: 15 min between entries on same symbol (prevent over-trading)
- Max 1 open position per symbol per strategy type
- Respect max_positions (8) and available margin

### Task 3: Strike Selection & Strategy Builder (`services/paper_trading/strategy_builder.py`)

Reads live option data from Redis to select strikes and compute premiums:

**Iron Condor / Strangle** (from RANGE_BOUND_SETUP):
- Short Put: `put_wall_strike` (sell PE)
- Short Call: `call_wall_strike` (sell CE)
- Long Put: 2 strike_gaps below put_wall_strike (buy PE — protection)
- Long Call: 2 strike_gaps above call_wall_strike (buy CE — protection)
- If `setup_type == "STRANGLE"`: same but no long legs (naked)
- Premium = (short_CE_ltp + short_PE_ltp) - (long_CE_ltp + long_PE_ltp)

**Credit Spread** (from SKEW_FADE_SETUP):
- Bullish fade (sell put credit spread):
  - Short Put: `sr_level` strike (sell PE)
  - Long Put: 2 strike_gaps below `sr_level` (buy PE)
- Bearish fade (sell call credit spread):
  - Short Call: `sr_level` strike (sell CE)
  - Long Call: 2 strike_gaps above `sr_level` (buy CE)

**Naked Directional** (from Confluence):
- BULLISH confluence → sell PE at 1 strike OTM (ATM - 1 strike_gap)
- BEARISH confluence → sell CE at 1 strike OTM (ATM + 1 strike_gap)
- For HIGH confluence (score > 15): sell 2 strikes OTM for more safety
- No long protection leg

**Mispriced Short** (from PINN vol:mispricing):
- Sell the overpriced strike identified by the PINN (CE or PE as specified)
- If z_score ∈ [1.28, 2.0): credit spread — buy protection 2 strikes further OTM
- If z_score > 2.0 (extreme mispricing): naked short — no protection leg (higher premium capture, higher risk)
- Direction: sell CE = bearish view on that strike (expect IV mean-reversion), sell PE = bullish view
- Position sizing: 3% of capital for credit spread, 2% for naked (smaller due to higher risk)
- This strategy profits from IV mean-reversion (the PINN predicts the "fair" IV, and the market IV is too high)

**Expiry selection:**
- Intraday: current weekly expiry (from `data:options_agg:{symbol}` or `data:zerodha:{symbol}`)
- Positional: current weekly expiry if >= 3 days to expiry, else next weekly expiry

**Lot size**: Read from `common.constants.INDEX_LOT_SIZES` (NIFTY=75, BANKNIFTY=15, SENSEX=10)

**Margin approximation**: SPAN margin ≈ 15% of notional for indices. Notional = strike × lot_size. For credit spreads, margin = max_loss = (short_strike - long_strike) × lot_size. For Iron Condor, margin = max(width_of_put_spread, width_of_call_spread) × lot_size.

**Position sizing**: Allocate 2-5% of capital per trade. For credit spreads, margin = defined risk → use 3% of capital. For naked, use 5% (higher margin but capped by max_positions). Compute lots = floor((capital × risk_pct) / margin_per_lot).

### Task 4: MTM & Exit Engine (`services/paper_trading/engine.py`)

Runs every 3 seconds. For each open position:

1. **Read current premiums**: For each leg, read `data:options_live:{symbol}` → find strike+type → get `ltp`
2. **Compute current position value**: Sum of (sell legs: current_premium × lots × lot_size) - (buy legs: current_premium × lots × lot_size). For a credit strategy, P&L = entry_credit - current_debit.
3. **Check exit rules** (in priority order):

| Priority | Rule | Intraday | Positional |
|----------|------|----------|------------|
| 1 | GAMMA_TRAP | Instant close | Instant close |
| 2 | Stop Loss | Current loss >= 200% of credit | 200% of credit |
| 3 | Profit Target | Current profit >= 50% of credit | 50% of credit |
| 4 | Theta Decay | 75% decay by 14:00 | 75% decay by T-3 days to expiry |
| 5 | Time Square-off | 15:15 IST | T-1 day to expiry at 15:00 |
| 6 | Expiry | N/A (intraday) | Expiry day at 15:20 |

4. **On exit**: Record close premium, compute P&L, update account (realized_pnl, margin release), move position from `paper:positions:open` to `paper:positions:closed:{date}`, append to `paper:trades` stream, send Telegram notification.

5. **Daily reset**: At 09:15 IST, snapshot `day_start_capital`, reset daily counters. At 15:30, compute daily P&L summary.

### Task 5: Service Entry Point (`services/paper_trading/main.py`)

Systemd service following same pattern as `services/analysis_engine/main.py`:

```
Threads:
  1. signal_router thread  — consumes analysis:results + intelligence:signals
  2. mtm_engine thread     — 3-second MTM + exit checks
  3. command_listener thread — consumes paper:commands stream (from bot)
  4. heartbeat thread      — 30s heartbeat to service:registry:paper-trading
```

- Crash handler via `services.common.crash_handler.install_crash_handler`
- Version stamping via `services.common.version`
- Graceful shutdown on SIGTERM/SIGINT
- Config loaded from `paper:config` Redis hash (with defaults)

### Task 6: Bot Commands (`notification/commands/paper.py`)

New command module following the existing pattern (`@guard` decorator, `HANDLERS` list):

| Command | Description |
|---------|-------------|
| `/paper_positions` | List all open positions with live MTM, entry credit, current P&L % |
| `/paper_pnl` | Today's P&L summary: realized, unrealized, total, win rate, max DD |
| `/paper_trades [N]` | Last N closed trades (default 10). Shows strategy, symbol, entry/exit, P&L, exit reason |
| `/paper_close [position_id]` | Manually close a position (or all if "all"). Records at current market premium |
| `/paper_config` | Show current config (capital, max_positions, SL%, target%, etc.) |
| `/paper_config set <key> <value>` | Update config in Redis (debug chat only) |
| `/paper_reset` | Reset account to ₹10L, close all positions (debug chat only) |
| `/paper_vol` | PINN vol-surface status: model date, days of training data, current mispricing signals, z-score distribution |

Commands write to `paper:commands` stream → paper-trading service consumes and executes. Results read directly from Redis for display.

Register in `notification/commands/__init__.py`: add `paper` to the module tuple.

### Task 7: Systemd Unit (`configs/stockanalysis-paper-trading.service`)

```ini
[Unit]
Description=StockAnalysis Paper Trading Service
After=network.target redis-server.service

[Service]
Type=simple
User=hacker
WorkingDirectory=/home/hacker/StockAnalysis
ExecStart=/home/hacker/StockAnalysis/.venv/bin/python -m services.paper_trading.main
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/hacker/StockAnalysis/.env
CPUQuota=15%
MemoryMax=150M

[Install]
WantedBy=multi-user.target
```

### Task 8: Systemd Unit for Vol-Surface Service (`configs/stockanalysis-vol-surface.service`)

```ini
[Unit]
Description=StockAnalysis PINN Volatility Surface Service
After=network.target redis-server.service

[Service]
Type=simple
User=hacker
WorkingDirectory=/home/hacker/StockAnalysis
ExecStart=/home/hacker/StockAnalysis/.venv/bin/python -m services.vol_surface.main
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/hacker/StockAnalysis/.env
CPUQuota=20%
MemoryMax=300M

[Install]
WantedBy=multi-user.target
```

### Task 9: PINN Data Collector (`services/vol_surface/collector.py`)

Persists live option chain data to Parquet for PINN training. Runs as a thread inside the vol-surface service.

**Collection cycle** (every 30 seconds during 09:15–15:30 IST):
1. For each symbol in [NIFTY, BANKNIFTY, SENSEX]:
   - Read `data:options_live:{symbol}` Redis hash → parse per-strike CE/PE ticks
   - Read `data:tick:{symbol}` → spot price (last_price)
   - Read `data:options_agg:{symbol}` → ATM strike, expiry
   - Read `data:tick:INDIA_VIX` → VIX value. If missing, fetch from yfinance (`^INDIAVIX`) as fallback.
2. For each strike × option_type, build a row:
   - `timestamp`, `symbol`, `strike`, `option_type` (CE/PE), `ltp`, `oi`, `volume`, `iv`, `gamma`, `delta`, `theta`, `vega`
   - `spot` (from equity tick), `atm_strike`, `expiry_date` (parsed from option chain metadata)
   - `dte` (days to expiry), `vix` (India VIX value)
3. Compute derived fields:
   - `k` = ln(strike / spot) — log-moneyness
   - `tau` = dte / 365 — time to maturity
   - Filter: only rows with τ ∈ [0.005, 0.25] (remove expiration-day noise + long-dated)
   - `w_actual` = (iv / 100)² × τ — target total variance (IV from Sensibull is in percentage form, e.g. 13.5 = 13.5%)
4. Maintain 30-day rolling VIX statistics in Redis: `vol:vix_stats` hash with `mean_30d`, `std_30d`, `last_30_values_json`
5. Compute `vix_zscore` = (vix - mean_30d) / std_30d
6. Compute `log_vol` = ln(volume + 1), `norm_oi` = oi / max(oi across chain)
7. Append rows to `data/vol_surface/{symbol}/{YYYY-MM-DD}.parquet` (create if not exists, append if exists)

**Parquet schema** (per row):
| Column | Type | Source |
|--------|------|--------|
| timestamp | float64 | unix epoch |
| symbol | string | NIFTY/BANKNIFTY/SENSEX |
| strike | float64 | from options_live key |
| option_type | string | CE/PE |
| ltp | float64 | tick.ltp |
| oi | float64 | tick.oi |
| volume | float64 | tick.volume |
| iv | float64 | tick.iv (percentage form) |
| gamma | float64 | tick.gamma |
| delta | float64 | tick.delta |
| spot | float64 | data:tick last_price |
| atm_strike | float64 | data:options_agg atm_strike |
| expiry_date | string | ISO date |
| dte | int | days to expiry |
| vix | float64 | India VIX |
| vix_zscore | float64 | 30-day rolling z-score |
| log_vol | float64 | ln(volume + 1) |
| norm_oi | float64 | oi / max_chain_oi |
| k | float64 | ln(strike / spot) |
| tau | float64 | dte / 365 |
| w_actual | float64 | (iv/100)² × tau |

**File rotation**: At market close (15:30), compress the day's Parquet to `.parquet.snappy` (already compressed by Parquet format, but gzip the final file). Delete files older than 90 days.

### Task 10: PINN Model Architecture (`services/vol_surface/model.py`)

Two sub-networks with multiplicative gating:

```python
class SpatialPINN(nn.Module):
    """Core arbitrage-free volatility surface — maps (k, τ) → w_base."""
    # 5 layers, 128 neurons, Softplus activations
    # Input: [k, τ] (2 features)
    # Output: w_base (scalar — baseline total variance)
    # Softplus ensures infinite differentiability for autodiff penalties

class ContextScaler(nn.Module):
    """Macro-regime scaler — adjusts w_base based on market context."""
    # 4 layers, 64 neurons, SiLU activations
    # Input: [vix_zscore, log_vol, norm_oi, w_base] (4 features)
    # Head A (Softplus): multiplier M ∈ (0, ∞)
    # Head B (Linear): log_predictive_variance ln(σ²_pred)

class VolSurfacePINN(nn.Module):
    """Combined model — multiplicative gating."""
    def forward(self, k, tau, vix_z, log_vol, norm_oi):
        spatial_input = torch.stack([k, tau], dim=1).requires_grad_(True)
        w_base = self.spatial(spatial_input)  # [N, 1]
        
        context_input = torch.stack([vix_z, log_vol, norm_oi, w_base.squeeze()], dim=1)
        M, ln_var = self.scaler(context_input)  # [N, 1], [N, 1]
        
        mu = w_base * M  # final fair-value total variance
        sigma_pred = torch.exp(ln_var)  # predictive variance
        return mu, sigma_pred, w_base, spatial_input
```

**Why this architecture:**
- Multiplicative gating prevents macro variables (VIX, volume, OI) from corrupting the spatial physics. The PINN learns the arbitrage-free surface shape first, then the scaler learns how macro regimes shift the entire surface up/down.
- Softplus in the spatial path ensures all derivatives exist (needed for Calendar + Durrleman penalties via `torch.autograd.grad`).
- Predictive variance (σ²_pred) acts as a noise absorber — the model learns which regions of the surface are uncertain, and the z-score boundary widens in those regions (avoids false signals in illiquid strikes).

### Task 11: Physics-Informed Loss Engine (`services/vol_surface/loss.py`)

Three penalty terms, combined end-to-end:

```python
def vol_surface_loss(model, data_batch, collocation_points):
    """
    Composite loss = Data Loss + λ_cal × Calendar Penalty + λ_dur × Durrleman Penalty
    """
    # ── 1. Data Loss (Gaussian NLL) ──────────────────────────────
    mu, sigma_pred, _, _ = model(k, tau, vix_z, log_vol, norm_oi)
    nll = 0.5 * torch.log(sigma_pred) + (mu - w_actual)² / (2 * sigma_pred)
    data_loss = nll.mean()
    
    # ── 2. Calendar Arbitrage Penalty ────────────────────────────
    # Total variance must be non-decreasing in τ: ∂μ/∂τ ≥ 0
    k_col, tau_col = collocation_points  # 2048 synthetic [k, τ]
    mu_col = model(k_col, tau_col, ...)  # forward on collocation grid
    dmu_dtau = torch.autograd.grad(
        mu_col.sum(), tau_col, create_graph=True
    )[0]
    calendar_penalty = torch.relu(-dmu_dtau).pow(2).mean()
    
    # ── 3. Durrleman Condition (Butterfly Arbitrage) ────────────
    # Risk-neutral density g(k) must be ≥ 0
    # g(k) = d²C/dK² where C is the call price function
    # In total variance parameterization: compute second derivative
    # of the call price w.r.t. strike k
    dmu_dk = torch.autograd.grad(mu_col.sum(), k_col, create_graph=True)[0]
    d2mu_dk2 = torch.autograd.grad(dmu_dk.sum(), k_col, create_graph=True)[0]
    # Durrleman function g(k) involves the Breeden-Litzenberger relation
    # applied to the total variance parameterization
    g_k = compute_durrleman_function(k_col, tau_col, mu_col, dmu_dk, d2mu_dk2, spot)
    durrleman_penalty = torch.relu(-g_k).pow(2).mean()
    
    return data_loss + lambda_cal * calendar_penalty + lambda_dur * durrleman_penalty
```

**Collocation grid** (regenerated each training step):
- Sample 2048 synthetic [k, τ] points uniformly
- k bounded by [k_min − 10%, k_max + 10%] where k_min/k_max are the day's actual trading range
- τ bounded by [0.005, 0.25]
- VIX/volume/OI for collocation points: use the day's mean values (the penalties are on the spatial structure, not the macro context)

**Penalty weights**: λ_cal = 1.0, λ_dur = 1.0 (start equal, can be tuned)

### Task 12: Training Pipeline (`services/vol_surface/train.py`)

Offline script, run manually or via cron during off-market hours (weekends).

**Usage**: `python -m services.vol_surface.train --symbol NIFTY --days 30 --output data/models/vol_surface/`

**Pipeline:**
1. Load Parquet files for the specified symbol and date range
2. Preprocess: filter τ ∈ [0.005, 0.25], remove rows with iv=0 or ltp=0
3. Normalize: k and τ are already normalized. VIX z-score, log_vol, norm_oi already computed during collection.
4. Split: 80% train, 20% validation (chronological split — no random shuffling for time series)
5. Stage 1 — Adam optimizer:
   - lr = 0.01, weight_decay = 1e-5
   - Batch size: 1024 data points + 2048 collocation points per step
   - Epochs: 3000 (or early stop if data_loss < 1e-6 for 100 consecutive epochs)
   - Log: data_loss, calendar_penalty, durrleman_penalty every 100 epochs
6. Stage 2 — L-BFGS optimizer:
   - lr = 1.0, max_iter = 500, tolerance_grad = 1e-7
   - Full-batch (all data points + 2048 collocation points)
   - Goal: drive calendar + Durrleman penalties to ≈ 0
   - Log: all three losses every 50 iterations
7. Export: save state_dict + normalization parameters (vix mean/std, k range) to `data/models/vol_surface/{symbol}_{YYYYMMDD}.pt`
8. Validation: compute RMSE on held-out validation set, log surface arbitrage violations (count of negative ∂μ/∂τ and negative g(k))
9. Keep last 3 model versions; older ones auto-deleted

**Training time estimate**: ~100K parameters, 4-core CPU, batch 1024+2048 = ~3K points/step. Adam 3000 epochs ≈ 15-25 min. L-BFGS 500 iters ≈ 5-10 min. Total ~20-35 min per symbol.

### Task 13: Vol-Surface Service Entry Point (`services/vol_surface/main.py`)

Systemd service with two threads + offline training script:

```
Threads:
  1. collector thread   — 30s Parquet persistence during market hours
  2. inference thread   — 3s PINN inference during market hours
  3. heartbeat thread   — 30s heartbeat to service:registry:vol-surface
```

**Inference cycle** (every 3 seconds during 09:15–15:30 IST):
1. Load model at startup: read latest `data/models/vol_surface/{symbol}_*.pt` for each symbol
2. For each symbol (NIFTY, BANKNIFTY, SENSEX):
   - Read `data:options_live:{symbol}` → all strikes with CE/PE ticks
   - Read `data:tick:{symbol}` → spot price
   - Read VIX from `data:tick:INDIA_VIX` (or fallback)
   - Read `vol:vix_stats` from Redis → compute vix_zscore
3. For each strike × option_type:
   - Skip if `iv` = 0 or `ltp` = 0 (illiquid)
   - Compute k = ln(strike / spot), τ = dte/365
   - Skip if τ outside [0.005, 0.25]
   - Build tensor input: [k, τ, vix_z, log_vol, norm_oi]
   - Run model → μ (fair total variance), σ²_pred (predictive variance)
   - Compute w_market = (iv / 100)² × τ
   - Compute z_score = (w_market - μ) / √σ²_pred
4. Filter: z_score > 1.28 (statistically overpriced at 90th percentile)
5. Sort by z_score descending, take top 3 per symbol
6. Publish each to `vol:mispricing` stream:
   ```
   {symbol, strike, option_type, z_score, w_market, mu, sigma_pred, spot, iv, ltp, timestamp}
   ```
   maxlen=500
7. Log: number of mispricing signals per symbol per cycle

**Model reload**: Support hot-reload — check model file mtime every 5 min. If newer file exists, swap model atomically. Allows retraining without restarting the service.

**Crash handler**: `services.common.crash_handler.install_crash_handler("vol-surface")`
**Version**: `services.common.version` stamping in heartbeat

### Task 14: Tests (`tests/services/test_paper_trading.py` + `tests/services/test_vol_surface.py`)

**Paper Trading tests** (~37 tests):

**Models** (5):
- `PaperPosition` serialization/deserialization to/from JSON
- `OptionLeg` premium computation
- `PaperAccount` margin recalculation
- Position close updates account correctly
- Multi-leg position P&L calculation

**Signal Router** (9):
- Parse RANGE_BOUND_SETUP from analysis_json → EntrySignal
- Parse SKEW_FADE_SETUP → EntrySignal with correct direction
- Parse GAMMA_TRAP → ExitSignal
- Skip results with no setups (fast path)
- Cooldown enforcement (15 min per symbol)
- Max positions filter
- Confluence → naked directional signal
- HIGH vs MODERATE confluence → strategy selection
- vol:mispricing → MISPRICED_SHORT signal with z-score threshold filtering

**Strategy Builder** (12):
- Iron Condor strike selection from wall strikes
- Strangle (no long legs) when setup_type=STRANGLE
- Bull put credit spread from SKEW_FADE (bullish)
- Bear call credit spread from SKEW_FADE (bearish)
- Naked PE for bullish confluence (1 strike OTM)
- Naked CE for bearish confluence (1 strike OTM)
- Mispriced short credit spread (z_score 1.28-2.0 → buy protection)
- Mispriced short naked (z_score > 2.0 → no protection)
- Expiry selection: current vs next based on days to expiry
- Margin calculation for each strategy type
- Position sizing respects max margin %
- Strike gap resolution from token_registry

**MTM & Exit Engine** (8):
- MTM reads option LTP from mock Redis
- Stop loss exit at 200% of credit
- Profit target exit at 50% of credit
- Theta decay exit (intraday: 75% by 14:00)
- Time-based square-off (intraday: 15:15)
- GAMMA_TRAP instant close
- Position close updates realized P&L + releases margin
- Multiple concurrent positions MTM

**Bot Commands** (4):
- `/paper_positions` renders open positions
- `/paper_pnl` renders daily summary
- `/paper_close` closes position and records trade
- `/paper_config` shows and updates config

---

**Vol-Surface tests** (~25 tests in `test_vol_surface.py`):

**Data Collector** (6):
- Parse options_live Redis hash into Parquet rows
- VIX fallback to yfinance when data:tick:INDIA_VIX missing
- τ filtering: exclude τ < 0.005 and τ > 0.25
- w_actual computation: (iv/100)² × τ
- VIX 30-day rolling z-score computation
- Parquet file append mode (existing file + new rows)

**PINN Model** (5):
- SpatialPINN forward pass output shape [N, 1]
- ContextScaler dual output (M + ln_var) shapes
- Multiplicative fusion: μ = w_base × M
- Softplus output is always positive (w_base > 0, M > 0)
- Predictive variance is always positive (exp(ln_var) > 0)

**Loss Engine** (5):
- Data loss (Gaussian NLL) computation
- Calendar arbitrage penalty: ∂μ/∂τ computed via autograd
- Durrleman penalty: g(k) computed via second-order autograd
- Collocation grid generation within bounds
- Loss is differentiable (backward pass doesn't error)

**Inference** (5):
- Model loading from .pt file
- z-score computation: (w_market - μ) / √σ²_pred
- Filter z_score > 1.28 → mispricing signal
- Top-3 per symbol selection
- vol:mispricing stream publish format

**Training** (4):
- Parquet loading and preprocessing pipeline
- Chronological train/val split (80/20)
- Adam stage runs for N epochs without error
- Model export to .pt file (state_dict + normalization params)

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Option LTP gaps (WS disconnect) | Skip MTM if `data:options_live` age > 10s (check `options_agg.last_updated`). Log warning. Don't trigger exits on stale prices. |
| Strike not in options_live (illiquid) | Fallback to `data:zerodha:{symbol}` REST snapshot. If also missing, skip entry. |
| Margin approximation inaccuracy | Document that SPAN is approximated. Real margin would need a SPAN calculator. 15% notional is conservative for index options. |
| Duplicate entries from re-emitted signals | Position dedup by (symbol, strategy) key. Cooldown Redis key with TTL. |
| Paper trader crash mid-position | All positions in Redis. On restart, re-loads open positions and resumes MTM. No in-memory state loss. |
| Signal stream lag | Consumer group with XREADGROUP BLOCK=5000. If lag > 60s on analysis:results, log warning (doesn't affect entries, only signal timeliness). |
| **PINN: Insufficient training data** | First 2-4 weeks: collector accumulates data, inference service runs but publishes no signals (model not trained). Bot command `/paper_vol` shows "collecting data: N days accumulated". Train first model after 10 trading days minimum. |
| **PINN: CPU training too slow** | Network is small (~100K params). If > 40 min per symbol, reduce Adam epochs to 2000 or reduce collocation points to 1024. L-BFGS stage is the critical one for arbitrage-free guarantees. |
| **PINN: Durrleman condition instability** | If g(k) computation is numerically unstable (second-order autograd), add gradient clipping (max_norm=1.0) and reduce λ_dur to 0.1 initially. Increase to 1.0 after Adam stage converges. |
| **PINN: Model overfitting** | Chronological 80/20 split. Monitor validation RMSE. If train RMSE << val RMSE, add dropout (0.1) to ContextScaler. SpatialPINN stays dropout-free (needs smooth derivatives). |
| **PINN: VIX data gap** | `data:tick:INDIA_VIX` may not be published if INDIA_VIX not subscribed via Zerodha WS. Fallback: fetch from yfinance `^INDIAVIX` every 30s in collector. Cache in Redis `vol:vix:latest`. |
| **PINN: Stale model** | Hot-reload: inference thread checks model file mtime every 5 min. Retrain weekly. Bot command `/paper_vol` shows model date + age in days. |
| **PINN: Parquet disk space** | Each symbol generates ~3K rows/day × ~40 strikes × 2 types = ~240K rows. At ~100 bytes/row ≈ 24MB/day/symbol. 3 symbols × 90 days = ~6.5GB. Acceptable on server. Auto-delete files > 90 days. |

## Validation Plan

### Paper Trading
1. **Unit tests**: All 37 tests in `test_paper_trading.py` pass
2. **Dry run**: Deploy on server. During market hours, observe `/paper_positions` — should see entries within 15 min of market open if signals fire
3. **P&L sanity**: After 1 trading day, `/paper_pnl` should show realistic numbers. Cross-check: each closed trade's P&L = entry_credit - exit_debit × lot_size
4. **Exit validation**: Verify that at 15:15 IST, all intraday positions auto-close. Verify GAMMA_TRAP closes positions within 3 seconds of signal.
5. **Bot command test**: Send `/paper_positions`, `/paper_pnl`, `/paper_trades`, `/paper_close`, `/paper_config`, `/paper_vol` from allowed chat — verify output format and data accuracy
6. **Resource check**: `systemctl status stockanalysis-paper-trading` — confirm < 150M RAM, 0 restarts, no errors in journalctl

### PINN Vol-Surface
7. **Collector validation**: After 1 trading day, verify `data/vol_surface/NIFTY/{date}.parquet` exists with > 1000 rows. Check schema: all columns present, no NaN in k/tau/w_actual.
8. **Training validation**: After 10 trading days, run `train.py --symbol NIFTY --days 10`. Check: data_loss converges, calendar_penalty → 0, durrleman_penalty → 0. Validation RMSE < 0.01 (in total variance units).
9. **Inference validation**: After model is trained and loaded, verify `vol:mispricing` stream receives messages during market hours. Check z-score distribution: should be roughly normal with most contracts |z| < 1, ~10% with z > 1.28.
10. **Arbitrage check**: After L-BFGS stage, verify on a grid of [k, τ] points that ∂μ/∂τ ≥ 0 everywhere (no calendar arbitrage) and g(k) ≥ 0 everywhere (no butterfly arbitrage). Log any violations.
11. **Signal quality**: After 1 week of PINN signals, compare P&L of MISPRICED_SHORT trades vs analyser-based trades via `/paper_trades`. MISPRICED_SHORT should show positive avg P&L (IV mean-reversion edge).
12. **Resource check**: `systemctl status stockanalysis-vol-surface` — confirm < 300M RAM, 0 restarts. Training script memory < 2GB (PyTorch + Parquet data).

## Open Questions (out of scope for this plan)

- **Real broker integration**: This plan covers paper trading only. Live execution would need Zerodha order API integration, real SPAN margin, and risk overrides — separate plan.
- **Stock options**: Currently limited to 3 indices (NIFTY/BANKNIFTY/SENSEX) where live option LTP is available. Expanding to stock F&O requires consuming `data:zerodha:{symbol}` periodic snapshots — adds pricing lag.
- **Portfolio-level hedging**: No delta-neutral or portfolio-level risk management in v1. Each position is independent. Future: portfolio delta tracking, correlation-based position limits.
- **Historical backtesting with option premiums**: The existing `backtest/` framework tests directional underlying trades. The PINN's Parquet archive enables future backtesting of vol-surface strategies on historical data.
- **Newton-Raphson IV inversion**: v1 uses IV from Sensibull/Zerodha tick data directly (already computed by the data provider). Future: implement BS inversion with Newton-Raphson for independent IV verification. Requires risk-free rate (RBI 91-day T-bill) and dividend yield assumptions.
- **Per-symbol vs unified model**: v1 trains a separate PINN per symbol (NIFTY, BANKNIFTY, SENSEX). Future: a unified model with symbol embedding could transfer learn across indices, useful for low-data regimes.
- **Online learning**: v1 uses offline batch training (weekly retrain). Future: online fine-tuning during market hours to adapt to intraday regime shifts. Risk: could introduce arbitrage violations if not constrained.
- **Durrleman function implementation**: The exact form of g(k) in the total variance parameterization requires careful derivation from the Breeden-Litzenberger relation applied to the BS call price formula. This is the most mathematically involved component — reference: Durrleman & El Karoui (2003), "Robbin-Lipshitz smoothing of the implied volatility surface."
