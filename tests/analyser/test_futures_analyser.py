"""Tests for analyser/Futures_Analyser.py."""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

import common.shared as shared
from services.analysis_engine.analyser.Futures_Analyser import FuturesAnalyser
from tests.analyser.conftest import make_stock


def _positional_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    return mock


def _intraday_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    return mock


def _futures_df(n=30, base_price=20000.0, base_oi=1_000_000, trend="up", oi_trend="up"):
    """Build a minimal futures DataFrame with required columns."""
    prices = [base_price * (1.0 + 0.005 * i if trend == "up" else 1.0 - 0.005 * i) for i in range(n)]
    ois    = [base_oi   * (1.0 + 0.01  * i if oi_trend == "up" else 1.0 - 0.01  * i) for i in range(n)]
    highs  = [p * 1.001 for p in prices]
    lows   = [p * 0.999 for p in prices]
    idx    = pd.date_range("2024-01-01", periods=n, freq="5min")
    return pd.DataFrame({
        "open":    prices,
        "high":    highs,
        "low":     lows,
        "close":   prices,
        "volume":  [50_000] * n,
        "oi":      ois,
        "underlying_price": prices,
    }, index=idx)


def _stock_with_futures(curr_df, next_df=None):
    s = make_stock()
    s.zerodha_ctx = {
        "futures_data": {
            "current": curr_df,
            "next": next_df if next_df is not None else pd.DataFrame(),
        }
    }
    return s


class TestCalculateAtr:
    def test_returns_float(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(20)
            atr = a.calculate_atr(df)
            assert isinstance(atr, (float, np.floating))
            assert atr >= 0.0

    def test_short_data_returns_float(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(3)
            atr = a.calculate_atr(df, period=14)
            assert isinstance(atr, (float, np.floating))


class TestGetDynamicThresholds:
    def test_returns_two_floats(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = make_stock()
            df = _futures_df(25)
            price_thr, oi_thr = a.get_dynamic_thresholds(s, df)
            assert isinstance(price_thr, (int, float, np.floating))
            assert isinstance(oi_thr, (int, float, np.floating))
            assert price_thr >= 0.0
            assert oi_thr >= 0.0

    def test_positional_threshold_minimum(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = make_stock()
            df = _futures_df(25)
            price_thr, oi_thr = a.get_dynamic_thresholds(s, df)
            assert float(price_thr) >= FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE

    def test_intraday_threshold_minimum(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = make_stock()
            df = _futures_df(25)
            price_thr, oi_thr = a.get_dynamic_thresholds(s, df)
            assert price_thr >= 0.0


class TestCalculateTrend:
    def test_uptrend_returns_bullish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(10, trend="up")
            assert a.calculate_trend(df) == "BULLISH"

    def test_downtrend_returns_bearish(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(10, trend="down")
            assert a.calculate_trend(df) == "BEARISH"

    def test_single_candle_returns_neutral(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(1)
            assert a.calculate_trend(df) == "NEUTRAL"

    def test_returns_string(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            df = _futures_df(10)
            result = a.calculate_trend(df)
            assert result in ("BULLISH", "BEARISH", "NEUTRAL")


class TestAnalyseIntradayCheckFutureAction:
    def test_long_buildup_bullish(self):
        """Price up + OI up → long buildup → BULLISH FUTURE_ACTION."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            # Make a large price+oi jump in the last row
            df = _futures_df(5, trend="up", oi_trend="up")
            # Exaggerate the last 2 rows for clear signal
            df.iloc[-1, df.columns.get_loc("close")] = df.iloc[-2]["close"] * 1.05
            df.iloc[-1, df.columns.get_loc("oi")]    = df.iloc[-2]["oi"]    * 1.05
            s = _stock_with_futures(df)
            result = a.analyse_intraday_check_future_action(s)
            assert result is True
            action = s.analysis["BULLISH"]["FUTURE_ACTION"]
            assert action.action == "long_buildup"
            assert action.price_percentage == pytest.approx(5.0)

    def test_short_buildup_bearish(self):
        """Price down + OI up → short buildup → BEARISH."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            df = _futures_df(5, trend="up", oi_trend="up")
            df.iloc[-1, df.columns.get_loc("close")] = df.iloc[-2]["close"] * 0.95
            df.iloc[-1, df.columns.get_loc("oi")]    = df.iloc[-2]["oi"]    * 1.05
            s = _stock_with_futures(df)
            result = a.analyse_intraday_check_future_action(s)
            assert result is True
            action = s.analysis["BEARISH"]["FUTURE_ACTION"]
            assert action.action == "short_buildup"
            assert action.price_percentage == pytest.approx(-5.0)

    def test_insufficient_data_returns_false(self):
        """Single row → cannot compute prev/curr comparison → False."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            df = _futures_df(1)
            s = _stock_with_futures(df)
            assert a.analyse_intraday_check_future_action(s) is False

    def test_no_futures_data_returns_false(self):
        """Empty DataFrame → should return False."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = _stock_with_futures(pd.DataFrame())
            assert a.analyse_intraday_check_future_action(s) is False


class TestResetConstants:
    def test_positional_thresholds(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            assert FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE == 3.0
            assert FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE == 1.0

    def test_intraday_thresholds(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            assert FuturesAnalyser.FUTURE_OI_INCREASE_PERCENTAGE == 0.10
            assert FuturesAnalyser.FUTURE_PRICE_CHANGE_PERCENTAGE == 0.15


# ── Per-symbol session state ─────────────────────────────────────────────────

def _orb_df(day="2024-01-02", breakout=True, base=20000.0, open_oi=1_000_000):
    """09:15-start 5-min session: flat ORB, then a final bar breaking out with OI + volume."""
    n = 12
    idx = pd.date_range(f"{day} 09:15", periods=n, freq="5min")
    closes = [base] * n
    vols = [50_000] * n
    ois = [open_oi + i * 1_000 for i in range(n)]
    if breakout:
        closes[-1] = base * 1.01
        vols[-1] = 200_000
        ois[-1] = ois[-2] * 1.02
    return pd.DataFrame({
        "open": closes, "high": [c * 1.0005 for c in closes], "low": [c * 0.9995 for c in closes],
        "close": closes, "volume": vols, "oi": ois, "underlying_price": closes,
    }, index=idx)


class TestPerSymbolSessionState:
    def setup_method(self):
        FuturesAnalyser._session_state = {}

    def test_orb_fired_for_one_symbol_does_not_block_another(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            FuturesAnalyser._session_state = {}
            s1 = _stock_with_futures(_orb_df())
            s1.stock_symbol = "NIFTY"
            s2 = _stock_with_futures(_orb_df())
            s2.stock_symbol = "BANKNIFTY"
            assert a.analyse_intraday_breakout_oi_confirmation(s1) is True
            assert a.analyse_intraday_breakout_oi_confirmation(s2) is True
            # Same symbol, same session → deduplicated
            s1b = _stock_with_futures(_orb_df())
            s1b.stock_symbol = "NIFTY"
            assert a.analyse_intraday_breakout_oi_confirmation(s1b) is False

    def test_orb_state_resets_on_new_session_date(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            FuturesAnalyser._session_state = {}
            s = _stock_with_futures(_orb_df(day="2024-01-02"))
            assert a.analyse_intraday_breakout_oi_confirmation(s) is True
            s_next = _stock_with_futures(_orb_df(day="2024-01-03"))
            assert a.analyse_intraday_breakout_oi_confirmation(s_next) is True

    def test_session_open_oi_is_per_symbol(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            FuturesAnalyser._session_state = {}
            s1 = _stock_with_futures(_orb_df(open_oi=1_000_000))
            s1.stock_symbol = "NIFTY"
            s2 = _stock_with_futures(_orb_df(open_oi=5_000_000))
            s2.stock_symbol = "BANKNIFTY"
            a.analyse_intraday_oi_buildup_from_open(s1)
            a.analyse_intraday_oi_buildup_from_open(s2)
            st = FuturesAnalyser._session_state
            assert st["NIFTY"]["open_oi"] == 1_000_000
            assert st["BANKNIFTY"]["open_oi"] == 5_000_000


# ══════════════════════════════════════════════════════════════════════════════
# Gap-coverage tests (audit 2026-11-23).
#
# analyse_intraday_price_volume_oi_pattern, analyse_positional_oi_trend,
# analyse_positional_cost_of_carry and analyse_positional_rollover_pressure
# were 46-53 % covered: the old tests only exercised the no-data paths.
# Every fixture here was verified to produce the stated signal.
# ══════════════════════════════════════════════════════════════════════════════

from datetime import date, timedelta


def _futures_series(n, price_rate=0.0, oi_rate=0.0, base_price=20_000.0,
                    base_oi=1_000_000, basis_gap=0.0):
    """Daily futures frame.

    ``basis_gap`` > 0 → spot below futures (contango); < 0 → futures below spot
    (backwardation). basis_pct = (futures - spot) / spot.
    """
    rows, price, oi = [], base_price, base_oi
    for _ in range(n):
        price *= 1 + price_rate
        oi *= 1 + oi_rate
        rows.append({
            "open": price, "high": price * 1.001, "low": price * 0.999,
            "close": price, "volume": 50_000, "oi": oi,
            "underlying_price": price * (1 - basis_gap),
        })
    frame = pd.DataFrame(rows)
    frame.index = pd.date_range("2024-01-01", periods=n, freq="D")
    return frame


def _futures_stock(current, next_frame=None, expiry_days=None):
    s = make_stock()
    s.zerodha_ctx = {
        "futures_data": {
            "current": current,
            "next": next_frame if next_frame is not None else pd.DataFrame(),
        }
    }
    if expiry_days is not None:
        s.zerodha_ctx["futures_mdata"] = {
            "current": pd.DataFrame({"expiry": [date.today() + timedelta(days=expiry_days)]})
        }
    return s


class TestIntradayPvoPattern:
    def _run(self, frame):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = _futures_stock(frame)
            return a.analyse_intraday_price_volume_oi_pattern(s), s

    def _spiked_frame(self, price_mult, vol_mult, oi_mult):
        frame = _futures_series(30, price_rate=0.002, oi_rate=0.01)
        prev = frame.iloc[-2]
        frame.iloc[-1, frame.columns.get_loc("close")] = prev["close"] * price_mult
        frame.iloc[-1, frame.columns.get_loc("volume")] = prev["volume"] * vol_mult
        frame.iloc[-1, frame.columns.get_loc("oi")] = prev["oi"] * oi_mult
        return frame

    def test_price_volume_oi_up_is_long_buildup(self):
        result, s = self._run(self._spiked_frame(1.02, 3.0, 1.02))
        assert result is True
        assert s.analysis["BULLISH"]["FUTURE_PVO_PATTERN"].pattern == "price_up_vol_oi_incr"

    def test_price_up_without_participation_is_divergence(self):
        result, s = self._run(self._spiked_frame(1.02, 1.0, 1.0))
        assert result is True
        pattern = s.analysis["NEUTRAL"]["FUTURE_PVO_PATTERN"].pattern
        assert pattern == "price_up_vol_oi_flat"

    def test_single_row_returns_false(self):
        result, _ = self._run(_futures_series(1))
        assert result is False


class TestPositionalOiTrend:
    def _run(self, frame, expiry_days=None):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = _futures_stock(frame, expiry_days=expiry_days)
            return a.analyse_positional_oi_trend(s), s

    @pytest.mark.parametrize(
        "price_rate,oi_rate,sentiment,action",
        [
            (0.005, 0.01, "BULLISH", "LONG_BUILDUP_TREND"),
            (-0.005, 0.01, "BEARISH", "SHORT_BUILDUP_TREND"),
            (0.005, -0.01, "BULLISH", "SHORT_COVERING_TREND"),
            (-0.005, -0.01, "BEARISH", "LONG_UNWINDING_TREND"),
        ],
    )
    def test_four_trend_quadrants(self, price_rate, oi_rate, sentiment, action):
        result, s = self._run(_futures_series(55, price_rate, oi_rate))
        assert result is True
        assert s.analysis[sentiment]["FUTURE_OI_TREND"].action == action

    def test_long_unwinding_suppressed_near_expiry(self):
        result, s = self._run(_futures_series(55, -0.005, -0.01), expiry_days=2)
        assert result is False
        assert "FUTURE_OI_TREND" not in s.analysis["BEARISH"]

    def test_declining_oi_with_flat_price_returns_false(self):
        result, _ = self._run(_futures_series(55, 0.0, -0.01))
        assert result is False


class TestCostOfCarry:
    def _run(self, frame, expiry_days=None, mode=None):
        with patch("common.shared.app_ctx", mode or _positional_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = _futures_stock(frame, expiry_days=expiry_days)
            return a.analyse_positional_cost_of_carry(s), s

    def test_futures_below_spot_is_backwardation(self):
        result, s = self._run(_futures_series(20, 0.001, 0.01, basis_gap=-0.005))
        assert result is True
        carry = s.analysis["BEARISH"]["FUTURE_COST_OF_CARRY"]
        assert carry.action == "BACKWARDATION"
        assert carry.basis_pct < -0.05

    def test_overheated_premium_is_high_cost_of_carry(self):
        result, s = self._run(_futures_series(20, 0.001, 0.01, basis_gap=0.02), expiry_days=30)
        assert result is True
        carry = s.analysis["BEARISH"]["FUTURE_COST_OF_CARRY"]
        assert carry.action == "HIGH_COST_OF_CARRY"
        assert carry.ann_coc > 15.0
        assert carry.days_to_expiry == 30

    def test_basis_expanding_near_expiry_is_bullish_conviction(self):
        frame = _futures_series(20, 0.001, 0.01, basis_gap=-0.0012)
        frame.loc[frame.index[-1], "underlying_price"] = frame["close"].iloc[-1] / 1.0025
        result, s = self._run(frame, expiry_days=8)
        assert result is True
        carry = s.analysis["BULLISH"]["FUTURE_COST_OF_CARRY"]
        assert carry.action == "BASIS_EXPANDING"
        assert carry.basis_trend > 0.05

    def test_unpopulated_spot_returns_false(self):
        result, _ = self._run(_futures_series(20, 0.001, 0.01, basis_gap=0.0))
        assert result is False

    def test_positional_needs_three_rows(self):
        result, _ = self._run(_futures_series(2, 0.001, 0.01, basis_gap=0.005))
        assert result is False


class TestRolloverPressure:
    def _run(self, current, next_frame):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = _futures_stock(current, next_frame)
            return a.analyse_positional_rollover_pressure(s), s

    def test_next_contract_dominant_is_rollover_active(self):
        current = _futures_series(10, 0.001, 0.0, base_oi=1_000_000)
        next_frame = _futures_series(10, 0.001, 0.0, base_oi=800_000)
        result, s = self._run(current, next_frame)
        assert result is True
        rollover = s.analysis["NEUTRAL"]["FUTURE_ROLLOVER"]
        assert rollover.action == "ROLLOVER_ACTIVE"
        assert rollover.ratio < 2.0

    def test_falling_ratio_between_2x_and_4x_is_rollover_starting(self):
        current = _futures_series(10, 0.001, 0.0, base_oi=1_000_000)
        current["oi"] = np.linspace(1_200_000, 250_000, 10)
        next_frame = _futures_series(10, 0.001, 0.0, base_oi=1_000_000)
        next_frame["oi"] = np.linspace(50_000, 100_000, 10)
        result, s = self._run(current, next_frame)
        assert result is True
        rollover = s.analysis["NEUTRAL"]["FUTURE_ROLLOVER"]
        assert rollover.action == "ROLLOVER_STARTING"
        assert rollover.ratio_trend < -0.5

    def test_missing_next_contract_returns_false(self):
        result, _ = self._run(_futures_series(10), None)
        assert result is False

    def test_zero_next_oi_returns_false(self):
        next_frame = _futures_series(10)
        next_frame["oi"] = 0
        result, _ = self._run(_futures_series(10), next_frame)
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# Gap-coverage tests (audit 2026-11-23, second pass): the other two FUTURE_ACTION
# quadrants and OI-buildup-from-open (79 %).
# ══════════════════════════════════════════════════════════════════════════════

class TestFutureActionQuadrants:
    def _run(self, price_mult, oi_mult):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            frame = _futures_df(5, trend="up", oi_trend="up")
            frame.iloc[-1, frame.columns.get_loc("close")] = (
                frame.iloc[-2]["close"] * price_mult
            )
            frame.iloc[-1, frame.columns.get_loc("oi")] = (
                frame.iloc[-2]["oi"] * oi_mult
            )
            s = _stock_with_futures(frame)
            return a.analyse_intraday_check_future_action(s), s

    def test_price_up_oi_down_is_short_covering(self):
        result, s = self._run(1.05, 0.95)
        assert result is True
        assert s.analysis["BULLISH"]["FUTURE_ACTION"].action == "short_covering"

    def test_price_down_oi_down_is_long_unwinding(self):
        result, s = self._run(0.95, 0.95)
        assert result is True
        assert s.analysis["BEARISH"]["FUTURE_ACTION"].action == "long_unwinding"


class TestOiBuildupFromOpen:
    def _run(self, price_rate, oi_rate, n=10):
        frame = _futures_series(n, price_rate, oi_rate)
        frame.index = pd.date_range("2024-01-02 09:15", periods=n, freq="5min")
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = FuturesAnalyser()
            a.reset_constants()
            s = _futures_stock(frame)
            return a.analyse_intraday_oi_buildup_from_open(s), s

    def test_oi_building_with_price_up_is_bullish(self):
        result, s = self._run(0.002, 0.004)
        assert result is True
        analysis = s.analysis["BULLISH"]["FUTURE_OI_FROM_OPEN"]
        assert analysis.action == "OI_BUILDUP_FROM_OPEN"
        assert analysis.oi_from_open_pct > 1.5

    def test_oi_building_with_price_down_is_bearish(self):
        result, s = self._run(-0.002, 0.004)
        assert result is True
        assert s.analysis["BEARISH"]["FUTURE_OI_FROM_OPEN"].action == "OI_SHORT_BUILD_FROM_OPEN"

    @pytest.mark.parametrize(
        "price_rate,action",
        [(0.002, "OI_UNWINDING_FROM_OPEN_UP"), (-0.002, "OI_UNWINDING_FROM_OPEN_DOWN")],
    )
    def test_oi_unwinding_direction_follows_price(self, price_rate, action):
        result, s = self._run(price_rate, -0.004)
        assert result is True
        intended = "BULLISH" if price_rate > 0 else "BEARISH"
        assert s.analysis[intended]["FUTURE_OI_FROM_OPEN"].action == action

    def test_two_rows_returns_false(self):
        result, _ = self._run(0.002, 0.004, n=2)
        assert result is False

    def test_flat_session_returns_false(self):
        result, _ = self._run(0.0, 0.0)
        assert result is False
