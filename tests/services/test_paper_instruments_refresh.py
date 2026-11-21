"""Instruments cache TTL / refresh (span_calculator + models config helpers)."""

from unittest.mock import MagicMock

from services.paper_trading import span_calculator as sc
from services.paper_trading.models import (
    INSTRUMENTS_CACHE_TTL_SECONDS,
    INSTRUMENTS_FETCHED_AT_KEY,
    instruments_key,
    parse_config_value,
)
from services.paper_trading.span_calculator import InstrumentEntry, SpanCalculator


def _cache(expiry="2026-07-21", lot=65):
    return {"NIFTY": {expiry: [InstrumentEntry(24000.0, "PE", "NIFTY2672124000PE", lot, "NFO")]}}


class TestSaveInstrumentsCacheTtl:
    def test_applies_ttl_and_fetched_at(self):
        redis = MagicMock()
        sc.save_instruments_cache(redis, _cache())
        redis.delete.assert_called_once_with(instruments_key("NIFTY"))
        redis.expire.assert_called_once_with(instruments_key("NIFTY"), INSTRUMENTS_CACHE_TTL_SECONDS)
        assert redis.set_with_ttl.call_args.args[0] == INSTRUMENTS_FETCHED_AT_KEY

    def test_fetched_at_defaults_to_zero(self):
        redis = MagicMock()
        redis.get.return_value = None
        assert sc.load_instruments_fetched_at(redis) == 0.0


class TestSpanCalculatorRefresh:
    def test_missing_expiry_triggers_refresh(self):
        refresher = MagicMock(return_value=_cache(expiry="2026-07-28", lot=75))
        span = SpanCalculator(_cache(), refresher=refresher)
        assert span.get_lot_size("NIFTY", "2026-07-28") == 75
        refresher.assert_called_once()

    def test_stale_cache_refreshes_lot_size(self):
        refresher = MagicMock(return_value=_cache(lot=75))
        span = SpanCalculator(_cache(lot=65), refresher=refresher, loaded_at=0.0)
        assert span.get_lot_size("NIFTY", "2026-07-21") == 75

    def test_fresh_cache_does_not_refresh(self):
        refresher = MagicMock()
        span = SpanCalculator(_cache(), refresher=refresher)
        assert span.get_lot_size("NIFTY", "2026-07-21") == 65
        refresher.assert_not_called()

    def test_refresh_attempts_rate_limited(self):
        refresher = MagicMock(return_value=None)   # Zerodha down
        span = SpanCalculator(_cache(), refresher=refresher)
        assert span.get_lot_size("NIFTY", "2026-07-28") is None
        assert span.get_lot_size("NIFTY", "2026-07-28") is None
        refresher.assert_called_once()

    def test_no_refresher_keeps_old_behaviour(self):
        span = SpanCalculator(_cache(), loaded_at=0.0)
        assert span.get_lot_size("NIFTY", "2026-07-21") == 65


class TestParseConfigValue:
    def test_valid_values(self):
        assert parse_config_value("max_positions", "5") == 5
        assert parse_config_value("risk_scale", "0.5") == 0.5

    def test_out_of_range_or_unknown(self):
        assert parse_config_value("max_positions", "0") is None
        assert parse_config_value("stop_loss_multiple", "abc") is None
        assert parse_config_value("slippage_bps", "10") is None
