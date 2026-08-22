"""
Black-Scholes pricing + implied-volatility inversion.

Shared by the PINN training pipeline (dataset.py converts Bhavcopy settlement
prices into implied vol) and, later, the live inference service (comparator.py
converts live LTPs into implied vol the same way). Keeping both paths on one
implementation guarantees training and inference define "implied vol"
identically — a mismatch here would silently bias the whole model.

Forward-price convention: all functions here take the forward price F would
normally require carrying r and q through every call. Callers in this project
prefer to derive F once (e.g. from a Bhavcopy FUTIDX row, or from
`data:options_agg:{symbol}` at inference), so bs_price/invert_bs still accept
(S, K, r, q) directly for standalone use, but forward-price-aware callers
should just pass q=0 and use F in place of S * exp(r*tau) upstream — this
module does not special-case the forward-price path itself, to keep the
formulas exactly matching the textbook definition and cheap to audit.
"""
from __future__ import annotations

import math

from scipy.optimize import brentq

from lib.logging_util import get_logger
logger = get_logger("pinn")


def norm_cdf(x: float) -> float:
    """Standard normal CDF, via math.erf (no scipy.stats dependency needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-x ** 2 / 2.0) / math.sqrt(2.0 * math.pi)


def bs_price(S: float, K: float, tau: float, r: float, q: float,
             sigma: float, option_type: str) -> float:
    """Black-Scholes price for a European CE/PE with continuous dividend yield q.

    Args:
        S: spot (underlying) price.
        K: strike price.
        tau: time to expiry in years.
        r: risk-free rate (annualized, continuously compounded).
        q: dividend yield (annualized, continuously compounded).
        sigma: volatility (annualized).
        option_type: "CE" (call) or "PE" (put).

    Returns:
        Theoretical option price. Falls back to discounted intrinsic value
        when tau <= 0 or sigma <= 0 (the BS formula is undefined there).
    """
    if option_type not in ("CE", "PE"):
        raise ValueError(f"option_type must be 'CE' or 'PE', got {option_type!r}")

    if tau <= 0 or sigma <= 0:
        fwd_spot = S * math.exp(-q * tau) if tau > 0 else S
        fwd_strike = K * math.exp(-r * tau) if tau > 0 else K
        if option_type == "CE":
            return max(fwd_spot - fwd_strike, 0.0)
        return max(fwd_strike - fwd_spot, 0.0)

    d1 = (math.log(S / K) + (r - q + sigma ** 2 / 2) * tau) / (sigma * math.sqrt(tau))
    d2 = d1 - sigma * math.sqrt(tau)

    if option_type == "CE":
        return S * math.exp(-q * tau) * norm_cdf(d1) - K * math.exp(-r * tau) * norm_cdf(d2)
    return K * math.exp(-r * tau) * norm_cdf(-d2) - S * math.exp(-q * tau) * norm_cdf(-d1)


def invert_bs(S: float, K: float, tau: float, r: float, q: float,
              price: float, option_type: str,
              vol_lo: float = 0.001, vol_hi: float = 5.0) -> float | None:
    """Invert Black-Scholes to recover implied volatility from an option price.

    Uses Brent's method (scipy.optimize.brentq) on [vol_lo, vol_hi] — bracketed
    root-finding, robust and doesn't need a derivative or a starting guess
    (unlike Newton-Raphson, which can diverge for deep OTM options).

    Returns:
        Implied volatility, or None if no solution exists in the bracket —
        this happens when `price` is below intrinsic value (bad/stale tick,
        or the option is trading through a wide/crossed spread) or the true
        implied vol falls outside [vol_lo, vol_hi] (essentially never happens
        for vol_hi=5.0 = 500% on real index options).
    """
    if tau <= 0 or price <= 0:
        return None

    def objective(sigma: float) -> float:
        return bs_price(S, K, tau, r, q, sigma, option_type) - price

    try:
        f_lo = objective(vol_lo)
        f_hi = objective(vol_hi)
        if f_lo * f_hi > 0:
            # No sign change across the bracket -> no root in range.
            return None
        return brentq(objective, vol_lo, vol_hi, xtol=1e-6, maxiter=100)
    except (ValueError, RuntimeError) as e:
        logger.debug("[pinn] invert_bs failed S=%.2f K=%.2f tau=%.4f price=%.2f %s: %s",
                     S, K, tau, price, option_type, e)
        return None


def vega(S: float, K: float, tau: float, r: float, q: float, sigma: float) -> float:
    """Black-Scholes Vega: dPrice/dSigma (same formula for CE and PE).

    Used to weight the training loss toward pricing-space error rather than
    raw vol-space error -- deep OTM/ITM options have Vega -> 0, meaning a
    tiny (even sub-tick) settlement-price change implies a huge apparent IV
    swing there (Delta_sigma ~ Delta_price / Vega blows up as Vega -> 0).
    An unweighted vol-space loss lets that noise dominate the gradient on
    illiquid fringe strikes; weighting by vega^2 makes the loss reflect
    monetary/pricing error (Delta_C ~ Vega * Delta_sigma) instead, which is
    naturally near-zero for exactly the samples where the IV reading itself
    is least trustworthy.

    Returns 0.0 for tau<=0 or sigma<=0 (undefined / no meaningful sensitivity).
    """
    if tau <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r - q + sigma ** 2 / 2) * tau) / (sigma * math.sqrt(tau))
    return S * math.exp(-q * tau) * norm_pdf(d1) * math.sqrt(tau)
