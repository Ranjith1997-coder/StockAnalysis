"""Shared fixtures for the analyser/ test suite."""
import time
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock

from common.Stock import Stock
import common.shared as shared


# ── Stock factory ─────────────────────────────────────────────────────────────

def make_stock(symbol="NIFTY", name="Nifty 50", index=False):
    s = Stock(name, symbol, is_index=index)
    s.set_prev_day_ohlcv(open=20000.0, close=20000.0, high=20200.0, low=19800.0, volume=500_000)
    return s


def make_ohlcv_df(n=60, base_close=100.0, trend="up", base_volume=100_000):
    """
    Build an n-row OHLCV DataFrame.

    trend: "up"   — each close ~+1% above previous
           "down" — each close ~−1% below previous
           "flat" — each close identical to base
    """
    closes = []
    c = base_close
    for i in range(n):
        if trend == "up":
            c = c * 1.01
        elif trend == "down":
            c = c * 0.99
        closes.append(round(c, 4))

    opens   = [c * 0.995 for c in closes]
    highs   = [c * 1.005 for c in closes]
    lows    = [c * 0.995 for c in closes]
    volumes = [base_volume] * n

    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=idx,
    )


# ── Sensibull context factories ───────────────────────────────────────────────

def make_sensibull_ctx(pcr=1.0, atm_iv=20.0, days_to_expiry=5,
                       expiry="2024-01-25", atm_iv_change=None):
    """Build a minimal sensibull_ctx dict."""
    expiry_data = {
        "atm_strike": 20000,
        "atm_iv": atm_iv,
        "atm_iv_change": atm_iv_change if atm_iv_change is not None else 0.0,
        "atm_iv_percentile": 50.0,
        "atm_ivp_type": "NORMAL",
        "future_price": 20050.0,
        "future_change_percent": 0.1,
        "max_pain_strike": 20000,
        "max_pain_value": None,
        "max_pain_type": "NEUTRAL",
        "pcr": pcr,
        "pcr_type": "NEUTRAL",
        "lot_size": 50,
        "days_to_expiry": days_to_expiry,
    }
    return {
        "last_fetch_time": None,
        "current": {
            "underlying_info": None,
            "stats": {
                "underlying_base_stats": {
                    "total_pcr": pcr,
                    "volume_spike": 1.0,
                    "volume_spike_type": "NORMAL",
                    "future_oi_change": 0.0,
                    "oi_change_type": "NEUTRAL",
                },
                "per_expiry_map": {expiry: expiry_data},
            },
            "per_expiry_map": {expiry: expiry_data},
            "nse_stats": None,
        },
        "historical_data": pd.DataFrame(),
        "oi_chain": None,
        "oi_chain_history": [],
    }


def make_oi_chain(strikes=None, spot=20000.0, expiry="2024-01-25"):
    """Build a minimal oi_chain snapshot dict."""
    if strikes is None:
        # Default: CE wall at 20200, PE wall at 19800
        strikes = {
            19800: {"call_oi": 10_000, "put_oi": 50_000, "prev_call_oi": 9000, "prev_put_oi": 45_000},
            20000: {"call_oi": 30_000, "put_oi": 30_000, "prev_call_oi": 28_000, "prev_put_oi": 28_000},
            20200: {"call_oi": 60_000, "put_oi": 8_000,  "prev_call_oi": 55_000, "prev_put_oi": 7_000},
        }
    return {
        "timestamp": None,
        "date": "2024-01-01",
        "expiry": expiry,
        "underlying_symbol": "NIFTY",
        "prev_ltp": spot * 0.99,
        "current_ltp": spot,
        "date_ltp": spot,
        "atm_strike": 20000,
        "total_call_oi": sum(v["call_oi"] for v in strikes.values()),
        "total_put_oi":  sum(v["put_oi"]  for v in strikes.values()),
        "total_call_oi_change": 0,
        "total_put_oi_change":  0,
        "pcr": 1.0,
        "per_strike_data": strikes,
        "strike_list": sorted(strikes.keys()),
        "min_strike": min(strikes),
        "max_strike": max(strikes),
        "underlying_token": 256265,
    }


# ── Mode context patches ──────────────────────────────────────────────────────

@pytest.fixture
def intraday_ctx():
    """Patch *every* shared.app_ctx reference to INTRADAY mode."""
    mock = MagicMock()
    mock.mode = shared.Mode.INTRADAY
    mock.signal_bus = None
    with patch("common.shared.app_ctx", mock):
        yield mock


@pytest.fixture
def positional_ctx():
    """Patch *every* shared.app_ctx reference to POSITIONAL mode."""
    mock = MagicMock()
    mock.mode = shared.Mode.POSITIONAL
    mock.signal_bus = None
    with patch("common.shared.app_ctx", mock):
        yield mock


def patch_ctx(mode):
    """
    Context-manager helper for tests that manage their own patching.
    Usage::
        with patch_ctx(shared.Mode.INTRADAY) as mock_ctx:
            ...
    """
    mock = MagicMock()
    mock.mode = mode
    mock.signal_bus = None
    return patch("common.shared.app_ctx", mock)
