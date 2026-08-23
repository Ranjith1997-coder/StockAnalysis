"""
Model evaluation against held-out data.

This starts with walk-forward holdout accuracy (evaluate_holdout) -- the
most directly useful check for "does this model actually predict tomorrow's
surface, not just interpolate within the training week." audit_arbitrage()
adds an independent, dense post-hoc no-arbitrage check. The full
acceptance-criteria gate (RMSE + calendar/butterfly violation rate
thresholds, plan section 7.4) is a separate, later piece of work.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import torch

from tools.pinn_volatility.model.pinn import normalize, RawInputModel, K_RANGE, TAU_RANGE
from tools.pinn_volatility.data.collocation import sample_collocation
from tools.pinn_volatility.losses.arbitrage import durrleman_density, calendar_slope


@dataclass
class HoldoutMetrics:
    n_samples: int
    rmse_w: float                       # RMSE on total implied variance w
    mae_sigma: float                    # mean absolute error on implied vol, in vol points (e.g. 0.02 = 2 pts)
    mean_bias_sigma: float              # signed: positive = model UNDER-predicts vol (actual > predicted)
    mae_sigma_by_moneyness: dict = field(default_factory=dict)  # {"atm": x, "wings": y} -- None if a bucket is empty


def evaluate_holdout(
    model,
    k: torch.Tensor,
    tau: torch.Tensor,
    w_actual: torch.Tensor,
    sigma_actual: torch.Tensor,
    atm_threshold: float = 0.1,
) -> HoldoutMetrics:
    """Evaluate a trained model against real (held-out) market data.

    Args:
        model: trained VolatilityPINN.
        k, tau: RAW (unnormalized) log-moneyness / time-to-expiry for the
            holdout samples (e.g. from split_by_holdout_date()'s output,
            via samples_to_tensors()).
        w_actual, sigma_actual: the REAL observed total variance / implied
            vol for those same samples (ground truth, from NSE settlement
            prices -- not model predictions).
        atm_threshold: |k| below this counts as "ATM" for the moneyness
            breakdown; the rest counts as "wings". The PINN's whole value
            proposition is shape (skew) accuracy, not just level -- a
            model that's accurate ATM but wrong on the wings would still
            fail at its actual job (SKEW_FADE/RANGE_BOUND signals are
            wing-driven), so this breakdown matters more than the
            aggregate MAE alone.

    Returns:
        HoldoutMetrics with aggregate + per-moneyness-bucket error.
    """
    model.eval()
    with torch.no_grad():
        mu_pred, _ = model(normalize(k, tau))
    w_pred = mu_pred.squeeze(-1)
    sigma_pred = torch.sqrt(torch.clamp(w_pred / tau, min=1e-8))

    rmse_w = torch.sqrt(((w_pred - w_actual) ** 2).mean()).item()
    mae_sigma = (sigma_pred - sigma_actual).abs().mean().item()
    mean_bias_sigma = (sigma_actual - sigma_pred).mean().item()

    atm_mask = k.abs() < atm_threshold
    wing_mask = ~atm_mask

    def _mae(mask: torch.Tensor):
        if mask.sum().item() == 0:
            return None
        return (sigma_pred[mask] - sigma_actual[mask]).abs().mean().item()

    return HoldoutMetrics(
        n_samples=len(k),
        rmse_w=rmse_w,
        mae_sigma=mae_sigma,
        mean_bias_sigma=mean_bias_sigma,
        mae_sigma_by_moneyness={"atm": _mae(atm_mask), "wings": _mae(wing_mask)},
    )


@dataclass
class ArbitrageAudit:
    n_points: int
    min_g: float                      # worst (most negative) Durrleman density found -- < 0 means a real butterfly violation exists
    min_calendar_slope: float         # worst (most negative) dw/dtau found -- < 0 means a real calendar violation exists
    butterfly_violation_rate: float   # fraction of audited points with g(k) < 0
    calendar_violation_rate: float    # fraction of audited points with dw/dtau < 0
    max_butterfly_violation: float    # magnitude of the worst g(k) violation (0.0 if none found)
    max_calendar_violation: float     # magnitude of the worst dw/dtau violation (0.0 if none found)


def audit_arbitrage(
    model,
    n_points: int = 5000,
    k_range: tuple[float, float] = K_RANGE,
    tau_range: tuple[float, float] = TAU_RANGE,
    seed: int | None = None,
) -> ArbitrageAudit:
    """Independent, dense post-hoc no-arbitrage check.

    Why this is separate from composite_loss's per-epoch min_g/
    min_calendar_slope: those are computed on whatever small (e.g. 512-point),
    stochastically-resampled collocation batch that particular training step
    happened to draw -- useful for a live progress signal, but not a
    trustworthy final verdict, since a single lucky/unlucky batch could hide
    or exaggerate a violation. This samples a fresh, larger, denser
    collocation set purely for auditing, independent of anything used during
    training.

    A penalty of exactly 0.0 during training is consistent with EITHER "no
    violations anywhere" OR "violations exist but are tiny enough that
    clamp(-x, min=0)**2 rounds away" -- this function's raw min/violation-rate
    numbers are what actually distinguish those two cases.

    Args:
        model: a trained VolatilityPINN (or anything with the same
            (k_tau_norm) -> (mu, v_squared) interface).
        n_points: size of the fresh audit collocation sample.
        k_range, tau_range: audit domain -- defaults to the same domain the
            model was trained/normalized against (model/pinn.py's K_RANGE/
            TAU_RANGE).
        seed: optional seed for reproducible audits.

    Returns:
        ArbitrageAudit with raw minimums, violation rates, and worst-violation
        magnitudes for both the calendar and butterfly conditions.
    """
    was_training = getattr(model, "training", False)
    if hasattr(model, "eval"):
        model.eval()

    rng = np.random.default_rng(seed) if seed is not None else None
    points = sample_collocation(n_points, k_range=k_range, tau_range=tau_range, rng=rng)
    wrapped = RawInputModel(model) if not isinstance(model, RawInputModel) else model

    g = durrleman_density(wrapped, points).detach()
    slope = calendar_slope(wrapped, points).detach()

    butterfly_violations = g[g < 0]
    calendar_violations = slope[slope < 0]

    audit = ArbitrageAudit(
        n_points=n_points,
        min_g=g.min().item(),
        min_calendar_slope=slope.min().item(),
        butterfly_violation_rate=(g < 0).float().mean().item(),
        calendar_violation_rate=(slope < 0).float().mean().item(),
        max_butterfly_violation=(-butterfly_violations.min().item() if len(butterfly_violations) else 0.0),
        max_calendar_violation=(-calendar_violations.min().item() if len(calendar_violations) else 0.0),
    )

    if was_training and hasattr(model, "train"):
        model.train()

    return audit
