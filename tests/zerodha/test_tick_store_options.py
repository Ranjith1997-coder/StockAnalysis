"""TickStore option-tick correctness: bid/ask, session-open OI, quote-mode OI, enrichment merge."""
from datetime import date, datetime

import pytest

from lib.zerodha.tick_store import TickStore


def _full_tick(ltp=100.0, oi=1000, bid=99.5, ask=100.5, ts=None):
    return {
        "last_price": ltp,
        "oi": oi,
        "volume_traded": 10,
        "total_buy_quantity": 500,
        "total_sell_quantity": 700,
        "exchange_timestamp": ts or datetime(2026, 11, 20, 10, 0, 0),
        "depth": {
            "buy": [{"price": bid, "quantity": 75, "orders": 1}] + [{"price": 0, "quantity": 0, "orders": 0}] * 4,
            "sell": [{"price": ask, "quantity": 50, "orders": 1}] + [{"price": 0, "quantity": 0, "orders": 0}] * 4,
        },
    }


def _quote_tick(ltp=10.0):
    # MODE_QUOTE (44-byte) packets carry no OI / exchange_timestamp / depth
    return {"last_price": ltp, "volume_traded": 5, "total_buy_quantity": 1, "total_sell_quantity": 2}


class TestBidAsk:
    def test_bid_ask_from_depth(self):
        ts = TickStore()
        ts.update_option_tick(24000.0, "CE", _full_tick(bid=99.5, ask=100.5))
        e = ts.options_live[24000.0]["CE"]
        assert e["bid"] == 99.5
        assert e["ask"] == 100.5
        # quantities stay quantities
        assert e["buy_qty"] == 500
        assert e["sell_qty"] == 700

    def test_zero_depth_price_not_written(self):
        ts = TickStore()
        ts.update_option_tick(24000.0, "CE", _full_tick(bid=0, ask=0))
        e = ts.options_live[24000.0]["CE"]
        assert "bid" not in e and "ask" not in e

    def test_explicit_bid_ask_keys(self):
        ts = TickStore()
        ts.update_option_tick(24000.0, "PE", {"last_price": 50, "oi": 10, "bid": 49.0, "ask": 51.0})
        e = ts.options_live[24000.0]["PE"]
        assert (e["bid"], e["ask"]) == (49.0, 51.0)


class TestSessionOpenOI:
    def test_net_change_is_since_open_not_tick_to_tick(self):
        ts = TickStore()
        ts.update_option_tick(24000.0, "CE", _full_tick(oi=1000))
        ts.update_option_tick(24000.0, "CE", _full_tick(oi=1200))
        ts.update_option_tick(24000.0, "CE", _full_tick(oi=1500))
        ts.update_option_tick(24000.0, "PE", _full_tick(oi=2000))
        ts.update_option_tick(24000.0, "PE", _full_tick(oi=1800))
        ts.recompute_options_aggregate(spot_price=24000.0)
        agg = ts.options_aggregate
        assert ts.options_live[24000.0]["CE"]["open_oi"] == 1000
        assert agg["net_ce_oi_change"] == 500          # 1500 - 1000 (session)
        assert agg["net_ce_oi_change_tick"] == 300     # 1500 - 1200 (last tick)
        assert agg["net_pe_oi_change"] == -200
        assert agg["net_pe_oi_change_tick"] == -200

    def test_explicit_open_oi_baseline_wins(self):
        ts = TickStore()
        ts.update_option_tick(24000.0, "CE", {"last_price": 1, "oi": 1500, "open_oi": 900})
        ts.recompute_options_aggregate(spot_price=24000.0)
        assert ts.options_aggregate["net_ce_oi_change"] == 600

    def test_zero_oi_does_not_set_baseline(self):
        ts = TickStore()
        ts.update_option_tick(24000.0, "CE", _full_tick(oi=0))
        assert ts.options_live[24000.0]["CE"].get("open_oi") is None
        ts.update_option_tick(24000.0, "CE", _full_tick(oi=700))
        assert ts.options_live[24000.0]["CE"]["open_oi"] == 700

    def test_new_trading_day_resets_baseline(self):
        ts = TickStore()
        ts.update_option_tick(24000.0, "CE", _full_tick(oi=1000))
        ts._oi_session_date = date(2000, 1, 1)  # pretend yesterday
        ts.update_option_tick(24000.0, "CE", _full_tick(oi=3000))
        assert ts.options_live[24000.0]["CE"]["open_oi"] == 3000


class TestQuoteModeOI:
    def test_quote_tick_keeps_last_oi_and_flags(self):
        ts = TickStore()
        ts.update_option_tick(24000.0, "CE", _full_tick(oi=1000))
        ts.update_option_tick(24000.0, "CE", _quote_tick(ltp=12.0))
        e = ts.options_live[24000.0]["CE"]
        assert e["oi"] == 1000
        assert e["prev_oi"] == 1000
        assert e["has_oi"] is False
        assert e["ltp"] == 12.0
        assert e["timestamp"] is not None  # never None (staleness checks)

    def test_peripheral_strikes_excluded_and_window_labelled(self):
        ts = TickStore()
        # ATM ±1% full-mode legs with OI
        ts.update_option_tick(24000.0, "CE", _full_tick(oi=1000))
        ts.update_option_tick(24000.0, "PE", _full_tick(oi=1500))
        ts.update_option_tick(24480.0, "CE", _full_tick(oi=500))   # 2% away
        # peripheral 4% away — quote mode, never had OI
        ts.update_option_tick(24960.0, "CE", _quote_tick())
        ts.update_option_tick(23040.0, "PE", _quote_tick())
        ts.recompute_options_aggregate(spot_price=24000.0)
        agg = ts.options_aggregate
        assert agg["total_ce_oi"] == 1500
        assert agg["total_pe_oi"] == 1500
        assert agg["live_pcr"] == pytest.approx(1.0)
        assert agg["pcr_strikes"] == 2
        assert agg["pcr_window_pct"] == pytest.approx(2.0)


class TestEnrichmentMerge:
    def test_merge_only_writes_greeks(self):
        ts = TickStore()
        ts.update_option_tick(24000.0, "CE", _full_tick(ltp=100.0, oi=1000))
        before = dict(ts.options_live[24000.0]["CE"])
        ts.update_option_tick(24000.0, "CE", {
            "delta": 0.5, "iv": 0.15,
            "last_price": 999, "ltp": 999, "oi": 1, "prev_oi": 1, "timestamp": None,
        }, merge=True)
        e = ts.options_live[24000.0]["CE"]
        assert e["delta"] == 0.5 and e["iv"] == 0.15
        for k in ("ltp", "oi", "prev_oi", "timestamp", "open_oi"):
            assert e[k] == before[k]

    def test_merge_skips_unknown_strike(self):
        ts = TickStore()
        ts.update_option_tick(25000.0, "CE", {"delta": 0.1}, merge=True)
        assert 25000.0 not in ts.options_live

    def test_set_expiry(self):
        ts = TickStore()
        ts.set_options_expiry("2026-11-25")
        assert ts.options_aggregate["expiry"] == "2026-11-25"
