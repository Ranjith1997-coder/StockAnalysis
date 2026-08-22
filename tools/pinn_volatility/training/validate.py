"""
Model evaluation against held-out data.

This starts with walk-forward holdout accuracy (evaluate_holdout) -- the
most directly useful check for "does this model actually predict tomorrow's
surface, not just interpolate within the training week." The full
acceptance-criteria gate (RMSE + calendar/butterfly violation rate
thresholds, plan section 7.4) is a separate, later piece of work.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import torch

from tools.pinn_volatility.model.pinn import normalize


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
