"""Integration: the volatility-engine iteration survives bad inputs (T4.6).

The inference loop is guarded twice (per-symbol try/except and a last-resort
try/except) because a single model or a transient Redis shape error must not
silently stop signal generation while the heartbeat still says "healthy".
"""
from __future__ import annotations

from datetime import datetime

import pytest

from services.volatility_engine import main as vol_main

pytestmark = pytest.mark.integration


class _ModelManager:
    def __init__(self, symbols: tuple[str, ...] = (), model=None) -> None:
        self.symbols = symbols
        self._model = model
        self.reloads = 0

    def hot_reload_check(self) -> None:
        self.reloads += 1

    def get_model(self, _symbol: str):
        return self._model


@pytest.fixture(autouse=True)
def _no_alerts(monkeypatch):
    monkeypatch.setattr(vol_main, "_alert_throttled", lambda *_a, **_k: None)


def test_iteration_without_models_is_a_healthy_noop(fake_redis) -> None:
    manager = _ModelManager(symbols=("NIFTY",), model=None)

    vol_main._run_inference_iteration(fake_redis, manager, datetime.now())

    assert manager.reloads == 1
    assert fake_redis.hgetall("pinn:zscore:NIFTY") == {}


def test_one_bad_symbol_does_not_stop_the_iteration(fake_redis, monkeypatch) -> None:
    calls: list[str] = []

    def explode(symbol, *_args, **_kwargs):
        calls.append(symbol)
        raise RuntimeError("surface rebuild failed")

    monkeypatch.setattr(vol_main, "evaluate_symbol", explode)
    manager = _ModelManager(symbols=("NIFTY", "BANKNIFTY"), model=object())

    vol_main._run_inference_iteration(fake_redis, manager, datetime.now())

    assert calls == ["NIFTY", "BANKNIFTY"], (
        "the first symbol's failure aborted the loop instead of being contained"
    )


def test_empty_symbol_list_touches_nothing(fake_redis) -> None:
    manager = _ModelManager(symbols=(), model=object())
    vol_main._run_inference_iteration(fake_redis, manager, datetime.now())
    assert manager.reloads == 1
