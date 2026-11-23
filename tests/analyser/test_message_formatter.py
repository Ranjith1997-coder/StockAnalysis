"""Tests for analyser/MessageFormatter.py."""
from collections import namedtuple
from services.analysis_engine.analyser.MessageFormatter import MessageFormatter


class TestRegisteredTypes:
    def test_returns_sorted_list(self):
        types = MessageFormatter.registered_types()
        assert types == sorted(types)

    def test_volume_breakout_registered(self):
        assert "VOLUME_BREAKOUT" in MessageFormatter.registered_types()

    def test_rsi_registered(self):
        assert "RSI" in MessageFormatter.registered_types()

    def test_ema_crossover_registered(self):
        assert "EMA_CROSSOVER" in MessageFormatter.registered_types()

    def test_bollinger_band_registered(self):
        assert "BollingerBand" in MessageFormatter.registered_types()

    def test_pcr_extreme_registered(self):
        assert "PCR_EXTREME" in MessageFormatter.registered_types()

    def test_max_pain_registered(self):
        assert "MAX_PAIN" in MessageFormatter.registered_types()


class TestFormatReturnsListOfStrings:
    def test_volume_breakout_format(self):
        VB = namedtuple("VolumeBreakoutAnalysis", ["volume", "volume_ma", "volume_ratio", "price_change_pct", "volume_trend"])
        result = MessageFormatter.format("VOLUME_BREAKOUT", VB(500_000, 200_000, 2.5, 1.5, "rising"), "BULLISH")
        assert isinstance(result, list)
        assert len(result) >= 1
        assert all(isinstance(line, str) for line in result)

    def test_rsi_format(self):
        RSI = namedtuple("RSIAnalysis", ["value"])
        result = MessageFormatter.format("RSI", RSI(value=28.5), "BULLISH")
        assert isinstance(result, list)
        assert "28.5" in result[0]

    def test_bollinger_band_format(self):
        BB = namedtuple("BBAnalysis", ["close", "upper_band", "lower_band"])
        result = MessageFormatter.format("BollingerBand", BB(close=105.0, upper_band=110.0, lower_band=90.0), "BULLISH")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_ema_crossover_format(self):
        EMA = namedtuple("EMAAnalysis", ["direction", "fast_ema", "slow_ema"])
        result = MessageFormatter.format("EMA_CROSSOVER", EMA(direction="BULLISH", fast_ema=100.0, slow_ema=95.0), "BULLISH")
        assert isinstance(result, list)
        assert "BULLISH" in result[0]

    def test_pcr_extreme_format(self):
        PCR = namedtuple("PCR_EXTREME", ["pcr_value", "zone", "signal"])
        result = MessageFormatter.format("PCR_EXTREME", PCR(pcr_value=0.25, zone="EXTREME_LOW", signal="Bullish"), "BULLISH")
        assert isinstance(result, list)
        assert "0.250" in result[0]

    def test_supertrend_format(self):
        ST = namedtuple("STAnalysis", ["supertrend_value", "close", "signal"])
        result = MessageFormatter.format("SUPERTREND", ST(supertrend_value=19500.0, close=20000.0, signal="BUY"), "BULLISH")
        assert isinstance(result, list)

    def test_stochastic_format_simple(self):
        Stoch = namedtuple("StochAnalysis", ["k_value", "d_value", "signal"])
        result = MessageFormatter.format("STOCHASTIC", Stoch(k_value=15.0, d_value=18.0, signal="Oversold"), "BULLISH")
        assert isinstance(result, list)
        assert "15.0" in result[0]


class TestFallback:
    def test_unknown_type_returns_list(self):
        result = MessageFormatter.format("UNKNOWN_ANALYSIS_XYZ", "some data", "BULLISH")
        assert isinstance(result, list)
        assert len(result) == 1

    def test_unknown_type_includes_type_name(self):
        result = MessageFormatter.format("UNKNOWN_ANALYSIS_XYZ", "some data", "BULLISH")
        assert "UNKNOWN_ANALYSIS_XYZ" in result[0]

    def test_formatter_exception_returns_error_line(self):
        # Force an AttributeError by passing wrong data type for a known formatter
        result = MessageFormatter.format("RSI", "bad_data_not_namedtuple", "BULLISH")
        assert isinstance(result, list)
        assert len(result) == 1


# ══════════════════════════════════════════════════════════════════════════════
# Gap-coverage tests (audit 2026-11-23): these two formatters had 0 % coverage.
# ══════════════════════════════════════════════════════════════════════════════

class TestFutureBreakoutFormatter:
    def _data(self, pattern, oi=True, vol=True, **extra):
        fields = {
            "pattern": pattern, "orb_low": 24_950.0, "orb_high": 25_120.5,
            "oi_confirm": oi, "vol_confirm": vol,
        }
        fields.update(extra)
        return namedtuple("FutureBreakout", fields.keys())(**fields)

    def test_up_breakout_uses_rocket_and_orb_range(self):
        lines = MessageFormatter.format("FUTURE_BREAKOUT_PATTERN", self._data("orb_up_breakout"), "BULLISH")
        assert len(lines) == 1
        line = lines[0]
        assert "🚀" in line
        assert "orb_up_breakout" in line
        assert "24950.00-25120.50" in line
        assert "OI✓" in line and "Vol✓" in line

    def test_down_breakout_uses_falling_emoji(self):
        lines = MessageFormatter.format("FUTURE_BREAKOUT_PATTERN", self._data("orb_down_breakout"), "BEARISH")
        assert "📉" in lines[0]

    def test_unconfirmed_breakout_omits_confirm_tags(self):
        lines = MessageFormatter.format(
            "FUTURE_BREAKOUT_PATTERN", self._data("orb_up_breakout", oi=False, vol=False), "BULLISH"
        )
        assert "OI✓" not in lines[0]
        assert "Vol✓" not in lines[0]

    def test_score_confidence_and_mtf_suffixes(self):
        data = self._data("orb_up_breakout", score=72.5, confidence="HIGH", mtf_aligned=True)
        line = MessageFormatter.format("FUTURE_BREAKOUT_PATTERN", data, "BULLISH")[0]
        assert "72.5" in line and "HIGH" in line
        assert "🔗MTF" in line


class TestFutureOiTrendFormatter:
    def _data(self, action, oi10, oi20, price10, price20):
        return namedtuple(
            "FutureOiTrend",
            ["action", "oi_chg_10d", "oi_chg_20d", "price_chg_10d", "price_chg_20d"],
        )(action, oi10, oi20, price10, price20)

    def test_bullish_trend_shows_both_windows(self):
        lines = MessageFormatter.format(
            "FUTURE_OI_TREND", self._data("long_buildup", 12.5, 20.0, 4.0, 7.5), "BULLISH"
        )
        assert len(lines) == 1
        line = lines[0]
        assert "📈" in line
        assert "long_buildup" in line
        assert "OI 10d=+12.5%" in line and "20d=+20.0%" in line
        assert "Price 10d=+4.0%" in line and "20d=+7.5%" in line

    def test_bearish_trend_uses_falling_emoji(self):
        lines = MessageFormatter.format(
            "FUTURE_OI_TREND", self._data("short_buildup", -8.0, -15.0, -3.0, -6.0), "BEARISH"
        )
        assert "📉" in lines[0]

    def test_optional_20d_windows_are_omitted_when_missing(self):
        line = MessageFormatter.format(
            "FUTURE_OI_TREND", self._data("long_buildup", 5.0, None, 1.0, None), "BULLISH"
        )[0]
        assert "10d=+5.0%" in line and "10d=+1.0%" in line
        assert "20d=" not in line
