"""
Analysis Engine — Job Processor

Single-job processor: reconstruct Stock from Redis hashes, run all analysers,
publish result to analysis:results stream.

Designed to run inside a worker process (services/analysis_engine/main.py).
Process-local shared.app_ctx is isolated per worker — no cross-process coupling.
"""

import json
import time

import common.constants as constant
import common.shared as shared
from analyser.Analyser import AnalyserOrchestrator
from analyser.GEXAnalyser import GEXAnalyser
from services.common.redis_proxy import RedisProxy
from services.common.stock_loader import (
    load_stock_from_redis,
    load_sensibull_from_redis,
    load_zerodha_from_redis,
    load_options_live_from_redis,
)
from services.common.serialization import safe_json_dumps, safe_json_loads
from services.common.metrics import incr_stock, set_stock, incr_system, incr_daily
from common.logging_util import logger


def _result_dict(
    job_id: str,
    cycle_id: str,
    symbol: str,
    is_index: bool,
    result: str,
    trend_found: bool,
    message: str,
    analysis_json: str,
    score_result_json: str,
    is_52w_high: bool,
    is_52w_low: bool,
    error: str,
    duration_ms: int,
) -> dict:
    return {
        "job_id": job_id,
        "cycle_id": cycle_id,
        "symbol": symbol,
        "is_index": str(is_index).lower(),
        "result": result,
        "trend_found": str(trend_found).lower(),
        "message": message,
        "analysis_json": analysis_json,
        "score_result_json": score_result_json,
        "is_52w_high": str(is_52w_high).lower(),
        "is_52w_low": str(is_52w_low).lower(),
        "error": error,
        "duration_ms": str(duration_ms),
        "timestamp": str(time.time()),
    }


def _load_gex_state(redis: RedisProxy, orchestrator: AnalyserOrchestrator, stock) -> None:
    """Load previous cycle's GEX state from Redis for cross-cycle analysis.

    Reads `data:gex_state:{symbol}` (persisted by the previous worker cycle)
    and sets:
      - gex_analyser._prev_gex_by_strike[symbol] for GEX_WALL_BREACH
      - stock.options_aggregate["gex_regime"] for regime flip detection
    """
    raw = redis.hgetall(f"data:gex_state:{stock.stock_symbol}")
    if not raw:
        return

    gex_by_strike_raw = safe_json_loads(raw.get("gex_by_strike_json", "{}")) or {}
    gex_by_strike = {float(k): v for k, v in gex_by_strike_raw.items()}
    gex_regime = raw.get("gex_regime", "")

    for analyser in orchestrator.analysers:
        if isinstance(analyser, GEXAnalyser):
            if not hasattr(analyser, "_prev_gex_by_strike"):
                analyser._prev_gex_by_strike = {}
            analyser._prev_gex_by_strike[stock.stock_symbol] = gex_by_strike
            break

    if gex_regime:
        stock.options_aggregate["gex_regime"] = gex_regime


def _persist_gex_state(redis: RedisProxy, stock) -> None:
    """Persist current cycle's GEX state to Redis for next cycle's workers."""
    agg = stock.options_aggregate
    gex_by_strike = agg.get("gex_by_strike", {})
    gex_by_strike_str = {str(k): v for k, v in gex_by_strike.items()}

    mapping = {
        "gex_by_strike_json": safe_json_dumps(gex_by_strike_str),
        "gex_regime": agg.get("gex_regime") or "",
        "gex_total": str(agg.get("gex_total", 0.0)),
        "gex_flip_level": str(agg.get("gex_flip_level") or ""),
    }
    redis.hset(f"data:gex_state:{stock.stock_symbol}", mapping=mapping)


def _record_metrics(sym: str, result: str, duration_ms: int, trend: bool = False, error: str = ""):
    """Record per-stock + system analysis metrics for one job result."""
    incr_stock(sym, "analysis_count")
    incr_system("analysis_runs")
    incr_daily("analysis_runs")
    set_stock(sym,
        last_analysis_result=result,
        last_analysis_duration_ms=str(duration_ms),
        last_analysis_time=str(time.time()),
    )
    if result == "ERROR":
        incr_stock(sym, "analysis_errors")
        incr_system("result_error_count")
    elif result == "NO_DATA":
        incr_system("result_no_data_count")
    elif trend:
        incr_stock(sym, "trends_found")
        incr_system("result_success_count")
    else:
        incr_system("result_success_count")


def process_job(
    redis: RedisProxy,
    orchestrator: AnalyserOrchestrator,
    job_fields: dict,
) -> dict:
    start = time.time()
    job_id = job_fields.get("job_id", "")
    cycle_id = job_fields.get("cycle_id", "")
    symbol = job_fields.get("symbol", "")
    is_index = job_fields.get("is_index", "false").lower() == "true"
    mode_str = job_fields.get("mode", "intraday")

    mode = shared.Mode.POSITIONAL if mode_str == "positional" else shared.Mode.INTRADAY
    shared.app_ctx.mode = mode

    logger.debug(f"[worker] Processing job {job_id}: {symbol} ({mode_str})")

    stock = load_stock_from_redis(redis, symbol, is_index=is_index)
    if stock is None:
        logger.warning(f"[worker] {symbol}: no price data in Redis")
        _record_metrics(symbol, "NO_DATA", int((time.time() - start) * 1000))
        return _result_dict(
            job_id, cycle_id, symbol, is_index,
            "NO_DATA", False, "", "{}", "{}",
            False, False, "",
            int((time.time() - start) * 1000),
        )

    min_rows = 3 if mode == shared.Mode.INTRADAY else 2
    if stock.priceData is None or len(stock.priceData) < min_rows:
        logger.debug(f"[worker] {symbol}: insufficient price data ({len(stock.priceData) if stock.priceData is not None else 0} rows, need {min_rows})")
        _record_metrics(symbol, "NO_DATA", int((time.time() - start) * 1000))
        return _result_dict(
            job_id, cycle_id, symbol, is_index,
            "NO_DATA", False, "", "{}", "{}",
            False, False, "",
            int((time.time() - start) * 1000),
        )

    stock.reset_analysis()
    stock.update_latest_data()

    if mode == shared.Mode.POSITIONAL:
        MIN_POSITIONAL_MOVE_PCT = 0.75
        if stock.ltp_change_perc is not None and abs(stock.ltp_change_perc) < MIN_POSITIONAL_MOVE_PCT:
            logger.debug(f"[worker] {symbol}: skipped — price move {stock.ltp_change_perc:+.2f}% < {MIN_POSITIONAL_MOVE_PCT}%")
            _record_metrics(symbol, "SKIPPED", int((time.time() - start) * 1000))
            return _result_dict(
                job_id, cycle_id, symbol, is_index,
                "SUCCESS", False, "", "{}", "{}",
                False, False, "",
                int((time.time() - start) * 1000),
            )

    if symbol in constant.INDEX_ANALYSIS_EXCLUDE:
        _record_metrics(symbol, "SKIPPED", int((time.time() - start) * 1000))
        return _result_dict(
            job_id, cycle_id, symbol, is_index,
            "SUCCESS", False, "", "{}", "{}",
            False, False, "",
            int((time.time() - start) * 1000),
        )

    sensibull_ok = load_sensibull_from_redis(redis, stock)
    if not sensibull_ok:
        logger.warning(f"[worker] {symbol}: no sensibull data in Redis")
        _record_metrics(symbol, "NO_DATA", int((time.time() - start) * 1000))
        return _result_dict(
            job_id, cycle_id, symbol, is_index,
            "NO_DATA", False, "", "{}", "{}",
            False, False, "",
            int((time.time() - start) * 1000),
        )

    min_rows = 3 if mode == shared.Mode.INTRADAY else 2
    if stock.priceData is None or len(stock.priceData) < min_rows:
        logger.debug(f"[worker] {symbol}: insufficient price data ({len(stock.priceData) if stock.priceData is not None else 0} rows, need {min_rows})")
        return _result_dict(
            job_id, cycle_id, symbol, is_index,
            "NO_DATA", False, "", "{}", "{}",
            False, False, "",
            int((time.time() - start) * 1000),
        )

    stock.reset_analysis()
    stock.update_latest_data()

    if mode == shared.Mode.POSITIONAL:
        MIN_POSITIONAL_MOVE_PCT = 0.75
        if stock.ltp_change_perc is not None and abs(stock.ltp_change_perc) < MIN_POSITIONAL_MOVE_PCT:
            logger.debug(f"[worker] {symbol}: skipped — price move {stock.ltp_change_perc:+.2f}% < {MIN_POSITIONAL_MOVE_PCT}%")
            return _result_dict(
                job_id, cycle_id, symbol, is_index,
                "SUCCESS", False, "", "{}", "{}",
                False, False, "",
                int((time.time() - start) * 1000),
            )

    if symbol in constant.INDEX_ANALYSIS_EXCLUDE:
        return _result_dict(
            job_id, cycle_id, symbol, is_index,
            "SUCCESS", False, "", "{}", "{}",
            False, False, "",
            int((time.time() - start) * 1000),
        )

    sensibull_ok = load_sensibull_from_redis(redis, stock)
    if not sensibull_ok:
        logger.warning(f"[worker] {symbol}: no sensibull data in Redis")
        return _result_dict(
            job_id, cycle_id, symbol, is_index,
            "NO_DATA", False, "", "{}", "{}",
            False, False, "",
            int((time.time() - start) * 1000),
        )

    load_zerodha_from_redis(redis, stock)

    if is_index and symbol in constant.LIVE_OPTIONS_INDICES:
        load_options_live_from_redis(redis, stock)
        _load_gex_state(redis, orchestrator, stock)

    orchestrator.reset_all_constants()

    try:
        if stock.is_index:
            trend_found, score_result = (
                orchestrator.run_all_positional(stock, index=True)
                if mode == shared.Mode.POSITIONAL
                else orchestrator.run_all_intraday(stock, index=True)
            )
        else:
            trend_found, score_result = (
                orchestrator.run_all_positional(stock)
                if mode == shared.Mode.POSITIONAL
                else orchestrator.run_all_intraday(stock)
            )
    except Exception as e:
        logger.exception(f"[worker] {symbol}: analyser error: {e}")
        _record_metrics(symbol, "ERROR", int((time.time() - start) * 1000), error=str(e))
        return _result_dict(
            job_id, cycle_id, symbol, is_index,
            "ERROR", False, "", "{}", "{}",
            False, False, str(e),
            int((time.time() - start) * 1000),
        )

    message = ""
    if trend_found:
        try:
            message = orchestrator.generate_analysis_message(stock)
        except Exception as e:
            logger.error(f"[worker] {symbol}: message generation error: {e}")

    analysis_json = json.dumps(stock.analysis, default=str)
    score_result_json = json.dumps(
        {"total_score": score_result.total_score, "priority": score_result.priority.name,
         "priority_value": score_result.priority.value}
        if score_result else {},
        default=str,
    )

    duration_ms = int((time.time() - start) * 1000)
    _record_metrics(symbol, "SUCCESS", duration_ms, trend=trend_found)

    is_52w_high = "52-week-high" in stock.analysis.get("NEUTRAL", {})
    is_52w_low = "52-week-low" in stock.analysis.get("NEUTRAL", {})

    if is_index and symbol in constant.LIVE_OPTIONS_INDICES:
        _persist_gex_state(redis, stock)

    return _result_dict(
        job_id, cycle_id, symbol, is_index,
        "SUCCESS", trend_found, message,
        analysis_json, score_result_json,
        is_52w_high, is_52w_low, "",
        duration_ms,
    )
