"""
No-arbitrage PDE penalties: calendar spread + butterfly (Durrleman condition).

Evaluated on synthetic collocation points (data/collocation.py), not market
data -- these enforce that the network's own surface stays internally
consistent everywhere in the domain, not just at the handful of strikes
NSE happened to trade that day.
"""
from __future__ import annotations

import torch


def calendar_slope(model, k_tau: torch.Tensor) -> torch.Tensor:
    """Raw ∂w/∂tau per collocation point, via autodiff -- NOT clamped or
    squared. Positive everywhere satisfies the calendar no-arbitrage
    condition; any negative value is a genuine violation at that point.

    Shape (N,) -- mirrors durrleman_density()'s convention (below), so both
    can feed either a training penalty (calendar_penalty) or diagnostic/audit
    code (training/validate.py's audit_arbitrage) that wants the raw
    min/distribution, not just the aggregate penalty.
    """
    k_tau = k_tau.clone().requires_grad_(True)
    mu, _ = model(k_tau)

    grad_outputs = torch.ones_like(mu)
    grads = torch.autograd.grad(mu, k_tau, grad_outputs=grad_outputs,
                                 create_graph=True)[0]
    return grads[:, 1]  # ∂w/∂tau (index 1 of the input is tau), shape (N,)


def calendar_penalty(model, k_tau_collocation: torch.Tensor) -> torch.Tensor:
    """Penalty for calendar arbitrage: total variance must be non-decreasing
    in tau, i.e. ∂w/∂tau >= 0 everywhere.

    Uses first-order autograd (via calendar_slope). Only violations
    (negative derivative) are penalized -- a surface with ∂w/∂tau > 0
    contributes zero penalty.
    """
    dw_dtau = calendar_slope(model, k_tau_collocation)
    violation = torch.clamp(-dw_dtau, min=0.0)
    return (violation ** 2).mean()


def durrleman_density(model, k_tau: torch.Tensor) -> torch.Tensor:
    """Compute g(k), the Durrleman risk-neutral-density proxy, via autodiff.

        g(k) = (1 - k*w'/(2w))^2 - (w'^2/4)*(1/w + 1/4) + w''/2

    where w' = dw/dk, w'' = d^2(w)/dk^2. g(k) >= 0 everywhere is the
    butterfly no-arbitrage condition; g(k) < 0 at some k means the implied
    risk-neutral density there is negative, which is not economically
    possible -- a genuine arbitrage opportunity in the surface.

    Uses second-order autograd (needs create_graph=True on the first grad
    call so the second grad call has something to differentiate through).

    Returns:
        g(k) per input point, shape (N,) -- NOT clamped or squared, so this
        is reusable both for the training penalty (butterfly_penalty, below)
        and for the live arbitrage monitor (Phase 2, checks g(k) < 0 at
        actual traded strikes to detect model degradation post-training).
    """
    k_tau = k_tau.clone().requires_grad_(True)
    mu, _ = model(k_tau)
    w = mu.squeeze(-1)  # (N,)

    grad1 = torch.autograd.grad(w.sum(), k_tau, create_graph=True, retain_graph=True)[0]
    w_prime = grad1[:, 0]  # ∂w/∂k

    grad2 = torch.autograd.grad(w_prime.sum(), k_tau, create_graph=True, retain_graph=True)[0]
    w_double_prime = grad2[:, 0]  # ∂²w/∂k²

    k = k_tau[:, 0]
    w_safe = w.clamp(min=1e-8)  # guard division; w should be > 0 for a valid surface

    term1 = (1 - k * w_prime / (2 * w_safe)) ** 2
    term2 = (w_prime ** 2 / 4) * (1 / w_safe + 0.25)
    term3 = w_double_prime / 2

    return term1 - term2 + term3


def butterfly_penalty(model, k_tau_collocation: torch.Tensor) -> torch.Tensor:
    """Penalty for butterfly arbitrage violations (negative risk-neutral density).

    Only violations (g(k) < 0) are penalized.
    """
    g = durrleman_density(model, k_tau_collocation)
    violation = torch.clamp(-g, min=0.0)
    return (violation ** 2).mean()
