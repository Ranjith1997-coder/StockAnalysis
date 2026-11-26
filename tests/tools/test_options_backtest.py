"""Tests for the offline options backtester (tools/options_backtest).

The key acceptance is live-vs-replay: a day recorded by the E2E harness must
replay to the same exit reason and P&L.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from services.market_data.chain_recorder import (
    CHAIN_COLUMNS,
    CONTEXT_COLUMNS,
    FUTURES_COLUMNS,
)
from tools.options_backtest.__main__ import main
from tools.options_backtest.loader import MissingCaptureError, load_day
from tools.options_backtest.replay_redis import ReplayRedis
from tools.options_backtest.simulate import BacktestConfig, Backtester

DATE = "2026-11-24"
SYMBOL = "NIFTY"
GAP = 50.0
STRIKES = [24_700.0, 24_800.0, 24_900.0, 25_000.0, 25_100.0,
           25_200.0, 25_300.0]


def _chain_row(ts, snap_id, strike, side, ltp, spot, oi=100_000):
    return {
        "snapshot_id": snap_id, "ts": ts.isoformat(timespec="seconds"),
        "ts_exchange": ts.isoformat(timespec="seconds"), "symbol": SYMBOL,
        "expiry": "2026-11-26", "dte": 2, "strike": strike, "side": side,
        "ltp": ltp, "bid": ltp - 0.05, "ask": ltp + 0.05,
        "bid_qty": 750, "ask_qty": 750, "oi": oi, "open_oi": oi,
        "prev_oi": oi, "has_oi": True, "volume": 5_000,
        "iv": 13.0, "iv_src": "sensibull", "delta": 0.4, "gamma": 0.0002,
        "theta": -1.0, "vega": 8.0, "iv_change": 0.0,
        "spot": spot, "is_core": True,
    }


def _ctx_row(ts, snap_id, spot):
    return {
        "snapshot_id": snap_id, "ts": ts.isoformat(timespec="seconds"),
        "symbol": SYMBOL, "spot": spot, "future_price": spot + 20,
        "fut_oi": 1_000_000, "fut_volume": 50_000, "vix": 13.5,
        "atm_strike": round(spot / GAP) * GAP, "atm_straddle": 120.0,
        "live_pcr": 1.0, "pcr_window_pct": 1.0, "max_pain_strike": 25_000.0,
        "expiry": "2026-11-26", "dte": 2, "option_tick_count": len(STRIKES) * 2,
    }


def write_day(root: Path, frames: list[tuple[datetime, float, dict[float, dict[str, float]]]],
              symbol: str = SYMBOL) -> None:
    """frames: (ts, spot, {strike: {"CE": ltp, "PE": ltp}})."""
    chain, context, futures = [], [], []
    for index, (ts, spot, ladder) in enumerate(frames):
        snap_id = f"snap-{index}"
        context.append(_ctx_row(ts, snap_id, spot))
        futures.append({"ts": ts.isoformat(timespec="seconds"), "symbol": symbol,
                        "expiry_key": "current", "ltp": spot + 20, "oi": 1_000_000,
                        "volume": 50_000, "change": 0.0})
        for strike, sides in ladder.items():
            for side, ltp in sides.items():
                chain.append(_chain_row(ts, snap_id, strike, side, ltp, spot))

    def frame(rows, columns):
        df = pd.DataFrame(rows)
        for column in columns:
            if column not in df.columns:
                df[column] = None
        return df[columns]

    folder = root / "chain" / symbol / DATE
    folder.mkdir(parents=True, exist_ok=True)
    frame(chain, CHAIN_COLUMNS).to_parquet(folder / "0915.parquet", index=False)
    ctx_folder = root / "context" / symbol / DATE
    ctx_folder.mkdir(parents=True, exist_ok=True)
    frame(context, CONTEXT_COLUMNS).to_parquet(ctx_folder / "0915.parquet", index=False)
    fut_folder = root / "futures" / symbol / DATE
    fut_folder.mkdir(parents=True, exist_ok=True)
    frame(futures, FUTURES_COLUMNS).to_parquet(fut_folder / "0915.parquet", index=False)


def ladder(put_shift: float = 0.0, call_shift: float = 0.0) -> dict[float, dict[str, float]]:
    """Base ladder: shorts at 24900/25100, wings at 24800/25200."""
    out = {strike: {"CE": 40.0, "PE": 40.0} for strike in STRIKES}
    out[24_900.0]["PE"] = 100.0 + put_shift
    out[25_100.0]["CE"] = 100.0 + call_shift
    out[24_800.0]["PE"] = 20.0 + put_shift / 2
    out[25_200.0]["CE"] = 20.0 + call_shift / 2
    return out


SIGNAL = {
    "at": "09:25", "symbol": SYMBOL, "strategy": "IRON_CONDOR",
    "put_wall_strike": 24_900.0, "call_wall_strike": 25_100.0,
    "source": "SCRIPTED",
}


def _config(root: Path, signals=SIGNAL, **overrides) -> BacktestConfig:
    return BacktestConfig(root=root, date=DATE, symbols=(SYMBOL,),
                          signals=tuple([signals] if isinstance(signals, dict) else signals),
                          **overrides)


class TestLoader:
    def test_reads_snapshots_and_context(self, tmp_path):
        base = datetime(2026, 11, 24, 9, 25)
        write_day(tmp_path, [(base, 25_000.0, ladder()),
                             (base + timedelta(minutes=5), 25_010.0, ladder())])

        day = load_day(tmp_path, DATE, SYMBOL)

        assert len(day.snapshots()) == 2
        snap = day.snapshots()[0]
        assert snap.spot == 25_000.0
        assert snap.expiry == "2026-11-26"
        assert set(snap.frame["side"]) == {"CE", "PE"}

    def test_missing_day_raises(self, tmp_path):
        with pytest.raises(MissingCaptureError, match="no chain parquet"):
            load_day(tmp_path, DATE, SYMBOL)


class TestReplayRedis:
    def test_hash_and_string_subset(self):
        redis = ReplayRedis()
        redis.hset("h", mapping={"a": 1, "b": "x"})
        redis.hset("h", "c", 3)
        assert redis.hget("h", "a") == "1"
        assert redis.hgetall("h") == {"a": "1", "b": "x", "c": "3"}
        assert redis.hlen("h") == 3
        redis.set("s", "v")
        assert redis.get("s") == "v"
        assert "h" in redis.keys("*")
        assert redis.hdel("h", "a") == 1

    def test_unknown_command_fails_loudly(self):
        with pytest.raises(AttributeError):
            ReplayRedis().xreadgroup("g", "c", {})


class TestBacktest:
    def test_entry_then_target_exit(self, tmp_path):
        """Shorts decay from 100 to 30 → 50 % target reached → TARGET exit."""
        base = datetime(2026, 11, 24, 9, 25)
        write_day(tmp_path, [
            (base, 25_000.0, ladder()),
            (base + timedelta(minutes=5), 25_000.0, ladder(put_shift=-35, call_shift=-35)),
            (base + timedelta(minutes=10), 25_000.0, ladder(put_shift=-70, call_shift=-70)),
        ])

        result = Backtester(_config(tmp_path)).run()

        entered = [o for o in result.outcomes if o.status == "entered"]
        assert len(entered) == 1
        outcome = entered[0]
        assert outcome.exit_reason == "TARGET"
        assert outcome.pnl is not None and outcome.pnl > 0
        assert outcome.entry_credit and outcome.entry_credit > 0
        assert outcome.max_loss and outcome.max_loss > 0
        assert outcome.mfe is not None and outcome.mfe > 0

    def test_gate_blocks_entries_outside_hours(self, tmp_path):
        base = datetime(2026, 11, 24, 9, 10)     # before the 09:20 window
        write_day(tmp_path, [(base, 25_000.0, ladder())])
        signal = {**SIGNAL, "at": "09:10"}

        result = Backtester(_config(tmp_path, signals=signal)).run()

        assert result.outcomes and result.outcomes[0].status == "blocked"
        assert result.outcomes[0].block_reason == "outside_entry_window"

    def test_missing_day_fails_cleanly(self, tmp_path):
        with pytest.raises(MissingCaptureError):
            Backtester(_config(tmp_path)).run()


class TestLiveVsReplay:
    def test_harness_trade_replays_identically(self, tmp_path):
        """Record a day with the E2E harness, then replay the same scripted signal."""
        import fakeredis

        from services.common.redis_proxy import RedisProxy
        from tests.e2e.sim.config import load_scenarios
        from tests.e2e.sim.harness import Harness

        scenario = {s.name: s for s in load_scenarios()}["trend_day_up"]
        capture_root = tmp_path / "capture"
        redis = RedisProxy.__new__(RedisProxy)
        redis._client = fakeredis.FakeRedis(decode_responses=True)

        live = Harness(scenario, redis, capture_root=capture_root).run()
        live_trades = list(live.trades)
        assert len(live_trades) == 1, "scenario should stop out"

        spec = {
            "at": "09:45", "symbol": "NIFTY", "strategy": "IRON_CONDOR",
            "put_wall_strike": 25350.0, "call_wall_strike": 25650.0,
            "source": "SCRIPTED",
        }
        config = BacktestConfig(root=capture_root, date=scenario.date.isoformat(),
                                symbols=("NIFTY",), signals=(spec,),
                                lot_sizes={"NIFTY": 75})
        replayed = Backtester(config).run()

        entered = [o for o in replayed.outcomes if o.status == "entered"]
        assert len(entered) == 1, [o.to_dict() for o in replayed.outcomes]
        trade = entered[0]
        assert trade.exit_reason == live_trades[0]["exit_reason"]
        assert trade.pnl == pytest.approx(live_trades[0]["pnl"], abs=1.0)


class TestCli:
    def test_list_and_missing(self, tmp_path, capsys):
        base = datetime(2026, 11, 24, 9, 25)
        write_day(tmp_path, [(base, 25_000.0, ladder()), ])

        assert main(["--root", str(tmp_path), "--list"]) == 0
        assert f"{DATE}  {SYMBOL}" in capsys.readouterr().out

        assert main(["--root", str(tmp_path), "--date", "2030-01-01"]) == 1

    def test_run_prints_table_and_json(self, tmp_path, capsys):
        base = datetime(2026, 11, 24, 9, 25)
        write_day(tmp_path, [
            (base, 25_000.0, ladder()),
            (base + timedelta(minutes=5), 25_000.0, ladder(put_shift=-70, call_shift=-70)),
        ])
        signals = tmp_path / "signals.json"
        signals.write_text(json.dumps([SIGNAL]))
        out = tmp_path / "report.json"

        code = main(["--root", str(tmp_path), "--date", DATE,
                     "--signals", str(signals), "--json", str(out)])

        assert code == 0
        stdout = capsys.readouterr().out
        assert "IRON_CONDOR" in stdout
        payload = json.loads(out.read_text())
        assert payload["date"] == DATE
        assert payload["summary"][0]["entries"] == 1


class TestReplayVolatilityContext:
    def test_two_expiries_survive_the_round_trip(self, tmp_path):
        """Captured ATM IVs rebuild a two-expiry map for the term analyser."""
        from tools.options_backtest.context import ReplayContext

        base = datetime(2026, 11, 24, 9, 25)
        write_day(tmp_path, [(base, 25_000.0, ladder())])
        # add the vol-context columns the recorder now writes
        import pandas as pd

        from services.market_data.chain_recorder import CONTEXT_COLUMNS

        path = next((tmp_path / "context" / SYMBOL / DATE).glob("*.parquet"))
        frame = pd.read_parquet(path)
        frame["atm_iv_percentile"] = 82.0
        frame["atm_iv_near"] = 13.0
        frame["atm_iv_next"] = 14.2
        frame["next_expiry"] = "2026-12-03"
        frame["term_slope"] = 1.2
        frame[CONTEXT_COLUMNS].to_parquet(path, index=False)

        day = load_day(tmp_path, DATE, SYMBOL)
        ctx = ReplayContext.for_symbol(SYMBOL)
        ctx.update(day.snapshots()[0], analysis_cycle=True)

        current = json.loads(ctx.redis.hget(f"data:sensibull:{SYMBOL}", "current_json"))
        per_expiry = current["stats"]["per_expiry_map"]
        assert set(per_expiry) == {"2026-11-26", "2026-12-03"}
        assert per_expiry["2026-11-26"]["atm_iv"] == pytest.approx(13.0)
        assert per_expiry["2026-12-03"]["atm_iv"] == pytest.approx(14.2)
        assert per_expiry["2026-11-26"]["atm_iv_percentile"] == pytest.approx(82.0)


class TestOldCapture:
    def test_day_without_vol_context_columns_still_loads(self, tmp_path):
        """Days captured before the Block 2 schema change must remain readable."""
        import pandas as pd

        from services.market_data.chain_recorder import CONTEXT_COLUMNS
        from tools.options_backtest.loader import (
            OPTIONAL_CONTEXT_COLUMNS,
        )

        base = datetime(2026, 11, 24, 9, 25)
        write_day(tmp_path, [(base, 25_000.0, ladder())])

        path = next((tmp_path / "context" / SYMBOL / DATE).glob("*.parquet"))
        frame = pd.read_parquet(path)
        old = frame[[c for c in CONTEXT_COLUMNS if c not in OPTIONAL_CONTEXT_COLUMNS]]
        old.to_parquet(path, index=False)

        day = load_day(tmp_path, DATE, SYMBOL)

        assert len(day.snapshots()) == 1
        assert day.context["atm_iv_near"].isna().all()


class TestCycleScheduling:
    def test_jittered_timestamps_still_run_every_interval(self, tmp_path):
        """Recorder timestamps drift by ~1 s; cycles must not be skipped."""
        from tests.tools.test_options_backtest import write_day as _write

        base = datetime(2026, 11, 24, 9, 25)
        frames = []
        for step in range(40):                      # 40 snapshots, mostly 31 s apart
            ts = base + timedelta(seconds=step * 31)
            frames.append((ts, 25_000.0, ladder()))
        _write(tmp_path, frames)

        config = BacktestConfig(root=tmp_path, date=DATE, symbols=(SYMBOL,),
                                signals=(SIGNAL,), analysis_interval=300)
        result = Backtester(config).run()

        # ~20 minutes of data at 5-minute cycles -> 4-5 cycles, not 0-1
        assert 4 <= result.analysis_cycles <= 6
