"""
PINNConfig — single reference point for the PINN's tunable defaults.

IMPORTANT SCOPING NOTE: this dataclass is NOT yet consumed by VolatilityPINN,
PINNTrainer, or composite_loss -- each of those still owns its own
constructor defaults directly (and remains the actual source of truth for
what a bare `VolatilityPINN()` / `PINNTrainer()` does). This class exists as
a single documented snapshot of the values found empirically best so far
(see conversation history on feature/pinn-volatility-engine), for
run_training.py (not yet built) and any script that wants one object to
construct model+trainer from, rather than restating every value by hand.
Wiring the actual classes to accept a PINNConfig instead of individual
kwargs is a separate, later refactor -- doing it now would touch every
call site in this package's test suite for no functional gain yet.

Values here track the ACTUAL current defaults in model/pinn.py and
training/trainer.py, not .kilo/plans/pinn-volatility-engine.md section 12's
original sketch -- several of those (adam_lr=1e-3, n_collocation=2000,
no num_fourier_bands at all) were superseded by empirical findings during
implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PINNConfig:
    # ── Model architecture ──
    hidden_dim: int = 128
    num_layers: int = 4
    # Found empirically optimal via a frequency sweep on real 8-day
    # NIFTY+BANKNIFTY walk-forward data: L=2 -> 3.79% MAE, L=3 -> 2.68%
    # (2.67% ATM / 2.75% wings, well-balanced), L=4 -> 3.32% with growing
    # bias (overshoot). See model/pinn.py's VolatilityPINN docstring.
    num_fourier_bands: int = 3

    # ── Training ──
    adam_epochs: int = 5000
    adam_lr: float = 5e-4          # lowered from the plan's original 1e-3 -- see trainer.py
    adam_lr_min: float = 1e-5
    lbfgs_max_iter: int = 500
    lbfgs_history_size: int = 50
    grad_clip_norm: float = 1.0

    # ── Loss weights ──
    lambda_data: float = 1.0
    lambda_calendar: float = 1.0
    # Raised from 0.5 -- at 0.5, num_fourier_bands=3's real wing-fitting
    # capacity let the model produce actual butterfly-arbitrage violations
    # (up to 7.6% of an audit grid on one holdout day, above the plan's 5%
    # threshold). 1.0 brought violations to a consistent 1.7-3.2% across 3
    # holdout days while wings MAE stayed under the 2.5% target. See
    # training/trainer.py's PINNTrainer docstring for the full context.
    lambda_butterfly: float = 1.0
    beta_nll: float = 0.5

    # NOTE: vega-weighting (use_vega_weight) was tried and removed -- an
    # isolation experiment found it actively hurt wing accuracy, both raw
    # and sqrt(tau)-normalized. See losses/data_loss.py's module-level note.
    # There is deliberately no vega_weight field here.

    # ── Collocation ──
    n_collocation: int = 512       # lowered from 2000 -- paired with continuous per-epoch resampling
    collocation_regen_every: int = 1   # every epoch -- fixes an observed "collocation shock"
    k_range: tuple[float, float] = (-2.0, 2.0)
    tau_range: tuple[float, float] = (0.003, 1.0)
    collocation_concentrated_frac: float = 0.6

    # ── Data ──
    symbols: list[str] = field(default_factory=lambda: ["NIFTY", "BANKNIFTY"])  # SENSEX excluded -- BSE product, not in NSE's file
    training_window_days: int = 7
    risk_free_rate: float = 0.07
    dividend_yield: float = 0.0
    max_iv: float = 2.0
    min_volume: int = 1
    k_max: float = 2.0

    # ── Paths ──
    model_dir: str = "data/pinn_models"
    training_data_dir: str = "data/pinn_training"

    # ── Reproducibility ──
    seed: int | None = None
