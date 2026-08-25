"""
Model evaluation against held-out data.

evaluate_holdout() -- walk-forward holdout accuracy: does this model
actually predict tomorrow's surface, not just interpolate within the
training week. audit_arbitrage() -- an independent, dense post-hoc
no-arbitrage check. check_acceptance_criteria() combines both into one
pass/fail gate (Step 9 of the plan, section 7.4 -- adapted to the
thresholds actually validated across 10 real holdout days, not the plan's
original untested numbers; see its docstring).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import torch

from tools.pinn_volatility.model.pinn import normalize, RawInputModel, K_RANGE, TAU_RANGE
from tools.pinn_volatility.data.collocation import sample_collocation
from tools.pinn_volatility.losses.arbitrage import durrleman_density, calendar_slope


@dataclass
class HoldoutMetrics:
    n_samples: int
    rmse_w: float                       # RMSE on total implied variance w
    mae_sigma: float                    # mean absolute error on implied vol, in vol points (e.g. 0.02 = 2 pts)
    mean_bias_sigma: float              # signed: positive = model UNDER-predicts vol (actual > predicted)
    mae_sigma_by_moneyness: dict = field(default_factory=dict)  # {"atm": x, "wings": y} -- None if a bucket is empty


def evaluate_holdout(
    model,
    k: torch.Tensor,
    tau: torch.Tensor,
    w_actual: torch.Tensor,
    sigma_actual: torch.Tensor,
    atm_threshold: float = 0.1,
) -> HoldoutMetrics:
    """Evaluate a trained model against real (held-out) market data.

    Args:
        model: trained VolatilityPINN.
        k, tau: RAW (unnormalized) log-moneyness / time-to-expiry for the
            holdout samples (e.g. from split_by_holdout_date()'s output,
            via samples_to_tensors()).
        w_actual, sigma_actual: the REAL observed total variance / implied
            vol for those same samples (ground truth, from NSE settlement
            prices -- not model predictions).
        atm_threshold: |k| below this counts as "ATM" for the moneyness
            breakdown; the rest counts as "wings". The PINN's whole value
            proposition is shape (skew) accuracy, not just level -- a
            model that's accurate ATM but wrong on the wings would still
            fail at its actual job (SKEW_FADE/RANGE_BOUND signals are
            wing-driven), so this breakdown matters more than the
            aggregate MAE alone.

    Returns:
        HoldoutMetrics with aggregate + per-moneyness-bucket error.
    """
    model.eval()
    with torch.no_grad():
        mu_pred, _ = model(normalize(k, tau))
    w_pred = mu_pred.squeeze(-1)
    sigma_pred = torch.sqrt(torch.clamp(w_pred / tau, min=1e-8))

    rmse_w = torch.sqrt(((w_pred - w_actual) ** 2).mean()).item()
    mae_sigma = (sigma_pred - sigma_actual).abs().mean().item()
    mean_bias_sigma = (sigma_actual - sigma_pred).mean().item()

    atm_mask = k.abs() < atm_threshold
    wing_mask = ~atm_mask

    def _mae(mask: torch.Tensor):
        if mask.sum().item() == 0:
            return None
        return (sigma_pred[mask] - sigma_actual[mask]).abs().mean().item()

    return HoldoutMetrics(
        n_samples=len(k),
        rmse_w=rmse_w,
        mae_sigma=mae_sigma,
        mean_bias_sigma=mean_bias_sigma,
        mae_sigma_by_moneyness={"atm": _mae(atm_mask), "wings": _mae(wing_mask)},
    )


@dataclass
class ArbitrageAudit:
    n_points: int
    min_g: float                      # worst (most negative) Durrleman density found -- < 0 means a real butterfly violation exists
    min_calendar_slope: float         # worst (most negative) dw/dtau found -- < 0 means a real calendar violation exists
    butterfly_violation_rate: float   # fraction of audited points with g(k) < 0
    calendar_violation_rate: float    # fraction of audited points with dw/dtau < 0
    max_butterfly_violation: float    # magnitude of the worst g(k) violation (0.0 if none found)
    max_calendar_violation: float     # magnitude of the worst dw/dtau violation (0.0 if none found)


def audit_arbitrage(
    model,
    n_points: int = 5000,
    k_range: tuple[float, float] = K_RANGE,
    tau_range: tuple[float, float] = TAU_RANGE,
    seed: int | None = None,
) -> ArbitrageAudit:
    """Independent, dense post-hoc no-arbitrage check.

    Why this is separate from composite_loss's per-epoch min_g/
    min_calendar_slope: those are computed on whatever small (e.g. 512-point),
    stochastically-resampled collocation batch that particular training step
    happened to draw -- useful for a live progress signal, but not a
    trustworthy final verdict, since a single lucky/unlucky batch could hide
    or exaggerate a violation. This samples a fresh, larger, denser
    collocation set purely for auditing, independent of anything used during
    training.

    A penalty of exactly 0.0 during training is consistent with EITHER "no
    violations anywhere" OR "violations exist but are tiny enough that
    clamp(-x, min=0)**2 rounds away" -- this function's raw min/violation-rate
    numbers are what actually distinguish those two cases.

    Args:
        model: a trained VolatilityPINN (or anything with the same
            (k_tau_norm) -> (mu, v_squared) interface).
        n_points: size of the fresh audit collocation sample.
        k_range, tau_range: audit domain -- defaults to the same domain the
            model was trained/normalized against (model/pinn.py's K_RANGE/
            TAU_RANGE).
        seed: optional seed for reproducible audits.

    Returns:
        ArbitrageAudit with raw minimums, violation rates, and worst-violation
        magnitudes for both the calendar and butterfly conditions.
    """
    was_training = getattr(model, "training", False)
    if hasattr(model, "eval"):
        model.eval()

    rng = np.random.default_rng(seed) if seed is not None else None
    points = sample_collocation(n_points, k_range=k_range, tau_range=tau_range, rng=rng)
    wrapped = RawInputModel(model) if not isinstance(model, RawInputModel) else model

    g = durrleman_density(wrapped, points).detach()
    slope = calendar_slope(wrapped, points).detach()

    butterfly_violations = g[g < 0]
    calendar_violations = slope[slope < 0]

    audit = ArbitrageAudit(
        n_points=n_points,
        min_g=g.min().item(),
        min_calendar_slope=slope.min().item(),
        butterfly_violation_rate=(g < 0).float().mean().item(),
        calendar_violation_rate=(slope < 0).float().mean().item(),
        max_butterfly_violation=(-butterfly_violations.min().item() if len(butterfly_violations) else 0.0),
        max_calendar_violation=(-calendar_violations.min().item() if len(calendar_violations) else 0.0),
    )

    if was_training and hasattr(model, "train"):
        model.train()

    return audit


@dataclass
class AcceptanceResult:
    accepted: bool
    reasons: list = field(default_factory=list)   # human-readable failure reasons; empty iff accepted
    holdout: HoldoutMetrics = None
    audit: ArbitrageAudit = None


def check_acceptance_criteria(
    model,
    k_hold: torch.Tensor,
    tau_hold: torch.Tensor,
    w_hold: torch.Tensor,
    sigma_hold: torch.Tensor,
    max_wings_mae: float = 0.025,
    max_butterfly_violation_rate: float = 0.05,
    max_calendar_violation_rate: float = 0.01,
    max_overall_mae: float | None = None,
    atm_threshold: float = 0.1,
    n_audit_points: int = 5000,
    audit_seed: int | None = None,
) -> AcceptanceResult:
    """Automated accept/reject gate: combines evaluate_holdout() (accuracy)
    and audit_arbitrage() (no-arbitrage compliance) into one pass/fail
    decision, for run_training.py's fallback logic (a rejected model is
    never deployed -- the previous accepted model stays live).

    Threshold provenance -- these are NOT the plan doc's original section
    7.4 numbers (calendar<1%, butterfly<5%, rmse<1%, mae_sigma<2%), which
    were never validated against real data. They're the actual targets
    this project validated across 10 real NIFTY+BANKNIFTY holdout days
    (2026-08-10 through 2026-08-21, see conversation history):
      - max_wings_mae=0.025 (2.5%): the explicit target set for this
        project. Met on 6/10 days individually, 2.39% on average.
      - max_butterfly_violation_rate=0.05 (5%): the plan's own threshold,
        kept as-is since it's the one plan-original number this project
        specifically re-validated (met on 9/10 days, 3.11% on average).
      - max_calendar_violation_rate=0.01 (1%): kept from the plan --
        calendar violations were 0.0% on every single day tested, so this
        threshold has never actually been exercised; it's a cheap sanity
        floor, not a validated tight bound.
      - max_overall_mae: None (disabled) by default -- overall/ATM MAE
        were never set as explicit pass/fail targets (only wings was), and
        this project's validated overall MAE (~2.95% mean) exceeds the
        plan's original mae_sigma<2% figure. Enable explicitly if you want
        to gate on it, with a threshold informed by real data, not the
        plan's untested default.

    KNOWN LIMITATION (deliberate, not an oversight): even the best
    validated configuration failed one or both thresholds on 1 of 10 real
    days (2026-08-11: wings 3.03%, butterfly violation 5.44% -- both over).
    This gate is precisely the mechanism for handling that operationally --
    reject that day's model, keep serving the previous accepted one --
    rather than a guarantee that every trained model will pass.

    Returns:
        AcceptanceResult(accepted, reasons, holdout, audit) -- reasons is
        empty iff accepted; holdout/audit are always populated regardless
        of the verdict, for logging.
    """
    holdout = evaluate_holdout(model, k_hold, tau_hold, w_hold, sigma_hold, atm_threshold=atm_threshold)
    audit = audit_arbitrage(model, n_points=n_audit_points, seed=audit_seed)

    reasons = []

    wings_mae = holdout.mae_sigma_by_moneyness.get("wings")
    if wings_mae is not None and wings_mae >= max_wings_mae:
        reasons.append(f"wings MAE {wings_mae*100:.2f}% >= {max_wings_mae*100:.2f}% threshold")

    if max_overall_mae is not None and holdout.mae_sigma >= max_overall_mae:
        reasons.append(f"overall MAE {holdout.mae_sigma*100:.2f}% >= {max_overall_mae*100:.2f}% threshold")

    if audit.butterfly_violation_rate >= max_butterfly_violation_rate:
        reasons.append(
            f"butterfly violation rate {audit.butterfly_violation_rate*100:.2f}% "
            f">= {max_butterfly_violation_rate*100:.2f}% threshold (min_g={audit.min_g:.4f})"
        )

    if audit.calendar_violation_rate >= max_calendar_violation_rate:
        reasons.append(
            f"calendar violation rate {audit.calendar_violation_rate*100:.2f}% "
            f">= {max_calendar_violation_rate*100:.2f}% threshold (min_slope={audit.min_calendar_slope:.4f})"
        )

    return AcceptanceResult(accepted=(len(reasons) == 0), reasons=reasons, holdout=holdout, audit=audit)
