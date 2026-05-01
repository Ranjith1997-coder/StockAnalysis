"""Tests for analyser/Analyser.py — BaseAnalyzer and AnalyserOrchestrator."""
import pytest
from unittest.mock import MagicMock, patch

from analyser.Analyser import BaseAnalyzer, AnalyserOrchestrator
import common.shared as shared
from tests.analyser.conftest import make_stock, patch_ctx


# ── Minimal concrete analyser for decorator tests ─────────────────────────────

class _SimpleAnalyser(BaseAnalyzer):
    def __init__(self):
        super().__init__()
        self.analyserName = "Simple"
        self.intraday_called = False
        self.positional_called = False
        self.both_called = False
        self.idx_intraday_called = False
        self.idx_positional_called = False

    def reset_constants(self, is_index=False):
        pass

    @BaseAnalyzer.intraday
    def _intraday_only(self, stock):
        self.intraday_called = True
        return True

    @BaseAnalyzer.positional
    def _positional_only(self, stock):
        self.positional_called = True
        return True

    @BaseAnalyzer.both
    def _both_modes(self, stock):
        self.both_called = True
        return True

    @BaseAnalyzer.index_intraday
    def _index_intraday(self, stock):
        self.idx_intraday_called = True
        return True

    @BaseAnalyzer.index_positional
    def _index_positional(self, stock):
        self.idx_positional_called = True
        return True


class TestDecoratorMarking:
    def test_intraday_flag_set(self):
        a = _SimpleAnalyser()
        assert any(getattr(m, "_is_intraday", False) for m in a._intraday_methods)

    def test_positional_flag_set(self):
        a = _SimpleAnalyser()
        assert any(getattr(m, "_is_positional", False) for m in a._positional_methods)

    def test_both_decorator_marks_intraday(self):
        assert getattr(_SimpleAnalyser._both_modes, "_is_intraday", False) is True

    def test_both_decorator_marks_positional(self):
        assert getattr(_SimpleAnalyser._both_modes, "_is_positional", False) is True

    def test_index_intraday_flag_set(self):
        assert getattr(_SimpleAnalyser._index_intraday, "_is_index_intraday", False) is True

    def test_index_positional_flag_set(self):
        assert getattr(_SimpleAnalyser._index_positional, "_is_index_positional", False) is True

    def test_pure_intraday_not_positional(self):
        assert not getattr(_SimpleAnalyser._intraday_only, "_is_positional", False)

    def test_pure_positional_not_intraday(self):
        assert not getattr(_SimpleAnalyser._positional_only, "_is_intraday", False)


class TestRunAllIntraday:
    def test_calls_intraday_and_both_methods(self):
        a = _SimpleAnalyser()
        s = make_stock()
        a.run_all_intraday_analyses(s)
        assert a.intraday_called is True
        assert a.both_called is True

    def test_does_not_call_positional_only(self):
        a = _SimpleAnalyser()
        s = make_stock()
        a.run_all_intraday_analyses(s)
        assert a.positional_called is False

    def test_returns_true_when_any_method_returns_true(self):
        a = _SimpleAnalyser()
        result = a.run_all_intraday_analyses(make_stock())
        assert result is True


class TestRunAllPositional:
    def test_calls_positional_and_both_methods(self):
        a = _SimpleAnalyser()
        s = make_stock()
        a.run_all_positional_analyses(s)
        assert a.positional_called is True
        assert a.both_called is True

    def test_does_not_call_intraday_only(self):
        a = _SimpleAnalyser()
        a.run_all_positional_analyses(make_stock())
        assert a.intraday_called is False

    def test_returns_false_when_all_return_false(self):
        class _FalseAnalyser(BaseAnalyzer):
            def reset_constants(self, is_index=False): pass
            @BaseAnalyzer.positional
            def _always_false(self, stock): return False

        a = _FalseAnalyser()
        assert a.run_all_positional_analyses(make_stock()) is False


class TestRunAllIndex:
    def test_run_all_index_intraday(self):
        a = _SimpleAnalyser()
        a.run_all_index_intraday_analyses(make_stock())
        assert a.idx_intraday_called is True
        assert a.idx_positional_called is False

    def test_run_all_index_positional(self):
        a = _SimpleAnalyser()
        a.run_all_index_positional_analyses(make_stock())
        assert a.idx_positional_called is True
        assert a.idx_intraday_called is False


class TestAnalyserOrchestrator:
    def test_register_accepts_base_analyser(self):
        orch = AnalyserOrchestrator()
        orch.register(_SimpleAnalyser())
        assert len(orch.analysers) == 1

    def test_register_rejects_non_analyser(self):
        orch = AnalyserOrchestrator()
        with pytest.raises(TypeError):
            orch.register(object())

    def test_run_all_intraday_calls_registered(self):
        with patch_ctx(shared.Mode.INTRADAY):
            orch = AnalyserOrchestrator()
            mock_ctx = MagicMock()
            mock_ctx.signal_bus = None
            a = _SimpleAnalyser()
            orch.register(a)
            stock = make_stock()
            with patch("common.shared.app_ctx", mock_ctx):
                mock_ctx.mode = shared.Mode.INTRADAY
                from common.scoring import NotificationPriority
                orch.run_all_intraday(stock, use_scoring=False)
            assert a.intraday_called or a.both_called

    def test_run_all_positional_calls_registered(self):
        mock_ctx = MagicMock()
        mock_ctx.mode = shared.Mode.POSITIONAL
        mock_ctx.signal_bus = None
        orch = AnalyserOrchestrator()
        a = _SimpleAnalyser()
        orch.register(a)
        with patch("common.shared.app_ctx", mock_ctx):
            orch.run_all_positional(make_stock(), use_scoring=False)
        assert a.positional_called or a.both_called

    def test_reset_all_constants_calls_each_analyser(self):
        orch = AnalyserOrchestrator()
        reset_called = []

        class _Tracker(BaseAnalyzer):
            def reset_constants(self, is_index=False):
                reset_called.append(True)

        orch.register(_Tracker())
        orch.register(_Tracker())
        orch.reset_all_constants()
        assert len(reset_called) == 2
