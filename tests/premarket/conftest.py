"""Shared fixtures and helpers for premarket tests."""
import datetime
import pandas as pd
import pytest
from unittest.mock import MagicMock


# ── Factory ───────────────────────────────────────────────────────────────────

def make_close_series(prices):
    """Return a pd.Series with a DatetimeIndex from a plain list of floats.

    Used to monkeypatch PreMarketReport._get_close_prices(ticker).
    """
    end = pd.Timestamp("2026-04-29")
    index = pd.date_range(end=end, periods=len(prices), freq="D")
    return pd.Series(prices, index=index, dtype=float)


def mock_response(json_data, status_code=200, raise_for_status=False):
    """Build a minimal requests.Response mock."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    if raise_for_status:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


# ── FII/DII raw fixture ───────────────────────────────────────────────────────

@pytest.fixture
def mock_fii_dii_raw():
    """StockEdge API response shape expected by _parse_fii_dii."""
    return [
        {
            "Date": "2026-04-29T00:00:00",
            "FIIDIIData": [
                {
                    "ShortName": "FII CM*",
                    "Name": "FII Cash Market",
                    "Value": 1500.0,
                    "ChildData": [],
                },
                {
                    "ShortName": "DII CM*",
                    "Name": "DII Cash Market",
                    "Value": -800.0,
                    "ChildData": [],
                },
                {
                    "ShortName": "FII Idx Fut",
                    "Name": "FII Index Futures",
                    "Value": 3000.0,
                    "ChildData": [
                        {"ShortName": "NIFTY", "Name": "Nifty Futures", "Value": 2000.0},
                        {"ShortName": "BANKNIFTY", "Name": "BankNifty Futures", "Value": 1000.0},
                    ],
                },
                {
                    "ShortName": "FII Idx Opt",
                    "Name": "FII Index Options",
                    "Value": -500.0,
                    "ChildData": [
                        {"ShortName": "NIFTY_OPT", "Name": "Nifty Options", "Value": -300.0},
                    ],
                },
            ],
        }
    ]


# ── NSE pre-open raw fixture ──────────────────────────────────────────────────

@pytest.fixture
def sample_preopen_data():
    """10-item pre-open data list as returned by NSE API's 'data' key.

    Contains 5 gainers, 4 losers, 1 neutral.
    One stock (IDX10) has 6x the average volume — should appear in high_volume_stocks.
    """
    base_vol = 100_000
    stocks = [
        # gainers
        {"symbol": "RELIANCE",  "finalPrice": 2900.0, "previousClose": 2800.0,
         "pChange":  3.57, "finalQuantity": base_vol,     "totalBuyQuantity": 80_000, "totalSellQuantity": 20_000},
        {"symbol": "TCS",       "finalPrice": 3600.0, "previousClose": 3500.0,
         "pChange":  2.86, "finalQuantity": base_vol,     "totalBuyQuantity": 70_000, "totalSellQuantity": 30_000},
        {"symbol": "HDFC",      "finalPrice": 1650.0, "previousClose": 1600.0,
         "pChange":  3.13, "finalQuantity": base_vol,     "totalBuyQuantity": 60_000, "totalSellQuantity": 40_000},
        {"symbol": "INFY",      "finalPrice": 1600.0, "previousClose": 1560.0,
         "pChange":  2.56, "finalQuantity": base_vol,     "totalBuyQuantity": 55_000, "totalSellQuantity": 45_000},
        {"symbol": "ICICI",     "finalPrice": 1100.0, "previousClose": 1080.0,
         "pChange":  1.85, "finalQuantity": base_vol,     "totalBuyQuantity": 50_000, "totalSellQuantity": 50_000},
        # losers
        {"symbol": "WIPRO",     "finalPrice":  490.0, "previousClose":  510.0,
         "pChange": -3.92, "finalQuantity": base_vol,     "totalBuyQuantity": 30_000, "totalSellQuantity": 70_000},
        {"symbol": "TITAN",     "finalPrice": 3200.0, "previousClose": 3280.0,
         "pChange": -2.44, "finalQuantity": base_vol,     "totalBuyQuantity": 35_000, "totalSellQuantity": 65_000},
        {"symbol": "BAJFIN",    "finalPrice": 6800.0, "previousClose": 6900.0,
         "pChange": -1.45, "finalQuantity": base_vol,     "totalBuyQuantity": 40_000, "totalSellQuantity": 60_000},
        {"symbol": "ASIANPNT",  "finalPrice": 2900.0, "previousClose": 2940.0,
         "pChange": -1.36, "finalQuantity": base_vol,     "totalBuyQuantity": 42_000, "totalSellQuantity": 58_000},
        # high volume outlier (6× avg ≈ 600 000 >> 2.5× threshold of ~150 000)
        {"symbol": "IDX10",     "finalPrice":  500.0, "previousClose":  495.0,
         "pChange":  1.01, "finalQuantity": base_vol * 6, "totalBuyQuantity": 200_000, "totalSellQuantity": 100_000},
    ]
    return [{"metadata": s} for s in stocks]
