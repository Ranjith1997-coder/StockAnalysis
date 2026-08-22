"""
VolatilityPINN — Physics-Informed Neural Network for the implied total
variance surface w(k, tau) = sigma_imp^2 * tau.

See .kilo/plans/pinn-volatility-engine.md section 5 for the full design
rationale (why Softplus, why log-variance output, why per-symbol models).
"""
from __future__ import annotations

import math

import torch
import torch.nn as nn

from lib.logging_util import get_logger
logger = get_logger("pinn")

# Raw input domain — used to normalize (k, tau) to [-1, 1] before the network
# sees them. Without this, tau's tiny scale (0.003-1.0) vs k's (-2, 2) would
# make the network's gradients dominated by tau alone.
K_RANGE = (-2.0, 2.0)
TAU_RANGE = (0.003, 1.0)


def normalize(k: torch.Tensor, tau: torch.Tensor) -> torch.Tensor:
    """Map raw (k, tau) into [-1, 1] x [-1, 1], stacked as network input.

    Args:
        k: log-moneyness, any shape.
        tau: time to expiry in years, same shape as k.

    Returns:
        Tensor of shape (*k.shape, 2) — ready to feed to VolatilityPINN.
    """
    k_norm = 2 * (k - K_RANGE[0]) / (K_RANGE[1] - K_RANGE[0]) - 1
    tau_norm = 2 * (tau - TAU_RANGE[0]) / (TAU_RANGE[1] - TAU_RANGE[0]) - 1
    return torch.stack([k_norm, tau_norm], dim=-1)


class VolatilityPINN(nn.Module):
    """
    Physics-Informed Neural Network for the implied variance surface.

    Input:  (k, tau) — 2D coordinate space (already normalized to [-1, 1])
    Output: (mu, v_squared) — mean and variance of total implied variance w

    Architecture: narrow + deep + smooth (Softplus) so torch.autograd.grad
    can take exact first AND second derivatives w.r.t. k (needed for the
    calendar and butterfly/Durrleman arbitrage penalties in losses/arbitrage.py).
    ReLU would give a zero second derivative almost everywhere, which would
    make the butterfly penalty a no-op.

    Optional Fourier feature encoding (num_fourier_bands > 0): plain MLPs
    with smooth activations are known to suffer "spectral bias" -- they
    learn low-frequency structure quickly but struggle to represent sharp,
    higher-frequency curvature (Tancik et al. 2020, "Fourier Features Let
    Networks Learn High Frequency Functions in Low Dimensional Domains").
    This shows up here as an over-smoothed wing/skew: ATM dominates the
    training distribution (in real 8-day NIFTY+BANKNIFTY data, ATM was
    ~84% of samples -- see conversation), so gradient descent naturally
    prioritizes fitting the flat, slowly-varying ATM region, leaving the
    wings comparatively flat too, even though the true smile curves there.
    Encoding k as gamma(k) = [sin(2^0 pi k), cos(2^0 pi k), ...,
    sin(2^(L-1) pi k), cos(2^(L-1) pi k)] gives the network direct access
    to higher-frequency basis functions in k, which sin/cos derivatives
    remain perfectly smooth (C-infinity) -- so the calendar/butterfly
    penalties' autograd requirements are unaffected either way.

    Default num_fourier_bands=0 (disabled) reproduces the original
    architecture and parameter count exactly -- this is opt-in, not a
    silent behavior change.
    """

    def __init__(self, hidden_dim: int = 128, num_layers: int = 4, num_fourier_bands: int = 0):
        super().__init__()
        self.num_fourier_bands = num_fourier_bands
        # gamma(k) (2 * num_fourier_bands dims) + tau (1 dim), or plain (k, tau) if disabled.
        input_dim = 2 * num_fourier_bands + 1 if num_fourier_bands > 0 else 2

        layers = [nn.Linear(input_dim, hidden_dim), nn.Softplus()]
        for _ in range(num_layers - 1):
            layers.extend([nn.Linear(hidden_dim, hidden_dim), nn.Softplus()])
        # Output: mean (mu) and raw log-variance (log_v2).
        # log_v2 instead of v2 directly so v2 = exp(log_v2) > 0 is guaranteed
        # without a Softplus on the output, which would squash gradients
        # right when the network needs to express low uncertainty.
        layers.append(nn.Linear(hidden_dim, 2))
        self.net = nn.Sequential(*layers)

        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_normal_(m.weight)
                nn.init.zeros_(m.bias)

    def _fourier_features(self, k_norm: torch.Tensor) -> torch.Tensor:
        """gamma(k) = [sin(2^0 pi k), cos(2^0 pi k), ..., sin(2^(L-1) pi k), cos(2^(L-1) pi k)].

        Applied to the NORMALIZED k (already in [-1, 1], the model's
        existing input contract) rather than raw k -- keeps this purely an
        internal feature transform, with no change needed to normalize()
        or RawInputModel's differentiate-through-normalize plumbing.
        """
        bands = []
        for i in range(self.num_fourier_bands):
            freq = (2.0 ** i) * math.pi
            bands.append(torch.sin(freq * k_norm))
            bands.append(torch.cos(freq * k_norm))
        return torch.cat(bands, dim=-1)

    def forward(self, k_tau: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """Forward pass.

        Args:
            k_tau: (batch, 2) tensor of normalized (k, tau) pairs.

        Returns:
            (mu, v_squared), each (batch, 1). v_squared = exp(log_v2) + 1e-8
            (the floor prevents the NLL loss's 1/v_squared term from
            blowing up if the network ever predicts near-zero variance).
        """
        if self.num_fourier_bands > 0:
            k_norm = k_tau[:, 0:1]
            tau_norm = k_tau[:, 1:2]
            net_input = torch.cat([self._fourier_features(k_norm), tau_norm], dim=-1)
        else:
            net_input = k_tau

        out = self.net(net_input)
        mu = out[:, 0:1]
        log_v2 = out[:, 1:2]
        v_squared = torch.exp(log_v2) + 1e-8
        return mu, v_squared

    def predict_w(self, k_tau: torch.Tensor) -> torch.Tensor:
        """Convenience: return only the mean (fair-value total variance w)."""
        return self.forward(k_tau)[0]


class RawInputModel:
    """Wraps a VolatilityPINN so it can be called directly on RAW (real-domain,
    unnormalized) (k, tau) pairs -- e.g. from data/collocation.py's
    sample_collocation(), which deliberately produces raw k in [-2, 2] and
    raw tau in [0.003, 1.0], not normalized values.

    Why this exists: the arbitrage penalties (losses/arbitrage.py) need
    derivatives w.r.t. the TRUE physical k -- Durrleman's g(k) formula and
    the calendar condition dw/dtau are both defined in terms of real
    log-moneyness and real time, not the network's internal [-1,1] encoding.
    But the network itself must only ever be evaluated on normalized input
    (that's what it's trained on -- see normalize()'s docstring). This
    wrapper normalizes on every call, so torch.autograd.grad(..., k_tau_raw)
    correctly differentiates through the normalize() step via the chain rule
    -- giving physically-correct derivatives w.r.t. real k/tau -- while the
    underlying network still only ever sees properly-scaled input.

    Presents the same (k_tau) -> (mu, v_squared) calling interface as
    VolatilityPINN itself, so it's a drop-in replacement anywhere a "model"
    is expected to accept raw input directly (calendar_penalty,
    butterfly_penalty, durrleman_density).
    """

    def __init__(self, model: VolatilityPINN):
        self.model = model

    def __call__(self, k_tau_raw: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        k_tau_norm = normalize(k_tau_raw[:, 0], k_tau_raw[:, 1])
        return self.model(k_tau_norm)

    def parameters(self):
        return self.model.parameters()
