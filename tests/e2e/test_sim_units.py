"""Unit tests for the simulator itself (T5.2/T5.3/T5.7).

The E2E layer is only trustworthy if its pieces fail when they should: scenario
validation names the bad key, the generator is reproducible, the feed honours the
subscription cap, and every oracle has a crafted failure case here.
"""
from __future__ import annotations

from datetime import time

import numpy as np
import pytest

from services.paper_trading.engine import compute_max_loss
from services.paper_trading.models import OptionLeg, PaperPosition
from tests.e2e.sim import feed as feed_module
from tests.e2e.sim.config import load_scenarios, parse_scenario
from tests.e2e.sim.feed import FakeFeed
from tests.e2e.sim.harness import Harness
from tests.e2e.sim.market import MarketGenerator
from tests.e2e.sim.oracles import ORACLES, evaluate
from tests.e2e.sim.result import RunResult

BASE = {
    "name": "unit",
    "date": "2026-11-24",
    "symbols": ["NIFTY"],
    "spot": {"NIFTY": 25_000},
    "strike_gap": {"NIFTY": 50},
    "expiry": {"NIFTY": "2026-11-25"},
    "start": "09:15",
    "end": "10:15",
}


def _scenario(**overrides):
    return parse_scenario({**BASE, **overrides})


def _result(**overrides) -> RunResult:
    scenario = overrides.pop("scenario", _scenario())
    return RunResult(scenario=scenario, **overrides)


def _position(pid: str = "p1", symbol: str = "NIFTY") -> PaperPosition:
    return PaperPosition(
        position_id=pid, symbol=symbol, strategy="IRON_CONDOR", mode="intraday",
        direction="NEUTRAL",
        legs=[
            OptionLeg(strike=25_100.0, option_type="CE", side="SELL", lots=1,
                      entry_premium=100.0),
            OptionLeg(strike=25_200.0, option_type="CE", side="BUY", lots=1,
                      entry_premium=40.0),
            OptionLeg(strike=24_900.0, option_type="PE", side="SELL", lots=1,
                      entry_premium=100.0),
            OptionLeg(strike=24_800.0, option_type="PE", side="BUY", lots=1,
                      entry_premium=40.0),
        ],
        expiry="2026-11-25", scrip="NIFTY", lot_size=75,
        entry_timestamp=0.0, entry_credit=4_000.0, margin_blocked=60_000.0,
    )


class TestScenarioValidation:
    def test_valid_scenario_parses(self):
        scenario = _scenario(step_seconds=15, expect_exits=2)
        assert scenario.session_seconds == 3_600
        assert scenario.step_seconds == 15
        assert scenario.expect_exits == 2

    def test_missing_symbol_data_names_the_symbol(self):
        with pytest.raises(ValueError, match="missing spot/strike_gap/expiry"):
            _scenario(symbols=["NIFTY", "SENSEX"])

    def test_unknown_oracle_is_rejected(self):
        with pytest.raises(ValueError, match="unknown oracle"):
            _scenario(oracles=["no_errors", "vibes"])

    def test_unknown_fault_is_rejected(self):
        with pytest.raises(ValueError, match="unknown fault"):
            _scenario(faults=[{"kind": "meteor"}])

    def test_end_before_start_is_rejected(self):
        with pytest.raises(ValueError, match="end must be after start"):
            _scenario(start="10:00", end="09:30")

    def test_signal_outside_the_session_is_rejected(self):
        with pytest.raises(ValueError, match="outside the session"):
            _scenario(signals=[{"at": "16:00", "symbol": "NIFTY"}])

    def test_signal_symbol_must_be_listed(self):
        with pytest.raises(ValueError, match="not in symbols"):
            _scenario(signals=[{"at": "09:30", "symbol": "BANKNIFTY"}])

    def test_all_catalogue_scenarios_load(self):
        scenarios = load_scenarios()
        assert {s.name for s in scenarios} >= {
            "range_day", "trend_day_up", "expiry_day", "holiday",
        }


class TestMarketDeterminism:
    def _series(self, seed: int, steps: int = 20) -> list[float]:
        scenario = _scenario()
        gen = MarketGenerator(scenario, np.random.default_rng(seed))
        return [round(gen.step(30).spot["NIFTY"], 6) for _ in range(steps)]

    def test_same_seed_is_identical(self):
        assert self._series(7) == self._series(7)

    def test_different_seed_differs(self):
        assert self._series(7) != self._series(8)

    def test_gap_is_applied_at_the_open(self):
        scenario = _scenario(gap_pct=-1.5)
        gen = MarketGenerator(scenario, np.random.default_rng(0))
        assert gen.open_spot["NIFTY"] == pytest.approx(25_000 * 0.985)

    def test_option_price_falls_with_distance_from_atm(self):
        scenario = _scenario()
        gen = MarketGenerator(scenario, np.random.default_rng(0))
        state = gen.step(30)
        atm = gen.option_tick("NIFTY", state, 25_000, "CE")["last_price"]
        otm = gen.option_tick("NIFTY", state, 25_500, "CE")["last_price"]
        assert atm > otm > 0

    def test_tick_shape_is_complete(self):
        scenario = _scenario()
        gen = MarketGenerator(scenario, np.random.default_rng(0))
        tick = gen.option_tick("NIFTY", gen.step(30), 25_000, "PE")
        assert {"last_price", "bid", "ask", "oi", "iv", "volume_traded",
                "depth"} <= set(tick)
        assert tick["bid"] < tick["last_price"] < tick["ask"]


class TestFeedCap:
    def test_subscriptions_never_exceed_the_cap(self, monkeypatch):
        import common.token_registry as registry_module

        monkeypatch.setattr(registry_module, "WS_MAX_OPTION_SUBSCRIPTIONS", 40)
        monkeypatch.setattr(feed_module, "WS_MAX_OPTION_SUBSCRIPTIONS", 40)
        scenario = _scenario()
        gen = MarketGenerator(scenario, np.random.default_rng(0))
        feed = FakeFeed(scenario, gen)
        for _ in range(5):
            feed.push(gen.step(30))
        assert feed.trace.max_seen <= 40
        assert feed.subscribed_count("NIFTY") <= 40

    def test_atm_stays_subscribed(self):
        scenario = _scenario(drift_pct_per_day=3.0, vol_annual=0.1)
        gen = MarketGenerator(scenario, np.random.default_rng(3))
        feed = FakeFeed(scenario, gen)
        for _ in range(20):
            feed.push(gen.step(30))
        assert not feed.trace.atm_misses

    def test_feed_writes_aggregate_and_live_chain(self):
        scenario = _scenario()
        gen = MarketGenerator(scenario, np.random.default_rng(0))
        feed = FakeFeed(scenario, gen)
        feed.push(gen.step(30))
        store = feed.stocks["NIFTY"]._tick_store
        assert store.options_aggregate["atm_strike"] == 25_000
        assert store.options_live
        tick = next(iter(store.options_live[25_000].values()))
        assert tick["iv"] > 0, "paper trading refuses SELL legs without IV"


class TestCapEnforcementMatters:
    """Aggressive re-centring across three dense chains must stay under 480."""

    def test_cap_holds_after_repeated_recentring(self):
        scenario = {s.name: s for s in load_scenarios()}["cap_pressure"]
        gen = MarketGenerator(scenario, np.random.default_rng(scenario.seed))
        feed = FakeFeed(scenario, gen)
        for multiplier in (1.005, 1.01, 1.02, 1.03):
            feed.registry.recenter_and_get_subscription_changes(
                "NIFTY", scenario.spot["NIFTY"] * multiplier
            )
            assert len(feed.registry.get_all_subscribed_tokens()) <= feed.cap


class TestOraclesCanFail:
    def test_pricing_oracle_fails_on_a_stale_leg(self):
        ok, detail = ORACLES["legs_priced_from_market"](_result(pricing_issues=[
            {"position_id": "p", "leg": "25650.0_CE", "issue": "stale premium 75.39 != 120.0"},
        ]))
        assert not ok and "stale premium" in detail

    def test_heartbeats_fails_on_a_ttl_violation(self):
        ok, detail = ORACLES["heartbeats_present"](_result(
            heartbeat_keys=["service:registry:data-gateway"],
            heartbeat_violations=["service:registry:data-gateway: age 300s >= ttl 180s"],
        ))
        assert not ok and "age 300s" in detail

    def test_no_errors_fails_on_an_error_record(self):
        ok, detail = ORACLES["no_errors"](_result(log_records=[
            {"level": "ERROR", "name": "SA.x", "msg": "boom"},
        ]))
        assert not ok and "boom" in detail

    def test_subs_within_cap_fails_above_the_cap(self):
        ok, _ = ORACLES["subs_within_cap"](_result(
            subscription_max=501, subscription_cap=480))
        assert not ok

    def test_chain_follows_spot_fails_on_an_atm_miss(self):
        ok, _ = ORACLES["chain_follows_spot"](_result(atm_misses=[120]))
        assert not ok

    def test_capture_coverage_fails_on_short_rows(self):
        ok, detail = ORACLES["capture_coverage"](_result(
            capture={"files": 1, "rows": {"chain:NIFTY": 1}},
            capture_expected={"chain:NIFTY": 10},
        ))
        assert not ok and "1/10" in detail

    def test_capture_coverage_passes_without_expectations(self):
        ok, _ = ORACLES["capture_coverage"](_result(
            capture={"files": 0, "rows": {}},
            capture_expected={"chain:NIFTY": 0},
        ))
        assert ok

    def test_entry_gates_fails_on_expected_but_unopened(self):
        ok, detail = ORACLES["entry_gates"](_result(signal_attempts=[
            {"time": time(10, 5), "symbol": "NIFTY", "expected": True, "opened": False},
        ]))
        assert not ok and "expected opened=True" in detail

    def test_entry_gates_fails_outside_the_window(self):
        ok, _ = ORACLES["entry_gates"](_result(entries=[
            {"position_id": "p", "symbol": "NIFTY", "strategy": "IRON_CONDOR",
             "time": time(9, 10), "expiry": "2026-11-25", "margin": 1, "max_loss": 1},
        ]))
        assert not ok

    def test_exits_valid_fails_on_a_double_close(self):
        trade = {"position_id": "p", "exit_reason": "STOP_LOSS", "pnl": 1.0}
        ok, detail = ORACLES["exits_valid"](_result(trades=[trade, trade]))
        assert not ok and "twice" in detail

    def test_exits_valid_fails_on_wrong_exit_reason(self):
        ok, detail = ORACLES["exits_valid"](_result(
            scenario=_scenario(expect_exit_reason="STOP_LOSS"),
            trades=[{"position_id": "p", "exit_reason": "THETA_DECAY", "pnl": 1.0}],
        ))
        assert not ok and "STOP_LOSS" in detail

    def test_exits_valid_fails_when_expected_exits_are_missing(self):
        ok, detail = ORACLES["exits_valid"](_result(
            scenario=_scenario(expect_exits=1), trades=[]))
        assert not ok and "expected 1" in detail

    def test_pnl_reconciles_fails_on_mismatch(self):
        ok, detail = ORACLES["pnl_reconciles"](_result(
            trades=[{"position_id": "p", "pnl": 100.0, "exit_reason": "TARGET",
                     "entry_credit": 1.0, "exit_premium": 0.0}],
            account={"realized_pnl": "250.0", "capital": "1000000.0"},
            daily_pnl={"realized": "250.0", "trades_count": "1"},
        ))
        assert not ok and "100.00" in detail

    def test_risk_limits_fails_too_many_positions_per_index(self):
        positions = [{"position_id": f"p{i}", "json": _position(f"p{i}").to_json()}
                     for i in range(3)]
        ok, detail = ORACLES["risk_limits"](_result(
            open_positions=positions,
            account={"capital": "1000000.0", "realized_pnl": "0"},
        ))
        assert not ok and "3 positions" in detail

    def test_risk_limits_fails_oversized_max_loss(self):
        position = _position()
        assert compute_max_loss(position) is not None
        ok, _ = ORACLES["risk_limits"](_result(
            open_positions=[{"position_id": "p1", "json": position.to_json()}],
            account={"capital": "1.0", "realized_pnl": "0"},
        ))
        assert not ok

    def test_heartbeats_fails_when_none_written(self):
        ok, _ = ORACLES["heartbeats_present"](_result(heartbeat_keys=[]))
        assert not ok

    def test_analysis_ran_fails_without_results(self):
        ok, _ = ORACLES["analysis_ran"](_result(analysis_results=[]))
        assert not ok

    def test_analysis_ran_fails_on_error_result(self):
        ok, detail = ORACLES["analysis_ran"](_result(analysis_results=[
            {"symbol": "NIFTY", "result": "ERROR", "error": "boom"},
        ]))
        assert not ok and "boom" in detail

    def test_evaluate_reports_every_named_oracle(self):
        outcomes = evaluate(["no_errors", "heartbeats_present"], _result(
            heartbeat_keys=["service:registry:a"],
        ))
        assert [name for name, _ok, _d in outcomes] == ["no_errors", "heartbeats_present"]


class TestFaultMechanics:
    def _harness(self):
        scenario = _scenario()
        gen = MarketGenerator(scenario, np.random.default_rng(0))
        feed = FakeFeed(scenario, gen)
        harness = Harness.__new__(Harness)
        harness.scenario = scenario
        harness.generator = gen
        harness.feed = feed
        harness._clock_offset = 0
        harness._errors = []
        harness._faults_seen = set()
        harness._elapsed = 0
        return harness

    def test_feed_gap_is_active_inside_its_window(self):
        harness = self._harness()
        harness.scenario = _scenario(faults=[
            {"kind": "feed_gap", "at_seconds": 60, "duration_seconds": 120},
        ])
        assert not harness._fault_active("feed_gap", 59)
        assert harness._fault_active("feed_gap", 60)
        assert harness._fault_active("feed_gap", 179)
        assert not harness._fault_active("feed_gap", 180)

    def test_clock_jump_advances_the_offset_once(self):
        harness = self._harness()
        harness._apply_faults(0)
        harness._on_fault({"kind": "clock_jump", "jump_seconds": 120}, 0)
        assert harness._clock_offset == 120
        assert any("clock_jump +120s" in e for e in harness._errors)

    def test_disk_low_suppresses_only_its_window(self):
        harness = self._harness()
        harness.scenario = _scenario(faults=[
            {"kind": "disk_low", "at_seconds": 300, "duration_seconds": 60},
        ])
        assert not harness._fault_active("disk_low", 299)
        assert harness._fault_active("disk_low", 300)
        assert not harness._fault_active("disk_low", 360)

    def test_restart_clears_open_oi_baselines(self):
        harness = self._harness()
        harness.feed.push(harness.generator.step(30))
        store = harness.feed.stocks["NIFTY"]._tick_store
        assert any("open_oi" in tick for sides in store.options_live.values()
                   for tick in sides.values())
        harness._on_fault({"kind": "restart_service"}, 0)
        assert not any("open_oi" in tick for sides in store.options_live.values()
                       for tick in sides.values())


class TestNoErrorsBitesInsideARealRun:
    def test_an_injected_service_error_fails_the_oracle(self, monkeypatch, fake_redis):
        import logging

        from tests.e2e.sim.config import parse_scenario

        error_logger = logging.getLogger("SA.paper-trading")

        def failing_heartbeats(self):
            error_logger.error("injected failure during the run")
            self._errors.append("heartbeat failed")

        monkeypatch.setattr(Harness, "_write_heartbeats", failing_heartbeats)
        scenario = parse_scenario({**BASE, "end": "09:45"})

        result = Harness(scenario, fake_redis).run()

        ok, detail = ORACLES["no_errors"](result)
        assert not ok
        assert "injected failure during the run" in detail


class TestLogCaptureReachesServiceLoggers:
    def test_an_sa_logger_error_is_captured(self):
        import logging

        from tests.e2e.sim.harness import _LogCapture

        capture = _LogCapture()
        capture.attach()
        try:
            logging.getLogger("SA.paper-trading").error("boom %s", "x")
        finally:
            capture.detach()
        assert any(r["level"] == "ERROR" and "boom x" in r["msg"] for r in capture.records)
