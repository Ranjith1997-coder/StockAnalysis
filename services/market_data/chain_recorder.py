"""Intraday options/futures/index capture (programme Milestone M2).

Records what the live pipeline currently throws away, so intraday strategies can
be measured and backtested. Design: docs/options-data-capture.md.

Layout (all Parquet, zstd, one file per 5-minute flush):

    data/options_history/chain/{SYM}/{YYYY-MM-DD}/{HHMM}.parquet
    data/options_history/context/{SYM}/{YYYY-MM-DD}/{HHMM}.parquet
    data/options_history/futures/{SYM}/{YYYY-MM-DD}/{HHMM}.parquet
    data/options_history/index/{YYYY-MM-DD}/{HHMM}.parquet
    data/options_history/eod/{YYYY-MM-DD}/manifest.json

Chain rows are per leg and reference a ``snapshot_id``; the matching context row
(spot, futures, VIX, PCR, max pain, …) lives in the context file. Depth is
flattened into columns and recorded only for CORE legs (|strike−spot| ≤ 1 %).

Capture reads the in-process TickStore directly (same snapshot the publisher
uses): no Redis round-trip, no extra staleness, no blocking of the tick path.
Writes are atomic (tmp + rename) and the writer never fails the service.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import common.constants as constant
from common.market_calendar import is_trading_day
from lib.logging_util import get_logger

logger = get_logger("market-data")

ROOT = Path("data/options_history")

CHAIN_INTERVAL = 30          # seconds between chain snapshots
FUTURES_INTERVAL = 1         # seconds between futures snapshots
INDEX_INTERVAL = 10          # seconds between spot/GIFT/VIX snapshots
FLUSH_INTERVAL = 300         # seconds between disk flushes
RETENTION_DAYS = 180         # hot retention
MIN_FREE_GB = 5.0            # stop capture below this
CORE_PCT = 0.01              # depth recorded for legs within 1 % of spot
DEPTH_LEVELS = 5
SESSION_START = (9, 15)
SESSION_END = (15, 30)
EXTRA_SYMBOLS = ("GIFTNIFTY", "INDIA_VIX")


def _f(value):
    try:
        v = float(value)
        return v if v == v else None      # NaN → None
    except (TypeError, ValueError):
        return None


def _i(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _dte(expiry: str, today: date) -> int | None:
    try:
        return (date.fromisoformat(str(expiry)[:10]) - today).days
    except (ValueError, TypeError):
        return None


def _flatten_depth(row: dict, depth) -> None:
    levels = depth if isinstance(depth, dict) else {}
    for side in ("buy", "sell"):
        for i, level in enumerate((levels.get(side) or [])[:DEPTH_LEVELS], start=1):
            row[f"{side}_px_{i}"] = _f(level.get("price"))
            row[f"{side}_qty_{i}"] = _i(level.get("quantity"))
            row[f"{side}_ord_{i}"] = _i(level.get("orders"))


def _depth_columns() -> list[str]:
    cols = []
    for side in ("buy", "sell"):
        for i in range(1, DEPTH_LEVELS + 1):
            cols += [f"{side}_px_{i}", f"{side}_qty_{i}", f"{side}_ord_{i}"]
    return cols


CHAIN_COLUMNS = [
    "snapshot_id", "ts", "ts_exchange", "symbol", "expiry", "dte",
    "strike", "side", "ltp", "bid", "ask", "bid_qty", "ask_qty",
    "oi", "open_oi", "prev_oi", "has_oi", "volume",
    "iv", "iv_src", "delta", "gamma", "theta", "vega", "iv_change",
    "spot", "is_core",
] + _depth_columns()

CONTEXT_COLUMNS = [
    "snapshot_id", "ts", "symbol", "spot", "future_price", "fut_oi",
    "fut_volume", "vix", "atm_strike", "atm_straddle", "live_pcr",
    "pcr_window_pct", "max_pain_strike", "expiry", "dte", "option_tick_count",
    # Volatility context read from data:sensibull (nullable: older days and
    # runs without the hash keep them empty).
    "atm_iv_percentile", "atm_iv_near", "atm_iv_next", "next_expiry", "term_slope",
]

FUTURES_COLUMNS = [
    "ts", "symbol", "expiry_key", "ltp", "oi", "volume", "change",
] + _depth_columns()

INDEX_COLUMNS = ["ts", "symbol", "ltp"]


class ChainRecorder:
    """Daemon thread capturing chain/futures/index snapshots to Parquet."""

    def __init__(self, objects, root: Path = ROOT, redis=None):
        # objects: Stock-like objects (index objects + optional GIFTNIFTY/VIX).
        self._objs = {o.stock_symbol: o for o in objects}
        self._root = Path(root)
        self._redis = redis
        self._running = False
        self._thread: threading.Thread | None = None
        self._chain: dict[str, list] = {}
        self._ctx: dict[str, list] = {}
        self._fut: dict[str, list] = {}
        self._idx: list = []
        self._session_date: date | None = None
        self._session_is_trading = False
        self._last_prune_date: date | None = None
        self._snapshot_seq = 0
        self.files_written = 0
        self.snapshots_taken = 0
        self._manifest_rows: dict[str, int] = defaultdict(int)

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="chain-recorder")
        self._thread.start()
        logger.info("[capture] Recorder started (chain=%ds, futures=%ds, index=%ds)",
                    CHAIN_INTERVAL, FUTURES_INTERVAL, INDEX_INTERVAL)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        self._flush_all()
        logger.info("[capture] Recorder stopped")

    # ── scheduling ───────────────────────────────────────────────────────
    def _refresh_session(self, now: datetime) -> None:
        if self._session_date != now.date():
            self._session_date = now.date()
            try:
                self._session_is_trading = bool(is_trading_day(now.date()))
            except Exception:
                self._session_is_trading = now.weekday() < 5
        if self._last_prune_date != now.date():
            self._last_prune_date = now.date()
            try:
                self.prune_old()
            except Exception:
                logger.exception("[capture] retention prune failed")

    def _in_session(self, now: datetime) -> bool:
        self._refresh_session(now)
        if not self._session_is_trading:
            return False
        hm = (now.hour, now.minute)
        return SESSION_START <= hm <= SESSION_END

    def _loop(self) -> None:
        next_chain = next_fut = next_idx = next_flush = 0.0
        while self._running:
            try:
                now_dt = datetime.now()
                if self._in_session(now_dt):
                    now = time.time()
                    if now >= next_fut:
                        self._capture_futures(now_dt)
                        next_fut = now + FUTURES_INTERVAL
                    if now >= next_chain:
                        self._capture_chain(now_dt)
                        next_chain = now + CHAIN_INTERVAL
                    if now >= next_idx:
                        self._capture_index(now_dt)
                        next_idx = now + INDEX_INTERVAL
                    if now >= next_flush:
                        self._flush_all()
                        next_flush = now + FLUSH_INTERVAL
                elif any(self._chain.values()) or any(self._ctx.values()) \
                        or any(self._fut.values()) or self._idx:
                    self._flush_all()          # session ended — drain buffers
                    next_chain = next_fut = next_idx = next_flush = 0.0
            except Exception:
                logger.exception("[capture] loop iteration failed")
            time.sleep(1)

    # ── capture ──────────────────────────────────────────────────────────
    def _next_snapshot_id(self) -> str:
        self._snapshot_seq += 1
        return f"{int(time.time())}-{self._snapshot_seq}"

    def _capture_chain(self, now: datetime) -> None:
        snap_id = self._next_snapshot_id()
        ts_iso = now.isoformat(timespec="seconds")
        for symbol, obj in self._objs.items():
            if symbol not in constant.LIVE_OPTIONS_INDICES:
                continue
            ts = obj._tick_store
            with ts._lock:
                agg = dict(ts.options_aggregate)
                legs = {k: dict(v) for k, v in ts.options_live.items()}
                opt_ticks = ts.option_tick_count
            spot = _f(ts._zerodha_data.get("last_price"))
            expiry = str(agg.get("expiry") or "")[:10]
            dte = _dte(expiry, now.date()) if expiry else None

            self._ctx.setdefault(symbol, []).append({
                "snapshot_id": snap_id, "ts": ts_iso, "symbol": symbol,
                "spot": spot,
                "future_price": _f((ts.futures_live.get("current") or {}).get("ltp")),
                "fut_oi": _i((ts.futures_live.get("current") or {}).get("oi")),
                "fut_volume": _i((ts.futures_live.get("current") or {}).get("volume")),
                "vix": self._spot_of("INDIA_VIX"),
                "atm_strike": _f(agg.get("atm_strike")),
                "atm_straddle": _f(agg.get("atm_straddle_premium")),
                "live_pcr": _f(agg.get("live_pcr")),
                "pcr_window_pct": _f(agg.get("pcr_window_pct")),
                "max_pain_strike": _f(agg.get("max_pain_strike")),
                "expiry": expiry, "dte": dte, "option_tick_count": opt_ticks,
                **self._vol_context(symbol),
            })

            for strike, sides in legs.items():
                for side in ("CE", "PE"):
                    tick = sides.get(side)
                    if not tick:
                        continue
                    row = {
                        "snapshot_id": snap_id, "ts": ts_iso,
                        "ts_exchange": str(tick.get("timestamp") or ""),
                        "symbol": symbol, "expiry": expiry, "dte": dte,
                        "strike": _f(strike), "side": side,
                        "ltp": _f(tick.get("ltp")), "bid": _f(tick.get("bid")),
                        "ask": _f(tick.get("ask")),
                        "bid_qty": _i(tick.get("buy_qty")),
                        "ask_qty": _i(tick.get("sell_qty")),
                        "oi": _i(tick.get("oi")), "open_oi": _i(tick.get("open_oi")),
                        "prev_oi": _i(tick.get("prev_oi")),
                        "has_oi": bool(tick.get("has_oi", True)),
                        "volume": _i(tick.get("volume")),
                        "iv": _f(tick.get("iv")),
                        "iv_src": "sensibull" if tick.get("iv") is not None else "",
                        "delta": _f(tick.get("delta")), "gamma": _f(tick.get("gamma")),
                        "theta": _f(tick.get("theta")), "vega": _f(tick.get("vega")),
                        "iv_change": _f(tick.get("iv_change")),
                        "spot": spot, "is_core": False,
                    }
                    if spot and row["strike"]:
                        row["is_core"] = abs(row["strike"] - spot) / spot <= CORE_PCT
                    if row["is_core"]:
                        _flatten_depth(row, tick.get("depth"))
                    self._chain.setdefault(symbol, []).append(row)
            self.snapshots_taken += 1

    def _vol_context(self, symbol: str) -> dict:
        """ATM IV by expiry and its percentile, from data:sensibull.

        The data-gateway publishes every expiry there; the tick store only
        holds the subscribed (nearest) one, so term structure and IV rank would
        otherwise be invisible to replay.
        """
        empty = {
            "atm_iv_percentile": None, "atm_iv_near": None,
            "atm_iv_next": None, "next_expiry": "", "term_slope": None,
        }
        if self._redis is None:
            return empty
        try:
            raw = self._redis.hget(f"data:sensibull:{symbol}", "current_json")
            if not raw:
                return empty
            current = json.loads(raw)
            per_expiry = (
                current.get("stats", {}).get("per_expiry_map")
                or current.get("per_expiry_map")
                or {}
            )
            keys = sorted(k for k in per_expiry if k)
            if not keys:
                return empty
            near = per_expiry[keys[0]] or {}
            near_iv = _f(near.get("atm_iv"))
            percentile = _f(near.get("atm_iv_percentile"))
            next_iv = None
            next_key = ""
            if len(keys) > 1:
                next_key = keys[1]
                next_iv = _f((per_expiry[next_key] or {}).get("atm_iv"))
            slope = (next_iv - near_iv) if (near_iv is not None and next_iv is not None) else None
            return {
                "atm_iv_percentile": percentile,
                "atm_iv_near": near_iv,
                "atm_iv_next": next_iv,
                "next_expiry": next_key,
                "term_slope": slope,
            }
        except Exception as exc:
            logger.debug("[capture] vol context unavailable for %s: %s", symbol, exc)
            return empty

    def _capture_futures(self, now: datetime) -> None:
        ts_iso = now.isoformat(timespec="seconds")
        for symbol, obj in self._objs.items():
            if symbol not in constant.LIVE_OPTIONS_INDICES:
                continue
            ts = obj._tick_store
            with ts._lock:
                live = {k: dict(v) for k, v in ts.futures_live.items()}
            for expiry_key, ft in live.items():
                if not ft:
                    continue
                row = {
                    "ts": ts_iso, "symbol": symbol, "expiry_key": expiry_key,
                    "ltp": _f(ft.get("ltp")), "oi": _i(ft.get("oi")),
                    "volume": _i(ft.get("volume")), "change": _f(ft.get("change")),
                }
                _flatten_depth(row, ft.get("depth"))
                self._fut.setdefault(symbol, []).append(row)

    def _capture_index(self, now: datetime) -> None:
        ts_iso = now.isoformat(timespec="seconds")
        for symbol, obj in self._objs.items():
            if symbol not in constant.LIVE_OPTIONS_INDICES and symbol not in EXTRA_SYMBOLS:
                continue
            ltp = self._spot_of(symbol)
            if ltp:
                self._idx.append({"ts": ts_iso, "symbol": symbol, "ltp": ltp})

    def _spot_of(self, symbol: str):
        obj = self._objs.get(symbol)
        if obj is None:
            return None
        return _f(obj._tick_store._zerodha_data.get("last_price"))

    # ── writing ──────────────────────────────────────────────────────────
    def _flush_all(self) -> None:
        if not self._enough_disk():
            logger.error("[capture] Disk below %.1f GB — capture stopped", MIN_FREE_GB)
            self._running = False
            return
        for symbol, rows in list(self._chain.items()):
            if rows:
                self._write(rows, CHAIN_COLUMNS, "chain", symbol)
                self._chain[symbol] = []
        for symbol, rows in list(self._ctx.items()):
            if rows:
                self._write(rows, CONTEXT_COLUMNS, "context", symbol)
                self._ctx[symbol] = []
        for symbol, rows in list(self._fut.items()):
            if rows:
                self._write(rows, FUTURES_COLUMNS, "futures", symbol)
                self._fut[symbol] = []
        if self._idx:
            self._write(self._idx, INDEX_COLUMNS, "index", None)
            self._idx = []
        if self._manifest_rows:
            self._write_manifest()

    def _expected_chain_snapshots(self) -> int:
        minutes = (SESSION_END[0] * 60 + SESSION_END[1]) - (SESSION_START[0] * 60 + SESSION_START[1])
        per_symbol = minutes * 60 // CHAIN_INTERVAL
        live = sum(1 for s in self._objs if s in constant.LIVE_OPTIONS_INDICES)
        return per_symbol * live

    def _write_manifest(self) -> None:
        """Per-day capture quality: row counts and snapshot coverage."""
        today = datetime.now().strftime("%Y-%m-%d")
        folder = self._root / "eod" / today
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "manifest.json"
            payload = {
                "date": today,
                "files": self.files_written,
                "rows": dict(self._manifest_rows),
                "snapshots_taken": self.snapshots_taken,
                "expected_chain_snapshots": self._expected_chain_snapshots(),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(path)
        except Exception:
            logger.exception("[capture] manifest write failed")

    def _enough_disk(self) -> bool:
        try:
            return shutil.disk_usage(self._root.parent if self._root.parent.exists()
                                     else Path(".")).free / 1024**3 >= MIN_FREE_GB
        except OSError:
            return True

    def _write(self, rows: list[dict], columns: list[str], tier: str, symbol: str | None) -> None:
        now = datetime.now()
        folder = self._root / tier
        if symbol:
            folder = folder / symbol
        folder = folder / now.strftime("%Y-%m-%d")
        path = folder / f"{now.strftime('%H%M')}.parquet"
        try:
            df = pd.DataFrame(rows)
            for col in columns:
                if col not in df.columns:
                    df[col] = None
            df = df[columns]
            folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".parquet.tmp")
            df.to_parquet(tmp, compression="zstd", index=False)
            tmp.replace(path)
            self.files_written += 1
            self._manifest_rows[f"{tier}:{symbol or 'ALL'}"] += len(df)
            logger.debug("[capture] wrote %s (%d rows)", path, len(df))
        except Exception:
            logger.exception("[capture] failed to write %s", path)

    # ── retention ────────────────────────────────────────────────────────
    def prune_old(self, keep_days: int = RETENTION_DAYS) -> int:
        """Delete capture directories older than ``keep_days`` (run daily)."""
        cutoff = date.today() - timedelta(days=keep_days)
        removed = 0
        if not self._root.exists():
            return 0
        for date_dir in self._root.rglob("*"):
            if not date_dir.is_dir():
                continue
            try:
                d = date.fromisoformat(date_dir.name)
            except ValueError:
                continue
            if d < cutoff:
                shutil.rmtree(date_dir, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("[capture] Pruned %d directory(ies) older than %d days",
                        removed, keep_days)
        return removed
