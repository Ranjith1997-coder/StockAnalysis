"""Scenario definitions and validation for the E2E simulator (T5.2).

A scenario is a YAML file under ``tests/e2e/scenarios/``. Validation is strict:
a typo in an oracle or fault name must fail at load time, naming the key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

import yaml

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "scenarios"

KNOWN_ORACLES = {
    "no_errors",
    "subs_within_cap",
    "chain_follows_spot",
    "capture_coverage",
    "entry_gates",
    "exits_valid",
    "legs_priced_from_market",
    "pnl_reconciles",
    "risk_limits",
    "heartbeats_present",
    "analysis_ran",
}

KNOWN_FAULTS = {
    "feed_gap",
    "stale_aggregate",
    "sensibull_5xx",
    "redis_down",
    "restart_service",
    "clock_jump",
    "disk_low",
}


@dataclass(frozen=True)
class ScriptedSignal:
    """A signal injected at a simulated time through the real entry path."""

    at: time
    symbol: str
    strategy: str = "IRON_CONDOR"
    direction: str = "NEUTRAL"
    put_wall_strike: float | None = None
    call_wall_strike: float | None = None
    sr_level: float | None = None
    level: str = "MODERATE"
    score: float = 50.0
    source: str = "E2E_SCRIPTED"
    expect_entry: bool = True


@dataclass(frozen=True)
class Scenario:
    name: str
    date: date
    symbols: tuple[str, ...]
    start: time
    end: time
    spot: dict[str, float]
    strike_gap: dict[str, float]
    expiry: dict[str, date]
    seed: int = 0
    step_seconds: int = 30
    drift_pct_per_day: float = 0.0
    vol_annual: float = 0.06
    gap_pct: float = 0.0
    vix_start: float = 14.0
    vix_end: float = 14.0
    iv_atm: float = 0.12
    iv_skew: float = -0.08
    iv_smile: float = 0.40
    term_slope_pct_per_week: float = 0.30
    atm_iv_percentile: float = 55.0
    oi_ce_writing_pct_per_min: float = 0.0
    oi_pe_writing_pct_per_min: float = 0.0
    lot_size: dict[str, int] = field(default_factory=dict)
    oracles: tuple[str, ...] = tuple(sorted(KNOWN_ORACLES))
    faults: tuple[dict, ...] = ()
    signals: tuple[ScriptedSignal, ...] = ()
    trading_day: bool = True
    expect_exits: int = 0
    expect_exit_reason: str | None = None
    description: str = ""
    capture_root: str | None = None

    @property
    def session_start(self) -> datetime:
        return datetime.combine(self.date, self.start)

    @property
    def session_seconds(self) -> int:
        return int((datetime.combine(self.date, self.end)
                    - datetime.combine(self.date, self.start)).total_seconds())

    def lot(self, symbol: str) -> int:
        return self.lot_size.get(symbol, 75)

    def signal_times(self) -> list[tuple[int, ScriptedSignal]]:
        """(elapsed_seconds, signal) pairs, validated to be inside the session."""
        out = []
        for signal in self.signals:
            at = datetime.combine(self.date, signal.at)
            elapsed = int((at - self.session_start).total_seconds())
            if not 0 < elapsed < self.session_seconds:
                raise ValueError(
                    f"{self.name}: signal at {signal.at} is outside the session "
                    f"{self.start}-{self.end}"
                )
            out.append((elapsed, signal))
        return sorted(out, key=lambda pair: pair[0])


def _require(section: dict, key: str, where: str):
    if key not in section:
        raise ValueError(f"{where}: missing required key {key!r}")
    return section[key]


def parse_scenario(raw: dict, source: str = "<dict>") -> Scenario:
    where = f"scenario {raw.get('name', source)}"
    date_str = _require(raw, "date", where)
    if not isinstance(date_str, str):
        raise ValueError(f"{where}: date must be an ISO string")
    symbols = tuple(_require(raw, "symbols", where))
    spot = {k: float(v) for k, v in _require(raw, "spot", where).items()}
    gap = {k: float(v) for k, v in _require(raw, "strike_gap", where).items()}
    expiry = {k: date.fromisoformat(v) for k, v in _require(raw, "expiry", where).items()}

    missing = [s for s in symbols if s not in spot or s not in gap or s not in expiry]
    if missing:
        raise ValueError(f"{where}: symbols missing spot/strike_gap/expiry: {missing}")

    start = time.fromisoformat(str(raw.get("start", "09:15")))
    end = time.fromisoformat(str(raw.get("end", "15:30")))
    if end <= start:
        raise ValueError(f"{where}: end must be after start")

    oracles = tuple(raw.get("oracles", sorted(KNOWN_ORACLES)))
    unknown = set(oracles) - KNOWN_ORACLES
    if unknown:
        raise ValueError(f"{where}: unknown oracle(s) {sorted(unknown)}")

    faults = tuple(raw.get("faults", ()))
    for fault in faults:
        if fault.get("kind") not in KNOWN_FAULTS:
            raise ValueError(
                f"{where}: unknown fault {fault.get('kind')!r} "
                f"(known: {sorted(KNOWN_FAULTS)})"
            )

    signals = tuple(
        ScriptedSignal(
            at=time.fromisoformat(str(sig["at"])),
            symbol=sig["symbol"],
            strategy=sig.get("strategy", "IRON_CONDOR"),
            direction=sig.get("direction", "NEUTRAL"),
            put_wall_strike=(float(sig["put_wall_strike"])
                             if sig.get("put_wall_strike") is not None else None),
            call_wall_strike=(float(sig["call_wall_strike"])
                              if sig.get("call_wall_strike") is not None else None),
            sr_level=(float(sig["sr_level"]) if sig.get("sr_level") is not None else None),
            level=sig.get("level", "MODERATE"),
            score=float(sig.get("score", 50.0)),
            source=sig.get("source", "E2E_SCRIPTED"),
            expect_entry=bool(sig.get("expect_entry", True)),
        )
        for sig in raw.get("signals", ())
    )
    for sig in signals:
        if sig.symbol not in symbols:
            raise ValueError(f"{where}: signal symbol {sig.symbol!r} not in symbols")

    vix = raw.get("vix", {})
    smile = raw.get("iv_smile", {})
    flow = raw.get("oi_flow", {})

    scenario = Scenario(
        name=raw["name"],
        description=raw.get("description", ""),
        date=date.fromisoformat(date_str),
        symbols=symbols,
        start=start,
        end=end,
        spot=spot,
        strike_gap=gap,
        expiry=expiry,
        seed=int(raw.get("seed", 0)),
        step_seconds=int(raw.get("step_seconds", 30)),
        drift_pct_per_day=float(raw.get("drift_pct_per_day", 0.0)),
        vol_annual=float(raw.get("vol_annual", 0.06)),
        gap_pct=float(raw.get("gap_pct", 0.0)),
        vix_start=float(vix.get("start", 14.0)),
        vix_end=float(vix.get("end", vix.get("start", 14.0))),
        iv_atm=float(smile.get("atm", 0.12)),
        iv_skew=float(smile.get("skew", -0.08)),
        iv_smile=float(smile.get("smile", 0.40)),
        term_slope_pct_per_week=float(raw.get("term_slope_pct_per_week", 0.30)),
        atm_iv_percentile=float(raw.get("atm_iv_percentile", 55.0)),
        oi_ce_writing_pct_per_min=float(flow.get("ce_writing_pct_per_min", 0.0)),
        oi_pe_writing_pct_per_min=float(flow.get("pe_writing_pct_per_min", 0.0)),
        lot_size={k: int(v) for k, v in raw.get("lot_size", {}).items()},
        oracles=oracles,
        faults=faults,
        signals=signals,
        trading_day=bool(raw.get("trading_day", True)),
        expect_exits=int(raw.get("expect_exits", 0)),
        expect_exit_reason=raw.get("expect_exit_reason"),
        capture_root=raw.get("capture_root"),
    )
    scenario.signal_times()          # raises on out-of-session signals
    if scenario.step_seconds <= 0:
        raise ValueError(f"{where}: step_seconds must be positive")
    return scenario


def load_scenario(path: Path) -> Scenario:
    return parse_scenario(yaml.safe_load(path.read_text()), source=str(path))


def load_scenarios(directory: Path | None = None) -> list[Scenario]:
    directory = directory or SCENARIO_DIR
    return [load_scenario(p) for p in sorted(directory.glob("*.yaml"))]
