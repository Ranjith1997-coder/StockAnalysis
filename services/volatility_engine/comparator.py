"""
Compares live option prices (from Redis) against a trained VolatilityPINN's
fair implied-variance surface, and turns the mispricing into PinnSignal
objects. See docs/pinn-volatility-engine.md section 8.4/8.5 for the design
this implements -- adapted to this project's actual Redis schema and
VolatilityPINN/normalize() interfaces (see module-level deviations below).

Kept free of RedisProxy/network access so it's fully unit-testable: callers
in main.py do the Redis reads and pass plain dicts/values in here.

Deviations from the design doc's section 8 pseudocode:
  - `read_future_price`/`read_nearest_expiry` don't exist as such -- the real
    source is `data:sensibull:{symbol}` -> `current_json` ->
    `stats.per_expiry_map[expiry]["future_price"]`, keyed by ISO date
    strings, nearest = `sorted(per_expiry_map.keys())[0]` (same pattern
    MaxPainAnalyser already uses). See `nearest_expiry_and_forward()`.
  - `data:options_live:{symbol}` ticks store `ltp` (not `last_price`) and a
    `timestamp` that serializes as `str(datetime)` (e.g.
    "2026-08-25 14:32:10.123456"), not a Unix epoch -- see `_tick_age_seconds()`.
  - The doc's `check_signal_thresholds(symbol, results, now, expiry)` reads a
    free variable `tau` that was never passed into that function (a bug in
    the pseudocode) -- fixed here by passing `tau` explicitly.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd
import torch

from tools.pinn_volatility.model.bs_utils import invert_bs
from tools.pinn_volatility.model.pinn import normalize
from lib.logging_util import get_logger
logger = get_logger("volatility-engine")

RISK_FREE_RATE = 0.07
DIVIDEND_YIELD = 0.0
K_MAX = 2.0
MAX_IV = 2.0
MAX_TICK_AGE_SECONDS = 10.0  # matches PAPER_TRADING_DESIGN.md's staleness convention

# Absolute minimum IV difference to avoid noise on near-zero variance.
MIN_IV_DIFF_PCT = 2.0  # |sigma_live - sigma_fair| must be > 2 percentage points

# z-score thresholds (section 8.5).
SKEW_FADE_Z_THRESHOLD = 2.0      # one-sided overpricing
RANGE_BOUND_Z_THRESHOLD = 1.5    # both wings overpriced
RANGE_BOUND_ATM_Z_MAX = 1.0      # ATM must NOT be overpriced (shape, not level)


@dataclass
class StrikeData:
    strike: float
    option_type: str  # "CE" | "PE"
    ltp: float
    k: float
    live_sigma: float
    live_w: float


@dataclass
class EvalResult:
    strike_data: StrikeData
    fair_w: float
    uncertainty: float  # model's predicted std-dev of w at this (k, tau)
    z: float


@dataclass
class PinnSignal:
    symbol: str
    signal_type: str   # "SKEW_FADE_SETUP" | "RANGE_BOUND_SETUP"
    strategy: str
    direction: str
    z_score: float
    expiry: str
    timestamp: datetime
    # SKEW_FADE_SETUP fields
    overpriced_strike: float | None = None
    overpriced_type: str | None = None
    sr_level: float | None = None
    fair_iv: float | None = None
    live_iv: float | None = None
    # RANGE_BOUND_SETUP fields
    put_wall_strike: float | None = None
    call_wall_strike: float | None = None
    fair_iv_ce: float | None = None
    fair_iv_pe: float | None = None
    live_iv_ce: float | None = None
    live_iv_pe: float | None = None


def compute_tau(expiry: str, today: date) -> float:
    """Same convention as tools/pinn_volatility/data/dataset.py: calendar
    days / 365, not a trading-day count."""
    expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    return (expiry_date - today).days / 365.0


def nearest_expiry_and_forward(sensibull_current_json: str) -> tuple[str, float] | None:
    """Parse `data:sensibull:{symbol}`'s `current_json` field down to the
    nearest weekly expiry and its Sensibull-computed forward price.

    Returns None if the payload is missing/malformed/has no expiries.
    """
    try:
        data = json.loads(sensibull_current_json)
    except (TypeError, json.JSONDecodeError):
        return None

    per_expiry_map = (data.get("stats") or {}).get("per_expiry_map") or {}
    if not per_expiry_map:
        return None

    nearest_expiry = sorted(per_expiry_map.keys())[0]
    future_price = per_expiry_map[nearest_expiry].get("future_price")
    if not future_price:
        return None
    return nearest_expiry, float(future_price)


def _tick_age_seconds(tick: dict, now: datetime) -> float:
    """`timestamp` was serialized via `json.dumps(tick, default=str)` on a
    `datetime` -- parse leniently and treat unparseable/missing as infinitely
    stale rather than raising."""
    raw = tick.get("timestamp")
    if not raw:
        return float("inf")
    ts = pd.to_datetime(raw, errors="coerce")
    if ts is None or pd.isna(ts):
        return float("inf")
    return (now - ts.to_pydatetime().replace(tzinfo=None)).total_seconds()


def build_strikes_data(
    options_live_raw: dict[str, str],
    spot: float,
    forward: float,
    tau: float,
    now: datetime,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
    k_max: float = K_MAX,
    max_iv: float = MAX_IV,
    max_tick_age_s: float = MAX_TICK_AGE_SECONDS,
) -> list[StrikeData]:
    """Convert a raw `data:options_live:{symbol}` hash into live (k, sigma, w)
    per strike, via the same BS-inversion convention as
    tools/pinn_volatility/data/dataset.py's build_training_samples -- so the
    live surface and the training surface are computed identically.

    Never raises on a single bad strike -- skips it and keeps going; logs a
    skip-reason breakdown at DEBUG (this runs every 3s, INFO would be noisy).
    """
    results: list[StrikeData] = []
    skipped = {"stale": 0, "bad_ltp": 0, "k_range": 0, "no_iv_solution": 0, "iv_range": 0}

    for key, raw in options_live_raw.items():
        parts = key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        try:
            strike = float(parts[0])
        except ValueError:
            continue
        option_type = parts[1]

        try:
            tick = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(tick, dict):
            continue

        if _tick_age_seconds(tick, now) > max_tick_age_s:
            skipped["stale"] += 1
            continue

        ltp = float(tick.get("ltp") or 0)
        if ltp <= 0:
            skipped["bad_ltp"] += 1
            continue

        k = math.log(strike / forward)
        if abs(k) > k_max:
            skipped["k_range"] += 1
            continue

        live_sigma = invert_bs(spot, strike, tau, r, q, ltp, option_type)
        if live_sigma is None:
            skipped["no_iv_solution"] += 1
            continue
        if live_sigma > max_iv:
            skipped["iv_range"] += 1
            continue

        live_w = live_sigma ** 2 * tau
        results.append(StrikeData(strike=strike, option_type=option_type, ltp=ltp,
                                   k=k, live_sigma=live_sigma, live_w=live_w))

    logger.debug("[vol-engine] build_strikes_data: %d kept, skipped=%s", len(results), skipped)
    return results


def evaluate_model(model, strikes: list[StrikeData], tau: float) -> list[EvalResult]:
    """Run the PINN forward pass and compute a z-score per strike:
    z = (live_w - fair_w) / max(model_std, eps) -- how many
    model-uncertainty-widths the market price sits from the fair surface."""
    if not strikes:
        return []

    k = torch.tensor([s.k for s in strikes], dtype=torch.float32)
    tau_t = torch.full_like(k, tau)
    with torch.no_grad():
        mu, v_squared = model(normalize(k, tau_t))
    fair_w = mu.squeeze(-1)
    std = torch.sqrt(torch.clamp(v_squared.squeeze(-1), min=0.0))

    results = []
    for i, sd in enumerate(strikes):
        fw = fair_w[i].item()
        v = std[i].item()
        z = (sd.live_w - fw) / max(v, 1e-8)
        results.append(EvalResult(strike_data=sd, fair_w=fw, uncertainty=v, z=z))
    return results


def _fair_iv(result: EvalResult, tau: float) -> float:
    return math.sqrt(max(result.fair_w, 0.0) / tau)


def check_signal_thresholds(
    symbol: str, results: list[EvalResult], tau: float, expiry: str, now: datetime,
) -> list[PinnSignal]:
    """Turn per-strike z-scores into SKEW_FADE_SETUP / RANGE_BOUND_SETUP
    signals. See section 8.5 of the design doc for the underlying logic --
    `tau` is passed explicitly here (the doc's pseudocode referenced it as an
    unpassed free variable)."""
    if not results:
        return []

    signals: list[PinnSignal] = []
    ce_results = [r for r in results if r.strike_data.option_type == "CE"]
    pe_results = [r for r in results if r.strike_data.option_type == "PE"]
    atm_result = min(results, key=lambda r: abs(r.strike_data.k))

    ce_max = max(ce_results, key=lambda r: r.z) if ce_results else None
    pe_max = max(pe_results, key=lambda r: r.z) if pe_results else None

    if ce_max and ce_max.z > SKEW_FADE_Z_THRESHOLD:
        fair_iv = _fair_iv(ce_max, tau)
        iv_diff_pct = (ce_max.strike_data.live_sigma - fair_iv) * 100
        if iv_diff_pct >= MIN_IV_DIFF_PCT:
            signals.append(PinnSignal(
                symbol=symbol, signal_type="SKEW_FADE_SETUP", strategy="CREDIT_SPREAD",
                direction="BEARISH", overpriced_strike=ce_max.strike_data.strike,
                overpriced_type="CE", sr_level=ce_max.strike_data.strike,
                z_score=ce_max.z, fair_iv=fair_iv, live_iv=ce_max.strike_data.live_sigma,
                expiry=expiry, timestamp=now,
            ))

    if pe_max and pe_max.z > SKEW_FADE_Z_THRESHOLD:
        fair_iv = _fair_iv(pe_max, tau)
        iv_diff_pct = (pe_max.strike_data.live_sigma - fair_iv) * 100
        if iv_diff_pct >= MIN_IV_DIFF_PCT:
            signals.append(PinnSignal(
                symbol=symbol, signal_type="SKEW_FADE_SETUP", strategy="CREDIT_SPREAD",
                direction="BULLISH", overpriced_strike=pe_max.strike_data.strike,
                overpriced_type="PE", sr_level=pe_max.strike_data.strike,
                z_score=pe_max.z, fair_iv=fair_iv, live_iv=pe_max.strike_data.live_sigma,
                expiry=expiry, timestamp=now,
            ))

    if (ce_max and pe_max
            and ce_max.z > RANGE_BOUND_Z_THRESHOLD
            and pe_max.z > RANGE_BOUND_Z_THRESHOLD
            and abs(atm_result.z) < RANGE_BOUND_ATM_Z_MAX):
        signals.append(PinnSignal(
            symbol=symbol, signal_type="RANGE_BOUND_SETUP", strategy="IRON_CONDOR",
            direction="NEUTRAL", put_wall_strike=pe_max.strike_data.strike,
            call_wall_strike=ce_max.strike_data.strike, z_score=min(ce_max.z, pe_max.z),
            fair_iv_ce=_fair_iv(ce_max, tau), fair_iv_pe=_fair_iv(pe_max, tau),
            live_iv_ce=ce_max.strike_data.live_sigma, live_iv_pe=pe_max.strike_data.live_sigma,
            expiry=expiry, timestamp=now,
        ))

    return signals
