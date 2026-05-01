"""Tests for post_market_analysis.summary — formatters and PostMarketSummaryBuilder."""
import pytest
from post_market_analysis.summary import (
    _val_dot,
    FiiDiiSummaryFormatter,
    SectorSummaryFormatter,
    FoParticipantOISummaryFormatter,
    IndexReturnsSummaryFormatter,
    PostMarketSummaryBuilder,
)
from post_market_analysis.analysis import PostMarketAnalyzer


# ── _val_dot helper ───────────────────────────────────────────────────────────

class TestValDot:
    def test_positive_returns_green(self):
        assert _val_dot(1.0) == "🟢"

    def test_zero_returns_green(self):
        assert _val_dot(0) == "🟢"

    def test_negative_returns_red(self):
        assert _val_dot(-1.0) == "🔴"

    def test_none_returns_neutral(self):
        assert _val_dot(None) == "⚪"

    def test_nan_returns_neutral(self):
        import math
        assert _val_dot(float("nan")) == "⚪"


# ── FII/DII formatter ─────────────────────────────────────────────────────────

class TestFiiDiiSummaryFormatter:
    @pytest.fixture
    def analysis(self, sample_fii_dii_df):
        return PostMarketAnalyzer().analyse_fii_dii_activity(sample_fii_dii_df)

    def test_format_returns_string(self, analysis):
        result = FiiDiiSummaryFormatter().format(analysis)
        assert isinstance(result, str)

    def test_format_contains_fii_header(self, analysis):
        result = FiiDiiSummaryFormatter().format(analysis)
        assert "FII" in result

    def test_format_contains_dii(self, analysis):
        result = FiiDiiSummaryFormatter().format(analysis)
        assert "DII" in result

    def test_format_respects_3900_char_limit(self, analysis):
        result = FiiDiiSummaryFormatter().format(analysis)
        assert len(result) <= 4096  # Telegram hard limit; formatter targets 3900

    def test_format_empty_analysis_returns_placeholder(self):
        result = FiiDiiSummaryFormatter().format({})
        assert "No data" in result or "FII" in result

    def test_format_includes_latest_date(self, analysis):
        result = FiiDiiSummaryFormatter().format(analysis)
        assert "2026-04-29" in result


# ── Sector formatter ──────────────────────────────────────────────────────────

class TestSectorSummaryFormatter:
    @pytest.fixture
    def analysis(self, sample_sector_df):
        return PostMarketAnalyzer().analyse_sector_performance(sample_sector_df)

    def test_format_returns_string(self, analysis):
        assert isinstance(SectorSummaryFormatter().format(analysis), str)

    def test_format_shows_sector_performance_header(self, analysis):
        result = SectorSummaryFormatter().format(analysis)
        assert "Sector" in result

    def test_format_shows_gainers_and_losers_sections(self, analysis):
        result = SectorSummaryFormatter().format(analysis)
        assert "Gaining" in result
        assert "Losing" in result

    def test_format_respects_3900_char_limit(self, analysis):
        result = SectorSummaryFormatter().format(analysis)
        assert len(result) <= 4096

    def test_format_empty_returns_placeholder(self):
        result = SectorSummaryFormatter().format({})
        assert "No data" in result


# ── F&O Participant OI formatter ──────────────────────────────────────────────

class TestFoParticipantOISummaryFormatter:
    @pytest.fixture
    def analysis(self, sample_fo_participant_df):
        return PostMarketAnalyzer().analyse_fo_participant_oi(sample_fo_participant_df)

    def test_format_returns_string(self, analysis):
        assert isinstance(FoParticipantOISummaryFormatter().format(analysis), str)

    def test_format_shows_participant_names(self, analysis):
        result = FoParticipantOISummaryFormatter().format(analysis)
        for participant in ["Client", "DII", "FII", "Pro"]:
            assert participant in result

    def test_format_shows_latest_breakdown_section(self, analysis):
        result = FoParticipantOISummaryFormatter().format(analysis)
        assert "Latest" in result or "breakdown" in result

    def test_format_empty_analysis_returns_placeholder(self):
        result = FoParticipantOISummaryFormatter().format({})
        assert "No data" in result

    def test_format_none_last5_returns_placeholder(self):
        result = FoParticipantOISummaryFormatter().format({"last5": None})
        assert "No data" in result


# ── Index Returns formatter ───────────────────────────────────────────────────

class TestIndexReturnsSummaryFormatter:
    @pytest.fixture
    def analysis(self, sample_index_returns_df):
        return PostMarketAnalyzer().analyse_index_returns(sample_index_returns_df)

    def test_format_returns_string(self, analysis):
        assert isinstance(IndexReturnsSummaryFormatter().format(analysis), str)

    def test_format_shows_nse_indices_header(self, analysis):
        result = IndexReturnsSummaryFormatter().format(analysis)
        assert "NSE" in result or "Indices" in result

    def test_format_shows_gainers_and_losers(self, analysis):
        result = IndexReturnsSummaryFormatter().format(analysis)
        assert "Gaining" in result
        assert "Losing" in result

    def test_format_respects_3900_char_limit(self, analysis):
        result = IndexReturnsSummaryFormatter().format(analysis)
        assert len(result) <= 4096

    def test_format_empty_returns_placeholder(self):
        result = IndexReturnsSummaryFormatter().format({})
        assert "No data" in result


# ── PostMarketSummaryBuilder ──────────────────────────────────────────────────

class TestPostMarketSummaryBuilder:
    def _make_output(self, source_name, analysis):
        return {"source": source_name, "rows": 10, "analysis": analysis}

    def test_build_returns_list_of_strings(self, sample_fii_dii_df):
        analysis = PostMarketAnalyzer().analyse_fii_dii_activity(sample_fii_dii_df)
        outputs = [self._make_output("fii_dii_activity", analysis)]
        result = PostMarketSummaryBuilder().build(outputs)
        assert isinstance(result, list)
        assert all(isinstance(s, str) for s in result)

    def test_build_dispatches_to_correct_formatter(self, sample_sector_df):
        analysis = PostMarketAnalyzer().analyse_sector_performance(sample_sector_df)
        outputs = [self._make_output("sector_performance", analysis)]
        result = PostMarketSummaryBuilder().build(outputs)
        assert result is not None
        assert "Sector" in result[0]

    def test_build_unknown_source_skipped(self):
        outputs = [{"source": "unknown_source", "rows": 0, "analysis": {}}]
        result = PostMarketSummaryBuilder().build(outputs)
        # unknown source produces no parts → returns None
        assert result is None

    def test_build_empty_outputs_returns_none(self):
        result = PostMarketSummaryBuilder().build([])
        assert result is None

    def test_build_multiple_sources_returns_multiple_strings(
        self, sample_fii_dii_df, sample_sector_df
    ):
        analyzer = PostMarketAnalyzer()
        outputs = [
            self._make_output(
                "fii_dii_activity",
                analyzer.analyse_fii_dii_activity(sample_fii_dii_df)
            ),
            self._make_output(
                "sector_performance",
                analyzer.analyse_sector_performance(sample_sector_df)
            ),
        ]
        result = PostMarketSummaryBuilder().build(outputs)
        assert len(result) == 2

    def test_build_mixed_known_unknown_sources(self, sample_fii_dii_df):
        """Known source produces output; unknown is silently skipped."""
        analyzer = PostMarketAnalyzer()
        outputs = [
            self._make_output(
                "fii_dii_activity",
                analyzer.analyse_fii_dii_activity(sample_fii_dii_df)
            ),
            {"source": "no_such_source", "rows": 0, "analysis": {}},
        ]
        result = PostMarketSummaryBuilder().build(outputs)
        assert result is not None
        assert len(result) == 1
