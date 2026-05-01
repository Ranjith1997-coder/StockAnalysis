"""Tests for common/market_calendar.py."""
import json
import pytest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock
import common.market_calendar as cal


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clear_all_caches():
    """Reset all in-process state between tests."""
    cal._nse_holiday_cache.clear()
    cal._load_custom_holidays.cache_clear()
    cal._get_xnse_calendar.cache_clear()


@pytest.fixture(autouse=True)
def clear_caches():
    _clear_all_caches()
    yield
    _clear_all_caches()


# ── is_trading_day ────────────────────────────────────────────────────────────

class TestIsTradingDay:
    def test_saturday_is_not_trading_day(self):
        # Find the next Saturday
        today = date(2026, 5, 2)  # Saturday
        assert today.weekday() == 5
        assert cal.is_trading_day(today) is False

    def test_sunday_is_not_trading_day(self):
        sunday = date(2026, 5, 3)
        assert sunday.weekday() == 6
        assert cal.is_trading_day(sunday) is False

    def test_weekday_not_in_holiday_set_is_trading_day(self):
        monday = date(2026, 5, 4)
        assert monday.weekday() == 0
        with patch("common.market_calendar._get_holiday_set", return_value=frozenset()):
            assert cal.is_trading_day(monday) is True

    def test_weekday_in_holiday_set_is_not_trading_day(self):
        monday = date(2026, 5, 4)
        with patch("common.market_calendar._get_holiday_set", return_value=frozenset([monday])):
            assert cal.is_trading_day(monday) is False

    def test_defaults_to_today(self):
        with patch("common.market_calendar._get_holiday_set", return_value=frozenset()):
            with patch("common.market_calendar.datetime") as mock_dt:
                # Use a known Monday
                mock_dt.now.return_value.date.return_value = date(2026, 5, 4)
                result = cal.is_trading_day()
        assert isinstance(result, bool)

    def test_known_republic_day_holiday(self):
        # 26 Jan 2026 is Republic Day (Monday) — should be in NSE holidays
        republic_day = date(2026, 1, 26)
        with patch("common.market_calendar._get_holiday_set",
                   return_value=frozenset([republic_day])):
            assert cal.is_trading_day(republic_day) is False


# ── get_upcoming_holidays ─────────────────────────────────────────────────────

class TestGetUpcomingHolidays:
    def test_days_ahead_less_than_one_returns_empty(self):
        assert cal.get_upcoming_holidays(days_ahead=0) == []
        assert cal.get_upcoming_holidays(days_ahead=-1) == []

    def test_no_holidays_in_window_returns_empty(self):
        with patch("common.market_calendar._get_holiday_set", return_value=frozenset()):
            result = cal.get_upcoming_holidays(days_ahead=7)
        assert result == []

    def test_holiday_within_window_returned(self):
        with patch("common.market_calendar.datetime") as mock_dt:
            today = date(2026, 5, 1)
            mock_dt.now.return_value.date.return_value = today
            holiday = today + timedelta(days=3)   # May 4 (Monday)
            with patch("common.market_calendar._get_holiday_set",
                       return_value=frozenset([holiday])):
                result = cal.get_upcoming_holidays(days_ahead=7)
        assert holiday in result

    def test_weekends_excluded_from_result(self):
        with patch("common.market_calendar.datetime") as mock_dt:
            today = date(2026, 5, 1)
            mock_dt.now.return_value.date.return_value = today
            saturday = date(2026, 5, 2)  # Saturday
            with patch("common.market_calendar._get_holiday_set",
                       return_value=frozenset([saturday])):
                result = cal.get_upcoming_holidays(days_ahead=7)
        assert saturday not in result

    def test_result_is_sorted(self):
        with patch("common.market_calendar.datetime") as mock_dt:
            today = date(2026, 5, 1)
            mock_dt.now.return_value.date.return_value = today
            h1 = date(2026, 5, 7)
            h2 = date(2026, 5, 5)
            with patch("common.market_calendar._get_holiday_set",
                       return_value=frozenset([h1, h2])):
                result = cal.get_upcoming_holidays(days_ahead=14)
        assert result == sorted(result)

    def test_year_boundary_scans_both_years(self):
        """Scanning from Dec 30 with days_ahead=7 must query both 2025 and 2026."""
        calls = []

        def track_calls(year):
            calls.append(year)
            return frozenset()

        with patch("common.market_calendar.datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = date(2025, 12, 30)
            with patch("common.market_calendar._get_holiday_set", side_effect=track_calls):
                cal.get_upcoming_holidays(days_ahead=7)
        assert 2025 in calls
        assert 2026 in calls


# ── clear_nse_cache ───────────────────────────────────────────────────────────

class TestClearNseCache:
    def test_clear_all_empties_cache(self):
        cal._nse_holiday_cache[2025] = frozenset()
        cal._nse_holiday_cache[2026] = frozenset()
        cal.clear_nse_cache()
        assert cal._nse_holiday_cache == {}

    def test_clear_specific_year(self):
        cal._nse_holiday_cache[2025] = frozenset()
        cal._nse_holiday_cache[2026] = frozenset()
        cal.clear_nse_cache(year=2025)
        assert 2025 not in cal._nse_holiday_cache
        assert 2026 in cal._nse_holiday_cache

    def test_clear_unknown_year_no_error(self):
        cal.clear_nse_cache(year=1999)  # must not raise


# ── _load_custom_holidays ─────────────────────────────────────────────────────

class TestLoadCustomHolidays:
    def test_valid_json_array_returns_frozenset(self, tmp_path):
        holidays_file = tmp_path / "custom_holidays.json"
        holidays_file.write_text('["2026-10-02", "2026-01-26"]', encoding="utf-8")
        cal._load_custom_holidays.cache_clear()
        with patch("common.market_calendar._CUSTOM_HOLIDAYS_PATH", holidays_file):
            result = cal._load_custom_holidays()
        assert date(2026, 10, 2) in result
        assert date(2026, 1, 26) in result

    def test_missing_file_returns_empty_frozenset(self, tmp_path):
        nonexistent = tmp_path / "no_file.json"
        cal._load_custom_holidays.cache_clear()
        with patch("common.market_calendar._CUSTOM_HOLIDAYS_PATH", nonexistent):
            result = cal._load_custom_holidays()
        assert result == frozenset()

    def test_non_array_json_returns_empty_frozenset(self, tmp_path):
        holidays_file = tmp_path / "custom_holidays.json"
        holidays_file.write_text('{"date": "2026-10-02"}', encoding="utf-8")
        cal._load_custom_holidays.cache_clear()
        with patch("common.market_calendar._CUSTOM_HOLIDAYS_PATH", holidays_file):
            result = cal._load_custom_holidays()
        assert result == frozenset()

    def test_malformed_date_skipped(self, tmp_path):
        holidays_file = tmp_path / "custom_holidays.json"
        holidays_file.write_text('["2026-10-02", "not-a-date", "2026-01-26"]',
                                  encoding="utf-8")
        cal._load_custom_holidays.cache_clear()
        with patch("common.market_calendar._CUSTOM_HOLIDAYS_PATH", holidays_file):
            result = cal._load_custom_holidays()
        assert date(2026, 10, 2) in result
        assert date(2026, 1, 26) in result
        assert len(result) == 2

    def test_empty_file_returns_empty_frozenset(self, tmp_path):
        holidays_file = tmp_path / "custom_holidays.json"
        holidays_file.write_text("", encoding="utf-8")
        cal._load_custom_holidays.cache_clear()
        with patch("common.market_calendar._CUSTOM_HOLIDAYS_PATH", holidays_file):
            result = cal._load_custom_holidays()
        assert result == frozenset()


# ── _get_holiday_set ──────────────────────────────────────────────────────────

class TestGetHolidaySet:
    def test_nse_data_used_as_base_when_present(self):
        nse_holiday = date(2026, 3, 25)
        custom_holiday = date(2026, 8, 15)

        with patch("common.market_calendar._get_nse_holiday_set",
                   return_value=frozenset([nse_holiday])):
            with patch("common.market_calendar._load_custom_holidays",
                       return_value=frozenset([custom_holiday])):
                result = cal._get_holiday_set(2026)

        assert nse_holiday in result
        assert custom_holiday in result

    def test_xnse_fallback_used_when_nse_empty(self):
        xnse_holiday = date(2026, 4, 14)
        custom_holiday = date(2026, 1, 26)

        with patch("common.market_calendar._get_nse_holiday_set", return_value=frozenset()):
            with patch("common.market_calendar._get_xnse_holiday_set",
                       return_value=frozenset([xnse_holiday])):
                with patch("common.market_calendar._load_custom_holidays",
                           return_value=frozenset([custom_holiday])):
                    result = cal._get_holiday_set(2026)

        assert xnse_holiday in result
        assert custom_holiday in result

    def test_custom_dates_only_merged_for_matching_year(self):
        nse = frozenset([date(2026, 3, 25)])
        # Custom has dates for both 2025 and 2026
        custom = frozenset([date(2025, 10, 2), date(2026, 8, 15)])

        with patch("common.market_calendar._get_nse_holiday_set", return_value=nse):
            with patch("common.market_calendar._load_custom_holidays", return_value=custom):
                result = cal._get_holiday_set(2026)

        assert date(2026, 8, 15) in result
        assert date(2025, 10, 2) not in result  # wrong year — must not bleed in
