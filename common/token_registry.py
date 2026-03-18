from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set, Tuple
from common.logging_util import logger
import threading
import math


class TokenType(Enum):
    EQUITY = "equity"
    INDEX = "index"
    OPTION = "option"
    FUTURE = "future"
    COMMODITY = "commodity"
    GLOBAL_INDEX = "global_index"


class OptionZone(Enum):
    CORE = "core"           # ATM ± 1% → MODE_FULL (depth + OI + volume)
    ACTIVE = "active"       # 1-3%     → MODE_QUOTE (OI + volume, no depth)
    PERIPHERAL = "peripheral"  # 3-5%  → MODE_LTP  (price only)


# Zerodha WebSocket modes (mirrored from KiteTicker for convenience)
WS_MODE_FULL = "full"
WS_MODE_QUOTE = "quote"
WS_MODE_LTP = "ltp"

# MODE_FULL (184 bytes) = OI + Volume + Depth (5 levels)
# MODE_QUOTE (44 bytes) = Volume only, NO OI, NO Depth
# MODE_LTP (8 bytes)    = Price only
#
# OI is critical for option strategies (OI walls, PCR, buildup detection),
# so both CORE and ACTIVE zones use MODE_FULL.
# PERIPHERAL uses MODE_QUOTE for volume data without the depth overhead.
ZONE_TO_WS_MODE = {
    OptionZone.CORE: WS_MODE_FULL,        # OI + Volume + Depth
    OptionZone.ACTIVE: WS_MODE_FULL,       # OI + Volume + Depth
    OptionZone.PERIPHERAL: WS_MODE_QUOTE,  # Volume only (for unusual activity detection)
}


@dataclass
class TokenInfo:
    token: int
    token_type: TokenType
    parent_symbol: str          # "NIFTY", "HDFCBANK", etc.
    tradingsymbol: str          # "NIFTY26MAR24000CE" or "HDFCBANK"
    strike: Optional[float] = None
    option_type: Optional[str] = None   # "CE" / "PE"
    expiry: Optional[object] = None     # date or string
    zone: Optional[OptionZone] = None   # Only for options
    is_subscribed: bool = False


class TokenRegistry:
    """
    Central registry that maps every instrument token to its metadata.
    Provides O(1) lookup for tick routing and manages option zone assignments.
    """

    def __init__(self):
        self._registry: Dict[int, TokenInfo] = {}
        self._lock = threading.Lock()

        # Reverse maps for fast grouped lookups
        # parent_symbol → {token_type → set of tokens}
        self._parent_map: Dict[str, Dict[TokenType, Set[int]]] = {}

        # parent_symbol → {strike → {"CE": token, "PE": token}}
        self._option_strike_map: Dict[str, Dict[float, Dict[str, int]]] = {}

        # parent_symbol → Stock/Index object reference
        self._parent_objects: Dict[str, object] = {}

        # Track ATM for dynamic re-centering
        self._current_atm: Dict[str, float] = {}  # parent_symbol → current ATM strike

        # Strike gap per symbol (50 for NIFTY, 100 for BANKNIFTY, etc.)
        self._strike_gaps: Dict[str, float] = {}

    def register(self, info: TokenInfo):
        with self._lock:
            self._registry[info.token] = info

            # Update parent map
            if info.parent_symbol not in self._parent_map:
                self._parent_map[info.parent_symbol] = {}
            type_set = self._parent_map[info.parent_symbol].setdefault(info.token_type, set())
            type_set.add(info.token)

            # Update option strike map
            if info.token_type == TokenType.OPTION and info.strike is not None and info.option_type:
                if info.parent_symbol not in self._option_strike_map:
                    self._option_strike_map[info.parent_symbol] = {}
                strike_dict = self._option_strike_map[info.parent_symbol].setdefault(info.strike, {})
                strike_dict[info.option_type] = info.token

    def register_batch(self, infos: List[TokenInfo]):
        for info in infos:
            self.register(info)

    def unregister(self, token: int):
        with self._lock:
            info = self._registry.pop(token, None)
            if info is None:
                return
            # Clean parent map
            parent_types = self._parent_map.get(info.parent_symbol, {})
            type_set = parent_types.get(info.token_type)
            if type_set:
                type_set.discard(token)
            # Clean option strike map
            if info.token_type == TokenType.OPTION and info.strike is not None and info.option_type:
                strike_dict = self._option_strike_map.get(info.parent_symbol, {}).get(info.strike, {})
                if strike_dict.get(info.option_type) == token:
                    del strike_dict[info.option_type]

    def lookup(self, token: int) -> Optional[TokenInfo]:
        return self._registry.get(token)

    def set_parent_object(self, symbol: str, obj):
        self._parent_objects[symbol] = obj

    def get_parent_object(self, symbol: str):
        return self._parent_objects.get(symbol)

    def set_strike_gap(self, symbol: str, gap: float):
        self._strike_gaps[symbol] = gap

    def get_strike_gap(self, symbol: str) -> float:
        return self._strike_gaps.get(symbol, 50.0)

    def get_tokens_by_type(self, parent_symbol: str, token_type: TokenType) -> Set[int]:
        return self._parent_map.get(parent_symbol, {}).get(token_type, set()).copy()

    def get_option_tokens_for(self, parent_symbol: str) -> Dict[float, Dict[str, int]]:
        return self._option_strike_map.get(parent_symbol, {}).copy()

    def get_all_subscribed_tokens(self) -> List[int]:
        return [t for t, info in self._registry.items() if info.is_subscribed]

    def get_option_tokens_by_zone(self, parent_symbol: str, zone: OptionZone) -> List[int]:
        tokens = []
        for token in self._parent_map.get(parent_symbol, {}).get(TokenType.OPTION, set()):
            info = self._registry.get(token)
            if info and info.zone == zone:
                tokens.append(token)
        return tokens

    # ─── Dynamic Zone Management ────────────────────────────────────────

    def round_to_strike(self, price: float, symbol: str) -> float:
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"Invalid spot price {price} for {symbol}")
        gap = self.get_strike_gap(symbol)
        return round(price / gap) * gap

    def get_current_atm(self, symbol: str) -> Optional[float]:
        return self._current_atm.get(symbol)

    def calculate_zones(self, parent_symbol: str, spot_price: float) -> Dict[float, OptionZone]:
        """
        Given a spot price, assign zones to all registered option strikes for a symbol.
        Returns {strike: zone} mapping.

        Core:       ATM ± 1% of spot
        Active:     1-3% of spot
        Peripheral: 3-5% of spot
        """
        strike_map = self._option_strike_map.get(parent_symbol, {})
        zone_map = {}
        for strike in strike_map:
            pct_away = abs(strike - spot_price) / spot_price * 100
            if pct_away <= 1.0:
                zone_map[strike] = OptionZone.CORE
            elif pct_away <= 3.0:
                zone_map[strike] = OptionZone.ACTIVE
            elif pct_away <= 5.0:
                zone_map[strike] = OptionZone.PERIPHERAL
            # Strikes > 5% away get no zone (won't be subscribed)
        return zone_map

    def recenter_and_get_subscription_changes(
        self, parent_symbol: str, new_spot: float
    ) -> Tuple[List[int], List[int], Dict[str, List[int]]]:
        """
        Recalculate zones based on new spot price.
        Returns:
            - new_subscribe: tokens to subscribe (newly in range)
            - unsubscribe: tokens to remove (now out of range)
            - mode_changes: {ws_mode: [tokens]} for zone promotions/demotions
        """
        new_atm = self.round_to_strike(new_spot, parent_symbol)
        old_atm = self._current_atm.get(parent_symbol)

        # If ATM hasn't changed, no action needed
        if old_atm == new_atm:
            return [], [], {}

        self._current_atm[parent_symbol] = new_atm

        new_zones = self.calculate_zones(parent_symbol, new_spot)

        new_subscribe = []
        unsubscribe = []
        mode_changes: Dict[str, List[int]] = {}

        with self._lock:
            strike_map = self._option_strike_map.get(parent_symbol, {})

            for strike, strike_tokens in strike_map.items():
                new_zone = new_zones.get(strike)
                for opt_type, token in strike_tokens.items():
                    info = self._registry.get(token)
                    if info is None:
                        continue

                    old_zone = info.zone

                    if new_zone is None and info.is_subscribed:
                        # Strike moved out of range — unsubscribe
                        unsubscribe.append(token)
                        info.is_subscribed = False
                        info.zone = None

                    elif new_zone is not None and not info.is_subscribed:
                        # Strike moved into range — subscribe
                        info.zone = new_zone
                        info.is_subscribed = True
                        new_subscribe.append(token)
                        ws_mode = ZONE_TO_WS_MODE[new_zone]
                        mode_changes.setdefault(ws_mode, []).append(token)

                    elif new_zone is not None and new_zone != old_zone:
                        # Zone changed — update mode
                        info.zone = new_zone
                        ws_mode = ZONE_TO_WS_MODE[new_zone]
                        mode_changes.setdefault(ws_mode, []).append(token)

        logger.info(
            f"[TokenRegistry] Recentered {parent_symbol}: ATM {old_atm} → {new_atm}, "
            f"+{len(new_subscribe)} subscribe, -{len(unsubscribe)} unsubscribe, "
            f"{sum(len(v) for v in mode_changes.values())} mode changes"
        )
        return new_subscribe, unsubscribe, mode_changes

    def initial_subscribe_options(
        self, parent_symbol: str, spot_price: float
    ) -> Tuple[List[int], Dict[str, List[int]]]:
        """
        First-time subscription: calculate zones and return tokens to subscribe with their modes.
        Returns:
            - subscribe_tokens: all tokens to subscribe
            - mode_map: {ws_mode: [tokens]}
        """
        self._current_atm[parent_symbol] = self.round_to_strike(spot_price, parent_symbol)
        new_zones = self.calculate_zones(parent_symbol, spot_price)

        subscribe_tokens = []
        mode_map: Dict[str, List[int]] = {}

        with self._lock:
            strike_map = self._option_strike_map.get(parent_symbol, {})
            for strike, strike_tokens in strike_map.items():
                zone = new_zones.get(strike)
                if zone is None:
                    continue
                for opt_type, token in strike_tokens.items():
                    info = self._registry.get(token)
                    if info is None:
                        continue
                    info.zone = zone
                    info.is_subscribed = True
                    subscribe_tokens.append(token)
                    ws_mode = ZONE_TO_WS_MODE[zone]
                    mode_map.setdefault(ws_mode, []).append(token)

        logger.info(
            f"[TokenRegistry] Initial subscribe {parent_symbol}: "
            f"{len(subscribe_tokens)} option tokens across {len(new_zones)} strikes"
        )
        return subscribe_tokens, mode_map

    def get_stats(self) -> dict:
        total = len(self._registry)
        subscribed = sum(1 for info in self._registry.values() if info.is_subscribed)
        by_type = {}
        for info in self._registry.values():
            by_type[info.token_type.value] = by_type.get(info.token_type.value, 0) + 1
        return {"total_registered": total, "subscribed": subscribed, "by_type": by_type}
