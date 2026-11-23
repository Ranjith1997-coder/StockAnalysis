"""Integration: capture flush, manifest and retention (T4.5).

Capture is the input to replay and training, so a silent flush failure or a
prune that eats today's files would be discovered only weeks later.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pyarrow.parquet as pq
import pytest

from lib.zerodha.tick_store import TickStore
from services.market_data.chain_recorder import CHAIN_COLUMNS, ChainRecorder

pytestmark = pytest.mark.integration


class _Obj:
    def __init__(self, symbol: str) -> None:
        self.stock_symbol = symbol
        self._tick_store = TickStore()


def _rows(n: int = 4) -> list[dict]:
    return [
        {
            "snapshot_id": f"snap-{i}",
            "symbol": "NIFTY",
            "strike": 25_000.0 + i * 50,
            "opt_type": "CE",
            "ltp": 100.0 + i,
            "oi": 1_000_000 + i,
            "has_oi": True,
        }
        for i in range(n)
    ]


def test_flush_writes_parquet_and_manifest(tmp_path) -> None:
    recorder = ChainRecorder([_Obj("NIFTY")], root=tmp_path)
    rows = _rows(6)
    recorder._chain["NIFTY"] = rows

    recorder._flush_all()

    files = list((tmp_path / "chain" / "NIFTY").rglob("*.parquet"))
    assert len(files) == 1, f"expected one parquet file, got {files}"
    table = pq.read_table(files[0])
    assert table.num_rows == len(rows)
    assert table.column_names == CHAIN_COLUMNS

    manifest_path = next((tmp_path / "eod").rglob("manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["rows"]["chain:NIFTY"] == len(rows)
    assert manifest["files"] == recorder.files_written
    assert manifest["expected_chain_snapshots"] > 0
    assert set(manifest) >= {"date", "files", "rows", "snapshots_taken", "updated_at"}


def test_buffers_are_empty_after_flush(tmp_path) -> None:
    recorder = ChainRecorder([_Obj("NIFTY")], root=tmp_path)
    recorder._chain["NIFTY"] = _rows(2)
    recorder._ctx["NIFTY"] = [{"snapshot_id": "c1"}]
    recorder._fut["NIFTY"] = [{"snapshot_id": "f1"}]
    recorder._idx = [{"symbol": "INDIA_VIX"}]

    recorder._flush_all()

    assert recorder._chain == {"NIFTY": []}
    assert recorder._ctx["NIFTY"] == []
    assert recorder._fut["NIFTY"] == []
    assert recorder._idx == []
    assert recorder.files_written == 4


def test_prune_removes_old_days_but_keeps_today(tmp_path) -> None:
    recorder = ChainRecorder([], root=tmp_path)
    old_day = (date.today() - timedelta(days=400)).isoformat()
    today = date.today().isoformat()
    for day in (old_day, today):
        folder = tmp_path / "chain" / "NIFTY" / day
        folder.mkdir(parents=True)
        (folder / "0900.parquet").write_bytes(b"x")

    removed = recorder.prune_old(keep_days=180)

    assert removed >= 1
    assert not (tmp_path / "chain" / "NIFTY" / old_day).exists()
    assert (tmp_path / "chain" / "NIFTY" / today).exists()


def test_prune_ignores_non_date_directories(tmp_path) -> None:
    recorder = ChainRecorder([], root=tmp_path)
    keep = tmp_path / "chain" / "NIFTY" / "scratch"
    keep.mkdir(parents=True)
    (keep / "notes.txt").write_text("keep me")

    recorder.prune_old(keep_days=180)
    assert keep.exists()
