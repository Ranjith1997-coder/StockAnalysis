"""market-data: registration (strike gap, expiry), Sensibull mode selection, atomic snapshot publish."""
import json
from datetime import date
from unittest.mock import MagicMock

import pandas as pd

import common.shared as shared
from common.Stock import Stock
from common.token_registry import TokenRegistry
from services.market_data import main as md_main
from services.market_data.snapshot_publisher import SnapshotPublisher


def _options_df():
    rows = []
    tok = 1
    for exp, strikes in ((date(2026, 11, 25), [52200, 52300, 52400, 52500]),
                         (date(2026, 12, 2), [52250, 52300])):   # finer far expiry ignored
        for k in strikes:
            for side in ("CE", "PE"):
                rows.append({"instrument_token": tok, "tradingsymbol": f"BN{k}{side}", "expiry": exp,
                             "strike": float(k), "instrument_type": side, "name": "BANKNIFTY"})
                tok += 1
    return pd.DataFrame(rows)


def _futures_df():
    return pd.DataFrame([{"instrument_token": 999, "tradingsymbol": "BNFUT", "expiry": date(2026, 11, 25),
                          "instrument_type": "FUT", "name": "BANKNIFTY"}])


class TestRegisterOneSymbol:
    def test_sets_strike_gap_and_expiry(self, monkeypatch):
        monkeypatch.setattr(shared.app_ctx, "stockExpires", ["x"], raising=False)
        reg = TokenRegistry()
        idx = Stock("Nifty Bank", "BANKNIFTY", is_index=True)
        md_main._register_one_symbol(reg, idx, _options_df(), _futures_df(), md_main.TokenType.INDEX)
        assert reg.get_strike_gap("BANKNIFTY") == 100.0
        assert idx.options_aggregate["expiry"] == "2026-11-25"
        assert reg.round_to_strike(52340.0, "BANKNIFTY") == 52300.0


class TestSensibullMode:
    def test_enrichment_only_unless_sensibull_sole_source(self):
        assert md_main._sensibull_enrichment_only("zerodha") is True
        assert md_main._sensibull_enrichment_only("both") is True
        assert md_main._sensibull_enrichment_only("") is True
        assert md_main._sensibull_enrichment_only("sensibull") is False
        assert md_main._sensibull_enrichment_only("SENSIBULL") is False


def _index_with_options():
    idx = Stock("Nifty 50", "NIFTY", is_index=True)
    idx.update_option_tick(24000.0, "CE", {"last_price": 100.0, "oi": 1000})
    idx.update_option_tick(24000.0, "PE", {"last_price": 90.0, "oi": 1200})
    idx._tick_store.set_options_expiry("2026-11-25")
    idx.recompute_options_aggregate(spot_price=24000.0)
    return idx


class TestSnapshotPublisherAtomic:
    def test_options_live_written_in_transaction(self):
        redis = MagicMock()
        pipe = MagicMock()
        redis.pipeline.return_value = pipe
        pub = SnapshotPublisher(redis, [], [_index_with_options()])
        pub._publish_options()
        redis.pipeline.assert_called_once_with(transaction=True)
        pipe.delete.assert_called_once_with("data:options_live:NIFTY")
        key, = pipe.hset.call_args.args
        mapping = pipe.hset.call_args.kwargs["mapping"]
        assert key == "data:options_live:NIFTY"
        assert json.loads(mapping["24000.0_CE"])["open_oi"] == 1000
        pipe.execute.assert_called_once()
        # never a bare (non-transactional) DELETE on the live hash
        redis.delete.assert_not_called()

    def test_agg_publishes_expiry_and_window(self):
        redis = MagicMock()
        pub = SnapshotPublisher(redis, [], [_index_with_options()])
        pub._publish_options()
        agg_calls = [c for c in redis.hset.call_args_list if c.args[0] == "data:options_agg:NIFTY"]
        assert agg_calls
        m = agg_calls[0].kwargs["mapping"]
        assert m["expiry"] == "2026-11-25"
        assert "pcr_window_pct" in m and "net_ce_oi_change_tick" in m

    def test_fallback_without_pipeline_never_empties_hash(self):
        class _Store:
            def __init__(self):
                self.h = {"data:options_live:NIFTY": {"23000.0_CE": "{}"}}
                self.empty_seen = False

            def hset(self, key, mapping):
                self.h.setdefault(key, {}).update(mapping)

            def hkeys(self, key):
                return list(self.h.get(key, {}))

            def hdel(self, key, *fields):
                for f in fields:
                    self.h[key].pop(f, None)
                if not self.h[key]:
                    self.empty_seen = True

        store = _Store()
        pub = SnapshotPublisher(store, [], [_index_with_options()])
        pub._replace_hash_atomically("data:options_live:NIFTY", {"24000.0_CE": "{}", "24000.0_PE": "{}"})
        assert set(store.h["data:options_live:NIFTY"]) == {"24000.0_CE", "24000.0_PE"}
        assert store.empty_seen is False
