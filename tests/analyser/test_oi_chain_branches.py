"""Branch-depth tests for OIChainAnalyser (audit 2026-11-23, second pass).

The main test file covers one happy path per analyser; these cover the
alternative decision branches that were still unexecuted:
put-side buildup, put-only / both walls, put-writing imbalance, abnormal
call writing below price, call-wall migration, retreat, and the S/R squeeze
and widening cases. Every fixture was verified to emit the stated signal.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import common.shared as shared
from services.analysis_engine.analyser.OIChainAnalyser import OIChainAnalyser
from tests.analyser.conftest import make_oi_chain, make_sensibull_ctx, make_stock


def _positional_ctx():
    m = MagicMock()
    m.mode = shared.Mode.POSITIONAL
    return m


def _intraday_ctx():
    m = MagicMock()
    m.mode = shared.Mode.INTRADAY
    return m


def _chain_stock(strikes, spot=20_000.0, prev_ltp=None):
    oi = make_oi_chain(strikes=strikes, spot=spot)
    if prev_ltp is not None:
        oi["prev_ltp"] = prev_ltp
    s = make_stock()
    s.ltp = spot
    s.sensibull_ctx = make_sensibull_ctx()
    s.sensibull_ctx["oi_chain"] = oi
    return s


def _flat_strikes(strikes):
    return {
        strike: {"call_oi": 10_000, "put_oi": 10_000,
                 "prev_call_oi": 9_500, "prev_put_oi": 9_500}
        for strike in strikes
    }


GRID = (19600, 19700, 19800, 19900, 20000, 20100, 20200, 20300, 20400)


class TestOiBuildupBranches:
    def test_put_writing_buildup_is_bullish(self):
        strikes = {}
        for strike in (19500, 19700, 19800, 19900, 20000, 20100, 20200, 20300, 20500):
            new_put = 40_000 if strike <= 20000 else 5_000
            strikes[strike] = {"call_oi": 5_000, "put_oi": new_put,
                               "prev_call_oi": 4_500,
                               "prev_put_oi": 20_000 if strike <= 20000 else 5_000}
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = _chain_stock(strikes, spot=19_950.0)
            s.sensibull_ctx["oi_chain"]["total_put_oi_change"] = 30_000
            assert a.analyse_oi_buildup(s) is True
            buildup = s.analysis["BULLISH"]["OI_BUILDUP"]
            assert buildup.buildup_type == "HEAVY_PUT_WRITING"
            assert "put writing" in buildup.signal
            assert "30,000" in buildup.signal


class TestOiWallBranches:
    def test_put_only_wall_is_bullish_support(self):
        strikes = {
            strike: {"call_oi": 10_000, "put_oi": 15_000,
                     "prev_call_oi": 9_500, "prev_put_oi": 14_000}
            for strike in GRID
        }
        strikes[19900]["put_oi"] = 150_000
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = _chain_stock(strikes)
            assert a.analyse_oi_wall(s) is True
            wall = s.analysis["BULLISH"]["OI_WALL"]
            assert wall.wall_type == "PUT_WALL_ONLY"
            assert wall.nearest_put_wall[0] == 19900

    def test_both_walls_direction_follows_the_closer_wall(self):
        strikes = _flat_strikes(GRID)
        strikes[20100]["call_oi"] = 150_000   # 0.5 % above
        strikes[19600]["put_oi"] = 150_000    # 2 % below
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = _chain_stock(strikes)
            assert a.analyse_oi_wall(s) is True
            wall = s.analysis["BEARISH"]["OI_WALL"]
            assert wall.wall_type == "BOTH_WALLS"
            assert wall.nearest_call_wall[0] == 20100

    def test_equidistant_walls_are_skipped(self):
        strikes = _flat_strikes(GRID)
        strikes[20200]["call_oi"] = 150_000   # 1 % above — same distance as put
        strikes[19800]["put_oi"] = 150_000    # 1 % below
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = _chain_stock(strikes)
            assert a.analyse_oi_wall(s) is False


class TestOiShiftBranches:
    def test_extreme_put_writing_is_bullish(self):
        strikes = {
            strike: {"call_oi": 100_000, "put_oi": 100_000,
                     "prev_call_oi": 99_800, "prev_put_oi": 80_000}
            for strike in (19800, 19900, 20000, 20100, 20200, 20300, 20400)
        }
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = _chain_stock(strikes)
            assert a.analyse_oi_shift(s) is True
            shift = s.analysis["BULLISH"]["OI_SHIFT"]
            assert shift.total_new_put_oi == 140_000
            assert shift.total_new_call_oi == 1_400

    def test_call_writing_below_price_is_abnormal_bearish(self):
        strikes = {}
        for strike in (19000, 19100, 19200, 19300, 19400, 19500, 19600):
            added = strike in (19000, 19100, 19200)
            strikes[strike] = {
                "call_oi": 100_000 if added else 50_000,
                "put_oi": 50_000,
                "prev_call_oi": 20_000 if added else 50_000,
                "prev_put_oi": 50_000,
            }
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = _chain_stock(strikes)
            assert a.analyse_oi_shift(s) is True
            shift = s.analysis["BEARISH"]["OI_SHIFT"]
            assert shift.call_shift_direction == "BELOW_PRICE"
            assert "ABNORMAL" in shift.signal


class TestOiWallMigrationBranches:
    def test_call_wall_moving_lower_is_bearish(self):
        strikes = _flat_strikes(GRID)
        strikes[20100]["call_oi"] = 150_000        # today
        strikes[20300]["prev_call_oi"] = 150_000   # yesterday
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = _chain_stock(strikes, prev_ltp=19_950.0)
            assert a.analyse_oi_wall_migration(s) is True
            migration = s.analysis["BEARISH"]["OI_WALL_MIGRATION"]
            assert migration.side == "CALL"
            assert migration.migration_direction == "LOWER"
            assert migration.migration_pts == -200

    def test_wall_retreat_is_neutral_warning(self):
        strikes = _flat_strikes(GRID)
        strikes[19600]["prev_put_oi"] = 150_000    # only yesterday had a wall
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = _chain_stock(strikes, prev_ltp=19_950.0)
            assert a.analyse_oi_wall_migration(s) is True
            migration = s.analysis["NEUTRAL"]["OI_WALL_MIGRATION"]
            assert migration.side == "PUT"
            assert migration.migration_direction == "RETREAT"
            assert migration.curr_wall_strike is None


class TestIntradayOiTrendBranches:
    def _snapshot(self, call_oi, put_oi, spot, pcr):
        return {"timestamp": None, "current_ltp": spot,
                "total_call_oi": call_oi, "total_put_oi": put_oi,
                "pcr": pcr, "per_strike_data": {}}

    def test_put_buildup_with_rising_pcr_is_bullish(self):
        history = [
            self._snapshot(100_000, 90_000, 20_000, 0.90),
            self._snapshot(100_000, 95_000, 20_010, 0.98),
            self._snapshot(100_000, 100_000, 20_020, 1.05),
            self._snapshot(100_000, 105_000, 20_030, 1.12),
            self._snapshot(100_000, 110_000, 20_040, 1.18),
            self._snapshot(100_000, 115_000, 20_050, 1.22),
        ]
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx()
            s.sensibull_ctx["oi_chain_history"] = history
            assert a.analyse_intraday_oi_trend(s) is True
            trend = s.analysis["BULLISH"]["OI_INTRADAY_TREND"]
            assert trend.put_oi_change_pct == pytest.approx(27.8, abs=0.1)
            assert "put writing" in trend.signal.lower()


class TestSrShiftBranches:
    def _snapshot(self, call_wall, put_wall, spot=20_000.0):
        data = {
            strike: {"call_oi": 100_000 if strike == call_wall else 20_000,
                     "put_oi": 100_000 if strike == put_wall else 20_000,
                     "prev_call_oi": 20_000, "prev_put_oi": 20_000}
            for strike in range(19600, 20500, 100)
        }
        return {"timestamp": None, "current_ltp": spot, "total_call_oi": 0,
                "total_put_oi": 0, "pcr": 1.0, "per_strike_data": data,
                "expiry": "2024-01-25"}

    def _run(self, pairs):
        history = [self._snapshot(c, p) for c, p in pairs]
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx()
            s.sensibull_ctx["oi_chain_history"] = history
            return a.analyse_intraday_oi_sr_shift(s), s

    def test_both_levels_falling_is_bearish(self):
        result, s = self._run([
            (20200, 19800), (20200, 19800), (20100, 19700),
            (20100, 19700), (20000, 19600), (20000, 19600),
        ])
        assert result is True
        shift = s.analysis["BEARISH"]["OI_SR_SHIFT"]
        assert shift.resistance_shift == -200 and shift.support_shift == -200

    def test_range_squeeze_is_neutral(self):
        result, s = self._run([
            (20200, 19800), (20200, 19800), (20100, 19900),
            (20100, 19900), (20000, 20000), (20000, 20000),
        ])
        assert result is True
        shift = s.analysis["NEUTRAL"]["OI_SR_SHIFT"]
        assert shift.range_narrowing is True
        assert "squeezing" in shift.signal.lower()

    def test_range_widening_is_neutral(self):
        result, s = self._run([
            (20200, 19800), (20200, 19800), (20300, 19700),
            (20300, 19700), (20400, 19600), (20400, 19600),
        ])
        assert result is True
        shift = s.analysis["NEUTRAL"]["OI_SR_SHIFT"]
        assert shift.range_narrowing is False
        assert "widening" in shift.signal.lower()
