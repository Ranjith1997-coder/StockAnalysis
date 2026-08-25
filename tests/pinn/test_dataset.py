"""Tests for tools/pinn_volatility/data/dataset.py — Step 3 of the PINN plan.

Uses bs_price() to construct realistic SttlmPric values for a known sigma,
so IV-inversion round-trips can be checked against an exact expected answer
rather than an arbitrary hardcoded price.
"""

import math
import pandas as pd
import pytest

from tools.pinn_volatility.model.bs_utils import bs_price, vega as bs_vega
from tools.pinn_volatility.data.dataset import (
    build_training_samples, build_forward_price_lookup,
    samples_to_tensors, vega_tensor, train_val_split, split_by_holdout_date, TrainingSample,
)

R, Q = 0.07, 0.0


def _option_row(symbol="NIFTY", trade_date="2026-08-14", expiry="2026-08-21",
                 strike=24000.0, option_type="CE", underlying=24100.0,
                 sigma=0.15, forward=None, volume=1000):
    """Build one IDO row with SttlmPric computed via bs_price for a known
    sigma, so tests can assert the recovered sigma matches exactly."""
    from datetime import datetime
    tau = (datetime.strptime(expiry, "%Y-%m-%d").date()
           - datetime.strptime(trade_date, "%Y-%m-%d").date()).days / 365.0
    settle = bs_price(underlying, strike, tau, R, Q, sigma, option_type)
    return dict(
        TradDt=trade_date, FinInstrmTp="IDO", TckrSymb=symbol,
        XpryDt=expiry, StrkPric=strike, OptnTp=option_type,
        SttlmPric=settle, TtlTradgVol=volume, OpnIntrst=1000,
        UndrlygPric=underlying,
    )


def _future_row(symbol="NIFTY", trade_date="2026-08-14", expiry="2026-08-27",
                 settle=24150.0, underlying=24100.0, volume=500):
    return dict(
        TradDt=trade_date, FinInstrmTp="IDF", TckrSymb=symbol,
        XpryDt=expiry, StrkPric=0.0, OptnTp="",
        SttlmPric=settle, TtlTradgVol=volume, OpnIntrst=1000,
        UndrlygPric=underlying,
    )


class TestForwardPriceLookup:
    def test_matching_future_used_when_expiry_aligns(self):
        """A weekly option expiry (Aug 21) that happens to also match a
        futures expiry (Aug 21, not the usual monthly Aug 27) -- lookup
        should return exactly that future's settlement price."""
        bhavcopy = pd.DataFrame([
            _option_row(expiry="2026-08-21"),
            _future_row(expiry="2026-08-21", settle=24175.0),
        ])
        samples = build_training_samples(bhavcopy)
        assert len(samples) == 1
        assert samples[0].forward_price == pytest.approx(24175.0)
        assert samples[0].forward_source == "future"

    def test_fallback_used_when_no_matching_future_expiry(self):
        """Realistic case: option expiry (weekly, Aug 21) has NO matching
        futures row (futures only trade monthly, Aug 27) -- must fall back
        to S*e^((r-q)*tau)."""
        bhavcopy = pd.DataFrame([
            _option_row(expiry="2026-08-21", underlying=24100.0),
            _future_row(expiry="2026-08-27"),  # different expiry -- no match
        ])
        samples = build_training_samples(bhavcopy)
        assert len(samples) == 1
        tau = samples[0].tau
        expected_f = 24100.0 * math.exp((R - Q) * tau)
        assert samples[0].forward_price == pytest.approx(expected_f, rel=1e-9)
        assert samples[0].forward_source == "fallback"

    def test_build_forward_price_lookup_keys(self):
        bhavcopy = pd.DataFrame([_future_row(symbol="NIFTY", expiry="2026-08-27", settle=24150.0)])
        lookup = build_forward_price_lookup(bhavcopy)
        assert lookup[("NIFTY", "2026-08-14", "2026-08-27")] == 24150.0


class TestKAndTauComputation:
    def test_tau_computation(self):
        bhavcopy = pd.DataFrame([_option_row(trade_date="2026-08-14", expiry="2026-09-13")])  # 30 days
        samples = build_training_samples(bhavcopy)
        assert samples[0].tau == pytest.approx(30 / 365.0, abs=1e-9)

    def test_k_computation_matches_log_moneyness(self):
        # No matching future -> forward computed via fallback; verify k = ln(strike/F) exactly.
        bhavcopy = pd.DataFrame([_option_row(strike=24000.0, underlying=24250.0)])
        samples = build_training_samples(bhavcopy)
        s = samples[0]
        assert s.k == pytest.approx(math.log(s.strike / s.forward_price), abs=1e-9)


class TestFiltering:
    def test_zero_volume_excluded(self):
        bhavcopy = pd.DataFrame([_option_row(volume=0)])
        samples = build_training_samples(bhavcopy, min_volume=1)
        assert len(samples) == 0

    def test_zero_settle_price_excluded(self):
        row = _option_row()
        row["SttlmPric"] = 0.0
        bhavcopy = pd.DataFrame([row])
        samples = build_training_samples(bhavcopy)
        assert len(samples) == 0

    def test_deep_otm_strike_excluded_by_k_range(self):
        # strike/F >> e^2 -> |k| > k_max=2.0
        bhavcopy = pd.DataFrame([_option_row(strike=200000.0, underlying=24100.0, sigma=0.15)])
        samples = build_training_samples(bhavcopy, k_max=2.0)
        assert len(samples) == 0

    def test_price_below_intrinsic_excluded(self):
        """Deliberately set SttlmPric below intrinsic value -- no valid
        implied vol exists, row must be skipped, not error."""
        row = _option_row(strike=20000.0, underlying=24100.0, option_type="CE")
        row["SttlmPric"] = 100.0  # deep ITM call priced far below intrinsic (~4100)
        bhavcopy = pd.DataFrame([row])
        samples = build_training_samples(bhavcopy)
        assert len(samples) == 0

    def test_high_iv_excluded(self):
        bhavcopy = pd.DataFrame([_option_row(sigma=2.5)])  # > default max_iv=2.0
        samples = build_training_samples(bhavcopy, max_iv=2.0)
        assert len(samples) == 0

    def test_symbol_not_in_list_excluded(self):
        bhavcopy = pd.DataFrame([_option_row(symbol="FINNIFTY")])
        samples = build_training_samples(bhavcopy, symbols=["NIFTY", "BANKNIFTY"])
        assert len(samples) == 0

    def test_stock_option_type_excluded(self):
        """FinInstrmTp == STO (stock option), not IDO -- must never appear."""
        row = _option_row(symbol="NIFTY")
        row["FinInstrmTp"] = "STO"
        bhavcopy = pd.DataFrame([row])
        samples = build_training_samples(bhavcopy)
        assert len(samples) == 0


class TestIvInversionRoundTrip:
    @pytest.mark.parametrize("sigma,option_type,strike", [
        (0.15, "CE", 24000.0),
        (0.15, "PE", 24000.0),
        (0.22, "CE", 24500.0),
        (0.10, "PE", 23500.0),
    ])
    def test_recovers_known_sigma(self, sigma, option_type, strike):
        bhavcopy = pd.DataFrame([_option_row(strike=strike, option_type=option_type, sigma=sigma)])
        samples = build_training_samples(bhavcopy)
        assert len(samples) == 1
        assert samples[0].sigma == pytest.approx(sigma, abs=1e-4)

    def test_w_equals_sigma_squared_times_tau(self):
        bhavcopy = pd.DataFrame([_option_row(sigma=0.18)])
        samples = build_training_samples(bhavcopy)
        s = samples[0]
        assert s.w == pytest.approx(s.sigma ** 2 * s.tau, abs=1e-9)


class TestVegaComputation:
    def test_vega_matches_bs_utils_vega_for_recovered_sigma(self):
        bhavcopy = pd.DataFrame([_option_row(strike=24000.0, underlying=24100.0, sigma=0.15)])
        samples = build_training_samples(bhavcopy)
        s = samples[0]
        expected = bs_vega(24100.0, 24000.0, s.tau, R, Q, s.sigma)
        assert s.vega == pytest.approx(expected, rel=1e-4)

    def test_deep_otm_sample_has_small_vega_relative_to_atm(self):
        bhavcopy = pd.DataFrame([
            _option_row(strike=24000.0, underlying=24100.0, sigma=0.15, volume=1),   # ~ATM
            _option_row(strike=30000.0, underlying=24100.0, sigma=0.60, volume=1),   # deep OTM (needs high sigma for a valid IV solution this far out)
        ])
        samples = build_training_samples(bhavcopy, k_max=5.0)  # widen k_max -- this strike is intentionally far out
        assert len(samples) == 2
        atm, otm = samples[0], samples[1]
        assert otm.vega < atm.vega


class TestSamplesToTensors:
    def test_shapes_and_values(self):
        bhavcopy = pd.DataFrame([
            _option_row(strike=24000.0, sigma=0.15),
            _option_row(strike=24200.0, sigma=0.16),
        ])
        samples = build_training_samples(bhavcopy)
        k, tau, w = samples_to_tensors(samples)
        assert k.shape == (2,)
        assert tau.shape == (2,)
        assert w.shape == (2,)
        assert k.dtype.is_floating_point

    def test_vega_tensor_shape_and_order(self):
        bhavcopy = pd.DataFrame([
            _option_row(strike=24000.0, sigma=0.15),
            _option_row(strike=24200.0, sigma=0.16),
        ])
        samples = build_training_samples(bhavcopy)
        vt = vega_tensor(samples)
        assert vt.shape == (2,)
        assert vt[0].item() == pytest.approx(samples[0].vega, rel=1e-5)
        assert vt[1].item() == pytest.approx(samples[1].vega, rel=1e-5)


class TestTrainValSplit:
    def _make_samples(self, n):
        return [TrainingSample(k=i * 0.01, tau=0.1, w=0.01, sigma=0.15,
                                symbol="NIFTY", strike=24000 + i, option_type="CE",
                                expiry="2026-08-21", trade_date="2026-08-14",
                                forward_price=24100.0, forward_source="future")
                for i in range(n)]

    def test_split_proportions(self):
        samples = self._make_samples(100)
        train, val = train_val_split(samples, val_frac=0.2, seed=42)
        assert len(val) == 20
        assert len(train) == 80

    def test_no_overlap_between_train_and_val(self):
        samples = self._make_samples(50)
        train, val = train_val_split(samples, val_frac=0.3, seed=1)
        train_keys = {(s.strike, s.k) for s in train}
        val_keys = {(s.strike, s.k) for s in val}
        assert train_keys.isdisjoint(val_keys)
        assert len(train) + len(val) == 50

    def test_reproducible_with_seed(self):
        samples = self._make_samples(30)
        train1, val1 = train_val_split(samples, val_frac=0.2, seed=7)
        train2, val2 = train_val_split(samples, val_frac=0.2, seed=7)
        assert [s.strike for s in val1] == [s.strike for s in val2]

    def test_empty_input(self):
        train, val = train_val_split([], val_frac=0.2)
        assert train == []
        assert val == []


class TestSplitByHoldoutDate:
    def _make_samples_across_dates(self, dates, per_date=5):
        samples = []
        for d in dates:
            for i in range(per_date):
                samples.append(TrainingSample(
                    k=i * 0.01, tau=0.1, w=0.01, sigma=0.15,
                    symbol="NIFTY", strike=24000 + i, option_type="CE",
                    expiry="2026-08-21", trade_date=d,
                    forward_price=24100.0, forward_source="future",
                ))
        return samples

    def test_holds_out_most_recent_date(self):
        samples = self._make_samples_across_dates(
            ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"])
        train, holdout, holdout_date = split_by_holdout_date(samples)
        assert holdout_date == "2026-08-14"
        assert all(s.trade_date == "2026-08-14" for s in holdout)
        assert all(s.trade_date != "2026-08-14" for s in train)

    def test_no_overlap_and_full_coverage(self):
        samples = self._make_samples_across_dates(["2026-08-10", "2026-08-11", "2026-08-12"], per_date=4)
        train, holdout, _ = split_by_holdout_date(samples)
        assert len(train) + len(holdout) == len(samples)
        train_dates = {s.trade_date for s in train}
        holdout_dates = {s.trade_date for s in holdout}
        assert train_dates.isdisjoint(holdout_dates)

    def test_holdout_date_order_independent_of_input_order(self):
        """Dates out of chronological order in the input list must still
        correctly identify the max (most recent) date as the holdout."""
        samples = self._make_samples_across_dates(
            ["2026-08-12", "2026-08-10", "2026-08-14", "2026-08-11"])
        _, _, holdout_date = split_by_holdout_date(samples)
        assert holdout_date == "2026-08-14"

    def test_raises_with_single_date(self):
        samples = self._make_samples_across_dates(["2026-08-14"])
        with pytest.raises(ValueError):
            split_by_holdout_date(samples)

    def test_raises_with_empty_input(self):
        with pytest.raises(ValueError):
            split_by_holdout_date([])
