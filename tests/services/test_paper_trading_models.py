"""Tests for services/paper_trading/models.py."""

from datetime import date

from services.paper_trading.models import (
    OptionLeg,
    PaperAccount,
    PaperPosition,
    apply_slippage,
    compute_brokerage,
    compute_exchange_charges,
    compute_stamp_duty,
    compute_stt,
    compute_strike_gap,
    cooldown_key,
    daily_pnl_key,
    positions_closed_key,
    select_expiry,
)


class TestCostModel:
    def test_sell_slippage_below_ltp(self):
        assert apply_slippage(100.0, "SELL", slippage_bps=50) == 99.5

    def test_buy_slippage_above_ltp(self):
        assert round(apply_slippage(100.0, "BUY", slippage_bps=50), 4) == 100.5

    def test_stt_only_on_sell_premium(self):
        assert round(compute_stt(84.575, 65, stt_pct=0.001), 2) == round(0.001 * 84.575 * 65, 2)

    def test_exchange_charges(self):
        assert round(compute_exchange_charges(84.575, 65, pct=0.0005), 2) == round(0.0005 * 84.575 * 65, 2)

    def test_stamp_duty(self):
        assert round(compute_stamp_duty(84.575, 65, pct=0.00003), 4) == round(0.00003 * 84.575 * 65, 4)

    def test_brokerage_per_leg(self):
        assert compute_brokerage(4, per_order=20.0) == 80.0


class TestSelectExpiry:
    EXPIRIES = ["2026-07-28", "2026-07-21", "2026-08-04"]

    def test_intraday_picks_nearest(self):
        assert select_expiry(self.EXPIRIES, "intraday", date(2026, 7, 19)) == "2026-07-21"

    def test_positional_skips_expiry_with_less_than_3_dte(self):
        # 2026-07-19 -> 2026-07-21 is 2 days away, must skip to next
        assert select_expiry(self.EXPIRIES, "positional", date(2026, 7, 19)) == "2026-07-28"

    def test_positional_uses_current_if_dte_sufficient(self):
        # 2026-07-15 -> 2026-07-21 is 6 days away, DTE >= 3 satisfied
        assert select_expiry(self.EXPIRIES, "positional", date(2026, 7, 15)) == "2026-07-21"

    def test_empty_expiries_returns_empty_string(self):
        assert select_expiry([], "intraday", date(2026, 7, 19)) == ""

    def test_positional_falls_back_to_nearest_if_none_have_enough_dte(self):
        near_expiries = ["2026-07-20"]
        assert select_expiry(near_expiries, "positional", date(2026, 7, 19)) == "2026-07-20"


class TestComputeStrikeGap:
    def test_derives_min_gap_from_keys(self):
        keys = ["24000.0_CE", "24000.0_PE", "24050.0_CE", "24100.0_CE"]
        assert compute_strike_gap(keys) == 50.0

    def test_returns_default_when_insufficient_strikes(self):
        assert compute_strike_gap(["24000.0_CE"], default=50.0) == 50.0

    def test_returns_default_when_empty(self):
        assert compute_strike_gap([], default=50.0) == 50.0


class TestRedisKeyHelpers:
    def test_positions_closed_key(self):
        assert positions_closed_key("2026-07-19") == "paper:positions:closed:2026-07-19"

    def test_daily_pnl_key(self):
        assert daily_pnl_key("2026-07-19") == "paper:daily_pnl:2026-07-19"

    def test_cooldown_key(self):
        assert cooldown_key("NIFTY", "IRON_CONDOR") == "paper:cooldown:NIFTY:IRON_CONDOR"


class TestOptionLeg:
    def test_construction_defaults(self):
        leg = OptionLeg(strike=24000.0, option_type="PE", side="SELL", lots=1, entry_premium=84.18)
        assert leg.current_premium == 0.0
        assert leg.entry_timestamp == 0.0


class TestPaperPositionSerialization:
    def _sample_position(self) -> PaperPosition:
        legs = [
            OptionLeg(strike=24000.0, option_type="PE", side="SELL", lots=1,
                      entry_premium=84.18, current_premium=84.18, entry_timestamp=100.0),
            OptionLeg(strike=23900.0, option_type="PE", side="BUY", lots=1,
                      entry_premium=62.41, current_premium=62.41, entry_timestamp=100.0),
        ]
        return PaperPosition(
            position_id="abc-123",
            symbol="NIFTY",
            strategy="CREDIT_SPREAD",
            mode="intraday",
            direction="BULLISH",
            legs=legs,
            expiry="2026-07-21",
            scrip="NIFTY26721",
            lot_size=65,
            entry_timestamp=100.0,
            entry_credit=21.77,
            margin_blocked=37370.06,
            signal_source="SKEW_FADE_SETUP",
        )

    def test_round_trip_preserves_fields(self):
        position = self._sample_position()
        restored = PaperPosition.from_json(position.to_json())

        assert restored.position_id == position.position_id
        assert restored.symbol == "NIFTY"
        assert restored.status == "OPEN"
        assert len(restored.legs) == 2
        assert all(isinstance(leg, OptionLeg) for leg in restored.legs)
        assert restored.legs[0].strike == 24000.0
        assert restored.legs[1].side == "BUY"

    def test_closed_position_round_trip(self):
        position = self._sample_position()
        position.status = "CLOSED"
        position.exit_timestamp = 200.0
        position.exit_reason = "TARGET"
        position.pnl = 1372.0

        restored = PaperPosition.from_json(position.to_json())
        assert restored.status == "CLOSED"
        assert restored.exit_reason == "TARGET"
        assert restored.pnl == 1372.0


class TestPaperAccountSerialization:
    def test_defaults(self):
        account = PaperAccount()
        assert account.capital == 1_000_000.0
        assert account.available_margin == 1_000_000.0
        assert account.open_positions == 0

    def test_round_trip_via_redis_mapping(self):
        account = PaperAccount(capital=1_000_000.0, realized_pnl=1372.0, margin_used=37370.06,
                                open_positions=2, daily_trades=3, daily_wins=2, daily_losses=1)
        restored = PaperAccount.from_redis_mapping(account.to_redis_mapping())

        assert restored.capital == 1_000_000.0
        assert restored.realized_pnl == 1372.0
        assert restored.margin_used == 37370.06
        assert restored.open_positions == 2
        assert restored.daily_trades == 3
        assert restored.daily_wins == 2
        assert restored.daily_losses == 1

    def test_empty_mapping_returns_defaults(self):
        account = PaperAccount.from_redis_mapping({})
        assert account.capital == 1_000_000.0
        assert account.day_start_capital == 1_000_000.0
