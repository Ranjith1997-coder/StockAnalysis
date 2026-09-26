"""Tests for services/pinn_trainer/main.py.

Scope: the pure/testable scheduling-decision helpers (day-idempotency
checks, the training cycle, alert formatting). The infinite-loop scheduling
functions (_run_schedule, main) are integration-level, same convention as
tests/services/test_paper_trading_main.py -- not unit tested here.
"""

from datetime import date
from unittest.mock import MagicMock, patch

from tools.pinn_volatility.config import PINNConfig
from tools.pinn_volatility.training.run_training import TrainingRunResult
import services.pinn_trainer.main as pinn_trainer_main
from services.pinn_trainer.main import (
    _already_trained_today,
    _all_trained_today,
    _format_result_alert,
    run_training_cycle,
)


def _config(symbols=("NIFTY", "BANKNIFTY")):
    return PINNConfig(symbols=list(symbols))


def _result(accepted=True, holdout_date="2026-09-20", **overrides):
    defaults = dict(
        symbol="NIFTY", accepted=accepted, reasons=[] if accepted else ["wings MAE too high"],
        holdout_date=holdout_date, wings_mae=0.02, atm_mae=0.02, overall_mae=0.02,
        butterfly_violation_rate=0.01, calendar_violation_rate=0.0,
    )
    defaults.update(overrides)
    return TrainingRunResult(**defaults)


class TestAlreadyTrainedToday:
    def test_true_when_train_date_matches_today(self):
        redis = MagicMock()
        redis.hget.return_value = date.today().isoformat()
        assert _already_trained_today(redis, "NIFTY") is True

    def test_false_when_train_date_is_yesterday(self):
        redis = MagicMock()
        redis.hget.return_value = "2020-01-01"
        assert _already_trained_today(redis, "NIFTY") is False

    def test_false_when_no_key_at_all(self):
        redis = MagicMock()
        redis.hget.return_value = None
        assert _already_trained_today(redis, "NIFTY") is False


class TestAllTrainedToday:
    def test_true_when_every_symbol_done(self):
        redis = MagicMock()
        redis.hget.return_value = date.today().isoformat()
        assert _all_trained_today(redis, _config()) is True

    def test_false_when_one_symbol_pending(self):
        redis = MagicMock()
        redis.hget.side_effect = lambda key, field: (
            date.today().isoformat() if key.endswith(":NIFTY") else None
        )
        assert _all_trained_today(redis, _config()) is False


class TestFormatResultAlert:
    def test_none_when_not_yet_reached_gate(self):
        result = _result(holdout_date=None, accepted=False, reasons=["no Bhavcopy data available"])
        assert _format_result_alert("NIFTY", result) is None

    def test_accepted_message_includes_metrics(self):
        result = _result(accepted=True)
        msg = _format_result_alert("NIFTY", result)
        assert "NIFTY" in msg
        assert "wings=2.00%" in msg

    def test_rejected_message_includes_reasons(self):
        result = _result(accepted=False, reasons=["wings MAE 3.00% >= 2.50% threshold"])
        msg = _format_result_alert("NIFTY", result)
        assert "FAILED" in msg
        assert "wings MAE 3.00%" in msg


class TestRunTrainingCycle:
    def test_skips_symbols_already_trained_today(self):
        redis = MagicMock()
        redis.hget.return_value = date.today().isoformat()  # everything already done
        with patch.object(pinn_trainer_main, "train_one_symbol") as mock_train:
            all_done = run_training_cycle(redis, _config())
        mock_train.assert_not_called()
        assert all_done is True

    def test_trains_pending_symbols_and_sends_alerts(self):
        redis = MagicMock()
        redis.hget.return_value = None  # nothing trained yet
        results = {"NIFTY": _result(accepted=True), "BANKNIFTY": _result(accepted=False)}

        def fake_train(symbol, config, redis=None, end_date=None):
            return results[symbol]

        with patch.object(pinn_trainer_main, "train_one_symbol", side_effect=fake_train), \
             patch.object(pinn_trainer_main, "_send_alert") as mock_alert, \
             patch.object(pinn_trainer_main, "_already_trained_today", return_value=False):
            all_done = run_training_cycle(redis, _config())

        assert mock_alert.call_count == 2
        assert all_done is False  # patched _already_trained_today always False

    def test_returns_false_when_a_symbol_is_not_yet_trainable(self):
        redis = MagicMock()
        redis.hget.return_value = None
        pending_result = _result(holdout_date=None, accepted=False,
                                  reasons=["no Bhavcopy data available for the requested window"])

        with patch.object(pinn_trainer_main, "train_one_symbol", return_value=pending_result), \
             patch.object(pinn_trainer_main, "_send_alert") as mock_alert:
            all_done = run_training_cycle(redis, _config(symbols=["NIFTY"]))

        mock_alert.assert_not_called()  # holdout_date=None -> no per-retry alert
        assert all_done is False
