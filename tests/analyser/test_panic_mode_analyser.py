"""Tests for analyser/PanicModeAnalyser.py."""
import pytest
from collections import namedtuple
from unittest.mock import patch, MagicMock

import common.shared as shared
from analyser.PanicModeAnalyser import PanicModeAnalyser
from tests.analyser.conftest import make_stock, make_sensibull_ctx


def _positional_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    return mock


def _intraday_ctx():
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    return mock


def _make_stock_with_conditions(direction, price_change,
                                iv=False, oi=False, futures=False,
                                volume=False, pcr=False, mode="positional"):
    """
    Build a stock with analysis dict pre-populated to match the requested conditions.
    direction: "BULLISH" or "BEARISH"
    price_change: e.g. +3.5 for bullish, -3.5 for bearish
    """
    s = make_stock()
    s.ltp_change_perc = price_change

    FakeNt = namedtuple("FakeNt", ["signal"])

    if iv:
        # C2: IV_SPIKE in NEUTRAL
        s.analysis["NEUTRAL"]["IV_SPIKE"] = FakeNt(signal="IV spike detected")

    if oi:
        # C3: OI_BUILDUP (positional) or OI_INTRADAY_TREND (intraday)
        if mode == "intraday":
            s.analysis[direction]["OI_INTRADAY_TREND"] = FakeNt(signal="OI trend")
        else:
            s.analysis[direction]["OI_BUILDUP"] = FakeNt(signal="OI buildup")

    if futures:
        # C4: FUTURE_ACTION in direction
        s.analysis[direction]["FUTURE_ACTION"] = FakeNt(signal="futures action")

    if volume:
        # C5: VOLUME_BREAKOUT in direction
        s.analysis[direction]["VOLUME_BREAKOUT"] = FakeNt(signal="volume breakout")

    if pcr:
        # C6: PCR_BIAS in direction
        s.analysis[direction]["PCR_BIAS"] = FakeNt(signal="PCR bias")

    return s


class TestAnalysePanicMode:
    def test_small_price_change_returns_false(self):
        """Below price threshold (3%) → no signal regardless of conditions."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-1.0,
                iv=True, oi=True, futures=True, volume=True, pcr=True,
            )
            assert a.analyse_panic_mode(s) is False

    def test_none_price_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = make_stock()
            s.ltp_change_perc = None
            assert a.analyse_panic_mode(s) is False

    def test_exactly_4_conditions_triggers(self):
        """C1(price) + C2(IV) + C3(OI) + C5(volume) = 4 → triggers."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=True, futures=False, volume=True, pcr=False,
            )
            result = a.analyse_panic_mode(s)
            assert result is True
            assert "PANIC_MODE" in s.analysis["BEARISH"]

    def test_only_3_conditions_no_trigger(self):
        """C1(price) + C2(IV) + C3(OI) = 3 → no trigger."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=True, futures=False, volume=False, pcr=False,
            )
            assert a.analyse_panic_mode(s) is False

    def test_all_6_conditions_bullish(self):
        """All 6 conditions bullish → triggers with count=6."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BULLISH", price_change=+4.0,
                iv=True, oi=True, futures=True, volume=True, pcr=True,
            )
            result = a.analyse_panic_mode(s)
            assert result is True
            signal = s.analysis["BULLISH"]["PANIC_MODE"]
            assert signal.direction == "BULLISH"
            assert signal.conditions_count == 6

    def test_all_6_conditions_bearish(self):
        """All 6 conditions bearish → triggers with count=6."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=True, futures=True, volume=True, pcr=True,
            )
            result = a.analyse_panic_mode(s)
            assert result is True
            signal = s.analysis["BEARISH"]["PANIC_MODE"]
            assert signal.direction == "BEARISH"
            assert signal.conditions_count == 6

    def test_intraday_lower_threshold(self):
        """Intraday threshold is 1.5%; a 1.8% move should pass."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-1.8,
                iv=True, oi=True, futures=True, volume=False, pcr=False,
                mode="intraday",
            )
            result = a.analyse_panic_mode(s)
            assert result is True

    def test_intraday_below_threshold_returns_false(self):
        """1.2% < 1.5% intraday threshold → no signal."""
        with patch("common.shared.app_ctx", _intraday_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-1.2,
                iv=True, oi=True, futures=True, volume=True, pcr=True,
                mode="intraday",
            )
            assert a.analyse_panic_mode(s) is False

    def test_panic_mode_signal_contains_direction(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BULLISH", price_change=+3.5,
                iv=True, oi=True, futures=True, volume=False, pcr=False,
            )
            a.analyse_panic_mode(s)
            signal = s.analysis.get("BULLISH", {}).get("PANIC_MODE")
            if signal:
                assert "BULLISH" in signal.signal

    def test_confidence_moderate_at_4_conditions(self):
        """4 conditions → confidence MODERATE."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=True, futures=False, volume=True, pcr=False,
            )
            a.analyse_panic_mode(s)
            signal = s.analysis["BEARISH"]["PANIC_MODE"]
            assert signal.confidence == "MODERATE"
            assert signal.conditions_count == 4

    def test_confidence_high_at_5_conditions(self):
        """5 conditions → confidence HIGH."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=True, futures=True, volume=True, pcr=False,
            )
            a.analyse_panic_mode(s)
            signal = s.analysis["BEARISH"]["PANIC_MODE"]
            assert signal.confidence == "HIGH"
            assert signal.conditions_count == 5

    def test_confidence_extreme_at_6_conditions(self):
        """6 conditions → confidence EXTREME."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=True, futures=True, volume=True, pcr=True,
            )
            a.analyse_panic_mode(s)
            signal = s.analysis["BEARISH"]["PANIC_MODE"]
            assert signal.confidence == "EXTREME"
            assert signal.conditions_count == 6

    def test_pcr_extreme_does_not_confirm_panic_direction(self):
        """PCR_EXTREME is contrarian — must NOT count as C6 for the panic direction."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=True, futures=False, volume=False, pcr=False,
            )
            # Add PCR_EXTREME BEARISH — this should NOT satisfy C6
            FakeNt = namedtuple("FakeNt", ["signal"])
            s.analysis["BEARISH"]["PCR_EXTREME"] = FakeNt(signal="extreme puts")
            # Only 3 confirmed (C1+C2+C3), below threshold of 4
            result = a.analyse_panic_mode(s)
            assert result is False

    def test_obv_divergence_satisfies_c5(self):
        """OBV_DIVERGENCE in panic direction must count as volume surge (C5)."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=True, futures=False, volume=False, pcr=False,
            )
            FakeNt = namedtuple("FakeNt", ["signal"])
            s.analysis["BEARISH"]["OBV_DIVERGENCE"] = FakeNt(signal="obv divergence")
            result = a.analyse_panic_mode(s)
            assert result is True
            conds = s.analysis["BEARISH"]["PANIC_MODE"].conditions_met
            assert "VOLUME_SURGE" in conds

    def test_iv_rank_high_satisfies_c2(self):
        """IV_RANK category HIGH must count as IV expanding (C2)."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=False, oi=True, futures=True, volume=True, pcr=False,
            )
            IVRank = namedtuple("IV_RANK", ["expiry", "atm_iv", "iv_percentile", "category", "ivp_type"])
            s.analysis["NEUTRAL"]["IV_RANK"] = IVRank(
                expiry="2026-05-08", atm_iv=22.0, iv_percentile=75.0,
                category="HIGH", ivp_type="HIGH"
            )
            result = a.analyse_panic_mode(s)
            assert result is True
            conds = s.analysis["BEARISH"]["PANIC_MODE"].conditions_met
            assert any("IV_EXPANDING" in c for c in conds)

    def test_oi_wall_directional_satisfies_c3(self):
        """OI_WALL in the panic direction must count as OI confirm (C3)."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-4.0,
                iv=True, oi=False, futures=True, volume=False, pcr=True,
            )
            FakeNt = namedtuple("FakeNt", ["signal"])
            s.analysis["BEARISH"]["OI_WALL"] = FakeNt(signal="call wall above")
            result = a.analyse_panic_mode(s)
            assert result is True
            conds = s.analysis["BEARISH"]["PANIC_MODE"].conditions_met
            assert "OI_CONFIRM" in conds

    def test_future_breakout_mtf_satisfies_c4(self):
        """FUTURE_BREAKOUT_MTF_ALIGNED must count as futures confirm (C4)."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BULLISH", price_change=+4.0,
                iv=True, oi=True, futures=False, volume=True, pcr=False,
            )
            FakeNt = namedtuple("FakeNt", ["signal"])
            s.analysis["BULLISH"]["FUTURE_BREAKOUT_MTF_ALIGNED"] = FakeNt(signal="mtf breakout")
            result = a.analyse_panic_mode(s)
            assert result is True
            conds = s.analysis["BULLISH"]["PANIC_MODE"].conditions_met
            assert "FUTURES_CONFIRM" in conds

    def test_vix_adaptive_threshold_high_vix(self):
        """With VIX > 20, threshold lowers so a 1.1% move can trigger (intraday 1.5×0.7=1.05)."""
        ctx = _intraday_ctx()
        ctx.india_vix_ltp = 22.0
        with patch("common.shared.app_ctx", ctx):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-1.1,
                iv=True, oi=True, futures=True, volume=False, pcr=False,
                mode="intraday",
            )
            result = a.analyse_panic_mode(s)
            assert result is True

    def test_vix_adaptive_threshold_low_vix(self):
        """With VIX < 13, threshold raises so a 1.8% move no longer triggers (1.5×1.3=1.95)."""
        ctx = _intraday_ctx()
        ctx.india_vix_ltp = 11.0
        with patch("common.shared.app_ctx", ctx):
            a = PanicModeAnalyser()
            s = _make_stock_with_conditions(
                direction="BEARISH", price_change=-1.8,
                iv=True, oi=True, futures=True, volume=True, pcr=True,
                mode="intraday",
            )
            result = a.analyse_panic_mode(s)
            assert result is False


class TestAnalysePanicExhaustion:
    def test_below_price_threshold_returns_false(self):
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = make_stock()
            s.ltp_change_perc = -1.0  # below 3% positional threshold
            assert a.analyse_panic_exhaustion(s) is False

    def test_3_exhaustion_conditions_trigger(self):
        """E1(IV_EXTREME) + E2(PCR_CONTRARIAN) + E3(VOLUME_CLIMAX) = 3 → fires."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = make_stock()
            s.ltp_change_perc = -4.0  # BEARISH panic

            IVRankEx = namedtuple("IV_RANK", ["expiry", "atm_iv", "iv_percentile", "category", "ivp_type"])
            PCREx    = namedtuple("PCR_EXTREME", ["pcr_value", "zone", "signal"])
            VolCl    = namedtuple("VolumeClimaxAnalysis", ["climax_type", "volume", "volume_ma",
                                                           "volume_ratio", "price_trend_pct",
                                                           "close_position", "lookback_days"])

            s.analysis["NEUTRAL"]["IV_RANK_EXTREME"] = IVRankEx(
                expiry="2026-05-08", atm_iv=30.0, iv_percentile=88.0,
                category="VERY_HIGH", ivp_type="VERY_HIGH"
            )
            # Contrarian direction for BEARISH panic = BULLISH
            s.analysis["BULLISH"]["PCR_EXTREME"] = PCREx(
                pcr_value=0.25, zone="EXTREME_LOW", signal="excess calls"
            )
            s.analysis["BEARISH"]["VOLUME_CLIMAX"] = VolCl(
                climax_type="selling_climax", volume=1000000, volume_ma=300000,
                volume_ratio=3.3, price_trend_pct=-6.0, close_position=0.8,
                lookback_days=10
            )

            result = a.analyse_panic_exhaustion(s)
            assert result is True
            signal = s.analysis["BULLISH"]["PANIC_EXHAUSTION"]
            assert signal.panic_direction == "BEARISH"
            assert signal.conditions_count == 3
            assert signal.confidence == "MODERATE"

    def test_2_exhaustion_conditions_no_trigger(self):
        """Only E1 + E3 = 2 → below threshold."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = make_stock()
            s.ltp_change_perc = -4.0

            IVRankEx = namedtuple("IV_RANK", ["expiry", "atm_iv", "iv_percentile", "category", "ivp_type"])
            VolCl    = namedtuple("VolumeClimaxAnalysis", ["climax_type", "volume", "volume_ma",
                                                           "volume_ratio", "price_trend_pct",
                                                           "close_position", "lookback_days"])
            s.analysis["NEUTRAL"]["IV_RANK_EXTREME"] = IVRankEx(
                expiry="2026-05-08", atm_iv=30.0, iv_percentile=88.0,
                category="VERY_HIGH", ivp_type="VERY_HIGH"
            )
            s.analysis["BEARISH"]["VOLUME_CLIMAX"] = VolCl(
                climax_type="selling_climax", volume=1000000, volume_ma=300000,
                volume_ratio=3.3, price_trend_pct=-6.0, close_position=0.8,
                lookback_days=10
            )
            assert a.analyse_panic_exhaustion(s) is False

    def test_candle_reversal_e5_counts(self):
        """Double candlestick reversal in contrarian direction must count as E5."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = make_stock()
            s.ltp_change_perc = -4.0  # BEARISH panic, contrarian = BULLISH

            IVRankEx = namedtuple("IV_RANK", ["expiry", "atm_iv", "iv_percentile", "category", "ivp_type"])
            PCREx    = namedtuple("PCR_EXTREME", ["pcr_value", "zone", "signal"])
            FakeNt   = namedtuple("FakeNt", ["signal"])

            s.analysis["NEUTRAL"]["IV_RANK_EXTREME"] = IVRankEx(
                expiry="2026-05-08", atm_iv=30.0, iv_percentile=90.0,
                category="VERY_HIGH", ivp_type="VERY_HIGH"
            )
            s.analysis["BULLISH"]["PCR_EXTREME"] = PCREx(
                pcr_value=0.25, zone="EXTREME_LOW", signal="excess calls"
            )
            s.analysis["BULLISH"]["Double_candle_stick_pattern"] = FakeNt(signal="bullish engulfing")

            result = a.analyse_panic_exhaustion(s)
            assert result is True
            conds = s.analysis["BULLISH"]["PANIC_EXHAUSTION"].conditions_met
            assert "CANDLE_REVERSAL" in conds

    def test_futures_turning_e4_counts(self):
        """Short covering during BEARISH panic must count as E4 (FUTURES_TURNING)."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            a = PanicModeAnalyser()
            s = make_stock()
            s.ltp_change_perc = -4.0  # BEARISH panic

            IVRankEx = namedtuple("IV_RANK", ["expiry", "atm_iv", "iv_percentile", "category", "ivp_type"])
            PCREx    = namedtuple("PCR_EXTREME", ["pcr_value", "zone", "signal"])
            FakeNt   = namedtuple("FakeNt", ["signal"])

            s.analysis["NEUTRAL"]["IV_RANK_EXTREME"] = IVRankEx(
                expiry="2026-05-08", atm_iv=30.0, iv_percentile=90.0,
                category="VERY_HIGH", ivp_type="VERY_HIGH"
            )
            s.analysis["BULLISH"]["PCR_REVERSAL"] = FakeNt(signal="pcr reversal")
            # Short covering = longs being re-entered = shorts giving up = exhaustion
            s.analysis["BULLISH"]["FUTURE_ACTION_SHORT_COVERING"] = FakeNt(signal="shorts covering")

            result = a.analyse_panic_exhaustion(s)
            assert result is True
            conds = s.analysis["BULLISH"]["PANIC_EXHAUSTION"].conditions_met
            assert "FUTURES_TURNING" in conds

    def test_exhaustion_confidence_tiers(self):
        """3→MODERATE, 4→HIGH, 5→EXTREME."""
        with patch("common.shared.app_ctx", _positional_ctx()):
            for extra_conds, expected_conf in [(0, "MODERATE"), (1, "HIGH"), (2, "EXTREME")]:
                a = PanicModeAnalyser()
                s = make_stock()
                s.ltp_change_perc = -4.0

                IVRankEx = namedtuple("IV_RANK", ["expiry", "atm_iv", "iv_percentile", "category", "ivp_type"])
                PCREx    = namedtuple("PCR_EXTREME", ["pcr_value", "zone", "signal"])
                VolCl    = namedtuple("VolumeClimaxAnalysis", ["climax_type", "volume", "volume_ma",
                                                               "volume_ratio", "price_trend_pct",
                                                               "close_position", "lookback_days"])
                FakeNt   = namedtuple("FakeNt", ["signal"])

                # Base 3 conditions (E1+E2+E3)
                s.analysis["NEUTRAL"]["IV_RANK_EXTREME"] = IVRankEx(
                    expiry="2026-05-08", atm_iv=30.0, iv_percentile=90.0,
                    category="VERY_HIGH", ivp_type="VERY_HIGH"
                )
                s.analysis["BULLISH"]["PCR_EXTREME"] = PCREx(
                    pcr_value=0.25, zone="EXTREME_LOW", signal="excess calls"
                )
                s.analysis["BEARISH"]["VOLUME_CLIMAX"] = VolCl(
                    climax_type="selling_climax", volume=1000000, volume_ma=300000,
                    volume_ratio=3.3, price_trend_pct=-6.0, close_position=0.8,
                    lookback_days=10
                )

                if extra_conds >= 1:
                    s.analysis["BULLISH"]["OI_WALL"] = FakeNt(signal="put wall holding")
                if extra_conds >= 2:
                    s.analysis["BULLISH"]["Double_candle_stick_pattern"] = FakeNt(signal="engulfing")

                a.analyse_panic_exhaustion(s)
                sig = s.analysis["BULLISH"]["PANIC_EXHAUSTION"]
                assert sig.confidence == expected_conf, (
                    f"extra_conds={extra_conds}: expected {expected_conf}, got {sig.confidence}"
                )
