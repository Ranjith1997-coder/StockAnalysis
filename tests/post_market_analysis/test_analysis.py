"""Tests for post_market_analysis.analysis — PostMarketAnalyzer."""
import pytest
import pandas as pd
import datetime
from post_market_analysis.analysis import PostMarketAnalyzer


@pytest.fixture
def analyzer():
    return PostMarketAnalyzer()


# ── FII/DII analysis ──────────────────────────────────────────────────────────

class TestAnalyseFiiDii:
    def test_empty_dataframe_returns_empty_dict(self, analyzer):
        result = analyzer.analyse_fii_dii_activity(pd.DataFrame())
        assert result == {}

    def test_extracts_latest_date(self, analyzer, sample_fii_dii_df):
        result = analyzer.analyse_fii_dii_activity(sample_fii_dii_df)
        assert result["date"] == "2026-04-29"

    def test_fii_cash_inflow_bias(self, analyzer, sample_fii_dii_df):
        """Latest FII cash = +1500 → INFLOW."""
        result = analyzer.analyse_fii_dii_activity(sample_fii_dii_df)
        assert result["fii_cash_net"] == pytest.approx(1500.0)
        assert result["bias_cash"] == "INFLOW"

    def test_fii_cash_outflow_bias(self, analyzer, sample_fii_dii_df):
        """Flip signs so latest FII cash is negative → OUTFLOW."""
        df = sample_fii_dii_df.copy()
        mask = (df["level"] == "category") & (df["category_short"] == "FII CM*")
        # Make the latest date row negative
        latest = df["date"].max()
        df.loc[mask & (df["date"] == latest), "value"] = -900.0
        result = analyzer.analyse_fii_dii_activity(df)
        assert result["bias_cash"] == "OUTFLOW"

    def test_dii_cash_extracted(self, analyzer, sample_fii_dii_df):
        result = analyzer.analyse_fii_dii_activity(sample_fii_dii_df)
        # Latest DII cash = -800
        assert result["dii_cash_net"] == pytest.approx(-800.0)

    def test_five_day_rolling_sum_fii_cash(self, analyzer, sample_fii_dii_df):
        """Sum of FII cash over available dates (1500 + -200 = 1300)."""
        result = analyzer.analyse_fii_dii_activity(sample_fii_dii_df)
        assert result["fii_cash_5d_sum"] == pytest.approx(1300.0)

    def test_index_futures_net_extracted(self, analyzer, sample_fii_dii_df):
        result = analyzer.analyse_fii_dii_activity(sample_fii_dii_df)
        assert result["fii_index_fut_net"] == pytest.approx(3000.0)

    def test_bias_index_fut_long(self, analyzer, sample_fii_dii_df):
        result = analyzer.analyse_fii_dii_activity(sample_fii_dii_df)
        assert result["bias_index_fut"] == "LONG"

    def test_last5_key_present(self, analyzer, sample_fii_dii_df):
        result = analyzer.analyse_fii_dii_activity(sample_fii_dii_df)
        assert "last5" in result
        assert isinstance(result["last5"], list)
        assert len(result["last5"]) >= 1


# ── Sector Performance analysis ───────────────────────────────────────────────

class TestAnalyseSectorPerformance:
    def test_empty_dataframe_returns_empty_dict(self, analyzer):
        assert analyzer.analyse_sector_performance(pd.DataFrame()) == {}

    def test_none_returns_empty_dict(self, analyzer):
        assert analyzer.analyse_sector_performance(None) == {}

    def test_top_gainers_sorted_descending(self, analyzer, sample_sector_df):
        result = analyzer.analyse_sector_performance(sample_sector_df)
        gainers = result["top_gainers"]
        changes = [g["chg"] for g in gainers]
        assert changes == sorted(changes, reverse=True)

    def test_top_gainers_max_five(self, analyzer, sample_sector_df):
        result = analyzer.analyse_sector_performance(sample_sector_df)
        assert len(result["top_gainers"]) <= 5

    def test_top_losers_sorted_ascending(self, analyzer, sample_sector_df):
        result = analyzer.analyse_sector_performance(sample_sector_df)
        losers = result["top_losers"]
        changes = [l["chg"] for l in losers]
        assert changes == sorted(changes)

    def test_top_losers_all_negative(self, analyzer, sample_sector_df):
        result = analyzer.analyse_sector_performance(sample_sector_df)
        for l in result["top_losers"]:
            assert l["chg"] < 0

    def test_advancing_declining_counts(self, analyzer, sample_sector_df):
        result = analyzer.analyse_sector_performance(sample_sector_df)
        # 5 positive + 3 negative in sample_sector_raw
        assert result["advancing"] == 5
        assert result["declining"] == 3

    def test_total_sectors_count(self, analyzer, sample_sector_df):
        result = analyzer.analyse_sector_performance(sample_sector_df)
        assert result["total_sectors"] == 8

    def test_sector_has_name_chg_mcap_stocks(self, analyzer, sample_sector_df):
        result = analyzer.analyse_sector_performance(sample_sector_df)
        for row in result["top_gainers"] + result["top_losers"]:
            assert "name" in row
            assert "chg" in row


# ── F&O Participant OI analysis ───────────────────────────────────────────────

class TestAnalyseFoParticipantOI:
    def test_empty_dataframe_returns_empty_dict(self, analyzer):
        assert analyzer.analyse_fo_participant_oi(pd.DataFrame()) == {}

    def test_none_returns_empty_dict(self, analyzer):
        assert analyzer.analyse_fo_participant_oi(None) == {}

    def test_last5_key_present(self, analyzer, sample_fo_participant_df):
        result = analyzer.analyse_fo_participant_oi(sample_fo_participant_df)
        assert "last5" in result

    def test_at_most_five_days(self, analyzer, sample_fo_participant_df):
        result = analyzer.analyse_fo_participant_oi(sample_fo_participant_df)
        assert len(result["last5"]) <= 5

    def test_participant_keys_present_in_each_day(self, analyzer, sample_fo_participant_df):
        result = analyzer.analyse_fo_participant_oi(sample_fo_participant_df)
        for day in result["last5"]:
            for participant in ["Client", "DII", "FII", "Pro"]:
                assert participant in day, f"{participant} missing from day {day['date']}"

    def test_participant_has_net_long_short(self, analyzer, sample_fo_participant_df):
        result = analyzer.analyse_fo_participant_oi(sample_fo_participant_df)
        day = result["last5"][0]
        for participant in ["Client", "DII", "FII", "Pro"]:
            assert set(day[participant].keys()) >= {"Net", "Long", "Short"}

    def test_days_sorted_most_recent_first(self, analyzer, sample_fo_participant_df):
        result = analyzer.analyse_fo_participant_oi(sample_fo_participant_df)
        dates = [d["date"] for d in result["last5"]]
        assert dates == sorted(dates, reverse=True)


# ── Index Returns analysis ────────────────────────────────────────────────────

class TestAnalyseIndexReturns:
    def test_empty_dataframe_returns_empty_dict(self, analyzer):
        assert analyzer.analyse_index_returns(pd.DataFrame()) == {}

    def test_none_returns_empty_dict(self, analyzer):
        assert analyzer.analyse_index_returns(None) == {}

    def test_top_gainers_sorted_descending(self, analyzer, sample_index_returns_df):
        result = analyzer.analyse_index_returns(sample_index_returns_df)
        changes = [g["chg_pct"] for g in result["top_gainers"]]
        assert changes == sorted(changes, reverse=True)

    def test_top_losers_sorted_ascending(self, analyzer, sample_index_returns_df):
        result = analyzer.analyse_index_returns(sample_index_returns_df)
        changes = [l["chg_pct"] for l in result["top_losers"]]
        assert changes == sorted(changes)

    def test_top_gainers_max_ten(self, analyzer, sample_index_returns_df):
        result = analyzer.analyse_index_returns(sample_index_returns_df)
        assert len(result["top_gainers"]) <= 10

    def test_advancing_declining_counts(self, analyzer, sample_index_returns_df):
        result = analyzer.analyse_index_returns(sample_index_returns_df)
        # sample has 11 positive (3.0 down to 0.0) and 9 negative (-0.3 to -2.7)
        assert result["advancing"] + result["declining"] + result["unchanged"] == result["total_indices"]

    def test_index_row_has_required_fields(self, analyzer, sample_index_returns_df):
        result = analyzer.analyse_index_returns(sample_index_returns_df)
        for row in result["top_gainers"] + result["top_losers"]:
            assert "name" in row
            assert "chg_pct" in row


# ── Dispatch routing ──────────────────────────────────────────────────────────

class TestDispatch:
    def test_dispatch_routes_fii_dii(self, analyzer, sample_fii_dii_df):
        result = analyzer.dispatch("fii_dii_activity", sample_fii_dii_df)
        assert "fii_cash_net" in result

    def test_dispatch_routes_sector(self, analyzer, sample_sector_df):
        result = analyzer.dispatch("sector_performance", sample_sector_df)
        assert "top_gainers" in result

    def test_dispatch_routes_fo_participant(self, analyzer, sample_fo_participant_df):
        result = analyzer.dispatch("fo_participant_oi", sample_fo_participant_df)
        assert "last5" in result

    def test_dispatch_routes_index_returns(self, analyzer, sample_index_returns_df):
        result = analyzer.dispatch("index_returns", sample_index_returns_df)
        assert "top_gainers" in result

    def test_dispatch_unknown_source_returns_empty_dict(self, analyzer):
        result = analyzer.dispatch("nonexistent_source", pd.DataFrame())
        assert result == {}
