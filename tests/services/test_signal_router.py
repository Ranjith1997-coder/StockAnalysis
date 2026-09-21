"""Tests for services/paper_trading/signal_router.py."""

import json
from datetime import datetime
from unittest.mock import MagicMock

from lib.intelligence.correlator import Confluence
from lib.intelligence.signal import Direction, Layer, Signal, SignalStrength
from services.paper_trading.models import PaperAccount, PaperPosition, OptionLeg
from services.paper_trading.signal_router import (
    EntrySignal,
    ExitSignal,
    check_entry_filters,
    correlated_naked_cap_hit,
    daily_loss_limit_hit,
    has_cooldown_lock,
    has_duplicate_position,
    parse_analysis_result,
    parse_confluence_message,
    parse_pinn_signal,
    portfolio_margin_exceeded,
)
from services.volatility_engine.comparator import PinnSignal
from services.volatility_engine.signal_emitter import emit_signal


def _fields(symbol="NIFTY", trend_found="true", priority_override="", analysis_json=None, mode=None):
    fields = {
        "symbol": symbol,
        "trend_found": trend_found,
        "PRIORITY_OVERRIDE": priority_override,
        "analysis_json": json.dumps(analysis_json or {}),
    }
    if mode is not None:
        fields["mode"] = mode
    return fields


class TestParseAnalysisResult:
    def test_fast_path_skips_when_no_trend_and_no_override(self):
        fields = _fields(trend_found="false", priority_override="", analysis_json={
            "NEUTRAL": {"RANGE_BOUND_SETUP": {"setup_type": "IRON_CONDOR"}}
        })
        entries, exits = parse_analysis_result(fields)
        assert entries == []
        assert exits == []

    def test_gamma_trap_produces_exit_signal(self):
        fields = _fields(priority_override="CRITICAL", analysis_json={
            "NEUTRAL": {"GAMMA_TRAP": {"direction": "BEARISH"}}
        })
        entries, exits = parse_analysis_result(fields)
        assert entries == []
        assert exits == [ExitSignal(symbol="NIFTY", reason="GAMMA_TRAP")]

    def test_gamma_trap_active_flag_also_triggers_exit(self):
        fields = _fields(priority_override="CRITICAL", analysis_json={
            "NEUTRAL": {"GAMMA_TRAP_ACTIVE": True}
        })
        _, exits = parse_analysis_result(fields)
        assert len(exits) == 1
        assert exits[0].reason == "GAMMA_TRAP"

    def test_range_bound_setup_produces_entry_signal(self):
        fields = _fields(priority_override="HIGH", analysis_json={
            "NEUTRAL": {
                "RANGE_BOUND_SETUP": {
                    "setup_type": "IRON_CONDOR",
                    "put_wall_strike": "24000.0",
                    "call_wall_strike": "25000.0",
                    "iv_percentile": "72",
                }
            }
        })
        entries, exits = parse_analysis_result(fields)
        assert len(entries) == 1
        signal = entries[0]
        assert signal.strategy == "IRON_CONDOR"
        assert signal.symbol == "NIFTY"
        assert signal.put_wall_strike == 24000.0
        assert signal.call_wall_strike == 25000.0
        assert signal.iv_percentile == 72.0
        assert signal.signal_source == "RANGE_BOUND_SETUP"
        assert signal.mode == "intraday"   # default when the field is absent

    def test_mode_propagates_from_analysis_results_message(self):
        # worker.py now echoes the job's actual mode onto every result --
        # this is the only way to tell an 8pm positional composite setup
        # from a 09:15-15:30 intraday one.
        fields = _fields(priority_override="HIGH", mode="positional", analysis_json={
            "NEUTRAL": {
                "RANGE_BOUND_SETUP": {
                    "setup_type": "STRANGLE", "put_wall_strike": "24000", "call_wall_strike": "25000",
                }
            }
        })
        entries, _ = parse_analysis_result(fields)
        assert entries[0].mode == "positional"

    def test_skew_fade_setup_produces_credit_spread_entry(self):
        fields = _fields(priority_override="HIGH", analysis_json={
            "NEUTRAL": {
                "SKEW_FADE_SETUP": {
                    "fade_direction": "BULLISH",
                    "sr_level": "24000.0",
                    "exhaustion_confidence": "0.8",
                }
            }
        })
        entries, _ = parse_analysis_result(fields)
        assert len(entries) == 1
        signal = entries[0]
        assert signal.strategy == "CREDIT_SPREAD"
        assert signal.direction == "BULLISH"
        assert signal.sr_level == 24000.0
        assert signal.signal_source == "SKEW_FADE_SETUP"

    def test_malformed_range_bound_setup_is_skipped_not_raised(self):
        fields = _fields(priority_override="HIGH", analysis_json={
            "NEUTRAL": {"RANGE_BOUND_SETUP": {"setup_type": "IRON_CONDOR"}}  # missing strikes
        })
        entries, exits = parse_analysis_result(fields)
        assert entries == []
        assert exits == []

    def test_malformed_json_does_not_raise(self):
        fields = _fields(trend_found="true")
        fields["analysis_json"] = "{not valid json"
        entries, exits = parse_analysis_result(fields)
        assert entries == []
        assert exits == []

    def test_both_setups_in_same_cycle(self):
        fields = _fields(priority_override="HIGH", analysis_json={
            "NEUTRAL": {
                "RANGE_BOUND_SETUP": {
                    "setup_type": "STRANGLE", "put_wall_strike": "24000", "call_wall_strike": "25000",
                },
                "GAMMA_TRAP_ACTIVE": True,
            }
        })
        entries, exits = parse_analysis_result(fields)
        assert len(entries) == 1
        assert len(exits) == 1


class TestParseConfluenceMessage:
    def _confluence_fields(self, symbol="NIFTY", direction=Direction.BULLISH,
                            layers=(Layer.LIVE, Layer.INTRADAY), score=11.0):
        signals = [
            Signal(symbol=symbol, direction=direction, source="vwap_cross",
                   layer=layers[0], strength=SignalStrength.STRONG),
        ]
        if len(layers) > 1:
            signals.append(Signal(symbol=symbol, direction=direction, source="rsi_divergence",
                                   layer=layers[1], strength=SignalStrength.MODERATE))
        confluence = Confluence(
            symbol=symbol, direction=direction, signals=signals,
            layers_involved=set(layers), score=score,
        )
        return confluence.to_stream_fields()

    def test_moderate_confluence_maps_to_credit_spread(self):
        fields = self._confluence_fields(layers=(Layer.LIVE, Layer.INTRADAY))
        signal = parse_confluence_message(fields, indices=("NIFTY", "BANKNIFTY", "SENSEX"))
        assert signal.strategy == "CREDIT_SPREAD"
        assert signal.level == "MODERATE"
        assert signal.signal_source == "CONFLUENCE"

    def test_high_bullish_confluence_maps_to_naked_pe(self):
        fields = self._confluence_fields(direction=Direction.BULLISH,
                                          layers=(Layer.LIVE, Layer.INTRADAY, Layer.POSITIONAL))
        signal = parse_confluence_message(fields, indices=("NIFTY", "BANKNIFTY", "SENSEX"))
        assert signal.strategy == "NAKED_PE"
        assert signal.level == "HIGH"

    def test_high_bearish_confluence_maps_to_naked_ce(self):
        fields = self._confluence_fields(direction=Direction.BEARISH,
                                          layers=(Layer.LIVE, Layer.INTRADAY, Layer.POSITIONAL))
        signal = parse_confluence_message(fields, indices=("NIFTY", "BANKNIFTY", "SENSEX"))
        assert signal.strategy == "NAKED_CE"

    def test_non_index_symbol_is_ignored(self):
        fields = self._confluence_fields(symbol="RELIANCE")
        signal = parse_confluence_message(fields, indices=("NIFTY", "BANKNIFTY", "SENSEX"))
        assert signal is None


class TestParsePinnSignal:
    """Round-trips PinnSignal through the REAL emitter
    (services/volatility_engine/signal_emitter.emit_signal) so these tests
    catch any drift between the emitter's wire format and
    parse_pinn_signal()'s expectations, instead of hand-writing a fixture
    dict that could silently diverge from the real schema."""

    def _emitted_fields(self, signal: PinnSignal) -> dict:
        redis = MagicMock()
        emit_signal(redis, signal)
        args, _ = redis.xadd.call_args
        return args[1]

    def _skew_fade_signal(self, symbol="NIFTY", direction="BULLISH"):
        return PinnSignal(
            symbol=symbol, signal_type="SKEW_FADE_SETUP", strategy="CREDIT_SPREAD",
            direction=direction, z_score=2.5, expiry="2026-09-22",
            timestamp=datetime(2026, 9, 20, 10, 0),
            sr_level=24000.0, overpriced_type="PE", fair_iv=0.12, live_iv=0.15,
        )

    def test_skew_fade_maps_to_credit_spread_with_sr_level(self):
        fields = self._emitted_fields(self._skew_fade_signal())
        entry = parse_pinn_signal(fields)
        assert entry.strategy == "CREDIT_SPREAD"
        assert entry.symbol == "NIFTY"
        assert entry.direction == "BULLISH"
        assert entry.sr_level == 24000.0
        assert entry.signal_source == "PINN_MISPRICING"
        assert entry.mode == "intraday"
        assert entry.score == 2.5
        assert entry.signal_context["overpriced_type"] == "PE"

    def test_range_bound_maps_to_iron_condor_with_walls(self):
        signal = PinnSignal(
            symbol="BANKNIFTY", signal_type="RANGE_BOUND_SETUP", strategy="IRON_CONDOR",
            direction="NEUTRAL", z_score=1.8, expiry="2026-09-22",
            timestamp=datetime(2026, 9, 20, 10, 0),
            put_wall_strike=51000.0, call_wall_strike=52000.0,
            fair_iv_ce=0.13, fair_iv_pe=0.14, live_iv_ce=0.16, live_iv_pe=0.17,
        )
        fields = self._emitted_fields(signal)
        entry = parse_pinn_signal(fields)
        assert entry.strategy == "IRON_CONDOR"
        assert entry.put_wall_strike == 51000.0
        assert entry.call_wall_strike == 52000.0
        assert entry.signal_source == "PINN_MISPRICING"
        assert entry.mode == "intraday"

    def test_non_index_symbol_is_ignored(self):
        fields = self._emitted_fields(self._skew_fade_signal(symbol="RELIANCE"))
        assert parse_pinn_signal(fields) is None

    def test_missing_symbol_is_ignored(self):
        assert parse_pinn_signal({"signal_type": "SKEW_FADE_SETUP"}) is None

    def test_unknown_signal_type_returns_none(self):
        assert parse_pinn_signal({"signal_type": "SOMETHING_ELSE", "symbol": "NIFTY"}) is None

    def test_malformed_numeric_field_returns_none(self):
        fields = {
            "signal_type": "SKEW_FADE_SETUP", "symbol": "NIFTY", "strategy": "CREDIT_SPREAD",
            "direction": "BULLISH", "sr_level": "not-a-number", "z_score": "1.0",
        }
        assert parse_pinn_signal(fields) is None


class TestEntryFilterPredicates:
    def test_has_cooldown_lock_true_when_key_present(self):
        redis = MagicMock()
        redis.get.return_value = "1"
        assert has_cooldown_lock(redis, "NIFTY", "IRON_CONDOR") is True

    def test_has_cooldown_lock_false_when_key_absent(self):
        redis = MagicMock()
        redis.get.return_value = None
        assert has_cooldown_lock(redis, "NIFTY", "IRON_CONDOR") is False

    def test_has_duplicate_position(self):
        positions = [PaperPosition(
            position_id="1", symbol="NIFTY", strategy="IRON_CONDOR", mode="intraday",
            direction="NEUTRAL", legs=[], expiry="2026-07-21", scrip="NIFTY26721",
            lot_size=65, entry_timestamp=0.0, entry_credit=0.0, margin_blocked=0.0,
        )]
        assert has_duplicate_position(positions, "NIFTY", "IRON_CONDOR") is True
        assert has_duplicate_position(positions, "NIFTY", "STRANGLE") is False
        assert has_duplicate_position(positions, "BANKNIFTY", "IRON_CONDOR") is False

    def test_portfolio_margin_exceeded(self):
        account = PaperAccount(capital=1_000_000.0, margin_used=400_000.0)
        assert portfolio_margin_exceeded(account) is True
        account.margin_used = 399_999.0
        assert portfolio_margin_exceeded(account) is False

    def test_daily_loss_limit_hit(self):
        account = PaperAccount(capital=1_000_000.0, daily_realized_pnl=-30_000.0)
        assert daily_loss_limit_hit(account) is True
        account.daily_realized_pnl = -29_999.0
        assert daily_loss_limit_hit(account) is False

    def test_correlated_naked_cap(self):
        positions = [
            PaperPosition(position_id=str(i), symbol=s, strategy="NAKED_PE", mode="intraday",
                          direction="BULLISH", legs=[], expiry="2026-07-21", scrip="X",
                          lot_size=65, entry_timestamp=0.0, entry_credit=0.0, margin_blocked=0.0)
            for i, s in enumerate(["NIFTY", "BANKNIFTY"])
        ]
        assert correlated_naked_cap_hit(positions, "BULLISH") is True
        assert correlated_naked_cap_hit(positions, "BEARISH") is False
        assert correlated_naked_cap_hit(positions[:1], "BULLISH") is False


class TestCheckEntryFilters:
    def _redis(self, cooldown=None, open_count=3):
        redis = MagicMock()
        redis.get.return_value = cooldown
        redis.hlen.return_value = open_count
        return redis

    def test_passes_when_all_clear(self):
        signal = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY")
        account = PaperAccount(capital=1_000_000.0)
        passed, reason = check_entry_filters(signal, self._redis(), account, [])
        assert passed is True
        assert reason == ""

    def test_fails_on_cooldown(self):
        signal = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY")
        account = PaperAccount(capital=1_000_000.0)
        passed, reason = check_entry_filters(signal, self._redis(cooldown="1"), account, [])
        assert passed is False
        assert reason == "cooldown_active"

    def test_fails_on_max_positions(self):
        signal = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY")
        account = PaperAccount(capital=1_000_000.0)
        passed, reason = check_entry_filters(signal, self._redis(open_count=8), account, [])
        assert passed is False
        assert reason == "max_positions_reached"

    def test_fails_on_daily_loss_limit(self):
        signal = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY")
        account = PaperAccount(capital=1_000_000.0, daily_realized_pnl=-40_000.0)
        passed, reason = check_entry_filters(signal, self._redis(), account, [])
        assert passed is False
        assert reason == "daily_loss_limit_hit"

    def test_naked_strategy_blocked_by_correlated_cap(self):
        signal = EntrySignal(strategy="NAKED_PE", symbol="SENSEX", direction="BULLISH")
        account = PaperAccount(capital=1_000_000.0)
        positions = [
            PaperPosition(position_id=str(i), symbol=s, strategy="NAKED_CE", mode="intraday",
                          direction="BULLISH", legs=[], expiry="2026-07-21", scrip="X",
                          lot_size=65, entry_timestamp=0.0, entry_credit=0.0, margin_blocked=0.0)
            for i, s in enumerate(["NIFTY", "BANKNIFTY"])
        ]
        passed, reason = check_entry_filters(signal, self._redis(), account, positions)
        assert passed is False
        assert reason == "correlated_naked_cap_hit"

    def test_defined_risk_strategy_not_subject_to_correlated_cap(self):
        signal = EntrySignal(strategy="IRON_CONDOR", symbol="SENSEX", direction="BULLISH")
        account = PaperAccount(capital=1_000_000.0)
        positions = [
            PaperPosition(position_id=str(i), symbol=s, strategy="NAKED_CE", mode="intraday",
                          direction="BULLISH", legs=[], expiry="2026-07-21", scrip="X",
                          lot_size=65, entry_timestamp=0.0, entry_credit=0.0, margin_blocked=0.0)
            for i, s in enumerate(["NIFTY", "BANKNIFTY"])
        ]
        passed, _ = check_entry_filters(signal, self._redis(), account, positions)
        assert passed is True
