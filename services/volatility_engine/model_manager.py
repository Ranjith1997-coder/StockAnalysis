"""
ModelManager — loads per-symbol PINN checkpoints and hot-reloads when
tools/pinn_volatility/training/run_training.py accepts a new one.

Deviation from docs/pinn-volatility-engine.md section 8.2's original sketch:
  - SYMBOLS defaults to PINNConfig().symbols (["NIFTY", "BANKNIFTY"]), not the
    doc's hardcoded ["NIFTY", "BANKNIFTY", "SENSEX"] -- SENSEX is a BSE
    product, never present in the NSE Bhavcopy this project trains on (see
    data/bhavcopy_fetcher.py's module docstring), so no SENSEX checkpoint
    will ever exist.
  - Reconstructs VolatilityPINN using the architecture fields
    (hidden_dim/num_layers/num_fourier_bands) that run_training.py actually
    saves into the checkpoint, instead of assuming a bare VolatilityPINN()
    matches whatever the model was trained with.
"""
from __future__ import annotations

import os

import torch

from tools.pinn_volatility.config import PINNConfig
from tools.pinn_volatility.model.pinn import VolatilityPINN
from lib.logging_util import get_logger
logger = get_logger("volatility-engine")


class ModelManager:
    """Owns the currently-loaded PINN model per symbol, with hot-reload."""

    def __init__(self, model_dir: str | None = None, symbols: list[str] | None = None):
        config = PINNConfig()
        self.model_dir = model_dir or config.model_dir
        self.symbols = symbols or list(config.symbols)
        self.models: dict[str, VolatilityPINN] = {}
        # Target filename the `_latest.pt` symlink points at -- changes
        # exactly when run_training.py's _update_symlink() repoints it.
        # Deliberately not mtime: same-second re-symlinks (as in tests, or
        # two accepted trainings within the same second) can leave mtime
        # unchanged depending on filesystem timestamp resolution, but the
        # target filename always differs (it's the training end_date).
        self._link_targets: dict[str, str] = {}
        self.train_dates: dict[str, str] = {}
        self._load_all()

    def _load_all(self) -> None:
        for symbol in self.symbols:
            self._load_symbol(symbol)

    def _symlink_path(self, symbol: str) -> str:
        return os.path.join(self.model_dir, f"{symbol}_latest.pt")

    def _load_symbol(self, symbol: str) -> None:
        path = self._symlink_path(symbol)
        if not os.path.exists(path):
            logger.warning("[vol-engine] No model for %s at %s, skipping", symbol, path)
            return

        try:
            checkpoint = torch.load(path, map_location="cpu", weights_only=True)
            model = VolatilityPINN(
                hidden_dim=checkpoint.get("hidden_dim", 128),
                num_layers=checkpoint.get("num_layers", 4),
                num_fourier_bands=checkpoint.get("num_fourier_bands", 3),
            )
            model.load_state_dict(checkpoint["model_state"])
            model.eval()
        except Exception:
            logger.error("[vol-engine] Failed to load %s model from %s", symbol, path, exc_info=True)
            return

        self.models[symbol] = model
        self.train_dates[symbol] = checkpoint.get("train_date", "unknown")
        self._link_targets[symbol] = os.readlink(path) if os.path.islink(path) else path
        logger.info("[vol-engine] Loaded %s model (trained %s, holdout %s)",
                    symbol, self.train_dates[symbol], checkpoint.get("holdout_date", "unknown"))

    def hot_reload_check(self) -> None:
        """Reload any symbol whose `_latest.pt` symlink was repointed since
        the last load (i.e. run_training.py accepted a new model)."""
        for symbol in self.symbols:
            path = self._symlink_path(symbol)
            if not os.path.exists(path):
                continue
            if symbol not in self.models:
                self._load_symbol(symbol)
                continue
            target = os.readlink(path) if os.path.islink(path) else path
            if target != self._link_targets.get(symbol):
                logger.info("[vol-engine] New model detected for %s, reloading", symbol)
                self._load_symbol(symbol)

    def get_model(self, symbol: str) -> VolatilityPINN | None:
        return self.models.get(symbol)
