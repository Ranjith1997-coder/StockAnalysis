"""
LiveStockEngine — lightweight per-tick analysis for equities.

Uses only zerodha_data fields (last_price, average_traded_price, buy/sell qty,
OHLC) — no options data needed. Emits Signal objects to the SignalBus.

Signals:
  - VWAP Cross:         Price crosses above/below exchange VWAP
  - Bid/Ask Imbalance:  Buy/sell quantity ratio at extremes
  - Opening Range Break: Price breaks the first 15-min high/low
  - Day High/Low Break:  Price makes new intraday high/low after first 30 min
"""

from __future__ import annotations
import time
import threading
from collections import defaultdict
from datetime import datetime, time as dtime

from intelligence.signal import Signal, Direction, Layer, SignalStrength
from intelligence.signal_bus import SignalBus
from common.logging_util import logger

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from common.Stock import Stock


class LiveStockEngine:
    """
    Per-tick equity analysis using WebSocket data.

    Usage:
        engine = LiveStockEngine(signal_bus)
        # On each equity tick:
        engine.on_tick(stock_obj)
    """

    # Bid/ask imbalance thresholds
    IMBALANCE_BULLISH = 2.5    # buy_qty / sell_qty > 2.5
    IMBALANCE_BEARISH = 0.4    # buy_qty / sell_qty < 0.4

    # Minimum seconds between consecutive signals of same type per symbol
    SIGNAL_COOLDOWN = 300  # 5 min

    # Throttle: min seconds between on_tick processing per symbol
    TICK_INTERVAL = 5  # process at most once every 5s per symbol

    def __init__(self, signal_bus: SignalBus):
        self._bus = signal_bus
        self._lock = threading.Lock()

        # Per-symbol state
        self._last_vwap_side: dict[str, str] = {}       # "above" / "below"
        self._orb: dict[str, dict] = {}                  # {"high": x, "low": x}
        self._orb_locked: bool = False                   # True after 9:30 AM
        self._day_high: dict[str, float] = {}
        self._day_low: dict[str, float] = {}
        self._day_high_alerted: dict[str, bool] = {}
        self._day_low_alerted: dict[str, bool] = {}

        # Cooldown tracking: (symbol, signal_type) -> last fire timestamp
        self._last_signal: dict[tuple[str, str], float] = {}

        # Tick throttle: symbol -> last processed timestamp
        self._last_tick_time: dict[str, float] = defaultdict(float)

    def on_tick(self, stock: Stock):
        """Entry point — called from _process_equity_tick for each equity tick."""
        symbol = stock.stock_symbol
        now = time.time()

        # Throttle per symbol
        if (now - self._last_tick_time[symbol]) < self.TICK_INTERVAL:
            return
        self._last_tick_time[symbol] = now

        data = stock.zerodha_data
        price = data.get("last_price", 0)
        if not price or price <= 0:
            return

        with self._lock:
            self._check_vwap_cross(symbol, price, data)
            self._check_bid_ask_imbalance(symbol, price, data)
            self._check_orb(symbol, price, data)
            self._check_day_high_low(symbol, price, data)

    def _cooldown_ok(self, symbol: str, signal_type: str) -> bool:
        last = self._last_signal.get((symbol, signal_type), 0.0)
        return (time.time() - last) >= self.SIGNAL_COOLDOWN

    def _emit(self, symbol: str, signal_type: str, direction: Direction,
              strength: SignalStrength, context: dict):
        if not self._cooldown_ok(symbol, signal_type):
            return
        self._last_signal[(symbol, signal_type)] = time.time()
        self._bus.emit(Signal(
            symbol=symbol,
            direction=direction,
            source=signal_type,
            layer=Layer.LIVE,
            strength=strength,
            context=context,
        ))

    # ── VWAP Cross ────────────────────────────────────────────────────────

    def _check_vwap_cross(self, symbol: str, price: float, data: dict):
        """Price crossing exchange VWAP — strong intraday directional signal."""
        vwap = data.get("average_traded_price", 0)
        if not vwap or vwap <= 0:
            return

        current_side = "above" if price > vwap else "below"
        prev_side = self._last_vwap_side.get(symbol)
        self._last_vwap_side[symbol] = current_side

        if prev_side and prev_side != current_side:
            direction = Direction.BULLISH if current_side == "above" else Direction.BEARISH
            self._emit(symbol, "vwap_cross", direction, SignalStrength.MODERATE,
                       {"price": price, "vwap": round(vwap, 2)})

    # ── Bid/Ask Imbalance ─────────────────────────────────────────────────

    def _check_bid_ask_imbalance(self, symbol: str, price: float, data: dict):
        """Buy/sell quantity ratio at extremes — institutional flow signal."""
        buy_q = data.get("total_buy_quantity", 0)
        sell_q = data.get("total_sell_quantity", 0)
        if not buy_q or not sell_q or sell_q <= 0 or buy_q <= 0:
            return

        ratio = buy_q / sell_q

        if ratio >= self.IMBALANCE_BULLISH:
            self._emit(symbol, "bid_ask_imbalance", Direction.BULLISH,
                       SignalStrength.MODERATE,
                       {"buy_qty": buy_q, "sell_qty": sell_q, "ratio": round(ratio, 2)})
        elif ratio <= self.IMBALANCE_BEARISH:
            self._emit(symbol, "bid_ask_imbalance", Direction.BEARISH,
                       SignalStrength.MODERATE,
                       {"buy_qty": buy_q, "sell_qty": sell_q, "ratio": round(ratio, 2)})

    # ── Opening Range Breakout ────────────────────────────────────────────

    def _check_orb(self, symbol: str, price: float, data: dict):
        """
        Track first 15-min range (9:15-9:30). After 9:30, fire on breakout.
        """
        now = datetime.now().time()

        # During first 15 min: build the opening range
        if dtime(9, 15) <= now < dtime(9, 30):
            if symbol not in self._orb:
                self._orb[symbol] = {"high": price, "low": price}
            else:
                self._orb[symbol]["high"] = max(self._orb[symbol]["high"], price)
                self._orb[symbol]["low"] = min(self._orb[symbol]["low"], price)
            return

        # After 9:30: check for breakout
        if symbol not in self._orb:
            return

        orb = self._orb[symbol]
        if price > orb["high"]:
            self._emit(symbol, "orb_breakout", Direction.BULLISH,
                       SignalStrength.STRONG,
                       {"price": price, "orb_high": orb["high"], "orb_low": orb["low"]})
        elif price < orb["low"]:
            self._emit(symbol, "orb_breakout", Direction.BEARISH,
                       SignalStrength.STRONG,
                       {"price": price, "orb_high": orb["high"], "orb_low": orb["low"]})

    # ── Day High/Low Break ────────────────────────────────────────────────

    def _check_day_high_low(self, symbol: str, price: float, data: dict):
        """
        After first 30 min (9:45+), fire when price makes a new day high/low.
        Resets the alert flag each time so subsequent new highs also fire.
        """
        now = datetime.now().time()
        high = data.get("high", 0)
        low = data.get("low", 0)

        if not high or not low:
            return

        # Track running high/low
        prev_high = self._day_high.get(symbol, 0)
        prev_low = self._day_low.get(symbol, float("inf"))
        self._day_high[symbol] = high
        self._day_low[symbol] = low

        # Only fire after first 30 min to avoid noise
        if now < dtime(9, 45):
            return

        # New day high
        if high > prev_high and prev_high > 0:
            self._emit(symbol, "day_high_break", Direction.BULLISH,
                       SignalStrength.MODERATE,
                       {"price": price, "new_high": high, "prev_high": prev_high})

        # New day low
        if low < prev_low and prev_low < float("inf"):
            self._emit(symbol, "day_low_break", Direction.BEARISH,
                       SignalStrength.MODERATE,
                       {"price": price, "new_low": low, "prev_low": prev_low})

    def reset_day(self):
        """Call at start of each trading day to reset all state."""
        with self._lock:
            self._last_vwap_side.clear()
            self._orb.clear()
            self._day_high.clear()
            self._day_low.clear()
            self._day_high_alerted.clear()
            self._day_low_alerted.clear()
            self._last_signal.clear()
            self._last_tick_time.clear()
