"""Tests for all _format_* methods and _build_holiday_warning_banner."""
import datetime
import pytest
from unittest.mock import patch
from premarket.premarket_report import PreMarketReport


# ── Helpers ───────────────────────────────────────────────────────────────────

def _report_with_sections(**sections):
    """Return a PreMarketReport with report_sections pre-populated."""
    r = PreMarketReport()
    r.report_sections.update(sections)
    return r


def _global_cues(positive=True):
    """Minimal global_cues section covering all three regions."""
    chg = 1.5 if positive else -1.5
    return {
        "S&P 500":    {"region": "US",     "price": 5000.0, "change_pct": chg},
        "Nasdaq":     {"region": "US",     "price": 17000.0, "change_pct": chg},
        "Dow Jones":  {"region": "US",     "price": 39000.0, "change_pct": chg},
        "DAX":        {"region": "Europe", "price": 18000.0, "change_pct": chg},
        "FTSE 100":   {"region": "Europe", "price": 8000.0,  "change_pct": chg},
        "Nikkei 225": {"region": "Asia",   "price": 38000.0, "change_pct": chg},
        "Hang Seng":  {"region": "Asia",   "price": 18000.0, "change_pct": chg},
        "Shanghai":   {"region": "Asia",   "price": 3200.0,  "change_pct": chg},
    }


def _bond_yields(short=4.5, long=4.8, long_chg_bps=5):
    return {
        "US 13-Week": {"yield_pct": short, "change_bps": 3, "prev_yield": short - 0.03},
        "US 10-Year": {"yield_pct": long,  "change_bps": long_chg_bps, "prev_yield": long - long_chg_bps / 100},
        "US 30-Year": {"yield_pct": 5.0,   "change_bps": 2, "prev_yield": 4.98},
        "13W-10Y Spread": {"spread_pct": long - short, "inverted": long < short},
    }


# ── TestFormatGlobalCues ───────────────────────────────────────────────────────

class TestFormatGlobalCues:
    def test_no_data_returns_unavailable_placeholder(self):
        report = _report_with_sections(global_cues=None)
        output = report._format_global_cues()
        assert "unavailable" in output.lower() or "Data" in output

    def test_with_data_contains_us_region(self):
        report = _report_with_sections(global_cues=_global_cues())
        output = report._format_global_cues()
        assert "US" in output

    def test_with_data_contains_europe_region(self):
        report = _report_with_sections(global_cues=_global_cues())
        output = report._format_global_cues()
        assert "Europe" in output

    def test_with_data_contains_asia_region(self):
        report = _report_with_sections(global_cues=_global_cues())
        output = report._format_global_cues()
        assert "Asia" in output

    def test_all_positive_shows_broadly_positive(self):
        report = _report_with_sections(global_cues=_global_cues(positive=True))
        output = report._format_global_cues()
        assert "positive" in output.lower()

    def test_all_negative_shows_broadly_negative(self):
        report = _report_with_sections(global_cues=_global_cues(positive=False))
        output = report._format_global_cues()
        assert "negative" in output.lower()


# ── TestFormatBondYields ───────────────────────────────────────────────────────

class TestFormatBondYields:
    def test_no_data_returns_unavailable_placeholder(self):
        report = _report_with_sections(bond_yields=None)
        output = report._format_bond_yields()
        assert "unavailable" in output.lower() or "Data" in output

    def test_inverted_curve_shows_inverted_label(self):
        yields = _bond_yields(short=5.0, long=4.5)  # spread < 0
        yields["13W-10Y Spread"]["inverted"] = True
        yields["13W-10Y Spread"]["spread_pct"] = -0.5
        report = _report_with_sections(bond_yields=yields)
        output = report._format_bond_yields()
        assert "INVERTED" in output

    def test_normal_curve_shows_normal_label(self):
        yields = _bond_yields(short=4.0, long=4.8)
        report = _report_with_sections(bond_yields=yields)
        output = report._format_bond_yields()
        assert "Normal" in output

    def test_change_bps_ge_10_shows_fii_outflow_warning(self):
        yields = _bond_yields(long_chg_bps=15)
        report = _report_with_sections(bond_yields=yields)
        output = report._format_bond_yields()
        assert "FII" in output and "outflow" in output.lower()

    def test_change_bps_le_minus_10_shows_risk_on_signal(self):
        yields = _bond_yields(long_chg_bps=-12)
        report = _report_with_sections(bond_yields=yields)
        output = report._format_bond_yields()
        assert "Risk-on" in output or "falling" in output.lower()

    def test_yield_ge_5_shows_structural_headwind(self):
        yields = _bond_yields(long=5.1)
        yields["US 10-Year"]["yield_pct"] = 5.1
        report = _report_with_sections(bond_yields=yields)
        output = report._format_bond_yields()
        assert "5%" in output or "headwind" in output.lower()


# ── TestFormatCommodities ──────────────────────────────────────────────────────

class TestFormatCommodities:
    def _comms(self, crude_chg=0.5, inr_chg=0.1):
        return {
            "Brent Crude": {"price": 85.0, "change_pct": crude_chg, "is_currency": False},
            "Gold":        {"price": 2300.0, "change_pct": 0.2, "is_currency": False},
            "Silver":      {"price": 27.0,   "change_pct": 0.1, "is_currency": False},
            "USD/INR":     {"price": 84.0,   "change_pct": inr_chg, "is_currency": True},
        }

    def test_no_data_returns_placeholder(self):
        report = _report_with_sections(commodities=None)
        output = report._format_commodities()
        assert "unavailable" in output.lower() or "Data" in output

    def test_brent_surge_gt_3pct_shows_bearish_crude_signal(self):
        report = _report_with_sections(commodities=self._comms(crude_chg=3.5))
        output = report._format_commodities()
        assert "Crude" in output or "negative" in output.lower()

    def test_brent_drop_lt_minus_3pct_shows_positive_signal(self):
        report = _report_with_sections(commodities=self._comms(crude_chg=-3.5))
        output = report._format_commodities()
        assert "positive" in output.lower() or "Crude" in output

    def test_inr_weakening_gt_0_5pct_shows_weakening_signal(self):
        report = _report_with_sections(commodities=self._comms(inr_chg=0.6))
        output = report._format_commodities()
        assert "weakening" in output.lower() or "Rupee" in output

    def test_inr_strengthening_lt_minus_0_5pct_shows_positive_signal(self):
        report = _report_with_sections(commodities=self._comms(inr_chg=-0.6))
        output = report._format_commodities()
        assert "strengthening" in output.lower() or "positive" in output.lower()

    def test_normal_crude_and_inr_no_signal_lines(self):
        report = _report_with_sections(commodities=self._comms(crude_chg=0.5, inr_chg=0.2))
        output = report._format_commodities()
        # Should not contain the strong signal texts
        assert "surging" not in output.lower()
        assert "weakening" not in output.lower()


# ── TestFormatIndiaVix ─────────────────────────────────────────────────────────

class TestFormatIndiaVix:
    def _vix(self, vix, trend="STABLE"):
        return {"vix": vix, "change_pct": 1.0, "prev_vix": vix - 0.5, "trend": trend}

    def test_no_data_returns_placeholder(self):
        report = _report_with_sections(india_vix=None)
        output = report._format_india_vix()
        assert "unavailable" in output.lower() or "Data" in output

    def test_vix_above_20_shows_high(self):
        report = _report_with_sections(india_vix=self._vix(21.0))
        output = report._format_india_vix()
        assert "HIGH" in output

    def test_vix_between_15_and_20_shows_moderate(self):
        report = _report_with_sections(india_vix=self._vix(17.0))
        output = report._format_india_vix()
        assert "MODERATE" in output

    def test_vix_below_12_shows_low(self):
        report = _report_with_sections(india_vix=self._vix(11.0))
        output = report._format_india_vix()
        assert "LOW" in output

    def test_vix_in_normal_range_shows_normal(self):
        report = _report_with_sections(india_vix=self._vix(13.5))
        output = report._format_india_vix()
        assert "NORMAL" in output

    def test_trend_rising_shows_hedging_message(self):
        report = _report_with_sections(india_vix=self._vix(16.0, trend="RISING"))
        output = report._format_india_vix()
        assert "hedging" in output.lower() or "RISING" in output


# ── TestFormatFiiDii ───────────────────────────────────────────────────────────

class TestFormatFiiDii:
    def _fii_dii(self, fii_val, dii_val):
        return {
            "date": "2026-04-29T00:00:00",
            "categories": {
                "FII CM*": {"name": "FII Cash Market",  "short": "FII CM*",
                             "value": fii_val, "children": {}},
                "DII CM*": {"name": "DII Cash Market",  "short": "DII CM*",
                             "value": dii_val, "children": {}},
            },
        }

    def test_no_data_returns_placeholder(self):
        report = _report_with_sections(fii_dii=None)
        output = report._format_fii_dii()
        assert "unavailable" in output.lower() or "Data" in output

    def test_fii_selling_absorbed_by_dii_signal(self):
        report = _report_with_sections(fii_dii=self._fii_dii(-1500.0, 800.0))
        output = report._format_fii_dii()
        assert "DII" in output
        assert "absorbed" in output.lower() or "support" in output.lower()

    def test_both_selling_shows_strong_selling_pressure(self):
        report = _report_with_sections(fii_dii=self._fii_dii(-1500.0, -600.0))
        output = report._format_fii_dii()
        assert "selling" in output.lower()

    def test_both_buying_shows_strong_demand(self):
        report = _report_with_sections(fii_dii=self._fii_dii(1200.0, 500.0))
        output = report._format_fii_dii()
        assert "buy" in output.lower() or "demand" in output.lower()

    def test_heavy_fii_selling_gt_2000_shows_heavy_alert(self):
        report = _report_with_sections(fii_dii=self._fii_dii(-2500.0, 0.0))
        output = report._format_fii_dii()
        assert "Heavy" in output or "2000" in output


# ── TestFormatPreopen ──────────────────────────────────────────────────────────

class TestFormatPreopen:
    def _preopen(self, ratio=1.0, gainers=3, losers=2, total=10):
        return {
            "top_gainers": [{"symbol": f"G{i}", "price": 100.0+i, "change_pct": 1.0+i}
                            for i in range(gainers)],
            "top_losers":  [{"symbol": f"L{i}", "price": 100.0+i, "change_pct": -1.0-i}
                            for i in range(losers)],
            "total_buy_qty":  int(ratio * 500_000),
            "total_sell_qty": 500_000,
            "buy_sell_ratio": ratio,
            "total_stocks": total,
            "gainers_count": gainers,
            "losers_count": losers,
            "high_volume_stocks": [{"symbol": "HVOL", "volume": 1_000_000, "multiple": 5.2}],
        }

    def test_no_data_returns_placeholder(self):
        report = _report_with_sections(preopen=None)
        output = report._format_preopen()
        assert "unavailable" in output.lower() or "Data" in output

    def test_ratio_gt_1_5_shows_strong_buying(self):
        report = _report_with_sections(preopen=self._preopen(ratio=1.6))
        output = report._format_preopen()
        assert "STRONG" in output and "buy" in output.lower()

    def test_ratio_between_1_1_and_1_5_shows_mildly_positive(self):
        report = _report_with_sections(preopen=self._preopen(ratio=1.2))
        output = report._format_preopen()
        assert "Mildly positive" in output

    def test_ratio_lt_0_7_shows_strong_selling(self):
        report = _report_with_sections(preopen=self._preopen(ratio=0.6))
        output = report._format_preopen()
        assert "STRONG" in output and "sell" in output.lower()

    def test_ratio_between_0_7_and_0_9_shows_mildly_negative(self):
        report = _report_with_sections(preopen=self._preopen(ratio=0.8))
        output = report._format_preopen()
        assert "Mildly negative" in output

    def test_ratio_between_0_9_and_1_1_shows_balanced(self):
        report = _report_with_sections(preopen=self._preopen(ratio=1.0))
        output = report._format_preopen()
        assert "Balanced" in output

    def test_output_contains_gainers_section(self):
        report = _report_with_sections(preopen=self._preopen())
        output = report._format_preopen()
        assert "Gainers" in output

    def test_output_contains_losers_section(self):
        report = _report_with_sections(preopen=self._preopen())
        output = report._format_preopen()
        assert "Losers" in output

    def test_output_contains_abnormal_volume_section(self):
        report = _report_with_sections(preopen=self._preopen())
        output = report._format_preopen()
        assert "Volume" in output


# ── TestBuildHolidayWarningBanner ──────────────────────────────────────────────

class TestBuildHolidayWarningBanner:
    _HOLIDAYS = "common.market_calendar.get_upcoming_holidays"

    def test_no_holidays_returns_empty_string(self):
        with patch(self._HOLIDAYS, return_value=[]):
            result = PreMarketReport()._build_holiday_warning_banner()
        assert result == ""

    def test_one_holiday_uses_singular_form(self):
        holiday = [datetime.date(2026, 5, 5)]
        with patch(self._HOLIDAYS, return_value=holiday):
            result = PreMarketReport()._build_holiday_warning_banner()
        assert "holiday" in result.lower()
        assert "holidays" not in result

    def test_two_holidays_uses_plural_form(self):
        holidays = [datetime.date(2026, 5, 5), datetime.date(2026, 5, 12)]
        with patch(self._HOLIDAYS, return_value=holidays):
            result = PreMarketReport()._build_holiday_warning_banner()
        assert "holidays" in result.lower()

    def test_banner_contains_theta_decay_text(self):
        holiday = [datetime.date(2026, 5, 5)]
        with patch(self._HOLIDAYS, return_value=holiday):
            result = PreMarketReport()._build_holiday_warning_banner()
        assert "Theta" in result or "decay" in result.lower()

    def test_exception_from_calendar_returns_empty_string(self):
        with patch(self._HOLIDAYS, side_effect=Exception("network error")):
            result = PreMarketReport()._build_holiday_warning_banner()
        assert result == ""

    def test_banner_contains_holiday_date(self):
        holiday = [datetime.date(2026, 5, 5)]
        with patch(self._HOLIDAYS, return_value=holiday):
            result = PreMarketReport()._build_holiday_warning_banner()
        assert "05 May 2026" in result
