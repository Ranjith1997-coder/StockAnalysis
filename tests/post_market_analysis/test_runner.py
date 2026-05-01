"""Tests for post_market_analysis.runner — pipeline orchestration."""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from post_market_analysis.runner import (
    run_post_market_pipeline,
    build_summary,
    run_and_summarize,
)
from post_market_analysis.base import PostMarketSource


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_stub_source(source_name, df=None, raises=False):
    """Build a minimal PostMarketSource stub for dependency injection."""
    class _Stub(PostMarketSource):
        def fetch_raw(self):
            return []

        def normalize(self, raw):
            return pd.DataFrame()

    stub = _Stub()
    stub.source_name = source_name

    if raises:
        stub.run = MagicMock(side_effect=RuntimeError(f"{source_name} exploded"))
    else:
        return_df = df if df is not None else pd.DataFrame({"x": [1]})
        return_df = return_df.copy()
        return_df["source"] = source_name
        stub.run = MagicMock(return_value=return_df)

    return stub


def _sample_outputs():
    return [
        {"source": "fii_dii_activity",   "rows": 10, "analysis": {}},
        {"source": "sector_performance",  "rows": 8,  "analysis": {}},
        {"source": "fo_participant_oi",   "rows": 5,  "analysis": {}},
        {"source": "index_returns",       "rows": 20, "analysis": {}},
    ]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRunPostMarketPipeline:
    def _patches(self, sources):
        """Patch load_sources and PostMarketAnalyzer.dispatch together."""
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(
            patch("post_market_analysis.runner.load_sources", return_value=sources)
        )
        mock_analyzer_cls = stack.enter_context(
            patch("post_market_analysis.runner.PostMarketAnalyzer")
        )
        mock_analyzer_cls.return_value.dispatch.return_value = {}
        return stack

    def test_pipeline_calls_run_on_each_source(self):
        stubs = [
            _make_stub_source("fii_dii_activity"),
            _make_stub_source("sector_performance"),
            _make_stub_source("fo_participant_oi"),
            _make_stub_source("index_returns"),
        ]
        with self._patches(stubs):
            run_post_market_pipeline()
        for stub in stubs:
            stub.run.assert_called_once()

    def test_pipeline_returns_outputs_and_analyzer_tuple(self):
        stubs = [_make_stub_source("fii_dii_activity")]
        with self._patches(stubs):
            result = run_post_market_pipeline()
        assert isinstance(result, tuple)
        outputs, analyzer = result
        assert isinstance(outputs, list)

    def test_pipeline_output_contains_source_name(self):
        stubs = [_make_stub_source("fii_dii_activity")]
        with self._patches(stubs):
            outputs, _ = run_post_market_pipeline()
        assert outputs[0]["source"] == "fii_dii_activity"

    def test_pipeline_output_contains_rows_count(self):
        df = pd.DataFrame({"a": [1, 2, 3]})
        stubs = [_make_stub_source("sector_performance", df=df)]
        with self._patches(stubs):
            outputs, _ = run_post_market_pipeline()
        # df has 3 rows (+1 col added by stub but row count stays 3)
        assert outputs[0]["rows"] == len(df)

    def test_pipeline_isolates_failing_source(self):
        """One source raises — others must still complete."""
        stubs = [
            _make_stub_source("fii_dii_activity", raises=True),
            _make_stub_source("sector_performance"),
            _make_stub_source("fo_participant_oi"),
        ]
        with self._patches(stubs):
            outputs, _ = run_post_market_pipeline()
        source_names = [o["source"] for o in outputs]
        assert "fii_dii_activity" not in source_names
        assert "sector_performance" in source_names
        assert "fo_participant_oi" in source_names

    def test_pipeline_all_sources_fail_returns_empty_outputs(self):
        stubs = [
            _make_stub_source("fii_dii_activity", raises=True),
            _make_stub_source("sector_performance", raises=True),
        ]
        with self._patches(stubs):
            outputs, _ = run_post_market_pipeline()
        assert outputs == []

    def test_pipeline_empty_dataframe_from_source_still_dispatched(self):
        """Empty DataFrame is allowed — dispatch returns {} but source is recorded."""
        stubs = [_make_stub_source("fo_participant_oi", df=pd.DataFrame())]
        with self._patches(stubs):
            outputs, _ = run_post_market_pipeline()
        assert len(outputs) == 1
        assert outputs[0]["rows"] == 0


class TestBuildSummary:
    def test_build_summary_returns_list(self):
        result = build_summary(_sample_outputs())
        assert isinstance(result, list)

    def test_build_summary_returns_none_for_empty_outputs(self):
        result = build_summary([])
        assert result is None

    def test_build_summary_each_item_is_string(self):
        result = build_summary(_sample_outputs())
        if result:
            for item in result:
                assert isinstance(item, str)


class TestRunAndSummarize:
    def test_run_and_summarize_calls_pipeline_and_summary(self):
        mock_outputs = _sample_outputs()
        mock_analyzer = MagicMock()

        with patch(
            "post_market_analysis.runner.run_post_market_pipeline",
            return_value=(mock_outputs, mock_analyzer)
        ):
            with patch(
                "post_market_analysis.runner.build_summary",
                return_value=["msg1", "msg2"]
            ) as mock_build:
                result = run_and_summarize()

        mock_build.assert_called_once_with(mock_outputs)
        assert result == ["msg1", "msg2"]

    def test_run_and_summarize_returns_none_when_no_outputs(self):
        with patch(
            "post_market_analysis.runner.run_post_market_pipeline",
            return_value=([], MagicMock())
        ):
            result = run_and_summarize()
        assert result is None
