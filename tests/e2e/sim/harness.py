"""In-process trading-day harness (T5.6).

Drives the real publisher, recorder, analysis worker, signal router, paper
trading entry/MTM/exit and capture flush against fakeredis with a frozen clock.
Only two things are stubbed, both external: SPAN margin (``span.py``) and the
Telegram/Ledger side effects (no network, tmp SQLite).
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import time_machine

import common.constants as constant
import common.shared as shared
from services.analysis_engine.analyser.Analyser import AnalyserOrchestrator
from services.analysis_engine.analyser.Futures_Analyser import FuturesAnalyser
from services.analysis_engine.analyser.GEXAnalyser import GEXAnalyser
from services.analysis_engine.analyser.IVAnalyser import IVAnalyser
from services.analysis_engine.analyser.MaxPainAnalyser import MaxPainAnalyser
from services.analysis_engine.analyser.OIChainAnalyser import OIChainAnalyser
from services.analysis_engine.analyser.OptionSellerCompositeAnalyser import (
    OptionSellerCompositeAnalyser,
)
from services.analysis_engine.analyser.PanicModeAnalyser import PanicModeAnalyser
from services.analysis_engine.analyser.PCRAnalyser import PCRAnalyser
from services.analysis_engine.analyser.TechnicalAnalyser import TechnicalAnalyser
from services.analysis_engine.analyser.VolumeAnalyser import VolumeAnalyser
from services.analysis_engine.analyser.candleStickPatternAnalyser import CandleStickAnalyser
from services.analysis_engine.worker import process_job
from services.common import metrics
from services.market_data.chain_recorder import ChainRecorder
from services.market_data.snapshot_publisher import SnapshotPublisher
from services.paper_trading import ledger
from services.paper_trading import main as pt_main
from services.paper_trading.models import (
    ACCOUNT_KEY,
    PaperAccount,
    TRADES_STREAM,
    daily_pnl_key,
)
from services.paper_trading.signal_router import EntrySignal

from .config import Scenario
from .context import BarSeries, ContextPublisher
from .feed import FakeFeed
from .market import MarketGenerator
from .result import RunResult
from .span import FakeSpanCalculator

ANALYSIS_INTERVAL = 310
HEARTBEAT_INTERVAL = 60
MTM_INTERVAL = 3
FLUSH_INTERVAL = 300
CHAIN_INTERVAL = 30
INDEX_INTERVAL = 10


def _local_instant(moment: datetime) -> datetime:
    """Aware UTC instant whose *local* rendering is ``moment``.

    time-machine takes an aware datetime as the absolute instant; the services
    read ``datetime.now()`` in host-local time. Converting via the host timezone
    keeps the simulated wall clock at the scenario time on any CI host.
    """
    return moment.astimezone(timezone.utc)


class _TelegramStub:
    is_production = False
    is_intraday = True

    def __init__(self) -> None:
        self.entries: list = []
        self.exits: list = []

    def send_live_options_notification(self, *_args, **_kwargs) -> None:
        return None


class _LogCapture(logging.Handler):
    """Collects WARNING+ from the application logger tree.

    The services' loggers set ``propagate = False``, so a handler on the root
    logger receives nothing; this is attached to the ``SA`` logger itself.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[dict] = []
        self._seen: set[int] = set()

    def emit(self, record: logging.LogRecord) -> None:
        if id(record) in self._seen:
            return
        self._seen.add(id(record))
        self.records.append({
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        })

    def attach(self) -> None:
        """Attach to every existing ``SA*`` logger (they set propagate=False)."""
        self._attached: list[logging.Logger] = []
        for name, obj in list(logging.Logger.manager.loggerDict.items()):
            if not isinstance(obj, logging.Logger):
                continue
            if name == "SA" or name.startswith("SA."):
                obj.addHandler(self)
                self._attached.append(obj)
        root = logging.getLogger()
        root.addHandler(self)
        self._attached.append(root)

    def detach(self) -> None:
        for logger in getattr(self, "_attached", []):
            logger.removeHandler(self)
        self._attached = []


def build_orchestrator() -> AnalyserOrchestrator:
    orchestrator = AnalyserOrchestrator()
    for analyser in (
        VolumeAnalyser(), TechnicalAnalyser(), CandleStickAnalyser(), IVAnalyser(),
        FuturesAnalyser(), PCRAnalyser(), MaxPainAnalyser(), OIChainAnalyser(),
        GEXAnalyser(), PanicModeAnalyser(), OptionSellerCompositeAnalyser(),
    ):
        orchestrator.register(analyser)
    return orchestrator


class Harness:
    def __init__(self, scenario: Scenario, redis, capture_root: Path | None = None) -> None:
        self.scenario = scenario
        self.redis = redis
        self._temp_dirs: list[Path] = []
        self._clock_offset = 0
        self.rng = np.random.default_rng(scenario.seed)
        self.generator = MarketGenerator(scenario, self.rng)
        self.feed = FakeFeed(scenario, self.generator)
        self.bars = BarSeries()
        self.context = ContextPublisher(redis, scenario, self.feed)
        if capture_root is None:
            temp = Path(tempfile.mkdtemp(prefix="e2e-capture-"))
            self._temp_dirs.append(temp)
            capture_root = temp / "options_history"
        root = capture_root
        self.recorder = ChainRecorder(self.feed.objects, root=root)
        self.publisher = SnapshotPublisher(redis, [], self.feed.objects)
        self.span = FakeSpanCalculator(scenario)
        self.orchestrator = build_orchestrator()
        self.telegram = _TelegramStub()
        self.logs = _LogCapture()

        self._last_spot: dict[str, float] = {}
        self._open_ids: set[str] = set()
        self._counter = 0
        self._heartbeat_keys: set[str] = set()
        self._errors: list[str] = []
        self._pricing_issues: list[dict] = []
        self._heartbeat_violations: list[str] = []
        self._capture_calls = {"chain": 0, "futures": 0, "index": 0}
        self._faults_seen: set[int] = set()

    # ── main loop ─────────────────────────────────────────────────────────

    def run(self) -> RunResult:
        scenario = self.scenario
        start = scenario.session_start
        clock = time_machine.travel(_local_instant(start), tick=False)
        with clock as traveller, self._environment():
            self.logs.attach()
            try:
                now = start
                elapsed = 0
                while elapsed < scenario.session_seconds:
                    step = min(scenario.step_seconds, scenario.session_seconds - elapsed)
                    elapsed += step
                    now = start + timedelta(seconds=elapsed + self._clock_offset)
                    traveller.move_to(_local_instant(now))

                    self._apply_faults(elapsed)
                    state = replace(self.generator.step(step), t=now)

                    self._state = state
                    self._market_step(now, state, elapsed)

                    if elapsed % HEARTBEAT_INTERVAL < step:
                        self._write_heartbeats()
                        self._sample_heartbeats(now)
                    if elapsed % ANALYSIS_INTERVAL < step:
                        self._analysis_cycle(now)

                    self._fire_scripted_signals(elapsed, now, state)
                    self._sync_state(now)
            finally:
                self.recorder._flush_all()
                self.logs.detach()
                for directory in self._temp_dirs:
                    shutil.rmtree(directory, ignore_errors=True)

        return self._build_result(now)

    # ── steps ─────────────────────────────────────────────────────────────

    def _market_step(self, now: datetime, state, elapsed: int) -> None:
        if self._fault_active("feed_gap", elapsed):
            return

        self.feed.push(state)
        for symbol in self.scenario.symbols:
            spot = state.spot[symbol]
            self.bars.add_option_bar(
                symbol, now, spot, self._last_spot.get(symbol),
                volume=60_000 + elapsed,
            )
            self._last_spot[symbol] = spot

        if not self._fault_active("stale_aggregate", elapsed):
            self.publisher._publish_all()

        if not self.scenario.trading_day:
            return
        self.recorder._capture_futures(now)
        self._capture_calls["futures"] += 1
        if elapsed % CHAIN_INTERVAL < self.scenario.step_seconds:
            self.recorder._capture_chain(now)
            self._capture_calls["chain"] += 1
        if elapsed % INDEX_INTERVAL < self.scenario.step_seconds:
            self.recorder._capture_index(now)
            self._capture_calls["index"] += 1
        if (elapsed % FLUSH_INTERVAL < self.scenario.step_seconds
                and not self._fault_active("disk_low", elapsed)):
            self.recorder._flush_all()

        if elapsed % MTM_INTERVAL < self.scenario.step_seconds:
            try:
                pt_main.mtm_once(self.redis, now, self._spot_tracker)
            except Exception as exc:      # noqa: BLE001 - recorded as a run error
                self._errors.append(f"mtm @{elapsed}s: {exc}")

    def _analysis_cycle(self, now: datetime) -> None:
        self._publish_analysis_inputs(now)
        for symbol in self.scenario.symbols:
            self._counter += 1
            fields = {
                "job_id": f"e2e-{symbol}-{self._counter}",
                "cycle_id": str(int(now.timestamp())),
                "symbol": symbol,
                "is_index": "true",
                "mode": "intraday",
            }
            try:
                result = process_job(self.redis, self.orchestrator, fields)
            except Exception as exc:      # noqa: BLE001
                self._errors.append(f"analysis {symbol}: {exc}")
                continue
            self._analysis_results.append(result)
            self.redis.xadd(constant.ANALYSIS_RESULTS_STREAM, result, maxlen=500)
            try:
                pt_main.process_analysis_fields(self.redis, result)
                pt_main.drain_signal_queues(self.redis, self.span)
            except Exception as exc:      # noqa: BLE001
                self._errors.append(f"paper analysis {symbol}: {exc}")

    def _publish_analysis_inputs(self, now: datetime) -> None:
        state = self._state
        for symbol in self.scenario.symbols:
            self.context.publish_price(symbol, self.bars, state)
            self.context.publish_zerodha(symbol, self.bars, state)
            if not self._fault_active("sensibull_5xx", self._elapsed):
                self.context.publish_sensibull(symbol, state)

    # ── scripted signals ──────────────────────────────────────────────────

    def _fire_scripted_signals(self, elapsed: int, now: datetime, state) -> None:
        for at, spec in self.scenario.signal_times():
            if not (elapsed - self.scenario.step_seconds < at <= elapsed):
                continue
            spot = state.spot[spec.symbol]
            gap = self.scenario.strike_gap[spec.symbol]
            atm = self.feed.atm_strike(spec.symbol, spot)
            signal = EntrySignal(
                strategy=spec.strategy,
                symbol=spec.symbol,
                direction=spec.direction,
                put_wall_strike=spec.put_wall_strike or float(atm - 3 * gap),
                call_wall_strike=spec.call_wall_strike or float(atm + 3 * gap),
                sr_level=spec.sr_level,
                level=spec.level,
                score=spec.score,
                signal_source=spec.source,
                mode="intraday",
                expiry=self.scenario.expiry[spec.symbol].isoformat(),
            )
            if self._fault_active("redis_down", elapsed) or not self.scenario.trading_day:
                continue
            before = {p.position_id for p in pt_main.load_open_positions(self.redis)}
            try:
                pt_main._handle_entry_signal(self.redis, self.span, signal)
            except Exception as exc:      # noqa: BLE001
                self._errors.append(f"entry {spec.symbol}: {exc}")
            after = {p.position_id for p in pt_main.load_open_positions(self.redis)}
            self._signal_attempts.append({
                "time": now.time(),
                "symbol": spec.symbol,
                "expected": spec.expect_entry,
                "opened": bool(after - before),
            })
            if spec.expect_entry and after == before:
                self._errors.append(f"entry {spec.symbol} at {now.time()} did not open")
            self._sync_state(now)

    # ── state collection ──────────────────────────────────────────────────

    def _sync_state(self, now: datetime) -> None:
        positions = pt_main.load_open_positions(self.redis)
        ids = {p.position_id for p in positions}
        for position in positions:
            if position.position_id in self._open_ids:
                continue
            self._entries.append({
                "position_id": position.position_id,
                "symbol": position.symbol,
                "strategy": position.strategy,
                "time": now.time(),
                "expiry": position.expiry,
                "margin": position.margin_blocked,
                "max_loss": position.max_loss if hasattr(position, "max_loss") else None,
            })
        self._open_ids = ids

    def _read_manifest(self, now: datetime) -> dict:
        path = self.recorder._root / "eod" / now.date().isoformat() / "manifest.json"
        if not path.is_file():
            return {}
        import json
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def _read_trades(self) -> list[dict]:
        raw = self.redis.xrange(TRADES_STREAM)
        trades = []
        for _msg_id, fields in raw:
            row = {k: v for k, v in fields.items()}
            row["pnl"] = float(row.get("pnl") or 0.0)
            row["entry_credit"] = float(row.get("entry_credit") or 0.0)
            row["exit_premium"] = float(row.get("exit_premium") or 0.0)
            trades.append(row)
        return trades

    def _write_heartbeats(self) -> None:
        from services.analysis_engine import main as ae_main
        from services.auth_service import main as auth_main
        from services.data_gateway import main as dg_main
        from services.orchestrator import main as orch_main
        from services.signal_intelligence import main as si_main

        try:
            auth_main._update_heartbeat(self.redis)
            ae_main._update_heartbeat(self.redis, "e2e")
            orch_main._write_orchestrator_heartbeat(self.redis)
            si_main._update_heartbeat(self.redis, 0)
            dg_main._touch_heartbeat(self.redis)
            pt_main.update_heartbeat(self.redis)
        except Exception as exc:          # noqa: BLE001
            self._errors.append(f"heartbeat: {exc}")
        for key in self.redis._client.scan_iter("service:registry:*"):
            self._heartbeat_keys.add(key)

    def _check_leg_pricing(self) -> None:
        """Every open leg must be re-priced from the published chain, not stale."""
        for position in pt_main.load_open_positions(self.redis):
            live = self.redis.hgetall(f"data:options_live:{position.symbol}")
            for leg in position.legs:
                key = pt_main.engine.option_field_key(leg.strike, leg.option_type)
                raw = live.get(key)
                if raw is None:
                    self._pricing_issues.append({
                        "position_id": position.position_id, "leg": key,
                        "issue": "no live tick",
                    })
                    continue
                try:
                    ltp = float(json.loads(raw).get("ltp", 0) or 0)
                except (ValueError, TypeError):
                    ltp = 0.0
                if ltp > 0 and leg.current_premium is not None and abs(leg.current_premium - ltp) > 1e-6:
                    self._pricing_issues.append({
                        "position_id": position.position_id, "leg": key,
                        "issue": f"stale premium {leg.current_premium} != {ltp}",
                    })

    def _sample_heartbeats(self, now: datetime) -> None:
        """Heartbeat age must stay below the key's TTL (the data-gateway bug)."""
        stamp = now.timestamp()
        for key in self.redis._client.scan_iter("service:registry:*"):
            raw = self.redis.hgetall(key)
            if not raw or "last_heartbeat" not in raw:
                continue
            try:
                age = stamp - float(raw["last_heartbeat"])
            except (TypeError, ValueError):
                self._heartbeat_violations.append(f"{key}: unparseable last_heartbeat")
                continue
            ttl = self.redis._client.ttl(key)
            if ttl is None or ttl <= 0:
                self._heartbeat_violations.append(f"{key}: no TTL")
            elif age < 0 or age >= ttl:
                self._heartbeat_violations.append(
                    f"{key}: age {age:.0f}s >= ttl {ttl}s"
                )

    def _build_result(self, now: datetime) -> RunResult:
        account = pt_main.get_account(self.redis)
        today = now.date().isoformat()
        trades = self._read_trades()
        manifest = self._read_manifest(now)
        expected = {
            f"chain:{symbol}": self._capture_calls["chain"]
            for symbol in self.scenario.symbols
        }
        expected.update({
            f"futures:{symbol}": self._capture_calls["futures"]
            for symbol in self.scenario.symbols
        })
        positions = pt_main.load_open_positions(self.redis)
        return RunResult(
            scenario=self.scenario,
            entries=self._entries,
            signal_attempts=self._signal_attempts,
            exits=trades,
            trades=trades,
            analysis_results=self._analysis_results,
            log_records=self.logs.records,
            subscription_max=self.feed.trace.max_seen,
            subscription_cap=self.feed.cap,
            atm_misses=self.feed.trace.atm_misses,
            capture={
                "files": self.recorder.files_written,
                "rows": dict(self.recorder._manifest_rows),
                "manifest": manifest,
            },
            capture_expected=expected,
            account=account.to_redis_mapping(),
            daily_pnl=self.redis.hgetall(daily_pnl_key(today)),
            open_positions=[
                {"position_id": p.position_id, "json": p.to_json()} for p in positions
            ],
            heartbeat_keys=sorted(self._heartbeat_keys),
            errors=self._errors,
            pricing_issues=self._pricing_issues[:50],
            heartbeat_violations=self._heartbeat_violations[:50],
            final_spot={s: round(v, 2) for s, v in self._last_spot.items()},
        )

    # ── environment ───────────────────────────────────────────────────────

    def _environment(self):
        stack = ExitStack()
        stack.enter_context(patch.object(pt_main, "TELEGRAM_NOTIFICATIONS", self.telegram))
        stack.enter_context(patch.object(pt_main, "is_trading_day", lambda _d: self.scenario.trading_day))
        stack.enter_context(patch(
            "services.market_data.chain_recorder.is_trading_day",
            lambda _d: self.scenario.trading_day,
        ))
        stack.enter_context(patch("common.market_calendar.is_trading_day",
                                  lambda _d: True))
        stack.enter_context(patch.object(ChainRecorder, "_enough_disk", lambda _self: True))
        db_path = Path(tempfile.mkdtemp(prefix="e2e-ledger-")) / "paper.db"
        stack.enter_context(patch.object(ledger, "DB_PATH", str(db_path)))
        ledger.init_db()
        previous_metrics = metrics._REDIS_CLIENT
        metrics._REDIS_CLIENT = self.redis._client
        previous_mode = shared.app_ctx.mode
        previous_bus = getattr(shared.app_ctx, "signal_bus", None)
        shared.app_ctx.signal_bus = None
        self._entries: list[dict] = []
        self._signal_attempts: list[dict] = []
        self._analysis_results: list[dict] = []
        self._spot_tracker = pt_main.engine.SpotMoveTracker()
        if not self.redis.hgetall(ACCOUNT_KEY):
            pt_main.save_account(self.redis, PaperAccount())

        def _restore() -> None:
            metrics._REDIS_CLIENT = previous_metrics
            shared.app_ctx.mode = previous_mode
            shared.app_ctx.signal_bus = previous_bus

        stack.callback(_restore)
        return stack

    # ── faults ────────────────────────────────────────────────────────────

    def _apply_faults(self, elapsed: int) -> None:
        self._elapsed = elapsed
        for index, fault in enumerate(self.scenario.faults):
            if index in self._faults_seen:
                continue
            at = int(fault.get("at_seconds", 0))
            if elapsed >= at:
                self._faults_seen.add(index)
                self._on_fault(fault, elapsed)

    def _on_fault(self, fault: dict, elapsed: int) -> None:
        kind = fault["kind"]
        if kind == "clock_jump":
            # A feed/clock jump: advance market time without simulating the
            # missing seconds, so downstream staleness gates must react.
            jump = int(fault.get("jump_seconds", 120))
            self._clock_offset += jump
            self.generator.step(jump)
            self._errors.append(f"clock_jump +{jump}s applied")
        if kind == "restart_service":
            # Restarting market-data resets the per-strike open_oi baselines.
            for symbol in self.scenario.symbols:
                store = self.feed.stocks[symbol]._tick_store
                with store._lock:
                    for sides in store.options_live.values():
                        for entry in sides.values():
                            entry.pop("open_oi", None)
                    store._oi_session_date = None

    def _fault_active(self, kind: str, elapsed: int) -> bool:
        for fault in self.scenario.faults:
            if fault["kind"] != kind:
                continue
            start = int(fault.get("at_seconds", 0))
            duration = int(fault.get("duration_seconds", 0))
            if start <= elapsed < start + duration:
                return True
        return False
