"""Tests for tools/pinn_volatility/training/run_training.py — Step 10 of the
PINN plan (CLI orchestration + fallback logic).

Mocks fetch_recent_bhavcopies (no network) and check_acceptance_criteria
(controls accept/reject deterministically) -- these tests verify the
orchestration and fallback LOGIC specifically, not model quality (already
covered extensively by test_trainer.py/test_validate.py). Real training
still runs underneath, just with a tiny epoch count to stay fast.
"""

import os
from datetime import date
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from tools.pinn_volatility.model.bs_utils import bs_price
from tools.pinn_volatility.config import PINNConfig
from tools.pinn_volatility.training.validate import AcceptanceResult, HoldoutMetrics, ArbitrageAudit
import tools.pinn_volatility.training.run_training as run_training_module
from tools.pinn_volatility.training.run_training import (
    train_one_symbol, main, _update_symlink, TrainingRunResult,
)

R, Q = 0.07, 0.0


def _synthetic_bhavcopy(dates, symbol="NIFTY", n_strikes=15, sigma=0.15):
    """Multi-day synthetic Bhavcopy with real settle prices (via bs_price)
    so IV inversion succeeds for essentially all rows -- enough survives
    filtering to comfortably clear train_one_symbol's minimum sample counts."""
    rows = []
    underlying = 24000.0
    expiry = "2026-09-25"  # comfortably after every trade_date below
    for trade_date in dates:
        for i in range(n_strikes):
            strike = underlying - 200 * (n_strikes // 2) + 200 * i
            for option_type in ("CE", "PE"):
                from datetime import datetime
                tau = (datetime.strptime(expiry, "%Y-%m-%d").date()
                       - datetime.strptime(trade_date, "%Y-%m-%d").date()).days / 365.0
                settle = bs_price(underlying, strike, tau, R, Q, sigma, option_type)
                rows.append(dict(
                    TradDt=trade_date, FinInstrmTp="IDO", TckrSymb=symbol,
                    XpryDt=expiry, StrkPric=strike, OptnTp=option_type,
                    SttlmPric=settle, TtlTradgVol=1000, OpnIntrst=1000,
                    UndrlygPric=underlying,
                ))
    return pd.DataFrame(rows)


def _tiny_config(**overrides) -> PINNConfig:
    config = PINNConfig(adam_epochs=5, n_collocation=20, lbfgs_max_iter=3)
    for k, v in overrides.items():
        setattr(config, k, v)
    return config


def _fake_acceptance(accepted: bool, reasons=None):
    holdout = HoldoutMetrics(n_samples=30, rmse_w=0.001, mae_sigma=0.02,
                              mean_bias_sigma=0.0, mae_sigma_by_moneyness={"atm": 0.02, "wings": 0.02})
    audit = ArbitrageAudit(n_points=100, min_g=0.1, min_calendar_slope=0.1,
                            butterfly_violation_rate=0.01, calendar_violation_rate=0.0,
                            max_butterfly_violation=0.0, max_calendar_violation=0.0)
    return AcceptanceResult(accepted=accepted, reasons=reasons or [], holdout=holdout, audit=audit)


class TestUpdateSymlink:
    def test_creates_new_symlink(self, tmp_path):
        target = tmp_path / "NIFTY_20260814.pt"
        target.write_text("model bytes")
        link = tmp_path / "NIFTY_latest.pt"

        _update_symlink(str(link), target.name)

        assert link.is_symlink()
        assert os.readlink(link) == target.name

    def test_replaces_existing_symlink(self, tmp_path):
        old_target = tmp_path / "NIFTY_20260813.pt"
        old_target.write_text("old")
        new_target = tmp_path / "NIFTY_20260814.pt"
        new_target.write_text("new")
        link = tmp_path / "NIFTY_latest.pt"
        os.symlink(old_target.name, link)

        _update_symlink(str(link), new_target.name)

        assert os.readlink(link) == new_target.name


class TestTrainOneSymbolFailureModes:
    def test_no_bhavcopy_data_returns_failure_without_touching_symlink(self, tmp_path):
        with patch.object(run_training_module, "fetch_recent_bhavcopies", return_value=pd.DataFrame()):
            result = train_one_symbol("NIFTY", _tiny_config(), model_dir=str(tmp_path))

        assert result.accepted is False
        assert "no Bhavcopy data" in result.reasons[0]
        assert not os.path.exists(tmp_path / "NIFTY_latest.pt")

    def test_too_few_samples_returns_failure(self, tmp_path):
        # Only one row -- far below the 50-sample minimum.
        bhavcopy = _synthetic_bhavcopy(["2026-08-14"], n_strikes=1)
        with patch.object(run_training_module, "fetch_recent_bhavcopies", return_value=bhavcopy.head(1)):
            result = train_one_symbol("NIFTY", _tiny_config(), model_dir=str(tmp_path))

        assert result.accepted is False
        assert "too few" in result.reasons[0]

    def test_single_trade_date_returns_failure(self, tmp_path):
        """split_by_holdout_date requires >= 2 distinct dates to hold one out.
        n_strikes=30 (60 rows) clears the 50-sample minimum on its own, so
        this isolates the single-date failure specifically."""
        bhavcopy = _synthetic_bhavcopy(["2026-08-14"], n_strikes=30)
        with patch.object(run_training_module, "fetch_recent_bhavcopies", return_value=bhavcopy):
            result = train_one_symbol("NIFTY", _tiny_config(), model_dir=str(tmp_path))

        assert result.accepted is False
        assert "trade date" in result.reasons[0].lower()


class TestTrainOneSymbolAcceptFallback:
    """Mocks check_acceptance_criteria directly -- controls the verdict
    deterministically to test save/symlink logic, independent of whether a
    5-epoch toy training run happens to produce a genuinely good model."""

    def _run(self, tmp_path, accepted: bool, reasons=None):
        bhavcopy = _synthetic_bhavcopy(["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"])
        with patch.object(run_training_module, "fetch_recent_bhavcopies", return_value=bhavcopy), \
             patch.object(run_training_module, "check_acceptance_criteria",
                           return_value=_fake_acceptance(accepted, reasons)):
            return train_one_symbol("NIFTY", _tiny_config(), model_dir=str(tmp_path),
                                     end_date=date(2026, 8, 14))

    def test_accepted_saves_model_and_creates_symlink(self, tmp_path):
        result = self._run(tmp_path, accepted=True)

        assert result.accepted is True
        assert result.model_path is not None
        assert os.path.exists(result.model_path)
        link = tmp_path / "NIFTY_latest.pt"
        assert link.is_symlink()
        assert os.readlink(link) == os.path.basename(result.model_path)

    def test_accepted_result_fields_populated_from_gate(self, tmp_path):
        result = self._run(tmp_path, accepted=True)

        assert result.wings_mae == pytest.approx(0.02)
        assert result.atm_mae == pytest.approx(0.02)
        assert result.overall_mae == pytest.approx(0.02)
        assert result.butterfly_violation_rate == pytest.approx(0.01)
        assert result.holdout_date == "2026-08-14"

    def test_rejected_does_not_save_or_touch_symlink(self, tmp_path):
        result = self._run(tmp_path, accepted=False, reasons=["wings MAE 3.00% >= 2.50% threshold"])

        assert result.accepted is False
        assert result.model_path is None
        assert result.reasons == ["wings MAE 3.00% >= 2.50% threshold"]
        assert not os.path.exists(tmp_path / "NIFTY_latest.pt")
        assert len(list(tmp_path.glob("*.pt"))) == 0

    def test_rejection_preserves_previous_accepted_model(self, tmp_path):
        """The core fallback guarantee: a rejected run must leave a
        pre-existing accepted model (and its symlink) completely untouched."""
        old_model = tmp_path / "NIFTY_20260813.pt"
        old_model.write_text("previous accepted model bytes")
        link = tmp_path / "NIFTY_latest.pt"
        os.symlink(old_model.name, link)

        self._run(tmp_path, accepted=False, reasons=["butterfly violation rate 7.00% >= 5.00% threshold"])

        assert os.readlink(link) == old_model.name
        assert old_model.read_text() == "previous accepted model bytes"

    def test_new_acceptance_replaces_previous_symlink(self, tmp_path):
        old_model = tmp_path / "NIFTY_20260813.pt"
        old_model.write_text("old")
        link = tmp_path / "NIFTY_latest.pt"
        os.symlink(old_model.name, link)

        result = self._run(tmp_path, accepted=True)

        assert os.readlink(link) == os.path.basename(result.model_path)
        assert os.readlink(link) != old_model.name


class TestMainCli:
    def test_all_accepted_returns_exit_code_zero(self, tmp_path):
        with patch.object(run_training_module, "train_one_symbol",
                           return_value=TrainingRunResult(symbol="NIFTY", accepted=True, reasons=[])):
            exit_code = main(["--symbols", "NIFTY", "--model-dir", str(tmp_path)])
        assert exit_code == 0

    def test_any_rejected_returns_nonzero_exit_code(self, tmp_path):
        def fake_train(symbol, config, model_dir, end_date=None):
            accepted = symbol == "NIFTY"
            return TrainingRunResult(symbol=symbol, accepted=accepted,
                                      reasons=[] if accepted else ["wings MAE too high"])

        with patch.object(run_training_module, "train_one_symbol", side_effect=fake_train):
            exit_code = main(["--symbols", "NIFTY", "BANKNIFTY", "--model-dir", str(tmp_path)])
        assert exit_code == 1

    def test_default_symbols_come_from_config(self, tmp_path):
        calls = []

        def fake_train(symbol, config, model_dir, end_date=None):
            calls.append(symbol)
            return TrainingRunResult(symbol=symbol, accepted=True, reasons=[])

        with patch.object(run_training_module, "train_one_symbol", side_effect=fake_train):
            main(["--model-dir", str(tmp_path)])  # no --symbols

        assert calls == PINNConfig().symbols  # ["NIFTY", "BANKNIFTY"]

    def test_seed_override_reaches_config(self, tmp_path):
        captured_configs = []

        def fake_train(symbol, config, model_dir, end_date=None):
            captured_configs.append(config)
            return TrainingRunResult(symbol=symbol, accepted=True, reasons=[])

        with patch.object(run_training_module, "train_one_symbol", side_effect=fake_train):
            main(["--symbols", "NIFTY", "--model-dir", str(tmp_path), "--seed", "123"])

        assert captured_configs[0].seed == 123

    def test_end_date_parsed_from_string(self, tmp_path):
        captured_end_dates = []

        def fake_train(symbol, config, model_dir, end_date=None):
            captured_end_dates.append(end_date)
            return TrainingRunResult(symbol=symbol, accepted=True, reasons=[])

        with patch.object(run_training_module, "train_one_symbol", side_effect=fake_train):
            main(["--symbols", "NIFTY", "--model-dir", str(tmp_path), "--end-date", "2026-08-14"])

        assert captured_end_dates[0] == date(2026, 8, 14)
