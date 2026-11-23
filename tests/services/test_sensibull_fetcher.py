"""Tests for services/data_gateway/sensibull_fetcher.py (audit 2026-11-23: 7 %).

This module feeds every Sensibull-backed analyser, yet had no tests. These
cover the pure computations and the Redis publish/deserialize round trip; the
HTTP calls themselves are exercised through mocked payloads in
``test_sensibull_fetch.py`` when needed.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from services.data_gateway.sensibull_fetcher import (
    _compute_iv_percentile,
    _compute_max_pain,
    _deserialize_sensibull_ctx,
    build_historical_row,
    publish_to_redis,
)


def _strikes(rows: list[tuple[float, int, int]]) -> dict:
    return {
        str(strike): {"call_oi": call_oi, "put_oi": put_oi}
        for strike, call_oi, put_oi in rows
    }


class TestComputeMaxPain:
    def test_symmetric_oi_picks_the_middle_strike(self):
        data = _strikes([(100, 1_000, 1_000), (110, 1_000, 1_000), (120, 1_000, 1_000)])
        assert _compute_max_pain(data, spot=110.0) == 110.0

    def test_heavy_put_oi_anchors_max_pain_at_the_put_strike(self):
        """Max pain minimises writer payout, so it sits on the big put strike."""
        data = _strikes([(100, 100, 10_000), (110, 100, 100), (120, 100, 100)])
        assert _compute_max_pain(data, spot=110.0) == 100.0

    def test_single_strike_returns_that_strike(self):
        assert _compute_max_pain(_strikes([(105, 10, 10)]), spot=105.0) == 105.0

    @pytest.mark.parametrize("data,spot", [(None, 100.0), ({}, 100.0), ({"100": {}}, None), ({"100": {}}, 0)])
    def test_missing_inputs_return_none(self, data, spot):
        assert _compute_max_pain(data, spot) is None

    def test_malformed_key_returns_none(self):
        assert _compute_max_pain({"abc": {"call_oi": 1, "put_oi": 1}}, 100.0) is None


class TestComputeIvPercentile:
    def _chart(self, values: list[float]) -> pd.DataFrame:
        return pd.DataFrame({
            "iv_close": values,
            "timestamp": pd.date_range("2023-01-01", periods=len(values), freq="D"),
        })

    def test_rising_series_is_very_high(self):
        percentile, kind = _compute_iv_percentile(self._chart([float(v) for v in range(1, 101)]))
        assert percentile == 100.0
        assert kind == "VERY_HIGH"

    def test_falling_series_is_very_low(self):
        percentile, kind = _compute_iv_percentile(
            self._chart([float(v) for v in range(100, 0, -1)])
        )
        assert percentile == 1.0
        assert kind == "VERY_LOW"

    @pytest.mark.parametrize(
        "values,expected",
        [
            ([10.0] * 80 + [30.0] * 19 + [20.0], ("HIGH", 81.0)),      # 81 % <= latest
            ([10.0] * 50 + [30.0] * 49 + [20.0], ("MODERATE", 51.0)),  # 51 % <= latest
        ],
    )
    def test_band_boundaries(self, values, expected):
        kind_expected, pct_expected = expected
        percentile, kind = _compute_iv_percentile(self._chart(values))
        assert kind == kind_expected
        assert percentile == pct_expected

    def test_history_is_truncated_to_one_year(self):
        values = [5.0] * 300 + [50.0] * 10
        percentile, kind = _compute_iv_percentile(self._chart(values))
        assert percentile == 100.0 and kind == "VERY_HIGH"

    @pytest.mark.parametrize("frame", [None, pd.DataFrame()])
    def test_empty_input_returns_none(self, frame):
        assert _compute_iv_percentile(frame) == (None, None)

    def test_missing_column_returns_none(self):
        frame = pd.DataFrame({"other": [1.0, 2.0]})
        assert _compute_iv_percentile(frame) == (None, None)


class TestBuildHistoricalRow:
    def test_flattens_base_stats_and_each_expiry(self):
        payload = {
            "stats": {
                "underlying_base_stats": {
                    "volume_spike": 1.4, "volume_spike_type": "HIGH",
                    "future_oi_change": 2.5, "oi_change_type": "BUILDUP",
                    "total_pcr": 0.85,
                },
                "per_expiry_map": {
                    "2024-01-25": {
                        "future_price": 21_000.0, "future_change_percent": 0.5,
                        "atm_strike": 21_000, "atm_iv": 15.2, "atm_iv_change": 0.3,
                        "atm_iv_percentile": 55.0, "atm_ivp_type": "MODERATE",
                        "max_pain_strike": 21_000, "max_pain_type": "Bullish",
                        "pcr": 1.1, "pcr_type": "Bullish", "lot_size": 50,
                    }
                },
            }
        }
        row = build_historical_row("NIFTY", payload)

        assert isinstance(row["timestamp"], datetime)
        assert row["volume_spike"] == 1.4
        assert row["total_pcr"] == 0.85
        suffix = "20240125"
        assert row[f"future_price_{suffix}"] == 21_000.0
        assert row[f"atm_iv_{suffix}"] == 15.2
        assert row[f"max_pain_type_{suffix}"] == "Bullish"
        assert row[f"pcr_{suffix}"] == 1.1
        assert row[f"lot_size_{suffix}"] == 50

    def test_multiple_expiries_get_separate_suffixes(self):
        payload = {
            "stats": {
                "underlying_base_stats": {},
                "per_expiry_map": {
                    "2024-01-25": {"atm_strike": 21_000},
                    "2024-02-29": {"atm_strike": 21_500},
                },
            }
        }
        row = build_historical_row("NIFTY", payload)
        assert row["atm_strike_20240125"] == 21_000
        assert row["atm_strike_20240229"] == 21_500

    def test_missing_sections_do_not_raise(self):
        row = build_historical_row("NIFTY", {})
        assert row["volume_spike"] is None
        assert not any(key.startswith("atm_strike_") for key in row)


class TestPublishToRedis:
    def _current(self):
        return {
            "stats": {
                "underlying_base_stats": {"total_pcr": 1.05},
                "per_expiry_map": {"2024-01-25": {"atm_strike": 21_000, "future_price": 21_010.0}},
            }
        }

    def test_publishes_all_fields(self, fake_redis):
        publish_to_redis(fake_redis, "NIFTY", self._current(), None, {})

        mapping = fake_redis.hgetall("data:sensibull:NIFTY")
        assert set(mapping) == {
            "last_fetch_time", "current_json", "historical_data_json",
            "oi_chain_json", "oi_chain_history_json",
            "iv_chart_history_json", "oi_history_json",
        }
        assert json.loads(mapping["current_json"])["stats"]["underlying_base_stats"]["total_pcr"] == 1.05
        assert mapping["oi_chain_json"] == "null"
        assert json.loads(mapping["oi_chain_history_json"]) == []

    def test_appends_a_historical_row(self, fake_redis):
        ctx = {"historical_data": pd.DataFrame({
            "timestamp": [datetime.now()], "value": [1.0],
        })}
        publish_to_redis(fake_redis, "NIFTY", self._current(), None, ctx)

        mapping = fake_redis.hgetall("data:sensibull:NIFTY")
        hist = pd.read_json(io.StringIO(mapping["historical_data_json"]), orient="split")
        assert len(hist) == 2
        assert "atm_strike_20240125" in hist.columns

    def test_positional_history_is_capped_at_30_rows(self, fake_redis):
        ctx = {"historical_data": pd.DataFrame({"timestamp": pd.date_range("2020-01-01", periods=40, freq="D")})}
        publish_to_redis(fake_redis, "NIFTY", self._current(), None, ctx, mode="positional")

        mapping = fake_redis.hgetall("data:sensibull:NIFTY")
        hist = pd.read_json(io.StringIO(mapping["historical_data_json"]), orient="split")
        assert len(hist) == 30

    def test_intraday_history_drops_rows_older_than_five_days(self, fake_redis):
        now = datetime.now()
        ctx = {"historical_data": pd.DataFrame({
            "timestamp": [now - timedelta(days=10), now],
            "value": [1, 2],
        })}
        publish_to_redis(fake_redis, "NIFTY", self._current(), None, ctx, mode="intraday")

        mapping = fake_redis.hgetall("data:sensibull:NIFTY")
        hist = pd.read_json(io.StringIO(mapping["historical_data_json"]), orient="split")
        assert len(hist) == 2, "one old row + the new row"

    def test_oi_chain_history_is_capped_at_15_snapshots(self, fake_redis):
        ctx = {"oi_chain_history": [{"i": i} for i in range(15)]}
        chain = {"per_strike_data": {"21000": {"call_oi": 1, "put_oi": 2}}, "current_ltp": 21_000}

        publish_to_redis(fake_redis, "NIFTY", None, chain, ctx)

        mapping = fake_redis.hgetall("data:sensibull:NIFTY")
        history = json.loads(mapping["oi_chain_history_json"])
        assert len(history) == 15
        assert history[-1] == chain
        assert json.loads(mapping["oi_chain_json"]) == chain

    def test_string_history_is_discarded_not_crashed(self, fake_redis):
        """A legacy/partial hash can hold a string; it must not take the cycle down."""
        publish_to_redis(fake_redis, "NIFTY", self._current(), None, {"historical_data": "broken"})

        mapping = fake_redis.hgetall("data:sensibull:NIFTY")
        hist = pd.read_json(io.StringIO(mapping["historical_data_json"]), orient="split")
        assert len(hist) == 1

    def test_iv_chart_and_oi_history_are_published(self, fake_redis):
        iv = pd.DataFrame({"iv_close": [12.0, 13.0]})
        oi = pd.DataFrame({"max_pain": [21_000.0], "spot": [21_050.0]})

        publish_to_redis(fake_redis, "NIFTY", None, None, {}, iv_chart=iv, oi_history=oi)

        mapping = fake_redis.hgetall("data:sensibull:NIFTY")
        assert pd.read_json(io.StringIO(mapping["iv_chart_history_json"]), orient="split")["iv_close"].tolist() == [12.0, 13.0]
        assert pd.read_json(io.StringIO(mapping["oi_history_json"]), orient="split")["max_pain"].tolist() == [21_000.0]

    def test_caller_context_dict_is_updated_in_place(self, fake_redis):
        ctx: dict = {}
        publish_to_redis(fake_redis, "NIFTY", self._current(), None, ctx)
        assert "historical_data" in ctx and len(ctx["historical_data"]) == 1
        publish_to_redis(fake_redis, "NIFTY", self._current(), None, ctx)
        assert len(ctx["historical_data"]) == 2

    def test_empty_payload_publishes_empty_placeholders(self, fake_redis):
        publish_to_redis(fake_redis, "NIFTY", None, None, None)
        mapping = fake_redis.hgetall("data:sensibull:NIFTY")
        assert mapping["current_json"] == "{}"
        assert mapping["last_fetch_time"] == ""


class TestDeserialize:
    def test_round_trip_restores_frames_and_chain(self, fake_redis):
        ctx = {
            "historical_data": pd.DataFrame({"timestamp": [datetime(2024, 1, 2)], "value": [1.5]}),
            "iv_chart_history": pd.DataFrame({"iv_close": [12.5]}),
            "oi_history": pd.DataFrame({"max_pain": [21_000.0]}),
        }
        chain = {"per_strike_data": {"21000": {"call_oi": 1, "put_oi": 2}}}
        publish_to_redis(fake_redis, "NIFTY", {"stats": {}}, chain, ctx, mode="positional")

        raw = fake_redis.hgetall("data:sensibull:NIFTY")
        restored = _deserialize_sensibull_ctx(raw)

        assert restored["current"] == {"stats": {}}
        assert restored["oi_chain"] == chain
        hist = restored["historical_data"]
        assert len(hist) == 2                      # existing row + published row
        assert hist["value"].iloc[0] == 1.5
        assert restored["iv_chart_history"]["iv_close"].tolist() == [12.5]
        assert restored["oi_history"]["max_pain"].tolist() == [21_000.0]
        assert restored["oi_chain_history"][0] == chain

    def test_empty_raw_returns_empty_ctx(self):
        assert _deserialize_sensibull_ctx({}) == {}

    def test_missing_fields_use_defaults(self):
        ctx = _deserialize_sensibull_ctx({"current_json": "{}"})
        assert ctx["current"] == {}
        assert ctx["historical_data"].empty
        assert ctx["oi_chain"] is None
        assert ctx["oi_chain_history"] == []
