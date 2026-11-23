"""Tests for services/market_data/chain_recorder.py (intraday capture)."""
import threading
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import services.market_data.chain_recorder as cr


class FakeTickStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._zerodha_data = {"last_price": 20_000.0}
        self.options_aggregate = {
            "expiry": "2026-11-25", "atm_strike": 20_000.0,
            "atm_straddle_premium": 180.0, "live_pcr": 0.9,
            "pcr_window_pct": 4.5, "max_pain_strike": 20_100.0,
        }
        self.futures_live = {
            "current": {
                "ltp": 20_050.0, "oi": 1_000, "volume": 500, "change": 0.2,
                "depth": {"buy": [{"price": 20_049.5, "quantity": 75, "orders": 3}],
                          "sell": [{"price": 20_050.5, "quantity": 60, "orders": 2}]},
            }
        }
        self.option_tick_count = 42
        self.options_live = {
            20_000.0: {  # CORE (0 % away) — depth must be recorded
                "CE": {"ltp": 120.0, "bid": 119.5, "ask": 120.5, "buy_qty": 500,
                       "sell_qty": 400, "oi": 1_000, "open_oi": 900, "prev_oi": 950,
                       "has_oi": True, "volume": 10_000, "iv": 0.14, "delta": 0.5,
                       "gamma": 0.001, "theta": -2.0, "vega": 8.0, "iv_change": 0.1,
                       "timestamp": "2026-11-23 10:00:00",
                       "depth": {"buy": [{"price": 119.5, "quantity": 300, "orders": 5}],
                                 "sell": [{"price": 120.5, "quantity": 250, "orders": 4}]}},
                "PE": {"ltp": 110.0, "oi": 800, "open_oi": 700, "has_oi": True},
            },
            21_000.0: {  # 5 % away — not CORE, no depth
                "CE": {"ltp": 10.0, "oi": 300, "has_oi": True},
            },
        }


class FakeObj:
    stock_symbol = "NIFTY"

    def __init__(self):
        self._tick_store = FakeTickStore()


def _rec(tmp_path):
    return cr.ChainRecorder([FakeObj()], root=tmp_path)


def test_chain_rows_and_depth_only_for_core(tmp_path):
    rec = _rec(tmp_path)
    rec._capture_chain(datetime(2026, 11, 23, 10, 0, 0))
    rows = rec._chain["NIFTY"]
    assert len(rows) == 3
    core = next(r for r in rows if r["strike"] == 20_000.0 and r["side"] == "CE")
    assert core["is_core"] is True
    assert core["buy_px_1"] == 119.5 and core["buy_qty_1"] == 300
    far = next(r for r in rows if r["strike"] == 21_000.0)
    assert far["is_core"] is False
    assert far.get("buy_px_1") is None
    assert core["dte"] == 2
    assert core["iv_src"] == "sensibull"


def test_context_row_has_chain_context(tmp_path):
    rec = _rec(tmp_path)
    rec._capture_chain(datetime(2026, 11, 23, 10, 0, 0))
    ctx = rec._ctx["NIFTY"][0]
    assert ctx["spot"] == 20_000.0
    assert ctx["future_price"] == 20_050.0
    assert ctx["live_pcr"] == 0.9
    assert ctx["expiry"] == "2026-11-25"
    assert ctx["option_tick_count"] == 42


def test_futures_rows_include_depth(tmp_path):
    rec = _rec(tmp_path)
    rec._capture_futures(datetime(2026, 11, 23, 10, 0, 0))
    row = rec._fut["NIFTY"][0]
    assert row["expiry_key"] == "current"
    assert row["buy_px_1"] == 20_049.5
    assert row["sell_qty_1"] == 60


def test_index_rows_include_symbols(tmp_path):
    rec = _rec(tmp_path)
    rec._capture_index(datetime(2026, 11, 23, 10, 0, 0))
    assert rec._idx[0]["symbol"] == "NIFTY"
    assert rec._idx[0]["ltp"] == 20_000.0


def test_flush_writes_readable_parquet(tmp_path):
    rec = _rec(tmp_path)
    rec._capture_chain(datetime(2026, 11, 23, 10, 0, 0))
    rec._capture_futures(datetime(2026, 11, 23, 10, 0, 0))
    rec._flush_all()
    assert rec.files_written == 3      # chain + context + futures
    files = list(Path(tmp_path).rglob("*.parquet"))
    assert len(files) == 3
    df = pd.read_parquet(files[0])
    assert len(df) > 0
    assert "snapshot_id" in df.columns or "expiry_key" in df.columns
    # buffers drained
    assert rec._chain["NIFTY"] == []


def test_prune_removes_only_old_days(tmp_path):
    old = Path(tmp_path) / "chain" / "NIFTY" / "2025-01-01"
    new = Path(tmp_path) / "chain" / "NIFTY" / date.today().isoformat()
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "x.parquet").write_bytes(b"x")
    removed = cr.ChainRecorder([], root=tmp_path).prune_old(keep_days=30)
    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_session_window(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "is_trading_day", lambda d=None: True)
    rec = _rec(tmp_path)
    assert rec._in_session(datetime(2026, 11, 23, 10, 0)) is True
    assert rec._in_session(datetime(2026, 11, 23, 9, 0)) is False
    assert rec._in_session(datetime(2026, 11, 23, 15, 31)) is False


def test_flush_writes_manifest(tmp_path):
    rec = _rec(tmp_path)
    rec._capture_chain(datetime(2026, 11, 23, 10, 0, 0))
    rec._flush_all()
    manifests = list(Path(tmp_path).rglob("manifest.json"))
    assert len(manifests) == 1
    import json
    payload = json.loads(manifests[0].read_text())
    assert payload["files"] >= 2      # chain + context (no futures captured)
    assert payload["rows"]["chain:NIFTY"] == 3
    assert payload["expected_chain_snapshots"] > 0
