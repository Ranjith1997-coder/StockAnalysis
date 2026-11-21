"""ContextBuilder data-path fixes."""
import sys
from unittest.mock import MagicMock, patch

from common.Stock import Stock
from lib.intelligence import narrator
from lib.intelligence.context_builder import ContextBuilder


def _stock():
    s = Stock("Nifty 50", "NIFTY", is_index=True)
    s.sensibull_ctx["current"]["stats"] = {
        "max_pain": 99999,   # legacy/wrong field — must be ignored
        "per_expiry_map": {
            "2099-01-08": {"max_pain_strike": 24100, "atm_iv_percentile": 40},
            "2099-01-29": {"max_pain_strike": 24500, "atm_iv_percentile": 60},
        },
    }
    s.sensibull_ctx["oi_chain"] = {"per_strike_data": {
        "24000.0": {"call_oi": 100, "put_oi": 900},
        "24100.0": {"call_oi": 500, "put_oi": 300},
        "24200.0": {"call_oi": 800, "put_oi": 50},
    }}
    return s


def test_max_pain_from_nearest_expiry():
    assert ContextBuilder()._get_max_pain(_stock()) == 24100


def test_max_pain_skips_expired_expiry():
    s = _stock()
    s.sensibull_ctx["current"]["stats"]["per_expiry_map"]["2000-01-01"] = {"max_pain_strike": 1}
    assert ContextBuilder()._get_max_pain(s) == 24100


def test_max_pain_missing_returns_none():
    s = _stock()
    s.sensibull_ctx["current"]["stats"] = {}
    assert ContextBuilder()._get_max_pain(s) is None


def test_top_oi_strikes_uses_call_put_oi_keys():
    cb = ContextBuilder()
    s = _stock()
    assert cb._get_top_oi_strikes(s, "ce", n=2) == [(24200.0, 800), (24100.0, 500)]
    assert cb._get_top_oi_strikes(s, "pe", n=2) == [(24000.0, 900), (24100.0, 300)]


def test_refresh_from_redis_noop_without_orchestrator():
    with patch.dict(sys.modules, {"services.orchestrator.main": None}):
        ContextBuilder()._refresh_from_redis(_stock())   # must not raise


def test_refresh_from_redis_uses_orchestrator_proxy():
    fake_main = MagicMock()
    fake_main.redis_proxy = MagicMock()
    with patch.dict(sys.modules, {"services.orchestrator.main": fake_main}), \
         patch("services.common.stock_loader.load_tick_from_redis") as load:
        s = _stock()
        ContextBuilder()._refresh_from_redis(s)
        load.assert_called_once_with(fake_main.redis_proxy, s)


def test_narrator_treats_sensex_as_index():
    assert "SENSEX" in narrator._INDEX_SYMBOLS
