"""Replay a captured day through the real analysis and paper-trading rules."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path

import time_machine

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
from services.paper_trading import engine
from services.paper_trading import main as pt_main
from services.paper_trading.models import (
    POSITIONS_OPEN_KEY,
    PaperAccount,
    PaperPosition,
    cooldown_key,
)
from services.paper_trading.signal_router import (
    EntrySignal,
    check_entry_filters,
    parse_analysis_result,
)
from services.paper_trading.strategy_builder import build_position

from .context import ReplayContext
from .loader import DayData, load_day
from .outcomes import SignalOutcome
from .replay_redis import ReplayRedis

logger = logging.getLogger("SA.backtest")


def _local_instant(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc)


def build_orchestrator() -> AnalyserOrchestrator:
    orchestrator = AnalyserOrchestrator()
    for analyser in (
        VolumeAnalyser(), TechnicalAnalyser(), CandleStickAnalyser(), IVAnalyser(),
        FuturesAnalyser(), PCRAnalyser(), MaxPainAnalyser(), OIChainAnalyser(),
        GEXAnalyser(), PanicModeAnalyser(), OptionSellerCompositeAnalyser(),
    ):
        orchestrator.register(analyser)
    return orchestrator


class FixedSpan:
    """SPAN margin is an external API; the backtest uses a fixed per-lot margin."""

    def __init__(self, lot_sizes: dict[str, int], margin_per_lot: float) -> None:
        self._lot_sizes = lot_sizes
        self._margin = margin_per_lot

    def get_lot_size(self, symbol: str, expiry: str) -> int | None:
        return self._lot_sizes.get(symbol)

    def get_scrip(self, symbol: str, expiry: str) -> str:
        return f"{symbol}{expiry.replace('-', '')}"

    def calculate_margin(self, legs) -> float | None:
        return self._margin if legs else None


@dataclass(frozen=True)
class BacktestConfig:
    root: Path
    date: str
    symbols: tuple[str, ...]
    signals: tuple[dict, ...] = ()
    capital: float = 1_000_000.0
    max_positions: int = 6
    margin_per_lot: float = 60_000.0
    analysis_interval: int = 300
    lot_sizes: dict[str, int] = field(default_factory=dict)


@dataclass
class BacktestResult:
    outcomes: list[SignalOutcome]
    analysis_cycles: int = 0
    analysis_failures: int = 0
    days: dict[str, DayData] = field(default_factory=dict)


class Backtester:
    def __init__(self, config: BacktestConfig) -> None:
        self._config = config
        self._orchestrator = build_orchestrator()
        self._account = PaperAccount(capital=config.capital)
        self._open: list[PaperPosition] = []
        self._outcomes: list[SignalOutcome] = []
        self._by_position: dict[str, SignalOutcome] = {}
        self._span = FixedSpan(
            {symbol: config.lot_sizes.get(symbol, 75) for symbol in config.symbols},
            config.margin_per_lot,
        )
        self._cycles = 0
        self._failures = 0
        self._cycle_id = 0

    # ── public ───────────────────────────────────────────────────────────

    def run(self) -> BacktestResult:
        days = {symbol: load_day(self._config.root, self._config.date, symbol)
                for symbol in self._config.symbols}

        previous_bus = getattr(shared.app_ctx, "signal_bus", None)
        shared.app_ctx.signal_bus = None
        original_gate = pt_main.is_trading_day
        pt_main.is_trading_day = lambda _d: True
        try:
            for symbol, day in days.items():
                self._run_symbol(symbol, day)
        finally:
            pt_main.is_trading_day = original_gate
            shared.app_ctx.signal_bus = previous_bus

        return BacktestResult(
            outcomes=self._outcomes,
            analysis_cycles=self._cycles,
            analysis_failures=self._failures,
            days=days,
        )

    # ── per symbol ───────────────────────────────────────────────────────

    def _run_symbol(self, symbol: str, day: DayData) -> None:
        context = ReplayContext.for_symbol(symbol, self._config.lot_sizes.get(symbol))
        snapshots = day.snapshots()
        if not snapshots:
            return
        session_start = snapshots[0].ts
        scripted = [spec for spec in self._config.signals if spec.get("symbol") == symbol]
        fired: set[int] = set()

        for snapshot in snapshots:
            with time_machine.travel(_local_instant(snapshot.ts), tick=False):
                elapsed = int((snapshot.ts - session_start).total_seconds())
                is_cycle = elapsed % self._config.analysis_interval == 0
                context.update(snapshot, analysis_cycle=is_cycle)
                redis = context.redis

                self._mark(redis, symbol, snapshot.ts)

                if is_cycle:
                    self._analysis_cycle(redis, symbol, snapshot.ts)

                for index, spec in enumerate(scripted):
                    if index in fired:
                        continue
                    if snapshot.ts.time() >= _at_time(spec.get("at")):
                        fired.add(index)
                        self._try_entry(_signal_from_spec(spec, context), redis, snapshot.ts)

        self._finalise(context, snapshots[-1].ts)

    # ── analysis ─────────────────────────────────────────────────────────

    def _analysis_cycle(self, redis: ReplayRedis, symbol: str, now: datetime) -> None:
        self._cycle_id += 1
        fields = {
            "job_id": f"bt-{symbol}-{self._cycle_id}",
            "cycle_id": str(self._cycle_id),
            "symbol": symbol,
            "is_index": "true",
            "mode": "intraday",
        }
        try:
            result = process_job(redis, self._orchestrator, fields)
        except Exception as exc:          # noqa: BLE001 - surfaced in the report
            self._failures += 1
            logger.warning("[backtest] analysis %s failed: %s", symbol, exc)
            return
        self._cycles += 1
        if result.get("result") != "SUCCESS":
            return
        try:
            entries, exits = parse_analysis_result(result)
        except Exception as exc:          # noqa: BLE001
            self._failures += 1
            logger.warning("[backtest] could not parse %s result: %s", symbol, exc)
            return
        for exit_signal in exits:
            self._apply_exit_signal(redis, exit_signal, now)
        for entry_signal in entries:
            self._try_entry(entry_signal, redis, now)

    # ── entries ──────────────────────────────────────────────────────────

    def _try_entry(self, signal: EntrySignal, redis: ReplayRedis, now: datetime) -> None:
        gate = pt_main.check_entry_gate(redis, signal.symbol, now=now)
        if gate:
            self._record_blocked(signal, now, gate)
            return
        passed, reason = check_entry_filters(
            signal, redis, self._account, self._open,
            max_positions=self._config.max_positions,
        )
        if not passed:
            self._record_blocked(signal, now, reason)
            return

        position = build_position(
            signal, redis, self._span, self._account, mode="intraday", now=now
        )
        if position is None:
            self._record_blocked(signal, now, "no_position")
            return

        self._open.append(position)
        self._account.margin_used += position.margin_blocked
        self._account.open_positions += 1
        redis.hset(POSITIONS_OPEN_KEY, position.position_id, position.to_json())
        outcome = SignalOutcome(
            ts=now, symbol=position.symbol, strategy=position.strategy,
            source=signal.signal_source or "analysis",
            status="entered", entry_credit=position.entry_credit,
            max_loss=engine.compute_max_loss(position),
        )
        self._outcomes.append(outcome)
        self._by_position[position.position_id] = outcome

    def _record_blocked(self, signal: EntrySignal, now: datetime, reason: str) -> None:
        self._outcomes.append(SignalOutcome(
            ts=now, symbol=signal.symbol, strategy=signal.strategy,
            source=signal.signal_source or "analysis",
            status="blocked", block_reason=reason,
        ))

    # ── marking, exits ───────────────────────────────────────────────────

    def _mark(self, redis: ReplayRedis, symbol: str, now: datetime) -> None:
        for position in list(self._open):
            if position.symbol != symbol:
                continue
            result = engine.evaluate_position(position, redis, now=now)
            if result is None:
                continue
            outcome = self._by_position.get(position.position_id)
            if outcome is None:
                continue
            if result.status == "CLOSED":
                self._book_exit(result, now, redis)
                continue
            unrealized = engine.compute_unrealized_pnl(
                result, engine.compute_current_debit(result)
            )
            outcome.mfe = unrealized if outcome.mfe is None else max(outcome.mfe, unrealized)
            outcome.mae = unrealized if outcome.mae is None else min(outcome.mae, unrealized)
            age = int((now - outcome.ts).total_seconds())
            if outcome.pnl_30m is None and age >= 1800:
                outcome.pnl_30m = unrealized
            if outcome.pnl_60m is None and age >= 3600:
                outcome.pnl_60m = unrealized

    def _apply_exit_signal(self, redis: ReplayRedis, signal, now: datetime) -> None:
        matches = [
            p for p in self._open
            if p.symbol == signal.symbol
            and (signal.position_id in (None, p.position_id))
        ]
        for position in matches:
            options_live = redis.hgetall(f"data:options_live:{position.symbol}")
            closed = engine.close_position(
                position, signal.reason or "MANUAL", now=now.timestamp(),
                options_live=options_live,
            )
            self._book_exit(closed, now, redis)

    def _book_exit(self, position: PaperPosition, now: datetime,
                   redis: ReplayRedis) -> None:
        outcome = self._by_position.pop(position.position_id, None)
        if position in self._open:
            self._open.remove(position)
        self._account.margin_used = max(0.0, self._account.margin_used - position.margin_blocked)
        self._account.open_positions = max(0, self._account.open_positions - 1)
        self._account.realized_pnl += position.pnl or 0.0
        if outcome is not None:
            outcome.exit_reason = position.exit_reason
            outcome.pnl = position.pnl
            outcome.exit_ts = now
            if position.pnl is not None:
                # A position can exit on its first mark; record the realised
                # point so MFE/MAE are never empty for an entered trade.
                outcome.mfe = position.pnl if outcome.mfe is None else max(outcome.mfe, position.pnl)
                outcome.mae = position.pnl if outcome.mae is None else min(outcome.mae, position.pnl)
        # Mirrors production: an exited position cannot immediately re-enter.
        redis.set(cooldown_key(position.symbol, position.strategy), "1")

    def _finalise(self, context: ReplayContext, now: datetime) -> None:
        for position in list(self._open):
            outcome = self._by_position.get(position.position_id)
            if outcome is None:
                continue
            unrealized = engine.compute_unrealized_pnl(
                position, engine.compute_current_debit(position)
            )
            outcome.exit_reason = "OPEN_AT_END"
            outcome.pnl = unrealized
            outcome.exit_ts = now
            self._by_position.pop(position.position_id, None)
            self._open.remove(position)


def _at_time(value) -> dtime:
    if isinstance(value, dtime):
        return value
    return dtime.fromisoformat(str(value))


def _signal_from_spec(spec: dict, context: ReplayContext) -> EntrySignal:
    spot = context.current_spot or 0.0
    gap = spec.get("strike_gap", 50.0)
    atm = spec.get("atm_strike") or (round(spot / gap) * gap if spot else 0.0)
    return EntrySignal(
        strategy=spec.get("strategy", "IRON_CONDOR"),
        symbol=spec["symbol"],
        direction=spec.get("direction", "NEUTRAL"),
        put_wall_strike=(float(spec["put_wall_strike"])
                         if spec.get("put_wall_strike") is not None
                         else float(atm - 3 * gap)),
        call_wall_strike=(float(spec["call_wall_strike"])
                          if spec.get("call_wall_strike") is not None
                          else float(atm + 3 * gap)),
        sr_level=(float(spec["sr_level"]) if spec.get("sr_level") is not None else None),
        level=spec.get("level", "MODERATE"),
        score=float(spec.get("score", 50.0)),
        signal_source=spec.get("source", "SCRIPTED"),
        mode="intraday",
        expiry=spec.get("expiry"),
    )
