"""E2E: the Block 2 volatility-context keys must appear on a simulated day."""
from __future__ import annotations

import json

import pytest

from tests.e2e.sim.config import load_scenarios
from tests.e2e.sim.harness import Harness

pytestmark = pytest.mark.e2e

REQUIRED = ("IV_CONTEXT", "VIX_REGIME", "TERM_STRUCTURE", "EXPECTED_MOVE", "POP_LADDER")


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def test_simulated_day_emits_volatility_context(fake_redis, tmp_path) -> None:
    scenario = {s.name: s for s in load_scenarios()}["expiry_day"]
    result = Harness(scenario, fake_redis,
                     capture_root=tmp_path / "options_history").run()

    successes = [r for r in result.analysis_results if r.get("result") == "SUCCESS"]
    assert successes, "no successful analysis to inspect"

    seen: dict[str, int] = {}
    for entry in successes:
        payload = json.loads(entry["analysis_json"])
        for key in payload.get("NEUTRAL", {}):
            seen[key] = seen.get(key, 0) + 1

    missing = [key for key in REQUIRED if key not in seen]
    assert not missing, f"missing volatility-context keys {missing}; saw {sorted(seen)}"

    # Term structure must carry a real two-expiry slope from the capture path.
    # Namedtuples serialise as arrays: [regime, near_expiry, next_expiry, ...].
    def _term(payload):
        raw = payload["NEUTRAL"]["TERM_STRUCTURE"]
        if isinstance(raw, list) and raw and isinstance(raw[0], (list, dict)):
            raw = raw[0]          # multiple cycles on one stock -> list of tuples
        return raw

    term = next(
        _term(json.loads(e["analysis_json"]))
        for e in successes
        if "TERM_STRUCTURE" in json.loads(e["analysis_json"]).get("NEUTRAL", {})
    )
    regime, near_expiry, next_expiry = term[0], term[1], term[2]
    assert regime in {"CONTANGO", "BACKWARDATION", "FLAT"}
    assert next_expiry != near_expiry
