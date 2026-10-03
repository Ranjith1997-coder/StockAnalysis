"""Tests for the walk-forward benchmark harness's pure helpers.

The training path itself mirrors run_training.train_one_symbol() and is
exercised end-to-end by real runs (see docs/pinn-volatility-engine.md 0.7.3);
these cover the aggregation/reporting logic the conclusions are read from.
"""
from tools.pinn_volatility.training.benchmark import VARIANTS, summarize


def _row(wings=0.02, butterfly=0.01, accepted=True, status="ok"):
    return {"status": status, "wings_mae": wings, "butterfly_rate": butterfly,
            "accepted": accepted, "elapsed_s": 100.0}


class TestSummarize:
    def test_aggregates_ok_rows(self):
        rows = [_row(wings=0.02, accepted=True), _row(wings=0.04, accepted=False)]
        s = summarize(rows)
        assert s["n_runs"] == 2 and s["n_ok"] == 2
        assert abs(s["wings_mean"] - 0.03) < 1e-9
        assert s["wings_max"] == 0.04
        assert s["accept_rate"] == 0.5
        assert s["butterfly_max"] == 0.01

    def test_ignores_non_ok_rows(self):
        s = summarize([_row(), {"status": "no_data"}])
        assert s["n_runs"] == 2 and s["n_ok"] == 1
        assert s["accept_rate"] == 1.0

    def test_no_ok_rows(self):
        assert summarize([{"status": "no_data"}]) == {"n_runs": 0, "n_ok": 0}

    def test_single_row_has_zero_stdev(self):
        assert summarize([_row()])["wings_stdev"] == 0.0


class TestVariantDefinitions:
    def test_baseline_disables_both_weights(self):
        assert VARIANTS["baseline"]["use_moneyness_weight"] is False
        assert VARIANTS["baseline"]["use_tau_weight"] is False

    def test_moneyness_variants_set_alpha(self):
        assert VARIANTS["money5"] == {"use_moneyness_weight": True, "moneyness_alpha": 5.0}
        assert VARIANTS["money1p5"] == {"use_moneyness_weight": True, "moneyness_alpha": 1.5}

    def test_tau_variant(self):
        assert VARIANTS["tau"] == {"use_tau_weight": True}
