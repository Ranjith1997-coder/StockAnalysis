"""Tests for services/volatility_engine/model_manager.py."""
import os

import torch

from services.volatility_engine.model_manager import ModelManager
from tools.pinn_volatility.model.pinn import VolatilityPINN


def _save_checkpoint(path, hidden_dim=16, num_layers=2, num_fourier_bands=2,
                      train_date="2026-08-20", holdout_date="2026-08-21"):
    model = VolatilityPINN(hidden_dim=hidden_dim, num_layers=num_layers, num_fourier_bands=num_fourier_bands)
    torch.save({
        "model_state": model.state_dict(),
        "hidden_dim": hidden_dim,
        "num_layers": num_layers,
        "num_fourier_bands": num_fourier_bands,
        "train_date": train_date,
        "holdout_date": holdout_date,
    }, path)


class TestModelManagerLoad:
    def test_loads_model_matching_checkpoint_architecture(self, tmp_path):
        checkpoint_path = tmp_path / "NIFTY_20260821.pt"
        _save_checkpoint(checkpoint_path, hidden_dim=16, num_layers=2, num_fourier_bands=2)
        os.symlink(checkpoint_path.name, tmp_path / "NIFTY_latest.pt")

        manager = ModelManager(model_dir=str(tmp_path), symbols=["NIFTY"])

        model = manager.get_model("NIFTY")
        assert model is not None
        assert isinstance(model, VolatilityPINN)
        assert manager.train_dates["NIFTY"] == "2026-08-20"

    def test_missing_symlink_leaves_model_unloaded(self, tmp_path):
        manager = ModelManager(model_dir=str(tmp_path), symbols=["NIFTY"])
        assert manager.get_model("NIFTY") is None

    def test_default_symbols_come_from_pinn_config_not_sensex(self, tmp_path):
        """Deviation from the design doc's hardcoded SYMBOLS list (which
        included SENSEX) -- SENSEX is never in the NSE Bhavcopy this project
        trains on, so no checkpoint for it will ever exist."""
        manager = ModelManager(model_dir=str(tmp_path))
        assert manager.symbols == ["NIFTY", "BANKNIFTY"]

    def test_broken_checkpoint_does_not_raise(self, tmp_path):
        bad_path = tmp_path / "NIFTY_bad.pt"
        bad_path.write_text("not a real checkpoint")
        os.symlink(bad_path.name, tmp_path / "NIFTY_latest.pt")

        manager = ModelManager(model_dir=str(tmp_path), symbols=["NIFTY"])
        assert manager.get_model("NIFTY") is None


class TestHotReload:
    def test_no_change_keeps_same_model_instance(self, tmp_path):
        checkpoint_path = tmp_path / "NIFTY_20260821.pt"
        _save_checkpoint(checkpoint_path)
        os.symlink(checkpoint_path.name, tmp_path / "NIFTY_latest.pt")

        manager = ModelManager(model_dir=str(tmp_path), symbols=["NIFTY"])
        model_before = manager.get_model("NIFTY")

        manager.hot_reload_check()

        assert manager.get_model("NIFTY") is model_before

    def test_repointed_symlink_triggers_reload(self, tmp_path):
        old_checkpoint = tmp_path / "NIFTY_20260821.pt"
        _save_checkpoint(old_checkpoint, train_date="2026-08-20")
        link = tmp_path / "NIFTY_latest.pt"
        os.symlink(old_checkpoint.name, link)

        manager = ModelManager(model_dir=str(tmp_path), symbols=["NIFTY"])
        model_before = manager.get_model("NIFTY")

        new_checkpoint = tmp_path / "NIFTY_20260822.pt"
        _save_checkpoint(new_checkpoint, train_date="2026-08-21")
        os.remove(link)
        os.symlink(new_checkpoint.name, link)

        manager.hot_reload_check()

        assert manager.get_model("NIFTY") is not model_before
        assert manager.train_dates["NIFTY"] == "2026-08-21"

    def test_model_appears_after_startup_with_none(self, tmp_path):
        """A symbol with no model at startup should pick one up once
        run_training.py produces its first accepted checkpoint."""
        manager = ModelManager(model_dir=str(tmp_path), symbols=["NIFTY"])
        assert manager.get_model("NIFTY") is None

        checkpoint_path = tmp_path / "NIFTY_20260821.pt"
        _save_checkpoint(checkpoint_path)
        os.symlink(checkpoint_path.name, tmp_path / "NIFTY_latest.pt")

        manager.hot_reload_check()

        assert manager.get_model("NIFTY") is not None
