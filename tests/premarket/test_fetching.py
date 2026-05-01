"""Tests for HTTP-fetching methods: _fetch_fii_dii, _parse_fii_dii,
_fetch_nse_preopen.
"""
import pytest
from unittest.mock import patch, MagicMock
from premarket.premarket_report import (
    PreMarketReport,
    FII_DII_RETRIES,
    FII_DII_TIMEOUT,
)
from tests.premarket.conftest import mock_response


# ── TestFetchFiiDii ────────────────────────────────────────────────────────────

class TestFetchFiiDii:
    def test_success_first_try_returns_parsed_data(self, mock_fii_dii_raw):
        with patch("premarket.premarket_report.requests.get",
                   return_value=mock_response(mock_fii_dii_raw, 200)):
            result = PreMarketReport()._fetch_fii_dii()
        assert result is not None
        assert "date" in result

    def test_retries_on_http_500(self, mock_fii_dii_raw):
        fail = mock_response(None, 500)
        ok   = mock_response(mock_fii_dii_raw, 200)
        with patch("premarket.premarket_report.requests.get",
                   side_effect=[fail, fail, ok]):
            with patch("premarket.premarket_report.time_module.sleep"):
                result = PreMarketReport()._fetch_fii_dii()
        assert result is not None

    def test_returns_none_after_all_retries_exhausted(self):
        fail = mock_response(None, 500)
        with patch("premarket.premarket_report.requests.get", return_value=fail):
            with patch("premarket.premarket_report.time_module.sleep"):
                result = PreMarketReport()._fetch_fii_dii()
        assert result is None

    def test_retries_on_connection_error(self, mock_fii_dii_raw):
        ok = mock_response(mock_fii_dii_raw, 200)
        with patch("premarket.premarket_report.requests.get",
                   side_effect=[ConnectionError("timeout"), ok]):
            with patch("premarket.premarket_report.time_module.sleep"):
                result = PreMarketReport()._fetch_fii_dii()
        assert result is not None

    def test_sleep_called_between_retries(self):
        fail = mock_response(None, 500)
        with patch("premarket.premarket_report.requests.get", return_value=fail):
            with patch("premarket.premarket_report.time_module.sleep") as mock_sleep:
                PreMarketReport()._fetch_fii_dii()
        assert mock_sleep.call_count == FII_DII_RETRIES

    def test_requests_get_uses_configured_timeout(self, mock_fii_dii_raw):
        with patch("premarket.premarket_report.requests.get",
                   return_value=mock_response(mock_fii_dii_raw, 200)) as mock_get:
            with patch("premarket.premarket_report.time_module.sleep"):
                PreMarketReport()._fetch_fii_dii()
        _, kwargs = mock_get.call_args
        assert kwargs.get("timeout") == FII_DII_TIMEOUT


# ── TestParseFiiDii ────────────────────────────────────────────────────────────

class TestParseFiiDii:
    def _parse(self, raw):
        return PreMarketReport()._parse_fii_dii(raw)

    def test_none_returns_none(self):
        assert self._parse(None) is None

    def test_empty_list_returns_none(self):
        assert self._parse([]) is None

    def test_valid_raw_returns_date(self, mock_fii_dii_raw):
        result = self._parse(mock_fii_dii_raw)
        assert result["date"] == "2026-04-29T00:00:00"

    def test_valid_raw_returns_categories_key(self, mock_fii_dii_raw):
        result = self._parse(mock_fii_dii_raw)
        assert "categories" in result

    def test_fii_cm_in_categories(self, mock_fii_dii_raw):
        result = self._parse(mock_fii_dii_raw)
        assert "FII CM*" in result["categories"]

    def test_dii_cm_value_preserved(self, mock_fii_dii_raw):
        result = self._parse(mock_fii_dii_raw)
        assert result["categories"]["DII CM*"]["value"] == -800.0

    def test_child_data_expanded_into_children_dict(self, mock_fii_dii_raw):
        result = self._parse(mock_fii_dii_raw)
        fii_idx_fut = result["categories"]["FII Idx Fut"]
        assert "NIFTY" in fii_idx_fut["children"]
        assert "BANKNIFTY" in fii_idx_fut["children"]

    def test_child_value_stored_correctly(self, mock_fii_dii_raw):
        result = self._parse(mock_fii_dii_raw)
        nifty_child = result["categories"]["FII Idx Fut"]["children"]["NIFTY"]
        assert nifty_child["value"] == 2000.0

    def test_category_without_children_has_empty_children_dict(self, mock_fii_dii_raw):
        result = self._parse(mock_fii_dii_raw)
        fii_cm = result["categories"]["FII CM*"]
        assert fii_cm["children"] == {}

    def test_non_list_input_returns_none(self):
        assert self._parse({"Date": "x"}) is None


# ── TestFetchNsePreopen ────────────────────────────────────────────────────────

class TestFetchNsePreopen:
    def _make_nse_response(self, preopen_data, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = {"data": preopen_data}
        return resp

    def test_nse_urlfetch_called_once(self, sample_preopen_data):
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response(sample_preopen_data)) as mock_fetch:
            PreMarketReport()._fetch_nse_preopen()
        mock_fetch.assert_called_once()

    def test_non_200_returns_none(self):
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response([], status_code=403)):
            result = PreMarketReport()._fetch_nse_preopen()
        assert result is None

    def test_empty_preopen_data_returns_none(self):
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response([])):
            result = PreMarketReport()._fetch_nse_preopen()
        assert result is None

    def test_gainers_sorted_descending_by_change_pct(self, sample_preopen_data):
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response(sample_preopen_data)):
            result = PreMarketReport()._fetch_nse_preopen()
        gainers = result["top_gainers"]
        pcts = [g["change_pct"] for g in gainers]
        assert pcts == sorted(pcts, reverse=True)

    def test_losers_sorted_ascending_by_change_pct(self, sample_preopen_data):
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response(sample_preopen_data)):
            result = PreMarketReport()._fetch_nse_preopen()
        losers = result["top_losers"]
        pcts = [l["change_pct"] for l in losers]
        assert pcts == sorted(pcts)

    def test_top_gainers_capped_at_five(self, sample_preopen_data):
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response(sample_preopen_data)):
            result = PreMarketReport()._fetch_nse_preopen()
        assert len(result["top_gainers"]) <= 5

    def test_top_losers_capped_at_five(self, sample_preopen_data):
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response(sample_preopen_data)):
            result = PreMarketReport()._fetch_nse_preopen()
        assert len(result["top_losers"]) <= 5

    def test_buy_sell_ratio_formula(self, sample_preopen_data):
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response(sample_preopen_data)):
            result = PreMarketReport()._fetch_nse_preopen()
        expected = result["total_buy_qty"] / result["total_sell_qty"]
        assert result["buy_sell_ratio"] == pytest.approx(expected)

    def test_buy_sell_ratio_zero_when_no_sell(self):
        """total_sell_qty=0 → ratio=0 (no division by zero)."""
        data = [{"metadata": {
            "symbol": "X", "finalPrice": 100, "previousClose": 99,
            "pChange": 1.0, "finalQuantity": 1000,
            "totalBuyQuantity": 5000, "totalSellQuantity": 0,
        }}]
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response(data)):
            result = PreMarketReport()._fetch_nse_preopen()
        assert result["buy_sell_ratio"] == 0

    def test_high_volume_stocks_above_2_5x_avg(self, sample_preopen_data):
        """IDX10 has 6× avg volume — must appear in high_volume_stocks."""
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response(sample_preopen_data)):
            result = PreMarketReport()._fetch_nse_preopen()
        high_syms = [h["symbol"] for h in result["high_volume_stocks"]]
        assert "IDX10" in high_syms

    def test_high_volume_stocks_capped_at_five(self, sample_preopen_data):
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response(sample_preopen_data)):
            result = PreMarketReport()._fetch_nse_preopen()
        assert len(result["high_volume_stocks"]) <= 5

    def test_result_contains_expected_keys(self, sample_preopen_data):
        with patch("premarket.premarket_report.nse_urlfetch",
                   return_value=self._make_nse_response(sample_preopen_data)):
            result = PreMarketReport()._fetch_nse_preopen()
        for key in ["top_gainers", "top_losers", "total_buy_qty", "total_sell_qty",
                    "buy_sell_ratio", "total_stocks", "gainers_count", "losers_count",
                    "high_volume_stocks"]:
            assert key in result, f"Missing key: {key}"

    def test_exception_in_nse_urlfetch_returns_none(self):
        with patch("premarket.premarket_report.nse_urlfetch",
                   side_effect=Exception("connection refused")):
            result = PreMarketReport()._fetch_nse_preopen()
        assert result is None
