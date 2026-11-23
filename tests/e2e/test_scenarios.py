"""E2E scenarios: run a simulated day through the real services (T5.8/T5.9)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.e2e.sim.config import load_scenarios
from tests.e2e.sim.harness import Harness
from tests.e2e.sim.oracles import evaluate

pytestmark = pytest.mark.e2e

SCENARIOS = {scenario.name: scenario for scenario in load_scenarios()}
ARTIFACTS = Path(__file__).resolve().parents[2] / "artifacts" / "e2e"


def _format(outcomes) -> str:
    return "\n".join(
        f"  {'PASS' if ok else 'FAIL'} {name}: {detail}"
        for name, ok, detail in outcomes
        if not ok
    )


def _write_artifact(name: str, seed: int, result, outcomes) -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / f"{name}-{seed}.json"
    path.write_text(json.dumps({
        "scenario": name,
        "seed": seed,
        "oracles": [
            {"name": n, "ok": ok, "detail": d} for n, ok, d in outcomes
        ],
        "entries": result.entries,
        "trades": result.trades,
        "errors": result.errors,
        "logs": [r for r in result.log_records if r["level"] == "ERROR"][:20],
        "subscription_max": result.subscription_max,
        "capture": result.capture,
        "account": result.account,
    }, indent=2, default=str))
    return path


def _seed_variants(scenario):
    """PR runs one seed; nightly (E2E_MULTI_SEED=1) runs three."""
    if os.getenv("E2E_MULTI_SEED") != "1":
        return [scenario]
    import dataclasses
    return [dataclasses.replace(scenario, seed=scenario.seed + offset) for offset in (0, 1, 2)]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario(name, fake_redis, tmp_path) -> None:
    for scenario in _seed_variants(SCENARIOS[name]):
        _run_one(scenario, fake_redis, tmp_path)


def _run_one(scenario, fake_redis, tmp_path) -> None:
    name = scenario.name
    harness = Harness(scenario, fake_redis, capture_root=tmp_path / "options_history")

    result = harness.run()
    outcomes = evaluate(scenario.oracles, result)

    if any(not ok for _n, ok, _d in outcomes):
        artifact = _write_artifact(name, scenario.seed, result, outcomes)
        pytest.fail(
            f"{name} (seed {scenario.seed}) failed; artifact: {artifact}\n"
            f"{_format(outcomes)}\n"
            f"errors: {result.errors[:5]}\n"
            f"entries: {len(result.entries)} trades: {len(result.trades)} "
            f"subs_max: {result.subscription_max}/{result.subscription_cap}"
        )
