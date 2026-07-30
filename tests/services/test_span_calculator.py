"""Tests for services/paper_trading/span_calculator.py."""

from datetime import date
from unittest.mock import MagicMock, patch

from services.paper_trading.span_calculator import (
    InstrumentEntry,
    SpanCalculator,
    SpanLegRequest,
    build_instruments_cache,
    deserialize_instruments_cache,
    derive_scrip,
    load_instruments_cache,
    save_instruments_cache,
    serialize_instruments_cache,
)


class TestDeriveScrip:
    def test_weekly_format(self):
        assert derive_scrip("NIFTY2672124050CE", 24050, "CE") == "NIFTY26721"

    def test_monthly_format(self):
        assert derive_scrip("NIFTY26JUL24050CE", 24050, "CE") == "NIFTY26JUL"

    def test_pe_suffix(self):
        assert derive_scrip("BANKNIFTY26JUL57600PE", 57600, "PE") == "BANKNIFTY26JUL"

    def test_direct_suffix_match(self):
        assert derive_scrip("SENSEX2672377200CE", 77200, "CE") == "SENSEX26723"

    def test_regex_fallback_degrades_to_alpha_prefix_on_strike_mismatch(self):
        # strike param mismatches what's actually embedded -- suffix match fails, and
        # the digits alone can't disambiguate the expiry/strike boundary, so the fallback
        # deliberately returns just the alpha prefix ("NIFTY") rather than guess wrong.
        # This is not a valid scrip by itself -- the SPAN API safely rejects it downstream.
        assert derive_scrip("NIFTY2672124050CE", 24000, "CE") == "NIFTY"

    def test_unparseable_returns_original(self):
        assert derive_scrip("GARBAGE", 100, "CE") == "GARBAGE"


class TestBuildInstrumentsCache:
    def _row(self, name="NIFTY", strike=24000.0, instrument_type="CE",
              tradingsymbol="NIFTY2672124000CE", expiry=date(2026, 7, 21),
              lot_size=65, exchange="NFO"):
        return {
            "name": name, "strike": strike, "instrument_type": instrument_type,
            "tradingsymbol": tradingsymbol, "expiry": expiry,
            "lot_size": lot_size, "exchange": exchange,
        }

    def test_filters_to_requested_symbols_and_option_types(self):
        rows = [
            self._row(),
            self._row(name="RELIANCE", tradingsymbol="RELIANCE26JUL2800CE"),
            self._row(name="NIFTY", instrument_type="FUT", tradingsymbol="NIFTY26JULFUT"),
        ]
        cache = build_instruments_cache(rows, symbols=["NIFTY", "BANKNIFTY", "SENSEX"])

        assert "RELIANCE" not in cache
        assert "NIFTY" in cache
        assert "2026-07-21" in cache["NIFTY"]
        assert len(cache["NIFTY"]["2026-07-21"]) == 1

    def test_entry_fields_correct(self):
        rows = [self._row()]
        cache = build_instruments_cache(rows, symbols=["NIFTY"])
        entry = cache["NIFTY"]["2026-07-21"][0]
        assert entry.strike == 24000.0
        assert entry.option_type == "CE"
        assert entry.tradingsymbol == "NIFTY2672124000CE"
        assert entry.lot_size == 65
        assert entry.exchange == "NFO"

    def test_string_expiry_handled(self):
        rows = [self._row(expiry="2026-07-21")]
        cache = build_instruments_cache(rows, symbols=["NIFTY"])
        assert "2026-07-21" in cache["NIFTY"]


class TestInstrumentsCacheSerialization:
    def test_round_trip(self):
        entries = {
            "NIFTY": {
                "2026-07-21": [
                    InstrumentEntry(24000.0, "CE", "NIFTY2672124000CE", 65, "NFO"),
                    InstrumentEntry(24000.0, "PE", "NIFTY2672124000PE", 65, "NFO"),
                ]
            }
        }
        serialized = serialize_instruments_cache(entries)
        restored = deserialize_instruments_cache(serialized)

        assert restored["NIFTY"]["2026-07-21"][0] == entries["NIFTY"]["2026-07-21"][0]
        assert len(restored["NIFTY"]["2026-07-21"]) == 2

    def test_save_and_load_via_redis_proxy(self):
        entries = {
            "NIFTY": {
                "2026-07-21": [InstrumentEntry(24000.0, "CE", "NIFTY2672124000CE", 65, "NFO")]
            }
        }
        redis = MagicMock()
        save_instruments_cache(redis, entries)
        redis.hset.assert_called_once()

        redis.hgetall.return_value = serialize_instruments_cache(entries)["NIFTY"]
        loaded = load_instruments_cache(redis, ["NIFTY"])
        assert loaded["NIFTY"]["2026-07-21"][0].tradingsymbol == "NIFTY2672124000CE"

    def test_load_skips_symbols_with_no_data(self):
        redis = MagicMock()
        redis.hgetall.return_value = {}
        loaded = load_instruments_cache(redis, ["SENSEX"])
        assert loaded == {}


def _nifty_instruments():
    return {
        "NIFTY": {
            "2026-07-21": [
                InstrumentEntry(24000.0, "PE", "NIFTY2672124000PE", 65, "NFO"),
                InstrumentEntry(23900.0, "PE", "NIFTY2672123900PE", 65, "NFO"),
                InstrumentEntry(25000.0, "CE", "NIFTY2672125000CE", 65, "NFO"),
            ]
        }
    }


class TestSpanCalculatorLotSize:
    def test_returns_lot_size_for_known_symbol_expiry(self):
        calc = SpanCalculator(_nifty_instruments())
        assert calc.get_lot_size("NIFTY", "2026-07-21") == 65

    def test_returns_none_for_unknown_expiry(self):
        calc = SpanCalculator(_nifty_instruments())
        assert calc.get_lot_size("NIFTY", "2026-08-04") is None

    def test_returns_none_for_unknown_symbol(self):
        calc = SpanCalculator(_nifty_instruments())
        assert calc.get_lot_size("SENSEX", "2026-07-21") is None


class TestSpanCalculatorScrip:
    def test_returns_scrip_for_known_symbol_expiry(self):
        calc = SpanCalculator(_nifty_instruments())
        assert calc.get_scrip("NIFTY", "2026-07-21") == "NIFTY26721"

    def test_returns_none_for_unknown_symbol_expiry(self):
        calc = SpanCalculator(_nifty_instruments())
        assert calc.get_scrip("SENSEX", "2026-07-21") is None


class TestSpanCalculator:
    def test_returns_none_for_empty_legs(self):
        calc = SpanCalculator(_nifty_instruments())
        assert calc.calculate_margin([]) is None

    def test_returns_none_when_leg_cannot_be_resolved(self):
        calc = SpanCalculator(_nifty_instruments())
        leg = SpanLegRequest(symbol="NIFTY", expiry="2026-07-21", strike=99999.0,
                              option_type="PE", qty=65, trade="sell")
        assert calc.calculate_margin([leg]) is None

    @patch("services.paper_trading.span_calculator.requests.post")
    def test_successful_call_returns_total_total(self, mock_post):
        mock_post.return_value = MagicMock(json=lambda: {
            "last": {"total": 100.0},
            "total": {"span": 100.0, "exposure": 20.0, "netoptionvalue": 5.0, "spread": 0, "total": 125.0},
        })
        calc = SpanCalculator(_nifty_instruments())
        leg = SpanLegRequest(symbol="NIFTY", expiry="2026-07-21", strike=24000.0,
                              option_type="PE", qty=65, trade="sell")
        margin = calc.calculate_margin([leg])
        assert margin == 125.0
        mock_post.assert_called_once()

    @patch("services.paper_trading.span_calculator.requests.post")
    def test_empty_list_response_is_invalid_scrip(self, mock_post):
        mock_post.return_value = MagicMock(json=lambda: {"last": [], "total": []})
        calc = SpanCalculator(_nifty_instruments())
        leg = SpanLegRequest(symbol="NIFTY", expiry="2026-07-21", strike=24000.0,
                              option_type="PE", qty=65, trade="sell")
        assert calc.calculate_margin([leg]) is None

    @patch("services.paper_trading.span_calculator.requests.post")
    def test_api_exception_returns_none(self, mock_post):
        mock_post.side_effect = Exception("connection refused")
        calc = SpanCalculator(_nifty_instruments())
        leg = SpanLegRequest(symbol="NIFTY", expiry="2026-07-21", strike=24000.0,
                              option_type="PE", qty=65, trade="sell")
        assert calc.calculate_margin([leg]) is None

    @patch("services.paper_trading.span_calculator.requests.post")
    def test_second_call_uses_cache_not_new_request(self, mock_post):
        mock_post.return_value = MagicMock(json=lambda: {
            "last": {}, "total": {"total": 125.0},
        })
        calc = SpanCalculator(_nifty_instruments())
        leg = SpanLegRequest(symbol="NIFTY", expiry="2026-07-21", strike=24000.0,
                              option_type="PE", qty=65, trade="sell")
        first = calc.calculate_margin([leg])
        second = calc.calculate_margin([leg])
        assert first == second == 125.0
        mock_post.assert_called_once()

    @patch("services.paper_trading.span_calculator.requests.post")
    def test_multi_leg_builds_repeated_form_fields(self, mock_post):
        mock_post.return_value = MagicMock(json=lambda: {
            "last": {}, "total": {"total": 68578.86},
        })
        calc = SpanCalculator(_nifty_instruments())
        legs = [
            SpanLegRequest("NIFTY", "2026-07-21", 24000.0, "PE", 65, "sell"),
            SpanLegRequest("NIFTY", "2026-07-21", 23900.0, "PE", 65, "buy"),
            SpanLegRequest("NIFTY", "2026-07-21", 25000.0, "CE", 65, "sell"),
        ]
        margin = calc.calculate_margin(legs)
        assert margin == 68578.86

        _, kwargs = mock_post.call_args
        form_data = dict(kwargs["data"]) if isinstance(kwargs["data"], dict) else kwargs["data"]
        scrip_values = [v for k, v in form_data if k == "scrip[]"]
        assert scrip_values == ["NIFTY26721", "NIFTY26721", "NIFTY26721"]
