"""Paper Trading — SQLite trade ledger for persistent trade history.

Survives Redis restarts (RDB does not persist streams).  Writes are
appended from ``persist_closed_position()`` in sync with Redis writes.
Reads serve the /paper_trades and /paper_export bot commands.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Optional

from lib.logging_util import get_logger
logger = get_logger("paper-trading")

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "paper_trades.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    position_id    TEXT PRIMARY KEY,
    symbol         TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    direction      TEXT,
    mode           TEXT,
    entry_credit   REAL,
    margin_blocked REAL,
    exit_premium   REAL,
    pnl            REAL,
    exit_reason    TEXT,
    signal_source  TEXT,
    signal_score   REAL,
    entry_ts       REAL,
    exit_ts        REAL,
    legs_json      TEXT,
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol  ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_exit_ts ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS idx_trades_date    ON trades(created_at);
"""


def _get_path() -> str:
    return os.path.abspath(DB_PATH)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_get_path())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    path = _get_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    logger.info("[ledger] SQLite trade ledger initialised at %s", path)


def insert_trade(position) -> None:
    import json

    conn = _connect()
    try:
        legs_json = json.dumps([
            {
                "strike": leg.strike,
                "option_type": leg.option_type,
                "side": leg.side,
                "lots": leg.lots,
                "entry_premium": leg.entry_premium,
                "current_premium": leg.current_premium,
            }
            for leg in position.legs
        ])
        conn.execute(
            """INSERT OR REPLACE INTO trades
               (position_id, symbol, strategy, direction, mode,
                entry_credit, margin_blocked, exit_premium, pnl, exit_reason,
                signal_source, signal_score, entry_ts, exit_ts, legs_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position.position_id,
                position.symbol,
                position.strategy,
                position.direction,
                position.mode,
                position.entry_credit,
                position.margin_blocked,
                position.exit_premium,
                position.pnl,
                position.exit_reason,
                position.signal_source,
                position.signal_score,
                position.entry_timestamp,
                position.exit_timestamp,
                legs_json,
            ),
        )
        conn.commit()
    except Exception as e:
        logger.error("[ledger] Failed to insert trade %s: %s", position.position_id, e, exc_info=True)
    finally:
        conn.close()


def get_recent_trades(limit: int = 10) -> list[dict]:
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY exit_ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[ledger] Failed to query trades: %s", e, exc_info=True)
        return []
    finally:
        conn.close()


def get_all_trades(symbol: Optional[str] = None) -> list[dict]:
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        if symbol:
            rows = conn.execute(
                "SELECT * FROM trades WHERE symbol = ? ORDER BY exit_ts DESC", (symbol,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY exit_ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[ledger] Failed to export trades: %s", e, exc_info=True)
        return []
    finally:
        conn.close()


def trade_count() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def total_pnl() -> float:
    conn = _connect()
    try:
        row = conn.execute("SELECT COALESCE(SUM(pnl), 0) FROM trades").fetchone()
        return row[0] if row else 0.0
    except Exception:
        return 0.0
    finally:
        conn.close()
