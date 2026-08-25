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
