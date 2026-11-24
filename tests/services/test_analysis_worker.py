"""Tests for services/analysis_engine/worker.py — process_job()."""

import json
import pytest
from unittest.mock import MagicMock, patch

from tests.conftest import FakeStock


def _fake_stock_with_data(symbol="NIFTY", is_index=True):
    """Return a FakeStock with minimal price data for analysis."""
    import pandas as pd

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


def _real_stock_with_data(symbol="RELIANCE", is_index=False):
    """Real common.Stock (has a genuine TickStore) with minimal price data."""
    import pandas as pd
    from common.Stock import Stock

    stock = Stock(symbol, symbol, is_index=is_index)
    stock.ltp = 2500.0
    stock.ltp_change_perc = 0.85
    stock._priceData = pd.DataFrame({
        "Close": [2440, 2470, 2500],
        "Volume": [100000, 110000, 120000],
    })
    stock.prevDayOHLCV = {"OPEN": 2440, "HIGH": 2560, "LOW": 2430, "CLOSE": 2450, "VOLUME": 1500000}
    stock.daily_hv = 18.5
    stock.sensibull_ctx = {
        "last_fetch_time": None,
        "current": {},
        "historical_data": pd.DataFrame(),
        "oi_chain_history": [],
        "iv_chart_history": pd.DataFrame(),
        "oi_history": pd.DataFrame(),
    }
    stock.reset_analysis = lambda: None
    stock.update_latest_data = lambda: None
    return stock


def _live_ltp_stock(symbol="NIFTY", is_index=True):
    """Real Stock whose update_latest_data prefers a fresh WS tick, like prod."""
    import pandas as pd
    from common.Stock import Stock

    stock = Stock(symbol, symbol, is_index=is_index)
    stock._priceData = pd.DataFrame({
        "Close": [24_400.0, 24_450.0, 24_500.0],
        "Volume": [1, 2, 3],
    })
    stock.prevDayOHLCV = {"OPEN": 24_400.0, "HIGH": 24_600.0,
                          "LOW": 24_300.0, "CLOSE": 24_450.0}
    stock.daily_hv = 18.5
    stock.sensibull_ctx = {
        "last_fetch_time": None, "current": {}, "historical_data": pd.DataFrame(),
        "oi_chain_history": [], "iv_chart_history": pd.DataFrame(), "oi_history": pd.DataFrame(),
    }
    stock.reset_analysis = lambda: None
    return stock


class TestProcessJob:
    """Tests for process_job()."""

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    @patch("services.analysis_engine.worker.load_sensibull_from_redis")
    @patch("services.analysis_engine.worker.load_zerodha_from_redis")
    @patch("services.analysis_engine.worker.load_equity_tick_from_redis")
    def test_success_intraday_trend_found(
        self,
        mock_load_equity_tick,
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
        assert result["mode"] == "intraday"

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
        assert result["mode"] == "positional"
        orchestrator.run_all_intraday.assert_not_called()
        orchestrator.run_all_positional.assert_not_called()

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    @patch("services.analysis_engine.worker.load_zerodha_from_redis")
    @patch("services.analysis_engine.worker.load_sensibull_from_redis")
    @patch("services.analysis_engine.worker.load_equity_tick_from_redis")
    def test_52_week_detected(self, mock_load_equity_tick, mock_load_sensibull, mock_load_zerodha, mock_load_stock, patch_app_ctx):
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
    @patch("services.analysis_engine.worker.load_equity_tick_from_redis")
    def test_index_analysis(self, mock_load_equity_tick, mock_load_sensibull, mock_load_zerodha, mock_load_stock, patch_app_ctx):
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
    @patch("services.analysis_engine.worker.load_equity_tick_from_redis")
    def test_analyser_exception_caught(self, mock_load_equity_tick, mock_load_sensibull, mock_load_zerodha, mock_load_stock, patch_app_ctx):
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


class TestLoadEquityTickWiring:
    """Regression guard: process_job() must hydrate zerodha_data (total_buy_quantity/
    total_sell_quantity) from data:tick:{symbol} via load_equity_tick_from_redis().

    Previously this loader existed in stock_loader.py but was never imported/called
    by worker.py, so analyze_buy_sell_quantity always saw zero-initialized values
    in the analysis-engine (production) path. See docs/ANALYSER_DATA_SOURCES.md.
    """

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    @patch("services.analysis_engine.worker.load_sensibull_from_redis")
    @patch("services.analysis_engine.worker.load_zerodha_from_redis")
    def test_equity_tick_hydrated_from_redis(
        self, mock_load_zerodha, mock_load_sensibull, mock_load_stock, patch_app_ctx,
    ):
        """load_equity_tick_from_redis is NOT mocked here — verifies the real
        function actually runs and writes into stock.zerodha_data."""
        import common.shared as shared
        from services.analysis_engine.worker import process_job

        patch_app_ctx.mode = shared.Mode.INTRADAY

        stock = _real_stock_with_data("RELIANCE", is_index=False)
        mock_load_stock.return_value = stock
        mock_load_sensibull.return_value = True

        redis = MagicMock()
        redis.hgetall.side_effect = lambda key: (
            {
                "last_price": "2500.5",
                "total_buy_quantity": "45000",
                "total_sell_quantity": "12000",
                "average_traded_price": "2495.0",
            }
            if key == "data:tick:RELIANCE" else {}
        )

        orchestrator = MagicMock()
        mock_score = MagicMock()
        mock_score.total_score = 0
        mock_score.priority.name = "LOW"
        mock_score.priority.value = 1
        orchestrator.run_all_intraday.return_value = (False, mock_score)

        job = {"job_id": "abc", "cycle_id": "c1", "symbol": "RELIANCE", "is_index": "false", "mode": "intraday"}
        process_job(redis, orchestrator, job)

        assert stock.zerodha_data["total_buy_quantity"] == 45000.0
        assert stock.zerodha_data["total_sell_quantity"] == 12000.0
        assert stock.zerodha_data["last_price"] == 2500.5

    @patch("services.analysis_engine.worker.load_stock_from_redis")
    @patch("services.analysis_engine.worker.load_sensibull_from_redis")
    @patch("services.analysis_engine.worker.load_zerodha_from_redis")
    def test_buy_sell_quantity_analyser_fires_when_hydrated(
        self, mock_load_zerodha, mock_load_sensibull, mock_load_stock, patch_app_ctx,
    ):
        """End-to-end: with real tick hydration, analyze_buy_sell_quantity (run via
        the real orchestrator) actually sets BUY_SELL on stock.analysis."""
        import common.shared as shared
        from services.analysis_engine.worker import process_job
        from services.analysis_engine.analyser.Analyser import AnalyserOrchestrator
        from services.analysis_engine.analyser.TechnicalAnalyser import TechnicalAnalyser

        patch_app_ctx.mode = shared.Mode.INTRADAY

        stock = _real_stock_with_data("RELIANCE", is_index=False)
        mock_load_stock.return_value = stock
        mock_load_sensibull.return_value = True

        redis = MagicMock()
        redis.hgetall.side_effect = lambda key: (
            {"total_buy_quantity": "90000", "total_sell_quantity": "10000"}
            if key == "data:tick:RELIANCE" else {}
        )

        orchestrator = AnalyserOrchestrator()
        orchestrator.register(TechnicalAnalyser())

        job = {"job_id": "abc", "cycle_id": "c1", "symbol": "RELIANCE", "is_index": "false", "mode": "intraday"}
        process_job(redis, orchestrator, job)

        assert "BUY_SELL" in stock.analysis["BULLISH"]
        assert stock.analysis["BULLISH"]["BUY_SELL"].buy_quantity == 90000.0


class TestLoadIndiaVix:
    def test_reads_last_price(self):
        from services.analysis_engine.worker import _load_india_vix
        redis = MagicMock()
        redis.hgetall.return_value = {"last_price": "14.25"}
        assert _load_india_vix(redis) == 14.25
        redis.hgetall.assert_called_once_with("data:tick:INDIA_VIX")

    @pytest.mark.parametrize("raw", [{}, None, {"last_price": "0"}, {"last_price": "bad"}])
    def test_missing_or_invalid_returns_none(self, raw):
        from services.analysis_engine.worker import _load_india_vix
        redis = MagicMock()
        redis.hgetall.return_value = raw
        assert _load_india_vix(redis) is None

    def test_redis_error_returns_none(self):
        from services.analysis_engine.worker import _load_india_vix
        redis = MagicMock()
        redis.hgetall.side_effect = ConnectionError("down")
        assert _load_india_vix(redis) is None


class TestLiveTickOrdering:
    @patch("services.analysis_engine.worker.load_stock_from_redis")
    @patch("services.analysis_engine.worker.load_sensibull_from_redis")
    @patch("services.analysis_engine.worker.load_zerodha_from_redis")
    def test_ltp_uses_the_live_tick_not_the_last_bar(
        self, mock_load_zerodha, mock_load_sensibull, mock_load_stock, patch_app_ctx,
    ):
        """The tick must be hydrated before update_latest_data runs (real method)."""
        import common.shared as shared
        from services.analysis_engine.worker import process_job

        patch_app_ctx.mode = shared.Mode.INTRADAY
        stock = _live_ltp_stock("RELIANCE", is_index=False)
        mock_load_stock.return_value = stock
        mock_load_sensibull.return_value = True

        redis = MagicMock()
        redis.hgetall.side_effect = lambda key: (
            {"last_price": "2525.0", "timestamp": str(__import__("time").time())}
            if key == "data:tick:RELIANCE" else {}
        )
        orchestrator = MagicMock()
        score = MagicMock()
        score.total_score = 0
        score.priority.name = "LOW"
        score.priority.value = 1
        orchestrator.run_all_intraday.return_value = (False, score)

        job = {"job_id": "j", "cycle_id": "c", "symbol": "RELIANCE",
               "is_index": "false", "mode": "intraday"}
        process_job(redis, orchestrator, job)

        assert stock.ltp == 2525.0, "ltp fell back to the 5-minute close"


class TestAggregateHydration:
    @patch("services.analysis_engine.worker.load_stock_from_redis")
    @patch("services.analysis_engine.worker.load_sensibull_from_redis")
    @patch("services.analysis_engine.worker.load_zerodha_from_redis")
    def test_options_agg_is_loaded_for_analysers(
        self, mock_load_zerodha, mock_load_sensibull, mock_load_stock, patch_app_ctx,
    ):
        """The worker never loaded data:options_agg, so aggregate-based
        analysers (straddle, walls, POP ladder) silently saw nothing."""
        import common.shared as shared
        from services.analysis_engine.worker import process_job

        patch_app_ctx.mode = shared.Mode.INTRADAY
        stock = _real_stock_with_data("NIFTY", is_index=True)
        mock_load_stock.return_value = stock
        mock_load_sensibull.return_value = True

        agg = {
            "atm_strike": "25500", "atm_straddle_premium": "120.5",
            "expiry": "2026-11-25", "live_pcr": "1.05", "last_updated": "1000",
        }

        def hgetall(key):
            if key.startswith("data:options_agg:"):
                return dict(agg)
            if key.startswith("data:tick:"):
                return {"last_price": "25500.0", "timestamp": "1000"}
            if key.startswith("data:options_live:"):
                return {"25500.0_CE": '{"ltp": 60, "oi": 1}', "25500.0_PE": '{"ltp": 60, "oi": 1}'}
            return {}

        redis = MagicMock()
        redis.hgetall.side_effect = hgetall

        orchestrator = MagicMock()
        score = MagicMock()
        score.total_score = 0
        score.priority.name = "LOW"
        score.priority.value = 1
        orchestrator.run_all_intraday.return_value = (False, score)

        job = {"job_id": "j", "cycle_id": "c", "symbol": "NIFTY",
               "is_index": "true", "mode": "intraday"}
        process_job(redis, orchestrator, job)

        assert stock.options_aggregate["atm_straddle_premium"] == 120.5
        assert stock.options_aggregate["live_pcr"] == 1.05
        assert stock.options_aggregate["expiry"] == "2026-11-25"
