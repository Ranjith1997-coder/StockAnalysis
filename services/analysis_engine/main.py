"""
Analysis Engine — Worker Entry Point

Consumes analysis jobs from orchestrator:analysis_jobs Redis Stream,
runs all analysers on each stock, publishes results to analysis:results.

Horizontal scaling: start N processes, all join the same analysis-workers
consumer group. Redis distributes jobs round-robin.
"""

import argparse
import gc
import os
import signal
import sys
import time

import common.constants as constant
from analyser.Analyser import AnalyserOrchestrator
from analyser.Futures_Analyser import FuturesAnalyser
from analyser.VolumeAnalyser import VolumeAnalyser
from analyser.TechnicalAnalyser import TechnicalAnalyser
from analyser.candleStickPatternAnalyser import CandleStickAnalyser
from analyser.IVAnalyser import IVAnalyser
from analyser.PCRAnalyser import PCRAnalyser
from analyser.MaxPainAnalyser import MaxPainAnalyser
from analyser.OIChainAnalyser import OIChainAnalyser
from analyser.GEXAnalyser import GEXAnalyser
from analyser.PanicModeAnalyser import PanicModeAnalyser
from analyser.OptionSellerCompositeAnalyser import OptionSellerCompositeAnalyser
from services.analysis_engine.worker import process_job
from services.common.redis_proxy import RedisProxy
from services.common.version import BUILD_LABEL, GIT_COMMIT, GIT_DIRTY
from common.logging_util import logger


_running = True


def signal_handler(signum, frame):
    global _running
    logger.info(f"[analysis-engine] Received signal {signum}, shutting down...")
    _running = False


def _update_heartbeat(redis: RedisProxy, worker_name: str):
    redis.hset(f"service:registry:analysis-engine:{worker_name}", mapping={
        "name": "analysis-engine",
        "worker": worker_name,
        "pid": str(os.getpid()),
        "status": "healthy",
        "last_heartbeat": str(time.time()),
        "version": BUILD_LABEL,
        "commit": GIT_COMMIT,
        "dirty": str(GIT_DIRTY),
    })
    redis.expire(f"service:registry:analysis-engine:{worker_name}", 120)


def main():
    global _running

    parser = argparse.ArgumentParser(description="StockAnalysis Analysis Engine")
    parser.add_argument("--worker-name", default="worker-1", help="Consumer name for this worker instance")
    args = parser.parse_args()
    worker_name = args.worker_name

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = RedisProxy(redis_url)

    try:
        redis.get("ping")
        logger.info(f"[analysis-engine] Connected to Redis at {redis_url}")
        logger.info(f"[analysis-engine] v{BUILD_LABEL} starting")
    except Exception as e:
        logger.error(f"[analysis-engine] Cannot connect to Redis at {redis_url}: {e}")
        sys.exit(1)

    from services.common.crash_handler import install_crash_handler
    install_crash_handler("analysis-engine")

    try:
        redis.xgroup_create(constant.ANALYSIS_JOBS_GROUP, constant.ANALYSIS_JOBS_STREAM, mkstream=True)
    except Exception:
        pass

    orchestrator = AnalyserOrchestrator()
    orchestrator.register(VolumeAnalyser())
    orchestrator.register(TechnicalAnalyser())
    orchestrator.register(CandleStickAnalyser())
    orchestrator.register(IVAnalyser())
    orchestrator.register(FuturesAnalyser())
    orchestrator.register(PCRAnalyser())
    orchestrator.register(MaxPainAnalyser())
    orchestrator.register(OIChainAnalyser())
    orchestrator.register(GEXAnalyser())
    orchestrator.register(PanicModeAnalyser())
    orchestrator.register(OptionSellerCompositeAnalyser())

    logger.info(
        f"[analysis-engine] Started worker={worker_name}, "
        f"registered {len(orchestrator.analysers)} analysers"
    )

    _update_heartbeat(redis, worker_name)

    heartbeat_counter = 0

    while _running:
        try:
            messages = redis.xreadgroup(
                constant.ANALYSIS_JOBS_GROUP,
                worker_name,
                {constant.ANALYSIS_JOBS_STREAM: ">"},
                count=10,
                block=5000,
            )
        except Exception as e:
            logger.error(f"[analysis-engine] Redis xreadgroup error: {e}")
            time.sleep(2)
            continue

        if not messages:
            heartbeat_counter += 1
            if heartbeat_counter % 6 == 0:
                _update_heartbeat(redis, worker_name)
                gc.collect()
            continue

        entries = messages[0][1] if isinstance(messages, list) and messages else []
        for msg_id, fields in entries:
            if not _running:
                break

            try:
                job_fields = {k: v for k, v in fields.items()}
                result = process_job(redis, orchestrator, job_fields)
                redis.xadd(constant.ANALYSIS_RESULTS_STREAM, result, maxlen=500)
            except Exception as e:
                logger.exception(f"[analysis-engine] Error processing job {msg_id}: {e}")
                redis.xadd(constant.ANALYSIS_RESULTS_STREAM, {
                    "job_id": fields.get("job_id", ""),
                    "cycle_id": fields.get("cycle_id", ""),
                    "symbol": fields.get("symbol", ""),
                    "result": "ERROR",
                    "error": str(e),
                    "timestamp": str(time.time()),
                }, maxlen=500)
            finally:
                try:
                    redis.xack(constant.ANALYSIS_JOBS_STREAM, constant.ANALYSIS_JOBS_GROUP, msg_id)
                except Exception:
                    pass

        heartbeat_counter += 1
        _update_heartbeat(redis, worker_name)
        gc.collect()

    logger.info(f"[analysis-engine] Worker {worker_name} shutting down...")
    redis.hset(f"service:registry:analysis-engine:{worker_name}", mapping={
        "status": "shutdown",
        "last_heartbeat": str(time.time()),
    })
    redis.close()


if __name__ == "__main__":
    main()
