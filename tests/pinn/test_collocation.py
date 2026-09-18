"""Tests for tools/pinn_volatility/data/collocation.py — Step 4 of the PINN plan."""

import numpy as np
import torch
import pytest

from tools.pinn_volatility.data.collocation import latin_hypercube, sample_collocation


class TestLatinHypercube:
    def test_output_shape(self):
        rng = np.random.default_rng(42)
        out = latin_hypercube(100, -2.0, 2.0, rng=rng)
        assert out.shape == (100,)

    def test_within_bounds(self):
        rng = np.random.default_rng(42)
        out = latin_hypercube(500, -2.0, 2.0, rng=rng)
        assert out.min() >= -2.0
        assert out.max() <= 2.0

    def test_space_filling_one_sample_per_bin(self):
        """LHS guarantees exactly one sample lands in each of the n equal
        bins -- this is what distinguishes it from plain uniform sampling,
        which can leave gaps. Verify no bin is empty."""
        n = 50
        rng = np.random.default_rng(7)
        out = latin_hypercube(n, 0.0, 100.0, rng=rng)
        edges = np.linspace(0.0, 100.0, n + 1)
        counts, _ = np.histogram(out, bins=edges)
        assert np.all(counts == 1)

    def test_reproducible_with_seeded_rng(self):
        out1 = latin_hypercube(50, -1.0, 1.0, rng=np.random.default_rng(123))
        out2 = latin_hypercube(50, -1.0, 1.0, rng=np.random.default_rng(123))
        assert np.array_equal(out1, out2)


class TestSampleCollocation:
    def test_output_shape_and_dtype(self):
        pts = sample_collocation(n_points=200, rng=np.random.default_rng(1))
        assert pts.shape == (200, 2)
        assert pts.dtype == torch.float32

    def test_within_domain_bounds(self):
        k_range = (-2.0, 2.0)
        tau_range = (0.003, 1.0)
        pts = sample_collocation(n_points=1000, k_range=k_range, tau_range=tau_range,
                                  rng=np.random.default_rng(2))
        k, tau = pts[:, 0], pts[:, 1]
        assert k.min() >= k_range[0] - 1e-9
        assert k.max() <= k_range[1] + 1e-9
        assert tau.min() >= tau_range[0] - 1e-9
        assert tau.max() <= tau_range[1] + 1e-9

    def test_density_weighting_concentrated_near_atm(self):
        """At least concentrated_frac of points must satisfy |k| < 0.5 --
        guaranteed by construction (the concentrated block always does;
        spread points may add a few more by chance, never fewer)."""
        n_points = 2000
        concentrated_frac = 0.6
        pts = sample_collocation(n_points=n_points, concentrated_frac=concentrated_frac,
                                  rng=np.random.default_rng(3))
        k = pts[:, 0]
        near_atm_count = (k.abs() < 0.5).sum().item()
        assert near_atm_count >= int(n_points * concentrated_frac)

    def test_short_dated_concentration(self):
        n_points = 2000
        concentrated_frac = 0.6
        pts = sample_collocation(n_points=n_points, concentrated_frac=concentrated_frac,
                                  rng=np.random.default_rng(4))
        tau = pts[:, 1]
        short_dated_count = (tau < 0.15).sum().item()
        assert short_dated_count >= int(n_points * concentrated_frac)

    def test_default_call_works_without_explicit_rng(self):
        """Sanity check the production default path (global np.random) runs
        cleanly end to end, not just the seeded-rng test path."""
        pts = sample_collocation(n_points=50)
        assert pts.shape == (50, 2)

    def test_narrower_domain_clips_concentrated_zone(self):
        """If the caller passes a k_range narrower than the usual +/-0.5
        ATM band, the concentrated zone must clip to it, not exceed it."""
        narrow_k_range = (-0.3, 0.3)
        pts = sample_collocation(n_points=500, k_range=narrow_k_range,
                                  rng=np.random.default_rng(5))
        k = pts[:, 0]
        assert k.min() >= narrow_k_range[0] - 1e-9
        assert k.max() <= narrow_k_range[1] + 1e-9


class TestShortTauBoost:
    def test_zero_frac_reproduces_original_output_exactly(self):
        """short_tau_boost_frac=0.0 (default) must be bit-identical to the
        pre-boost code path -- same rng consumption, same point count."""
        pts_no_boost_kw = sample_collocation(n_points=1000, rng=np.random.default_rng(9))
        pts_explicit_zero = sample_collocation(n_points=1000, short_tau_boost_frac=0.0,
                                                rng=np.random.default_rng(9))
        assert torch.equal(pts_no_boost_kw, pts_explicit_zero)

    def test_boost_points_land_in_requested_window(self):
        n_points = 2000
        boost_frac = 0.4
        boost_range = (0.005, 0.02)
        pts = sample_collocation(n_points=n_points, short_tau_boost_frac=boost_frac,
                                  short_tau_boost_range=boost_range,
                                  rng=np.random.default_rng(6))
        tau = pts[:, 1]
        in_window = ((tau >= boost_range[0] - 1e-9) & (tau <= boost_range[1] + 1e-9)).sum().item()
        # At least the boost allocation must land there (concentrated/spread
        # zones may also contribute a few points to this narrow window by chance).
        assert in_window >= int(n_points * boost_frac)

    def test_boost_preserves_total_point_count(self):
        pts = sample_collocation(n_points=777, short_tau_boost_frac=0.4,
                                  rng=np.random.default_rng(7))
        assert pts.shape == (777, 2)

    def test_boost_spans_full_k_range_not_just_atm(self):
        """Unlike the existing ATM-concentrated zone, the boost window is
        meant to anchor the surface across all moneyness at short tau."""
        k_range = (-2.0, 2.0)
        pts = sample_collocation(n_points=2000, k_range=k_range, short_tau_boost_frac=0.4,
                                  short_tau_boost_range=(0.005, 0.02),
                                  rng=np.random.default_rng(8))
        tau = pts[:, 1]
        boosted_k = pts[(tau >= 0.005) & (tau <= 0.02), 0]
        # With 40% of 2000 = 800 points spread uniformly over (-2, 2), we
        # should see real coverage well outside the +/-0.5 ATM band.
        assert (boosted_k.abs() > 0.5).sum().item() > 0
