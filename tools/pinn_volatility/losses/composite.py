"""
Composite training loss: weighted sum of the data-fit term (beta-NLL) and
the two no-arbitrage PDE penalties (calendar + butterfly/Durrleman).

Accepts RAW (k, tau, w) training samples and RAW collocation points --
wraps the model in RawInputModel internally (model/pinn.py) so callers never
need to think about normalize() explicitly; the network still only ever
sees properly-normalized input under the hood.
"""
from __future__ import annotations

import torch

from tools.pinn_volatility.model.pinn import VolatilityPINN, RawInputModel
from tools.pinn_volatility.losses.data_loss import beta_nll_loss, tau_weight, moneyness_weight
from tools.pinn_volatility.losses.arbitrage import calendar_penalty, butterfly_penalty


def composite_loss(
    model: VolatilityPINN,
    k_train: torch.Tensor,
    tau_train: torch.Tensor,
    w_train: torch.Tensor,
    collocation: torch.Tensor,
    beta: float = 0.5,
    lambda_data: float = 1.0,
    lambda_cal: float = 1.0,
    lambda_but: float = 0.5,
    use_tau_weight: bool = False,
    use_moneyness_weight: bool = False,
    tau_weight_max: float = 20.0,
    moneyness_alpha: float = 5.0,
) -> tuple[torch.Tensor, dict[str, float]]:
    """Compute the full composite training loss.

    Args:
        model: the underlying VolatilityPINN (normalization handled internally
            via RawInputModel -- pass the raw model here, not a wrapper).
        k_train, tau_train, w_train: RAW training samples (e.g. from
            dataset.samples_to_tensors()), each shape (N,).
        collocation: RAW (k, tau) collocation points, shape (M, 2)
            (e.g. from data/collocation.sample_collocation()).
        beta: beta-NLL interpolation parameter (see losses/data_loss.py).
        lambda_data, lambda_cal, lambda_but: loss term weights.
        use_tau_weight: if True, up-weight short-dated samples so the data
            loss grades implied-vol accuracy rather than total-variance
            accuracy (see losses/data_loss.tau_weight docstring). Default
            False -- reproduces the original behavior exactly.
        use_moneyness_weight: if True, up-weight wing (OTM/ITM) samples
            relative to ATM (see losses/data_loss.moneyness_weight
            docstring). Default False -- reproduces the original behavior.
        tau_weight_max, moneyness_alpha: tuning knobs for the above, only
            relevant when the corresponding flag is True.

    Returns:
        (total_loss, breakdown) -- total_loss is the scalar to call
        .backward() on; breakdown is a dict of the individual UNWEIGHTED
        component values as plain floats (for logging/monitoring during
        training, e.g. to see the butterfly penalty trending down even while
        the weighted total is dominated by the data term).
    """
    wrapped = RawInputModel(model)

    k_tau_train = torch.stack([k_train, tau_train], dim=-1)
    mu, v_squared = wrapped(k_tau_train)

    sample_weights = None
    if use_tau_weight or use_moneyness_weight:
        sample_weights = torch.ones_like(tau_train)
        if use_tau_weight:
            sample_weights = sample_weights * tau_weight(tau_train, max_weight=tau_weight_max)
        if use_moneyness_weight:
            sample_weights = sample_weights * moneyness_weight(k_train, alpha=moneyness_alpha)

    l_data = beta_nll_loss(mu, v_squared, w_train, beta=beta, sample_weights=sample_weights)
    l_cal = calendar_penalty(wrapped, collocation)
    l_but = butterfly_penalty(wrapped, collocation)

    total = lambda_data * l_data + lambda_cal * l_cal + lambda_but * l_but

    breakdown = {
        "data": l_data.item(),
        "calendar": l_cal.item(),
        "butterfly": l_but.item(),
        "total": total.item(),
    }
    return total, breakdown
