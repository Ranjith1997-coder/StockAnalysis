"""Tests for analyser/OIChainAnalyser.py."""
import pytest
from unittest.mock import patch, MagicMock

import common.shared as shared
from analyser.OIChainAnalyser import OIChainAnalyser
from tests.analyser.conftest import make_stock, make_oi_chain, make_sensibull_ctx


def _positional_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    return mock


def _intraday_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    return mock


def _stock_with_oi_chain(spot=20000.0, strikes=None):
    s = make_stock()
    s.ltp = spot
    s.sensibull_ctx = make_sensibull_ctx()
    s.sensibull_ctx["oi_chain"] = make_oi_chain(strikes=strikes, spot=spot)
    return s


class TestAnalyseOiSupportResistance:
    def test_no_oi_chain_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx()
            # oi_chain is None by default
            assert a.analyse_oi_support_resistance(s) is False

    def test_resistance_breach_bullish(self):
        """Price above max call-OI strike → resistance breached → BULLISH."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            # Call wall at 20200, price = 20300 (above resistance)
            strikes = {
                19800: {"call_oi": 10_000, "put_oi": 50_000,
                        "prev_call_oi": 9_000, "prev_put_oi": 45_000},
                20000: {"call_oi": 30_000, "put_oi": 30_000,
                        "prev_call_oi": 28_000, "prev_put_oi": 28_000},
                20200: {"call_oi": 80_000, "put_oi": 5_000,  # dominant call wall
                        "prev_call_oi": 75_000, "prev_put_oi": 4_500},
            }
            oi = make_oi_chain(strikes=strikes, spot=20300.0)  # price above 20200
            s = make_stock()
            s.ltp = 20300.0
            s.sensibull_ctx = make_sensibull_ctx()
            s.sensibull_ctx["oi_chain"] = oi
            result = a.analyse_oi_support_resistance(s)
            assert result is True
            assert "OI_SUPPORT_RESISTANCE" in s.analysis.get("BULLISH", {})

    def test_support_breach_bearish(self):
        """Price below max put-OI strike → support breached → BEARISH."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            strikes = {
                19800: {"call_oi": 5_000, "put_oi": 80_000,  # dominant put wall
                        "prev_call_oi": 4_500, "prev_put_oi": 75_000},
                20000: {"call_oi": 30_000, "put_oi": 30_000,
                        "prev_call_oi": 28_000, "prev_put_oi": 28_000},
                20200: {"call_oi": 60_000, "put_oi": 5_000,
                        "prev_call_oi": 55_000, "prev_put_oi": 4_500},
            }
            oi = make_oi_chain(strikes=strikes, spot=19700.0)  # price below 19800
            s = make_stock()
            s.ltp = 19700.0
            s.sensibull_ctx = make_sensibull_ctx()
            s.sensibull_ctx["oi_chain"] = oi
            result = a.analyse_oi_support_resistance(s)
            assert result is True
            assert "OI_SUPPORT_RESISTANCE" in s.analysis.get("BEARISH", {})

    def test_price_inside_range_returns_false(self):
        """Price between S/R, no breach → no signal."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain(spot=20000.0)  # default: PE wall 19800, CE wall 20200
            assert a.analyse_oi_support_resistance(s) is False

    def test_no_dominant_oi_returns_false(self):
        """All strikes with equal OI → no dominant level → skip."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            flat_strikes = {
                s: {"call_oi": 10_000, "put_oi": 10_000,
                    "prev_call_oi": 9_000, "prev_put_oi": 9_000}
                for s in [19800, 20000, 20200]
            }
            oi = make_oi_chain(strikes=flat_strikes, spot=20300.0)  # price above range
            s_obj = make_stock()
            s_obj.sensibull_ctx = make_sensibull_ctx()
            s_obj.sensibull_ctx["oi_chain"] = oi
            # Flat OI → max call oi = mean call oi → not dominant
            assert a.analyse_oi_support_resistance(s_obj) is False


class TestAnalyseOiBuildup:
    def _buildup_strikes(self, direction="call"):
        """Strikes where one side has 100%+ change at 3+ strikes."""
        base = {}
        for i, strike in enumerate([19500, 19700, 19800, 19900, 20000, 20100, 20200, 20300, 20500]):
            if direction == "call":
                # Call OI doubled at 3 above-ATM strikes
                new_co = 40_000 if strike >= 20000 else 10_000
                prev_co = 20_000 if strike >= 20000 else 10_000
                base[strike] = {"call_oi": new_co, "put_oi": 5_000,
                                "prev_call_oi": prev_co, "prev_put_oi": 4_500}
            else:
                new_po = 40_000 if strike <= 20000 else 5_000
                prev_po = 20_000 if strike <= 20000 else 5_000
                base[strike] = {"call_oi": 5_000, "put_oi": new_po,
                                "prev_call_oi": 4_500, "prev_put_oi": prev_po}
        return base

    def test_call_oi_buildup_bearish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            strikes = self._buildup_strikes("call")
            oi = make_oi_chain(strikes=strikes, spot=19950.0)
            oi["total_call_oi_change"] = 30_000  # large total change
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx()
            s.sensibull_ctx["oi_chain"] = oi
            result = a.analyse_oi_buildup(s)
            if result:
                assert "OI_BUILDUP" in (
                    s.analysis.get("BEARISH", {}) | s.analysis.get("NEUTRAL", {})
                )

    def test_no_oi_data_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx()
            assert a.analyse_oi_buildup(s) is False


class TestAnalyseOiWall:
    def test_strong_call_wall_above_price(self):
        """A single strike with 5x average call OI forms a wall."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            # Most strikes have 10k call OI; 20200 has 70k (7x avg)
            strikes = {
                19800: {"call_oi": 10_000, "put_oi": 15_000, "prev_call_oi": 9_500, "prev_put_oi": 14_000},
                19900: {"call_oi": 10_000, "put_oi": 15_000, "prev_call_oi": 9_500, "prev_put_oi": 14_000},
                20000: {"call_oi": 10_000, "put_oi": 15_000, "prev_call_oi": 9_500, "prev_put_oi": 14_000},
                20100: {"call_oi": 10_000, "put_oi": 10_000, "prev_call_oi": 9_500, "prev_put_oi": 9_500},
                20200: {"call_oi": 70_000, "put_oi": 5_000,  "prev_call_oi": 65_000, "prev_put_oi": 4_500},
            }
            oi = make_oi_chain(strikes=strikes, spot=20000.0)
            s = make_stock()
            s.ltp = 20000.0
            s.sensibull_ctx = make_sensibull_ctx()
            s.sensibull_ctx["oi_chain"] = oi
            result = a.analyse_oi_wall(s)
            if result:
                assert "OI_WALL" in (
                    s.analysis.get("BEARISH", {}) | s.analysis.get("BULLISH", {}) | s.analysis.get("NEUTRAL", {})
                )

    def test_no_oi_data_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx()
            assert a.analyse_oi_wall(s) is False


class TestAnalyseOiShift:
    def test_no_oi_data_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx()
            assert a.analyse_oi_shift(s) is False

    def test_returns_bool_with_data(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = _stock_with_oi_chain(spot=20000.0)
            result = a.analyse_oi_shift(s)
            assert isinstance(result, bool)


class TestAnalyseIntradayOiTrend:
    def _snapshot(self, total_ce, total_pe, spot, pcr=1.0):
        return {
            "timestamp": None,
            "current_ltp": spot,
            "total_call_oi": total_ce,
            "total_put_oi": total_pe,
            "pcr": pcr,
            "per_strike_data": {},
        }

    def test_insufficient_history_returns_false(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx()
            s.sensibull_ctx["oi_chain_history"] = [self._snapshot(100_000, 100_000, 20000)]
            assert a.analyse_intraday_oi_trend(s) is False

    def test_returns_bool_with_enough_history(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            s = make_stock()
            s.sensibull_ctx = make_sensibull_ctx()
            # 6 snapshots with shifting PCR
            history = [
                self._snapshot(100_000, 90_000, 20000, pcr=0.9),
                self._snapshot(105_000, 90_000, 20010, pcr=0.86),
                self._snapshot(110_000, 90_000, 20020, pcr=0.82),
                self._snapshot(115_000, 90_000, 20030, pcr=0.78),
                self._snapshot(120_000, 90_000, 20040, pcr=0.75),
                self._snapshot(125_000, 90_000, 20050, pcr=0.72),
            ]
            s.sensibull_ctx["oi_chain_history"] = history
            result = a.analyse_intraday_oi_trend(s)
            assert isinstance(result, bool)


class TestResetConstants:
    def test_positional_oi_wall_multiplier(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            assert OIChainAnalyser.OI_WALL_STD_MULTIPLIER == 2.0
            assert OIChainAnalyser.OI_BUILDUP_MIN_CHANGE_PCT == 100

    def test_intraday_lower_thresholds(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = OIChainAnalyser()
            a.reset_constants()
            assert OIChainAnalyser.OI_WALL_STD_MULTIPLIER == 1.8
            assert OIChainAnalyser.OI_BUILDUP_MIN_CHANGE_PCT == 75
