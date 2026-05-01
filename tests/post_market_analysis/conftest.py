"""
Shared fixtures for post_market_analysis tests.

Provides:
- mock_response()           : factory for requests.Response mocks
- sample_fii_dii_raw        : minimal valid FII/DII JSON (2 dates, 2 categories, children)
- sample_fii_dii_df         : normalized DataFrame with expected FII/DII columns
- sample_sector_raw         : minimal sector JSON list
- sample_sector_df          : normalized sector DataFrame
- sample_fo_participant_raw : minimal F&O participant OI JSON list
- sample_fo_participant_df  : normalized F&O participant DataFrame
- sample_index_returns_raw  : minimal index returns JSON list (2 pages)
- sample_index_returns_df   : normalized index returns DataFrame
"""
import datetime
import pytest
import pandas as pd
from unittest.mock import MagicMock


# ── HTTP Mock Helper ─────────────────────────────────────────────────────────

def mock_response(json_data=None, status_code=200, raise_for_status=False):
    """Return a MagicMock that mimics a requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else []
    if raise_for_status:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


# ── FII / DII fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def sample_fii_dii_raw():
    """Two days of FII/DII data with category + child instrument breakdown."""
    return [
        {
            "Date": "2026-04-29T00:00:00",
            "FIIDIIData": [
                {
                    "Name": "FII Cash Market",
                    "ShortName": "FII CM*",
                    "Value": 1500.0,
                    "ChildData": []
                },
                {
                    "Name": "DII Cash Market",
                    "ShortName": "DII CM*",
                    "Value": -800.0,
                    "ChildData": []
                },
                {
                    "Name": "FII Index Futures",
                    "ShortName": "FII Idx Fut",
                    "Value": 3000.0,
                    "ChildData": [
                        {"Name": "NIFTY", "ShortName": "NIFTY", "Value": 2000.0},
                        {"Name": "BANKNIFTY", "ShortName": "BANKNIFTY", "Value": 1000.0}
                    ]
                },
                {
                    "Name": "FII Index Options",
                    "ShortName": "FII Idx Opt",
                    "Value": -500.0,
                    "ChildData": [
                        {"Name": "NIFTY", "ShortName": "NIFTY", "Value": -300.0},
                        {"Name": "BANKNIFTY", "ShortName": "BANKNIFTY", "Value": -200.0}
                    ]
                },
            ],
            "ClosePrice": [
                {"Symbol": "NIFTY", "C": 24000.0, "CZ": 50.0, "CZG": 0.21}
            ]
        },
        {
            "Date": "2026-04-28T00:00:00",
            "FIIDIIData": [
                {
                    "Name": "FII Cash Market",
                    "ShortName": "FII CM*",
                    "Value": -200.0,
                    "ChildData": []
                },
                {
                    "Name": "DII Cash Market",
                    "ShortName": "DII CM*",
                    "Value": 400.0,
                    "ChildData": []
                },
                {
                    "Name": "FII Index Futures",
                    "ShortName": "FII Idx Fut",
                    "Value": -1000.0,
                    "ChildData": [
                        {"Name": "NIFTY", "ShortName": "NIFTY", "Value": -700.0},
                        {"Name": "BANKNIFTY", "ShortName": "BANKNIFTY", "Value": -300.0}
                    ]
                },
                {
                    "Name": "FII Index Options",
                    "ShortName": "FII Idx Opt",
                    "Value": 200.0,
                    "ChildData": [
                        {"Name": "NIFTY", "ShortName": "NIFTY", "Value": 150.0},
                        {"Name": "BANKNIFTY", "ShortName": "BANKNIFTY", "Value": 50.0}
                    ]
                },
            ],
            "ClosePrice": []
        }
    ]


@pytest.fixture
def sample_fii_dii_df(sample_fii_dii_raw):
    """Normalized FII/DII DataFrame (via FiiDiiActivitySource.normalize)."""
    from post_market_analysis.fii_dii import FiiDiiActivitySource
    src = FiiDiiActivitySource()
    return src.normalize(sample_fii_dii_raw)


# ── Sector Performance fixtures ───────────────────────────────────────────────

@pytest.fixture
def sample_sector_raw():
    """Minimal sector performance JSON list with 5 gaining and 3 losing sectors."""
    return [
        {"ID": 1, "Name": "IT",          "McapZG": 2.5,  "Mcap": 500000, "StocksCount": 20, "IndustriesCount": 3, "Slug": "it",     "IndustriesForSector": []},
        {"ID": 2, "Name": "Banking",     "McapZG": 1.8,  "Mcap": 900000, "StocksCount": 35, "IndustriesCount": 5, "Slug": "banking","IndustriesForSector": []},
        {"ID": 3, "Name": "FMCG",        "McapZG": 0.5,  "Mcap": 300000, "StocksCount": 25, "IndustriesCount": 4, "Slug": "fmcg",   "IndustriesForSector": []},
        {"ID": 4, "Name": "Pharma",      "McapZG": 0.3,  "Mcap": 400000, "StocksCount": 30, "IndustriesCount": 6, "Slug": "pharma", "IndustriesForSector": []},
        {"ID": 5, "Name": "Auto",        "McapZG": 0.1,  "Mcap": 200000, "StocksCount": 15, "IndustriesCount": 2, "Slug": "auto",   "IndustriesForSector": []},
        {"ID": 6, "Name": "Metals",      "McapZG": -1.2, "Mcap": 150000, "StocksCount": 18, "IndustriesCount": 3, "Slug": "metals", "IndustriesForSector": []},
        {"ID": 7, "Name": "Realty",      "McapZG": -2.4, "Mcap": 80000,  "StocksCount": 12, "IndustriesCount": 2, "Slug": "realty", "IndustriesForSector": []},
        {"ID": 8, "Name": "Media",       "McapZG": -3.1, "Mcap": 60000,  "StocksCount": 8,  "IndustriesCount": 1, "Slug": "media",  "IndustriesForSector": []},
    ]


@pytest.fixture
def sample_sector_df(sample_sector_raw):
    from post_market_analysis.sector_performance import SectorPerformanceSource
    src = SectorPerformanceSource()
    return src.normalize(sample_sector_raw)


# ── F&O Participant OI fixtures ───────────────────────────────────────────────

@pytest.fixture
def sample_fo_participant_raw():
    """F&O participant OI for last 3 days, 4 participants each."""
    rows = []
    for d, date_str in enumerate(["2026-04-29", "2026-04-28", "2026-04-25"]):
        for participant, net, long, short in [
            ("Client",  -5000, 80000, 85000),
            ("DII",      1000, 10000,  9000),
            ("FII",      3000, 50000, 47000),
            ("Pro",      1000, 20000, 19000),
        ]:
            rows.append({
                "Date": date_str,
                "FoParticipantTypeName": participant,
                "Net": net + d * 100,
                "Long": long,
                "Short": short,
            })
    return rows


@pytest.fixture
def sample_fo_participant_df(sample_fo_participant_raw):
    from post_market_analysis.fo_participant_oi import FoParticipantOISource
    src = FoParticipantOISource()
    return src.normalize(sample_fo_participant_raw)


# ── Index Returns fixtures ────────────────────────────────────────────────────

@pytest.fixture
def sample_index_returns_raw():
    """20 index rows — split across two pages (10 each)."""
    names = [
        "NIFTY 50", "NIFTY BANK", "NIFTY IT", "NIFTY MIDCAP 100", "NIFTY SMALLCAP 100",
        "NIFTY AUTO", "NIFTY PHARMA", "NIFTY FMCG", "NIFTY METAL", "NIFTY REALTY",
        "NIFTY INFRA", "NIFTY ENERGY", "NIFTY MEDIA", "NIFTY PSU BANK", "NIFTY PRIVATE BANK",
        "NIFTY CONSUMPTION", "NIFTY CPSE", "NIFTY MNC", "NIFTY SERV SECTOR", "NIFTY 100",
    ]
    rows = []
    for i, name in enumerate(names):
        change_pct = 3.0 - i * 0.3   # ranges from +3.0 down to -2.7
        rows.append({
            "SecurityName": name,
            "Open": 10000.0 + i * 100,
            "High": 10100.0 + i * 100,
            "Low":   9900.0 + i * 100,
            "Close": 10050.0 + i * 100,
            "PreviousClose": 9750.0 + i * 100,
            "Change": change_pct * 100,
            "ChangePercentage": change_pct,
        })
    return rows


@pytest.fixture
def sample_index_returns_df(sample_index_returns_raw):
    from post_market_analysis.index_returns import IndexReturnsSource
    src = IndexReturnsSource()
    return src.normalize(sample_index_returns_raw)
