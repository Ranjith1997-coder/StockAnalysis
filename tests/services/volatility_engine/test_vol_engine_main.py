"""Tests for services/volatility_engine/main.py.

Scope: the heartbeat (incl. the `models` field), alert throttling, and the
loop guards that keep the inference/arbitrage threads alive -- the infinite
loops themselves are integration-level, same convention as
tests/services/test_paper_trading_main.py.
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import torch

import services.volatility_engine.main as ve


class TestAlertThrottle:
    # Any timestamp >= ALERT_COOLDOWN_SECONDS, since the "never alerted before"
    # baseline is 0.0 -- a small `now` would look like an in-cooldown first call.
    BASE = 1_000_000.0

    def setup_method(self):
        ve._last_alert_at.clear()

    def test_first_alert_sent_then_throttled(self):
        with patch.object(ve, "_send_alert") as mock_alert:
            assert ve._alert_throttled("k", "msg", now=self.BASE) is True
            assert ve._alert_throttled("k", "msg", now=self.BASE) is False
        mock_alert.assert_called_once()

    def test_sent_again_after_cooldown(self):
        with patch.object(ve, "_send_alert") as mock_alert:
            ve._alert_throttled("k", "msg", now=self.BASE)
            later = self.BASE + ve.ALERT_COOLDOWN_SECONDS + 1
            assert ve._alert_throttled("k", "msg", now=later) is True
        assert mock_alert.call_count == 2

    def test_keys_are_independent(self):
        with patch.object(ve, "_send_alert") as mock_alert:
            assert ve._alert_throttled("a", "m", now=self.BASE) is True
            assert ve._alert_throttled("b", "m", now=self.BASE) is True
        assert mock_alert.call_count == 2


class TestHeartbeat:
    def _mapping(self, redis):
        return redis.hset.call_args[1]["mapping"]

    def test_includes_loaded_models(self):
        redis = MagicMock()
        mm = MagicMock()
        mm.models = {"NIFTY": object(), "BANKNIFTY": object()}
        mm.train_dates = {"NIFTY": "2026-09-25", "BANKNIFTY": "2026-09-24"}

        ve.update_heartbeat(redis, mm)

        mapping = self._mapping(redis)
        assert mapping["models"] == "BANKNIFTY@2026-09-24,NIFTY@2026-09-25"
        assert mapping["status"] == "healthy"
        redis.expire.assert_called_once_with("service:registry:volatility-engine", 120)

    def test_none_when_no_model_loaded(self):
        redis = MagicMock()
        mm = MagicMock()
        mm.models = {}
        mm.train_dates = {}

        ve.update_heartbeat(redis, mm)

        assert self._mapping(redis)["models"] == "none"

    def test_optional_model_manager_omits_field(self):
        redis = MagicMock()
        ve.update_heartbeat(redis)
        assert "models" not in self._mapping(redis)


class TestInferenceLoopGuards:
    def test_outside_market_hours_sleeps_without_evaluating(self):
        redis, mm = MagicMock(), MagicMock()

        def stop(*_a, **_kw):
            ve._running = False

        with patch.object(ve, "is_market_hours", return_value=False), \
             patch.object(ve, "_run_inference_iteration") as mock_iter, \
             patch.object(ve, "_sleep", side_effect=stop) as mock_sleep:
            ve._running = True
            try:
                ve.inference_loop(redis, mm)
            finally:
                ve._running = True

        mock_iter.assert_not_called()
        mock_sleep.assert_called_once_with(ve.OUTSIDE_HOURS_SLEEP_SECONDS)

    def test_iteration_failure_alerts_and_loop_survives(self):
        redis, mm = MagicMock(), MagicMock()

        def stop(*_a, **_kw):
            ve._running = False

        with patch.object(ve, "is_market_hours", return_value=True), \
             patch.object(ve, "_run_inference_iteration", side_effect=RuntimeError("boom")), \
             patch.object(ve, "_alert_throttled") as mock_throttle, \
             patch.object(ve, "_sleep", side_effect=stop):
            ve._running = True
            try:
                ve.inference_loop(redis, mm)  # must not raise
            finally:
                ve._running = True

        assert mock_throttle.called

    def test_hot_reload_failure_is_caught_by_the_iteration_guard(self):
        redis, mm = MagicMock(), MagicMock()
        mm.symbols = ["NIFTY"]
        mm.hot_reload_check.side_effect = RuntimeError("symlink race")

        def stop(*_a, **_kw):
            ve._running = False

        with patch.object(ve, "is_market_hours", return_value=True), \
             patch.object(ve, "_alert_throttled") as mock_throttle, \
             patch.object(ve, "_sleep", side_effect=stop):
            ve._running = True
            try:
                ve.inference_loop(redis, mm)
            finally:
                ve._running = True

        assert mock_throttle.called

    def test_per_symbol_eval_failure_alerts_and_continues(self):
        redis, mm = MagicMock(), MagicMock()
        mm.symbols = ["NIFTY"]
        mm.get_model.return_value = object()

        with patch.object(ve, "evaluate_symbol", side_effect=RuntimeError("bad data")), \
             patch.object(ve, "_alert_throttled") as mock_throttle:
            ve._run_inference_iteration(redis, mm, datetime.now())  # must not raise

        mm.hot_reload_check.assert_called_once()
        assert mock_throttle.called


class TestArbitrageMonitor:
    def test_skips_outside_market_hours(self):
        redis, mm = MagicMock(), MagicMock()
        mm.symbols = ["NIFTY"]
        mm.get_model.return_value = object()

        def stop(*_a, **_kw):
            ve._running = False

        with patch.object(ve, "is_market_hours", return_value=False), \
             patch.object(ve, "audit_model_surface") as mock_audit, \
             patch.object(ve, "_sleep", side_effect=stop):
            ve._running = True
            try:
                ve.arbitrage_monitor(redis, mm)
            finally:
                ve._running = True

        mock_audit.assert_not_called()

    def test_audits_loaded_models_during_market_hours(self):
        redis, mm = MagicMock(), MagicMock()
        model = object()
        mm.symbols = ["NIFTY"]
        mm.get_model.return_value = model
        calls = {"n": 0}

        def stop(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] >= 1:  # first sleep -> audit -> loop exits
                ve._running = False

        with patch.object(ve, "is_market_hours", return_value=True), \
             patch.object(ve, "audit_model_surface") as mock_audit, \
             patch.object(ve, "_sleep", side_effect=stop):
            ve._running = True
            try:
                ve.arbitrage_monitor(redis, mm)
            finally:
                ve._running = True

        mock_audit.assert_called_once_with(redis, "NIFTY", model)


class TestAuditModelSurface:
    def test_writes_status_and_returns_violation_rate(self):
        redis = MagicMock()
        g = torch.tensor([1.0, -1.0, 1.0, 1.0, 1.0])  # 1/5 negative

        with patch.object(ve, "sample_collocation", return_value=torch.zeros(5, 2)), \
             patch.object(ve, "durrleman_density", return_value=g):
            rate = ve.audit_model_surface(redis, "NIFTY", MagicMock())

        assert abs(rate - 0.2) < 1e-6
        redis.hset.assert_called_once()
        assert redis.hset.call_args[0][0] == "pinn:arbitrage_status:NIFTY"
        assert "checked_at" in redis.hset.call_args[1]["mapping"]

    def test_no_write_when_no_violations(self):
        redis = MagicMock()
        g = torch.ones(4)

        with patch.object(ve, "sample_collocation", return_value=torch.zeros(4, 2)), \
             patch.object(ve, "durrleman_density", return_value=g):
            rate = ve.audit_model_surface(redis, "NIFTY", MagicMock())

        assert rate == 0.0
        redis.hset.assert_not_called()
