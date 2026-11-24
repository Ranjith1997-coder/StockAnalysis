"""Tests for services/paper_trading/strategy_builder.py."""

import json
from unittest.mock import MagicMock, patch

from services.paper_trading.models import PaperAccount
from services.paper_trading.signal_router import EntrySignal
from services.paper_trading.span_calculator import InstrumentEntry, SpanCalculator
from services.paper_trading.strategy_builder import (
    FilledLeg,
    PlannedLeg,
    build_position,
    compute_entry_costs,
    compute_entry_credit,
    compute_lots,
    fetch_atm_strike,
    fetch_expiry,
    fetch_strike_gap,
    fetch_tick,
    get_ltp,
    has_valid_greeks,
    select_strikes,
)


class TestComputeLots:
    def test_normal_case(self):
        assert compute_lots(1_000_000.0, 68578.86, 0.08) == 1

    def test_zero_when_margin_exceeds_budget(self):
        assert compute_lots(1_000_000.0, 155069.0, 0.05) == 0

    def test_zero_margin_is_safe(self):
        assert compute_lots(1_000_000.0, 0.0, 0.08) == 0


class TestSelectStrikesIronCondorStrangle:
    def test_iron_condor_has_four_legs(self):
        signal = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY",
                              put_wall_strike=24000.0, call_wall_strike=25000.0)
        legs = select_strikes(signal, strike_gap=50.0)
        assert len(legs) == 4
        sells = [leg for leg in legs if leg.side == "SELL"]
        buys = [leg for leg in legs if leg.side == "BUY"]
        assert {leg.strike for leg in sells} == {24000.0, 25000.0}
        assert {leg.strike for leg in buys} == {23900.0, 25100.0}

    def test_strangle_has_two_naked_legs(self):
        signal = EntrySignal(strategy="STRANGLE", symbol="NIFTY",
                              put_wall_strike=24000.0, call_wall_strike=25000.0)
        legs = select_strikes(signal, strike_gap=50.0)
        assert len(legs) == 2
        assert all(leg.side == "SELL" for leg in legs)

    def test_missing_wall_strikes_returns_empty(self):
        signal = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY")
        assert select_strikes(signal, strike_gap=50.0) == []


class TestSelectStrikesCreditSpread:
    def test_skew_fade_bullish_sells_put_spread(self):
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BULLISH",
                              sr_level=24000.0, signal_source="SKEW_FADE_SETUP")
        legs = select_strikes(signal, strike_gap=50.0)
        assert legs == [
            PlannedLeg(24000.0, "PE", "SELL"),
            PlannedLeg(23900.0, "PE", "BUY"),
        ]

    def test_skew_fade_bearish_sells_call_spread(self):
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BEARISH",
                              sr_level=25000.0, signal_source="SKEW_FADE_SETUP")
        legs = select_strikes(signal, strike_gap=50.0)
        assert legs == [
            PlannedLeg(25000.0, "CE", "SELL"),
            PlannedLeg(25100.0, "CE", "BUY"),
        ]

    def test_confluence_moderate_bullish_sells_put_spread_near_atm(self):
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BULLISH",
                              signal_source="CONFLUENCE", level="MODERATE")
        legs = select_strikes(signal, strike_gap=50.0, atm_strike=24500.0)
        assert legs == [
            PlannedLeg(24450.0, "PE", "SELL"),
            PlannedLeg(24400.0, "PE", "BUY"),
        ]

    def test_missing_sr_level_for_skew_fade_returns_empty(self):
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BULLISH",
                              signal_source="SKEW_FADE_SETUP")
        assert select_strikes(signal, strike_gap=50.0) == []

    def test_pinn_mispricing_bullish_sells_put_spread(self):
        """Regression guard: PINN_MISPRICING signals carry sr_level too (see
        signal_router.parse_pinn_signal()), but the OLD code only checked
        signal_source == "SKEW_FADE_SETUP" and silently fell through to the
        CONFLUENCE atm_strike path for anything else -- the one-line fix is
        checking sr_level's presence instead of the specific source string."""
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BULLISH",
                              sr_level=24000.0, signal_source="PINN_MISPRICING")
        legs = select_strikes(signal, strike_gap=50.0)
        assert legs == [
            PlannedLeg(24000.0, "PE", "SELL"),
            PlannedLeg(23900.0, "PE", "BUY"),
        ]

    def test_missing_atm_for_confluence_credit_spread_returns_empty(self):
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BULLISH",
                              signal_source="CONFLUENCE")
        assert select_strikes(signal, strike_gap=50.0, atm_strike=None) == []


class TestSelectStrikesHighConfluence:
    def test_high_bullish_sells_put_spread_two_strikes_otm_with_wing(self):
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BULLISH",
                              signal_source="CONFLUENCE", level="HIGH", score=12.0)
        legs = select_strikes(signal, strike_gap=50.0, atm_strike=24500.0)
        assert legs == [
            PlannedLeg(24400.0, "PE", "SELL"),
            PlannedLeg(24300.0, "PE", "BUY"),
        ]

    def test_high_bearish_very_high_score_goes_three_strikes_otm(self):
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BEARISH",
                              signal_source="CONFLUENCE", level="HIGH", score=21.0)
        legs = select_strikes(signal, strike_gap=50.0, atm_strike=24500.0)
        assert legs == [
            PlannedLeg(24650.0, "CE", "SELL"),
            PlannedLeg(24750.0, "CE", "BUY"),
        ]

    def test_naked_strategies_no_longer_opened(self):
        for strat in ("NAKED_PE", "NAKED_CE"):
            signal = EntrySignal(strategy=strat, symbol="NIFTY", direction="BULLISH", score=8.0)
            assert select_strikes(signal, strike_gap=50.0, atm_strike=24500.0) == []

    def test_neutral_direction_confluence_returns_empty(self):
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="NEUTRAL",
                              signal_source="CONFLUENCE", level="HIGH")
        assert select_strikes(signal, strike_gap=50.0, atm_strike=24500.0) == []


class TestGetLtpAndGreeks:
    def test_get_ltp_valid(self):
        assert get_ltp({"ltp": "84.6"}) == 84.6

    def test_get_ltp_zero_is_none(self):
        assert get_ltp({"ltp": 0}) is None

    def test_get_ltp_missing_is_none(self):
        assert get_ltp({}) is None

    def test_has_valid_greeks_true(self):
        assert has_valid_greeks({"iv": "18.4"}) is True

    def test_has_valid_greeks_false_when_zero_or_missing(self):
        assert has_valid_greeks({"iv": 0}) is False
        assert has_valid_greeks({}) is False


class TestCostAndCreditAccounting:
    def test_entry_stt_on_sold_leg_stamp_on_bought_leg(self):
        from services.paper_trading.models import DEFAULT_STAMP_DUTY_PCT, DEFAULT_STT_PCT
        legs = [
            FilledLeg(24000.0, "PE", "SELL", 84.18),
            FilledLeg(23900.0, "PE", "BUY", 62.41),
        ]
        with_statutory = compute_entry_costs(legs, qty=65)
        without = compute_entry_costs(legs, qty=65, stt_pct=0.0, stamp_duty_pct=0.0)
        expected = DEFAULT_STT_PCT * 84.18 * 65 + DEFAULT_STAMP_DUTY_PCT * 62.41 * 65
        assert abs((with_statutory - without) - expected) < 1e-6

    def test_entry_costs_include_brokerage_and_sell_side_charges(self):
        legs = [
            FilledLeg(24000.0, "PE", "SELL", 84.18),
            FilledLeg(23900.0, "PE", "BUY", 62.41),
        ]
        costs = compute_entry_costs(legs, qty=65)
        # brokerage(2 legs) + exchange(both legs) + stt+stamp(sell leg only)
        assert costs > compute_entry_costs([legs[1]], qty=65)  # sell leg costs more than buy-only

    def test_entry_credit_nets_costs(self):
        legs = [
            FilledLeg(24000.0, "PE", "SELL", 84.18),
            FilledLeg(23900.0, "PE", "BUY", 62.41),
        ]
        costs = compute_entry_costs(legs, qty=65)
        credit = compute_entry_credit(legs, qty=65, entry_costs=costs)
        gross = (84.18 - 62.41) * 65
        assert credit == gross - costs
        assert credit < gross


class TestRedisFacingWrappers:
    def test_fetch_expiry_intraday(self):
        redis = MagicMock()
        redis.hget.return_value = json.dumps({
            "per_expiry_map": {"2026-07-28": {}, "2026-07-21": {}}
        })
        from datetime import date
        assert fetch_expiry("NIFTY", "intraday", redis, today=date(2026, 7, 19)) == "2026-07-21"

    def test_fetch_expiry_missing_data_returns_empty(self):
        redis = MagicMock()
        redis.hget.return_value = None
        assert fetch_expiry("NIFTY", "intraday", redis) == ""

    def test_fetch_strike_gap(self):
        redis = MagicMock()
        redis.hgetall.return_value = {"24000.0_CE": "{}", "24050.0_CE": "{}"}
        assert fetch_strike_gap("NIFTY", redis) == 50.0

    def test_fetch_strike_gap_empty_uses_default(self):
        redis = MagicMock()
        redis.hgetall.return_value = {}
        assert fetch_strike_gap("NIFTY", redis) == 50.0

    def test_fetch_atm_strike(self):
        redis = MagicMock()
        redis.hget.return_value = "24500.0"
        assert fetch_atm_strike("NIFTY", redis) == 24500.0

    def test_fetch_atm_strike_missing(self):
        redis = MagicMock()
        redis.hget.return_value = None
        assert fetch_atm_strike("NIFTY", redis) is None

    def test_fetch_tick(self):
        redis = MagicMock()
        redis.hget.return_value = json.dumps({"ltp": 84.6, "iv": 18.4})
        tick = fetch_tick("NIFTY", 24000.0, "PE", redis)
        assert tick["ltp"] == 84.6

    def test_fetch_tick_missing_returns_none(self):
        redis = MagicMock()
        redis.hget.return_value = None
        assert fetch_tick("NIFTY", 24000.0, "PE", redis) is None


def _nifty_instruments():
    return {
        "NIFTY": {
            "2026-07-21": [
                InstrumentEntry(24000.0, "PE", "NIFTY2672124000PE", 65, "NFO"),
                InstrumentEntry(23900.0, "PE", "NIFTY2672123900PE", 65, "NFO"),
                InstrumentEntry(25000.0, "CE", "NIFTY2672125000CE", 65, "NFO"),
                InstrumentEntry(25100.0, "CE", "NIFTY2672125100CE", 65, "NFO"),
            ]
        }
    }


class TestBuildPositionEndToEnd:
    def _redis(self, ticks: dict, expiry="2026-07-21", strike_gap_keys=None):
        redis = MagicMock()

        def hget(key, field=None):
            if key.startswith("data:sensibull:"):
                return json.dumps({"per_expiry_map": {expiry: {}}})
            if key.startswith("data:options_agg:"):
                return "24500.0"
            if key.startswith("data:options_live:"):
                return json.dumps(ticks[field]) if field in ticks else None
            return None

        redis.hget.side_effect = hget
        redis.hgetall.return_value = {k: "{}" for k in (strike_gap_keys or ticks.keys())}
        return redis

    def _signal(self):
        return EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY",
                            put_wall_strike=24000.0, call_wall_strike=25000.0,
                            signal_source="RANGE_BOUND_SETUP")

    @patch("services.paper_trading.strategy_builder.time.time", return_value=1000.0)
    def test_happy_path_builds_position(self, _mock_time):
        ticks = {
            "24000.0_PE": {"ltp": 84.6, "iv": 18.4},
            "23900.0_PE": {"ltp": 62.1, "iv": 18.0},
            "25000.0_CE": {"ltp": 77.4, "iv": 17.5},
            "25100.0_CE": {"ltp": 55.3, "iv": 17.0},
        }
        # Realistic full strike ladder (50-wide) for strike-gap derivation --
        # the 4 traded strikes alone would look 100-wide and throw off ±2*gap math.
        full_chain_keys = [f"{s}.0_CE" for s in range(23800, 25300, 50)]
        redis = self._redis(ticks, strike_gap_keys=full_chain_keys)
        span = SpanCalculator(_nifty_instruments())
        with patch.object(span, "calculate_margin", return_value=68578.86):
            account = PaperAccount(capital=1_000_000.0)
            position = build_position(self._signal(), redis, span, account)

        assert position is not None
        assert position.symbol == "NIFTY"
        assert position.strategy == "IRON_CONDOR"
        assert position.scrip == "NIFTY26721"
        assert position.lot_size == 65
        assert len(position.legs) == 4
        assert position.margin_blocked == 68578.86  # 1 lot
        assert position.entry_credit > 0

    def test_skips_when_illiquid_strike(self):
        ticks = {
            "24000.0_PE": {"ltp": 84.6, "iv": 18.4},
            # 23900.0_PE missing entirely -> illiquid
            "25000.0_CE": {"ltp": 77.4, "iv": 17.5},
            "25100.0_CE": {"ltp": 55.3, "iv": 17.0},
        }
        redis = self._redis(ticks, strike_gap_keys=["24000.0_PE", "25000.0_CE"])
        span = SpanCalculator(_nifty_instruments())
        account = PaperAccount(capital=1_000_000.0)
        assert build_position(self._signal(), redis, span, account) is None

    def test_skips_when_short_leg_missing_greeks(self):
        ticks = {
            "24000.0_PE": {"ltp": 84.6, "iv": 0},   # short leg, no iv
            "23900.0_PE": {"ltp": 62.1, "iv": 18.0},
            "25000.0_CE": {"ltp": 77.4, "iv": 17.5},
            "25100.0_CE": {"ltp": 55.3, "iv": 17.0},
        }
        redis = self._redis(ticks)
        span = SpanCalculator(_nifty_instruments())
        account = PaperAccount(capital=1_000_000.0)
        assert build_position(self._signal(), redis, span, account) is None

    def test_skips_when_span_returns_none(self):
        ticks = {
            "24000.0_PE": {"ltp": 84.6, "iv": 18.4},
            "23900.0_PE": {"ltp": 62.1, "iv": 18.0},
            "25000.0_CE": {"ltp": 77.4, "iv": 17.5},
            "25100.0_CE": {"ltp": 55.3, "iv": 17.0},
        }
        redis = self._redis(ticks)
        span = SpanCalculator(_nifty_instruments())
        with patch.object(span, "calculate_margin", return_value=None):
            account = PaperAccount(capital=1_000_000.0)
            assert build_position(self._signal(), redis, span, account) is None

    def test_skips_when_zero_lots_from_sizing(self):
        ticks = {
            "24000.0_PE": {"ltp": 84.6, "iv": 18.4},
            "23900.0_PE": {"ltp": 62.1, "iv": 18.0},
            "25000.0_CE": {"ltp": 77.4, "iv": 17.5},
            "25100.0_CE": {"ltp": 55.3, "iv": 17.0},
        }
        redis = self._redis(ticks)
        span = SpanCalculator(_nifty_instruments())
        with patch.object(span, "calculate_margin", return_value=68578.86):
            account = PaperAccount(capital=1000.0)  # tiny capital -> 0 lots
            assert build_position(self._signal(), redis, span, account) is None

    @patch("services.paper_trading.strategy_builder.time.time", return_value=1000.0)
    def test_lots_capped_by_portfolio_margin_headroom(self, _mock_time):
        ticks = {
            "24000.0_PE": {"ltp": 84.6, "iv": 18.4},
            "23900.0_PE": {"ltp": 62.1, "iv": 18.0},
            "25000.0_CE": {"ltp": 77.4, "iv": 17.5},
            "25100.0_CE": {"ltp": 55.3, "iv": 17.0},
        }
        full_chain_keys = [f"{s}.0_CE" for s in range(23800, 25300, 50)]
        redis = self._redis(ticks, strike_gap_keys=full_chain_keys)
        span = SpanCalculator(_nifty_instruments())
        with patch.object(span, "calculate_margin", return_value=60_000.0):
            # risk sizing alone -> 1.33 -> 1 lot; but only 50k headroom under 40% cap
            account = PaperAccount(capital=1_000_000.0, margin_used=350_000.0)
            assert build_position(self._signal(), redis, span, account) is None

    def test_expiry_day_after_cutoff_blocks_entry(self):
        from datetime import datetime
        redis = self._redis({}, expiry="2026-07-21")
        span = SpanCalculator(_nifty_instruments())
        account = PaperAccount(capital=1_000_000.0)
        assert build_position(self._signal(), redis, span, account,
                              now=datetime(2026, 7, 21, 14, 5)) is None

    @patch("services.paper_trading.strategy_builder.time.time", return_value=1000.0)
    def test_signal_expiry_used_when_present(self, _mock_time):
        from datetime import datetime
        ticks = {
            "24000.0_PE": {"ltp": 84.6, "iv": 18.4},
            "23900.0_PE": {"ltp": 62.1, "iv": 18.0},
            "25000.0_CE": {"ltp": 77.4, "iv": 17.5},
            "25100.0_CE": {"ltp": 55.3, "iv": 17.0},
        }
        full_chain_keys = [f"{s}.0_CE" for s in range(23800, 25300, 50)]
        # chain would pick 2026-07-28; PINN signal says 2026-07-21
        redis = self._redis(ticks, expiry="2026-07-28", strike_gap_keys=full_chain_keys)
        span = SpanCalculator(_nifty_instruments())
        signal = self._signal()
        signal.expiry = "2026-07-21"
        with patch.object(span, "calculate_margin", return_value=68578.86):
            position = build_position(signal, redis, span, PaperAccount(capital=1_000_000.0),
                                      now=datetime(2026, 7, 19, 10, 0))
        assert position is not None
        assert position.expiry == "2026-07-21"

    def test_skips_when_no_expiry(self):
        redis = MagicMock()
        redis.hget.return_value = None
        span = SpanCalculator(_nifty_instruments())
        account = PaperAccount(capital=1_000_000.0)
        assert build_position(self._signal(), redis, span, account) is None


class TestPopGate:
    def test_disabled_by_default(self, monkeypatch):
        from services.paper_trading.strategy_builder import _min_pop_from_env

        monkeypatch.delenv("PAPER_MIN_POP", raising=False)
        assert _min_pop_from_env() == 0.0
        monkeypatch.setenv("PAPER_MIN_POP", "0.7")
        assert _min_pop_from_env() == 0.7
        monkeypatch.setenv("PAPER_MIN_POP", "not-a-number")
        assert _min_pop_from_env() == 0.0

    @patch("services.paper_trading.strategy_builder.time.time", return_value=1000.0)
    def test_enabled_gate_widens_the_short_strikes(self, _mock_time):
        from lib.fno.option_math import widen_short_strikes

        expected_put, expected_call = widen_short_strikes(
            24_000, 25_000, 24_500, 50, 0.18, 2 / 365, 0.95
        )
        assert (expected_put, expected_call) != (24_000, 25_000)

        ticks = {}
        for strike in range(23_400, 25_700, 50):
            ticks[f"{float(strike)}_CE"] = {"ltp": 50.0, "iv": 18.0}
            ticks[f"{float(strike)}_PE"] = {"ltp": 50.0, "iv": 18.0}

        redis = MagicMock()
        def hget(key, field=None):
            if key.startswith("data:sensibull:"):
                return json.dumps({"per_expiry_map": {"2026-07-21": {}}})
            if key.startswith("data:options_agg:"):
                return "24500.0" if field == "atm_strike" else None
            if key.startswith("data:options_live:"):
                return json.dumps(ticks[field]) if field in ticks else None
            return None
        redis.hget.side_effect = hget
        redis.hgetall.side_effect = lambda key: (
            {"atm_iv": "18.0", "dte": "2"}
            if key.startswith("data:options_agg:")
            else {k: "{}" for k in ticks}
        )

        span = SpanCalculator(_nifty_instruments())
        with patch.object(span, "calculate_margin", return_value=68_578.86):
            position = build_position(
                EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY",
                            put_wall_strike=24_000.0, call_wall_strike=25_000.0),
                redis, span, PaperAccount(capital=1_000_000.0),
                min_pop=0.95,
            )

        assert position is not None
        shorts = {leg.strike for leg in position.legs if leg.side == "SELL"}
        assert shorts == {expected_put, expected_call}
