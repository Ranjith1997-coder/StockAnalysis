"""Tests for services/paper_trading/main.py.

Scope: the persistence, formatting, and command-handling helpers. The
infinite-loop thread functions (analysis_consumer, mtm_engine, etc.) are
integration-level and exercised by the design doc's dry-run validation
plan, not unit tests here.
"""

import json
from datetime import datetime, time as dtime
from unittest.mock import MagicMock, patch

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
    def test_writes_position_cooldown_and_account(self):
        redis = MagicMock()
        redis.hgetall.return_value = {}  # empty account -> defaults
        position = _position()

        with patch.object(pt_main.TELEGRAM_NOTIFICATIONS, "send_live_options_notification") as tg:
            pt_main.persist_new_position(redis, position)

        redis.hset.assert_any_call(pt_main.POSITIONS_OPEN_KEY, mapping={"pos-1": position.to_json()})
        redis.set_with_ttl.assert_called_once()
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

    @patch("zerodha.zerodha_connect.KiteConnect")
    def test_returns_none_on_fetch_exception(self, mock_kite_cls):
        redis = MagicMock()
        redis.hget.return_value = "some-enctoken"
        mock_kite_cls.return_value.instruments.side_effect = Exception("network error")
        assert pt_main.try_load_instruments(redis) is None

    @patch("zerodha.zerodha_connect.KiteConnect")
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


class TestHandleEntrySignal:
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

    def test_reset_clears_positions_and_account(self):
        redis = MagicMock()
        position = _position()
        redis.hgetall.return_value = {"pos-1": position.to_json()}
        pt_main._handle_command(redis, {"command": "reset"})
        redis.hdel.assert_called_once_with(pt_main.POSITIONS_OPEN_KEY, "pos-1")
        account_calls = [c for c in redis.hset.call_args_list if c.args[0] == ACCOUNT_KEY]
        assert len(account_calls) == 1

    def test_config_set(self):
        redis = MagicMock()
        pt_main._handle_command(redis, {"command": "config_set", "key": "max_positions", "value": "10"})
        redis.hset.assert_called_once_with(pt_main.CONFIG_KEY, mapping={"max_positions": "10"})

    def test_unknown_command_does_nothing(self):
        redis = MagicMock()
        pt_main._handle_command(redis, {"command": "bogus"})
        redis.hset.assert_not_called()
        redis.hdel.assert_not_called()
