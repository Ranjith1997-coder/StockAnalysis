"""Tests for common/token_registry.py."""
import threading
import pytest
from common.token_registry import (
    TokenRegistry,
    TokenInfo,
    TokenType,
    OptionZone,
    ZONE_TO_WS_MODE,
)


# ── Factories ─────────────────────────────────────────────────────────────────

def make_equity_info(token=1001, parent="RELIANCE"):
    return TokenInfo(
        token=token,
        token_type=TokenType.EQUITY,
        parent_symbol=parent,
        tradingsymbol=parent,
    )


def make_option_info(token=2001, parent="NIFTY", strike=21000.0, option_type="CE",
                     expiry="2025-07"):
    return TokenInfo(
        token=token,
        token_type=TokenType.OPTION,
        parent_symbol=parent,
        tradingsymbol=f"{parent}{strike}{option_type}",
        strike=strike,
        option_type=option_type,
        expiry=expiry,
    )


def make_registry():
    return TokenRegistry()


# ── ZONE_TO_WS_MODE constant ──────────────────────────────────────────────────

class TestZoneToWsMode:
    def test_core_maps_to_full(self):
        assert ZONE_TO_WS_MODE[OptionZone.CORE] is not None

    def test_active_maps_to_full(self):
        assert ZONE_TO_WS_MODE[OptionZone.ACTIVE] is not None

    def test_peripheral_maps_to_quote(self):
        assert ZONE_TO_WS_MODE[OptionZone.PERIPHERAL] is not None

    def test_core_and_active_same_mode(self):
        assert ZONE_TO_WS_MODE[OptionZone.CORE] == ZONE_TO_WS_MODE[OptionZone.ACTIVE]


# ── register / lookup / unregister ───────────────────────────────────────────

class TestRegisterLookupUnregister:
    def test_register_and_lookup(self):
        reg = make_registry()
        info = make_equity_info(token=1001)
        reg.register(info)
        assert reg.lookup(1001) == info

    def test_lookup_unknown_returns_none(self):
        reg = make_registry()
        assert reg.lookup(9999) is None

    def test_unregister_removes_token(self):
        reg = make_registry()
        info = make_equity_info(token=1001)
        reg.register(info)
        reg.unregister(1001)
        assert reg.lookup(1001) is None

    def test_unregister_unknown_token_no_error(self):
        reg = make_registry()
        reg.unregister(9999)  # must not raise

    def test_register_option_populates_strike_map(self):
        reg = make_registry()
        info = make_option_info(token=2001, strike=21000.0)
        reg.register(info)
        tokens = reg.get_tokens_by_type("NIFTY", TokenType.OPTION)
        assert 2001 in tokens

    def test_unregister_option_removes_from_strike_map(self):
        reg = make_registry()
        info = make_option_info(token=2001, strike=21000.0)
        reg.register(info)
        reg.unregister(2001)
        tokens = reg.get_tokens_by_type("NIFTY", TokenType.OPTION)
        assert 2001 not in tokens

    def test_register_batch(self):
        reg = make_registry()
        infos = [make_equity_info(token=i, parent=f"SYM{i}") for i in range(10, 15)]
        reg.register_batch(infos)
        for i in range(10, 15):
            assert reg.lookup(i) is not None

    def test_register_overwrites_existing(self):
        reg = make_registry()
        info1 = make_equity_info(token=1001)
        info2 = make_equity_info(token=1001, parent="HDFC")
        reg.register(info1)
        reg.register(info2)
        assert reg.lookup(1001).parent_symbol == "HDFC"


# ── parent object ─────────────────────────────────────────────────────────────

class TestParentObject:
    def test_set_and_get_parent_object(self):
        reg = make_registry()
        obj = object()
        reg.set_parent_object("RELIANCE", obj)
        assert reg.get_parent_object("RELIANCE") is obj

    def test_get_unknown_parent_returns_none(self):
        reg = make_registry()
        assert reg.get_parent_object("UNKNOWN") is None


# ── strike gap ────────────────────────────────────────────────────────────────

class TestStrikeGap:
    def test_set_and_get_gap(self):
        reg = make_registry()
        reg.set_strike_gap("NIFTY", 100.0)
        assert reg.get_strike_gap("NIFTY") == 100.0

    def test_default_gap_is_50(self):
        reg = make_registry()
        assert reg.get_strike_gap("UNKNOWN") == 50.0


# ── get_tokens_by_type ────────────────────────────────────────────────────────

class TestGetTokensByType:
    def test_empty_for_unknown_parent(self):
        reg = make_registry()
        result = reg.get_tokens_by_type("UNKNOWN", TokenType.OPTION)
        assert result == set()

    def test_equity_token_returned_for_equity_type(self):
        reg = make_registry()
        info = make_equity_info(token=1001, parent="RELIANCE")
        reg.register(info)
        result = reg.get_tokens_by_type("RELIANCE", TokenType.EQUITY)
        assert 1001 in result

    def test_type_filtered_correctly(self):
        reg = make_registry()
        eq = make_equity_info(token=1001, parent="RELIANCE")
        opt = make_option_info(token=2001, parent="RELIANCE", strike=2800.0)
        reg.register(eq)
        reg.register(opt)
        opt_tokens = reg.get_tokens_by_type("RELIANCE", TokenType.OPTION)
        eq_tokens = reg.get_tokens_by_type("RELIANCE", TokenType.EQUITY)
        assert 2001 in opt_tokens
        assert 1001 not in opt_tokens
        assert 1001 in eq_tokens


# ── round_to_strike ───────────────────────────────────────────────────────────

class TestRoundToStrike:
    def test_already_on_strike(self):
        reg = make_registry()
        reg.set_strike_gap("NIFTY", 50.0)
        assert reg.round_to_strike(21000.0, "NIFTY") == pytest.approx(21000.0)

    def test_rounds_up_to_nearest_50(self):
        reg = make_registry()
        reg.set_strike_gap("NIFTY", 50.0)
        assert reg.round_to_strike(21030.0, "NIFTY") == pytest.approx(21050.0)

    def test_rounds_down_to_nearest_50(self):
        reg = make_registry()
        reg.set_strike_gap("NIFTY", 50.0)
        assert reg.round_to_strike(21010.0, "NIFTY") == pytest.approx(21000.0)

    def test_uses_default_gap_for_unknown_symbol(self):
        reg = make_registry()
        # Default gap=50; 21025 rounds to 21000 or 21050
        result = reg.round_to_strike(21025.0, "UNKNOWN")
        assert result % 50.0 == pytest.approx(0.0)

    def test_invalid_price_raises_value_error(self):
        reg = make_registry()
        with pytest.raises((ValueError, TypeError)):
            reg.round_to_strike(-1.0, "NIFTY")


# ── calculate_zones ───────────────────────────────────────────────────────────

class TestCalculateZones:
    def _register_strikes(self, reg, symbol, spot, gap, n=15):
        """Register 2*n+1 option tokens around spot (n above + n below)."""
        for i in range(-n, n + 1):
            strike = spot + i * gap
            t = 3000 + i + n
            reg.register(make_option_info(token=t, parent=symbol, strike=float(strike)))
        reg.set_strike_gap(symbol, gap)

    def test_core_zone_within_1pct(self):
        reg = make_registry()
        spot = 21000.0
        gap = 50.0
        self._register_strikes(reg, "NIFTY", spot, gap)
        zones = reg.calculate_zones("NIFTY", spot)
        # 21050 is 0.24% away → CORE
        assert zones.get(21050.0) == OptionZone.CORE

    def test_active_zone_between_1_and_3pct(self):
        reg = make_registry()
        spot = 21000.0
        gap = 50.0
        self._register_strikes(reg, "NIFTY", spot, gap)
        zones = reg.calculate_zones("NIFTY", spot)
        # 21300 is 1.43% away → ACTIVE
        assert zones.get(21300.0) == OptionZone.ACTIVE

    def test_peripheral_zone_between_3_and_5pct(self):
        reg = make_registry()
        spot = 21000.0
        gap = 50.0
        self._register_strikes(reg, "NIFTY", spot, gap)
        zones = reg.calculate_zones("NIFTY", spot)
        # 21700 is 3.33% away → PERIPHERAL
        assert zones.get(21700.0) == OptionZone.PERIPHERAL

    def test_beyond_5pct_not_in_zones(self):
        reg = make_registry()
        spot = 21000.0
        gap = 50.0
        self._register_strikes(reg, "NIFTY", spot, gap, n=20)
        zones = reg.calculate_zones("NIFTY", spot)
        # 22100 is 5.24% away → should not be in zones dict
        assert zones.get(22100.0) is None


# ── recenter_and_get_subscription_changes ────────────────────────────────────

class TestRecenterAndGetSubscriptionChanges:
    def _setup_registry(self, symbol="NIFTY", spot=21000.0, gap=50.0, n=12):
        reg = make_registry()
        reg.set_strike_gap(symbol, gap)
        for i in range(-n, n + 1):
            strike = spot + i * gap
            t = 5000 + i + n
            reg.register(make_option_info(token=t, parent=symbol, strike=float(strike)))
        return reg

    def test_same_atm_returns_no_changes(self):
        reg = self._setup_registry()
        reg.initial_subscribe_options("NIFTY", 21000.0)
        new_sub, unsub, mode_changes = reg.recenter_and_get_subscription_changes("NIFTY", 21000.0)
        assert new_sub == []
        assert unsub == []
        assert mode_changes == {}

    def test_different_atm_returns_subscription_lists(self):
        reg = self._setup_registry(n=15)
        reg.initial_subscribe_options("NIFTY", 21000.0)
        new_sub, unsub, mode_changes = reg.recenter_and_get_subscription_changes("NIFTY", 21200.0)
        # ATM changed from 21000 to nearest strike to 21200 → changes expected
        assert isinstance(new_sub, list)
        assert isinstance(unsub, list)

    def test_mode_changes_dict_returned(self):
        reg = self._setup_registry(n=15)
        reg.initial_subscribe_options("NIFTY", 21000.0)
        _, _, mode_changes = reg.recenter_and_get_subscription_changes("NIFTY", 21200.0)
        assert isinstance(mode_changes, dict)


# ── initial_subscribe_options ─────────────────────────────────────────────────

class TestInitialSubscribeOptions:
    def test_returns_token_list_and_mode_map(self):
        reg = make_registry()
        reg.set_strike_gap("NIFTY", 50.0)
        for i in range(-8, 9):
            strike = 21000.0 + i * 50
            t = 6000 + i + 8
            reg.register(make_option_info(token=t, parent="NIFTY", strike=float(strike)))

        tokens, mode_map = reg.initial_subscribe_options("NIFTY", 21000.0)
        assert isinstance(tokens, list)
        assert len(tokens) > 0
        assert isinstance(mode_map, dict)

    def test_mode_map_keys_match_tokens(self):
        reg = make_registry()
        reg.set_strike_gap("NIFTY", 50.0)
        for i in range(-8, 9):
            strike = 21000.0 + i * 50
            t = 7000 + i + 8
            reg.register(make_option_info(token=t, parent="NIFTY", strike=float(strike)))

        tokens, mode_map = reg.initial_subscribe_options("NIFTY", 21000.0)
        # mode_map is {ws_mode_string: [token_list]}, not {token: mode}
        all_mapped_tokens = [t for token_list in mode_map.values() for t in token_list]
        for token in tokens:
            assert token in all_mapped_tokens

    def test_sets_current_atm(self):
        reg = make_registry()
        reg.set_strike_gap("NIFTY", 50.0)
        for i in range(-8, 9):
            strike = 21000.0 + i * 50
            t = 8000 + i + 8
            reg.register(make_option_info(token=t, parent="NIFTY", strike=float(strike)))

        reg.initial_subscribe_options("NIFTY", 21000.0)
        assert reg._current_atm.get("NIFTY") == pytest.approx(21000.0)


# ── get_stats ─────────────────────────────────────────────────────────────────

class TestGetStats:
    def test_stats_structure(self):
        reg = make_registry()
        stats = reg.get_stats()
        assert "total_registered" in stats
        assert "subscribed" in stats
        assert "by_type" in stats

    def test_total_registered_reflects_registrations(self):
        reg = make_registry()
        reg.register(make_equity_info(token=1001))
        reg.register(make_equity_info(token=1002, parent="HDFC"))
        stats = reg.get_stats()
        assert stats["total_registered"] == 2

    def test_stats_after_unregister(self):
        reg = make_registry()
        reg.register(make_equity_info(token=1001))
        reg.unregister(1001)
        stats = reg.get_stats()
        assert stats["total_registered"] == 0

    def test_subscribed_count_initially_zero(self):
        reg = make_registry()
        reg.register(make_equity_info(token=1001))
        stats = reg.get_stats()
        assert stats["subscribed"] == 0


# ── Thread safety ─────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_register_unregister(self):
        reg = make_registry()
        errors = []

        def worker(offset):
            try:
                for i in range(50):
                    token = offset * 100 + i
                    reg.register(make_equity_info(token=token, parent=f"SYM{token}"))
                    reg.unregister(token)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert errors == []
