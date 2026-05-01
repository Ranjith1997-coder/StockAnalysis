"""Tests for analyser/LiveOptionsHistory.py."""
import time
import pytest
from analyser.LiveOptionsHistory import LiveOptionsHistory, OptionsSnapshot


def _make_agg(pcr=1.0, ce_oi=100_000, pe_oi=100_000, straddle=200.0,
              atm_strike=20000, ce_wall=20200, pe_wall=19800):
    return {
        "live_pcr": pcr,
        "total_ce_oi": ce_oi,
        "total_pe_oi": pe_oi,
        "atm_straddle_premium": straddle,
        "atm_strike": atm_strike,
        "max_oi_ce_strike": ce_wall,
        "max_oi_pe_strike": pe_wall,
        "net_ce_oi_change": 0,
        "net_pe_oi_change": 0,
    }


def _make_options_live(ce_wall=20200, pe_wall=19800,
                       ce_wall_oi=60_000, pe_wall_oi=50_000):
    return {
        ce_wall: {"CE": {"oi": ce_wall_oi}},
        pe_wall: {"PE": {"oi": pe_wall_oi}},
    }


def _force_snapshot(history, agg, options_live, spot):
    """Bypass the 60-s throttle by setting _last_ts to 0."""
    history._last_ts = 0.0
    return history.record(agg, options_live, spot)


class TestRecord:
    def test_first_record_saved(self):
        h = LiveOptionsHistory("NIFTY")
        result = _force_snapshot(h, _make_agg(), _make_options_live(), 20000.0)
        assert result is True
        assert h.size() == 1

    def test_throttle_blocks_second_immediate_record(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(), _make_options_live(), 20000.0)
        # Second call immediately — _last_ts is set, less than SAMPLE_INTERVAL
        result = h.record(_make_agg(), _make_options_live(), 20000.0)
        assert result is False
        assert h.size() == 1

    def test_snapshot_values_stored_correctly(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(pcr=1.23, ce_oi=200_000, pe_oi=246_000), _make_options_live(), 20100.0)
        snap = h.latest()
        assert snap.pcr == 1.23
        assert snap.total_ce_oi == 200_000
        assert snap.spot == 20100.0

    def test_ce_wall_oi_extracted(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(ce_wall=20200), _make_options_live(ce_wall=20200, ce_wall_oi=70_000), 20000.0)
        assert h.latest().ce_wall_oi == 70_000

    def test_pe_wall_oi_extracted(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(pe_wall=19800), _make_options_live(pe_wall=19800, pe_wall_oi=45_000), 20000.0)
        assert h.latest().pe_wall_oi == 45_000


class TestAccessors:
    def test_size_empty(self):
        h = LiveOptionsHistory("NIFTY")
        assert h.size() == 0

    def test_size_after_records(self):
        h = LiveOptionsHistory("NIFTY")
        for _ in range(3):
            _force_snapshot(h, _make_agg(), _make_options_live(), 20000.0)
        assert h.size() == 3

    def test_minutes_of_data_empty(self):
        h = LiveOptionsHistory("NIFTY")
        assert h.minutes_of_data() == 0.0

    def test_minutes_of_data_single_snap(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(), _make_options_live(), 20000.0)
        assert h.minutes_of_data() == 0.0

    def test_latest_returns_last(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(pcr=1.0), _make_options_live(), 20000.0)
        _force_snapshot(h, _make_agg(pcr=1.1), _make_options_live(), 20100.0)
        assert h.latest().pcr == 1.1

    def test_oldest_returns_first(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(pcr=1.0), _make_options_live(), 20000.0)
        _force_snapshot(h, _make_agg(pcr=1.1), _make_options_live(), 20100.0)
        assert h.oldest().pcr == 1.0

    def test_latest_empty_returns_none(self):
        assert LiveOptionsHistory("NIFTY").latest() is None

    def test_oldest_empty_returns_none(self):
        assert LiveOptionsHistory("NIFTY").oldest() is None


class TestLastN:
    def test_last_n_returns_n_items(self):
        h = LiveOptionsHistory("NIFTY")
        for _ in range(5):
            _force_snapshot(h, _make_agg(), _make_options_live(), 20000.0)
        assert len(h.last_n(3)) == 3

    def test_last_n_more_than_available(self):
        h = LiveOptionsHistory("NIFTY")
        for _ in range(2):
            _force_snapshot(h, _make_agg(), _make_options_live(), 20000.0)
        assert len(h.last_n(10)) == 2

    def test_last_n_most_recent(self):
        h = LiveOptionsHistory("NIFTY")
        for i in range(4):
            _force_snapshot(h, _make_agg(pcr=float(i)), _make_options_live(), 20000.0)
        last2 = h.last_n(2)
        assert last2[0].pcr == 2.0
        assert last2[1].pcr == 3.0


class TestPcrSeries:
    def test_pcr_series_returns_values(self):
        h = LiveOptionsHistory("NIFTY")
        for pcr in [1.0, 1.1, 1.2]:
            _force_snapshot(h, _make_agg(pcr=pcr), _make_options_live(), 20000.0)
        series = h.pcr_series(minutes=10000)
        assert len(series) == 3
        assert series == [1.0, 1.1, 1.2]

    def test_pcr_series_excludes_zero_pcr(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(pcr=0.0), _make_options_live(), 20000.0)
        _force_snapshot(h, _make_agg(pcr=1.0), _make_options_live(), 20000.0)
        assert 0.0 not in h.pcr_series(minutes=10000)


class TestPcrTrendSlope:
    def test_rising_pcr_slope_positive(self):
        h = LiveOptionsHistory("NIFTY")
        for pcr in [0.8, 0.9, 1.0, 1.1, 1.2]:
            _force_snapshot(h, _make_agg(pcr=pcr), _make_options_live(), 20000.0)
        slope = h.pcr_trend_slope(minutes=10000)
        assert slope is not None
        assert slope > 0

    def test_falling_pcr_slope_negative(self):
        h = LiveOptionsHistory("NIFTY")
        for pcr in [1.2, 1.1, 1.0, 0.9, 0.8]:
            _force_snapshot(h, _make_agg(pcr=pcr), _make_options_live(), 20000.0)
        slope = h.pcr_trend_slope(minutes=10000)
        assert slope is not None
        assert slope < 0

    def test_insufficient_data_returns_none(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(pcr=1.0), _make_options_live(), 20000.0)
        _force_snapshot(h, _make_agg(pcr=1.1), _make_options_live(), 20000.0)
        assert h.pcr_trend_slope(minutes=10000) is None  # needs >= 3


class TestOiSeries:
    def test_oi_series_returns_tuples(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(ce_oi=100_000, pe_oi=120_000), _make_options_live(), 20000.0)
        oi = h.oi_series(minutes=10000)
        assert oi == [(100_000, 120_000)]


class TestOiChangePct:
    def test_ce_oi_increase(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(ce_oi=100_000), _make_options_live(), 20000.0)
        _force_snapshot(h, _make_agg(ce_oi=110_000), _make_options_live(), 20000.0)
        pct = h.ce_oi_change_pct(minutes=10000)
        assert pct is not None
        assert abs(pct - 10.0) < 0.1

    def test_pe_oi_decrease(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(pe_oi=100_000), _make_options_live(), 20000.0)
        _force_snapshot(h, _make_agg(pe_oi=90_000), _make_options_live(), 20000.0)
        pct = h.pe_oi_change_pct(minutes=10000)
        assert pct is not None
        assert abs(pct - (-10.0)) < 0.1

    def test_single_snap_returns_none(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(), _make_options_live(), 20000.0)
        assert h.ce_oi_change_pct(minutes=10000) is None


class TestWallOiTrend:
    def test_stable_ce_wall_returns_tuple(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(ce_wall=20200), _make_options_live(ce_wall=20200, ce_wall_oi=60_000), 20000.0)
        _force_snapshot(h, _make_agg(ce_wall=20200), _make_options_live(ce_wall=20200, ce_wall_oi=55_000), 20000.0)
        result = h.wall_oi_trend("CE", minutes=10000)
        assert result == (60_000, 55_000)

    def test_migrated_wall_returns_none(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(ce_wall=20200), _make_options_live(ce_wall=20200, ce_wall_oi=60_000), 20000.0)
        _force_snapshot(h, _make_agg(ce_wall=20400), _make_options_live(ce_wall=20400, ce_wall_oi=70_000), 20000.0)
        assert h.wall_oi_trend("CE", minutes=10000) is None

    def test_single_snap_returns_none(self):
        h = LiveOptionsHistory("NIFTY")
        _force_snapshot(h, _make_agg(), _make_options_live(), 20000.0)
        assert h.wall_oi_trend("CE", minutes=10000) is None
