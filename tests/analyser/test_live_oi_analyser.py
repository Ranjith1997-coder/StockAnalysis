"""Tests for analyser/LiveOIAnalyser.py."""
import pytest
from analyser.LiveOIAnalyser import LiveOIAnalyser
from analyser.LiveOptionsHistory import LiveOptionsHistory


def _make_agg(pcr=1.0, ce_oi=100_000, pe_oi=100_000, ce_wall=20200,
              pe_wall=19800, straddle=200.0, atm_strike=20000):
    return {
        "live_pcr": pcr,
        "total_ce_oi": ce_oi,
        "total_pe_oi": pe_oi,
        "max_oi_ce_strike": ce_wall,
        "max_oi_pe_strike": pe_wall,
        "atm_straddle_premium": straddle,
        "atm_strike": atm_strike,
        "net_ce_oi_change": 0,
        "net_pe_oi_change": 0,
    }


def _make_options_live(ce_wall=20200, ce_oi=60_000, pe_wall=19800, pe_oi=50_000):
    return {
        ce_wall: {"CE": {"oi": ce_oi}},
        pe_wall: {"PE": {"oi": pe_oi}},
    }


def _make_history():
    return LiveOptionsHistory("NIFTY")


def _force_snap(history, agg, options_live, spot):
    history._last_ts = 0.0
    history.record(agg, options_live, spot)


class TestPcrCrossover:
    def test_returns_none_before_3_ticks(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        # Only 2 ticks — no signal
        analyser.check_pcr_crossover(_make_agg(pcr=0.9))
        result = analyser.check_pcr_crossover(_make_agg(pcr=0.95))
        assert result is None

    def test_bullish_crossover_detected(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        analyser.check_pcr_crossover(_make_agg(pcr=0.95))
        analyser.check_pcr_crossover(_make_agg(pcr=0.98))
        result = analyser.check_pcr_crossover(_make_agg(pcr=1.05))  # crosses above 1.0
        assert result is not None
        assert result[0] == "PCR_CROSSOVER_BULLISH"
        assert isinstance(result[1], str)

    def test_bearish_crossover_detected(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        analyser.check_pcr_crossover(_make_agg(pcr=1.05))
        analyser.check_pcr_crossover(_make_agg(pcr=1.02))
        result = analyser.check_pcr_crossover(_make_agg(pcr=0.98))  # drops below 1.0
        assert result is not None
        assert result[0] == "PCR_CROSSOVER_BEARISH"

    def test_no_crossover_returns_none(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        analyser.check_pcr_crossover(_make_agg(pcr=1.1))
        analyser.check_pcr_crossover(_make_agg(pcr=1.15))
        result = analyser.check_pcr_crossover(_make_agg(pcr=1.2))  # stays above
        assert result is None

    def test_message_contains_symbol(self):
        analyser = LiveOIAnalyser("BANKNIFTY", 100)
        analyser.check_pcr_crossover(_make_agg(pcr=0.95))
        analyser.check_pcr_crossover(_make_agg(pcr=0.98))
        result = analyser.check_pcr_crossover(_make_agg(pcr=1.05))
        assert "BANKNIFTY" in result[1]


class TestPcrExtreme:
    def test_returns_none_with_insufficient_history(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        result = analyser.check_pcr_extreme(_make_agg(pcr=1.4))
        assert result is None  # only 1 entry from crossover check (or 0 if not called)

    def test_pe_heavy_zone_detected(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        analyser.update_pcr(1.25)  # prev below PCR_EXTREME_PE (1.3)
        analyser.update_pcr(1.35)  # curr crosses into extreme PE
        result = analyser.check_pcr_extreme(_make_agg(pcr=1.35))
        assert result is not None
        assert result[0] == "PCR_EXTREME_PE"

    def test_ce_heavy_zone_detected(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        analyser.update_pcr(0.75)
        analyser.update_pcr(0.65)  # drops into extreme CE zone
        result = analyser.check_pcr_extreme(_make_agg(pcr=0.65))
        assert result is not None
        assert result[0] == "PCR_EXTREME_CE"

    def test_normal_zone_returns_none(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        analyser.update_pcr(0.9)
        analyser.update_pcr(1.1)
        result = analyser.check_pcr_extreme(_make_agg(pcr=1.1))
        assert result is None


class TestPcrSustainedTrend:
    def test_insufficient_history_returns_none(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        h = _make_history()
        # Only 5 min of data (< PCR_SUSTAINED_MIN=15)
        for i in range(3):
            _force_snap(h, _make_agg(pcr=1.0 + i * 0.05), _make_options_live(), 20000.0)
        result = analyser.check_pcr_sustained_trend(h)
        assert result is None  # minutes_of_data < 15

    def test_sustained_bullish_trend_detected(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        h = _make_history()
        # Simulate 20 minutes of data with rising PCR
        now = __import__("time").time() - 20 * 60
        for i in range(20):
            h._last_ts = 0.0
            snap = h.record(
                _make_agg(pcr=0.90 + i * 0.02),
                _make_options_live(),
                20000.0,
            )
            h._buf[-1] = h._buf[-1].__class__(
                **{**h._buf[-1].__dict__,
                   **{"ts": now + i * 60}}
            )
        # Manually adjust timestamps so minutes_of_data >= 15
        # Rewrite using direct manipulation
        from analyser.LiveOptionsHistory import OptionsSnapshot
        import time as t
        h2 = _make_history()
        base = t.time() - 20 * 60
        for i in range(20):
            s = OptionsSnapshot(
                ts=base + i * 60, spot=20000.0,
                pcr=0.90 + i * 0.02, straddle=200.0, atm_strike=20000,
                total_ce_oi=100_000, total_pe_oi=100_000 + i * 1000,
                ce_wall=20200, pe_wall=19800, ce_wall_oi=60_000, pe_wall_oi=50_000,
                net_ce_oi_change=0, net_pe_oi_change=0,
            )
            h2._buf.append(s)
        h2._last_ts = h2._buf[-1].ts
        result = analyser.check_pcr_sustained_trend(h2)
        # PCR goes from 0.90 to 1.28, ends above 1.0, slope positive, change >= 0.10
        assert result is not None
        assert result[0] == "PCR_SUSTAINED_BULLISH"

    def test_sustained_bearish_trend_detected(self):
        from analyser.LiveOptionsHistory import OptionsSnapshot
        import time as t
        analyser = LiveOIAnalyser("NIFTY", 100)
        h = _make_history()
        base = t.time() - 20 * 60
        # PCR: 1.20 → 0.95 over 20 snaps (falls below PCR_CROSS_LEVEL=1.0)
        for i in range(20):
            s = OptionsSnapshot(
                ts=base + i * 60, spot=20000.0,
                pcr=1.20 - i * 0.02, straddle=200.0, atm_strike=20000,
                total_ce_oi=100_000, total_pe_oi=100_000,
                ce_wall=20200, pe_wall=19800, ce_wall_oi=60_000, pe_wall_oi=50_000,
                net_ce_oi_change=0, net_pe_oi_change=0,
            )
            h._buf.append(s)
        h._last_ts = h._buf[-1].ts
        result = analyser.check_pcr_sustained_trend(h)
        # PCR goes from 1.20 to 1.20-19*0.02=0.82, slope negative, last_pcr=0.82 < 1.0 ✓
        assert result is not None
        assert result[0] == "PCR_SUSTAINED_BEARISH"


class TestUpdatePcr:
    def test_positive_pcr_stored(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        analyser.update_pcr(1.2)
        assert 1.2 in list(analyser._pcr_history)

    def test_zero_pcr_ignored(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        analyser.update_pcr(0.0)
        assert len(analyser._pcr_history) == 0

    def test_negative_pcr_ignored(self):
        analyser = LiveOIAnalyser("NIFTY", 100)
        analyser.update_pcr(-0.5)
        assert len(analyser._pcr_history) == 0
