"""
beta-NLL loss (Seitzer et al. 2022, "On the Pitfalls of Heteroscedastic
Uncertainty Estimation with Probabilistic Neural Networks").

NOTE on the plan doc's pseudocode: .kilo/plans/pinn-volatility-engine.md
section 6.2 detaches v_squared inside BOTH the NLL term and the re-weighting
factor. That's a bug, not a simplification -- it removes every gradient path
to the variance head entirely (confirmed empirically: raw_v2.grad is None
with that version), so the network could never learn to predict uncertainty
at all, silently defeating the whole point of beta-NLL. The fix (below,
matching the actual paper): only the re-weighting FACTOR is stop-gradiented;
v_squared inside the NLL term itself keeps its gradient.
"""
from __future__ import annotations

import torch


def beta_nll_loss(mu: torch.Tensor, v_squared: torch.Tensor, target: torch.Tensor,
                   beta: float = 0.5, sample_weights: torch.Tensor | None = None) -> torch.Tensor:
    """beta-NLL loss. Re-weights the per-sample NLL by (v_squared)^beta,
    with the weighting factor's gradient stopped -- this is what prevents
    variance collapse, while the NLL term itself keeps a real gradient path
    to v_squared so the network can still learn heteroscedastic uncertainty.

    beta=0: reduces to standard Gaussian NLL (susceptible to collapse).
    beta=1: NLL's own v_squared-dependence is exactly cancelled by the
            (now-constant-valued) weight at the point of evaluation, leaving
            behavior close to plain MSE on the mean prediction.
    beta=0.5: paper's recommended balance.

    Args:
        mu: (N, 1) predicted mean.
        v_squared: (N, 1) predicted variance (must be > 0).
        target: (N,) or (N, 1) ground truth.
        beta: interpolation parameter in [0, 1].
        sample_weights: optional (N,) or (N, 1) per-sample importance
            weights (e.g. tau_weight()/moneyness_weight() from this same
            module) -- multiplies the WHOLE per-sample NLL term (both the
            residual and the log-variance parts), standard weighted-loss
            convention. Internally re-normalized to mean 1.0 so the overall
            loss scale stays comparable to the unweighted case -- otherwise
            the average weight magnitude would silently rescale this term
            relative to the calendar/butterfly penalties in composite_loss.
            None (default) reproduces the original unweighted behavior
            exactly.

    Returns:
        Scalar loss (mean over the batch).
    """
    target = target.reshape(mu.shape)

    # Stop-gradient ONLY on the re-weighting factor -- this is the one
    # detach() call that belongs in this function.
    weight = v_squared.detach() ** beta

    nll = 0.5 * torch.log(v_squared) + 0.5 * (target - mu) ** 2 / v_squared
    per_sample = weight * nll

    if sample_weights is not None:
        sw = sample_weights.reshape(mu.shape)
        sw = sw / sw.mean()  # keep overall loss scale comparable to unweighted
        per_sample = per_sample * sw

    return per_sample.mean()


def tau_weight(tau: torch.Tensor, floor: float = 1e-4, max_weight: float = 20.0) -> torch.Tensor:
    """Up-weight short-dated samples in the loss so the network is graded on
    implied-vol accuracy (sigma), not total-variance accuracy (w).

    Since sigma = sqrt(w/tau), d(sigma)/d(w) = 1/(2*sigma*tau) -- a FIXED
    w-residual corresponds to a LARGER sigma-residual as tau shrinks. An
    unweighted w-loss therefore implicitly under-penalizes short-dated
    (weekly) option errors relative to longer-dated ones.

    Capped at `max_weight`: an uncapped 1/tau^2 reaches ~9,174x at
    tau=0.003 (the plan's own minimum, K_RANGE/TAU_RANGE in model/pinn.py)
    -- large enough to dominate the loss and plausibly cause the same kind
    of gradient instability that motivated gradient clipping in the trainer,
    which would defeat the purpose of adding this weight in the first place.
    """
    raw = 1.0 / (tau ** 2 + floor)
    return torch.clamp(raw, max=max_weight)


def moneyness_weight(k: torch.Tensor, alpha: float = 5.0) -> torch.Tensor:
    """Up-weight wing (OTM/ITM, large |k|) samples relative to ATM.

    Wings are structurally under-represented in the training data (in a
    real 8-day NIFTY+BANKNIFTY sample, wings were ~11-16% of rows depending
    on the liquidity filter -- see conversation), yet wing accuracy is
    exactly what the PINN's shape/skew signal (SKEW_FADE_SETUP,
    RANGE_BOUND_SETUP) depends on -- an unweighted loss lets the optimizer
    "cheat" by fitting ATM well and leaving the wings comparatively flat,
    since ATM samples dominate the average.
    """
    return 1.0 + alpha * k ** 2


# NOTE: a vega_weight() (Black-Scholes-vega-squared sample reweighting) was
# tried here and deliberately removed. An isolation experiment (see
# conversation, feature/pinn-volatility-engine branch history) found it
# actively HURT wing accuracy -- both raw and sqrt(tau)-normalized variants
# -- rather than helping: it suppresses gradient signal from low-vega
# (mostly deep-wing) samples by design, which is the opposite of what wing
# *accuracy* needs. Fourier feature encoding (model/pinn.py's
# num_fourier_bands) turned out to be doing 100% of the real improvement;
# combining it with vega-weighting only diluted that gain. bs_utils.vega()
# itself is kept (a correct, independently useful BS primitive, and
# dataset.py still records it per-sample) -- only the loss-reweighting use
# of it was removed.
