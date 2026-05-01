"""Tests for post_market_analysis.base — PostMarketSource abstract base class."""
import pytest
import pandas as pd
from post_market_analysis.base import PostMarketSource


# ── Concrete stub for testing ─────────────────────────────────────────────────

class _ConcreteSource(PostMarketSource):
    source_name = "test_source"

    def __init__(self, raw_data=None):
        self._raw = raw_data or [{"col": "val"}]

    def fetch_raw(self):
        return self._raw

    def normalize(self, raw):
        return pd.DataFrame(raw)


class _EmptySource(PostMarketSource):
    source_name = "empty_source"

    def fetch_raw(self):
        return []

    def normalize(self, raw):
        return pd.DataFrame()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPostMarketSource:
    def test_run_returns_dataframe(self):
        df = _ConcreteSource().run()
        assert isinstance(df, pd.DataFrame)

    def test_run_adds_source_column(self):
        df = _ConcreteSource().run()
        assert "source" in df.columns

    def test_run_source_column_value_matches_source_name(self):
        df = _ConcreteSource().run()
        assert (df["source"] == "test_source").all()

    def test_run_preserves_existing_columns(self):
        df = _ConcreteSource(raw_data=[{"col": "a"}, {"col": "b"}]).run()
        assert "col" in df.columns

    def test_run_on_empty_normalize_still_adds_source_column(self):
        # Even if normalize() returns empty, run() should add the column
        df = _EmptySource().run()
        assert "source" in df.columns

    def test_abstract_fetch_raw_enforced(self):
        """Cannot instantiate without implementing fetch_raw."""
        class _Missing(PostMarketSource):
            source_name = "x"
            def normalize(self, raw):
                return pd.DataFrame()
        with pytest.raises(TypeError):
            _Missing()

    def test_abstract_normalize_enforced(self):
        """Cannot instantiate without implementing normalize."""
        class _Missing(PostMarketSource):
            source_name = "x"
            def fetch_raw(self):
                return []
        with pytest.raises(TypeError):
            _Missing()
