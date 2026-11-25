"""Tests for services/paper_trading/main.py.

Scope: the persistence, formatting, and command-handling helpers. The
infinite-loop thread functions (analysis_consumer, mtm_engine, etc.) are
integration-level and exercised by the design doc's dry-run validation
plan, not unit tests here.
"""

import time
from datetime import date, datetime, time as dtime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from services.paper_trading import engine
from services.paper_trading import main as pt_main
from services.paper_trading.models import ACCOUNT_KEY, OptionLeg, PaperAccount, PaperPosition
from services.paper_trading.signal_router import EntrySignal, ExitSignal


def _position(symbol="NIFTY", strategy="IRON_CONDOR", entry_credit=2809.0, margin=68578.86):
    legs = [
        OptionLeg(strike=24000.0, option_type="PE", side="SELL", lots=1,
                  entry_premium=84.18, current_premium=84.18),
        OptionLeg(strike=23900.0, option_type="PE", side="BUY", lots=1,
                  entry_premium=62.41, current_premium=62.41),
    ]
    return PaperPosition(
        position_id="pos-1", symbol=symbol, strategy=strategy, mode="intraday",
        direction="NEUTRAL", legs=legs, expiry="2026-07-21", scrip="NIFTY26721",
        lot_size=65, entry_timestamp=0.0, entry_credit=entry_credit, margin_blocked=margin,
        signal_source="RANGE_BOUND_SETUP",
    )


class TestIsMarketHours:
    def test_within_hours(self):
        assert pt_main.is_market_hours(datetime(2026, 7, 19, 10, 0)) is True

    def test_before_open(self):
        assert pt_main.is_market_hours(datetime(2026, 7, 19, 9, 0)) is False

    def test_after_close(self):
        assert pt_main.is_market_hours(datetime(2026, 7, 19, 15, 31)) is False

    def test_exact_boundaries(self):
        assert pt_main.is_market_hours(datetime(2026, 7, 19, 9, 15)) is True
        assert pt_main.is_market_hours(datetime(2026, 7, 19, 15, 30)) is True


class TestAccountPersistence:
    def test_get_account_defaults_when_empty(self):
        redis = MagicMock()
        redis.hgetall.return_value = {}
        account = pt_main.get_account(redis)
        assert account.capital == 1_000_000.0

    def test_daily_counters_reset_on_new_trading_day(self):
        redis = MagicMock()
        stale = PaperAccount(daily_realized_pnl=-40_000.0, daily_trades=7, daily_wins=2,
                             daily_losses=5, realized_pnl=-40_000.0, trading_day="2026-07-17")
        redis.hgetall.return_value = stale.to_redis_mapping()
        account = pt_main.get_account(redis, today="2026-07-20")
        assert account.trading_day == "2026-07-20"
        assert account.daily_realized_pnl == 0.0
        assert account.daily_trades == 0 and account.daily_losses == 0
        assert account.realized_pnl == -40_000.0          # lifetime P&L untouched
        assert account.day_start_capital == 960_000.0

    def test_daily_counters_kept_within_same_day(self):
        redis = MagicMock()
        same = PaperAccount(daily_realized_pnl=-10_000.0, daily_trades=3, trading_day="2026-07-20")
        redis.hgetall.return_value = same.to_redis_mapping()
        account = pt_main.get_account(redis, today="2026-07-20")
        assert account.daily_realized_pnl == -10_000.0
        assert account.daily_trades == 3

    def test_save_account_recomputes_available_margin(self):
        redis = MagicMock()
        account = PaperAccount(capital=1_000_000.0, margin_used=200_000.0)
        pt_main.save_account(redis, account)
        _, kwargs = redis.hset.call_args
        mapping = kwargs["mapping"]
        assert float(mapping["available_margin"]) == 800_000.0


class TestLoadOpenPositions:
    def test_parses_all_valid_positions(self):
        redis = MagicMock()
        position = _position()
        redis.hgetall.return_value = {"pos-1": position.to_json()}
        positions = pt_main.load_open_positions(redis)
        assert len(positions) == 1
        assert positions[0].symbol == "NIFTY"

    def test_skips_malformed_entries(self):
        redis = MagicMock()
        redis.hgetall.return_value = {"bad": "{not json"}
        assert pt_main.load_open_positions(redis) == []


class TestPersistNewPosition:
    def test_writes_position_and_account_no_cooldown_at_entry(self):
        redis = MagicMock()
        redis.hgetall.return_value = {}  # empty account -> defaults
        position = _position()

        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification") as tg:
            pt_main.persist_new_position(redis, position)

        redis.hset.assert_any_call(pt_main.POSITIONS_OPEN_KEY, mapping={"pos-1": position.to_json()})
        redis.set_with_ttl.assert_not_called()   # cooldown is set on exit
        tg.assert_called_once()

        # account update call: find the ACCOUNT_KEY hset
        account_calls = [c for c in redis.hset.call_args_list if c.args[0] == ACCOUNT_KEY]
        assert len(account_calls) == 1
        mapping = account_calls[0].kwargs["mapping"]
        assert float(mapping["margin_used"]) == 68578.86
        assert int(mapping["open_positions"]) == 1


class TestPersistClosedPosition:
    def test_writes_closed_position_trade_and_account(self):
        redis = MagicMock()
        redis.hgetall.return_value = {}
        position = _position()
        position.status = "CLOSED"
        position.exit_timestamp = 100.0
        position.exit_premium = 1450.0
        position.pnl = 1200.0
        position.exit_reason = "TARGET"

        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification") as tg:
            pt_main.persist_closed_position(redis, position)

        redis.hdel.assert_called_once_with(pt_main.POSITIONS_OPEN_KEY, "pos-1")
        redis.xadd.assert_called_once()
        tg.assert_called_once()

        account_calls = [c for c in redis.hset.call_args_list if c.args[0] == ACCOUNT_KEY]
        mapping = account_calls[0].kwargs["mapping"]
        assert float(mapping["realized_pnl"]) == 1200.0
        assert int(mapping["daily_wins"]) == 1
        assert int(mapping["daily_losses"]) == 0

    def test_losing_trade_increments_daily_losses(self):
        redis = MagicMock()
        redis.hgetall.return_value = {}
        position = _position()
        position.status = "CLOSED"
        position.exit_timestamp = 100.0
        position.exit_premium = 5000.0
        position.pnl = -1500.0
        position.exit_reason = "STOP_LOSS"

        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification"):
            pt_main.persist_closed_position(redis, position)

        account_calls = [c for c in redis.hset.call_args_list if c.args[0] == ACCOUNT_KEY]
        mapping = account_calls[0].kwargs["mapping"]
        assert int(mapping["daily_losses"]) == 1
        assert float(mapping["realized_pnl"]) == -1500.0
        # stop-loss exit sets the longer cooldown
        redis.set_with_ttl.assert_called_once_with(
            "paper:cooldown:NIFTY:IRON_CONDOR", "1", ex=pt_main.STOP_LOSS_COOLDOWN_TTL_SECONDS)

    def test_target_exit_sets_normal_cooldown(self):
        redis = MagicMock()
        redis.hgetall.return_value = {}
        position = _position()
        position.pnl = 100.0
        position.exit_reason = "TARGET"
        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification"), \
                patch.object(pt_main.ledger, "insert_trade"):
            pt_main.persist_closed_position(redis, position)
        redis.set_with_ttl.assert_called_once_with(
            "paper:cooldown:NIFTY:IRON_CONDOR", "1", ex=pt_main.COOLDOWN_TTL_SECONDS)

    def test_already_closed_position_is_not_double_booked(self):
        redis = MagicMock()
        redis.hdel.return_value = 0   # another thread already removed it
        position = _position()
        position.pnl = 100.0
        position.exit_reason = "TARGET"
        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification") as tg, \
                patch.object(pt_main.ledger, "insert_trade") as ins:
            assert pt_main.persist_closed_position(redis, position) is False
        redis.xadd.assert_not_called()
        ins.assert_not_called()
        tg.assert_not_called()


class TestApplyMtmResult:
    def test_open_position_written_back_when_still_open(self):
        redis = MagicMock()
        redis.hget.return_value = "{}"
        position = _position()
        assert pt_main.apply_mtm_result(redis, position) is True
        redis.hset.assert_called_once_with(pt_main.POSITIONS_OPEN_KEY, mapping={"pos-1": position.to_json()})

    def test_position_closed_by_other_thread_not_resurrected(self):
        redis = MagicMock()
        redis.hget.return_value = None
        assert pt_main.apply_mtm_result(redis, _position()) is False
        redis.hset.assert_not_called()


class TestSettleExpiredPositions:
    def test_expired_position_settled_outside_market_hours(self):
        redis = MagicMock()
        position = _position()   # expiry 2026-07-21
        redis.hgetall.side_effect = lambda key: (
            {"pos-1": position.to_json()} if key == pt_main.POSITIONS_OPEN_KEY
            else {"last_price": "24500"} if key == "data:tick:NIFTY"
            else {}
        )
        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification"), \
                patch.object(pt_main.ledger, "insert_trade"):
            pt_main.settle_expired_positions(redis, datetime(2026, 7, 22, 20, 0))
        redis.hdel.assert_called_once_with(pt_main.POSITIONS_OPEN_KEY, "pos-1")
        stream_fields = redis.xadd.call_args.args[1]
        assert stream_fields["exit_reason"] == "EXPIRY_SETTLEMENT"


class TestNotificationFormatting:
    def test_entry_notification_contains_key_fields(self):
        position = _position()
        msg = pt_main.format_entry_notification(position)
        assert "NIFTY" in msg
        assert "IRON_CONDOR" in msg
        assert "OPENED" in msg

    def test_exit_notification_contains_pnl(self):
        position = _position()
        position.pnl = 1200.0
        position.exit_premium = 1450.0
        position.exit_reason = "TARGET"
        msg = pt_main.format_exit_notification(position)
        assert "CLOSED" in msg
        assert "TARGET" in msg


class TestTryLoadInstruments:
    def test_returns_none_without_enctoken(self):
        redis = MagicMock()
        redis.hget.return_value = None
        assert pt_main.try_load_instruments(redis) is None

    @patch("lib.zerodha.zerodha_connect.KiteConnect")
    def test_returns_none_on_fetch_exception(self, mock_kite_cls):
        redis = MagicMock()
        redis.hget.return_value = "some-enctoken"
        mock_kite_cls.return_value.instruments.side_effect = Exception("network error")
        assert pt_main.try_load_instruments(redis) is None

    @patch("lib.zerodha.zerodha_connect.KiteConnect")
    def test_builds_and_saves_cache_on_success(self, mock_kite_cls):
        redis = MagicMock()
        redis.hget.return_value = "some-enctoken"
        mock_kite_cls.return_value.instruments.return_value = [{
            "name": "NIFTY", "strike": 24000.0, "instrument_type": "PE",
            "tradingsymbol": "NIFTY2672124000PE", "expiry": "2026-07-21",
            "lot_size": 65, "exchange": "NFO",
        }]
        cache = pt_main.try_load_instruments(redis)
        assert "NIFTY" in cache
        redis.hset.assert_called()


class TestHandleExitSignal:
    def _redis(self, positions):
        redis = MagicMock()
        redis.hgetall.side_effect = lambda key: (
            {p.position_id: p.to_json() for p in positions} if key == pt_main.POSITIONS_OPEN_KEY
            else {}
        )
        return redis

    def test_directional_gamma_trap_only_closes_threatened_side(self):
        put_spread = _position()                      # short PE only
        call_spread = _position(strategy="CREDIT_SPREAD")
        call_spread.position_id = "pos-2"
        call_spread.legs = [
            OptionLeg(strike=25000.0, option_type="CE", side="SELL", lots=1, entry_premium=50, current_premium=50),
            OptionLeg(strike=25100.0, option_type="CE", side="BUY", lots=1, entry_premium=30, current_premium=30),
        ]
        redis = self._redis([put_spread, call_spread])
        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification"), \
                patch.object(pt_main.ledger, "insert_trade"):
            pt_main._handle_exit_signal(redis, ExitSignal(symbol="NIFTY", reason="GAMMA_TRAP",
                                                          direction="BULLISH"))
        redis.hdel.assert_called_once_with(pt_main.POSITIONS_OPEN_KEY, "pos-2")

    def test_closes_matching_position(self):
        redis = MagicMock()
        position = _position()
        redis.hgetall.side_effect = lambda key: (
            {"pos-1": position.to_json()} if key == pt_main.POSITIONS_OPEN_KEY
            else {"ltp": "500.0"} if "options_live" in key
            else {}
        )
        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification"):
            pt_main._handle_exit_signal(redis, ExitSignal(symbol="NIFTY", reason="GAMMA_TRAP"))
        redis.hdel.assert_called_once_with(pt_main.POSITIONS_OPEN_KEY, "pos-1")

    def test_ignores_other_symbols(self):
        redis = MagicMock()
        position = _position(symbol="BANKNIFTY")
        redis.hgetall.return_value = {"pos-1": position.to_json()}
        pt_main._handle_exit_signal(redis, ExitSignal(symbol="NIFTY", reason="GAMMA_TRAP"))
        redis.hdel.assert_not_called()


class TestCheckEntryGate:
    def _redis(self, last_updated, sensibull_age=0):
        redis = MagicMock()
        redis.hgetall.return_value = {"last_updated": str(last_updated)}
        redis.hget.side_effect = lambda key, field: (
            (datetime.fromtimestamp(last_updated) - timedelta(seconds=sensibull_age)).isoformat()
            if key.startswith("data:sensibull:") else None
        )
        return redis

    @patch("services.paper_trading.main.is_trading_day", return_value=True)
    def test_allows_fresh_data_in_window(self, _td):
        now = datetime(2026, 7, 20, 10, 0)
        assert pt_main.check_entry_gate(self._redis(now.timestamp() - 2), "NIFTY", now) == ""

    @patch("services.paper_trading.main.is_trading_day", return_value=True)
    def test_rejects_outside_window(self, _td):
        for t in (dtime(9, 19), dtime(15, 1), dtime(20, 0)):
            now = datetime.combine(date(2026, 7, 20), t)
            assert pt_main.check_entry_gate(self._redis(now.timestamp()), "NIFTY", now) == "outside_entry_window"

    @patch("services.paper_trading.main.is_trading_day", return_value=True)
    def test_rejects_stale_sensibull_context(self, _td):
        now = datetime(2026, 7, 20, 10, 0)
        redis = self._redis(now.timestamp(), sensibull_age=5 * 3600)
        assert pt_main.check_entry_gate(redis, "NIFTY", now) == "stale_sensibull"

    @patch("services.paper_trading.main.is_trading_day", return_value=False)
    def test_rejects_non_trading_day(self, _td):
        now = datetime(2026, 7, 20, 10, 0)
        assert pt_main.check_entry_gate(self._redis(now.timestamp()), "NIFTY", now) == "not_trading_day"

    @patch("services.paper_trading.main.is_trading_day", return_value=True)
    def test_rejects_stale_options_agg(self, _td):
        now = datetime(2026, 7, 20, 10, 0)
        assert pt_main.check_entry_gate(self._redis(now.timestamp() - 30), "NIFTY", now) == "stale_options_agg"

    @patch("services.paper_trading.main.build_position")
    def test_gate_rejection_skips_everything(self, mock_build):
        with patch.object(pt_main, "check_entry_gate", return_value="outside_entry_window"):
            pt_main._handle_entry_signal(MagicMock(), MagicMock(), EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY"))
        mock_build.assert_not_called()


class TestHandleEntrySignal:
    @pytest.fixture(autouse=True)
    def _open_gate(self):
        with patch.object(pt_main, "check_entry_gate", return_value=""):
            yield

    @patch("services.paper_trading.main.persist_new_position")
    @patch("services.paper_trading.main.build_position")
    @patch("services.paper_trading.main.check_entry_filters")
    def test_new_position_margin_counted_against_cap(self, mock_filters, mock_build, mock_persist):
        mock_filters.return_value = (True, "")
        mock_build.return_value = _position(margin=150_000.0)
        redis = MagicMock()
        account = PaperAccount(capital=1_000_000.0, margin_used=300_000.0,
                               trading_day=date.today().isoformat())
        redis.hgetall.side_effect = lambda key: account.to_redis_mapping() if key == ACCOUNT_KEY else {}
        signal = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY")
        pt_main._handle_entry_signal(redis, MagicMock(), signal)
        mock_persist.assert_not_called()   # 300k + 150k > 40% of 10L

    @patch("services.paper_trading.main.build_position")
    @patch("services.paper_trading.main.check_entry_filters")
    def test_rejected_by_filters_does_not_build(self, mock_filters, mock_build):
        mock_filters.return_value = (False, "cooldown_active")
        redis = MagicMock()
        redis.hgetall.return_value = {}
        span = MagicMock()
        signal = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY")

        pt_main._handle_entry_signal(redis, span, signal)
        mock_build.assert_not_called()

    @patch("services.paper_trading.main.persist_new_position")
    @patch("services.paper_trading.main.build_position")
    @patch("services.paper_trading.main.check_entry_filters")
    def test_passed_filters_but_no_position_built(self, mock_filters, mock_build, mock_persist):
        mock_filters.return_value = (True, "")
        mock_build.return_value = None
        redis = MagicMock()
        redis.hgetall.return_value = {}
        span = MagicMock()
        signal = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY")

        pt_main._handle_entry_signal(redis, span, signal)
        mock_persist.assert_not_called()

    @patch("services.paper_trading.main.persist_new_position")
    @patch("services.paper_trading.main.build_position")
    @patch("services.paper_trading.main.check_entry_filters")
    def test_passed_filters_and_position_built_gets_persisted(self, mock_filters, mock_build, mock_persist):
        mock_filters.return_value = (True, "")
        position = _position()
        mock_build.return_value = position
        redis = MagicMock()
        redis.hgetall.return_value = {}
        span = MagicMock()
        signal = EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY")

        pt_main._handle_entry_signal(redis, span, signal)
        mock_persist.assert_called_once_with(redis, position)

    @patch("services.paper_trading.main.persist_new_position")
    @patch("services.paper_trading.main.build_position")
    @patch("services.paper_trading.main.check_entry_filters")
    def test_signal_mode_passed_through_to_build_position(self, mock_filters, mock_build, mock_persist):
        # signal.mode comes from analysis:results' echoed job mode (or defaults
        # to "intraday" for CONFLUENCE signals) -- must reach build_position,
        # not be silently overridden.
        mock_filters.return_value = (True, "")
        mock_build.return_value = _position()
        redis = MagicMock()
        redis.hgetall.return_value = {}
        span = MagicMock()
        signal = EntrySignal(strategy="STRANGLE", symbol="NIFTY", mode="positional")

        pt_main._handle_entry_signal(redis, span, signal)

        _, kwargs = mock_build.call_args
        assert kwargs["mode"] == "positional"

    @patch("services.paper_trading.main.persist_new_position")
    @patch("services.paper_trading.main.build_position")
    @patch("services.paper_trading.main.check_entry_filters")
    def test_low_pinn_confirmation_suppresses_non_pinn_signal(self, mock_filters, mock_build, mock_persist):
        mock_filters.return_value = (True, "")
        redis = MagicMock()
        redis.hgetall.return_value = {"last_updated": str(time.time()), "zscore_24000.0_PE": "0.2"}
        span = MagicMock()
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BULLISH",
                              sr_level=24000.0, signal_source="SKEW_FADE_SETUP")

        pt_main._handle_entry_signal(redis, span, signal)

        mock_build.assert_not_called()
        mock_persist.assert_not_called()

    @patch("services.paper_trading.main.persist_new_position")
    @patch("services.paper_trading.main.build_position")
    @patch("services.paper_trading.main.check_entry_filters")
    def test_high_pinn_confirmation_boosts_and_still_builds(self, mock_filters, mock_build, mock_persist):
        mock_filters.return_value = (True, "")
        mock_build.return_value = _position()
        redis = MagicMock()
        redis.hgetall.return_value = {"last_updated": str(time.time()), "zscore_24000.0_PE": "1.5"}
        span = MagicMock()
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BULLISH",
                              sr_level=24000.0, signal_source="SKEW_FADE_SETUP")

        pt_main._handle_entry_signal(redis, span, signal)

        mock_build.assert_called_once()
        mock_persist.assert_called_once()
        assert signal.signal_context["pinn_confirmed"] is True
        assert signal.signal_context["pinn_z"] == 1.5

    @patch("services.paper_trading.main.get_pinn_confirmation")
    @patch("services.paper_trading.main.persist_new_position")
    @patch("services.paper_trading.main.build_position")
    @patch("services.paper_trading.main.check_entry_filters")
    def test_pinn_sourced_signal_skips_self_confirmation(self, mock_filters, mock_build, mock_persist,
                                                          mock_confirm):
        """A PINN_MISPRICING signal never checks its own confirmation cache
        -- that would be circular (see main.py's _handle_entry_signal)."""
        mock_filters.return_value = (True, "")
        mock_build.return_value = _position()
        redis = MagicMock()
        span = MagicMock()
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BULLISH",
                              sr_level=24000.0, signal_source="PINN_MISPRICING")

        pt_main._handle_entry_signal(redis, span, signal)

        mock_confirm.assert_not_called()
        mock_persist.assert_called_once()

    @patch("services.paper_trading.main.persist_new_position")
    @patch("services.paper_trading.main.build_position")
    @patch("services.paper_trading.main.check_entry_filters")
    def test_missing_pinn_cache_fails_open(self, mock_filters, mock_build, mock_persist):
        mock_filters.return_value = (True, "")
        mock_build.return_value = _position()
        redis = MagicMock()
        redis.hgetall.return_value = {}
        span = MagicMock()
        signal = EntrySignal(strategy="CREDIT_SPREAD", symbol="NIFTY", direction="BULLISH",
                              sr_level=24000.0, signal_source="SKEW_FADE_SETUP")

        pt_main._handle_entry_signal(redis, span, signal)

        mock_persist.assert_called_once()


class TestHandleCommand:
    def test_close_all(self):
        redis = MagicMock()
        position = _position()
        redis.hgetall.side_effect = lambda key: (
            {"pos-1": position.to_json()} if key == pt_main.POSITIONS_OPEN_KEY
            else {"ltp": "500.0"} if "options_live" in key
            else {}
        )
        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification"):
            pt_main._handle_command(redis, {"command": "close", "position_id": "all"})
        redis.hdel.assert_called_once_with(pt_main.POSITIONS_OPEN_KEY, "pos-1")

    def test_close_specific_position_id(self):
        redis = MagicMock()
        pos1 = _position()
        pos2 = _position()
        pos2.position_id = "pos-2"
        redis.hgetall.side_effect = lambda key: (
            {"pos-1": pos1.to_json(), "pos-2": pos2.to_json()} if key == pt_main.POSITIONS_OPEN_KEY
            else {"ltp": "500.0"} if "options_live" in key
            else {}
        )
        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification"):
            pt_main._handle_command(redis, {"command": "close", "position_id": "pos-2"})
        redis.hdel.assert_called_once_with(pt_main.POSITIONS_OPEN_KEY, "pos-2")

    def test_reset_books_positions_then_resets_account(self):
        redis = MagicMock()
        position = _position()
        redis.hgetall.side_effect = lambda key: (
            {"pos-1": position.to_json()} if key == pt_main.POSITIONS_OPEN_KEY else {}
        )
        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification"), \
                patch.object(pt_main.ledger, "insert_trade") as ins:
            pt_main._handle_command(redis, {"command": "reset"})
        redis.hdel.assert_called_once_with(pt_main.POSITIONS_OPEN_KEY, "pos-1")
        ins.assert_called_once()
        assert ins.call_args.args[0].exit_reason == "RESET"
        account_calls = [c for c in redis.hset.call_args_list if c.args[0] == ACCOUNT_KEY]
        final = account_calls[-1].kwargs["mapping"]
        assert float(final["realized_pnl"]) == 0.0
        assert int(final["open_positions"]) == 0

    def test_config_set(self):
        redis = MagicMock()
        try:
            pt_main._handle_command(redis, {"command": "config_set", "key": "max_positions", "value": "10"})
            redis.hset.assert_called_once_with(pt_main.CONFIG_KEY, mapping={"max_positions": "10"})
            assert pt_main._runtime_config["max_positions"] == 10
        finally:
            pt_main._runtime_config.clear()

    def test_config_set_rejects_unknown_or_invalid(self):
        redis = MagicMock()
        pt_main._handle_command(redis, {"command": "config_set", "key": "bogus", "value": "1"})
        pt_main._handle_command(redis, {"command": "config_set", "key": "max_positions", "value": "500"})
        redis.hset.assert_not_called()
        assert "max_positions" not in pt_main._runtime_config

    def test_load_config_applies_exit_params(self):
        redis = MagicMock()
        redis.hgetall.return_value = {"stop_loss_multiple": "1.5", "profit_target_pct": "0.4", "junk": "x"}
        try:
            cfg = pt_main.load_config(redis)
            assert cfg == {"stop_loss_multiple": 1.5, "profit_target_pct": 0.4}
            assert engine.STOP_LOSS_MULTIPLE == 1.5
            assert engine.PROFIT_TARGET_PCT == 0.4
        finally:
            engine.set_exit_params(stop_loss_multiple=2.0, profit_target_pct=0.5)
            pt_main._runtime_config.clear()

    @patch("services.paper_trading.main.persist_new_position")
    @patch("services.paper_trading.main.build_position")
    @patch("services.paper_trading.main.check_entry_filters")
    def test_config_overrides_reach_entry_path(self, mock_filters, mock_build, mock_persist):
        mock_filters.return_value = (True, "")
        mock_build.return_value = None
        redis = MagicMock()
        redis.hgetall.return_value = {}
        pt_main._runtime_config.update({"max_positions": 3, "risk_scale": 0.5})
        try:
            with patch.object(pt_main, "check_entry_gate", return_value=""):
                pt_main._handle_entry_signal(redis, MagicMock(), EntrySignal(strategy="IRON_CONDOR", symbol="NIFTY"))
        finally:
            pt_main._runtime_config.clear()
        assert mock_filters.call_args.kwargs["max_positions"] == 3
        assert mock_build.call_args.kwargs["risk_scale"] == 0.5

    def test_unknown_command_does_nothing(self):
        redis = MagicMock()
        pt_main._handle_command(redis, {"command": "bogus"})
        redis.hset.assert_not_called()
        redis.hdel.assert_not_called()
