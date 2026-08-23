"""Tests for tools/pinn_volatility/model/pinn.py — Step 5 of the PINN plan."""

import math
import numpy as np
import torch
import pytest

from tools.pinn_volatility.model.pinn import VolatilityPINN, RawInputModel, normalize, K_RANGE, TAU_RANGE


class TestNormalize:
    def test_range_endpoints_map_to_unit_interval(self):
        k = torch.tensor([K_RANGE[0], K_RANGE[1]])
        tau = torch.tensor([TAU_RANGE[0], TAU_RANGE[1]])
        out = normalize(k, tau)
        assert out[0, 0].item() == pytest.approx(-1.0, abs=1e-6)
        assert out[1, 0].item() == pytest.approx(1.0, abs=1e-6)
        assert out[0, 1].item() == pytest.approx(-1.0, abs=1e-6)
        assert out[1, 1].item() == pytest.approx(1.0, abs=1e-6)

    def test_midpoint_maps_to_zero(self):
        k_mid = (K_RANGE[0] + K_RANGE[1]) / 2
        tau_mid = (TAU_RANGE[0] + TAU_RANGE[1]) / 2
        out = normalize(torch.tensor([k_mid]), torch.tensor([tau_mid]))
        assert out[0, 0].item() == pytest.approx(0.0, abs=1e-6)
        assert out[0, 1].item() == pytest.approx(0.0, abs=1e-6)

    def test_output_shape(self):
        k = torch.randn(10)
        tau = torch.rand(10)
        out = normalize(k, tau)
        assert out.shape == (10, 2)


class TestVolatilityPINN:
    def test_forward_shape(self):
        model = VolatilityPINN()
        k_tau = torch.randn(16, 2)
        mu, v_squared = model(k_tau)
        assert mu.shape == (16, 1)
        assert v_squared.shape == (16, 1)

    def test_param_count_fourier_disabled(self):
        """Explicit num_fourier_bands=0 -- the "bare" 2-input architecture.
        Note: this is no longer the default (see TestFourierFeatureEncoding
        .test_default_is_now_three_fourier_bands) -- num_fourier_bands=3 was
        found empirically optimal (2.68% MAE walk-forward, see conversation)
        and is now VolatilityPINN's default."""
        model = VolatilityPINN(hidden_dim=128, num_layers=4, num_fourier_bands=0)
        n_params = sum(p.numel() for p in model.parameters())
        # 2->128 (384) + 3x(128->128) (3x16512=49536) + 128->2 (258) = 50178
        assert n_params == 50178

    def test_v_squared_always_positive(self):
        model = VolatilityPINN()
        # Wide range including extreme inputs, to catch any path that could
        # produce non-positive variance.
        k_tau = torch.tensor([[-10.0, -10.0], [0.0, 0.0], [10.0, 10.0], [1e6, -1e6]])
        _, v_squared = model(k_tau)
        assert torch.all(v_squared > 0)

    def test_predict_w_matches_forward_mu(self):
        model = VolatilityPINN()
        k_tau = torch.randn(5, 2)
        mu, _ = model(k_tau)
        w = model.predict_w(k_tau)
        assert torch.allclose(mu, w)

    def test_second_derivative_wrt_k_is_nonzero(self):
        """Critical test: the butterfly/Durrleman penalty (Step 6) needs a
        genuine, non-zero d^2(w)/dk^2. Softplus guarantees this; if this
        model were ever swapped to ReLU, this test would catch it (ReLU's
        second derivative is zero almost everywhere -> silent no-op penalty).
        """
        model = VolatilityPINN()
        k_tau = torch.tensor([[0.3, 0.1]], requires_grad=True)
        mu, _ = model(k_tau)
        w = mu.squeeze(-1)

        grad1 = torch.autograd.grad(w.sum(), k_tau, create_graph=True)[0]
        w_prime = grad1[:, 0]

        grad2 = torch.autograd.grad(w_prime.sum(), k_tau, create_graph=True)[0]
        w_double_prime = grad2[:, 0]

        assert not torch.allclose(w_double_prime, torch.zeros_like(w_double_prime))
        assert torch.isfinite(w_double_prime).all()

    def test_first_derivative_wrt_tau_exists(self):
        """Calendar penalty (Step 6) needs d(w)/d(tau) — sanity check it's
        computable and finite before the loss is built on top of it."""
        model = VolatilityPINN()
        k_tau = torch.tensor([[0.0, 0.2]], requires_grad=True)
        mu, _ = model(k_tau)

        grads = torch.autograd.grad(mu.sum(), k_tau, create_graph=True)[0]
        dw_dtau = grads[:, 1]
        assert torch.isfinite(dw_dtau).all()

    def test_xavier_init_produces_reasonable_weight_scale(self):
        model = VolatilityPINN()
        for m in model.modules():
            if isinstance(m, torch.nn.Linear):
                assert torch.all(m.bias == 0)
                # Xavier normal std = sqrt(2 / (fan_in + fan_out)); weights
                # should not be degenerate (all-zero) or blown up.
                assert m.weight.std().item() > 0
                assert m.weight.abs().max().item() < 5.0

    def test_eval_mode_is_deterministic(self):
        """No dropout/batchnorm in this architecture — same input should give
        bit-identical output across repeated forward passes."""
        model = VolatilityPINN()
        model.eval()
        k_tau = torch.randn(4, 2)
        with torch.no_grad():
            mu1, v1 = model(k_tau)
            mu2, v2 = model(k_tau)
        assert torch.equal(mu1, mu2)
        assert torch.equal(v1, v2)


class TestRawInputModel:
    """RawInputModel lets calendar_penalty/butterfly_penalty (which need
    derivatives w.r.t. TRUE physical k/tau) operate directly on raw
    collocation points, while the underlying network still only ever sees
    properly-normalized input -- see the class docstring for the full
    rationale. This is the fix for a real gap: sample_collocation() produces
    raw domain points, but VolatilityPINN requires normalized input."""

    def test_forward_matches_manual_normalize_then_model(self):
        model = VolatilityPINN()
        wrapped = RawInputModel(model)
        k_tau_raw = torch.tensor([[0.3, 0.1], [-0.5, 0.2]])

        mu_wrapped, v2_wrapped = wrapped(k_tau_raw)
        norm = normalize(k_tau_raw[:, 0], k_tau_raw[:, 1])
        mu_manual, v2_manual = model(norm)

        assert torch.allclose(mu_wrapped, mu_manual)
        assert torch.allclose(v2_wrapped, v2_manual)

    def test_gradient_wrt_raw_k_matches_finite_difference(self):
        """The critical correctness check: autograd through the wrapper must
        give the TRUE d(w)/d(k_raw), not d(w)/d(k_normalized) -- verified
        against a direct finite-difference approximation in raw-k space."""
        model = VolatilityPINN()
        model.eval()
        wrapped = RawInputModel(model)
        k0, tau0 = 0.3, 0.1

        k_tau = torch.tensor([[k0, tau0]], requires_grad=True)
        mu, _ = wrapped(k_tau)
        grad = torch.autograd.grad(mu.sum(), k_tau)[0]
        dw_dk_autograd = grad[0, 0].item()

        eps = 1e-4
        with torch.no_grad():
            mu_plus, _ = wrapped(torch.tensor([[k0 + eps, tau0]]))
            mu_minus, _ = wrapped(torch.tensor([[k0 - eps, tau0]]))
        dw_dk_finite_diff = (mu_plus.item() - mu_minus.item()) / (2 * eps)

        assert dw_dk_autograd == pytest.approx(dw_dk_finite_diff, abs=1e-2)

    def test_parameters_match_underlying_model(self):
        model = VolatilityPINN()
        wrapped = RawInputModel(model)
        assert sum(p.numel() for p in wrapped.parameters()) == \
               sum(p.numel() for p in model.parameters())

    def test_gradient_flows_to_underlying_model_parameters(self):
        model = VolatilityPINN()
        wrapped = RawInputModel(model)
        k_tau = torch.tensor([[0.3, 0.1], [-0.2, 0.3]])

        mu, _ = wrapped(k_tau)
        mu.sum().backward()

        assert any(p.grad is not None and p.grad.abs().sum().item() > 0
                   for p in model.parameters())

    def test_integrates_with_arbitrage_penalties_on_raw_collocation(self):
        """The actual intended use: pass RawInputModel wherever
        calendar_penalty/butterfly_penalty expect a 'model', directly on
        RAW collocation points from sample_collocation() -- no manual
        normalize() call needed at the call site."""
        from tools.pinn_volatility.losses.arbitrage import calendar_penalty, butterfly_penalty
        from tools.pinn_volatility.data.collocation import sample_collocation

        model = VolatilityPINN()
        wrapped = RawInputModel(model)
        collocation = sample_collocation(n_points=50, rng=np.random.default_rng(0))

        cal = calendar_penalty(wrapped, collocation)
        but = butterfly_penalty(wrapped, collocation)

        assert torch.isfinite(cal)
        assert torch.isfinite(but)


class TestFourierFeatureEncoding:
    """num_fourier_bands=3 is now VolatilityPINN's default -- found
    empirically optimal via a frequency sweep (L=2: 3.79% MAE, L=3: 2.68%,
    L=4: 3.32%, all on the same seeded 8-day walk-forward holdout; L=4
    overshoots into higher bias, confirming the classic Fourier-feature
    bias/variance tradeoff). num_fourier_bands=0 remains available and
    exactly reproduces the pre-Fourier architecture when explicitly passed.
    These tests re-verify the critical property the whole architecture
    depends on (non-degenerate second derivative w.r.t. k) still holds with
    Fourier encoding enabled."""

    def test_default_is_now_three_fourier_bands(self):
        model = VolatilityPINN()  # no args -- exercises the actual default
        assert model.num_fourier_bands == 3
        input_dim = 2 * 3 + 1
        expected = (
            (input_dim * 128 + 128)
            + 3 * (128 * 128 + 128)
            + (128 * 2 + 2)
        )
        n_params = sum(p.numel() for p in model.parameters())
        assert n_params == expected

    def test_explicit_disabled_preserves_original_param_count(self):
        model = VolatilityPINN(num_fourier_bands=0)
        n_params = sum(p.numel() for p in model.parameters())
        assert n_params == 50178  # same as TestVolatilityPINN.test_param_count_fourier_disabled

    def test_gamma_k_matches_hand_computed_values(self):
        model = VolatilityPINN(num_fourier_bands=2)
        k_norm = torch.tensor([[0.5]])
        gamma = model._fourier_features(k_norm)

        expected = torch.tensor([[
            math.sin(1 * math.pi * 0.5), math.cos(1 * math.pi * 0.5),   # band 0: 2^0=1
            math.sin(2 * math.pi * 0.5), math.cos(2 * math.pi * 0.5),   # band 1: 2^1=2
        ]])
        assert torch.allclose(gamma, expected, atol=1e-6)

    def test_gamma_output_shape(self):
        model = VolatilityPINN(num_fourier_bands=4)
        k_norm = torch.rand(10, 1)
        gamma = model._fourier_features(k_norm)
        assert gamma.shape == (10, 8)  # 2 * num_fourier_bands

    def test_param_count_matches_formula_for_enabled_bands(self):
        L = 4
        hidden_dim = 128
        model = VolatilityPINN(hidden_dim=hidden_dim, num_layers=4, num_fourier_bands=L)
        input_dim = 2 * L + 1  # gamma(k) + tau

        expected = (
            (input_dim * hidden_dim + hidden_dim)          # input -> hidden 1
            + 3 * (hidden_dim * hidden_dim + hidden_dim)   # 3 hidden->hidden layers
            + (hidden_dim * 2 + 2)                          # hidden -> output
        )
        n_params = sum(p.numel() for p in model.parameters())
        assert n_params == expected

    def test_forward_shape_with_fourier_enabled(self):
        model = VolatilityPINN(num_fourier_bands=6)
        k_tau = torch.randn(16, 2)
        mu, v_squared = model(k_tau)
        assert mu.shape == (16, 1)
        assert v_squared.shape == (16, 1)

    def test_second_derivative_wrt_k_nonzero_with_fourier_enabled(self):
        """Critical regression check: this is the exact property
        (test_second_derivative_wrt_k_is_nonzero in TestVolatilityPINN) that
        the butterfly/Durrleman penalty depends on. sin/cos are smooth
        (C-infinity), so this must still hold with Fourier encoding on."""
        model = VolatilityPINN(num_fourier_bands=4)
        k_tau = torch.tensor([[0.3, 0.1]], requires_grad=True)
        mu, _ = model(k_tau)
        w = mu.squeeze(-1)

        grad1 = torch.autograd.grad(w.sum(), k_tau, create_graph=True)[0]
        w_prime = grad1[:, 0]
        grad2 = torch.autograd.grad(w_prime.sum(), k_tau, create_graph=True)[0]
        w_double_prime = grad2[:, 0]

        assert not torch.allclose(w_double_prime, torch.zeros_like(w_double_prime))
        assert torch.isfinite(w_double_prime).all()

    def test_raw_input_model_integration_with_fourier_enabled(self):
        """RawInputModel + calendar/butterfly penalties must work transparently
        with a Fourier-enabled model -- no plumbing changes needed since the
        encoding is purely internal to forward()."""
        from tools.pinn_volatility.losses.arbitrage import calendar_penalty, butterfly_penalty
        from tools.pinn_volatility.data.collocation import sample_collocation

        model = VolatilityPINN(num_fourier_bands=4)
        wrapped = RawInputModel(model)
        collocation = sample_collocation(n_points=50, rng=np.random.default_rng(1))

        cal = calendar_penalty(wrapped, collocation)
        but = butterfly_penalty(wrapped, collocation)
        assert torch.isfinite(cal)
        assert torch.isfinite(but)
