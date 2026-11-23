"""Tests for analyser/OptionSellerCompositeAnalyser.py — conditions are satisfiable
from the keys/structures the upstream analysers actually emit."""
import json
from collections import namedtuple
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import common.shared as shared
from common.scoring import NotificationPriority
from services.analysis_engine.analyser.Analyser import AnalyserOrchestrator
from services.analysis_engine.analyser.MessageFormatter import MessageFormatter
from services.analysis_engine.analyser.OptionSellerCompositeAnalyser import (
    OptionSellerCompositeAnalyser,
)
from tests.analyser.conftest import make_stock

SPOT = 24000.0

IVRank = namedtuple("IVRank", ["expiry", "atm_iv", "iv_percentile", "category", "ivp_type"])
GexRegime = namedtuple("GexRegime", ["regime", "gex_total", "magnitude"])
FutAction = namedtuple("FutureActionAnalysis",
                       ["expiry", "action", "price_percentage", "oi_percentage", "score", "confidence"])
OICap = namedtuple("OICap", ["side", "unwound_pct", "top_strikes"])
IVSpike = namedtuple("IV_SPIKE", ["expiry", "iv", "iv_change", "source", "direction"])
PanicEx = namedtuple("PANIC_EXHAUSTION", ["panic_direction", "confidence", "iv_percentile"])
Candle = namedtuple("Candle", ["pattern"])


def _intraday_ctx():
    m = MagicMock()
    m.mode = shared.Mode.INTRADAY
    m.signal_bus = None
    return m


def _chain(put_walls=None, call_walls=None, base_oi=100_000, prev=None):
    """Strikes 23000..25000 step 100 with flat OI plus the given wall strikes."""
    put_walls = put_walls or {}
    call_walls = call_walls or {}
    data = {}
    strikes = sorted(set(range(23000, 25001, 100)) | set(put_walls) | set(call_walls))
    for k in strikes:
        d = {
            "call_oi": call_walls.get(k, base_oi),
            "put_oi": put_walls.get(k, base_oi),
        }
        if prev:
            d.update(prev.get(k, {}))
        data[str(float(k))] = d
    return {"per_strike_data": data, "current_ltp": SPOT}


def _index_stock(max_pain=24000, chain=None, change_pct=0.2):
    s = make_stock(symbol="NIFTY", index=True)
    s.ltp = SPOT
    s.ltp_change_perc = change_pct
    s.sensibull_ctx["current"]["stats"] = {
        "per_expiry_map": {"2024-01-25": {"max_pain_strike": max_pain}},
    }
    # Centred range: equal-distance put floor 23500 / call ceiling 24500 (OI_WALL's
    # asymmetry gate would reject this, R2 must accept it).
    s.sensibull_ctx["oi_chain"] = chain or _chain(put_walls={23500: 600_000},
                                                  call_walls={24500: 600_000})
    s.analysis["NEUTRAL"]["IV_RANK_EXTREME"] = IVRank("2024-01-25", 18.0, 82.0, "HIGH", "")
    s.analysis["NEUTRAL"]["GEX_REGIME"] = GexRegime("POSITIVE", 900.0, "MODERATE")
    return s


def _fut_df(n=30, surge=False, up=True):
    closes = [SPOT] * n
    vols = [50_000] * n
    if surge:
        vols[-1] = 200_000
        closes[-1] = SPOT * (1.004 if up else 0.996)
    idx = pd.date_range("2024-01-02 09:15", periods=n, freq="5min")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes,
                         "volume": vols, "oi": [1_000_000] * n}, index=idx)


# ── RANGE_BOUND ──────────────────────────────────────────────────────────────

class TestRangeBound:
    def test_centred_range_index_fires_iron_condor(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock()
            a = OptionSellerCompositeAnalyser()
            assert a.detect_range_bound_premium_setup(s) is True
            rb = s.analysis["NEUTRAL"]["RANGE_BOUND_SETUP"]
            assert rb["setup_type"] == "IRON_CONDOR"
            assert rb["put_wall_strike"] == 23500
            assert rb["call_wall_strike"] == 24500
            assert set(rb["conditions_detail"]) == {
                "OVERPRICED_VOL", "CEILING_AND_FLOOR", "NEUTRAL_MOMENTUM",
                "NO_INSTIT_PUSH", "MAX_PAIN_MAGNET", "GEX_POSITIVE_REGIME",
            }
            assert rb["max_pain_dev_pct"] == pytest.approx(0.0)
            assert s.analysis["PRIORITY_OVERRIDE"] == NotificationPriority.HIGH

    def test_r5_max_pain_from_per_expiry_map_without_max_pain_signal(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock(max_pain=23800)   # +0.84% → within 1.5%
            assert "MAX_PAIN" not in s.analysis["BULLISH"]
            assert "MAX_PAIN" not in s.analysis["BEARISH"]
            OptionSellerCompositeAnalyser().detect_range_bound_premium_setup(s)
            rb = s.analysis["NEUTRAL"]["RANGE_BOUND_SETUP"]
            assert "MAX_PAIN_MAGNET" in rb["conditions_detail"]
            assert rb["max_pain_dev_pct"] == pytest.approx((SPOT - 23800) / 23800 * 100)

    def test_r5_far_max_pain_fails(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock(max_pain=23000)   # 4.3% away
            OptionSellerCompositeAnalyser().detect_range_bound_premium_setup(s)
            rb = s.analysis["NEUTRAL"]["RANGE_BOUND_SETUP"]   # 5/6 still fires
            assert "MAX_PAIN_MAGNET" not in rb["conditions_detail"]

    def test_r2_requires_dominant_walls(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock(chain=_chain())   # flat chain, no wall
            a = OptionSellerCompositeAnalyser()
            assert a.detect_range_bound_premium_setup(s) is True   # 5/6
            rb = s.analysis["NEUTRAL"]["RANGE_BOUND_SETUP"]
            assert "CEILING_AND_FLOOR" not in rb["conditions_detail"]
            assert rb["setup_type"] == "STRANGLE"

    def test_r4_real_future_action_long_buildup_blocks(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock(chain=_chain(), max_pain=23000)   # R2 + R5 fail too
            s.analysis["BULLISH"]["FUTURE_ACTION"] = FutAction(
                "current", "long_buildup", 0.4, 0.6, 60, "MEDIUM")
            assert OptionSellerCompositeAnalyser().detect_range_bound_premium_setup(s) is False

    def test_r4_short_covering_is_not_a_push(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock()
            s.analysis["BULLISH"]["FUTURE_ACTION"] = FutAction(
                "current", "short_covering", 0.4, -0.6, 60, "MEDIUM")
            OptionSellerCompositeAnalyser().detect_range_bound_premium_setup(s)
            assert "NO_INSTIT_PUSH" in s.analysis["NEUTRAL"]["RANGE_BOUND_SETUP"]["conditions_detail"]

    def test_r4_buildup_marks_condition_absent(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock()
            s.analysis["BEARISH"]["FUTURE_ACTION"] = FutAction(
                "current", "short_buildup", -0.4, 0.6, 60, "MEDIUM")
            OptionSellerCompositeAnalyser().detect_range_bound_premium_setup(s)
            assert "NO_INSTIT_PUSH" not in s.analysis["NEUTRAL"]["RANGE_BOUND_SETUP"]["conditions_detail"]

    def test_suppressed_by_panic_exhaustion(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock()
            s.analysis["BULLISH"]["PANIC_EXHAUSTION"] = PanicEx("BEARISH", "HIGH", 85.0)
            assert OptionSellerCompositeAnalyser().detect_range_bound_premium_setup(s) is False

    def test_paper_trading_router_parses_output(self):
        from services.paper_trading.signal_router import parse_analysis_result
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock()
            OptionSellerCompositeAnalyser().detect_range_bound_premium_setup(s)
        fields = {"symbol": "NIFTY", "trend_found": "true", "mode": "intraday",
                  "analysis_json": json.dumps(s.analysis, default=str)}
        entries, exits = parse_analysis_result(fields)
        assert exits == []
        assert len(entries) == 1
        assert entries[0].put_wall_strike == 23500
        assert entries[0].call_wall_strike == 24500


# ── GAMMA_TRAP ───────────────────────────────────────────────────────────────

def _gamma_stock():
    s = _index_stock(chain=_chain())
    s.analysis["BULLISH"]["OI_CAPITULATION"] = OICap("CALL", 40.0, [24500])
    return s


class TestGammaTrap:
    def test_fires_with_real_futures_keys_and_futures_volume(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _gamma_stock()
            s.zerodha_ctx["futures_data"]["current"] = _fut_df(surge=True, up=True)
            s.analysis["BULLISH"]["FUTURE_ACTION"] = FutAction(
                "current", "long_buildup", 0.4, 0.6, 60, "MEDIUM")
            a = OptionSellerCompositeAnalyser()
            assert a.detect_gamma_trap_warning(s) is True
            gt = s.analysis["NEUTRAL"]["GAMMA_TRAP"]
            assert gt["direction"] == "BULLISH"
            assert gt["volume_signal"] == "FUTURES_VOLUME_SURGE"
            assert "FUTURES(FUTURE_ACTION:long_buildup)" in gt["conditions_detail"]
            assert s.analysis["PRIORITY_OVERRIDE"] == NotificationPriority.CRITICAL

    def test_next_expiry_buildup_does_not_count(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _gamma_stock()
            s.zerodha_ctx["futures_data"]["current"] = _fut_df(surge=True, up=True)
            s.analysis["BULLISH"]["FUTURE_ACTION"] = FutAction(
                "next", "long_buildup", 0.4, 0.6, 60, "MEDIUM")
            assert OptionSellerCompositeAnalyser().detect_gamma_trap_warning(s) is False

    def test_iv_crush_does_not_satisfy_g4(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _gamma_stock()
            s.analysis["BULLISH"]["FUTURE_ACTION"] = FutAction(
                "current", "long_buildup", 0.4, 0.6, 60, "MEDIUM")
            s.analysis["NEUTRAL"]["IV_CRUSH"] = IVSpike("2024-01-25", 12.0, -30.0, "x", "DOWN")
            assert OptionSellerCompositeAnalyser().detect_gamma_trap_warning(s) is False
            s.analysis["NEUTRAL"]["IV_SPIKE"] = IVSpike("2024-01-25", 25.0, 30.0, "x", "UP")
            assert OptionSellerCompositeAnalyser().detect_gamma_trap_warning(s) is True

    def test_futures_volume_against_direction_ignored(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _gamma_stock()
            s.zerodha_ctx["futures_data"]["current"] = _fut_df(surge=True, up=False)
            s.analysis["BULLISH"]["FUTURE_ACTION"] = FutAction(
                "current", "long_buildup", 0.4, 0.6, 60, "MEDIUM")
            assert OptionSellerCompositeAnalyser().detect_gamma_trap_warning(s) is False


# ── SKEW_FADE ────────────────────────────────────────────────────────────────

def _skew_stock(prev=None, support=23950):
    chain = _chain(put_walls={support: 600_000}, prev=prev)
    s = _index_stock(chain=chain)
    s.analysis["BULLISH"]["PANIC_EXHAUSTION"] = PanicEx("BEARISH", "HIGH", 85.0)
    s.analysis["BULLISH"]["Double_candle_stick_pattern"] = Candle("Bullish Engulfing")
    s.analysis["BULLISH"]["PCR_EXTREME"] = Candle("x")
    s.analysis["BULLISH"]["PCR_REVERSAL"] = Candle("x")
    return s


class TestSkewFade:
    def test_bullish_fade_at_holding_put_support(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _skew_stock()
            assert OptionSellerCompositeAnalyser().detect_skew_fade_setup(s) is True
            sf = s.analysis["NEUTRAL"]["SKEW_FADE_SETUP"]
            assert sf["fade_direction"] == "BULLISH"
            assert sf["sr_level"] == 23950
            assert sf["sr_proximity_pct"] < 0.5

    def test_support_being_unwound_is_not_holding(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _skew_stock(prev={23950: {"prev_put_oi": 1_000_000}})   # -40% put OI
            assert OptionSellerCompositeAnalyser().detect_skew_fade_setup(s) is False

    def test_support_far_from_spot_fails(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _skew_stock(support=23700)   # 1.25% away
            assert OptionSellerCompositeAnalyser().detect_skew_fade_setup(s) is False

    def test_suppressed_by_gamma_trap(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _skew_stock()
            s.analysis["NEUTRAL"]["GAMMA_TRAP_ACTIVE"] = True
            assert OptionSellerCompositeAnalyser().detect_skew_fade_setup(s) is False


# ── Consistency / dispatch ───────────────────────────────────────────────────

class TestCompositeConsistency:
    def test_skew_fade_and_range_bound_mutually_exclusive(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _skew_stock()
            OptionSellerCompositeAnalyser().run_all_index_intraday_analyses(s)
            neutral = s.analysis["NEUTRAL"]
            assert "SKEW_FADE_SETUP" in neutral
            assert "RANGE_BOUND_SETUP" not in neutral

    def test_gamma_trap_suppresses_both_and_keeps_critical(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _skew_stock()
            s.analysis["BULLISH"]["OI_CAPITULATION"] = OICap("CALL", 40.0, [24500])
            s.analysis["BULLISH"]["FUTURE_ACTION"] = FutAction(
                "current", "long_buildup", 0.4, 0.6, 60, "MEDIUM")
            s.analysis["NEUTRAL"]["IV_SPIKE"] = IVSpike("2024-01-25", 25.0, 30.0, "x", "UP")
            OptionSellerCompositeAnalyser().run_all_index_intraday_analyses(s)
            neutral = s.analysis["NEUTRAL"]
            assert "GAMMA_TRAP" in neutral
            assert "RANGE_BOUND_SETUP" not in neutral
            assert "SKEW_FADE_SETUP" not in neutral
            assert s.analysis["PRIORITY_OVERRIDE"] == NotificationPriority.CRITICAL


# ── Formatting of dict-stored composite cards ───────────────────────────────

class TestCompositeFormatting:
    def _all_cards(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            rb = _index_stock()
            OptionSellerCompositeAnalyser().detect_range_bound_premium_setup(rb)
            sf = _skew_stock()
            OptionSellerCompositeAnalyser().detect_skew_fade_setup(sf)
            gt = _gamma_stock()
            gt.zerodha_ctx["futures_data"]["current"] = _fut_df(surge=True, up=True)
            gt.analysis["BULLISH"]["FUTURE_ACTION"] = FutAction(
                "current", "long_buildup", 0.4, 0.6, 60, "MEDIUM")
            OptionSellerCompositeAnalyser().detect_gamma_trap_warning(gt)
        return {
            "RANGE_BOUND_SETUP": rb.analysis["NEUTRAL"]["RANGE_BOUND_SETUP"],
            "SKEW_FADE_SETUP": sf.analysis["NEUTRAL"]["SKEW_FADE_SETUP"],
            "GAMMA_TRAP": gt.analysis["NEUTRAL"]["GAMMA_TRAP"],
        }

    @pytest.mark.parametrize("key", ["RANGE_BOUND_SETUP", "SKEW_FADE_SETUP", "GAMMA_TRAP"])
    def test_each_card_formats_without_error(self, key):
        data = self._all_cards()[key]
        assert isinstance(data, dict)
        for payload in (data, [data, data]):
            lines = MessageFormatter.format(key, payload, "NEUTRAL")
            assert lines
            assert not any("format error" in ln for ln in lines)

    def test_gamma_card_marks_futures_volume_as_fired(self):
        data = self._all_cards()["GAMMA_TRAP"]
        text = "\n".join(MessageFormatter.format("GAMMA_TRAP", data, "NEUTRAL"))
        assert "✅ Volume" in text
        assert "✅ Futures" in text

    def test_generate_analysis_message_composite_path(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock()
            orch = AnalyserOrchestrator()
            orch.register(OptionSellerCompositeAnalyser())
            orch.run_all_intraday(s, index=True)
            msg = orch.generate_analysis_message(s)
        assert "RANGE PREMIUM" in msg
        assert "format error" not in msg


# ══════════════════════════════════════════════════════════════════════════════
# Gap-coverage tests (audit 2026-11-23): the G1 wall-migration proxy, the
# RETREAT non-signal and the stock (non-index) volume path were uncovered.
# ══════════════════════════════════════════════════════════════════════════════

WallMigration = namedtuple(
    "OIWallMigration",
    ["side", "prev_wall_strike", "curr_wall_strike", "migration_direction",
     "migration_pts", "migration_pct", "signal", "expiry"],
)
VolBreak = namedtuple(
    "VolumeBreakoutAnalysis",
    ["volume", "volume_ma", "volume_ratio", "price_change_pct", "volume_trend"],
)


class TestGammaTrapGapCoverage:
    def test_call_wall_migrating_higher_counts_as_breach(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock(chain=_chain())
            s.analysis["BULLISH"]["OI_WALL_MIGRATION"] = WallMigration(
                "CALL", 24200, 24500, "HIGHER", 300, 1.2, "ceiling raised", "2024-01-25")
            s.zerodha_ctx["futures_data"]["current"] = _fut_df(surge=True, up=True)
            s.analysis["BULLISH"]["FUTURE_ACTION"] = FutAction(
                "current", "long_buildup", 0.4, 0.6, 60, "MEDIUM")
            assert OptionSellerCompositeAnalyser().detect_gamma_trap_warning(s) is True
            gt = s.analysis["NEUTRAL"]["GAMMA_TRAP"]
            assert gt["direction"] == "BULLISH"
            assert gt["breach_signal"] == "OI_WALL_MIGRATION"

    def test_wall_retreat_is_not_a_breach(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = _index_stock(chain=_chain())
            s.analysis["NEUTRAL"]["OI_WALL_MIGRATION"] = WallMigration(
                "PUT", 24000, None, "RETREAT", None, None, "floor removed", "2024-01-25")
            s.zerodha_ctx["futures_data"]["current"] = _fut_df(surge=True, up=True)
            s.analysis["BULLISH"]["FUTURE_ACTION"] = FutAction(
                "current", "long_buildup", 0.4, 0.6, 60, "MEDIUM")
            assert OptionSellerCompositeAnalyser().detect_gamma_trap_warning(s) is False

    def test_stock_volume_breakout_satisfies_g2(self):
        with patch("common.shared.app_ctx", _intraday_ctx()):
            s = make_stock()
            s.is_index = False
            s.analysis["BULLISH"]["OI_CAPITULATION"] = OICap("CALL", 45.0, [24500])
            s.analysis["BULLISH"]["VOLUME_BREAKOUT"] = VolBreak(
                500_000, 120_000, 4.1, 2.5, "rising")
            s.analysis["BULLISH"]["FUTURE_ACTION"] = FutAction(
                "current", "long_buildup", 0.4, 0.6, 60, "MEDIUM")
            assert OptionSellerCompositeAnalyser().detect_gamma_trap_warning(s) is True
            gt = s.analysis["NEUTRAL"]["GAMMA_TRAP"]
            assert gt["volume_signal"] == "VOLUME_BREAKOUT"
            assert gt["conditions_met"] >= 3
