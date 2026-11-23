"""Property tests: capture file schema stability (T3).

Downstream replay and training read parquet datasets across many flushes. If one
flush writes a different dtype for the same column, dataset reads break days
later. These tests write through the real ``ChainRecorder._write``.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from services.market_data.chain_recorder import ChainRecorder

pytestmark = pytest.mark.property

COLUMNS = [
    "snapshot_id", "symbol", "strike", "opt_type", "ltp", "oi", "bid", "ask",
    "has_oi", "depth_buy_px_1", "depth_sell_qty_1",
]


def _row(snapshot_id: str, strike: float, ltp: float, oi: int, has_oi: bool) -> dict:
    return {
        "snapshot_id": snapshot_id,
        "symbol": "NIFTY",
        "strike": strike,
        "opt_type": "CE",
        "ltp": ltp,
        "oi": oi,
        "bid": ltp - 0.05,
        "ask": ltp + 0.05,
        "has_oi": has_oi,
        "depth_buy_px_1": ltp - 1.0,
        "depth_sell_qty_1": 750,
    }


def _root() -> Path:
    """Per-example directory: hypothesis fixtures are not reset between inputs."""
    tmp = tempfile.TemporaryDirectory()
    return Path(tmp.name)


def _read(root, tier, symbol):
    files = sorted((root / tier / symbol).rglob("*.parquet"))
    assert files, f"no parquet written under {root / tier / symbol}"
    return [pq.read_table(f) for f in files]


@settings(max_examples=25, deadline=None)
@given(
    n_rows=st.integers(min_value=1, max_value=50),
    ltp=st.floats(min_value=0.5, max_value=1000.0, allow_nan=False),
    oi=st.integers(min_value=0, max_value=10_000_000),
    has_oi=st.booleans(),
)
def test_two_flushes_produce_identical_schema(n_rows, ltp, oi, has_oi) -> None:
    tmp_path = _root()
    recorder = ChainRecorder([], root=tmp_path)
    rows = [
        _row(f"s{i}", 25_000.0 + i, ltp + i, oi + i, has_oi) for i in range(n_rows)
    ]

    recorder._write(rows, COLUMNS, "chain", "A")
    recorder._write(rows, COLUMNS, "chain", "B")

    tables = _read(tmp_path, "chain", "A") + _read(tmp_path, "chain", "B")
    first = tables[0].schema
    for table in tables[1:]:
        assert table.schema.equals(first), (
            f"schema drift across flushes:\n{first}\n---\n{table.schema}"
        )
        assert table.num_rows == n_rows
    assert tables[0].column_names == COLUMNS


@settings(max_examples=25, deadline=None)
@given(n_rows=st.integers(min_value=1, max_value=50))
def test_missing_optional_column_is_filled_not_dropped(n_rows) -> None:
    tmp_path = _root()
    recorder = ChainRecorder([], root=tmp_path)
    sparse = [{"snapshot_id": f"s{i}", "symbol": "NIFTY"} for i in range(n_rows)]

    recorder._write(sparse, COLUMNS, "chain", "A")
    table = _read(tmp_path, "chain", "A")[0]

    assert table.column_names == COLUMNS
    assert table.num_rows == n_rows


@settings(max_examples=25, deadline=None)
@given(n_rows=st.integers(min_value=1, max_value=50))
def test_manifest_counts_match_rows_written(n_rows) -> None:
    tmp_path = _root()
    recorder = ChainRecorder([], root=tmp_path)
    rows = [_row(f"s{i}", 25_000.0 + i, 100.0, 1_000, True) for i in range(n_rows)]
    recorder._write(rows, COLUMNS, "chain", "A")
    assert recorder._manifest_rows["chain:A"] == n_rows
