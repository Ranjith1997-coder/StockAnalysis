"""Everything a scenario produced, in one object the oracles read (T5.6)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Scenario


@dataclass
class RunResult:
    scenario: Scenario
    entries: list[dict] = field(default_factory=list)
    signal_attempts: list[dict] = field(default_factory=list)
    exits: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    analysis_results: list[dict] = field(default_factory=list)
    log_records: list[dict] = field(default_factory=list)
    subscription_max: int = 0
    subscription_cap: int = 0
    atm_misses: list[int] = field(default_factory=list)
    capture: dict = field(default_factory=dict)
    capture_expected: dict = field(default_factory=dict)
    account: dict = field(default_factory=dict)
    daily_pnl: dict = field(default_factory=dict)
    open_positions: list[dict] = field(default_factory=list)
    heartbeat_keys: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pricing_issues: list[dict] = field(default_factory=list)
    heartbeat_violations: list[str] = field(default_factory=list)
    final_spot: dict[str, float] = field(default_factory=dict)

    def errors_in_logs(self) -> list[dict]:
        return [r for r in self.log_records if r["level"] == "ERROR"]
