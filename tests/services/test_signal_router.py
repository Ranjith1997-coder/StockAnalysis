"""Tests for services/paper_trading/signal_router.py."""

import json
from unittest.mock import MagicMock

from intelligence.correlator import Confluence
from intelligence.signal import Direction, Layer, Signal, SignalStrength
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
    portfolio_margin_exceeded,
)


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
