"""Tests for entry-point functions: _send_report, run_global_cues_report,
run_preopen_report.
"""
import os
import pytest
from unittest.mock import patch, MagicMock, call
from premarket.premarket_report import (
    _send_report,
    run_global_cues_report,
    run_preopen_report,
)

_TELEGRAM = "premarket.premarket_report.TELEGRAM_NOTIFICATIONS.send_notification"
_IS_TRADING = "common.market_calendar.is_trading_day"
_SYS_EXIT = "premarket.premarket_report.sys.exit"


# ── TestSendReport ─────────────────────────────────────────────────────────────

class TestSendReport:
    def test_none_does_not_call_telegram(self):
        with patch(_TELEGRAM) as mock_send:
            _send_report(None)
        mock_send.assert_not_called()

    def test_empty_string_does_not_call_telegram(self):
        with patch(_TELEGRAM) as mock_send:
            _send_report("")
        mock_send.assert_not_called()

    def test_short_report_calls_telegram_once(self):
        with patch(_TELEGRAM) as mock_send:
            _send_report("short report")
        mock_send.assert_called_once()

    def test_short_report_sends_html_parse_mode(self):
        with patch(_TELEGRAM) as mock_send:
            _send_report("test message")
        _, kwargs = mock_send.call_args
        assert kwargs.get("parse_mode") == "HTML"

    def test_report_exactly_4096_chars_calls_telegram_once(self):
        msg = "A" * 4096
        with patch(_TELEGRAM) as mock_send:
            _send_report(msg)
        mock_send.assert_called_once()

    def test_report_over_4096_calls_telegram_multiple_times(self):
        # Build a report > 4096 chars with emoji section headers so it splits
        section = "\n🌍 Section header\n" + "x" * 800
        msg = section * 7  # ~5600 chars
        with patch(_TELEGRAM) as mock_send:
            _send_report(msg)
        assert mock_send.call_count >= 2

    def test_all_chunks_use_html_parse_mode(self):
        section = "\n🌍 Section\n" + "x" * 800
        msg = section * 7
        with patch(_TELEGRAM) as mock_send:
            _send_report(msg)
        for c in mock_send.call_args_list:
            _, kwargs = c
            assert kwargs.get("parse_mode") == "HTML"

    def test_no_chunk_exceeds_4096_chars(self):
        section = "\n🌍 Section\n" + "x" * 800
        msg = section * 7
        with patch(_TELEGRAM) as mock_send:
            _send_report(msg)
        for c in mock_send.call_args_list:
            text = c.args[0] if c.args else c[0][0]
            assert len(text) <= 4096


# ── TestRunGlobalCuesReport ────────────────────────────────────────────────────

class TestRunGlobalCuesReport:
    def _mock_report(self):
        """Return a mock PreMarketReport that produces a known report string."""
        mock = MagicMock()
        mock.generate_global_report.return_value = "Global cues report"
        return mock

    def test_non_production_mode_does_not_check_trading_day(self):
        with patch.dict(os.environ, {"PRODUCTION": "0"}):
            with patch("premarket.premarket_report.PreMarketReport",
                       return_value=self._mock_report()):
                with patch(_TELEGRAM):
                    with patch(_IS_TRADING) as mock_is_trading:
                        run_global_cues_report()
        mock_is_trading.assert_not_called()

    def test_production_mode_non_trading_day_calls_sys_exit(self):
        with patch.dict(os.environ, {"PRODUCTION": "1"}):
            with patch(_IS_TRADING, return_value=False):
                with patch(_SYS_EXIT) as mock_exit:
                    run_global_cues_report()
        mock_exit.assert_called_once_with(0)

    def test_production_mode_trading_day_proceeds_to_generate(self):
        with patch.dict(os.environ, {"PRODUCTION": "1"}):
            with patch(_IS_TRADING, return_value=True):
                mock_rpt = self._mock_report()
                with patch("premarket.premarket_report.PreMarketReport",
                           return_value=mock_rpt):
                    with patch(_TELEGRAM):
                        run_global_cues_report()
        mock_rpt.generate_global_report.assert_called_once()

    def test_returns_report_string_on_success(self):
        with patch.dict(os.environ, {"PRODUCTION": "0"}):
            with patch("premarket.premarket_report.PreMarketReport",
                       return_value=self._mock_report()):
                with patch(_TELEGRAM):
                    result = run_global_cues_report()
        assert result == "Global cues report"

    def test_exception_in_generate_returns_none(self):
        mock_rpt = MagicMock()
        mock_rpt.generate_global_report.side_effect = RuntimeError("network error")
        with patch.dict(os.environ, {"PRODUCTION": "0"}):
            with patch("premarket.premarket_report.PreMarketReport",
                       return_value=mock_rpt):
                result = run_global_cues_report()
        assert result is None

    def test_send_report_called_with_generated_report(self):
        with patch.dict(os.environ, {"PRODUCTION": "0"}):
            with patch("premarket.premarket_report.PreMarketReport",
                       return_value=self._mock_report()):
                with patch(_TELEGRAM) as mock_send:
                    run_global_cues_report()
        mock_send.assert_called_once()


# ── TestRunPreopenReport ───────────────────────────────────────────────────────

class TestRunPreopenReport:
    def _mock_report(self):
        mock = MagicMock()
        mock.generate_preopen_report.return_value = "Pre-open report"
        return mock

    def test_returns_report_string_on_success(self):
        with patch("premarket.premarket_report.PreMarketReport",
                   return_value=self._mock_report()):
            with patch(_TELEGRAM):
                result = run_preopen_report()
        assert result == "Pre-open report"

    def test_generate_preopen_report_called(self):
        mock_rpt = self._mock_report()
        with patch("premarket.premarket_report.PreMarketReport",
                   return_value=mock_rpt):
            with patch(_TELEGRAM):
                run_preopen_report()
        mock_rpt.generate_preopen_report.assert_called_once()

    def test_send_report_called_with_generated_report(self):
        with patch("premarket.premarket_report.PreMarketReport",
                   return_value=self._mock_report()):
            with patch(_TELEGRAM) as mock_send:
                run_preopen_report()
        mock_send.assert_called_once()

    def test_exception_in_generate_returns_none(self):
        mock_rpt = MagicMock()
        mock_rpt.generate_preopen_report.side_effect = RuntimeError("api down")
        with patch("premarket.premarket_report.PreMarketReport",
                   return_value=mock_rpt):
            result = run_preopen_report()
        assert result is None

    def test_exception_does_not_propagate(self):
        mock_rpt = MagicMock()
        mock_rpt.generate_preopen_report.side_effect = RuntimeError("crash")
        with patch("premarket.premarket_report.PreMarketReport",
                   return_value=mock_rpt):
            # Should not raise
            run_preopen_report()
