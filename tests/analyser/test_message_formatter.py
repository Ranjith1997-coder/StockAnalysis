"""Tests for analyser/MessageFormatter.py."""
from collections import namedtuple
from analyser.MessageFormatter import MessageFormatter


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
