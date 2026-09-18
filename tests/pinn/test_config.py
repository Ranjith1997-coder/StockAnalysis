"""Tests for tools/pinn_volatility/config.py.

PINNConfig is a documented reference snapshot, NOT yet wired into
VolatilityPINN/PINNTrainer (see config.py's module docstring) -- these
tests mainly guard against it silently drifting out of sync with the
classes' own actual defaults, since nothing enforces that automatically.
"""

from tools.pinn_volatility.config import PINNConfig
from tools.pinn_volatility.model.pinn import VolatilityPINN
from tools.pinn_volatility.training.trainer import PINNTrainer


class TestPINNConfig:
    def test_constructs_with_defaults(self):
        config = PINNConfig()
        assert config.num_fourier_bands == 3
        assert config.adam_epochs == 5000

    def test_num_fourier_bands_matches_volatility_pinn_default(self):
        """The empirically-chosen L=3 must agree between the two -- this
        would NOT fail automatically if someone changed one but not the
        other, since they're independent classes."""
        assert PINNConfig().num_fourier_bands == VolatilityPINN().num_fourier_bands

    def test_trainer_relevant_fields_match_pinn_trainer_defaults(self):
        config = PINNConfig()
        trainer = PINNTrainer()
        assert config.adam_epochs == trainer.adam_epochs
        assert config.adam_lr == trainer.adam_lr
        assert config.adam_lr_min == trainer.adam_lr_min
        assert config.lbfgs_max_iter == trainer.lbfgs_max_iter
        assert config.n_collocation == trainer.n_collocation
        assert config.collocation_regen_every == trainer.collocation_regen_every
        assert config.grad_clip_norm == trainer.grad_clip_norm
        assert config.lambda_data == trainer.lambda_data
        assert config.lambda_calendar == trainer.lambda_cal
        assert config.lambda_butterfly == trainer.lambda_but
        assert config.beta_nll == trainer.beta_nll
        assert config.enable_deterministic_threads == trainer.deterministic_threads
        assert config.short_tau_boost_range == trainer.short_tau_boost_range

    def test_experimental_flags_default_off(self):
        """The 2026-09-17-investigation flags must default to the original,
        already-validated behavior -- opt-in only, never silently active."""
        config = PINNConfig()
        assert config.enable_deterministic_threads is False
        assert config.short_tau_collocation_boost is False
        assert config.max_tau is None

    def test_symbols_excludes_sensex(self):
        assert "SENSEX" not in PINNConfig().symbols
        assert set(PINNConfig().symbols) == {"NIFTY", "BANKNIFTY"}

    def test_has_no_vega_weight_field(self):
        """Regression guard: vega-weighting was deliberately removed after
        being found harmful -- it must not silently reappear here."""
        assert not hasattr(PINNConfig(), "use_vega_weight")
        assert not hasattr(PINNConfig(), "vega_weight")
