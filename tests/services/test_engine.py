"""Tests for services/paper_trading/engine.py."""

import json
from datetime import date, datetime, time as dtime
from unittest.mock import MagicMock

from services.paper_trading.engine import (
    check_gamma_trap_proxy,
    check_profit_target,
    check_stop_loss,
    check_theta_decay,
    check_time_squareoff,
    close_position,
    compute_current_debit,
    compute_dte,
    compute_exit_costs,
    compute_exit_pnl,
    compute_qty,
    compute_unrealized_pnl,
    determine_exit_reason,
    evaluate_position,
    is_agg_data_stale,
    update_leg_premiums,
)
from services.paper_trading.models import OptionLeg, PaperPosition


def _position(mode="intraday", expiry="2026-07-21", entry_credit=2900.0, lot_size=65, lots=1):
    legs = [
        OptionLeg(strike=24000.0, option_type="PE", side="SELL", lots=lots,
                  entry_premium=84.18, current_premium=84.18),
        OptionLeg(strike=23900.0, option_type="PE", side="BUY", lots=lots,
                  entry_premium=62.41, current_premium=62.41),
        OptionLeg(strike=25000.0, option_type="CE", side="SELL", lots=lots,
                  entry_premium=77.02, current_premium=77.02),
        OptionLeg(strike=25100.0, option_type="CE", side="BUY", lots=lots,
                  entry_premium=55.58, current_premium=55.58),
    ]
    return PaperPosition(
        position_id="p1", symbol="NIFTY", strategy="IRON_CONDOR", mode=mode,
        direction="NEUTRAL", legs=legs, expiry=expiry, scrip="NIFTY26721",
        lot_size=lot_size, entry_timestamp=0.0, entry_credit=entry_credit,
        margin_blocked=68578.86,
    )


class TestStaleDataGuard:
    def test_missing_agg_is_stale(self):
        assert is_agg_data_stale({}) is True

    def test_fresh_data_not_stale(self):
        assert is_agg_data_stale({"last_updated": "1000.0"}, now=1005.0) is False

    def test_old_data_is_stale(self):
        assert is_agg_data_stale({"last_updated": "1000.0"}, now=1011.0) is True

    def test_boundary_exactly_10s_not_stale(self):
        assert is_agg_data_stale({"last_updated": "1000.0"}, now=1010.0) is False


class TestUpdateLegPremiums:
    def test_updates_all_legs_when_ticks_present(self):
        position = _position()
        options_live = {
            "24000.0_PE": json.dumps({"ltp": 90.0}),
            "23900.0_PE": json.dumps({"ltp": 65.0}),
            "25000.0_CE": json.dumps({"ltp": 70.0}),
            "25100.0_CE": json.dumps({"ltp": 50.0}),
        }
        result = update_leg_premiums(position, options_live)
        assert result is True
        assert position.legs[0].current_premium == 90.0

    def test_missing_leg_keeps_last_known_premium(self):
        position = _position()
        options_live = {
            "24000.0_PE": json.dumps({"ltp": 90.0}),
            # other 3 legs missing
        }
        result = update_leg_premiums(position, options_live)
        assert result is False
        assert position.legs[0].current_premium == 90.0
        assert position.legs[1].current_premium == 62.41  # unchanged

    def test_zero_ltp_does_not_overwrite(self):
        position = _position()
        options_live = {
            "24000.0_PE": json.dumps({"ltp": 0}),
            "23900.0_PE": json.dumps({"ltp": 65.0}),
            "25000.0_CE": json.dumps({"ltp": 70.0}),
            "25100.0_CE": json.dumps({"ltp": 50.0}),
        }
        update_leg_premiums(position, options_live)
        assert position.legs[0].current_premium == 84.18  # unchanged, kept last known


class TestMtmComputation:
    def test_compute_qty(self):
        position = _position(lot_size=65, lots=1)
        assert compute_qty(position) == 65

    def test_compute_current_debit(self):
        position = _position()
        # sell_value = (84.18+77.02)*65 = 10488.0; buy_value=(62.41+55.58)*65=7674.35 (approx)
        debit = compute_current_debit(position)
        expected = ((84.18 + 77.02) - (62.41 + 55.58)) * 65
        assert round(debit, 2) == round(expected, 2)

    def test_compute_unrealized_pnl(self):
        position = _position(entry_credit=2809.0)
        debit = compute_current_debit(position)
        pnl = compute_unrealized_pnl(position, debit)
        assert pnl == 2809.0 - debit


class TestGammaTrapProxy:
    def test_triggers_above_threshold(self):
        assert check_gamma_trap_proxy({"last_price": 24980, "close": 24480}) is True

    def test_does_not_trigger_below_threshold(self):
        assert check_gamma_trap_proxy({"last_price": 24500, "close": 24480}) is False

    def test_empty_tick_is_false(self):
        assert check_gamma_trap_proxy({}) is False

    def test_zero_close_is_false(self):
        assert check_gamma_trap_proxy({"last_price": 100, "close": 0}) is False


class TestExitRulePredicates:
    def test_stop_loss_triggers_at_200pct_loss(self):
        assert check_stop_loss(unrealized_pnl=-5800.0, entry_credit=2900.0) is True
        assert check_stop_loss(unrealized_pnl=-5799.0, entry_credit=2900.0) is False

    def test_profit_target_triggers_at_50pct(self):
        assert check_profit_target(unrealized_pnl=1450.0, entry_credit=2900.0) is True
        assert check_profit_target(unrealized_pnl=1449.0, entry_credit=2900.0) is False

    def test_theta_decay_on_expiry_day_needs_time_gate(self):
        # dte == 0 -- position's own expiry is today -- uses the tight 14:00 gate
        before_gate = datetime(2026, 7, 19, 13, 59)
        after_gate = datetime(2026, 7, 19, 14, 0)
        assert check_theta_decay(current_debit=700.0, entry_credit=2900.0,
                                  dte=0, now=before_gate) is False
        assert check_theta_decay(current_debit=700.0, entry_credit=2900.0,
                                  dte=0, now=after_gate) is True

    def test_theta_decay_not_enough_decay_never_fires(self):
        now = datetime(2026, 7, 19, 15, 0)
        assert check_theta_decay(current_debit=1000.0, entry_credit=2900.0,
                                  dte=0, now=now) is False

    def test_theta_decay_multi_day_out_uses_dte_gate_not_clock_time(self):
        # dte > 0 -- no same-day clock gate, decay alone is enough once dte<=3
        now = datetime(2026, 7, 19, 10, 0)
        assert check_theta_decay(current_debit=700.0, entry_credit=2900.0,
                                  dte=3, now=now) is True
        assert check_theta_decay(current_debit=700.0, entry_credit=2900.0,
                                  dte=4, now=now) is False

    def test_time_squareoff_on_expiry_day(self):
        assert check_time_squareoff(dte=0, now=datetime(2026, 7, 19, 15, 15)) is True
        assert check_time_squareoff(dte=0, now=datetime(2026, 7, 19, 15, 14)) is False

    def test_time_squareoff_day_before_expiry(self):
        assert check_time_squareoff(dte=1, now=datetime(2026, 7, 19, 15, 0)) is True
        assert check_time_squareoff(dte=2, now=datetime(2026, 7, 19, 15, 0)) is False

    def test_compute_dte(self):
        assert compute_dte("2026-07-21", date(2026, 7, 19)) == 2


class TestDetermineExitReasonPriority:
    def test_gamma_trap_takes_priority_over_everything(self):
        position = _position(entry_credit=2900.0)
        reason = determine_exit_reason(position, unrealized_pnl=1450.0, current_debit=1450.0,
                                        now=datetime(2026, 7, 19, 10, 0), gamma_trap_triggered=True)
        assert reason == "GAMMA_TRAP"

    def test_stop_loss_before_target(self):
        position = _position(entry_credit=2900.0)
        # both stop-loss and (hypothetically) other conditions can't both be true here,
        # just confirm stop loss fires when triggered
        reason = determine_exit_reason(position, unrealized_pnl=-6000.0, current_debit=8900.0,
                                        now=datetime(2026, 7, 19, 10, 0))
        assert reason == "STOP_LOSS"

    def test_target_fires_when_profitable_enough(self):
        position = _position(entry_credit=2900.0)
        reason = determine_exit_reason(position, unrealized_pnl=1500.0, current_debit=1400.0,
                                        now=datetime(2026, 7, 19, 10, 0))
        assert reason == "TARGET"

    def test_no_exit_when_nothing_triggers(self):
        position = _position(entry_credit=2900.0, expiry="2026-08-04")
        reason = determine_exit_reason(position, unrealized_pnl=500.0, current_debit=2400.0,
                                        now=datetime(2026, 7, 19, 10, 0))
        assert reason is None

    def test_squareoff_tagged_expiry_on_the_expiry_day_itself(self):
        # dte == 0 -- reuses the same 15:15 gate as any same-day square-off,
        # but tags the reason distinctly for the trade log
        position = _position(entry_credit=2900.0, expiry="2026-07-19")
        reason = determine_exit_reason(position, unrealized_pnl=0.0, current_debit=2900.0,
                                        now=datetime(2026, 7, 19, 15, 25))
        assert reason == "EXPIRY"

    def test_squareoff_tagged_plain_when_not_expiry_day(self):
        # dte == 1 -- day before expiry, same rule fires under the SQUARE_OFF tag
        position = _position(entry_credit=2900.0, expiry="2026-07-20")
        reason = determine_exit_reason(position, unrealized_pnl=0.0, current_debit=2900.0,
                                        now=datetime(2026, 7, 19, 15, 25))
        assert reason == "SQUARE_OFF"

    def test_position_untouched_2_days_before_expiry_before_any_gate(self):
        # dte == 2 -- neither the theta-decay dte<=3 gate needs clock time nor
        # square-off's dte==1 condition applies here; confirms no gate misfires early
        position = _position(entry_credit=2900.0, expiry="2026-07-21")
        reason = determine_exit_reason(position, unrealized_pnl=500.0, current_debit=2400.0,
                                        now=datetime(2026, 7, 19, 10, 0))
        assert reason is None


class TestExitCostsAndPnl:
    def test_exit_costs_include_stt_on_buyback_legs_only(self):
        position = _position()
        qty = compute_qty(position)
        costs = compute_exit_costs(position, qty)
        # buy-back (originally SELL) legs incur STT+stamp; originally-BUY legs do not
        assert costs > 0

    def test_exit_pnl_does_not_double_subtract_entry_costs(self):
        position = _position(entry_credit=2900.0)
        current_debit = 1450.0
        qty = compute_qty(position)
        exit_costs = compute_exit_costs(position, qty)
        pnl = compute_exit_pnl(position, current_debit, exit_costs)
        assert pnl == (2900.0 - current_debit) - exit_costs


class TestClosePosition:
    def test_sets_all_exit_fields(self):
        position = _position(entry_credit=2900.0)
        closed = close_position(position, "TARGET", current_debit=1450.0, now=12345.0)
        assert closed.status == "CLOSED"
        assert closed.exit_reason == "TARGET"
        assert closed.exit_timestamp == 12345.0
        assert closed.exit_premium == 1450.0
        assert closed.pnl is not None


class TestEvaluatePosition:
    def _redis(self, agg, live):
        redis = MagicMock()
        redis.hgetall.side_effect = lambda key: agg if "options_agg" in key else live
        return redis

    def test_skips_when_agg_stale(self):
        position = _position()
        redis = self._redis(agg={}, live={})
        assert evaluate_position(position, redis) is None

    def test_skips_when_options_live_empty(self):
        position = _position()
        now = datetime.now()
        redis = self._redis(agg={"last_updated": str(now.timestamp())}, live={})
        assert evaluate_position(position, redis, now=now) is None

    def test_returns_open_position_when_no_exit_fires(self):
        position = _position(entry_credit=2900.0, expiry="2026-08-04")
        now = datetime(2026, 7, 19, 10, 0)
        live = {
            "24000.0_PE": json.dumps({"ltp": 80.0}),
            "23900.0_PE": json.dumps({"ltp": 60.0}),
            "25000.0_CE": json.dumps({"ltp": 75.0}),
            "25100.0_CE": json.dumps({"ltp": 54.0}),
        }
        redis = self._redis(agg={"last_updated": str(now.timestamp())}, live=live)
        result = evaluate_position(position, redis, now=now)
        assert result is not None
        assert result.status == "OPEN"

    def test_returns_closed_position_when_exit_fires(self):
        position = _position(entry_credit=2900.0, expiry="2026-08-04")
        now = datetime(2026, 7, 19, 10, 0)
        # crash all short-leg premiums up hard -> stop loss
        live = {
            "24000.0_PE": json.dumps({"ltp": 500.0}),
            "23900.0_PE": json.dumps({"ltp": 400.0}),
            "25000.0_CE": json.dumps({"ltp": 500.0}),
            "25100.0_CE": json.dumps({"ltp": 400.0}),
        }
        redis = self._redis(agg={"last_updated": str(now.timestamp())}, live=live)
        result = evaluate_position(position, redis, now=now)
        assert result is not None
        assert result.status == "CLOSED"
        assert result.exit_reason == "STOP_LOSS"
