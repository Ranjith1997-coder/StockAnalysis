"""
Shared fixtures for zerodha test suite.
"""
import pytest
from unittest.mock import MagicMock, patch
from common.token_registry import TokenInfo, TokenType, OptionZone


# ── Stock mock ────────────────────────────────────────────────────────────────

def make_stock(symbol="NIFTY", last_price=22000.0):
    """Return a lightweight mock Stock with the attributes ZerodhaTickerManager uses."""
    stock = MagicMock()
    stock.stock_symbol = symbol
    stock.ltp = last_price
    stock.zerodha_data = {
        "last_price": last_price,
        "average_traded_price": last_price * 0.99,
        "total_buy_quantity": 10_000,
        "total_sell_quantity": 5_000,
        "high": last_price * 1.02,
        "low": last_price * 0.98,
    }
    stock.options_aggregate = {}
    stock.options_live = {}
    return stock


@pytest.fixture
def nifty_stock():
    return make_stock("NIFTY", 22000.0)


@pytest.fixture
def banknifty_stock():
    return make_stock("BANKNIFTY", 48000.0)


# ── TokenInfo builders ────────────────────────────────────────────────────────

def make_equity_info(token=1234, symbol="RELIANCE"):
    return TokenInfo(
        token=token,
        token_type=TokenType.EQUITY,
        parent_symbol=symbol,
        tradingsymbol=symbol,
    )


def make_index_info(token=256265, symbol="NIFTY"):
    return TokenInfo(
        token=token,
        token_type=TokenType.INDEX,
        parent_symbol=symbol,
        tradingsymbol=f"{symbol} 50",
    )


def make_option_info(token=99001, symbol="NIFTY", strike=22000.0, opt_type="CE"):
    return TokenInfo(
        token=token,
        token_type=TokenType.OPTION,
        parent_symbol=symbol,
        tradingsymbol=f"{symbol}26APR{int(strike)}{opt_type}",
        strike=strike,
        option_type=opt_type,
        expiry="2026-04-24",
        zone=OptionZone.CORE,
    )


def make_future_info(token=55001, symbol="NIFTY", expiry="2026-04-24"):
    return TokenInfo(
        token=token,
        token_type=TokenType.FUTURE,
        parent_symbol=symbol,
        tradingsymbol=f"{symbol}26APRFUT",
        expiry=expiry,
    )


# ── TokenRegistry mock ────────────────────────────────────────────────────────

@pytest.fixture
def mock_registry():
    reg = MagicMock()
    reg.lookup.return_value = None       # default: unknown token
    reg.get_parent_object.return_value = None
    reg.get_strike_gap.return_value = 50.0
    return reg


# ── SignalBus mock ────────────────────────────────────────────────────────────

@pytest.fixture
def mock_signal_bus():
    bus = MagicMock()
    bus.emit = MagicMock()
    return bus


# ── AppContext patch ───────────────────────────────────────────────────────────

class FakeAppCtx:
    def __init__(self, signal_bus=None, token_registry=None):
        self.stock_token_obj_dict = {}
        self.index_token_obj_dict = {}
        self.commodity_token_obj_dict = {}
        self.global_indices_token_obj_dict = {}
        self.stockExpires = []
        self.token_registry = token_registry
        self.signal_bus = signal_bus


@pytest.fixture
def fake_app_ctx(mock_registry, mock_signal_bus):
    return FakeAppCtx(signal_bus=mock_signal_bus, token_registry=mock_registry)


@pytest.fixture
def patched_app_ctx(fake_app_ctx):
    with patch("common.shared.app_ctx", fake_app_ctx):
        yield fake_app_ctx
