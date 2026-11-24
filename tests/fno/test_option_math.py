"""Hand-computed tests for lib/fno/option_math.py."""
from __future__ import annotations

import math

import pytest

from lib.fno.option_math import (
    daily_expected_move,
    d2,
    expected_move_band,
    expected_move_sigma,
    iv_percentile,
    iv_rank,
    pop_condor,
    pop_short_call,
    pop_short_put,
    prob_above,
    prob_below,
    suggest_condor_strikes,
)
from tools.pinn_volatility.model.bs_utils import norm_cdf

SPOT, IV, TAU = 100.0, 0.20, 0.25


class TestExpectedMove:
    def test_daily_move_scales_with_sqrt_time(self):
        assert daily_expected_move(120.0, 2) == pytest.approx(120 / math.sqrt(2), rel=1e-9)
        assert daily_expected_move(120.0, 1) == pytest.approx(120.0)
        assert daily_expected_move(120.0, None) == pytest.approx(120.0)
        assert daily_expected_move(120.0, 4) == pytest.approx(60.0)

    @pytest.mark.parametrize("straddle", [0, -1, None])
    def test_non_positive_straddle_is_zero(self, straddle):
        assert daily_expected_move(straddle, 2) == 0.0

    def test_one_sigma_is_straddle_over_point_eight(self):
        assert expected_move_sigma(120.0) == pytest.approx(150.0)

    def test_band_is_symmetric(self):
        low, high = expected_move_band(25_000.0, 120.0)
        assert low == pytest.approx(24_850.0)
        assert high == pytest.approx(25_150.0)


class TestProbabilities:
    def test_d2_hand_value(self):
        # (0.065 - 0.5*0.04) * 0.25 / (0.2 * 0.5) = 0.01125 / 0.1
        assert d2(SPOT, SPOT, IV, TAU) == pytest.approx(0.1125, rel=1e-9)

    def test_prob_above_atm_is_slightly_above_half_with_carry(self):
        assert prob_above(SPOT, SPOT, IV, TAU) == pytest.approx(norm_cdf(0.1125), rel=1e-12)
        assert prob_below(SPOT, SPOT, IV, TAU) == pytest.approx(1 - norm_cdf(0.1125), rel=1e-12)

    def test_short_put_pop_hand_value(self):
        # K=90: d2 = (ln(100/90) + 0.01125) / 0.1
        expected = norm_cdf((math.log(100 / 90) + 0.01125) / 0.1)
        assert pop_short_put(90.0, SPOT, IV, TAU) == pytest.approx(expected, rel=1e-12)
        assert pop_short_call(110.0, SPOT, IV, TAU) == pytest.approx(
            norm_cdf(-(math.log(100 / 110) + 0.01125) / 0.1), rel=1e-12
        )

    def test_condor_pop_hand_value(self):
        above_put = norm_cdf((math.log(100 / 90) + 0.01125) / 0.1)
        above_call = norm_cdf((math.log(100 / 110) + 0.01125) / 0.1)
        assert pop_condor(90.0, 110.0, SPOT, IV, TAU) == pytest.approx(
            above_put - above_call, rel=1e-12
        )

    def test_pop_is_monotone_in_width(self):
        narrow = pop_condor(95.0, 105.0, SPOT, IV, TAU)
        wide = pop_condor(85.0, 115.0, SPOT, IV, TAU)
        assert 0 <= narrow < wide <= 1

    def test_condor_pop_below_single_leg(self):
        condor = pop_condor(90.0, 110.0, SPOT, IV, TAU)
        assert condor < pop_short_put(90.0, SPOT, IV, TAU)

    @pytest.mark.parametrize("bad", [("put>=call", 110.0, 90.0), ("zero", 0.0, 0.0)])
    def test_invalid_bounds_raise(self, bad):
        _label, put_k, call_k = bad
        with pytest.raises(ValueError):
            pop_condor(put_k, call_k, SPOT, IV, TAU)

    def test_zero_iv_raises(self):
        with pytest.raises(ValueError):
            prob_above(SPOT, 105.0, 0.0, TAU)


class TestIvStats:
    SERIES = list(range(1, 101))

    def test_percentile_counts_at_or_below(self):
        assert iv_percentile(self.SERIES, 80) == pytest.approx(80.0)
        assert iv_percentile(self.SERIES, 0) == pytest.approx(0.0)
        assert iv_percentile(self.SERIES, 1000) == pytest.approx(100.0)

    def test_rank_uses_min_max(self):
        assert iv_rank(self.SERIES, 80) == pytest.approx(100 * 79 / 99, rel=1e-9)

    def test_short_history_returns_none(self):
        assert iv_percentile([1, 2, 3], 2) is None
        assert iv_rank([1, 2, 3], 2) is None

    def test_flat_series_has_no_rank(self):
        assert iv_rank([10.0] * 30, 10.0) is None


class TestStrikeSuggestion:
    def test_suggests_the_narrowest_width_meeting_the_target(self):
        spot, gap = 25_000.0, 50.0
        iv, tau, target = 0.13, 2 / 365, 0.70

        result = suggest_condor_strikes(spot, gap, iv, tau, target)

        assert result is not None
        put_k, call_k, pop = result
        assert pop >= target
        assert call_k - spot == spot - put_k
        # one step narrower must miss the target
        narrower = pop_condor(put_k + gap, call_k - gap, spot, iv, tau)
        assert narrower < target

    def test_wider_target_needs_wider_strikes(self):
        spot, gap = 25_000.0, 50.0
        iv, tau = 0.13, 2 / 365
        narrow = suggest_condor_strikes(spot, gap, iv, tau, 0.60)
        wide = suggest_condor_strikes(spot, gap, iv, tau, 0.90)
        assert narrow and wide
        assert wide[1] - wide[0] > narrow[1] - narrow[0]

    def test_unreachable_target_returns_none(self):
        assert suggest_condor_strikes(25_000.0, 50.0, 0.13, 2 / 365, 1.0) is None

    def test_invalid_inputs_return_none(self):
        assert suggest_condor_strikes(0.0, 50.0, 0.13, 0.1, 0.7) is None
        assert suggest_condor_strikes(25_000.0, 0.0, 0.13, 0.1, 0.7) is None


class TestPopGate:
    def test_disabled_gate_never_moves_strikes(self):
        from lib.fno.option_math import widen_short_strikes

        assert widen_short_strikes(24_000, 25_000, 24_500, 50, 0.18, 2 / 365, 0.0) == (24_000, 25_000)

    def test_gate_widens_until_the_target_is_met(self):
        from lib.fno.option_math import widen_short_strikes

        assert pop_condor(24_000, 25_000, 24_500, 0.18, 2 / 365) < 0.95
        put_k, call_k = widen_short_strikes(24_000, 25_000, 24_500, 50, 0.18, 2 / 365, 0.95)
        assert (put_k, call_k) != (24_000, 25_000)
        assert pop_condor(put_k, call_k, 24_500, 0.18, 2 / 365) >= 0.95

    def test_strikes_already_above_the_target_are_kept(self):
        from lib.fno.option_math import widen_short_strikes

        assert widen_short_strikes(24_000, 25_000, 24_500, 50, 0.18, 2 / 365, 0.80) == (
            24_000, 25_000)

    def test_unreachable_target_keeps_the_original(self):
        from lib.fno.option_math import widen_short_strikes

        assert widen_short_strikes(24_000, 25_000, 24_500, 50, 0.18, 2 / 365, 0.999) == (24_000, 25_000)
