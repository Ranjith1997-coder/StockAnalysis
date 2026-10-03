"""Walk-forward benchmark harness for the PINN volatility model.

The 0.4/0.7 sweeps documented in docs/pinn-volatility-engine.md were run via
ad-hoc scripts that were never committed; this is the reproducible harness for
the Phase 1/2 experiment (deterministic baseline + isolated loss-weight A/B).

It deliberately mirrors tools/pinn_volatility/training/run_training.py's
train_one_symbol() pipeline exactly (fetch -> dataset -> split -> train ->
gate) so numbers are comparable to the documented 0.4 baseline, but it:
  - passes the trainer's loss-weight flags (use_moneyness_weight /
    use_tau_weight), which run_training.py does not expose (they are not
    PINNConfig fields), and
  - never writes a model or Redis key (pure measurement).

Usage:
    python -m tools.pinn_volatility.training.benchmark --variant baseline
    python -m tools.pinn_volatility.training.benchmark --variant money5 \
        --dates 2026-08-10:2026-08-21 --symbols NIFTY BANKNIFTY --seeds 42 43

Variants:
    baseline   no sample weighting (documented 0.4 config)
    money5     use_moneyness_weight=True, moneyness_alpha=5.0 (trainer default)
    money1p5   use_moneyness_weight=True, moneyness_alpha=1.5
    tau        use_tau_weight=True
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import date, timedelta

import torch

from common.market_calendar import is_trading_day
from tools.pinn_volatility.config import PINNConfig
from tools.pinn_volatility.data.bhavcopy_fetcher import fetch_recent_bhavcopies
from tools.pinn_volatility.data.dataset import (
    build_training_samples, samples_to_tensors, split_by_holdout_date,
)
from tools.pinn_volatility.model.pinn import VolatilityPINN
from tools.pinn_volatility.training.trainer import PINNTrainer
from tools.pinn_volatility.training.validate import check_acceptance_criteria

VARIANTS = {
    "baseline": {"use_moneyness_weight": False, "use_tau_weight": False},
    "money5": {"use_moneyness_weight": True, "moneyness_alpha": 5.0},
    "money1p5": {"use_moneyness_weight": True, "moneyness_alpha": 1.5},
    "tau": {"use_tau_weight": True},
}


def trading_days(start: date, end: date) -> list[date]:
    days, cur = [], start
    while cur <= end:
        if is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def run_one(symbol: str, end_date: date, seed: int, variant: dict, config: PINNConfig) -> dict:
    """One walk-forward run. Mirrors run_training.train_one_symbol() steps 1-4."""
    t0 = time.time()

    bhavcopy = fetch_recent_bhavcopies(
        n_days=config.training_window_days + 1, end_date=end_date, symbols=[symbol],
    )
    if bhavcopy.empty:
        return {"status": "no_data", "symbol": symbol, "end_date": end_date.isoformat(), "seed": seed}

    samples = build_training_samples(
        bhavcopy, symbols=[symbol], r=config.risk_free_rate, q=config.dividend_yield,
        k_max=config.k_max, max_iv=config.max_iv, min_volume=config.min_volume,
        max_tau=config.max_tau,
    )
    if len(samples) < 50:
        return {"status": "too_few_samples", "symbol": symbol,
                "end_date": end_date.isoformat(), "seed": seed, "n_samples": len(samples)}

    train_samples, holdout_samples, holdout_date = split_by_holdout_date(samples)
    if len(holdout_samples) < 10:
        return {"status": "too_few_holdout", "symbol": symbol,
                "end_date": end_date.isoformat(), "seed": seed, "n_holdout": len(holdout_samples)}

    k_train, tau_train, w_train = samples_to_tensors(train_samples)
    k_hold, tau_hold, w_hold = samples_to_tensors(holdout_samples)
    sigma_hold = torch.tensor([s.sigma for s in holdout_samples], dtype=torch.float32)

    if config.seed is not None:
        torch.manual_seed(seed)
    model = VolatilityPINN(
        hidden_dim=config.hidden_dim, num_layers=config.num_layers,
        num_fourier_bands=config.num_fourier_bands,
    )
    trainer = PINNTrainer(
        adam_epochs=config.adam_epochs, adam_lr=config.adam_lr, adam_lr_min=config.adam_lr_min,
        lbfgs_max_iter=config.lbfgs_max_iter, lbfgs_history_size=config.lbfgs_history_size,
        n_collocation=config.n_collocation, collocation_regen_every=config.collocation_regen_every,
        lambda_data=config.lambda_data, lambda_cal=config.lambda_calendar, lambda_but=config.lambda_butterfly,
        beta_nll=config.beta_nll, grad_clip_norm=config.grad_clip_norm, seed=seed,
        deterministic_threads=config.enable_deterministic_threads,
        short_tau_boost_frac=config.short_tau_boost_frac if config.short_tau_collocation_boost else 0.0,
        short_tau_boost_range=config.short_tau_boost_range,
        **variant,
    )
    trainer.train(model, k_train, tau_train, w_train)

    acceptance = check_acceptance_criteria(
        model, k_hold, tau_hold, w_hold, sigma_hold, audit_seed=seed,
    )
    holdout = acceptance.holdout

    return {
        "status": "ok",
        "symbol": symbol,
        "end_date": end_date.isoformat(),
        "holdout_date": holdout_date,
        "seed": seed,
        "accepted": acceptance.accepted,
        "reasons": acceptance.reasons,
        "wings_mae": holdout.mae_sigma_by_moneyness.get("wings"),
        "atm_mae": holdout.mae_sigma_by_moneyness.get("atm"),
        "overall_mae": holdout.mae_sigma,
        "butterfly_rate": acceptance.audit.butterfly_violation_rate,
        "calendar_rate": acceptance.audit.calendar_violation_rate,
        "n_holdout": len(holdout_samples),
        "n_train": len(train_samples),
        "elapsed_s": time.time() - t0,
    }


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("status") == "ok"]
    if not ok:
        return {"n_runs": 0, "n_ok": 0}
    wings = [r["wings_mae"] for r in ok if r["wings_mae"] is not None]
    butterfly = [r["butterfly_rate"] for r in ok]
    return {
        "n_runs": len(rows),
        "n_ok": len(ok),
        "wings_mean": statistics.mean(wings) if wings else None,
        "wings_stdev": statistics.stdev(wings) if len(wings) > 1 else 0.0,
        "wings_max": max(wings) if wings else None,
        "butterfly_mean": statistics.mean(butterfly) if butterfly else None,
        "butterfly_max": max(butterfly) if butterfly else None,
        "accept_rate": sum(1 for r in ok if r["accepted"]) / len(ok),
        "mean_train_s": statistics.mean(r["elapsed_s"] for r in ok),
    }


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--variant", required=True, choices=sorted(VARIANTS))
    parser.add_argument("--dates", default="2026-08-10:2026-08-21", help="START:END (inclusive)")
    parser.add_argument("--symbols", nargs="+", default=["NIFTY", "BANKNIFTY"])
    parser.add_argument("--seeds", nargs="+", type=int, default=[42])
    parser.add_argument("--out", default=None, help="JSON output path (default: /tmp/pinn_bench_<variant>.json)")
    parser.add_argument("--no-deterministic", action="store_true",
                        help="Disable torch.set_num_threads(1) (faster, non-reproducible)")
    args = parser.parse_args(argv)

    config = PINNConfig()
    config.enable_deterministic_threads = not args.no_deterministic
    variant = VARIANTS[args.variant]

    start_s, end_s = args.dates.split(":")
    days = trading_days(date.fromisoformat(start_s), date.fromisoformat(end_s))
    out_path = args.out or f"/tmp/pinn_bench_{args.variant}.json"

    print(f"variant={args.variant} flags={variant} deterministic={config.enable_deterministic_threads}")
    print(f"days={[d.isoformat() for d in days]} symbols={args.symbols} seeds={args.seeds}")
    print(f"total runs = {len(days) * len(args.symbols) * len(args.seeds)}")

    rows = []
    for seed in args.seeds:
        for day in days:
            for symbol in args.symbols:
                row = run_one(symbol, day, seed, variant, config)
                rows.append(row)
                if row["status"] == "ok":
                    print(f"  {day} {symbol} seed={seed}: wings={row['wings_mae']*100:.2f}% "
                          f"atm={row['atm_mae']*100:.2f}% but={row['butterfly_rate']*100:.2f}% "
                          f"accepted={row['accepted']} ({row['elapsed_s']:.0f}s)")
                else:
                    print(f"  {day} {symbol} seed={seed}: SKIPPED ({row['status']})")

    summary = summarize(rows)
    with open(out_path, "w") as fh:
        json.dump({"variant": args.variant, "variant_flags": variant, "rows": rows, "summary": summary}, fh, indent=2)

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
