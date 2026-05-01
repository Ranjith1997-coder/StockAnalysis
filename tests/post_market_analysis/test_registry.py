"""Tests for post_market_analysis.registry — load_sources()."""
import pytest
from post_market_analysis.registry import load_sources, SOURCE_CLASSES
from post_market_analysis.base import PostMarketSource


class TestRegistry:
    def test_load_sources_returns_list(self):
        result = load_sources()
        assert isinstance(result, list)

    def test_load_sources_returns_four_sources(self):
        result = load_sources()
        assert len(result) == 4

    def test_all_expected_source_names_present(self):
        result = load_sources()
        names = {src.source_name for src in result}
        assert names == {
            "fii_dii_activity",
            "sector_performance",
            "fo_participant_oi",
            "index_returns",
        }

    def test_each_item_is_post_market_source_instance(self):
        result = load_sources()
        for src in result:
            assert isinstance(src, PostMarketSource), (
                f"{src!r} is not a PostMarketSource subclass"
            )

    def test_load_sources_returns_independent_instances(self):
        """Two calls must return different objects — no shared state."""
        first = load_sources()
        second = load_sources()
        for a, b in zip(first, second):
            assert a is not b

    def test_source_classes_constant_has_four_entries(self):
        assert len(SOURCE_CLASSES) == 4

    def test_source_classes_are_subclasses_of_base(self):
        for cls in SOURCE_CLASSES:
            assert issubclass(cls, PostMarketSource)
