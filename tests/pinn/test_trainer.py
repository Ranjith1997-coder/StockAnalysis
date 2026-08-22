"""Tests for tools/pinn_volatility/training/trainer.py — Step 8 of the PINN plan.

Uses small epoch counts (calibrated against a real run -- see conversation:
200 Adam epochs + 20 L-BFGS iters on 60 synthetic samples took ~1.2s) so
these stay fast while still exercising real convergence behavior, not mocks.
"""

import math
import numpy as np
import torch
import pytest
from unittest.mock import patch

from tools.pinn_volatility.model.pinn import VolatilityPINN
from tools.pinn_volatility.training.trainer import PINNTrainer, TrainingResult
import tools.pinn_volatility.training.trainer as trainer_module


def _synthetic_training_data(n=60, seed=0):
    """A simple, easily-learnable, roughly arbitrage-friendly surface --
    used to exercise the training loop's mechanics, not to validate model
    quality (that's Step 9 + a real full-scale run's job)."""
    rng = np.random.default_rng(seed)
    k = torch.tensor(rng.uniform(-1.0, 1.0, n), dtype=torch.float32)
    tau = torch.tensor(rng.uniform(0.02, 0.3, n), dtype=torch.float32)
    w = 0.03 + 0.05 * tau + 0.01 * k ** 2
    return k, tau, torch.tensor(w.numpy(), dtype=torch.float32)


class TestPINNTrainerDefaults:
    def test_default_construction_matches_plan_values(self):
        """Cheap documentation/regression check -- doesn't run training."""
        t = PINNTrainer()
        assert t.adam_epochs == 5000
        # Lowered from the plan's original 1e-3 -- an empirical comparison
        # (see conversation) found 1e-3 oscillated and 1e-4 under-trained at
        # a fixed 5000-epoch budget; 5e-4 is the requested middle ground.
        assert t.adam_lr == 5e-4
        assert t.lbfgs_max_iter == 500
        # Lowered from 2000 -- with continuous per-epoch resampling (below),
        # a smaller batch per step is the standard stochastic-PDE-residual
        # tradeoff (more, cheaper updates vs. fewer, larger ones).
        assert t.n_collocation == 512
        # Default is 1 (resample every epoch) -- fixes an observed
        # "collocation shock" where a large regen interval caused a real
        # loss discontinuity at each regen boundary. See class docstring.
        assert t.collocation_regen_every == 1
        assert t.lambda_data == 1.0
        assert t.lambda_cal == 1.0
        assert t.lambda_but == 0.5
        assert t.beta_nll == 0.5
        assert t.seed is None
        assert t.grad_clip_norm == 1.0


class TestTrainingMechanics:
    def test_loss_decreases_substantially_during_adam(self):
        torch.manual_seed(0)
        k, tau, w = _synthetic_training_data()
        model = VolatilityPINN()
        trainer = PINNTrainer(adam_epochs=100, n_collocation=50,
                               collocation_regen_every=50, lbfgs_max_iter=5, log_every=1)

        result = trainer.train(model, k, tau, w)

        adam_entries = [h for h in result.epoch_history if h["stage"] == "adam"]
        first_loss = adam_entries[0]["total"]
        last_adam_loss = adam_entries[-1]["total"]
        # Calibrated empirically: this scenario drops from ~2.4 to below -0.05
        # within 100 epochs -- assert a generous, non-flaky margin.
        assert last_adam_loss < first_loss - 1.0

    def test_all_losses_finite_no_nan_explosion(self):
        """Regression guard for the plan's own flagged risk: butterfly-penalty
        gradient explosion -> NaN loss. Gradient clipping should prevent it."""
        torch.manual_seed(1)
        k, tau, w = _synthetic_training_data(seed=1)
        model = VolatilityPINN()
        trainer = PINNTrainer(adam_epochs=100, n_collocation=50,
                               collocation_regen_every=50, lbfgs_max_iter=5, log_every=1)

        result = trainer.train(model, k, tau, w)

        for entry in result.epoch_history:
            assert math.isfinite(entry["total"]), entry

    def test_lbfgs_stage_runs_and_appends_final_entry(self):
        torch.manual_seed(2)
        k, tau, w = _synthetic_training_data(seed=2)
        model = VolatilityPINN()
        trainer = PINNTrainer(adam_epochs=20, n_collocation=30,
                               collocation_regen_every=10, lbfgs_max_iter=10, log_every=5)

        result = trainer.train(model, k, tau, w)

        assert result.epoch_history[-1]["stage"] == "lbfgs"
        assert math.isfinite(result.epoch_history[-1]["total"])

    def test_result_model_is_same_object_trained_in_place(self):
        torch.manual_seed(3)
        k, tau, w = _synthetic_training_data(seed=3)
        model = VolatilityPINN()
        trainer = PINNTrainer(adam_epochs=10, n_collocation=20, lbfgs_max_iter=5, log_every=5)

        result = trainer.train(model, k, tau, w)

        assert result.model is model

    def test_final_breakdown_has_expected_keys(self):
        torch.manual_seed(4)
        k, tau, w = _synthetic_training_data(seed=4)
        model = VolatilityPINN()
        trainer = PINNTrainer(adam_epochs=10, n_collocation=20, lbfgs_max_iter=5, log_every=5)

        result = trainer.train(model, k, tau, w)

        assert set(result.final_breakdown.keys()) == {"data", "calendar", "butterfly", "total"}


class TestCollocationRegeneration:
    def test_regenerated_at_expected_epoch_boundaries(self):
        """25 Adam epochs, regen every 10 -> resample at epoch 0, 10, 20
        (3 calls) + 1 more for the L-BFGS stage's final collocation = 4 total."""
        torch.manual_seed(5)
        k, tau, w = _synthetic_training_data(seed=5)
        model = VolatilityPINN()
        trainer = PINNTrainer(adam_epochs=25, n_collocation=20,
                               collocation_regen_every=10, lbfgs_max_iter=5, log_every=25)

        with patch.object(trainer_module, "sample_collocation",
                           wraps=trainer_module.sample_collocation) as mock_sample:
            trainer.train(model, k, tau, w)

        assert mock_sample.call_count == 4

    def test_no_regeneration_when_regen_interval_exceeds_epoch_count(self):
        """collocation_regen_every > adam_epochs -> only the initial sample
        during Adam + 1 for L-BFGS = 2 total calls."""
        torch.manual_seed(6)
        k, tau, w = _synthetic_training_data(seed=6)
        model = VolatilityPINN()
        trainer = PINNTrainer(adam_epochs=15, n_collocation=20,
                               collocation_regen_every=1000, lbfgs_max_iter=5, log_every=15)

        with patch.object(trainer_module, "sample_collocation",
                           wraps=trainer_module.sample_collocation) as mock_sample:
            trainer.train(model, k, tau, w)

        assert mock_sample.call_count == 2


class TestGradientClipping:
    def test_clip_grad_norm_called_with_configured_max_norm(self):
        torch.manual_seed(7)
        k, tau, w = _synthetic_training_data(seed=7)
        model = VolatilityPINN()
        trainer = PINNTrainer(adam_epochs=5, n_collocation=20,
                               lbfgs_max_iter=3, grad_clip_norm=0.5, log_every=5)

        with patch("torch.nn.utils.clip_grad_norm_") as mock_clip:
            trainer.train(model, k, tau, w)

        assert mock_clip.called
        # Called once per Adam epoch (not during L-BFGS, per design).
        assert mock_clip.call_count == 5
        _, kwargs = mock_clip.call_args
        args = mock_clip.call_args[0]
        max_norm_used = kwargs.get("max_norm", args[1] if len(args) > 1 else None)
        assert max_norm_used == 0.5


class TestReproducibility:
    """A seeded trainer's own collocation sampling must be 100% deterministic
    across repeated runs -- fixes ~2 vol points of run-to-run metric noise
    that came from numpy's unseeded global RNG (see conversation)."""

    def test_same_seed_produces_identical_loss_trajectory(self):
        k, tau, w = _synthetic_training_data(seed=0)

        def run():
            torch.manual_seed(42)  # also fix torch-side randomness (model init)
            model = VolatilityPINN()
            trainer = PINNTrainer(adam_epochs=15, n_collocation=20, lbfgs_max_iter=5,
                                   log_every=1, seed=123)
            return trainer.train(model, k, tau, w)

        result1 = run()
        result2 = run()

        losses1 = [h["total"] for h in result1.epoch_history]
        losses2 = [h["total"] for h in result2.epoch_history]
        assert losses1 == losses2

    def test_different_seeds_produce_different_collocation_samples(self):
        """Checks the actual collocation tensors directly rather than
        inferring it through the loss -- in a small, early-training
        scenario, calendar/butterfly can legitimately be exactly 0.0 for
        every epoch regardless of which collocation points are drawn (this
        happened in practice -- see git history), which would make a
        loss-based comparison a false negative even though seeding is
        working correctly."""
        trainer_a = PINNTrainer(n_collocation=20, seed=1)
        trainer_b = PINNTrainer(n_collocation=20, seed=2)

        colloc_a = trainer_a._sample_collocation()
        colloc_b = trainer_b._sample_collocation()

        assert not torch.equal(colloc_a, colloc_b)

    def test_seeded_collocation_advances_across_successive_calls(self):
        """Continuous per-epoch resampling (Fix 1) requires the SAME seeded
        generator to produce a NEW sample each call, not repeat the first
        draw -- otherwise every epoch would train on identical collocation
        points, defeating the point of continuous resampling."""
        trainer = PINNTrainer(n_collocation=20, seed=1)
        first = trainer._sample_collocation()
        second = trainer._sample_collocation()
        assert not torch.equal(first, second)

    def test_no_seed_still_runs_without_error(self):
        """seed=None (default) must fall back to numpy's global RNG cleanly,
        not error -- backward compatible with all prior unseeded tests."""
        k, tau, w = _synthetic_training_data(seed=0)
        model = VolatilityPINN()
        trainer = PINNTrainer(adam_epochs=5, n_collocation=20, lbfgs_max_iter=3, log_every=5)
        result = trainer.train(model, k, tau, w)
        assert math.isfinite(result.final_breakdown["total"])


# use_vega_weight/vega_train were removed from PINNTrainer after an
# isolation experiment found vega-weighting actively hurt wing accuracy --
# see data_loss.py's module-level note and conversation history on
# feature/pinn-volatility-engine.
