"""Tests for parsing methods: _parse_global_cues, _parse_bond_yields,
_parse_commodities_currencies, _parse_india_vix.

Strategy: create a real PreMarketReport instance and monkeypatch
_get_close_prices so no network call is made.
"""
import pytest
from premarket.premarket_report import (
    PreMarketReport,
    GLOBAL_INDICES,
    BOND_YIELD_TICKERS,
    COMMODITY_TICKERS,
    CURRENCY_TICKERS,
    INDIA_VIX_TICKER,
)
from tests.premarket.conftest import make_close_series


# ── TestParseGlobalCues ────────────────────────────────────────────────────────

class TestParseGlobalCues:
    def _report_with_prices(self, monkeypatch, price_map):
        """Build report where _get_close_prices returns make_close_series for each ticker."""
        report = PreMarketReport()

        def fake_get_close(ticker):
            prices = price_map.get(ticker)
            return make_close_series(prices) if prices is not None else None

        monkeypatch.setattr(report, "_get_close_prices", fake_get_close)
        report._parse_global_cues()
        return report

    def _sp500_prices(self):
        return {ticker: [100.0, 105.0]
                for region in GLOBAL_INDICES.values()
                for ticker in region.values()}

    def test_correct_change_pct_formula(self, monkeypatch):
        prices = {t: [100.0, 110.0]
                  for r in GLOBAL_INDICES.values() for t in r.values()}
        report = self._report_with_prices(monkeypatch, prices)
        cues = report.report_sections["global_cues"]
        for info in cues.values():
            assert info["change_pct"] == pytest.approx(10.0)

    def test_section_set_with_valid_data(self, monkeypatch):
        prices = self._sp500_prices()
        report = self._report_with_prices(monkeypatch, prices)
        assert report.report_sections["global_cues"] is not None

    def test_fewer_than_two_bars_excluded(self, monkeypatch):
        # Only SP500 has 2 bars; rest return only 1 bar (or None)
        sp500_ticker = GLOBAL_INDICES["US"]["S&P 500"]
        prices = {sp500_ticker: [100.0, 105.0]}  # all others → None
        report = self._report_with_prices(monkeypatch, prices)
        cues = report.report_sections["global_cues"]
        # Only S&P 500 should be in results
        assert "S&P 500" in cues
        # Nasdaq / DAX etc. had no data — should be absent
        assert "Nasdaq" not in cues

    def test_none_close_prices_excluded(self, monkeypatch):
        prices = {}  # all tickers → None
        report = self._report_with_prices(monkeypatch, prices)
        # section may be None or empty dict
        cues = report.report_sections["global_cues"]
        assert cues is None or len(cues) == 0

    def test_region_stored_in_each_entry(self, monkeypatch):
        prices = self._sp500_prices()
        report = self._report_with_prices(monkeypatch, prices)
        cues = report.report_sections["global_cues"]
        for name, info in cues.items():
            assert "region" in info
            assert info["region"] in ("US", "Europe", "Asia")

    def test_price_stored_is_latest_close(self, monkeypatch):
        sp500_ticker = GLOBAL_INDICES["US"]["S&P 500"]
        prices = {sp500_ticker: [90.0, 120.0]}
        report = self._report_with_prices(monkeypatch, prices)
        cues = report.report_sections["global_cues"]
        assert cues["S&P 500"]["price"] == pytest.approx(120.0)


# ── TestParseBondYields ────────────────────────────────────────────────────────

class TestParseBondYields:
    def _report(self, monkeypatch, price_map):
        report = PreMarketReport()

        def fake(ticker):
            prices = price_map.get(ticker)
            return make_close_series(prices) if prices else None

        monkeypatch.setattr(report, "_get_close_prices", fake)
        report._parse_bond_yields()
        return report

    def _all_yields(self, prev=4.0, cur=4.1):
        return {t: [prev, cur] for t in BOND_YIELD_TICKERS.values()}

    def test_change_bps_formula(self, monkeypatch):
        # prev=4.0%, cur=4.5% → (4.5-4.0)*100 = 50 bps
        prices = {t: [4.0, 4.5] for t in BOND_YIELD_TICKERS.values()}
        report = self._report(monkeypatch, prices)
        yields = report.report_sections["bond_yields"]
        for name in ["US 13-Week", "US 10-Year", "US 30-Year"]:
            assert yields[name]["change_bps"] == pytest.approx(50.0)

    def test_yield_spread_computed(self, monkeypatch):
        prices = {
            BOND_YIELD_TICKERS["US 13-Week"]: [3.0, 5.0],   # 13W = 5.0%
            BOND_YIELD_TICKERS["US 10-Year"]: [4.0, 4.5],   # 10Y = 4.5%
            BOND_YIELD_TICKERS["US 30-Year"]: [4.5, 4.8],
        }
        report = self._report(monkeypatch, prices)
        yields = report.report_sections["bond_yields"]
        spread = yields["13W-10Y Spread"]["spread_pct"]
        # 10Y(4.5) - 13W(5.0) = -0.5 → inverted
        assert spread == pytest.approx(-0.5)
        assert yields["13W-10Y Spread"]["inverted"] is True

    def test_normal_yield_curve_not_inverted(self, monkeypatch):
        prices = {
            BOND_YIELD_TICKERS["US 13-Week"]: [3.0, 4.0],   # 13W = 4.0%
            BOND_YIELD_TICKERS["US 10-Year"]: [4.0, 4.8],   # 10Y = 4.8%
            BOND_YIELD_TICKERS["US 30-Year"]: [4.5, 5.0],
        }
        report = self._report(monkeypatch, prices)
        yields = report.report_sections["bond_yields"]
        assert yields["13W-10Y Spread"]["inverted"] is False

    def test_missing_ticker_data_excluded(self, monkeypatch):
        # Only supply 10-Year — 13-Week/30-Year absent
        prices = {BOND_YIELD_TICKERS["US 10-Year"]: [4.0, 4.5]}
        report = self._report(monkeypatch, prices)
        yields = report.report_sections["bond_yields"]
        assert "US 10-Year" in yields
        assert "US 13-Week" not in yields
        # spread requires both 13W and 10Y → should not be computed
        assert "13W-10Y Spread" not in yields

    def test_no_data_section_is_none(self, monkeypatch):
        report = self._report(monkeypatch, {})
        assert report.report_sections["bond_yields"] is None


# ── TestParseCommoditiesCurrencies ─────────────────────────────────────────────

class TestParseCommoditiesCurrencies:
    def _report(self, monkeypatch, price_map):
        report = PreMarketReport()

        def fake(ticker):
            prices = price_map.get(ticker)
            return make_close_series(prices) if prices else None

        monkeypatch.setattr(report, "_get_close_prices", fake)
        report._parse_commodities_currencies()
        return report

    def _all_prices(self):
        m = {t: [100.0, 105.0] for t in COMMODITY_TICKERS.values()}
        m.update({t: [83.0, 84.0] for t in CURRENCY_TICKERS.values()})
        return m

    def test_commodities_have_is_currency_false(self, monkeypatch):
        report = self._report(monkeypatch, self._all_prices())
        comms = report.report_sections["commodities"]
        for name in COMMODITY_TICKERS:
            assert comms[name]["is_currency"] is False

    def test_currencies_have_is_currency_true(self, monkeypatch):
        report = self._report(monkeypatch, self._all_prices())
        comms = report.report_sections["commodities"]
        for name in CURRENCY_TICKERS:
            assert comms[name]["is_currency"] is True

    def test_change_pct_formula(self, monkeypatch):
        ticker = list(COMMODITY_TICKERS.values())[0]
        prices = {ticker: [100.0, 110.0]}
        report = self._report(monkeypatch, prices)
        comms = report.report_sections["commodities"]
        name = list(COMMODITY_TICKERS.keys())[0]
        assert comms[name]["change_pct"] == pytest.approx(10.0)

    def test_no_data_section_is_none(self, monkeypatch):
        report = self._report(monkeypatch, {})
        assert report.report_sections["commodities"] is None


# ── TestParseIndiaVix ──────────────────────────────────────────────────────────

class TestParseIndiaVix:
    def _report(self, monkeypatch, prices):
        report = PreMarketReport()
        series = make_close_series(prices) if prices is not None else None

        def fake(ticker):
            return series

        monkeypatch.setattr(report, "_get_close_prices", fake)
        report._parse_india_vix()
        return report

    def test_rising_trend_four_consecutive_up_days(self, monkeypatch):
        # 5 values, 4 rising steps
        report = self._report(monkeypatch, [15.0, 16.0, 17.0, 18.0, 19.0])
        assert report.report_sections["india_vix"]["trend"] == "RISING"

    def test_falling_trend_zero_rising_days(self, monkeypatch):
        report = self._report(monkeypatch, [20.0, 19.0, 18.0, 17.0, 16.0])
        assert report.report_sections["india_vix"]["trend"] == "FALLING"

    def test_stable_trend_two_of_four_rising(self, monkeypatch):
        # up, down, up, down → 2 rising
        report = self._report(monkeypatch, [15.0, 16.0, 15.5, 16.2, 15.8])
        assert report.report_sections["india_vix"]["trend"] == "STABLE"

    def test_fewer_than_five_bars_trend_is_na(self, monkeypatch):
        report = self._report(monkeypatch, [15.0, 16.0])
        assert report.report_sections["india_vix"]["trend"] == "N/A"

    def test_prev_zero_sets_section_none(self, monkeypatch):
        report = self._report(monkeypatch, [0.0, 15.0])
        assert report.report_sections["india_vix"] is None

    def test_none_data_sets_section_none(self, monkeypatch):
        report = self._report(monkeypatch, None)
        assert report.report_sections["india_vix"] is None

    def test_change_pct_formula(self, monkeypatch):
        report = self._report(monkeypatch, [10.0, 11.0])
        vix_data = report.report_sections["india_vix"]
        assert vix_data["change_pct"] == pytest.approx(10.0)

    def test_vix_and_prev_stored(self, monkeypatch):
        report = self._report(monkeypatch, [14.0, 15.0])
        vix_data = report.report_sections["india_vix"]
        assert vix_data["vix"] == pytest.approx(15.0)
        assert vix_data["prev_vix"] == pytest.approx(14.0)
