"""Tests for tools/pinn_volatility/losses/ — Step 6 of the PINN plan.

Uses a small closed-form "SyntheticSurface" (w = a*tau + b*k^2 + c) instead of
a real trained VolatilityPINN wherever exact values matter -- its derivatives
are trivial to hand-derive (w' = 2bk, w'' = 2b), which lets these tests check
autograd correctness directly against known closed-form answers rather than
just "the code runs without crashing."
"""

import math
import torch
import torch.nn as nn
import pytest

from tools.pinn_volatility.model.pinn import VolatilityPINN
from tools.pinn_volatility.losses.data_loss import beta_nll_loss, tau_weight, moneyness_weight
from tools.pinn_volatility.losses.arbitrage import (
    calendar_penalty, butterfly_penalty, durrleman_density, calendar_slope,
)


class SyntheticSurface(nn.Module):
    """w(k, tau) = a*tau + b*k^2 + c, with constant variance. Exposes the
    same (mu, v_squared) forward interface as VolatilityPINN so the loss
    functions under test can't tell the difference."""

    def __init__(self, a=0.5, b=0.3, c=0.5, v2=0.01):
        super().__init__()
        # Real parameters (not buffers) so autograd treats them as leaves,
        # matching how a real model's weights participate in the graph.
        self.a = nn.Parameter(torch.tensor(a))
        self.b = nn.Parameter(torch.tensor(b))
        self.c = nn.Parameter(torch.tensor(c))
        self.v2 = v2

    def forward(self, k_tau):
        k = k_tau[:, 0:1]
        tau = k_tau[:, 1:2]
        w = self.a * tau + self.b * k ** 2 + self.c
        v_squared = torch.full_like(w, self.v2)
        return w, v_squared


def _grid(k_lo=-2.0, k_hi=2.0, n=21, tau=0.1):
    ks = torch.linspace(k_lo, k_hi, n)
    taus = torch.full_like(ks, tau)
    return torch.stack([ks, taus], dim=1)


class TestBetaNllLoss:
    def test_beta_zero_matches_standard_gaussian_nll(self):
        mu = torch.tensor([[0.1], [0.2]])
        v2 = torch.tensor([[0.02], [0.03]])
        target = torch.tensor([0.15, 0.18])

        loss = beta_nll_loss(mu, v2, target, beta=0.0)
        expected = (0.5 * torch.log(v2) + 0.5 * (target.reshape(mu.shape) - mu) ** 2 / v2).mean()
        assert loss.item() == pytest.approx(expected.item(), abs=1e-6)

    def test_gradient_flows_to_variance_parameters(self):
        """Regression test for the plan's pseudocode bug: the variance head
        must receive a non-None, non-zero gradient. With the plan's literal
        (fully-detached) version, raw_v2.grad is None -- confirmed manually
        before writing this fix."""
        raw_v2 = torch.nn.Parameter(torch.tensor([[0.0], [0.5]]))
        mu = torch.tensor([[0.1], [0.2]])
        target = torch.tensor([0.15, 0.5])  # residuals chosen to be non-trivial

        v_squared = torch.exp(raw_v2) + 1e-8
        loss = beta_nll_loss(mu, v_squared, target, beta=0.5)
        loss.backward()

        assert raw_v2.grad is not None
        assert not torch.allclose(raw_v2.grad, torch.zeros_like(raw_v2.grad))

    def test_mu_gradient_matches_residual_direction(self):
        mu = torch.nn.Parameter(torch.tensor([[0.1]]))
        v2 = torch.tensor([[0.05]])
        target = torch.tensor([0.2])  # target > mu

        loss = beta_nll_loss(mu, v2, target, beta=0.5)
        loss.backward()
        # d/dmu of 0.5*(target-mu)^2/v2 = -(target-mu)/v2 < 0 when target > mu
        assert mu.grad.item() < 0

    def test_loss_is_finite_scalar(self):
        mu = torch.randn(10, 1)
        v2 = torch.rand(10, 1) + 0.01
        target = torch.randn(10)
        loss = beta_nll_loss(mu, v2, target, beta=0.5)
        assert loss.dim() == 0
        assert torch.isfinite(loss)

    def test_beta_one_weight_cancels_variance_scale_in_forward_value(self):
        """At beta=1, weight == v_squared (numerically, forward pass only),
        so loss = v2*(0.5*log(v2)) + 0.5*(target-mu)^2 -- the residual term
        loses its 1/v2 scaling, matching the plan's 'beta=1: pure MSE' claim
        for the mean-prediction term."""
        mu = torch.tensor([[0.3]])
        v2 = torch.tensor([[0.02]])
        target = torch.tensor([0.5])

        loss = beta_nll_loss(mu, v2, target, beta=1.0)
        expected = (v2 * 0.5 * torch.log(v2) + 0.5 * (target.reshape(mu.shape) - mu) ** 2).mean()
        assert loss.item() == pytest.approx(expected.item(), abs=1e-6)


class TestCalendarSlope:
    def test_matches_hand_derived_closed_form(self):
        """w = a*tau + b*k^2 + c -> dw/dtau = a exactly, everywhere,
        independent of k -- trivial closed form to check autograd against."""
        a, b, c = 0.35, 0.3, 0.5
        surface = SyntheticSurface(a=a, b=b, c=c)
        k_tau = _grid(n=15, tau=0.2)
        slope = calendar_slope(surface, k_tau)
        assert torch.allclose(slope, torch.full_like(slope, a), atol=1e-5)

    def test_shape_matches_durrleman_density_convention(self):
        surface = SyntheticSurface()
        k_tau = _grid(n=10)
        slope = calendar_slope(surface, k_tau)
        assert slope.shape == (10,)

    def test_calendar_penalty_uses_same_slope_values(self):
        """calendar_penalty must be exactly derivable from calendar_slope's
        raw values -- regression guard for the refactor that extracted
        calendar_slope out of calendar_penalty's body."""
        surface = SyntheticSurface(a=-0.3, b=0.3, c=0.5)
        k_tau = _grid()
        slope = calendar_slope(surface, k_tau)
        expected_penalty = (torch.clamp(-slope, min=0.0) ** 2).mean()
        actual_penalty = calendar_penalty(surface, k_tau)
        assert actual_penalty.item() == pytest.approx(expected_penalty.item(), abs=1e-6)


class TestCalendarPenalty:
    def test_no_violation_when_monotonic_increasing(self):
        """a=0.5 > 0 -> dw/dtau = 0.5 everywhere -> zero violation, exactly."""
        surface = SyntheticSurface(a=0.5, b=0.3, c=0.5)
        k_tau = _grid()
        penalty = calendar_penalty(surface, k_tau)
        assert penalty.item() == pytest.approx(0.0, abs=1e-6)

    def test_violation_when_decreasing_in_tau(self):
        """a=-0.5 -> dw/dtau = -0.5 everywhere -> violation = 0.5 exactly,
        so penalty = mean(0.5^2) = 0.25 exactly, deterministic."""
        surface = SyntheticSurface(a=-0.5, b=0.3, c=0.5)
        k_tau = _grid()
        penalty = calendar_penalty(surface, k_tau)
        assert penalty.item() == pytest.approx(0.25, abs=1e-6)

    def test_penalty_gradient_reaches_model_params(self):
        surface = SyntheticSurface(a=-0.2, b=0.3, c=0.5)
        k_tau = _grid()
        penalty = calendar_penalty(surface, k_tau)
        penalty.backward()
        assert surface.a.grad is not None
        assert surface.a.grad.item() != 0.0


class TestDurrlemanDensity:
    def test_matches_hand_derived_closed_form(self):
        """w = a*tau + b*k^2 + c has trivial closed-form derivatives:
        w' = 2bk, w'' = 2b. Plug those into the g(k) formula by hand (plain
        Python floats, no autograd) and compare against durrleman_density's
        autograd-computed result -- this is the direct check that autograd
        is correctly wired through the model."""
        a, b, c, tau = 0.5, 0.3, 0.5, 0.1
        surface = SyntheticSurface(a=a, b=b, c=c)
        k_tau = _grid(n=15, tau=tau)

        g_autograd = durrleman_density(surface, k_tau)

        for i, k_tau_row in enumerate(k_tau):
            k = k_tau_row[0].item()
            w = a * tau + b * k ** 2 + c
            w_prime = 2 * b * k
            w_double_prime = 2 * b
            g_hand = (1 - k * w_prime / (2 * w)) ** 2 - (w_prime ** 2 / 4) * (1 / w + 0.25) + w_double_prime / 2
            assert g_autograd[i].item() == pytest.approx(g_hand, abs=1e-4)


class TestButterflyPenalty:
    def test_valid_surface_near_zero_penalty(self):
        """Empirically verified (see conversation): a=0.5, b=0.3, c=0.5 keeps
        g(k) >= ~0.10 across k in [-2, 2] at tau=0.1 -- no violation anywhere."""
        surface = SyntheticSurface(a=0.5, b=0.3, c=0.5)
        k_tau = _grid(n=21, tau=0.1)
        penalty = butterfly_penalty(surface, k_tau)
        assert penalty.item() == pytest.approx(0.0, abs=1e-6)

    def test_invalid_surface_positive_penalty(self):
        """Empirically verified: a=0.5, b=1.5, c=0.3 produces g(k) as low as
        ~-2.2 at tau=0.1 (curvature too sharp relative to the surface's
        level -- a genuine, deliberately-constructed arbitrage violation)."""
        surface = SyntheticSurface(a=0.5, b=1.5, c=0.3)
        k_tau = _grid(n=21, tau=0.1)
        penalty = butterfly_penalty(surface, k_tau)
        assert penalty.item() > 0.0

    def test_penalty_gradient_reaches_model_params(self):
        surface = SyntheticSurface(a=0.5, b=1.5, c=0.3)
        k_tau = _grid(n=21, tau=0.1)
        penalty = butterfly_penalty(surface, k_tau)
        penalty.backward()
        assert surface.b.grad is not None
        assert surface.b.grad.item() != 0.0


class TestLossesWithRealModel:
    """Sanity check: the loss functions also run cleanly against the actual
    VolatilityPINN (not just the synthetic surface), since that's how
    they'll really be used in the trainer (Step 8)."""

    def test_calendar_penalty_runs_on_real_model(self):
        model = VolatilityPINN()
        k_tau = _grid()
        penalty = calendar_penalty(model, k_tau)
        assert torch.isfinite(penalty)
        assert penalty.item() >= 0.0

    def test_butterfly_penalty_runs_on_real_model(self):
        model = VolatilityPINN()
        k_tau = _grid()
        penalty = butterfly_penalty(model, k_tau)
        assert torch.isfinite(penalty)
        assert penalty.item() >= 0.0

    def test_beta_nll_runs_on_real_model_output(self):
        model = VolatilityPINN()
        k_tau = _grid(n=10)
        mu, v2 = model(k_tau)
        target = torch.rand(10) * 0.1
        loss = beta_nll_loss(mu, v2, target, beta=0.5)
        assert torch.isfinite(loss)


class TestTauWeight:
    def test_larger_for_shorter_dated_tau(self):
        """Uses tau values that stay below the default cap (max_weight=20
        kicks in below tau~0.22 -- see test_all_short_dated_collapse_to_cap
        below) so this tests the formula's own monotonicity, not the cap."""
        tau = torch.tensor([0.3, 0.5, 1.0])
        w = tau_weight(tau)
        assert w[0] > w[1] > w[2]

    def test_all_short_dated_collapse_to_cap(self):
        """Real property worth being explicit about: with the default cap
        (20.0), every tau below ~0.22 -- which covers essentially all
        weekly options, exactly the case this weight targets -- collapses
        to the SAME weight. The cap makes this mostly a binary
        short-dated-vs-not boost, not a fine-grained one, within that
        range."""
        tau = torch.tensor([0.003, 0.01, 0.05, 0.1, 0.2])
        w = tau_weight(tau, max_weight=20.0)
        assert torch.all(w == 20.0)

    def test_capped_at_max_weight(self):
        """Uncapped 1/tau^2 at tau=0.003 (plan's own minimum) would be
        ~9174x -- must be clamped, not allowed through raw."""
        tau = torch.tensor([0.003])
        w = tau_weight(tau, max_weight=20.0)
        assert w.item() == pytest.approx(20.0, abs=1e-6)

    def test_uncapped_below_max_matches_formula(self):
        tau = torch.tensor([0.5])
        floor = 1e-4
        expected = 1.0 / (0.5 ** 2 + floor)
        w = tau_weight(tau, floor=floor, max_weight=1000.0)
        assert w.item() == pytest.approx(expected, rel=1e-5)

    def test_always_positive(self):
        tau = torch.tensor([0.003, 0.01, 0.1, 1.0])
        w = tau_weight(tau)
        assert torch.all(w > 0)


class TestMoneynessWeight:
    def test_atm_weight_is_one(self):
        k = torch.tensor([0.0])
        w = moneyness_weight(k, alpha=5.0)
        assert w.item() == pytest.approx(1.0, abs=1e-6)

    def test_wing_weight_larger_than_atm(self):
        k = torch.tensor([0.0, 0.3, 0.6])
        w = moneyness_weight(k, alpha=5.0)
        assert w[0] < w[1] < w[2]

    def test_matches_formula(self):
        k = torch.tensor([0.5])
        alpha = 5.0
        expected = 1.0 + alpha * 0.25
        w = moneyness_weight(k, alpha=alpha)
        assert w.item() == pytest.approx(expected, abs=1e-6)

    def test_symmetric_in_k(self):
        """Wings on both sides (CE and PE) should be weighted equally --
        k^2 is symmetric regardless of sign."""
        w_pos = moneyness_weight(torch.tensor([0.4]))
        w_neg = moneyness_weight(torch.tensor([-0.4]))
        assert w_pos.item() == pytest.approx(w_neg.item())


# vega_weight() was removed from data_loss.py after an isolation experiment
# found it actively hurt wing accuracy (both raw and sqrt(tau)-normalized
# variants) -- see data_loss.py's module-level note and conversation
# history on feature/pinn-volatility-engine. Its test coverage is removed
# along with it; bs_utils.vega() itself is untouched and still tested in
# test_bs_utils.py.


class TestBetaNllLossSampleWeights:
    def test_none_reproduces_unweighted_behavior_exactly(self):
        mu = torch.tensor([[0.1], [0.2], [0.3]])
        v2 = torch.tensor([[0.02], [0.03], [0.01]])
        target = torch.tensor([0.15, 0.18, 0.25])

        unweighted = beta_nll_loss(mu, v2, target, beta=0.5)
        explicit_none = beta_nll_loss(mu, v2, target, beta=0.5, sample_weights=None)
        assert unweighted.item() == pytest.approx(explicit_none.item(), abs=1e-9)

    def test_uniform_weights_reproduce_unweighted_behavior(self):
        """All-equal weights should be a no-op after the internal
        mean-1.0 renormalization."""
        mu = torch.tensor([[0.1], [0.2], [0.3]])
        v2 = torch.tensor([[0.02], [0.03], [0.01]])
        target = torch.tensor([0.15, 0.18, 0.25])

        unweighted = beta_nll_loss(mu, v2, target, beta=0.5)
        uniform_weighted = beta_nll_loss(mu, v2, target, beta=0.5,
                                          sample_weights=torch.full((3,), 7.0))
        assert unweighted.item() == pytest.approx(uniform_weighted.item(), abs=1e-5)

    def test_upweighting_a_sample_increases_its_relative_influence(self):
        """Heavily upweighting the worst-fit sample should increase the
        total loss relative to the unweighted case (that sample's large
        residual now dominates the mean)."""
        mu = torch.tensor([[0.1], [0.1], [0.1]])
        v2 = torch.tensor([[0.02], [0.02], [0.02]])
        target = torch.tensor([0.1, 0.1, 0.5])  # 3rd sample is a big outlier

        unweighted = beta_nll_loss(mu, v2, target, beta=0.5)
        weighted = beta_nll_loss(mu, v2, target, beta=0.5,
                                  sample_weights=torch.tensor([1.0, 1.0, 100.0]))
        assert weighted.item() > unweighted.item()

    def test_gradient_still_flows_to_variance_with_weights(self):
        """Regression guard: sample_weights must not accidentally break the
        beta-NLL variance-gradient fix from earlier in this file."""
        raw_v2 = torch.nn.Parameter(torch.tensor([[0.0], [0.5]]))
        mu = torch.tensor([[0.1], [0.2]])
        target = torch.tensor([0.15, 0.5])
        v_squared = torch.exp(raw_v2) + 1e-8

        loss = beta_nll_loss(mu, v_squared, target, beta=0.5,
                              sample_weights=torch.tensor([2.0, 5.0]))
        loss.backward()

        assert raw_v2.grad is not None
        assert not torch.allclose(raw_v2.grad, torch.zeros_like(raw_v2.grad))
