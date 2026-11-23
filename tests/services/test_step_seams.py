"""The extracted step seams must still be called by the real loops (T5.1).

These are the "old path still works" tests the refactor requires: each loop is
run for one iteration against a stub and must invoke the seam exactly once.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.paper_trading import main as pt_main
from services.signal_intelligence import main as si_main


@pytest.fixture(autouse=True)
def _reset_flags():
    yield
    pt_main._running = True
    si_main._running = True


def _stop_after_first(call_log):
    def _seam(*args, **kwargs):
        call_log.append(args)
        pt_main._running = False
        return 0
    return _seam


def test_mtm_engine_calls_mtm_once(monkeypatch):
    calls = []
    monkeypatch.setattr(pt_main, "mtm_once", _stop_after_first(calls))
    monkeypatch.setattr(pt_main.time, "sleep", lambda _s: None)

    pt_main.mtm_engine(MagicMock())

    assert len(calls) == 1


def test_strategy_processor_calls_drain_signal_queues(monkeypatch):
    calls = []
    monkeypatch.setattr(pt_main, "drain_signal_queues", _stop_after_first(calls))
    monkeypatch.setattr(pt_main.time, "sleep", lambda _s: None)

    pt_main.strategy_processor(MagicMock(), MagicMock())

    assert len(calls) == 1


def test_analysis_consumer_calls_process_analysis_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(pt_main, "process_analysis_fields", _stop_after_first(calls))
    redis = MagicMock()
    redis.xreadgroup.return_value = [["analysis:results", [("1-0", {"symbol": "NIFTY"})]]]

    pt_main.analysis_consumer(redis)

    assert len(calls) == 1
    assert calls[0][1] == {"symbol": "NIFTY"}
    redis.xack.assert_called_once()


def test_confluence_consumer_calls_process_confluence_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(pt_main, "process_confluence_fields", _stop_after_first(calls))
    redis = MagicMock()
    redis.xreadgroup.return_value = [["signals:confluence", [("2-0", {"symbol": "NIFTY"})]]]

    pt_main.confluence_consumer(redis)

    assert len(calls) == 1
    assert calls[0][0] == {"symbol": "NIFTY"}


def test_pinn_consumer_calls_process_pinn_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(pt_main, "process_pinn_fields", _stop_after_first(calls))
    redis = MagicMock()
    redis.xreadgroup.return_value = [["pinn:signals", [("3-0", {"symbol": "NIFTY"})]]]

    pt_main.pinn_signal_consumer(redis)

    assert len(calls) == 1


def test_drain_handles_one_exit_and_one_entry(monkeypatch):
    exits, entries = [], []
    monkeypatch.setattr(pt_main, "_handle_exit_signal", lambda _r, s: exits.append(s))
    monkeypatch.setattr(pt_main, "_handle_entry_signal", lambda _r, _c, s: entries.append(s))
    pt_main.exit_queue.put("exit-1")
    pt_main.entry_queue.put("entry-1")

    handled = pt_main.drain_signal_queues(MagicMock(), MagicMock())

    assert handled == 2
    assert exits == ["exit-1"]
    assert entries == ["entry-1"]


def test_drain_returns_zero_when_idle(monkeypatch):
    monkeypatch.setattr(pt_main, "_handle_exit_signal", MagicMock())
    monkeypatch.setattr(pt_main, "_handle_entry_signal", MagicMock())
    pt_main._drain_queues_for_test = True

    assert pt_main.drain_signal_queues(MagicMock(), MagicMock()) == 0


def test_drain_is_one_signal_per_queue_per_call(monkeypatch):
    entries = []
    monkeypatch.setattr(pt_main, "_handle_entry_signal", lambda _r, _c, s: entries.append(s))
    monkeypatch.setattr(pt_main, "_handle_exit_signal", MagicMock())
    pt_main.entry_queue.put("a")
    pt_main.entry_queue.put("b")

    assert pt_main.drain_signal_queues(MagicMock(), MagicMock()) == 1
    assert pt_main.drain_signal_queues(MagicMock(), MagicMock()) == 1
    assert entries == ["a", "b"]


def test_process_analysis_fields_queues_parsed_signals(monkeypatch):
    monkeypatch.setattr(pt_main, "parse_analysis_result", lambda _f: (["E"], ["X"]))
    try:
        pt_main.process_analysis_fields(MagicMock(), {"symbol": "NIFTY"})
        assert pt_main.entry_queue.get_nowait() == "E"
        assert pt_main.exit_queue.get_nowait() == "X"
    finally:
        for q in (pt_main.entry_queue, pt_main.exit_queue):
            while not q.empty():
                q.get_nowait()


def test_process_confluence_fields_queues_only_a_signal(monkeypatch):
    monkeypatch.setattr(pt_main, "parse_confluence_message", lambda _f: None)
    assert pt_main.process_confluence_fields({"x": "1"}) is None
    assert pt_main.entry_queue.empty()

    monkeypatch.setattr(pt_main, "parse_confluence_message", lambda _f: "SIG")
    assert pt_main.process_confluence_fields({"x": "1"}) == "SIG"
    assert pt_main.entry_queue.get_nowait() == "SIG"


def test_handle_signal_reconstructs_and_correlates(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(si_main, "reconstruct_signal", lambda _f: sentinel)
    correlator = MagicMock()

    result = si_main.handle_signal(MagicMock(), correlator, {"symbol": "NIFTY"})

    assert result is sentinel
    correlator.on_signal.assert_called_once_with(sentinel)
