"""Tests for tools/pinn_volatility/training/validate.py — walk-forward
holdout evaluation (the start of Step 9)."""

import math
import torch
import pytest

from tools.pinn_volatility.model.pinn import VolatilityPINN
from tools.pinn_volatility.training.validate import evaluate_holdout, HoldoutMetrics


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
