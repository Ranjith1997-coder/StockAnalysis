"""Shared fixtures for common/ test suite."""
import json
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

from common.Stock import Stock
import common.shared as shared


# ── Stock factory ─────────────────────────────────────────────────────────────

def make_stock(symbol="RELIANCE", name="Reliance Industries", index=False):
    """Return a minimal Stock with a simple prevDayOHLCV set."""
    s = Stock(name, symbol, is_index=index)
    s.set_prev_day_ohlcv(open=2800.0, close=2800.0, high=2850.0, low=2750.0, volume=1_000_000)
    return s


def make_price_df(closes, opens=None):
    """Return a minimal price DataFrame with Close (and optional Open) columns."""
    n = len(closes)
    data = {"Close": closes, "Open": opens if opens else closes}
    return pd.DataFrame(data)


# ── Analysis dict factories ───────────────────────────────────────────────────

def bullish_analysis(**kwargs):
    """Return an analysis dict with BULLISH entries."""
    return {"BULLISH": dict(kwargs), "BEARISH": {}, "NEUTRAL": {}, "NoOfTrends": len(kwargs)}


def bearish_analysis(**kwargs):
    return {"BULLISH": {}, "BEARISH": dict(kwargs), "NEUTRAL": {}, "NoOfTrends": len(kwargs)}


def mixed_analysis(bullish=None, bearish=None, neutral=None):
    return {
        "BULLISH": bullish or {},
        "BEARISH": bearish or {},
        "NEUTRAL": neutral or {},
        "NoOfTrends": len(bullish or {}) + len(bearish or {}) + len(neutral or {}),
    }


# ── Shared app_ctx patch ──────────────────────────────────────────────────────

@pytest.fixture
def patched_intraday_ctx():
    """Patch shared.app_ctx with INTRADAY mode."""
    with patch("common.shared.app_ctx") as mock_ctx:
        mock_ctx.mode = shared.Mode.INTRADAY
        yield mock_ctx


@pytest.fixture
def patched_positional_ctx():
    """Patch shared.app_ctx with POSITIONAL mode."""
    with patch("common.shared.app_ctx") as mock_ctx:
        mock_ctx.mode = shared.Mode.POSITIONAL
        yield mock_ctx
