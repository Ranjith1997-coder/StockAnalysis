import os
from dotenv import load_dotenv

load_dotenv()

#ENVS
ENV_PRODUCTION = "PRODUCTION"
ENV_SHUTDOWN = "SHUTDOWN"
ENV_ENABLE_ZERODHA_DERIVATIVES = "ENABLE_ZERODHA_DERIVATIVES"
ENV_ENABLE_ZERODHA_API = "ENABLE_ZERODHA_API"
ENV_ENABLE_TELEGRAM_BOT = "ENABLE_TELEGRAM_BOT"
ENV_ENABLE_POST_MARKET = "ENABLE_POST_MARKET"
ENV_ENABLE_LIVE_OPTIONS = "ENABLE_LIVE_OPTIONS"   # Toggle real-time options analysis + alerts
ENV_LIVE_OPTIONS_ONLY   = "LIVE_OPTIONS_ONLY"     # Skip all regular analysis; run live options engine only
ENV_ENABLE_INTELLIGENCE = "ENABLE_INTELLIGENCE"   # Toggle SignalBus + Correlator + morning bias
ENV_ENABLE_NARRATOR     = "ENABLE_NARRATOR"       # Toggle LLM-powered trade narratives (requires GEMINI_API_KEY)
ENV_OPTIONS_SOURCE      = "OPTIONS_SOURCE"         # "zerodha" (default) or "sensibull"

# Indices for which live option chains are subscribed via WebSocket (both Zerodha and Sensibull).
# SENSEX uses BFO segment on Zerodha (BSE derivatives); NIFTY/BANKNIFTY use NFO (NSE derivatives).
LIVE_OPTIONS_INDICES = ["NIFTY", "BANKNIFTY", "SENSEX"]

# Lot sizes per index for GEX computation (gamma × OI × lot_size × spot²/100).
# Used by GEXAnalyser. Fallback = 1 (relative GEX, not absolute) for unknown symbols.
INDEX_LOT_SIZES = {
    "NIFTY":     75,
    "BANKNIFTY": 15,
    "SENSEX":    10,
    "FINNIFTY":  40,
}

# Indices excluded from all analysis (fetch + orchestrator).
# INDIA_VIX — volatility index, has no options chain; Sensibull API returns 500 for it.
# FINNIFTY  — Sensibull insights API returns 500; low retail relevance.
INDEX_ANALYSIS_EXCLUDE = {"INDIA_VIX", "FINNIFTY"}


#DEV ENVIRONMENTS
ENV_DEV_INTRADAY = "DEV_INTRADAY"
ENV_DEV_POSITIONAL = "DEV_POSITIONAL"
ENV_DEV_NOTIFY = "DEV_NOTIFY"    # Set to "1" to send Telegram alerts in dev mode
ENV_NO_OF_STOCKS = "NO_OF_STOCKS"
ENV_NO_OF_INDEX  = "NO_OF_INDEX"
ENV_DEV_MAX_CYCLES = "DEV_MAX_CYCLES"        # Max intraday loop cycles in dev mode (0 = unlimited)
ENV_DEV_LOOP_WAIT  = "DEV_LOOP_WAIT_TIME"    # Seconds to sleep between dev cycles (-1 = use production wait time)
ENV_THREAD_POOL_WORKERS = "THREAD_POOL_WORKERS"  # Number of worker threads in the analysis pool (default: 20)


#DEV_CONSTANTS
# Set NO_OF_STOCKS / NO_OF_INDEX env vars to limit how many stocks/indices are loaded
# in dev mode (e.g. NO_OF_STOCKS=5). -1 means no limit (all loaded).
NO_OF_STOCKS = int(os.environ.get(ENV_NO_OF_STOCKS, -1))
NO_OF_INDEX  = int(os.environ.get(ENV_NO_OF_INDEX,  -1))
# Worker threads for the per-stock analysis ThreadPoolExecutor.
# Optimal for I/O-bound workload (90% network wait): 20 on i5-6200U (4 hw-threads).
# Raise to 32 for larger FnO universes; lower to 12 if Sensibull 429s are observed.
THREAD_POOL_WORKERS = int(os.environ.get(ENV_THREAD_POOL_WORKERS, "20"))


#INTRADAY CONSTANTS
INTRADAY_SLEEP_TIME = 310


#NOTIFICATION CONSTANTS

TELEGRAM_INTRADAY_TOKEN    = os.environ.get("TELEGRAM_INTRADAY_TOKEN", "")
TELEGRAM_INTRADAY_CHAT_ID  = os.environ.get("TELEGRAM_INTRADAY_CHAT_ID", "")

TELEGRAM_POSITIONAL_TOKEN   = os.environ.get("TELEGRAM_POSITIONAL_TOKEN", "")
TELEGRAM_POSITIONAL_CHAT_ID = os.environ.get("TELEGRAM_POSITIONAL_CHAT_ID", "")

# Dedicated channel for real-time options alerts (LiveOIAnalyser + LiveStraddleAnalyser)
TELEGRAM_LIVE_OPTIONS_TOKEN    = os.environ.get("TELEGRAM_LIVE_OPTIONS_TOKEN", "")
TELEGRAM_LIVE_OPTIONS_CHAT_ID  = os.environ.get("TELEGRAM_LIVE_OPTIONS_CHAT_ID", "")

TELEGRAM_URL = 'https://api.telegram.org/bot'

#FILE NAMES
DERIVATIVE_LIST_FILENAME = "final_derivatives_list.json"
STOCK_DATA_FILENAME = "stock_data.json"


#ANALYSIS CONSTANTS
# Legacy - kept for backward compatibility
REQUIRED_TRENDS = 3

# Scoring-based notification system
# Each analysis type has a weight that contributes to the total score
ANALYSIS_WEIGHTS = {
    # Technical Indicators
    "RSI": 15,
    "RSI_CROSSOVER": 12,          # RSI crossing key levels
    "MACD": 15,
    "EMA_CROSSOVER": 12,
    "BOLLINGERBAND": 10,          # Price at Bollinger Bands
    "VWAP_DEVIATION": 12,         # VWAP deviation signal
    "ATR": 8,                     # Volatility measure
    "VOLUME": 10,
    "VOLUME_BREAKOUT": 12,        # Volume breakout with price confirmation
    "OBV_DIVERGENCE": 16,         # OBV divergence - smart money signal
    "VOLUME_CLIMAX": 15,          # Volume climax - exhaustion reversal
    "BUY_SELL": 10,               # Buy/Sell quantity imbalance
    "SUPERTREND": 15,             # Supertrend reversal signal
    "RSI_DIVERGENCE": 18,         # RSI divergence - high conviction reversal
    "STOCHASTIC": 12,             # Stochastic oscillator crossover
    "OBV": 12,                    # On-Balance Volume divergence
    "PIVOT_POINTS": 10,           # Pivot point breakout/breakdown
    
    # Candlestick Patterns
    # === OPTIMIZED WEIGHTS BASED ON BACKTEST RESULTS ===
    # Single MOMENTUM: Bullish/Bearish Marubozu (NOT RELIABLE - test PF 0.82)
    "Single_candle_stick_pattern": 6,  # Reduced from 12 - negative expectancy
    
    # Single REVERSAL: Hammer, Shooting Star (MARGINAL - test PF 1.03)
    "Single_candle_reversal_pattern": 8,  # Reduced from 10 - marginal reliability
    
    # Double REVERSAL: Engulfing, Piercing, Dark Cloud (MOST RELIABLE - test PF 1.06)
    "Double_candle_stick_pattern": 18,  # Increased from 13 - most reliable candlestick
    
    # Double CONTINUATION: 2 Continuous Increase/Decrease (NOT RELIABLE - test PF 0.96)
    "Double_candle_continuation_pattern": 4,  # Reduced from 8 - negative expectancy
    
    # Triple REVERSAL: Morning/Evening Star (RELIABLE - test PF 1.09)
    "Triple_candle_stick_pattern": 16,  # Increased from 15 - reliable reversal
    "Triple_candle_reversal_pattern": 16,  # New split method - reliable
    
    # Triple CONTINUATION: 3 Continuous Increase/Decrease (NOT RELIABLE - test PF 0.71)
    "Triple_candle_continuation_pattern": 3,  # Very low - worst performer
    
    # Options & Derivatives Analysis
    "MAX_PAIN": 15,
    "MAX_PAIN_TREND": 12,
    "MAX_PAIN_ALIGNMENT": 18,     # High weight when multiple signals align
    "PCR_EXTREME": 14,
    "PCR_BIAS": 10,
    "PCR_TREND": 12,
    "PCR_INTRADAY_TREND": 13,    # Intraday PCR momentum across session snapshots
    "PCR_REVERSAL": 16,          # PCR zone crossover or trend reversal (intraday)
    "PCR_POS_REVERSAL": 17,      # Multi-day PCR reversal using 3-day averages (positional)
    "PCR_DIVERGENCE": 14,
    "IV_SPIKE": 12,
    "IV_TREND": 10,
    "IV_RANK": 15,           # IV rank (percentile) — high/low signals option selling/buying edge
    "IV_RANK_EXTREME": 18,   # IV percentile at extremes (< 10 or > 85) — very high conviction
    
    # Futures Analysis
    "FUTURES_PREMIUM": 12,
    "OI_BUILDUP": 14,             # OI buildup from per-strike OI chain data
    "FUTURE_ACTION": 14,          # Futures OI + Price action (base weight)
    "FUTURE_ACTION_LONG_BUILDUP": 16,     # Strong bullish signal - new positions
    "FUTURE_ACTION_SHORT_BUILDUP": 16,    # Strong bearish signal - new positions
    "FUTURE_ACTION_SHORT_COVERING": 14,   # Bullish - shorts closing
    "FUTURE_ACTION_LONG_UNWINDING": 14,   # Bearish - longs closing
    "FUTURE_BREAKOUT_PATTERN": 15,        # ORB breakout with OI confirmation (base)
    "FUTURE_BREAKOUT_CONFIRMED": 18,      # ORB breakout with all confirmations
    "FUTURE_BREAKOUT_MTF_ALIGNED": 20,    # ORB breakout with multi-timeframe alignment
    "FUTURE_PVO_PATTERN": 10,             # Price/Volume/OI patterns (base)
    "FUTURE_PVO_BUILDUP": 12,             # PVO pattern with OI buildup
    "FUTURE_SIGNAL_SCORE_HIGH": 20,       # High confidence futures signal (score >= 70)
    "FUTURE_SIGNAL_SCORE_MEDIUM": 15,     # Medium confidence futures signal (score >= 50)
    "FUTURE_SIGNAL_SCORE_LOW": 10,        # Low confidence futures signal (score >= 30)
    # New futures signals
    "FUTURE_OI_TREND": 16,                # Positional: multi-day OI buildup/unwinding trend
    "FUTURE_COST_OF_CARRY": 14,           # Positional: basis backwardation / high CoC
    "FUTURE_ROLLOVER": 8,                 # Positional: rollover pressure (NEUTRAL — context only)
    "FUTURE_OI_FROM_OPEN": 15,            # Intraday: sustained OI buildup from session open
    
    # OI Chain Analysis (per-strike data from Sensibull OI endpoint)
    "OI_SUPPORT_RESISTANCE": 14,  # OI-based support/resistance levels
    "OI_WALL": 13,                # OI wall detection (concentrated OI barriers)
    "OI_SHIFT": 13,               # OI position migration / shift analysis
    "OI_INTRADAY_TREND": 15,      # Intraday OI + PCR trend across periodic snapshots
    "OI_SR_SHIFT": 14,            # Intraday support/resistance level migration
    "OI_CAPITULATION": 16,        # Institutional OI unwinding near money (panic/covering)
    "OI_WALL_MIGRATION": 15,      # Overnight wall migration — institutions rolling defences
    "OI_POSITIONAL_TREND": 16,    # Multi-day call/put OI build-up with futures confirmation
    "OI_ACCELERATION": 17,        # Sudden 2x+ jump in daily OI writing velocity

    # IV vs Historical Volatility
    "IV_PREMIUM": 18,             # IV overpriced vs HV (EXPENSIVE/EXTREME zone) -- seller's edge signal

    # Composite panic detection
    "PANIC_MODE": 22,             # Active panic -- price + IV + OI + futures + volume + PCR aligned
    "PANIC_EXHAUSTION": 25,       # Panic exhaustion -- IV extreme + contrarian PCR + volume climax + OI wall

    # Option-seller composite setups (OptionSellerCompositeAnalyser)
    # Weight = 0 intentionally: these setups bypass the score gate via PRIORITY_OVERRIDE
    # so they never need to accumulate score themselves. A non-zero weight here would
    # artificially inflate scores for stocks where these fire alongside regular signals.
    "RANGE_BOUND_SETUP": 0,  # Iron Condor / Strangle — range-trapped + overpriced vol
    "SKEW_FADE_SETUP":   0,  # Directional credit spread — panic exhaustion at OI wall
    "GAMMA_TRAP":        0,  # Kill-switch — close short positions, directional breach

    # GEX (Gamma Exposure) signals — GEXAnalyser
    "GEX_REGIME":         0,   # Positive/negative gamma regime — informational (excluded from score)
    "GEX_FLIP_PROXIMITY": 16,  # Spot near GEX zero-crossing — regime change imminent
    "GEX_WALL":           15,  # Gamma concentration wall — sticky price level
    "GEX_WALL_BREACH":    18,  # Wall broken + dealer GEX dropped — confirmed directional
    "GEX_IMBALANCE":      13,  # CE/PE gamma dominance imbalance (>2.5x ratio)

    # Price Levels
    "52-week-high": 8,
    "52-week-low": 8,
    
    # Default weight for unlisted analysis types
    "DEFAULT": 10
}

# Notification priority thresholds
# Recalibrated for expanded analyser pool:
#   ~18 technical indicators (RSI, MACD, Supertrend, RSI Divergence, Stochastic, OBV,
#    Pivot Points, EMA, Bollinger, VWAP, ATR, Volume, BuySell + 3 candlestick tiers)
#   ~10 options/OI chain signals, ~5 futures signals
#
# Typical score ranges with alignment bonuses:
#   - 2-3 random mixed signals: ~25-35 × 1.0 = 25-35  (noise, ignore)
#   - 3-4 aligned technical only: ~40-55 × 1.3 = 52-71 (informational)
#   - 4-5 strong aligned technical: ~55-70 × 1.3 = 71-91 (notable trend)
#   - 5-6 cross-category (tech + options): ~70-85 × 1.5 = 105-127 (actionable)
#   - 7+ across 3+ categories: ~90-120 × 1.5 = 135-180 (strong conviction)
NOTIFICATION_PRIORITY = {
    "LOW": 35,       # Score >= 35: 3-4 aligned signals, worth monitoring
    "MEDIUM": 60,    # Score >= 60: 4-5 aligned signals, notable trend forming
    "HIGH": 90,      # Score >= 90: Strong cross-category confirmation, actionable
    "CRITICAL": 130  # Score >= 130: Overwhelming conviction across 3+ categories
}

# Minimum score required to send any notification, split by mode.
# Intraday: 110 — 5-min cycles need a lower bar since fewer analysers fire per tick.
# Positional: 150 — runs on 50+ stocks once/day; near-expiry week inflates scores
#   mechanically (BASIS_EXPANDING, LONG_UNWINDING each fire on 40-50% of stocks).
#   A score of 150 requires genuine cross-category alignment (futures + options + technical).
MIN_NOTIFICATION_SCORE = 110           # intraday default (also used as fallback)
MIN_NOTIFICATION_SCORE_POSITIONAL = 150

# Bonus multipliers for signal alignment
SIGNAL_ALIGNMENT_BONUS = {
    "ALL_BULLISH": 1.3,    # All signals are bullish - 30% bonus
    "ALL_BEARISH": 1.3,    # All signals are bearish - 30% bonus  
    "MIXED": 1.0,          # Mixed signals - no bonus
    "CONFIRMATION": 1.5    # Technical + Options aligned - 50% bonus
}

# Analysis categories for alignment detection
TECHNICAL_ANALYSES = {"RSI", "MACD", "EMA_CROSSOVER",
                      "Single_candle_stick_pattern", "Single_candle_reversal_pattern",
                      "Double_candle_stick_pattern", "Double_candle_continuation_pattern",
                      "Triple_candle_stick_pattern", "Triple_candle_reversal_pattern",
                      "Triple_candle_continuation_pattern",
                      "SUPERTREND", "RSI_DIVERGENCE", "STOCHASTIC", "OBV", "PIVOT_POINTS"}
OPTIONS_ANALYSES = {"MAX_PAIN", "MAX_PAIN_TREND", "MAX_PAIN_ALIGNMENT",
                    "GEX_FLIP_PROXIMITY", "GEX_WALL", "GEX_WALL_BREACH", "GEX_IMBALANCE",
                    "PCR_EXTREME", "PCR_BIAS", "PCR_TREND", "PCR_INTRADAY_TREND",
                    "PCR_REVERSAL", "PCR_POS_REVERSAL", "PCR_DIVERGENCE",
                    "IV_SPIKE", "IV_TREND", "IV_RANK", "IV_RANK_EXTREME",
                    "OI_BUILDUP", "OI_SUPPORT_RESISTANCE", "OI_WALL", "OI_SHIFT",
                    "OI_INTRADAY_TREND", "OI_SR_SHIFT",
                    "OI_CAPITULATION", "OI_WALL_MIGRATION",
                    "OI_POSITIONAL_TREND", "OI_ACCELERATION",
                    "IV_PREMIUM",
                    "PANIC_MODE", "PANIC_EXHAUSTION"}
FUTURES_ANALYSES = {"FUTURES_PREMIUM", "FUTURE_ACTION", "FUTURE_ACTION_LONG_BUILDUP",
                     "FUTURE_ACTION_SHORT_BUILDUP", "FUTURE_ACTION_SHORT_COVERING",
                     "FUTURE_ACTION_LONG_UNWINDING", "FUTURE_BREAKOUT_PATTERN",
                     "FUTURE_BREAKOUT_CONFIRMED", "FUTURE_BREAKOUT_MTF_ALIGNED",
                     "FUTURE_PVO_PATTERN", "FUTURE_PVO_BUILDUP",
                     "FUTURE_SIGNAL_SCORE_HIGH", "FUTURE_SIGNAL_SCORE_MEDIUM", "FUTURE_SIGNAL_SCORE_LOW",
                     "FUTURE_OI_TREND", "FUTURE_COST_OF_CARRY", "FUTURE_ROLLOVER", "FUTURE_OI_FROM_OPEN"}

# NEUTRAL signals that should NOT contribute to score
# These indicate uncertainty/mixed signals rather than actionable info
NEUTRAL_EXCLUDE_FROM_SCORE = {
    "MAX_PAIN_ALIGNMENT",   # When DIVERGENT - conflicting signals
    "MAX_PAIN_TREND",       # When DIVERGING - price moving away from max pain
    # PCR_DIVERGENCE removed — now emits directional BEARISH/BULLISH signals
    "OI_SUPPORT_RESISTANCE",# When neutral - just informational S/R levels
    "OI_SR_SHIFT",          # When neutral - range squeeze/expand is informational
    "FUTURE_ROLLOVER",      # Rollover pressure is context — not a directional signal
    # Option-seller composite keys — weight=0 above, also excluded here for safety
    # so the zero-weight fallback path in calculate_score cannot accidentally score them
    "RANGE_BOUND_SETUP",
    "SKEW_FADE_SETUP",
    "GAMMA_TRAP",
    "GAMMA_TRAP_ACTIVE",    # Boolean suppression flag written by Gamma Trap
    # GEX regime is purely informational — the directional signals (GEX_WALL_BREACH,
    # GEX_FLIP_PROXIMITY) score normally; regime context does not.
    "GEX_REGIME",
}

# NEUTRAL signals that SHOULD contribute to score (informational but valuable)
# Everything not in NEUTRAL_EXCLUDE_FROM_SCORE will score:
# - IV_SPIKE: Important volatility event
# - IV_TREND: Directional IV movement  
# - 52-week-high/low: Key price levels
# - ATR: Volatility measure
# - FUTURE_PVO_PATTERN: Price/Volume/OI patterns

#ZERODHA CONSTANTS
ENV_ZERODHA_USERNAME = "ZERODHA_USER"
ENV_ZERODHA_PASSWORD = "ZERODHA_PASS"
ENV_ZERODHA_ENC_TOKEN = "ZERODHA_ENC_TOKEN"
DUMMY_API_KEY_ZERODHA = "dummy_api_key"


NseOptionChainURL = "https://www.nseindia.com/option-chain"

# ---------- column lists-----------------






