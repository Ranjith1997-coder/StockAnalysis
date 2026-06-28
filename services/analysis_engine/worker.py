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
from services.common.redis_proxy import RedisProxy
from services.common.stock_loader import (
    load_stock_from_redis,
    load_sensibull_from_redis,
    load_zerodha_from_redis,
)
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

    is_52w_high = "52-week-high" in stock.analysis.get("BULLISH", {})
    is_52w_low = "52-week-low" in stock.analysis.get("BEARISH", {})

    return _result_dict(
        job_id, cycle_id, symbol, is_index,
        "SUCCESS", trend_found, message,
        analysis_json, score_result_json,
        is_52w_high, is_52w_low, "",
        int((time.time() - start) * 1000),
    )
