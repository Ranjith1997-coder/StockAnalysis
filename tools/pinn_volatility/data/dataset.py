"""
Converts filtered Bhavcopy rows (bhavcopy_fetcher.extract_index_rows /
fetch_recent_bhavcopies output) into (k, tau, w) training samples.

Column names below match the REAL current NSE schema verified live in Step 2
(FinInstrmTp, TckrSymb, XpryDt, StrkPric, OptnTp, SttlmPric, TtlTradgVol,
UndrlygPric, TradDt) -- NOT the plan doc's stale assumed schema
(INSTRUMENT/SYMBOL/SETTLE_PR/CONTRACTS/...).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd
import torch

from tools.pinn_volatility.model.bs_utils import invert_bs, vega as bs_vega
from lib.logging_util import get_logger
logger = get_logger("pinn")

INDEX_OPTION_TYPE = "IDO"
INDEX_FUTURE_TYPE = "IDF"


@dataclass
class TrainingSample:
    k: float
    tau: float
    w: float
    sigma: float
    symbol: str
    strike: float
    option_type: str
    expiry: str
    trade_date: str
    forward_price: float
    forward_source: str  # "future" (matched an IDF row) | "fallback" (S*e^((r-q)tau))
    vega: float = 0.0     # BS vega at the recovered sigma -- see losses/data_loss.vega_weight


def _parse_date(value) -> date:
    """Bhavcopy dates come back as 'YYYY-MM-DD' strings after a CSV round-trip
    (or already as date/Timestamp objects if called in-process before any
    CSV serialization) -- handle both."""
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def build_forward_price_lookup(bhavcopy: pd.DataFrame) -> dict[tuple[str, str, str], float]:
    """Map (symbol, trade_date, expiry) -> futures settlement price, from IDF rows.

    Note: NIFTY/BANKNIFTY options have many WEEKLY expiries, but futures only
    trade a handful of MONTHLY expiries (current/next/far) -- most option
    expiries will NOT have a matching key here. build_training_samples()
    falls back to S*e^((r-q)*tau) using UndrlygPric for those.
    """
    futures = bhavcopy[bhavcopy["FinInstrmTp"] == INDEX_FUTURE_TYPE]
    lookup = {}
    for _, row in futures.iterrows():
        key = (row["TckrSymb"], str(row["TradDt"])[:10], str(row["XpryDt"])[:10])
        lookup[key] = float(row["SttlmPric"])
    return lookup


def build_training_samples(
    bhavcopy: pd.DataFrame,
    symbols: list[str] = ("NIFTY", "BANKNIFTY"),
    r: float = 0.07,
    q: float = 0.0,
    k_max: float = 2.0,
    max_iv: float = 2.0,
    min_volume: int = 1,
) -> list[TrainingSample]:
    """Convert Bhavcopy option rows into (k, tau, w) training samples.

    Per option row:
      1. Filter: TtlTradgVol >= min_volume, SttlmPric > 0.
      2. tau = (expiry - trade_date).days / 365.
      3. Forward price F: matching IDF row's SttlmPric if one exists for the
         same (symbol, trade_date, expiry); else UndrlygPric * exp((r-q)*tau).
      4. k = ln(strike / F). Filter: |k| < k_max.
      5. sigma = invert_bs(UndrlygPric, strike, tau, r, q, SttlmPric, type).
         Skip if no solution (price below intrinsic) or sigma > max_iv.
      6. w = sigma^2 * tau.

    Returns:
        List of TrainingSample, one per surviving row. Logs a skip-reason
        breakdown at INFO level (never silently drops data without a count).
    """
    forward_lookup = build_forward_price_lookup(bhavcopy)
    options = bhavcopy[
        (bhavcopy["FinInstrmTp"] == INDEX_OPTION_TYPE) & (bhavcopy["TckrSymb"].isin(symbols))
    ]

    samples: list[TrainingSample] = []
    skipped = {"volume": 0, "settle_price": 0, "tau": 0, "k_range": 0, "no_iv_solution": 0, "iv_range": 0}

    for _, row in options.iterrows():
        if float(row.get("TtlTradgVol", 0) or 0) < min_volume:
            skipped["volume"] += 1
            continue

        settle = float(row["SttlmPric"])
        if settle <= 0:
            skipped["settle_price"] += 1
            continue

        trade_date = _parse_date(row["TradDt"])
        expiry_date = _parse_date(row["XpryDt"])
        tau = (expiry_date - trade_date).days / 365.0
        if tau <= 0:
            skipped["tau"] += 1
            continue

        symbol = row["TckrSymb"]
        underlying = float(row["UndrlygPric"])
        key = (symbol, str(row["TradDt"])[:10], str(row["XpryDt"])[:10])
        if key in forward_lookup:
            forward_price = forward_lookup[key]
            forward_source = "future"
        else:
            forward_price = underlying * math.exp((r - q) * tau)
            forward_source = "fallback"

        strike = float(row["StrkPric"])
        k = math.log(strike / forward_price)
        if abs(k) > k_max:
            skipped["k_range"] += 1
            continue

        option_type = str(row["OptnTp"]).strip()
        sigma = invert_bs(underlying, strike, tau, r, q, settle, option_type)
        if sigma is None:
            skipped["no_iv_solution"] += 1
            continue
        if sigma > max_iv:
            skipped["iv_range"] += 1
            continue

        w = sigma ** 2 * tau
        v = bs_vega(underlying, strike, tau, r, q, sigma)
        samples.append(TrainingSample(
            k=k, tau=tau, w=w, sigma=sigma, symbol=symbol, strike=strike,
            option_type=option_type, expiry=str(row["XpryDt"])[:10],
            trade_date=str(row["TradDt"])[:10],
            forward_price=forward_price, forward_source=forward_source,
            vega=v,
        ))

    logger.info("[pinn] build_training_samples: %d kept, skipped=%s", len(samples), skipped)
    return samples


def samples_to_tensors(samples: list[TrainingSample]) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Convert to (k, tau, w) float32 tensors, ready for normalize() + the model."""
    k = torch.tensor([s.k for s in samples], dtype=torch.float32)
    tau = torch.tensor([s.tau for s in samples], dtype=torch.float32)
    w = torch.tensor([s.w for s in samples], dtype=torch.float32)
    return k, tau, w


def vega_tensor(samples: list[TrainingSample]) -> torch.Tensor:
    """Extract per-sample BS vega as a float32 tensor, in the same order as
    samples_to_tensors()'s (k, tau, w) -- for losses/data_loss.vega_weight()."""
    return torch.tensor([s.vega for s in samples], dtype=torch.float32)


def train_val_split(
    samples: list[TrainingSample], val_frac: float = 0.2, seed: int | None = None,
) -> tuple[list[TrainingSample], list[TrainingSample]]:
    """Random split at the individual-sample level.

    Known simplification: this does NOT hold out entire trading days, so
    rows from the same day/expiry can land in both train and val -- the plan
    doesn't specify day-level holdout either. Use split_by_holdout_date()
    instead when validating whether the model generalizes to an unseen
    future day (walk-forward validation) -- that's the methodology that
    actually matches how this model gets used in production (trained
    nightly on the past week, used the next trading day).
    """
    if not samples:
        return [], []
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(samples))
    n_val = int(len(samples) * val_frac)
    val_idx = set(idx[:n_val].tolist())
    train = [s for i, s in enumerate(samples) if i not in val_idx]
    val = [s for i, s in enumerate(samples) if i in val_idx]
    return train, val


def split_by_holdout_date(
    samples: list[TrainingSample],
) -> tuple[list[TrainingSample], list[TrainingSample], str]:
    """Split samples by trade_date, holding out the MOST RECENT date as a
    true walk-forward test set.

    This is a materially stronger validation than train_val_split()'s random
    row-level split, which can leak same-day strikes into both train and
    val. It directly tests what the model will actually be asked to do in
    production: predict tomorrow's implied-variance surface having only
    ever seen data through yesterday. The model never sees a single row
    from the held-out date during training.

    Returns:
        (train_samples, holdout_samples, holdout_date) -- holdout_date is
        the ISO date string ("YYYY-MM-DD") that was held out.

    Raises:
        ValueError: if samples span fewer than 2 distinct trade dates
            (nothing meaningful to hold out).
    """
    dates = sorted({s.trade_date for s in samples})
    if len(dates) < 2:
        raise ValueError(
            f"Need at least 2 distinct trade dates to hold one out, got {len(dates)}"
        )
    holdout_date = dates[-1]
    train = [s for s in samples if s.trade_date != holdout_date]
    holdout = [s for s in samples if s.trade_date == holdout_date]
    return train, holdout, holdout_date
