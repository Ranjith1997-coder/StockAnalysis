"""Option maths shared by analysts, paper trading and the backtester.

One implementation per formula so the venue cannot drift between the signal
that suggests a trade, the code that sizes it and the report that scores it.

Conventions:
- ``iv`` is a decimal (0.13 = 13 %), ``tau`` is years to expiry.
- Probabilities are **risk-neutral** (from the option's own implied vol), not a
  forecast. They answer "what does the market pay for this outcome", not "what
  will happen".
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

from tools.pinn_volatility.model.bs_utils import norm_cdf

RISK_FREE = 0.065
STRADDLE_TO_SIGMA = 0.8      # ATM straddle ≈ 0.8 · σ√T


def daily_expected_move(straddle: float, dte_trading_days: Optional[int]) -> float:
    """1-day expected move implied by an ATM straddle.

    ATM straddle ≈ 0.8·σ·S·√T, so the expected absolute move for one session is
    ``straddle·√(1/DTE)`` with DTE in trading days. DTE ≤ 1 → the straddle.
    """
    if straddle is None or straddle <= 0:
        return 0.0
    dte = max(1, int(dte_trading_days or 1))
    return straddle * math.sqrt(1.0 / dte)


def expected_move_sigma(straddle: float) -> float:
    """One standard deviation of the move over the remaining life."""
    if straddle is None or straddle <= 0:
        return 0.0
    return straddle / STRADDLE_TO_SIGMA


def d2(spot: float, strike: float, iv: float, tau: float,
       r: float = RISK_FREE, q: float = 0.0) -> float:
    if spot <= 0 or strike <= 0 or iv <= 0 or tau <= 0:
        raise ValueError("d2 needs spot>0, strike>0, iv>0, tau>0")
    return (math.log(spot / strike) + (r - q - 0.5 * iv * iv) * tau) / (iv * math.sqrt(tau))


def prob_above(spot: float, strike: float, iv: float, tau: float,
               r: float = RISK_FREE, q: float = 0.0) -> float:
    """Risk-neutral P(S_T > strike)."""
    return norm_cdf(d2(spot, strike, iv, tau, r, q))


def prob_below(spot: float, strike: float, iv: float, tau: float,
               r: float = RISK_FREE, q: float = 0.0) -> float:
    return 1.0 - prob_above(spot, strike, iv, tau, r, q)


def pop_short_put(strike: float, spot: float, iv: float, tau: float) -> float:
    """P(S_T > strike) — a sold put finishes worthless."""
    return prob_above(spot, strike, iv, tau)


def pop_short_call(strike: float, spot: float, iv: float, tau: float) -> float:
    """P(S_T < strike) — a sold call finishes worthless."""
    return prob_below(spot, strike, iv, tau)


def pop_condor(put_k: float, call_k: float, spot: float, iv: float, tau: float) -> float:
    """P(put_k < S_T < call_k), the probability a credit condor expires full."""
    if put_k >= call_k:
        raise ValueError("pop_condor needs put_k < call_k")
    return max(0.0, min(1.0, prob_above(spot, put_k, iv, tau)
                        - prob_above(spot, call_k, iv, tau)))


def iv_percentile(series: Iterable[float], value: float,
                  min_samples: int = 20) -> Optional[float]:
    """Percent of historical closes at or below ``value`` (0-100)."""
    values = [float(x) for x in series if x is not None and not _is_nan(x)]
    if value is None or len(values) < min_samples:
        return None
    return 100.0 * sum(1 for x in values if x <= value) / len(values)


def iv_rank(series: Iterable[float], value: float,
            min_samples: int = 20) -> Optional[float]:
    """Position of ``value`` within the historical min-max range (0-100)."""
    values = [float(x) for x in series if x is not None and not _is_nan(x)]
    if value is None or len(values) < min_samples:
        return None
    low, high = min(values), max(values)
    if high <= low:
        return None
    return max(0.0, min(100.0, 100.0 * (float(value) - low) / (high - low)))


def suggest_condor_strikes(spot: float, gap: float, iv: float, tau: float,
                           target_pop: float, max_width: int = 8) -> Optional[tuple[float, float, float]]:
    """Narrowest symmetric condor meeting ``target_pop``.

    Returns ``(put_strike, call_strike, achieved_pop)`` or None when the widest
    tested width still falls short.
    """
    if spot <= 0 or gap <= 0:
        return None
    for width in range(1, max_width + 1):
        put_k = spot - width * gap
        call_k = spot + width * gap
        if put_k <= 0:
            continue
        pop = pop_condor(put_k, call_k, spot, iv, tau)
        if pop >= target_pop:
            return put_k, call_k, pop
    return None


def _is_nan(value) -> bool:
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True


def widen_short_strikes(put_k: float, call_k: float, spot: float, gap: float,
                        iv: float, tau: float, min_pop: float,
                        max_steps: int = 4) -> tuple[float, float]:
    """Widen a condor/strangle until ``pop_condor`` reaches ``min_pop``.

    Returns the original strikes when the gate is disabled (``min_pop <= 0``)
    or when even the widest allowed step falls short — a gate that silently
    moves the trade further than intended would be worse than no gate.
    """
    if not min_pop or min_pop <= 0 or gap <= 0:
        return put_k, call_k
    if pop_condor(put_k, call_k, spot, iv, tau) >= min_pop:
        return put_k, call_k
    for step in range(1, max_steps + 1):
        wider_put = put_k - step * gap
        wider_call = call_k + step * gap
        if wider_put <= 0:
            break
        if pop_condor(wider_put, wider_call, spot, iv, tau) >= min_pop:
            return wider_put, wider_call
    return put_k, call_k


def expected_move_band(spot: float, straddle: float, sigmas: float = 1.0) -> tuple[float, float]:
    """Price band ``spot ± sigmas·σ`` for the remaining life."""
    sigma = expected_move_sigma(straddle) * sigmas
    return spot - sigma, spot + sigma
