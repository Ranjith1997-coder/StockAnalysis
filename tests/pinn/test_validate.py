"""Tests for tools/pinn_volatility/training/validate.py — walk-forward
holdout evaluation (the start of Step 9)."""

import math
import torch
import torch.nn as nn
import pytest

from tools.pinn_volatility.model.pinn import VolatilityPINN
import tools.pinn_volatility.training.validate as validate_module
from tools.pinn_volatility.training.validate import (
    evaluate_holdout, HoldoutMetrics, audit_arbitrage, ArbitrageAudit,
)


class _SyntheticSurface(nn.Module):
    """w(k, tau) = a*tau + b*k^2 + c -- mirrors test_losses.py's fixture
    (duplicated here to keep this file self-contained). Trivial closed-form
    derivatives (w'=2bk, w''=2b, dw/dtau=a) let tests assert exact expected
    audit values instead of just "runs without crashing"."""

    def __init__(self, a=0.5, b=0.3, c=0.5, v2=0.01):
        super().__init__()
        self.a = nn.Parameter(torch.tensor(a))
        self.b = nn.Parameter(torch.tensor(b))
        self.c = nn.Parameter(torch.tensor(c))
        self.v2 = v2

    def forward(self, k_tau):
        k = k_tau[:, 0:1]
        tau = k_tau[:, 1:2]
        w = self.a * tau + self.b * k ** 2 + self.c
        return w, torch.full_like(w, self.v2)


class _PassthroughWrap:
    """Test double standing in for RawInputModel, WITHOUT its normalize()
    step -- lets these tests feed _SyntheticSurface truly raw (k, tau) and
    hand-verify against the same closed-form parameter combinations already
    empirically verified valid/invalid in test_losses.py (RawInputModel's
    fixed global normalize() would otherwise silently remap the domain,
    invalidating those already-checked parameter choices)."""

    def __init__(self, model):
        self.model = model

    def __call__(self, k_tau):
        return self.model(k_tau)

    def parameters(self):
        return self.model.parameters()


class _FakeModel:
    """Deterministic stand-in: always predicts a fixed w, regardless of
    input. Lets tests hand-verify exact RMSE/MAE values instead of relying
    on a real (stochastic) trained network."""

    def __init__(self, w_pred_value: float, v2_value: float = 0.01):
        self.w_pred_value = w_pred_value
        self.v2_value = v2_value

    def eval(self):
        pass

    def __call__(self, k_tau_norm):
        n = k_tau_norm.shape[0]
        mu = torch.full((n, 1), self.w_pred_value)
        v2 = torch.full((n, 1), self.v2_value)
        return mu, v2


class TestEvaluateHoldout:
    def test_perfect_prediction_gives_zero_error(self):
        tau = torch.tensor([0.1, 0.1, 0.1])
        k = torch.tensor([0.0, 0.05, -0.05])
        w_pred_value = 0.02
        sigma_actual = torch.sqrt(torch.full((3,), w_pred_value) / tau)
        w_actual = torch.full((3,), w_pred_value)

        model = _FakeModel(w_pred_value)
        metrics = evaluate_holdout(model, k, tau, w_actual, sigma_actual)

        assert metrics.rmse_w == pytest.approx(0.0, abs=1e-6)
        assert metrics.mae_sigma == pytest.approx(0.0, abs=1e-6)
        assert metrics.mean_bias_sigma == pytest.approx(0.0, abs=1e-6)

    def test_known_rmse_for_constant_offset(self):
        tau = torch.tensor([0.1, 0.1])
        k = torch.tensor([0.0, 0.0])
        w_pred_value = 0.02
        offset = 0.005
        w_actual = torch.full((2,), w_pred_value + offset)
        sigma_actual = torch.sqrt(w_actual / tau)

        model = _FakeModel(w_pred_value)
        metrics = evaluate_holdout(model, k, tau, w_actual, sigma_actual)

        assert metrics.rmse_w == pytest.approx(offset, abs=1e-6)

    def test_bias_sign_positive_when_model_underpredicts_vol(self):
        """mean_bias_sigma > 0 should mean actual > predicted (model is
        too conservative / underpredicting vol)."""
        tau = torch.tensor([0.1, 0.1])
        k = torch.tensor([0.0, 0.0])
        w_pred_value = 0.01  # low predicted variance
        w_actual = torch.tensor([0.03, 0.03])  # actual vol is higher
        sigma_actual = torch.sqrt(w_actual / tau)

        model = _FakeModel(w_pred_value)
        metrics = evaluate_holdout(model, k, tau, w_actual, sigma_actual)

        assert metrics.mean_bias_sigma > 0

    def test_moneyness_breakdown_separates_atm_and_wings(self):
        # 2 ATM samples (|k|<0.1) with zero error, 2 wing samples (|k|>=0.1) with error.
        k = torch.tensor([0.0, 0.05, 0.5, -0.6])
        tau = torch.tensor([0.1, 0.1, 0.1, 0.1])
        w_pred_value = 0.02
        w_actual = torch.tensor([0.02, 0.02, 0.03, 0.03])  # first 2 match exactly, last 2 don't
        sigma_actual = torch.sqrt(w_actual / tau)

        model = _FakeModel(w_pred_value)
        metrics = evaluate_holdout(model, k, tau, w_actual, sigma_actual, atm_threshold=0.1)

        assert metrics.mae_sigma_by_moneyness["atm"] == pytest.approx(0.0, abs=1e-6)
        assert metrics.mae_sigma_by_moneyness["wings"] > 0

    def test_empty_bucket_returns_none(self):
        # All samples ATM -> "wings" bucket has nothing in it.
        k = torch.tensor([0.0, 0.02, -0.03])
        tau = torch.tensor([0.1, 0.1, 0.1])
        w_actual = torch.tensor([0.02, 0.02, 0.02])
        sigma_actual = torch.sqrt(w_actual / tau)

        model = _FakeModel(0.02)
        metrics = evaluate_holdout(model, k, tau, w_actual, sigma_actual, atm_threshold=0.1)

        assert metrics.mae_sigma_by_moneyness["wings"] is None
        assert metrics.mae_sigma_by_moneyness["atm"] is not None

    def test_n_samples_matches_input_length(self):
        k = torch.tensor([0.0, 0.1, 0.2, -0.1, -0.2])
        tau = torch.full((5,), 0.1)
        w_actual = torch.full((5,), 0.02)
        sigma_actual = torch.sqrt(w_actual / tau)

        model = _FakeModel(0.02)
        metrics = evaluate_holdout(model, k, tau, w_actual, sigma_actual)

        assert metrics.n_samples == 5

    def test_works_with_real_pinn_model(self):
        """Sanity check against the real (randomly initialized) network --
        just confirms shapes/finiteness, not specific accuracy values."""
        model = VolatilityPINN()
        k = torch.linspace(-1.0, 1.0, 20)
        tau = torch.full((20,), 0.1)
        w_actual = torch.full((20,), 0.02)
        sigma_actual = torch.sqrt(w_actual / tau)

        metrics = evaluate_holdout(model, k, tau, w_actual, sigma_actual)

        assert isinstance(metrics, HoldoutMetrics)
        assert metrics.n_samples == 20
        assert math.isfinite(metrics.rmse_w)
        assert math.isfinite(metrics.mae_sigma)


class TestAuditArbitrage:
    """Uses _PassthroughWrap (monkeypatched in for RawInputModel) so
    _SyntheticSurface receives truly raw (k, tau) -- matching the exact
    parameter combinations already empirically verified valid/invalid in
    test_losses.py's TestButterflyPenalty/TestCalendarPenalty."""

    def test_valid_surface_no_violations(self, monkeypatch):
        monkeypatch.setattr(validate_module, "RawInputModel", _PassthroughWrap)
        # a=0.5,b=0.3,c=0.5 empirically verified (test_losses.py): g(k) >= ~0.10
        # across k in [-2,2] at tau=0.1 -- valid, no butterfly violation.
        surface = _SyntheticSurface(a=0.5, b=0.3, c=0.5)

        audit = audit_arbitrage(surface, n_points=2000, k_range=(-2.0, 2.0),
                                 tau_range=(0.09, 0.11), seed=0)

        assert isinstance(audit, ArbitrageAudit)
        assert audit.min_g > 0
        assert audit.min_calendar_slope == pytest.approx(0.5, abs=1e-3)  # dw/dtau = a, exactly, everywhere
        assert audit.butterfly_violation_rate == 0.0
        assert audit.calendar_violation_rate == 0.0
        assert audit.max_butterfly_violation == 0.0
        assert audit.max_calendar_violation == 0.0

    def test_invalid_surface_butterfly_violation_detected(self, monkeypatch):
        monkeypatch.setattr(validate_module, "RawInputModel", _PassthroughWrap)
        # a=0.5,b=1.5,c=0.3 empirically verified (test_losses.py): min g(k)
        # as low as ~-2.2 at tau=0.1 -- a genuine, deliberate violation.
        surface = _SyntheticSurface(a=0.5, b=1.5, c=0.3)

        audit = audit_arbitrage(surface, n_points=2000, k_range=(-2.0, 2.0),
                                 tau_range=(0.09, 0.11), seed=0)

        assert audit.min_g < 0
        assert audit.butterfly_violation_rate > 0
        assert audit.max_butterfly_violation > 0
        assert audit.max_butterfly_violation == pytest.approx(-audit.min_g, abs=1e-4)

    def test_invalid_surface_calendar_violation_detected(self, monkeypatch):
        monkeypatch.setattr(validate_module, "RawInputModel", _PassthroughWrap)
        # a<0 -> dw/dtau = a < 0 everywhere -> every point violates.
        surface = _SyntheticSurface(a=-0.3, b=0.3, c=0.5)

        audit = audit_arbitrage(surface, n_points=500, k_range=(-1.0, 1.0),
                                 tau_range=(0.05, 0.15), seed=0)

        assert audit.min_calendar_slope == pytest.approx(-0.3, abs=1e-3)
        assert audit.calendar_violation_rate == pytest.approx(1.0, abs=1e-6)
        assert audit.max_calendar_violation == pytest.approx(0.3, abs=1e-3)

    def test_runs_on_real_pinn_model(self):
        """No monkeypatch here -- sanity check against the actual
        RawInputModel-normalized path with a real VolatilityPINN."""
        model = VolatilityPINN()
        audit = audit_arbitrage(model, n_points=500, seed=0)

        assert isinstance(audit, ArbitrageAudit)
        assert audit.n_points == 500
        assert math.isfinite(audit.min_g)
        assert math.isfinite(audit.min_calendar_slope)
        assert 0.0 <= audit.butterfly_violation_rate <= 1.0
        assert 0.0 <= audit.calendar_violation_rate <= 1.0

    def test_reproducible_with_seed(self):
        model = VolatilityPINN()
        audit1 = audit_arbitrage(model, n_points=200, seed=7)
        audit2 = audit_arbitrage(model, n_points=200, seed=7)
        assert audit1.min_g == audit2.min_g
        assert audit1.min_calendar_slope == audit2.min_calendar_slope

    def test_restores_original_training_mode(self):
        """Must not silently leave the model in eval() mode if it was
        training when called (e.g. mid-training diagnostic use)."""
        model = VolatilityPINN()
        model.train()
        audit_arbitrage(model, n_points=100, seed=0)
        assert model.training is True
