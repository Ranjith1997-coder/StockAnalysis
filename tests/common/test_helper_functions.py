"""Tests for common/helperFunctions.py."""
import json
import pytest
from datetime import time
from unittest.mock import patch, mock_open

from common.helperFunctions import percentageChange, isNowInTimePeriod, get_stock_objects_from_json


# ── percentageChange ──────────────────────────────────────────────────────────

class TestPercentageChange:
    def test_positive_gain(self):
        assert percentageChange(110.0, 100.0) == pytest.approx(10.0)

    def test_negative_loss(self):
        assert percentageChange(90.0, 100.0) == pytest.approx(-10.0)

    def test_no_change(self):
        assert percentageChange(100.0, 100.0) == pytest.approx(0.0)

    def test_large_gain(self):
        assert percentageChange(200.0, 100.0) == pytest.approx(100.0)

    def test_fractional_values(self):
        result = percentageChange(1.05, 1.0)
        assert result == pytest.approx(5.0)

    def test_zero_new_value(self):
        # 0 - 100 / 100 = -100%
        assert percentageChange(0.0, 100.0) == pytest.approx(-100.0)

    def test_formula_symmetric_sign(self):
        # percentageChange(110, 100) should be positive
        assert percentageChange(110.0, 100.0) > 0
        # percentageChange(90, 100) should be negative
        assert percentageChange(90.0, 100.0) < 0


# ── isNowInTimePeriod ─────────────────────────────────────────────────────────

class TestIsNowInTimePeriod:
    # ── Normal window (start < end) ──────────────────────────────────────────

    def test_inside_normal_window(self):
        assert isNowInTimePeriod(time(9, 15), time(15, 30), time(12, 0)) is True

    def test_before_normal_window(self):
        assert isNowInTimePeriod(time(9, 15), time(15, 30), time(8, 0)) is False

    def test_after_normal_window(self):
        assert isNowInTimePeriod(time(9, 15), time(15, 30), time(16, 0)) is False

    def test_on_start_boundary(self):
        assert isNowInTimePeriod(time(9, 15), time(15, 30), time(9, 15)) is True

    def test_on_end_boundary(self):
        assert isNowInTimePeriod(time(9, 15), time(15, 30), time(15, 30)) is True

    # ── Midnight-wrapping window (start > end) ────────────────────────────────

    def test_inside_overnight_window_evening(self):
        # Window: 22:00 – 06:00, now=23:30 → inside
        assert isNowInTimePeriod(time(22, 0), time(6, 0), time(23, 30)) is True

    def test_inside_overnight_window_morning(self):
        # Window: 22:00 – 06:00, now=01:00 → inside
        assert isNowInTimePeriod(time(22, 0), time(6, 0), time(1, 0)) is True

    def test_outside_overnight_window(self):
        # Window: 22:00 – 06:00, now=12:00 → outside
        assert isNowInTimePeriod(time(22, 0), time(6, 0), time(12, 0)) is False

    def test_on_overnight_start_boundary(self):
        assert isNowInTimePeriod(time(22, 0), time(6, 0), time(22, 0)) is True

    def test_on_overnight_end_boundary(self):
        assert isNowInTimePeriod(time(22, 0), time(6, 0), time(6, 0)) is True


# ── get_stock_objects_from_json ───────────────────────────────────────────────

class TestGetStockObjectsFromJson:
    _JSON = {
        "data": {
            "UnderlyingList": [{"symbol": "RELIANCE"}],
            "IndexList": [{"symbol": "NIFTY"}],
            "CommodityList": [{"symbol": "GOLD"}],
            "GlobalIndicesList": [{"symbol": "SP500"}],
        }
    }

    def test_returns_four_tuple(self):
        m = mock_open(read_data=json.dumps(self._JSON))
        with patch("builtins.open", m):
            result = get_stock_objects_from_json()
        assert len(result) == 4

    def test_underlying_list_correct(self):
        m = mock_open(read_data=json.dumps(self._JSON))
        with patch("builtins.open", m):
            underlying, *_ = get_stock_objects_from_json()
        assert underlying == [{"symbol": "RELIANCE"}]

    def test_index_list_correct(self):
        m = mock_open(read_data=json.dumps(self._JSON))
        with patch("builtins.open", m):
            _, index_list, *_ = get_stock_objects_from_json()
        assert index_list == [{"symbol": "NIFTY"}]

    def test_commodity_list_correct(self):
        m = mock_open(read_data=json.dumps(self._JSON))
        with patch("builtins.open", m):
            _, _, commodity_list, _ = get_stock_objects_from_json()
        assert commodity_list == [{"symbol": "GOLD"}]

    def test_global_indices_list_correct(self):
        m = mock_open(read_data=json.dumps(self._JSON))
        with patch("builtins.open", m):
            _, _, _, global_list = get_stock_objects_from_json()
        assert global_list == [{"symbol": "SP500"}]

    def test_missing_commodity_list_defaults_empty(self):
        data = {
            "data": {
                "UnderlyingList": [],
                "IndexList": [],
                # CommodityList absent
            }
        }
        m = mock_open(read_data=json.dumps(data))
        with patch("builtins.open", m):
            _, _, commodity_list, global_list = get_stock_objects_from_json()
        assert commodity_list == []
        assert global_list == []
