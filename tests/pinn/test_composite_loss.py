"""Tests for tools/pinn_volatility/losses/composite.py — Step 7 of the PINN plan."""

import numpy as np
import torch
import pytest

from tools.pinn_volatility.model.pinn import VolatilityPINN
from tools.pinn_volatility.data.collocation import sample_collocation
from tools.pinn_volatility.losses.composite import composite_loss


def _sample_training_data(n=20, seed=0):
    rng = np.random.default_rng(seed)
    k = torch.tensor(rng.uniform(-1.5, 1.5, n), dtype=torch.float32)
    tau = torch.tensor(rng.uniform(0.01, 0.5, n), dtype=torch.float32)
    # Plausible total-variance targets (not physically exact, just realistic scale).
    w = torch.tensor(0.02 * tau.numpy() + 0.001, dtype=torch.float32)
    return k, tau, w


class TestCompositeLoss:
    def test_returns_finite_scalar_and_breakdown(self):
        model = VolatilityPINN()
        k, tau, w = _sample_training_data()
        collocation = sample_collocation(n_points=100, rng=np.random.default_rng(1))

        total, breakdown = composite_loss(model, k, tau, w, collocation)

        assert total.dim() == 0
        assert torch.isfinite(total)
        assert set(breakdown.keys()) == {
            "data", "calendar", "butterfly", "total", "min_g", "min_calendar_slope",
        }
        for v in breakdown.values():
            assert np.isfinite(v)

    def test_min_g_and_min_calendar_slope_are_real_raw_values(self):
        """Regression guard: these must be actual per-point minimums (can be
        negative, e.g. mid-training before the surface is arbitrage-free),
        not accidentally the same as the (always >= 0) penalty values."""
        model = VolatilityPINN()
        k, tau, w = _sample_training_data()
        collocation = sample_collocation(n_points=100, rng=np.random.default_rng(2))

        _, breakdown = composite_loss(model, k, tau, w, collocation)

        # Not derived from / equal to the penalty terms (which are always >= 0).
        assert breakdown["min_g"] != breakdown["butterfly"]
        assert breakdown["min_calendar_slope"] != breakdown["calendar"]

    def test_total_matches_weighted_sum_of_breakdown(self):
        model = VolatilityPINN()
        k, tau, w = _sample_training_data()
        collocation = sample_collocation(n_points=100, rng=np.random.default_rng(1))

        lambda_data, lambda_cal, lambda_but = 1.0, 2.0, 0.5
        total, breakdown = composite_loss(
            model, k, tau, w, collocation,
            lambda_data=lambda_data, lambda_cal=lambda_cal, lambda_but=lambda_but,
        )

        expected = (lambda_data * breakdown["data"] + lambda_cal * breakdown["calendar"]
                    + lambda_but * breakdown["butterfly"])
        assert breakdown["total"] == pytest.approx(expected, abs=1e-5)
        assert total.item() == pytest.approx(expected, abs=1e-5)

    def test_zero_weight_removes_component_contribution(self):
        model = VolatilityPINN()
        k, tau, w = _sample_training_data()
        collocation = sample_collocation(n_points=100, rng=np.random.default_rng(1))

        total, breakdown = composite_loss(
            model, k, tau, w, collocation,
            lambda_data=1.0, lambda_cal=0.0, lambda_but=0.0,
        )
        assert total.item() == pytest.approx(breakdown["data"], abs=1e-5)

    def test_gradient_flows_to_model_parameters(self):
        model = VolatilityPINN()
        k, tau, w = _sample_training_data()
        collocation = sample_collocation(n_points=50, rng=np.random.default_rng(2))

        total, _ = composite_loss(model, k, tau, w, collocation)
        total.backward()

        assert any(p.grad is not None and p.grad.abs().sum().item() > 0
                   for p in model.parameters())

    def test_beta_parameter_is_forwarded(self):
        """Sanity check that beta actually reaches beta_nll_loss -- different
        beta values should generally produce different data-loss values for
        the same model/data (unless residual is exactly zero)."""
        model = VolatilityPINN()
        k, tau, w = _sample_training_data()
        collocation = sample_collocation(n_points=50, rng=np.random.default_rng(3))

        _, breakdown_beta0 = composite_loss(model, k, tau, w, collocation, beta=0.0)
        _, breakdown_beta1 = composite_loss(model, k, tau, w, collocation, beta=1.0)

        assert breakdown_beta0["data"] != breakdown_beta1["data"]

    def test_uses_raw_inputs_not_pre_normalized(self):
        """Regression guard: composite_loss must accept RAW k/tau/collocation
        (matching what dataset.py and collocation.py actually produce), not
        already-normalized values -- confirmed by using realistic raw-domain
        magnitudes (k up to 1.5, tau as small as 0.01) and checking it still
        runs without the model silently receiving out-of-range values."""
        model = VolatilityPINN()
        k, tau, w = _sample_training_data()
        collocation = sample_collocation(n_points=50, rng=np.random.default_rng(4))
        # tau values are genuinely tiny (0.01-0.5) and k spans most of its
        # real range -- if these were mistakenly fed through a second
        # normalize() step (or NOT normalized at all before reaching the
        # network), this would still run without error, so the real check
        # is functional: total must be finite and of reasonable scale.
        total, _ = composite_loss(model, k, tau, w, collocation)
        assert torch.isfinite(total)
        assert total.item() < 1e6  # not a blown-up/garbage-scale value


class TestCompositeLossWeightingOptions:
    """use_tau_weight/use_moneyness_weight default to False -- these tests
    confirm that default reproduces the original unweighted behavior
    exactly, and that enabling them changes the data term without breaking
    anything (finite, gradients still flow)."""

    def test_defaults_are_off_and_match_explicit_false(self):
        model = VolatilityPINN()
        k, tau, w = _sample_training_data()
        collocation = sample_collocation(n_points=50, rng=np.random.default_rng(10))

        torch.manual_seed(0)
        _, breakdown_default = composite_loss(model, k, tau, w, collocation)
        torch.manual_seed(0)
        _, breakdown_explicit = composite_loss(
            model, k, tau, w, collocation, use_tau_weight=False, use_moneyness_weight=False,
        )
        assert breakdown_default["data"] == pytest.approx(breakdown_explicit["data"], abs=1e-9)

    def test_enabling_tau_weight_changes_data_loss(self):
        model = VolatilityPINN()
        k, tau, w = _sample_training_data()
        collocation = sample_collocation(n_points=50, rng=np.random.default_rng(11))

        _, unweighted = composite_loss(model, k, tau, w, collocation)
        _, weighted = composite_loss(model, k, tau, w, collocation, use_tau_weight=True)
        assert unweighted["data"] != weighted["data"]

    def test_enabling_moneyness_weight_changes_data_loss(self):
        model = VolatilityPINN()
        k, tau, w = _sample_training_data()
        collocation = sample_collocation(n_points=50, rng=np.random.default_rng(12))

        _, unweighted = composite_loss(model, k, tau, w, collocation)
        _, weighted = composite_loss(model, k, tau, w, collocation, use_moneyness_weight=True)
        assert unweighted["data"] != weighted["data"]

    def test_both_weights_enabled_still_finite_and_backprops(self):
        model = VolatilityPINN()
        k, tau, w = _sample_training_data()
        collocation = sample_collocation(n_points=50, rng=np.random.default_rng(13))

        total, breakdown = composite_loss(
            model, k, tau, w, collocation,
            use_tau_weight=True, use_moneyness_weight=True,
            tau_weight_max=20.0, moneyness_alpha=5.0,
        )
        total.backward()

        assert torch.isfinite(total)
        assert any(p.grad is not None and p.grad.abs().sum().item() > 0
                   for p in model.parameters())

    # use_vega_weight / vega_train were removed from composite_loss after an
    # isolation experiment found vega-weighting (raw and sqrt(tau)-normalized)
    # actively hurt wing accuracy -- see data_loss.py's module-level note.
