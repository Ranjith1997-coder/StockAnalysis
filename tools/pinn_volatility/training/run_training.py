"""
CLI entry point: orchestrates the full nightly training pipeline for one or
more symbols -- fetch Bhavcopy -> build dataset -> train -> accept/reject
gate -> save + symlink (or fall back to the previous accepted model).

Usage:
    python -m tools.pinn_volatility.training.run_training
    python -m tools.pinn_volatility.training.run_training --symbols NIFTY
    python -m tools.pinn_volatility.training.run_training --symbols NIFTY BANKNIFTY --seed 42
    python -m tools.pinn_volatility.training.run_training --end-date 2026-08-14

Exit code is 0 iff every requested symbol's model was accepted; nonzero if
any symbol was rejected/failed (still leaves the previous accepted model in
place for that symbol -- a nonzero exit is a signal to alert on, not a sign
the service is down).
"""
from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import date

import torch

from tools.pinn_volatility.config import PINNConfig
from tools.pinn_volatility.data.bhavcopy_fetcher import fetch_recent_bhavcopies
from tools.pinn_volatility.data.dataset import (
    build_training_samples, samples_to_tensors, split_by_holdout_date,
)
from tools.pinn_volatility.model.pinn import VolatilityPINN
from tools.pinn_volatility.training.trainer import PINNTrainer
from tools.pinn_volatility.training.validate import check_acceptance_criteria
from lib.logging_util import get_logger
logger = get_logger("pinn")


@dataclass
class TrainingRunResult:
    symbol: str
    accepted: bool
    reasons: list
    holdout_date: str | None = None
    train_time_s: float = 0.0
    overall_mae: float | None = None
    atm_mae: float | None = None
    wings_mae: float | None = None
    butterfly_violation_rate: float | None = None
    calendar_violation_rate: float | None = None
    model_path: str | None = None


def _update_symlink(link_path: str, target_filename: str) -> None:
    """Point `link_path` at `target_filename` (same directory) -- a relative
    symlink so the model directory stays portable if moved/copied."""
    if os.path.islink(link_path) or os.path.exists(link_path):
        os.remove(link_path)
    os.symlink(target_filename, link_path)


def _failure_result(symbol: str, reason: str) -> TrainingRunResult:
    logger.error("[pinn] %s: training aborted -- %s (fallback: previous model, if any, stays live)",
                 symbol, reason)
    return TrainingRunResult(symbol=symbol, accepted=False, reasons=[reason])


def train_one_symbol(
    symbol: str,
    config: PINNConfig,
    model_dir: str | None = None,
    end_date: date | None = None,
) -> TrainingRunResult:
    """Run the full pipeline for one symbol: fetch -> dataset -> train ->
    gate -> save/fallback. Never raises on ordinary failure modes (no data,
    too little data, gate rejection) -- those are reported in the returned
    result so a caller training multiple symbols can continue past one
    failure; only genuinely unexpected errors propagate.
    """
    model_dir = model_dir or config.model_dir
    end_date = end_date or date.today()

    logger.info("[pinn] === Training %s (end_date=%s) ===", symbol, end_date)

    # 1. Fetch -- training_window_days to train on + 1 more as the genuine
    # held-out "next day" the gate evaluates against.
    bhavcopy = fetch_recent_bhavcopies(
        n_days=config.training_window_days + 1, end_date=end_date, symbols=[symbol],
    )
    if bhavcopy.empty:
        return _failure_result(symbol, "no Bhavcopy data available for the requested window")

    # 2. Dataset
    samples = build_training_samples(
        bhavcopy, symbols=[symbol], r=config.risk_free_rate, q=config.dividend_yield,
        k_max=config.k_max, max_iv=config.max_iv, min_volume=config.min_volume,
        max_tau=config.max_tau,
    )
    if len(samples) < 50:
        return _failure_result(symbol, f"too few training samples after filtering ({len(samples)})")

    try:
        train_samples, holdout_samples, holdout_date = split_by_holdout_date(samples)
    except ValueError as e:
        return _failure_result(symbol, str(e))

    if len(holdout_samples) < 10:
        return _failure_result(symbol, f"too few holdout samples on {holdout_date} ({len(holdout_samples)})")

    k_train, tau_train, w_train = samples_to_tensors(train_samples)
    k_hold, tau_hold, w_hold = samples_to_tensors(holdout_samples)
    sigma_hold = torch.tensor([s.sigma for s in holdout_samples], dtype=torch.float32)

    # 3. Train
    if config.seed is not None:
        torch.manual_seed(config.seed)
    model = VolatilityPINN(
        hidden_dim=config.hidden_dim, num_layers=config.num_layers,
        num_fourier_bands=config.num_fourier_bands,
    )
    trainer = PINNTrainer(
        adam_epochs=config.adam_epochs, adam_lr=config.adam_lr, adam_lr_min=config.adam_lr_min,
        lbfgs_max_iter=config.lbfgs_max_iter, lbfgs_history_size=config.lbfgs_history_size,
        n_collocation=config.n_collocation, collocation_regen_every=config.collocation_regen_every,
        lambda_data=config.lambda_data, lambda_cal=config.lambda_calendar, lambda_but=config.lambda_butterfly,
        beta_nll=config.beta_nll, grad_clip_norm=config.grad_clip_norm, seed=config.seed,
        deterministic_threads=config.enable_deterministic_threads,
        short_tau_boost_frac=config.short_tau_boost_frac if config.short_tau_collocation_boost else 0.0,
        short_tau_boost_range=config.short_tau_boost_range,
    )
    t0 = time.time()
    trainer.train(model, k_train, tau_train, w_train)
    train_time = time.time() - t0
    logger.info("[pinn] %s: training finished in %.1fs", symbol, train_time)

    # 4. Gate
    acceptance = check_acceptance_criteria(
        model, k_hold, tau_hold, w_hold, sigma_hold, audit_seed=config.seed,
    )

    # 5. Save (accepted) or fall back (rejected) -- a rejected model is
    # never written to the _latest symlink, so whatever passed the gate
    # last time stays live.
    model_path = None
    if acceptance.accepted:
        os.makedirs(model_dir, exist_ok=True)
        filename = f"{symbol}_{end_date.strftime('%Y%m%d')}.pt"
        model_path = os.path.join(model_dir, filename)
        torch.save({
            "model_state": model.state_dict(),
            "num_fourier_bands": config.num_fourier_bands,
            "hidden_dim": config.hidden_dim,
            "num_layers": config.num_layers,
            "train_date": end_date.isoformat(),
            "holdout_date": holdout_date,
        }, model_path)
        _update_symlink(os.path.join(model_dir, f"{symbol}_latest.pt"), filename)
        logger.info("[pinn] %s: ACCEPTED -- saved %s, _latest symlink updated", symbol, model_path)
    else:
        logger.warning("[pinn] %s: REJECTED (%s) -- previous accepted model stays live",
                        symbol, "; ".join(acceptance.reasons))

    return TrainingRunResult(
        symbol=symbol,
        accepted=acceptance.accepted,
        reasons=acceptance.reasons,
        holdout_date=holdout_date,
        train_time_s=train_time,
        overall_mae=acceptance.holdout.mae_sigma,
        atm_mae=acceptance.holdout.mae_sigma_by_moneyness.get("atm"),
        wings_mae=acceptance.holdout.mae_sigma_by_moneyness.get("wings"),
        butterfly_violation_rate=acceptance.audit.butterfly_violation_rate,
        calendar_violation_rate=acceptance.audit.calendar_violation_rate,
        model_path=model_path,
    )


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", nargs="+", default=None,
                         help="Symbols to train (default: PINNConfig's symbols, i.e. NIFTY BANKNIFTY)")
    parser.add_argument("--model-dir", default=None, help="Override PINNConfig.model_dir")
    parser.add_argument("--seed", type=int, default=None, help="Override PINNConfig.seed")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD -- defaults to today")
    args = parser.parse_args(argv)

    config = PINNConfig()
    if args.seed is not None:
        config.seed = args.seed
    symbols = args.symbols or config.symbols
    model_dir = args.model_dir or config.model_dir
    end_date = date.fromisoformat(args.end_date) if args.end_date else None

    results = [train_one_symbol(symbol, config, model_dir, end_date=end_date) for symbol in symbols]

    logger.info("[pinn] === Training run summary ===")
    for r in results:
        if r.accepted:
            logger.info("[pinn]   %s: ACCEPTED  wings=%.2f%% but_viol=%.2f%%  -> %s",
                        r.symbol, (r.wings_mae or 0) * 100, (r.butterfly_violation_rate or 0) * 100, r.model_path)
        else:
            logger.warning("[pinn]   %s: REJECTED  (%s)", r.symbol, "; ".join(r.reasons))

    return 1 if any(not r.accepted for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
