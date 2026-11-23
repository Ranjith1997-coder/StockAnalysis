"""HTTP-path tests for sensibull_fetcher (audit 2026-11-23).

No real network: ``requests.get/post`` are replaced with scripted fakes, cookies
and the rate limiter are stubbed, and the 429 backoff is skipped. These cover the
parse/fallback logic that feeds every Sensibull analyser, plus the cycle runners
and their per-symbol failure isolation.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest
import requests

import services.data_gateway.sensibull_fetcher as sf


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(sf, "_get_sensibull_cookies", lambda: {"access_token": "t"})
    monkeypatch.setattr(sf, "_get_sensibull_headers", lambda: {"x-api": "y"})
    limiter = MagicMock()
    limiter.acquire.return_value = None
    monkeypatch.setattr(sf, "get_sensibull_limiter", lambda: limiter)
    # 429 backoff sleeps inside the shared decorator
    monkeypatch.setattr("services.common.rate_limiter.time.sleep", lambda _s: None)


def _response(payload, status: int = 200, headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = payload
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    return resp


def _http_error(status=500, headers=None) -> MagicMock:
    return _response({}, status=status, headers=headers)


def _stock_info_payload() -> dict:
    return {
        "success": True,
        "payload": {
            "underlying_info": {"symbol": "NIFTY", "lot_size": 50},
            "stats": {
                "per_expiry_map": {
                    "2024-01-25": {"atm_strike": 21_000, "max_pain_strike": 21_000, "pcr": 1.1}
                },
                "underlying_base_stats": {"total_pcr": 1.05, "volume_spike": 1.2},
            },
            "nse_stats": {"advances": 30},
        },
    }


def _oi_payload(enabled="2024-01-25") -> dict:
    return {
        "success": True,
        "payload": {
            "input": {
                "date": "2024-01-24",
                "expiries": {
                    "2024-01-25": {"is_enabled": enabled == "2024-01-25"},
                    "2024-02-29": {"is_enabled": enabled == "2024-02-29"},
                },
            },
            "prev_ltp": 20_900.0,
            "current_ltp": None,
            "date_ltp": 21_000.0,
            "atm_strike": 21_000,
            "total_call_oi": 500_000,
            "total_put_oi": 600_000,
            "total_call_oi_change": 10_000,
            "total_put_oi_change": 20_000,
            "pcr": 1.2,
            "per_strike_data": {
                "21000": {"call_oi": 100_000, "put_oi": 150_000,
                          "prev_call_oi": 90_000, "prev_put_oi": 140_000},
            },
            "strike_list": [20_900, 21_000, 21_100],
            "min_strike": 20_900,
            "max_strike": 21_100,
            "underlying_token": 256265,
        },
    }


def _iv_payload() -> dict:
    return {
        "success": True,
        "payload": {
            "iv_ohlc_data": {
                "2024-01-03": {"iv": 14.0, "close": 21_050.0},
                "2024-01-02": {"iv": 13.5, "close": 21_000.0},
                "2024-01-04": {"iv": None, "close": 21_100.0},   # skipped
            }
        },
    }


class TestFetchSensibullData:
    def test_stock_info_success(self, monkeypatch):
        get = MagicMock(return_value=_response(_stock_info_payload()))
        monkeypatch.setattr(sf.requests, "get", get)

        result = sf.fetch_sensibull_data("NIFTY")

        assert result["per_expiry_map"]["2024-01-25"]["pcr"] == 1.1
        assert result["stats"]["underlying_base_stats"]["total_pcr"] == 1.05
        assert result["nse_stats"] == {"advances": 30}
        assert "tradingsymbol=NIFTY" in get.call_args.args[0]

    def test_404_falls_back_to_oi_chain_and_iv_chart(self, monkeypatch):
        monkeypatch.setattr(sf.requests, "get", MagicMock(side_effect=[
            _http_error(404),
            _response(_iv_payload()),
        ]))
        monkeypatch.setattr(sf.requests, "post", MagicMock(return_value=_response(_oi_payload())))

        result = sf.fetch_sensibull_data("NIFTY")

        assert result is not None
        nearest = result["per_expiry_map"]["2024-01-25"]
        assert nearest["atm_iv"] == 14.0          # newest non-null IV, not the blank day
        assert nearest["atm_iv_percentile"] == 100.0
        assert nearest["atm_ivp_type"] == "VERY_HIGH"
        assert nearest["max_pain_strike"] == 21_000.0
        assert nearest["pcr"] == 1.2
        # other expiries are stubs
        assert result["per_expiry_map"]["2024-02-29"]["atm_iv"] is None

    def test_fallback_without_expiries_returns_none(self, monkeypatch):
        empty = _oi_payload()
        empty["payload"]["input"]["expiries"] = {}
        monkeypatch.setattr(sf.requests, "get", MagicMock(return_value=_http_error(404)))
        monkeypatch.setattr(sf.requests, "post", MagicMock(return_value=_response(empty)))

        assert sf.fetch_sensibull_data("NIFTY") is None

    def test_both_paths_fail_returns_none(self, monkeypatch):
        monkeypatch.setattr(sf.requests, "get", MagicMock(side_effect=requests.exceptions.Timeout()))
        monkeypatch.setattr(sf.requests, "post", MagicMock(side_effect=requests.exceptions.Timeout()))

        assert sf.fetch_sensibull_data("NIFTY") is None

    def test_429_is_retried_then_succeeds(self, monkeypatch):
        get = MagicMock(side_effect=[
            _http_error(429, headers={"Retry-After": "0"}),
            _response(_stock_info_payload()),
        ])
        monkeypatch.setattr(sf.requests, "get", get)

        result = sf.fetch_sensibull_data("NIFTY")

        assert result["per_expiry_map"]
        assert get.call_count == 2


class TestFetchSensibullOiChain:
    def test_parses_payload_and_marks_enabled_expiry(self, monkeypatch):
        monkeypatch.setattr(sf.requests, "post", MagicMock(return_value=_response(_oi_payload())))

        chain = sf.fetch_sensibull_oi_chain("NIFTY", {"2024-01-25": {}, "2024-02-29": {}})

        assert chain["expiry"] == "2024-01-25"
        assert chain["current_ltp"] == 21_000.0        # falls back to date_ltp
        assert chain["prev_ltp"] == 20_900.0
        assert chain["per_strike_data"]["21000"]["call_oi"] == 100_000
        assert chain["strike_list"] == [20_900, 21_000, 21_100]
        assert pd.Timestamp(chain["timestamp"])

    def test_no_expiries_returns_none_without_http(self, monkeypatch):
        post = MagicMock()
        monkeypatch.setattr(sf.requests, "post", post)
        assert sf.fetch_sensibull_oi_chain("NIFTY", {}) is None
        post.assert_not_called()

    def test_unsuccessful_payload_returns_none(self, monkeypatch):
        monkeypatch.setattr(sf.requests, "post", MagicMock(
            return_value=_response({"success": False})))
        assert sf.fetch_sensibull_oi_chain("NIFTY", {"2024-01-25": {}}) is None

    def test_timeout_returns_none(self, monkeypatch):
        monkeypatch.setattr(sf.requests, "post", MagicMock(side_effect=requests.exceptions.Timeout()))
        assert sf.fetch_sensibull_oi_chain("NIFTY", {"2024-01-25": {}}) is None


class TestFetchIvChart:
    def test_parses_sorted_rows_and_skips_missing_iv(self, monkeypatch):
        monkeypatch.setattr(sf.requests, "get", MagicMock(return_value=_response(_iv_payload())))

        frame = sf.fetch_iv_chart("NIFTY")

        assert frame["date"].tolist() == ["2024-01-02", "2024-01-03"]
        assert frame["iv_close"].tolist() == [13.5, 14.0]
        assert frame["price_close"].tolist() == [21_000.0, 21_050.0]

    def test_unsuccessful_payload_returns_none(self, monkeypatch):
        monkeypatch.setattr(sf.requests, "get", MagicMock(return_value=_response({"success": False})))
        assert sf.fetch_iv_chart("NIFTY") is None

    def test_empty_chart_returns_none(self, monkeypatch):
        monkeypatch.setattr(sf.requests, "get", MagicMock(
            return_value=_response({"success": True, "payload": {"iv_ohlc_data": {}}})))
        assert sf.fetch_iv_chart("NIFTY") is None

    def test_all_entries_missing_iv_returns_none(self, monkeypatch):
        payload = {"success": True, "payload": {"iv_ohlc_data": {"2024-01-02": {"close": 1.0}}}}
        monkeypatch.setattr(sf.requests, "get", MagicMock(return_value=_response(payload)))
        assert sf.fetch_iv_chart("NIFTY") is None


class TestFetchOiHistory:
    def _payload(self) -> dict:
        return {
            "success": True,
            "payload": {
                "chart_data": {
                    "2024-01-03T00:00:00": {
                        "spot": 21_050.0,
                        "oi_options": {"call_oi": 500_000, "put_oi": 600_000},
                        "oi_futures": {"futures_oi": 700_000},
                        "oi_change_options": {"call_oi_change": 10_000, "put_oi_change": 20_000},
                        "oi_change_futures": {"future_oi_change": 5_000},
                        "pcr_data": {"pcr": 1.2},
                        "max_pain_data": {"max_pain": 21_000},
                    },
                    "2024-01-02T00:00:00": {
                        "spot": 21_000.0,
                        "oi_options": {"call_oi": 490_000, "put_oi": 590_000},
                        "pcr_data": {"pcr": 1.2},
                        "max_pain_data": {"max_pain": 20_900},
                    },
                    "2024-01-04T00:00:00": {          # missing OI → dropped
                        "spot": 21_100.0,
                        "oi_options": {},
                    },
                }
            },
        }

    def test_parses_sorts_and_drops_rows_without_oi(self, monkeypatch):
        monkeypatch.setattr(sf.requests, "post", MagicMock(return_value=_response(self._payload())))

        frame = sf.fetch_oi_history("NIFTY", {"2024-01-25": {}, "2024-02-29": {}})

        assert frame["date"].tolist() == ["2024-01-02", "2024-01-03"]
        assert frame["call_oi"].tolist() == [490_000.0, 500_000.0]
        assert frame["max_pain"].tolist() == [20_900, 21_000]
        assert frame["future_oi_change"].iloc[-1] == 5_000.0

    def test_no_expiries_returns_none(self, monkeypatch):
        post = MagicMock()
        monkeypatch.setattr(sf.requests, "post", post)
        assert sf.fetch_oi_history("NIFTY", {}) is None
        post.assert_not_called()

    def test_missing_chart_data_returns_none(self, monkeypatch):
        payload = {"success": True, "payload": {"chart_data": {}}}
        monkeypatch.setattr(sf.requests, "post", MagicMock(return_value=_response(payload)))
        assert sf.fetch_oi_history("NIFTY", {"2024-01-25": {}}) is None


class TestFetchAndPublishCycle:
    def test_publishes_each_symbol_and_isolates_failures(self, monkeypatch, fake_redis):
        def fake_data(symbol, mode="intraday"):
            if symbol == "BROKEN":
                raise RuntimeError("boom")
            return {"stats": {}, "per_expiry_map": {"2024-01-25": {"atm_strike": 21_000}}}

        monkeypatch.setattr(sf, "fetch_sensibull_data", fake_data)
        monkeypatch.setattr(sf, "fetch_sensibull_oi_chain", lambda *a, **k: None)

        sf.fetch_and_publish_cycle(fake_redis, ["NIFTY", "BROKEN"], ["INDIA_VIX"])

        assert "data:sensibull:NIFTY" in fake_redis._client.keys("*")
        assert "data:sensibull:BROKEN" not in fake_redis._client.keys("*")
        assert "data:sensibull:INDIA_VIX" not in fake_redis._client.keys("*")

    def test_none_insight_counts_as_failure_and_skips_publish(self, monkeypatch, fake_redis):
        monkeypatch.setattr(sf, "fetch_sensibull_data", lambda *a, **k: None)
        sf.fetch_and_publish_cycle(fake_redis, ["NIFTY"], [])
        assert fake_redis._client.keys("data:sensibull:*") == []

    def test_positional_fetches_sources_when_cache_is_empty(self, monkeypatch, fake_redis):
        calls = {"iv": 0, "oi": 0}
        monkeypatch.setattr(sf, "fetch_sensibull_data", lambda *a, **k: {
            "stats": {}, "per_expiry_map": {"2024-01-25": {"atm_strike": 21_000}},
        })
        monkeypatch.setattr(sf, "fetch_sensibull_oi_chain", lambda *a, **k: None)
        monkeypatch.setattr(sf, "fetch_iv_chart", lambda *a, **k: calls.__setitem__("iv", calls["iv"] + 1))
        monkeypatch.setattr(sf, "fetch_oi_history", lambda *a, **k: calls.__setitem__("oi", calls["oi"] + 1))

        sf.fetch_and_publish_cycle(fake_redis, [], ["NIFTY"], mode="positional")

        assert calls == {"iv": 1, "oi": 1}

    def test_positional_skips_sources_when_cached_today(self, monkeypatch, fake_redis):
        today = str(date.today())
        ctx = {
            "current_json": "{}",
            "historical_data_json": "{}",
            "iv_chart_history_json": pd.DataFrame({"date": [today], "iv_close": [12.0]}).to_json(orient="split"),
            "oi_history_json": pd.DataFrame({"date": [today], "max_pain": [21_000]}).to_json(orient="split"),
            "oi_chain_json": "null",
            "oi_chain_history_json": "[]",
        }
        fake_redis.hset("data:sensibull:NIFTY", mapping=ctx)
        calls = {"iv": 0, "oi": 0}
        monkeypatch.setattr(sf, "fetch_sensibull_data", lambda *a, **k: {
            "stats": {}, "per_expiry_map": {"2024-01-25": {"atm_strike": 21_000}},
        })
        monkeypatch.setattr(sf, "fetch_sensibull_oi_chain", lambda *a, **k: None)
        monkeypatch.setattr(sf, "fetch_iv_chart", lambda *a, **k: calls.__setitem__("iv", calls["iv"] + 1))
        monkeypatch.setattr(sf, "fetch_oi_history", lambda *a, **k: calls.__setitem__("oi", calls["oi"] + 1))

        sf.fetch_and_publish_cycle(fake_redis, [], ["NIFTY"], mode="positional")

        assert calls == {"iv": 0, "oi": 0}


class TestParallelCycle:
    def test_returns_counts_and_isolates_failures(self, monkeypatch, fake_redis):
        def fake_data(symbol, mode="intraday"):
            if symbol == "BROKEN":
                raise RuntimeError("boom")
            return {"stats": {}, "per_expiry_map": {"2024-01-25": {"atm_strike": 21_000}}}

        monkeypatch.setattr(sf, "fetch_sensibull_data", fake_data)
        monkeypatch.setattr(sf, "fetch_sensibull_oi_chain", lambda *a, **k: None)

        success, failure = sf.fetch_and_publish_cycle_parallel(
            fake_redis, ["NIFTY", "TCS", "BROKEN"], []
        )

        assert (success, failure) == (2, 1)
        assert sorted(fake_redis._client.keys("data:sensibull:*")) == [
            "data:sensibull:NIFTY", "data:sensibull:TCS",
        ]

    def test_empty_symbol_list(self, fake_redis):
        assert sf.fetch_and_publish_cycle_parallel(fake_redis, [], []) == (0, 0)
