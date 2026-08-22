"""Tests for tools/pinn_volatility/model/bs_utils.py — Step 1 of the PINN plan."""

import math
import pytest

from tools.pinn_volatility.model.bs_utils import norm_cdf, norm_pdf, bs_price, invert_bs, vega


class TestNormCdf:
    def test_zero(self):
        assert norm_cdf(0.0) == pytest.approx(0.5, abs=1e-9)

    def test_known_value(self):
        # Φ(1.96) ≈ 0.975 (standard 95% one-sided normal quantile)
        assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)

    def test_symmetry(self):
        assert norm_cdf(-1.5) == pytest.approx(1 - norm_cdf(1.5), abs=1e-9)


class TestBsPrice:
    def test_invalid_option_type_raises(self):
        with pytest.raises(ValueError):
            bs_price(100, 100, 0.1, 0.07, 0.0, 0.2, "XX")

    def test_call_price_positive_and_reasonable(self):
        price = bs_price(S=24000, K=24000, tau=30 / 365, r=0.07, q=0.0,
                          sigma=0.15, option_type="CE")
        # ATM 30D NIFTY-scale call at 15% vol should be a small % of spot
        assert 0 < price < 24000 * 0.05

    def test_put_call_parity(self):
        S, K, tau, r, q, sigma = 24000, 24200, 30 / 365, 0.07, 0.0, 0.18
        call = bs_price(S, K, tau, r, q, sigma, "CE")
        put = bs_price(S, K, tau, r, q, sigma, "PE")
        # C - P = S*e^-qτ - K*e^-rτ  (standard put-call parity)
        lhs = call - put
        rhs = S * math.exp(-q * tau) - K * math.exp(-r * tau)
        assert lhs == pytest.approx(rhs, abs=1e-6)

    def test_zero_tau_returns_intrinsic(self):
        call = bs_price(S=24100, K=24000, tau=0.0, r=0.07, q=0.0,
                         sigma=0.15, option_type="CE")
        assert call == pytest.approx(100.0, abs=1e-6)

        put = bs_price(S=23900, K=24000, tau=0.0, r=0.07, q=0.0,
                        sigma=0.15, option_type="PE")
        assert put == pytest.approx(100.0, abs=1e-6)

    def test_zero_sigma_returns_intrinsic(self):
        call = bs_price(S=24500, K=24000, tau=30 / 365, r=0.07, q=0.0,
                         sigma=0.0, option_type="CE")
        assert call > 0  # discounted intrinsic, not zero

    def test_deep_otm_call_near_zero(self):
        call = bs_price(S=24000, K=30000, tau=7 / 365, r=0.07, q=0.0,
                         sigma=0.15, option_type="CE")
        assert call == pytest.approx(0.0, abs=0.5)


class TestInvertBs:
    @pytest.mark.parametrize("K,tau,sigma,option_type", [
        (24000, 30 / 365, 0.12, "CE"),
        (24000, 30 / 365, 0.12, "PE"),
        (24500, 7 / 365, 0.25, "CE"),
        (23500, 7 / 365, 0.25, "PE"),
        (25000, 90 / 365, 0.18, "CE"),
    ])
    def test_recovers_known_sigma(self, K, tau, sigma, option_type):
        S, r, q = 24000, 0.07, 0.0
        price = bs_price(S, K, tau, r, q, sigma, option_type)
        recovered = invert_bs(S, K, tau, r, q, price, option_type)
        assert recovered is not None
        assert recovered == pytest.approx(sigma, abs=1e-4)

    def test_price_below_intrinsic_returns_none(self):
        # Deep ITM call priced below intrinsic value -> no valid vol solution.
        S, K, tau, r, q = 25000, 24000, 30 / 365, 0.07, 0.0
        intrinsic = S - K * math.exp(-r * tau)
        bogus_price = intrinsic * 0.5
        assert invert_bs(S, K, tau, r, q, bogus_price, "CE") is None

    def test_zero_price_returns_none(self):
        assert invert_bs(24000, 24000, 30 / 365, 0.07, 0.0, 0.0, "CE") is None

    def test_zero_tau_returns_none(self):
        assert invert_bs(24000, 24000, 0.0, 0.07, 0.0, 100.0, "CE") is None

    def test_negative_price_returns_none(self):
        assert invert_bs(24000, 24000, 30 / 365, 0.07, 0.0, -5.0, "CE") is None


class TestNormPdf:
    def test_zero(self):
        assert norm_pdf(0.0) == pytest.approx(1.0 / math.sqrt(2 * math.pi), abs=1e-9)

    def test_symmetric(self):
        assert norm_pdf(1.5) == pytest.approx(norm_pdf(-1.5), abs=1e-9)

    def test_integrates_to_one_numerically(self):
        # Crude Riemann check over a wide range -- pdf must integrate to ~1.
        xs = [i * 0.01 for i in range(-1000, 1000)]
        total = sum(norm_pdf(x) * 0.01 for x in xs)
        assert total == pytest.approx(1.0, abs=1e-3)


class TestVega:
    def test_atm_vega_positive_and_symmetric_ce_pe(self):
        """Vega is identical for CE and PE at the same strike (no option_type
        argument -- same formula for both, standard BS result)."""
        v = vega(S=24000, K=24000, tau=30 / 365, r=0.07, q=0.0, sigma=0.15)
        assert v > 0

    def test_deep_otm_vega_near_zero(self):
        """This is the core premise behind Fix 3: deep OTM options have
        near-zero vega -- a small settlement-price noise implies a huge
        apparent IV swing there."""
        atm_vega = vega(S=24000, K=24000, tau=7 / 365, r=0.07, q=0.0, sigma=0.15)
        deep_otm_vega = vega(S=24000, K=30000, tau=7 / 365, r=0.07, q=0.0, sigma=0.15)
        assert deep_otm_vega < atm_vega * 0.05  # at least 20x smaller

    def test_vega_matches_finite_difference_of_bs_price(self):
        """Direct correctness check: vega must equal d(price)/d(sigma)."""
        S, K, tau, r, q, sigma = 24000, 24200, 30 / 365, 0.07, 0.0, 0.18
        eps = 1e-5
        price_plus = bs_price(S, K, tau, r, q, sigma + eps, "CE")
        price_minus = bs_price(S, K, tau, r, q, sigma - eps, "CE")
        fd_vega = (price_plus - price_minus) / (2 * eps)

        analytic_vega = vega(S, K, tau, r, q, sigma)
        assert analytic_vega == pytest.approx(fd_vega, rel=1e-4)

    def test_zero_tau_returns_zero(self):
        assert vega(24000, 24000, 0.0, 0.07, 0.0, 0.15) == 0.0

    def test_zero_sigma_returns_zero(self):
        assert vega(24000, 24000, 30 / 365, 0.07, 0.0, 0.0) == 0.0

    def test_increases_with_tau(self):
        """Longer-dated options have more time value sensitivity to vol."""
        short = vega(24000, 24000, 7 / 365, 0.07, 0.0, 0.15)
        long = vega(24000, 24000, 90 / 365, 0.07, 0.0, 0.15)
        assert long > short
