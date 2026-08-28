"""Tests for services/volatility_engine/comparator.py."""
import json
from datetime import date, datetime, timedelta

import pytest
import torch

from services.volatility_engine.comparator import (
    EvalResult, StrikeData, build_strikes_data, check_signal_thresholds,
    compute_tau, evaluate_model, nearest_expiry_and_forward,
)
from tools.pinn_volatility.model.bs_utils import bs_price

R, Q = 0.07, 0.0
UNDERLYING = 24500.0


def _tick(ltp, ts):
    return json.dumps({"ltp": ltp, "timestamp": str(ts)})


class TestComputeTau:
    def test_computes_calendar_day_fraction(self):
        assert compute_tau("2026-09-01", date(2026, 8, 25)) == pytest.approx(7 / 365.0)

    def test_past_expiry_is_negative_or_zero(self):
        assert compute_tau("2026-08-20", date(2026, 8, 25)) < 0


class TestNearestExpiryAndForward:
    def test_parses_nearest_sorted_expiry(self):
        payload = json.dumps({"stats": {"per_expiry_map": {
            "2026-09-01": {"future_price": 24600.0},
            "2026-08-27": {"future_price": 24550.0},
        }}})
        assert nearest_expiry_and_forward(payload) == ("2026-08-27", 24550.0)

    def test_missing_per_expiry_map_returns_none(self):
        assert nearest_expiry_and_forward(json.dumps({"stats": {}})) is None

    def test_malformed_json_returns_none(self):
        assert nearest_expiry_and_forward("not json") is None

    def test_missing_future_price_returns_none(self):
        payload = json.dumps({"stats": {"per_expiry_map": {"2026-08-27": {}}}})
        assert nearest_expiry_and_forward(payload) is None


class TestBuildStrikesData:
    def test_recovers_known_sigma_from_bs_price(self):
        now = datetime(2026, 8, 25, 10, 0, 0)
        tau = 7 / 365.0
        true_sigma = 0.12
        strike = 24500.0
        ltp = bs_price(UNDERLYING, strike, tau, R, Q, true_sigma, "CE")

        raw = {f"{strike}_CE": _tick(ltp, now)}
        results = build_strikes_data(raw, spot=UNDERLYING, forward=UNDERLYING, tau=tau, now=now)

        assert len(results) == 1
        assert results[0].live_sigma == pytest.approx(true_sigma, abs=1e-3)

    def test_skips_stale_tick(self):
        now = datetime(2026, 8, 25, 10, 0, 0)
        stale_ts = now - timedelta(seconds=30)
        raw = {"24500.0_CE": _tick(50.0, stale_ts)}
        assert build_strikes_data(raw, spot=UNDERLYING, forward=UNDERLYING, tau=0.02, now=now) == []

    def test_skips_zero_or_negative_ltp(self):
        now = datetime(2026, 8, 25, 10, 0, 0)
        raw = {"24500.0_CE": _tick(0.0, now)}
        assert build_strikes_data(raw, spot=UNDERLYING, forward=UNDERLYING, tau=0.02, now=now) == []

    def test_skips_out_of_k_range_strike(self):
        now = datetime(2026, 8, 25, 10, 0, 0)
        # strike far enough from forward that |k| > k_max=2.0
        raw = {"1.0_CE": _tick(24499.0, now)}
        assert build_strikes_data(raw, spot=UNDERLYING, forward=UNDERLYING, tau=0.02, now=now) == []

    def test_malformed_key_is_skipped_not_raised(self):
        now = datetime(2026, 8, 25, 10, 0, 0)
        raw = {"garbage_key_no_strike": _tick(50.0, now)}
        assert build_strikes_data(raw, spot=UNDERLYING, forward=UNDERLYING, tau=0.02, now=now) == []


class _StubModel:
    """Returns fixed (mu, v_squared) regardless of input, for isolating
    evaluate_model()'s z-score arithmetic from the real network."""
    def __init__(self, mu_value, v_squared_value):
        self.mu_value = mu_value
        self.v_squared_value = v_squared_value

    def __call__(self, k_tau_norm):
        n = k_tau_norm.shape[0]
        mu = torch.full((n, 1), self.mu_value)
        v_squared = torch.full((n, 1), self.v_squared_value)
        return mu, v_squared


class TestEvaluateModel:
    def test_zscore_matches_manual_formula(self):
        tau = 0.02
        sd = StrikeData(strike=24500.0, option_type="CE", ltp=50.0, k=0.01,
                         live_sigma=0.15, live_w=0.15 ** 2 * tau)
        fair_w = 0.02
        std = 0.01
        model = _StubModel(mu_value=fair_w, v_squared_value=std ** 2)

        results = evaluate_model(model, [sd], tau)

        expected_z = (sd.live_w - fair_w) / std
        assert results[0].z == pytest.approx(expected_z, rel=1e-4)

    def test_empty_strikes_returns_empty(self):
        assert evaluate_model(_StubModel(0.02, 0.0001), [], 0.02) == []


def _eval_result(k, option_type, live_sigma, tau, fair_w, uncertainty):
    sd = StrikeData(strike=24500.0 * (1 + k), option_type=option_type, ltp=1.0,
                     k=k, live_sigma=live_sigma, live_w=live_sigma ** 2 * tau)
    z = (sd.live_w - fair_w) / uncertainty
    return EvalResult(strike_data=sd, fair_w=fair_w, uncertainty=uncertainty, z=z)


class TestCheckSignalThresholds:
    def test_skew_fade_when_one_side_overpriced(self):
        tau = 0.02
        now = datetime(2026, 8, 25, 10, 0, 0)
        # CE massively overpriced (high live sigma vs low fair variance -> big z, big IV gap)
        ce = _eval_result(k=0.02, option_type="CE", live_sigma=0.30, tau=tau, fair_w=0.0002, uncertainty=0.0001)
        pe = _eval_result(k=-0.02, option_type="PE", live_sigma=0.10, tau=tau, fair_w=0.0002, uncertainty=0.01)
        atm = _eval_result(k=0.001, option_type="CE", live_sigma=0.10, tau=tau, fair_w=0.0002, uncertainty=0.01)

        signals = check_signal_thresholds("NIFTY", [ce, pe, atm], tau, "2026-09-01", now)

        assert len(signals) == 1
        assert signals[0].signal_type == "SKEW_FADE_SETUP"
        assert signals[0].direction == "BEARISH"
        assert signals[0].overpriced_type == "CE"

    def test_no_signal_when_iv_diff_below_min_threshold(self):
        """z-score alone crossing the threshold isn't enough -- the absolute
        IV gap must also clear MIN_IV_DIFF_PCT (2 points)."""
        tau = 0.02
        now = datetime(2026, 8, 25, 10, 0, 0)
        # Tiny uncertainty makes z blow up even though live/fair IV are nearly identical.
        ce = _eval_result(k=0.02, option_type="CE", live_sigma=0.121, tau=tau, fair_w=0.12 ** 2 * tau, uncertainty=1e-6)
        pe = _eval_result(k=-0.02, option_type="PE", live_sigma=0.10, tau=tau, fair_w=0.0002, uncertainty=0.01)

        signals = check_signal_thresholds("NIFTY", [ce, pe], tau, "2026-09-01", now)

        assert signals == []

    def test_range_bound_when_both_wings_overpriced_and_atm_fair(self):
        tau = 0.02
        now = datetime(2026, 8, 25, 10, 0, 0)
        ce = _eval_result(k=0.03, option_type="CE", live_sigma=0.25, tau=tau, fair_w=0.0003, uncertainty=0.0003)
        pe = _eval_result(k=-0.03, option_type="PE", live_sigma=0.25, tau=tau, fair_w=0.0003, uncertainty=0.0003)
        atm = _eval_result(k=0.001, option_type="CE", live_sigma=0.10, tau=tau, fair_w=0.0002, uncertainty=1.0)

        signals = check_signal_thresholds("NIFTY", [ce, pe, atm], tau, "2026-09-01", now)

        types = {s.signal_type for s in signals}
        assert "RANGE_BOUND_SETUP" in types

    def test_empty_results_returns_no_signals(self):
        assert check_signal_thresholds("NIFTY", [], 0.02, "2026-09-01", datetime(2026, 8, 25)) == []
