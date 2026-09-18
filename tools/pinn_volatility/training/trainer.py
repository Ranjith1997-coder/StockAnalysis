"""
PINNTrainer — two-stage optimization: Adam (coarse) -> L-BFGS (precision).

NOTE on scope vs. the plan doc: .kilo/plans/pinn-volatility-engine.md section
7.1's trainer calls a `self._validate(val_samples)` every 500 epochs for
progress logging -- that's Step 9 (training/validate.py, full RMSE +
arbitrage-violation-rate check), which doesn't exist yet at this point in the
build order. This trainer logs the composite loss breakdown (already
available every epoch) instead for progress visibility during Adam, and
leaves the full acceptance-criteria validation as a separate, final step
(intended call site: run_training.py, after train() returns).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import torch

from tools.pinn_volatility.model.pinn import VolatilityPINN
from tools.pinn_volatility.data.collocation import sample_collocation
from tools.pinn_volatility.losses.composite import composite_loss
from lib.logging_util import get_logger
logger = get_logger("pinn")


@dataclass
class TrainingResult:
    model: VolatilityPINN
    final_breakdown: dict
    epoch_history: list = field(default_factory=list)  # [{epoch, stage, total, data, calendar, butterfly}, ...]


class PINNTrainer:
    """Two-stage optimizer for VolatilityPINN.

    Stage 1 (Adam): coarse convergence over `adam_epochs`, cosine LR decay,
    collocation points regenerated every `collocation_regen_every` epochs
    (default: every epoch -- standard PINN practice, and the fix for an
    observed "collocation shock": with a large regen interval, e.g. every
    1000 epochs, a fresh synthetic sample introduces a real loss
    discontinuity right at that boundary since the network can partially
    overfit to one fixed set of collocation points between regens. Resampling
    every step makes that structurally impossible -- the network is never
    shown the same synthetic points twice, so it can't memorize them and
    must actually learn the smooth underlying surface). Gradient norm
    clipped to `grad_clip_norm` -- the plan's own risk register flags
    butterfly-penalty gradient explosion as a real risk with clipping as the
    named mitigation.

    Stage 2 (L-BFGS): full-batch precision convergence on the final data +
    a fresh collocation sample. No gradient clipping here -- L-BFGS's
    quasi-Newton updates rely on true (unclipped) gradients for its internal
    line search; clipping would interfere with its convergence properties.

    Reproducibility: `seed`, when given, seeds a dedicated
    numpy.random.Generator used for ALL of this trainer's own collocation
    resampling (not torch's RNG -- model init / other torch randomness is
    the caller's responsibility, e.g. torch.manual_seed() before
    constructing the model). Without a seed, collocation sampling falls
    back to numpy's global unseeded RNG state, which is what caused ~2 vol
    points of run-to-run metric noise between otherwise-identical configs
    (see conversation) -- purely from different random collocation draws,
    not genuine model-quality differences.

    Even with a seed, a *second* nondeterminism source remains:
    `deterministic_threads=True` calls `torch.set_num_threads(1)` at the
    start of train() to close it. PyTorch's CPU matmul/conv kernels use
    multi-threaded reductions whose floating-point order (and therefore
    exact result) isn't guaranteed reproducible across runs even with
    torch.manual_seed() fixed -- confirmed empirically on 2026-09-17: an
    identical (seed, config, data) re-run produced wings MAE 2.53%/butterfly
    1.48% vs. the original run's 3.41%/7.68%, on a borderline day where that
    gap flips the accept/reject verdict. Single-threaded execution is slower
    but bit-reproducible; default is False since most callers care more
    about wall-clock than exact reproducibility.

    lambda_but=0.7 (raised from 0.5): with num_fourier_bands=3 (the model's
    own default), the network has real capacity to fit wing curvature --
    but that same capacity let it produce locally negative Durrleman
    density (a genuine butterfly-arbitrage violation) at lambda_but=0.5,
    confirmed via training/validate.py's audit_arbitrage() at up to 7.6% of
    a dense audit grid on one holdout day, above the plan's own 5%
    acceptance threshold (section 7.4). A coarse sweep (0.5/1.0/2.0/3.0)
    found 1.0 resolved the violation but cost ~0.3 points of ATM/overall
    MAE; a finer sweep between 0.5 and 1.0 found violations drop sharply
    already by 0.6 and stay roughly flat (2-3%) from there to 1.0 -- so
    0.7 clears the threshold with real margin (2.83 +/- 0.55% across 3
    holdout days, max 3.58%, vs. the 5% limit) while recovering nearly all
    of the 0.5 config's ATM/bias quality (ATM 2.99 vs. 1.0's 3.18; bias
    -1.79 vs. -1.96), with wings essentially unchanged (2.05 vs. 2.06).
    See conversation history for the full sweep data.
    """

    def __init__(
        self,
        adam_epochs: int = 5000,
        adam_lr: float = 5e-4,
        adam_lr_min: float = 1e-5,
        lbfgs_max_iter: int = 500,
        lbfgs_history_size: int = 50,
        n_collocation: int = 512,
        collocation_regen_every: int = 1,
        lambda_data: float = 1.0,
        lambda_cal: float = 1.0,
        lambda_but: float = 0.7,
        beta_nll: float = 0.5,
        grad_clip_norm: float = 1.0,
        log_every: int = 500,
        use_tau_weight: bool = False,
        use_moneyness_weight: bool = False,
        tau_weight_max: float = 20.0,
        moneyness_alpha: float = 5.0,
        seed: int | None = None,
        deterministic_threads: bool = False,
        short_tau_boost_frac: float = 0.0,
        short_tau_boost_range: tuple[float, float] = (0.005, 0.02),
    ):
        self.adam_epochs = adam_epochs
        self.adam_lr = adam_lr
        self.adam_lr_min = adam_lr_min
        self.lbfgs_max_iter = lbfgs_max_iter
        self.lbfgs_history_size = lbfgs_history_size
        self.n_collocation = n_collocation
        self.collocation_regen_every = collocation_regen_every
        self.lambda_data = lambda_data
        self.lambda_cal = lambda_cal
        self.lambda_but = lambda_but
        self.beta_nll = beta_nll
        self.grad_clip_norm = grad_clip_norm
        self.log_every = log_every
        self.use_tau_weight = use_tau_weight
        self.use_moneyness_weight = use_moneyness_weight
        self.tau_weight_max = tau_weight_max
        self.moneyness_alpha = moneyness_alpha
        self.seed = seed
        self.deterministic_threads = deterministic_threads
        self.short_tau_boost_frac = short_tau_boost_frac
        self.short_tau_boost_range = short_tau_boost_range
        self._rng = np.random.default_rng(seed) if seed is not None else None

    def _sample_collocation(self):
        return sample_collocation(
            self.n_collocation, rng=self._rng,
            short_tau_boost_frac=self.short_tau_boost_frac,
            short_tau_boost_range=self.short_tau_boost_range,
        )

    def _loss(self, model, k_train, tau_train, w_train, collocation):
        return composite_loss(
            model, k_train, tau_train, w_train, collocation,
            beta=self.beta_nll, lambda_data=self.lambda_data,
            lambda_cal=self.lambda_cal, lambda_but=self.lambda_but,
            use_tau_weight=self.use_tau_weight, use_moneyness_weight=self.use_moneyness_weight,
            tau_weight_max=self.tau_weight_max, moneyness_alpha=self.moneyness_alpha,
        )

    def train(
        self,
        model: VolatilityPINN,
        k_train: torch.Tensor,
        tau_train: torch.Tensor,
        w_train: torch.Tensor,
    ) -> TrainingResult:
        """Run the full two-stage training loop in place on `model`.

        Args:
            model: freshly-constructed VolatilityPINN (trained in place --
                the same object is returned in the result).
            k_train, tau_train, w_train: RAW training samples (from
                dataset.samples_to_tensors()), each shape (N,).

        Returns:
            TrainingResult with the (now-trained) model, the final loss
            breakdown, and a history of logged checkpoints.
        """
        if self.deterministic_threads:
            torch.set_num_threads(1)

        model.train()
        history: list = []

        # ── Stage 1: Adam ──────────────────────────────────────────────
        optimizer = torch.optim.Adam(model.parameters(), lr=self.adam_lr)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer, T_max=max(self.adam_epochs, 1), eta_min=self.adam_lr_min,
        )

        collocation = self._sample_collocation()

        for epoch in range(self.adam_epochs):
            if epoch > 0 and epoch % self.collocation_regen_every == 0:
                collocation = self._sample_collocation()

            loss, breakdown = self._loss(model, k_train, tau_train, w_train, collocation)

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), self.grad_clip_norm)
            optimizer.step()
            scheduler.step()

            if epoch % self.log_every == 0 or epoch == self.adam_epochs - 1:
                logger.info(
                    "[pinn] Adam epoch %d/%d: total=%.6f data=%.6f cal=%.6f but=%.6f "
                    "min_g=%.6f min_cal_slope=%.6f",
                    epoch, self.adam_epochs, breakdown["total"], breakdown["data"],
                    breakdown["calendar"], breakdown["butterfly"],
                    breakdown["min_g"], breakdown["min_calendar_slope"],
                )
                history.append({"epoch": epoch, "stage": "adam", **breakdown})

        # ── Stage 2: L-BFGS ────────────────────────────────────────────
        collocation_final = self._sample_collocation()
        lbfgs = torch.optim.LBFGS(
            model.parameters(), lr=1.0, max_iter=self.lbfgs_max_iter,
            history_size=self.lbfgs_history_size, tolerance_grad=1e-7,
            tolerance_change=1e-9, line_search_fn="strong_wolfe",
        )

        last_breakdown = history[-1] if history else {}

        def closure():
            nonlocal last_breakdown
            lbfgs.zero_grad()
            loss, breakdown = self._loss(model, k_train, tau_train, w_train, collocation_final)
            loss.backward()
            last_breakdown = breakdown
            return loss

        lbfgs.step(closure)

        logger.info(
            "[pinn] L-BFGS final: total=%.6f data=%.6f cal=%.6f but=%.6f "
            "min_g=%.6f min_cal_slope=%.6f",
            last_breakdown.get("total", 0.0), last_breakdown.get("data", 0.0),
            last_breakdown.get("calendar", 0.0), last_breakdown.get("butterfly", 0.0),
            last_breakdown.get("min_g", 0.0), last_breakdown.get("min_calendar_slope", 0.0),
        )
        history.append({"epoch": self.adam_epochs, "stage": "lbfgs", **last_breakdown})

        return TrainingResult(model=model, final_breakdown=last_breakdown, epoch_history=history)
