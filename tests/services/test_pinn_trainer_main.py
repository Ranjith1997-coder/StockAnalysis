"""Tests for services/pinn_trainer/main.py.

Scope: the pure/testable scheduling-decision helpers (day-idempotency
checks, the training cycle, alert formatting). The infinite-loop scheduling
functions (_run_schedule, main) are integration-level, same convention as
tests/services/test_paper_trading_main.py -- not unit tested here.
"""

from datetime import date, datetime
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


class TestRunScheduleGuards:
    """The scheduling loop itself is integration-level (see module docstring),
    but its two safety guards exist to prevent silent failure modes and are
    worth driving deterministically: weekend/holiday alert noise, and a dead
    schedule thread while the heartbeat keeps reporting healthy."""

    FAKE_NOW = datetime(2026, 9, 25, 21, 30)  # Friday, just after TRAIN_TIME

    def _drive(self, redis, *, trading_day, cycle_side_effect, iterations):
        """Run _run_schedule at FAKE_NOW, stopping the loop after `iterations`
        sleeps so the test terminates."""
        sleeps = {"n": 0}

        def stop_after(*args, **kwargs):
            sleeps["n"] += 1
            if sleeps["n"] >= iterations:
                pinn_trainer_main._running = False

        mock_cycle = MagicMock(side_effect=cycle_side_effect)
        with patch.object(pinn_trainer_main, "is_trading_day", return_value=trading_day), \
             patch.object(pinn_trainer_main, "_all_trained_today", return_value=False), \
             patch.object(pinn_trainer_main, "run_training_cycle", mock_cycle), \
             patch.object(pinn_trainer_main, "_send_alert") as mock_alert, \
             patch.object(pinn_trainer_main, "_sleep_until_midnight", side_effect=stop_after), \
             patch.object(pinn_trainer_main, "_sleep_responsive", side_effect=stop_after), \
             patch.object(pinn_trainer_main, "datetime") as mock_dt:
            mock_dt.now.return_value = self.FAKE_NOW
            pinn_trainer_main._running = True
            try:
                pinn_trainer_main._run_schedule(redis, _config(symbols=["NIFTY"]))
            finally:
                pinn_trainer_main._running = True
        return mock_cycle, mock_alert

    def test_non_trading_day_skips_training_and_alerts(self):
        redis = MagicMock()
        mock_cycle, mock_alert = self._drive(
            redis, trading_day=False, cycle_side_effect=None, iterations=1,
        )
        mock_cycle.assert_not_called()
        mock_alert.assert_not_called()

    def test_training_exception_alerts_once_and_keeps_retrying(self):
        redis = MagicMock()
        mock_cycle, mock_alert = self._drive(
            redis, trading_day=True,
            cycle_side_effect=RuntimeError("torch exploded"), iterations=2,
        )
        assert mock_cycle.call_count == 2  # the loop survived and retried
        error_alerts = [
            c.args[0] for c in mock_alert.call_args_list if "unexpected error" in c.args[0]
        ]
        assert len(error_alerts) == 1  # once/day, not once per retry
