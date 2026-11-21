"""Tests for services/paper_trading/engine.py."""

import json
from datetime import date, datetime
from unittest.mock import MagicMock

from services.paper_trading.engine import (
    SpotMoveTracker,
    check_gamma_trap_proxy,
    check_profit_target,
    check_stop_loss,
    check_theta_decay,
    check_time_squareoff,
    close_position,
    compute_current_debit,
    compute_dte,
    compute_exit_costs,
    compute_exit_fills,
    compute_exit_pnl,
    compute_max_loss,
    compute_qty,
    compute_unrealized_pnl,
    determine_exit_reason,
    evaluate_position,
    intrinsic_value,
    is_agg_data_stale,
    is_past_expiry,
    stop_loss_amount,
    update_leg_premiums,
)
from services.paper_trading.models import (
    DEFAULT_STAMP_DUTY_PCT,
    DEFAULT_STT_PCT,
    OptionLeg,
    PaperPosition,
    compute_transaction_costs,
)


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


def _put_spread():
    legs = [
        OptionLeg(strike=24000.0, option_type="PE", side="SELL", lots=1,
                  entry_premium=50.0, current_premium=50.0),
        OptionLeg(strike=23950.0, option_type="PE", side="BUY", lots=1,
                  entry_premium=35.0, current_premium=35.0),
    ]
    return PaperPosition(
        position_id="ps1", symbol="NIFTY", strategy="CREDIT_SPREAD", mode="intraday",
        direction="BULLISH", legs=legs, expiry="2026-08-04", scrip="X",
        lot_size=65, entry_timestamp=0.0, entry_credit=975.0, margin_blocked=20000.0,
    )


class TestSpotMoveTracker:
    def test_move_within_window(self):
        t = SpotMoveTracker(window_seconds=300)
        t.update("NIFTY", 24000.0, 0.0)
        t.update("NIFTY", 24300.0, 120.0)
        assert round(t.move_pct("NIFTY"), 2) == 1.25

    def test_old_points_drop_out_of_window(self):
        t = SpotMoveTracker(window_seconds=300)
        t.update("NIFTY", 24000.0, 0.0)       # 2% below, but > 5 min old
        t.update("NIFTY", 24400.0, 400.0)
        t.update("NIFTY", 24480.0, 500.0)
        assert t.move_pct("NIFTY") < 1.0

    def test_single_point_is_zero(self):
        t = SpotMoveTracker()
        t.update("NIFTY", 24000.0, 0.0)
        assert t.move_pct("NIFTY") == 0.0
        assert t.move_pct("BANKNIFTY") == 0.0


class TestGammaTrapProxy:
    def test_fast_selloff_closes_short_put_spread(self):
        assert check_gamma_trap_proxy(_put_spread(), move_pct=-1.2) is True

    def test_fast_rally_does_not_close_short_put_spread(self):
        # short leg is on the other side of the move -- not threatened
        assert check_gamma_trap_proxy(_put_spread(), move_pct=1.5) is False

    def test_slow_trend_day_move_below_threshold_is_ignored(self):
        assert check_gamma_trap_proxy(_put_spread(), move_pct=-0.8) is False

    def test_iron_condor_threatened_either_way(self):
        assert check_gamma_trap_proxy(_position(), move_pct=1.1) is True
        assert check_gamma_trap_proxy(_position(), move_pct=-1.1) is True


class TestExitRulePredicates:
    def test_stop_loss_triggers_at_200pct_loss(self):
        assert check_stop_loss(unrealized_pnl=-5800.0, entry_credit=2900.0) is True
        assert check_stop_loss(unrealized_pnl=-5799.0, entry_credit=2900.0) is False

    def test_profit_target_triggers_at_50pct(self):
        assert check_profit_target(unrealized_pnl=1450.0, entry_credit=2900.0) is True
        assert check_profit_target(unrealized_pnl=1449.0, entry_credit=2900.0) is False

    def test_defined_risk_stop_loss_uses_fraction_of_max_loss(self):
        # 50-wide put spread, 65 qty, credit 975 -> max loss 3250-975=2275.
        # 60% of max loss (1365) is tighter than 2x credit (1950), so it wins.
        position = _put_spread()
        max_loss = compute_max_loss(position)
        assert max_loss == 50 * 65 - 975.0
        assert stop_loss_amount(position.entry_credit, max_loss) == 0.6 * max_loss
        assert check_stop_loss(-0.6 * max_loss, position.entry_credit, max_loss) is True
        assert check_stop_loss(-0.6 * max_loss + 1, position.entry_credit, max_loss) is False

    def test_narrow_spread_stop_loss_reachable(self):
        # credit 900 on a 20-wide spread: 2x credit (1800) > max loss (400) -> unreachable before fix
        position = _put_spread()
        position.legs[1].strike = 23980.0
        position.entry_credit = 900.0
        max_loss = compute_max_loss(position)
        assert max_loss == 20 * 65 - 900.0
        assert stop_loss_amount(position.entry_credit, max_loss) < max_loss

    def test_naked_or_strangle_keeps_2x_credit(self):
        position = _put_spread()
        position.legs = position.legs[:1]   # unhedged short
        assert compute_max_loss(position) is None
        assert stop_loss_amount(975.0, None) == 1950.0

    def test_iron_condor_max_loss_uses_widest_wing(self):
        position = _position(entry_credit=2900.0)
        assert compute_max_loss(position) == 100 * 65 - 2900.0

    def test_theta_decay_on_expiry_day_needs_time_gate(self):
        # dte == 0 -- position's own expiry is today -- uses the tight 14:00 gate
        before_gate = datetime(2026, 7, 19, 13, 59)
        after_gate = datetime(2026, 7, 19, 14, 0)
        assert check_theta_decay(unrealized_pnl=1000.0, entry_credit=2900.0,
                                  dte=0, now=before_gate) is False
        assert check_theta_decay(unrealized_pnl=1000.0, entry_credit=2900.0,
                                  dte=0, now=after_gate) is True

    def test_theta_decay_not_enough_profit_never_fires(self):
        now = datetime(2026, 7, 19, 15, 0)
        assert check_theta_decay(unrealized_pnl=700.0, entry_credit=2900.0,
                                  dte=0, now=now) is False

    def test_theta_decay_multi_day_out_uses_dte_gate_not_clock_time(self):
        # dte > 0 -- no same-day clock gate, banked profit alone is enough once dte<=3
        now = datetime(2026, 7, 19, 10, 0)
        assert check_theta_decay(unrealized_pnl=1000.0, entry_credit=2900.0,
                                  dte=3, now=now) is True
        assert check_theta_decay(unrealized_pnl=1000.0, entry_credit=2900.0,
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

    def test_theta_decay_fires_below_target_near_expiry(self):
        # 34% of credit banked, 2 days to expiry: TARGET (50%) not hit, THETA_DECAY fires
        position = _position(entry_credit=2900.0, expiry="2026-07-21")
        reason = determine_exit_reason(position, unrealized_pnl=1000.0, current_debit=1900.0,
                                        now=datetime(2026, 7, 19, 10, 0))
        assert reason == "THETA_DECAY"

    def test_target_still_wins_over_theta_decay(self):
        position = _position(entry_credit=2900.0, expiry="2026-07-21")
        reason = determine_exit_reason(position, unrealized_pnl=1500.0, current_debit=1400.0,
                                        now=datetime(2026, 7, 19, 10, 0))
        assert reason == "TARGET"

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
    def test_exit_stt_on_longs_sold_not_on_shorts_bought_back(self):
        position = _put_spread()
        qty = compute_qty(position)
        fills = [50.0, 35.0]   # short PE bought back @50, long PE sold @35
        costs = compute_exit_costs(position, qty, fills)
        expected = compute_transaction_costs([(50.0, "BUY"), (35.0, "SELL")], qty,
                                             exchange_charges_pct=position.exchange_charges_pct)
        assert costs == expected
        # STT only on the 35 premium being sold; stamp only on the 50 being bought
        stt_part = DEFAULT_STT_PCT * 35.0 * qty
        stamp_part = DEFAULT_STAMP_DUTY_PCT * 50.0 * qty
        no_statutory = compute_transaction_costs([(50.0, "BUY"), (35.0, "SELL")], qty,
                                                 stt_pct=0.0, stamp_duty_pct=0.0,
                                                 exchange_charges_pct=position.exchange_charges_pct)
        assert abs((costs - no_statutory) - (stt_part + stamp_part)) < 1e-6

    def test_gst_and_sebi_included(self):
        qty = 65
        base = compute_transaction_costs([(100.0, "SELL")], qty, stt_pct=0.0,
                                         exchange_charges_pct=0.0, stamp_duty_pct=0.0)
        # brokerage 20 + sebi(6500*10/1e7) with 18% GST on both
        assert round(base, 6) == round((20.0 + 6500 * 10 / 1e7) * 1.18, 6)

    def test_exit_fills_apply_slippage_against_us(self):
        position = _put_spread()
        fills = compute_exit_fills(position)
        assert fills[0] > 50.0    # buying back short above LTP
        assert fills[1] < 35.0    # selling long below LTP

    def test_exit_fills_prefer_depth_touch_prices(self):
        position = _put_spread()
        live = {
            "24000.0_PE": json.dumps({"ltp": 50.0, "depth": {"buy": [{"price": 49.5}], "sell": [{"price": 50.7}]}}),
            "23950.0_PE": json.dumps({"ltp": 35.0, "depth": {"buy": [{"price": 34.6}], "sell": [{"price": 35.4}]}}),
        }
        assert compute_exit_fills(position, live) == [50.7, 34.6]

    def test_exit_fills_prefer_top_level_bid_ask(self):
        position = _put_spread()
        live = {
            "24000.0_PE": json.dumps({"ltp": 50.0, "bid": 49.8, "ask": 50.3}),
            "23950.0_PE": json.dumps({"ltp": 35.0, "bid": 34.8, "ask": 35.2}),
        }
        assert compute_exit_fills(position, live) == [50.3, 34.8]

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
        mark = compute_current_debit(position)
        closed = close_position(position, "TARGET", now=12345.0)
        assert closed.status == "CLOSED"
        assert closed.exit_reason == "TARGET"
        assert closed.exit_timestamp == 12345.0
        assert closed.exit_premium > mark   # exit slippage widens the debit
        assert closed.pnl is not None


class TestExpirySettlement:
    def test_past_expiry_detection(self):
        position = _position(expiry="2026-07-21")
        assert is_past_expiry(position, datetime(2026, 7, 22, 10, 0)) is True
        assert is_past_expiry(position, datetime(2026, 7, 21, 15, 30)) is True
        assert is_past_expiry(position, datetime(2026, 7, 21, 15, 29)) is False
        assert is_past_expiry(position, datetime(2026, 7, 20, 16, 0)) is False

    def test_intrinsic_value(self):
        assert intrinsic_value(24000.0, "PE", 23900.0) == 100.0
        assert intrinsic_value(24000.0, "PE", 24100.0) == 0.0
        assert intrinsic_value(25000.0, "CE", 25050.0) == 50.0

    def test_stuck_expired_position_settled_at_intrinsic(self):
        position = _position(entry_credit=2900.0, expiry="2026-07-21")
        redis = MagicMock()
        # spot 23950: short 24000PE ITM by 50, long 23900PE OTM, CE side worthless
        redis.hgetall.side_effect = lambda key: {"last_price": "23950"} if key == "data:tick:NIFTY" else {}
        result = evaluate_position(position, redis, now=datetime(2026, 7, 23, 10, 0))
        assert result.status == "CLOSED"
        assert result.exit_reason == "EXPIRY_SETTLEMENT"
        assert result.exit_premium == 50.0 * 65
        assert result.pnl == 2900.0 - 50.0 * 65

    def test_expired_without_spot_is_skipped(self):
        position = _position(expiry="2026-07-21")
        redis = MagicMock()
        redis.hgetall.return_value = {}
        assert evaluate_position(position, redis, now=datetime(2026, 7, 23, 10, 0)) is None


class TestEvaluatePosition:
    def _redis(self, agg, live):
        redis = MagicMock()
        redis.hgetall.side_effect = lambda key: agg if "options_agg" in key else live
        return redis

    def test_skips_when_agg_stale(self):
        position = _position(expiry="2026-08-04")
        redis = self._redis(agg={}, live={})
        assert evaluate_position(position, redis, now=datetime(2026, 7, 19, 10, 0)) is None

    def test_skips_when_options_live_empty(self):
        position = _position(expiry="2026-08-04")
        now = datetime(2026, 7, 19, 10, 0)
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
