"""Tests for services/analysis_engine/worker.py — process_job()."""

import json
import pytest
from unittest.mock import MagicMock, patch

from tests.conftest import FakeStock


def _fake_stock_with_data(symbol="NIFTY", is_index=True):
    """Return a FakeStock with minimal price data for analysis."""
    import pandas as pd
    import common.shared as shared

    stock = FakeStock(symbol=symbol)
    stock.is_index = is_index
    stock.ltp = 24500.0
    stock.ltp_change_perc = 0.85
    stock._priceData = pd.DataFrame({
        "Close": [24400, 24450, 24500],
        "Volume": [100000, 110000, 120000],
    })
    stock.prevDayOHLCV = {"OPEN": 24400, "HIGH": 24600, "LOW": 24300, "CLOSE": 24450, "VOLUME": 1500000}
    stock.daily_hv = 18.5
    stock.analysis = {
        "Timestamp": None,
        "BULLISH": {},
        "BEARISH": {},
        "NEUTRAL": {},
        "NoOfTrends": 0,
        "PRIORITY_OVERRIDE": None,
    }
    # Populate stock.sensibull_ctx to avoid TypeError on missing keys
    stock.sensibull_ctx = {
        "last_fetch_time": None,
        "current": {},
        "historical_data": pd.DataFrame(),
        "oi_chain_history": [],
        "iv_chart_history": pd.DataFrame(),
        "oi_history": pd.DataFrame(),
    }

    # Stub methods that the worker calls
    stock.reset_analysis = lambda: setattr(stock, 'analysis', {
        "Timestamp": None, "BULLISH": {}, "BEARISH": {},
        "NEUTRAL": {}, "NoOfTrends": 0, "PRIORITY_OVERRIDE": None,
    })
    stock.update_latest_data = lambda: None

    return stock


class TestProcessJob:
    """Tests for process_job()."""

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    @patch("services.analysis_engine.worker.load_sensibull_from_redis")
    @patch("services.analysis_engine.worker.load_zerodha_from_redis")
    def test_success_intraday_trend_found(
        self,
        mock_load_zerodha,
        mock_load_sensibull,
        mock_load_stock,
        patch_app_ctx,
    ):
        """Successful intraday analysis with trend found."""
        import common.shared as shared
        from services.analysis_engine.worker import process_job

        patch_app_ctx.mode = shared.Mode.INTRADAY

        stock = _fake_stock_with_data("RELIANCE", is_index=False)
        mock_load_stock.return_value = stock
        mock_load_sensibull.return_value = True

        redis = MagicMock()
        orchestrator = MagicMock()

        mock_score = MagicMock()
        mock_score.total_score = 150
        mock_score.priority.name = "HIGH"
        mock_score.priority.value = 3

        orchestrator.run_all_intraday.return_value = (True, mock_score)
        orchestrator.generate_analysis_message.return_value = "<b>Bullish</b> RSI divergence"

        job = {
            "job_id": "abc123",
            "cycle_id": "2026-06-28-1",
            "symbol": "RELIANCE",
            "is_index": "false",
            "mode": "intraday",
        }

        result = process_job(redis, orchestrator, job)

        assert result["job_id"] == "abc123"
        assert result["cycle_id"] == "2026-06-28-1"
        assert result["symbol"] == "RELIANCE"
        assert result["is_index"] == "false"
        assert result["result"] == "SUCCESS"
        assert result["trend_found"] == "true"
        assert "Bullish" in result["message"]
        assert result["error"] == ""

        analysis = json.loads(result["analysis_json"])
        assert isinstance(analysis, dict)

        score = json.loads(result["score_result_json"])
        assert score["total_score"] == 150
        assert score["priority"] == "HIGH"

        orchestrator.run_all_intraday.assert_called_once()
        orchestrator.generate_analysis_message.assert_called_once()

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    def test_no_price_data(self, mock_load_stock, patch_app_ctx):
        """Stock not found in Redis → NO_DATA."""
        import common.shared as shared
        from services.analysis_engine.worker import process_job

        patch_app_ctx.mode = shared.Mode.INTRADAY
        mock_load_stock.return_value = None

        redis = MagicMock()
        orchestrator = MagicMock()

        job = {"job_id": "abc", "cycle_id": "c1", "symbol": "MISSING", "is_index": "false", "mode": "intraday"}
        result = process_job(redis, orchestrator, job)

        assert result["result"] == "NO_DATA"
        assert result["trend_found"] == "false"

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    def test_insufficient_price_rows(self, mock_load_stock, patch_app_ctx):
        """Price data with <3 rows (intraday) → NO_DATA."""
        import pandas as pd
        import common.shared as shared
        from services.analysis_engine.worker import process_job

        patch_app_ctx.mode = shared.Mode.INTRADAY

        stock = _fake_stock_with_data("WIPRO", is_index=False)
        stock._priceData = pd.DataFrame({"Close": [100], "Volume": [1000]})  # only 1 row
        mock_load_stock.return_value = stock

        redis = MagicMock()
        orchestrator = MagicMock()

        job = {"job_id": "abc", "cycle_id": "c1", "symbol": "WIPRO", "is_index": "false", "mode": "intraday"}
        result = process_job(redis, orchestrator, job)

        assert result["result"] == "NO_DATA"
        assert result["trend_found"] == "false"
        orchestrator.run_all_intraday.assert_not_called()

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    @patch("services.analysis_engine.worker.load_sensibull_from_redis")
    def test_no_sensibull_data(self, mock_load_sensibull, mock_load_stock, patch_app_ctx):
        """Sensibull data missing → NO_DATA."""
        import common.shared as shared
        from services.analysis_engine.worker import process_job

        patch_app_ctx.mode = shared.Mode.INTRADAY

        stock = _fake_stock_with_data("TCS", is_index=False)
        mock_load_stock.return_value = stock
        mock_load_sensibull.return_value = False

        redis = MagicMock()
        orchestrator = MagicMock()

        job = {"job_id": "abc", "cycle_id": "c1", "symbol": "TCS", "is_index": "false", "mode": "intraday"}
        result = process_job(redis, orchestrator, job)

        assert result["result"] == "NO_DATA"
        assert result["trend_found"] == "false"

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    def test_positional_small_move_skipped(self, mock_load_stock, patch_app_ctx):
        """Positional mode: price move <0.75% → SUCCESS with no trend."""
        import common.shared as shared
        from services.analysis_engine.worker import process_job

        patch_app_ctx.mode = shared.Mode.POSITIONAL

        stock = _fake_stock_with_data("HDFC", is_index=False)
        stock.ltp_change_perc = 0.3  # Below threshold
        mock_load_stock.return_value = stock

        redis = MagicMock()
        orchestrator = MagicMock()

        job = {"job_id": "abc", "cycle_id": "c1", "symbol": "HDFC", "is_index": "false", "mode": "positional"}
        result = process_job(redis, orchestrator, job)

        assert result["result"] == "SUCCESS"
        assert result["trend_found"] == "false"
        orchestrator.run_all_intraday.assert_not_called()
        orchestrator.run_all_positional.assert_not_called()

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    @patch("services.analysis_engine.worker.load_zerodha_from_redis")
    @patch("services.analysis_engine.worker.load_sensibull_from_redis")
    def test_52_week_detected(self, mock_load_sensibull, mock_load_zerodha, mock_load_stock, patch_app_ctx):
        """52-week high detected in analysis → is_52w_high=true."""
        import common.shared as shared
        from services.analysis_engine.worker import process_job

        patch_app_ctx.mode = shared.Mode.INTRADAY

        stock = _fake_stock_with_data("MARUTI", is_index=False)
        mock_load_stock.return_value = stock
        mock_load_sensibull.return_value = True

        redis = MagicMock()
        orchestrator = MagicMock()
        def _mock_run_all_intraday(stock, index=False, use_scoring=True, min_priority=None):
            stock.analysis["NEUTRAL"]["52-week-high"] = {"level": 12000}
            mock_score = MagicMock()
            mock_score.total_score = 100
            mock_score.priority.name = "NORMAL"
            mock_score.priority.value = 2
            return True, mock_score

        orchestrator.run_all_intraday.side_effect = _mock_run_all_intraday
        orchestrator.generate_analysis_message.return_value = "<b>52W High</b> breakout"

        job = {"job_id": "abc", "cycle_id": "c1", "symbol": "MARUTI", "is_index": "false", "mode": "intraday"}
        result = process_job(redis, orchestrator, job)

        assert result["is_52w_high"] == "true"
        assert result["is_52w_low"] == "false"

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    @patch("services.analysis_engine.worker.load_zerodha_from_redis")
    @patch("services.analysis_engine.worker.load_sensibull_from_redis")
    def test_index_analysis(self, mock_load_sensibull, mock_load_zerodha, mock_load_stock, patch_app_ctx):
        """Index stocks use run_all_intraday with index=True."""
        import common.shared as shared
        from services.analysis_engine.worker import process_job

        patch_app_ctx.mode = shared.Mode.INTRADAY

        stock = _fake_stock_with_data("NIFTY", is_index=True)
        mock_load_stock.return_value = stock
        mock_load_sensibull.return_value = True

        redis = MagicMock()
        orchestrator = MagicMock()
        mock_score = MagicMock()
        mock_score.total_score = 0
        mock_score.priority.name = "LOW"
        mock_score.priority.value = 1

        orchestrator.run_all_intraday.return_value = (False, mock_score)

        job = {"job_id": "abc", "cycle_id": "c1", "symbol": "NIFTY", "is_index": "true", "mode": "intraday"}
        result = process_job(redis, orchestrator, job)

        assert result["result"] == "SUCCESS"
        assert result["trend_found"] == "false"
        orchestrator.run_all_intraday.assert_called_once()
        # Verify index=True was passed
        args, kwargs = orchestrator.run_all_intraday.call_args
        assert kwargs.get("index") is True

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    @patch("services.analysis_engine.worker.load_zerodha_from_redis")
    @patch("services.analysis_engine.worker.load_sensibull_from_redis")
    def test_analyser_exception_caught(self, mock_load_sensibull, mock_load_zerodha, mock_load_stock, patch_app_ctx):
        """Exception in analyser → ERROR result."""
        import common.shared as shared
        from services.analysis_engine.worker import process_job

        patch_app_ctx.mode = shared.Mode.INTRADAY

        stock = _fake_stock_with_data("INFY", is_index=False)
        mock_load_stock.return_value = stock
        mock_load_sensibull.return_value = True

        redis = MagicMock()
        orchestrator = MagicMock()
        orchestrator.run_all_intraday.side_effect = ValueError("Bad data")

        job = {"job_id": "abc", "cycle_id": "c1", "symbol": "INFY", "is_index": "false", "mode": "intraday"}
        result = process_job(redis, orchestrator, job)

        assert result["result"] == "ERROR"
        assert "Bad data" in result["error"]
