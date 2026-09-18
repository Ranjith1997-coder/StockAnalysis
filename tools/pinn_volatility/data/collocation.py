"""
Collocation-point sampling for the PDE (no-arbitrage) penalty terms.

These points carry no market data at all — they're synthetic (k, tau)
coordinates used purely to evaluate whether the network's own surface
satisfies the calendar and butterfly/Durrleman conditions (losses/arbitrage.py)
across the input domain, not just at the handful of strikes NSE happened to
trade that day.
"""
from __future__ import annotations

import numpy as np
import torch


def latin_hypercube(n: int, lo: float, hi: float, rng=None) -> np.ndarray:
    """Latin Hypercube Sampling in 1D — better space-filling than plain uniform.

    Splits [lo, hi] into n equal bins and draws exactly one uniform sample
    from each bin, then shuffles — guarantees coverage across the whole
    range even for small n, unlike plain np.random.uniform(lo, hi, n) which
    can leave gaps by chance.
    """
    rng = rng if rng is not None else np.random
    edges = np.linspace(lo, hi, n + 1)
    samples = np.array([rng.uniform(edges[i], edges[i + 1]) for i in range(n)])
    rng.shuffle(samples)
    return samples


def sample_collocation(
    n_points: int = 2000,
    k_range: tuple[float, float] = (-2.0, 2.0),
    tau_range: tuple[float, float] = (0.003, 1.0),
    concentrated_frac: float = 0.6,
    short_tau_boost_frac: float = 0.0,
    short_tau_boost_range: tuple[float, float] = (0.005, 0.02),
    rng=None,
) -> torch.Tensor:
    """Sample collocation points for PDE penalty evaluation.

    Strategy: `concentrated_frac` of points are concentrated near ATM
    (|k| < 0.5) and short-dated (tau < 0.15) — where the vol surface has
    the most curvature and arbitrage violations are most likely to hide.
    The remainder is spread across the full domain via Latin Hypercube
    Sampling, so the penalty is still checked everywhere, just less densely.

    Args:
        short_tau_boost_frac: optional extra fraction of `n_points` carved
            out and placed uniformly across the *full* k_range within the
            narrow `short_tau_boost_range` window, on top of the existing
            concentrated/spread split (which already covers tau < 0.15, but
            at much lower density right at the tau_range floor). 0.0
            (default) reproduces the original two-zone behavior exactly.
            Experimental -- added for the 2026-09-17 short-tau extrapolation
            investigation (see conversation history), not validated as a
            default: collocation points only constrain the arbitrage/PDE
            penalty (calendar + butterfly), never the data-fitting loss, so
            this can enforce local smoothness near the tau floor but cannot
            by itself correct a data-fitting level error there.
        short_tau_boost_range: the narrow tau window to boost, clipped to
            the caller's tau_range.

    Returns:
        (n_points, 2) float32 tensor of (k, tau) pairs, shuffled.
    """
    rng = rng if rng is not None else np.random

    n_boost = int(n_points * short_tau_boost_frac) if short_tau_boost_frac > 0 else 0
    n_remaining = n_points - n_boost

    n_concentrated = int(n_remaining * concentrated_frac)
    n_spread = n_remaining - n_concentrated

    # Concentrated zone, clipped to the caller's domain in case it's narrower
    # than the usual ATM/short-dated band.
    k_conc_lo = max(-0.5, k_range[0])
    k_conc_hi = min(0.5, k_range[1])
    tau_conc_lo = tau_range[0]
    tau_conc_hi = min(0.15, tau_range[1])

    k_conc = rng.uniform(k_conc_lo, k_conc_hi, n_concentrated)
    tau_conc = rng.uniform(tau_conc_lo, tau_conc_hi, n_concentrated)

    # Spread zone, full domain via Latin Hypercube.
    lhs_k = latin_hypercube(n_spread, k_range[0], k_range[1], rng=rng)
    lhs_tau = latin_hypercube(n_spread, tau_range[0], tau_range[1], rng=rng)

    k_parts = [k_conc, lhs_k]
    tau_parts = [tau_conc, lhs_tau]

    if n_boost > 0:
        boost_tau_lo = max(short_tau_boost_range[0], tau_range[0])
        boost_tau_hi = min(short_tau_boost_range[1], tau_range[1])
        k_boost = rng.uniform(k_range[0], k_range[1], n_boost)
        tau_boost = rng.uniform(boost_tau_lo, boost_tau_hi, n_boost)
        k_parts.append(k_boost)
        tau_parts.append(tau_boost)

    k_all = np.concatenate(k_parts)
    tau_all = np.concatenate(tau_parts)

    idx = rng.permutation(n_points)
    k_all = k_all[idx]
    tau_all = tau_all[idx]

    return torch.tensor(np.stack([k_all, tau_all], axis=1), dtype=torch.float32)
