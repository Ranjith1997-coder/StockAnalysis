"""SensibullAdapter: enrichment-only must never overwrite Zerodha-owned fields."""
from datetime import datetime
from unittest.mock import MagicMock

from common.Stock import Stock
from lib.fno.sensibull_adapter import SensibullAdapter


def _snapshot(ltp=555.0, oi=99999, oi_change=-1000, future_price=24150.0):
    leg = {
        "last_price": ltp, "oi": oi, "volume": 1, "oi_change_quantity": oi_change,
        "best_buy_price": ltp - 1, "best_sell_price": ltp + 1,
    }
    return {
        "chain": {"24000": {
            "call": dict(leg), "put": dict(leg),
            "greeks": {"call_delta": 0.52, "gamma": 0.001, "theta": -10.0, "vega": 5.0, "iv": 0.14},
            "iv_change": 0.01,
        }},
        "future_price": future_price,
        "atm_iv": 0.14, "atm_iv_percentile": 40.0, "atm_ivp_type": "x", "max_pain_strike": 24000,
    }


def _zerodha_seeded_index():
    idx = Stock("Nifty 50", "NIFTY", is_index=True)
    idx.update_zerodha_data({"last_price": 24010.0})
    ts = datetime(2026, 11, 20, 10, 0, 0)
    for side in ("CE", "PE"):
        idx.update_option_tick(24000.0, side, {"last_price": 100.0, "oi": 1000, "exchange_timestamp": ts})
        idx.update_option_tick(24000.0, side, {"last_price": 101.0, "oi": 1100, "exchange_timestamp": ts})
    idx.recompute_options_aggregate(spot_price=24010.0)
    return idx


class TestEnrichmentOnly:
    def test_does_not_overwrite_ltp_oi_prev_oi_timestamp(self):
        idx = _zerodha_seeded_index()
        before = {s: dict(idx.options_live[24000.0][s]) for s in ("CE", "PE")}
        engine = MagicMock()
        SensibullAdapter().apply(idx, _snapshot(), engine, enrichment_only=True)
        for side in ("CE", "PE"):
            e = idx.options_live[24000.0][side]
            for k in ("ltp", "oi", "prev_oi", "timestamp", "open_oi", "buy_qty", "sell_qty"):
                assert e[k] == before[side][k], (side, k)
            assert "bid" not in e  # Sensibull prices never written in enrichment mode
            assert e["iv"] == 0.14
        assert idx.options_live[24000.0]["PE"]["delta"] == -0.48
        engine.on_aggregate_updated.assert_not_called()

    def test_does_not_recompute_aggregate_with_futures_spot(self):
        idx = _zerodha_seeded_index()
        agg_before = dict(idx.options_aggregate)
        SensibullAdapter().apply(idx, _snapshot(future_price=99999.0), None, enrichment_only=True)
        agg = idx.options_aggregate
        assert agg["last_updated"] == agg_before["last_updated"]
        assert agg["atm_strike"] == agg_before["atm_strike"]
        assert agg["total_ce_oi"] == agg_before["total_ce_oi"]
        # enrichment fields patched
        assert agg["future_price"] == 99999.0
        assert agg["atm_iv"] == 0.14
        assert agg["max_pain_strike"] == 24000


class TestFullWriter:
    def test_bid_ask_prices_not_in_qty_fields(self):
        idx = Stock("Nifty 50", "NIFTY", is_index=True)
        SensibullAdapter().apply(idx, _snapshot(ltp=200.0), None, enrichment_only=False)
        e = idx.options_live[24000.0]["CE"]
        assert e["bid"] == 199.0
        assert e["ask"] == 201.0
        assert e["buy_qty"] == 0 and e["sell_qty"] == 0
        assert e["timestamp"] is not None

    def test_open_oi_from_oi_change_quantity(self):
        idx = Stock("Nifty 50", "NIFTY", is_index=True)
        SensibullAdapter().apply(idx, _snapshot(oi=5000, oi_change=1500), None, enrichment_only=False)
        assert idx.options_live[24000.0]["CE"]["open_oi"] == 3500
        assert idx.options_aggregate["net_ce_oi_change"] == 1500

    def test_uses_index_spot_not_futures(self):
        idx = Stock("Nifty 50", "NIFTY", is_index=True)
        idx.update_zerodha_data({"last_price": 24010.0})
        engine = MagicMock()
        SensibullAdapter().apply(idx, _snapshot(future_price=24300.0), engine, enrichment_only=False)
        engine.on_aggregate_updated.assert_called_once_with(idx, 24010.0)

    def test_falls_back_to_futures_without_index_tick(self):
        idx = Stock("Nifty 50", "NIFTY", is_index=True)
        engine = MagicMock()
        SensibullAdapter().apply(idx, _snapshot(future_price=24300.0), engine, enrichment_only=False)
        engine.on_aggregate_updated.assert_called_once_with(idx, 24300.0)
