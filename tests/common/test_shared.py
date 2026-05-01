"""Tests for common/shared.py — AppContext and Mode."""
import pytest
from common.shared import AppContext, Mode, app_ctx, ticker_52_week_high_list, ticker_52_week_low_list


class TestMode:
    def test_intraday_value(self):
        assert Mode.INTRADAY.value == 1

    def test_positional_value(self):
        assert Mode.POSITIONAL.value == 2

    def test_are_distinct(self):
        assert Mode.INTRADAY != Mode.POSITIONAL

    def test_name_intraday(self):
        assert Mode.INTRADAY.name == "INTRADAY"

    def test_name_positional(self):
        assert Mode.POSITIONAL.name == "POSITIONAL"


class TestAppContextDefaults:
    def setup_method(self):
        self.ctx = AppContext()

    def test_stock_token_dict_empty(self):
        assert self.ctx.stock_token_obj_dict == {}

    def test_index_token_dict_empty(self):
        assert self.ctx.index_token_obj_dict == {}

    def test_commodity_token_dict_empty(self):
        assert self.ctx.commodity_token_obj_dict == {}

    def test_global_indices_token_dict_empty(self):
        assert self.ctx.global_indices_token_obj_dict == {}

    def test_stocks_list_empty(self):
        assert self.ctx.stocks_list == []

    def test_index_list_empty(self):
        assert self.ctx.index_list == []

    def test_commodity_list_empty(self):
        assert self.ctx.commodity_list == []

    def test_global_indices_list_empty(self):
        assert self.ctx.global_indices_list == []

    def test_stock_expires_empty(self):
        assert self.ctx.stockExpires == []

    def test_mode_is_none(self):
        assert self.ctx.mode is None

    def test_zd_ticker_manager_is_none(self):
        assert self.ctx.zd_ticker_manager is None

    def test_zd_kc_is_none(self):
        assert self.ctx.zd_kc is None

    def test_token_registry_is_none(self):
        assert self.ctx.token_registry is None

    def test_signal_bus_is_none(self):
        assert self.ctx.signal_bus is None

    def test_correlator_is_none(self):
        assert self.ctx.correlator is None

    def test_narrator_is_none(self):
        assert self.ctx.narrator is None

    def test_mode_can_be_set(self):
        self.ctx.mode = Mode.INTRADAY
        assert self.ctx.mode == Mode.INTRADAY


class TestModuleGlobals:
    def test_app_ctx_is_app_context_instance(self):
        assert isinstance(app_ctx, AppContext)

    def test_ticker_52_week_high_list_is_list(self):
        assert isinstance(ticker_52_week_high_list, list)

    def test_ticker_52_week_low_list_is_list(self):
        assert isinstance(ticker_52_week_low_list, list)
