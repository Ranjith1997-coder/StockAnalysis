from zerodha.zerodha_ticker import KiteTicker
import common.shared as shared
import time
from common.Stock import Stock
from common.logging_util import logger
from common.token_registry import (
    TokenType, OptionZone, TokenRegistry, TokenInfo,
    ZONE_TO_WS_MODE, WS_MODE_FULL, WS_MODE_QUOTE, WS_MODE_LTP,
)
import threading
import queue
from collections import defaultdict
from notification.Notification import TELEGRAM_NOTIFICATIONS
import requests


class ZerodhaTickerManager:
    # Aggregate recomputation throttle (seconds)
    AGGREGATE_INTERVAL = 1.0
    # How much spot must move (in strikes) before re-centering option subscriptions
    RECENTER_THRESHOLD_STRIKES = 2  # recenter only after 2-strike ATM move (avoids boundary oscillation)

    def __init__(self, userName, password, encToken):
        self.username = userName
        self.password = password
        self.encToken = encToken
        self.apiKey = "kitefront"
        self.root = "wss://ws.zerodha.com"
        self.connected = False
        self._kt: KiteTicker | None = None
        self.max_retries = 3
        self.retry_delay = 5  # seconds
        self.tick_queue = queue.Queue()
        self.processor_thread = None
        self.stop_processor = False
        self.notification_cooldown = 300  # 5 minutes cooldown
        self.last_notification_time = defaultdict(float)
        self.is_enctoken_updated = False
        self.reconnect_attempts = 0

        # Track last ATM per symbol for re-centering decisions
        self._last_atm: dict = {}
        # Track last aggregate update time per symbol
        self._last_aggregate_time: dict = defaultdict(float)

        # Real-time options analysis engine (injected by intraday_monitor when enabled)
        self.live_options_engine = None

        # Live stock analysis engine (injected by intraday_monitor when signal_bus exists)
        self.live_stock_engine: object | None = None

        # When True: on_connect subscribes only index tokens (skips 206 equity stocks).
        # Set to True in LIVE_OPTIONS_ONLY mode to stay within the 500-token limit.
        self.index_only_mode = False

    @property
    def token_registry(self) -> TokenRegistry:
        return shared.app_ctx.token_registry

    def initialize_kite_ticker(self):
        self._kt = KiteTicker(self.apiKey, self.username, self.encToken, root=self.root, reconnect=True, reconnect_max_tries=self.max_retries)
        self._kt.on_connect = self.on_connect
        self._kt.on_close = self.on_close
        self._kt.on_error = self.on_error
        self._kt.on_ticks = self.on_ticks
        self._kt.on_reconnect = self.on_reconnect

    def connect(self):
        try:
            logger.info(f"Attempting to connect to Zerodha WebSocket with user: {self.username}")
            self.initialize_kite_ticker()
            self._kt.connect(threaded=True)
            connection_timeout = 30
            start_time = time.time()

            while time.time() - start_time < connection_timeout and self.reconnect_attempts < self.max_retries:
                if self._kt.is_connected():
                    logger.info("Successfully connected to Zerodha WebSocket")
                    return True
                time.sleep(0.5)
        except Exception as e:
            logger.error(f"Error while connecting to Zerodha WebSocket: {str(e)}")
            self.connected = False
            self.is_enctoken_updated = False
            return False
        finally:
            if not (self._kt and self._kt.is_connected()):
                logger.error("Failed to connect to Zerodha WebSocket after multiple attempts")
                self.close_connection()

    def close_connection(self):
        if self._kt:
            try:
                self._kt.close()
                logger.info("WebSocket connection closed successfully")
                self.connected = False
                self.is_enctoken_updated = False
            except Exception as e:
                logger.error(f"Error while closing WebSocket connection: {str(e)}")
        else:
            logger.info("WebSocket connection is already closed or not initialized")

    def update_enctoken(self, new_enctoken):
        self.encToken = new_enctoken
        self.initialize_kite_ticker()
        logger.info("Enctoken updated successfully")
        self.is_enctoken_updated = True

    def refresh_enctoken(self, twofa):
        new_enctoken = self.get_new_enctoken(twofa)
        if new_enctoken:
            self.encToken = new_enctoken
            logger.info("Successfully refreshed enctoken.")
            self.is_enctoken_updated = True
            return True
        else:
            logger.error("Failed to get new enctoken.")
            self.is_enctoken_updated = False
            return False

    def get_new_enctoken(self, twofa):
        session = requests.Session()
        response = session.post('https://kite.zerodha.com/api/login', data={
            "user_id": self.username,
            "password": self.password
        })
        response = session.post('https://kite.zerodha.com/api/twofa', data={
            "request_id": response.json()['data']['request_id'],
            "twofa_value": twofa,
            "user_id": response.json()['data']['user_id']
        })
        enctoken = response.cookies.get('enctoken')
        if enctoken:
            return enctoken
        else:
            raise Exception("Enter valid details !!!!")

    # ─── Tick Processing ────────────────────────────────────────────────

    def start_tick_processor(self):
        self.stop_processor = False
        self.processor_thread = threading.Thread(target=self.process_ticks, daemon=True)
        self.processor_thread.start()

    def stop_tick_processor(self):
        self.stop_processor = True
        if self.processor_thread:
            self.processor_thread.join(timeout=5)

    def process_ticks(self):
        while not self.stop_processor:
            try:
                tick = self.tick_queue.get(timeout=1)
                self._route_tick(tick)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing tick: {e}")

    def _route_tick(self, tick):
        """Route a tick to the appropriate handler based on token registry lookup."""
        token = tick.get("instrument_token")
        if token is None:
            return

        registry = self.token_registry
        if registry is None:
            # Fallback: try legacy stock dict lookup
            self._process_equity_tick_legacy(tick, token)
            return

        info = registry.lookup(token)
        if info is None:
            return

        if info.token_type in (TokenType.EQUITY, TokenType.INDEX, TokenType.COMMODITY, TokenType.GLOBAL_INDEX):
            self._process_equity_tick(tick, info)
        elif info.token_type == TokenType.OPTION:
            self._process_option_tick(tick, info)
        elif info.token_type == TokenType.FUTURE:
            self._process_future_tick(tick, info)

    def _get_parent_object(self, info: TokenInfo) -> Stock | None:
        """Resolve the parent Stock/Index object for a token."""
        registry = self.token_registry
        parent = registry.get_parent_object(info.parent_symbol)
        if parent:
            return parent

        # Fallback: search the legacy dicts
        for d in [shared.app_ctx.stock_token_obj_dict,
                   shared.app_ctx.index_token_obj_dict,
                   shared.app_ctx.commodity_token_obj_dict,
                   shared.app_ctx.global_indices_token_obj_dict]:
            for t, obj in d.items():
                if obj.stock_symbol == info.parent_symbol:
                    registry.set_parent_object(info.parent_symbol, obj)
                    return obj
        return None

    def _process_equity_tick(self, tick, info: TokenInfo):
        """Handle equity/index/commodity tick — update _zerodha_data."""
        parent = self._get_parent_object(info)
        if parent is None:
            return
        parent.update_zerodha_data(tick)

        # Live stock analysis (VWAP cross, bid/ask imbalance, ORB, etc.)
        if self.live_stock_engine and info.token_type in (TokenType.EQUITY, TokenType.INDEX):
            self.live_stock_engine.on_tick(parent)

        # For index ticks, check if option re-centering is needed
        if info.token_type == TokenType.INDEX:
            spot = tick.get("last_price")
            if spot and spot > 0:
                self._check_recentering(info.parent_symbol, spot)

    def _process_equity_tick_legacy(self, tick, token):
        """Legacy fallback when token registry is not initialized."""
        if token in shared.app_ctx.stock_token_obj_dict:
            stock = shared.app_ctx.stock_token_obj_dict[token]
            stock.update_zerodha_data(tick)
        elif token in shared.app_ctx.index_token_obj_dict:
            index = shared.app_ctx.index_token_obj_dict[token]
            index.update_zerodha_data(tick)

    def _process_option_tick(self, tick, info: TokenInfo):
        """Handle option tick — update parent's options_live data."""
        parent = self._get_parent_object(info)
        if parent is None:
            return

        parent.update_option_tick(info.strike, info.option_type, tick)

        # Throttled aggregate recomputation
        now = time.time()
        if now - self._last_aggregate_time[info.parent_symbol] >= self.AGGREGATE_INTERVAL:
            spot = parent.zerodha_data.get("last_price") or parent.ltp
            parent.recompute_options_aggregate(spot_price=spot)
            self._last_aggregate_time[info.parent_symbol] = now

            agg = parent.options_aggregate
            logger.debug(
                f"[ZerodhaWS] {info.parent_symbol} aggregate updated — "
                f"strikes={len(parent.options_live)}, spot={spot}, "
                f"pcr={agg.get('live_pcr', 0):.3f}, "
                f"atm_strike={agg.get('atm_strike')}, "
                f"straddle={agg.get('atm_straddle_premium', 0):.1f}"
            )

            # Real-time options analysis (only when engine is enabled)
            if self.live_options_engine and spot:
                self.live_options_engine.on_aggregate_updated(parent, spot)

    def _process_future_tick(self, tick, info: TokenInfo):
        """Handle futures tick — update parent's futures_live data."""
        parent = self._get_parent_object(info)
        if parent is None:
            return

        expiry_key = "current" if info.expiry == self._get_current_expiry(info.parent_symbol) else "next"
        parent.update_futures_tick(expiry_key, tick)

    def _get_current_expiry(self, parent_symbol):
        """Get the current (nearest) expiry for a symbol."""
        if shared.app_ctx.stockExpires:
            return shared.app_ctx.stockExpires[0]
        return None

    # ─── Dynamic Re-centering ──────────────────────────────────────────

    def _check_recentering(self, parent_symbol: str, spot_price: float):
        """Check if ATM has shifted enough to warrant re-subscribing option tokens."""
        registry = self.token_registry
        if registry is None:
            return

        current_atm = registry.round_to_strike(spot_price, parent_symbol)
        last_atm = self._last_atm.get(parent_symbol)

        if last_atm is None:
            self._last_atm[parent_symbol] = current_atm
            return

        strike_gap = registry.get_strike_gap(parent_symbol)
        strikes_moved = abs(current_atm - last_atm) / strike_gap

        if strikes_moved < self.RECENTER_THRESHOLD_STRIKES:
            return

        self._last_atm[parent_symbol] = current_atm
        new_sub, unsub, mode_changes = registry.recenter_and_get_subscription_changes(
            parent_symbol, spot_price
        )

        if unsub and self._kt and self._kt.is_connected():
            try:
                self._kt.unsubscribe(unsub)
            except Exception as e:
                logger.error(f"Error unsubscribing during recenter: {e}")

        if new_sub and self._kt and self._kt.is_connected():
            try:
                self._kt.subscribe(new_sub)
            except Exception as e:
                logger.error(f"Error subscribing during recenter: {e}")

        for ws_mode, tokens in mode_changes.items():
            if tokens and self._kt and self._kt.is_connected():
                try:
                    self._kt.set_mode(ws_mode, tokens)
                except Exception as e:
                    logger.error(f"Error setting mode {ws_mode} during recenter: {e}")

    def subscribe_options_for_symbol(self, parent_symbol: str, spot_price: float):
        """
        Initial subscription of option tokens for a symbol.
        Call this after registering option tokens and connecting WebSocket.
        """
        import math
        registry = self.token_registry
        if registry is None:
            logger.error("Token registry not initialized")
            return

        if not spot_price or not math.isfinite(spot_price) or spot_price <= 0:
            logger.error(f"Invalid spot price {spot_price} for {parent_symbol}, skipping option subscription")
            return

        subscribe_tokens, mode_map = registry.initial_subscribe_options(parent_symbol, spot_price)

        if not subscribe_tokens:
            logger.warning(f"No option tokens to subscribe for {parent_symbol}")
            return

        if self._kt and self._kt.is_connected():
            try:
                self._kt.subscribe(subscribe_tokens)
                for ws_mode, tokens in mode_map.items():
                    self._kt.set_mode(ws_mode, tokens)
                self._last_atm[parent_symbol] = registry.round_to_strike(spot_price, parent_symbol)
                logger.info(f"Subscribed {len(subscribe_tokens)} option tokens for {parent_symbol}")
            except Exception as e:
                logger.error(f"Error subscribing options for {parent_symbol}: {e}")

    # ─── Live Options Subscription ────────────────────────────────────

    def subscribe_live_options(self, wait_for_ticks: bool = True) -> None:
        """Subscribe Zerodha option tokens for all LIVE_OPTIONS_INDICES.

        Replaces the old ``_subscribe_registered_options`` helper in
        ``notification/commands/account.py``.  Can be called from any context
        — init auto-connect, Telegram bot, or live_options_analysis — without
        importing from the notification module.

        Args:
            wait_for_ticks: When True (default) sleeps 2 s so that the first
                            index tick with spot price has time to arrive.
                            Pass False when spot prices are already available.
        """
        from common.token_registry import TokenType, OptionZone
        from common.constants import LIVE_OPTIONS_INDICES
        from common.logging_util import logger
        from notification.Notification import TELEGRAM_NOTIFICATIONS

        registry = shared.app_ctx.token_registry
        if registry is None:
            logger.warning("[subscribe_live_options] token_registry not initialised, skip")
            return

        if wait_for_ticks:
            import time as _time
            _time.sleep(2)  # wait for first index tick with spot price

        total_option_tokens = 0
        option_lines = []

        for token, index_obj in shared.app_ctx.index_token_obj_dict.items():
            symbol = index_obj.stock_symbol

            if symbol not in LIVE_OPTIONS_INDICES:
                continue

            option_tokens = registry.get_tokens_by_type(symbol, TokenType.OPTION)
            if not option_tokens:
                logger.warning(f"[subscribe_live_options] no option tokens registered for {symbol}")
                continue

            spot = index_obj.zerodha_data.get("last_price") or index_obj.ltp
            if not spot or spot <= 0:
                logger.warning(f"[subscribe_live_options] no spot price for {symbol}, skipping")
                continue

            self.subscribe_options_for_symbol(symbol, spot)
            logger.info(f"[subscribe_live_options] option subscription initiated for {symbol} at spot {spot:.0f}")

            subscribed_count = sum(
                len(registry.get_option_tokens_by_zone(symbol, zone))
                for zone in OptionZone
            )
            total_option_tokens += subscribed_count
            option_lines.append(f"  {symbol}: {subscribed_count} tokens (spot {spot:.0f})")

        index_count  = len(shared.app_ctx.index_token_obj_dict)
        equity_count = len(shared.app_ctx.stock_token_obj_dict)
        base_count   = index_count + (0 if self.index_only_mode else equity_count)
        total        = base_count + total_option_tokens

        mode_note = "index-only" if self.index_only_mode else f"{equity_count} equity + {index_count} index"
        summary_lines = [
            "WebSocket connected",
            f"Base: {base_count} ({mode_note})",
            f"Options: {total_option_tokens}",
        ]
        summary_lines.extend(option_lines)
        summary_lines.append(f"Total: {total} / 500")

        TELEGRAM_NOTIFICATIONS.send_notification("\n".join(summary_lines))
        logger.info(f"[subscribe_live_options] subscription complete — total {total} tokens")

    # ─── Notification ──────────────────────────────────────────────────

    def send_notification(self, stock, direction, buy_quantity, sell_quantity):
        message = f"Alert for {stock.stockName} ({stock.stock_symbol}): "
        message += f"High {direction} pressure. "
        message += f"Buy Quantity: {buy_quantity}, Sell Quantity: {sell_quantity}"
        logger.info(message)
        TELEGRAM_NOTIFICATIONS.send_notification(message)

    # ─── Subscribe / Unsubscribe ───────────────────────────────────────

    def unsubscribe(self, instrument_tokens):
        if not self._kt or not self._kt.is_connected():
            logger.error("Kite Ticker is not connected. Cannot unsubscribe.")
            return False

        try:
            self._kt.unsubscribe(instrument_tokens)
            logger.info(f"Successfully unsubscribed from instrument tokens: {instrument_tokens}")
            return True
        except Exception as e:
            logger.error(f"Error while unsubscribing: {str(e)}")
            return False

    # ─── WebSocket Callbacks ───────────────────────────────────────────

    def on_connect(self, ws, response):
        self.connected = True
        self.reconnect_attempts = 0
        logger.info(f"Successfully connected. Response: {response}")

        # Collect tokens for subscription.
        # In index_only_mode (LIVE_OPTIONS_ONLY) skip equity stocks to stay under 500-token limit.
        index_tokens  = list(shared.app_ctx.index_token_obj_dict.keys())
        equity_tokens = [] if self.index_only_mode else list(shared.app_ctx.stock_token_obj_dict.keys())

        all_base_tokens = equity_tokens + index_tokens

        self.start_tick_processor()

        if all_base_tokens:
            ws.subscribe(all_base_tokens)
            ws.set_mode(ws.MODE_FULL, all_base_tokens)
            if self.index_only_mode:
                logger.info(f"Subscribed to {len(index_tokens)} indices only (index_only_mode)")
            else:
                logger.info(f"Subscribed to {len(equity_tokens)} stocks, {len(index_tokens)} indices")

        # Option tokens are subscribed separately via subscribe_options_for_symbol()
        # after we receive the first index tick with spot price

    def on_close(self, ws, code, reason):
        self.connected = False
        logger.info(f"Connection closed. Code: {code}, Reason: {reason}")
        self.stop_tick_processor()

    def on_error(self, ws, code, reason):
        logger.error(f"Error in connection. Code: {code}, Reason: {reason}")
        self.stop_tick_processor()
        # 403 = enctoken expired — alert and trigger a fresh login in a background thread.
        # The auth script writes the new token to .env; the next reconnect attempt picks it up.
        if "403" in str(reason):
            logger.warning("[ZerodhaWS] 403 Forbidden — enctoken expired mid-session, re-authing")
            from notification.Notification import TELEGRAM_NOTIFICATIONS
            TELEGRAM_NOTIFICATIONS.send_notification(
                "⚠️ <b>Zerodha WS 403 — enctoken expired</b>\n"
                "Attempting automatic re-login via auth_login.py…",
                parse_mode="HTML",
            )
            import threading
            def _reauth():
                try:
                    import subprocess, sys, os
                    script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth", "auth_login.py")
                    result = subprocess.run(
                        [sys.executable, script, "--force"],
                        capture_output=True, text=True, timeout=60,
                    )
                    if result.returncode == 0:
                        logger.info("[ZerodhaWS] re-auth succeeded — new enctoken written to .env")
                        # Reload the new token and reconnect
                        from dotenv import load_dotenv
                        load_dotenv(override=True)
                        import common.constants as _c
                        from urllib.parse import quote as _quote
                        new_token = os.getenv(_c.ENV_ZERODHA_ENC_TOKEN, "")
                        if new_token:
                            self.update_enctoken(_quote(new_token, safe=""))
                            if self.connect():
                                logger.info("[ZerodhaWS] reconnected with fresh enctoken")
                                self.subscribe_live_options(wait_for_ticks=True)
                    else:
                        logger.error(f"[ZerodhaWS] re-auth failed: {result.stderr[:200]}")
                        TELEGRAM_NOTIFICATIONS.send_notification(
                            "🚨 <b>Zerodha re-auth FAILED</b> — manual intervention required",
                            parse_mode="HTML",
                        )
                except Exception as e:
                    logger.error(f"[ZerodhaWS] re-auth exception: {e}")
            threading.Thread(target=_reauth, name="ws-reauth", daemon=True).start()

    def on_ticks(self, ws, ticks):
        import time
        import common.shared as shared
        logger.debug(f"Received {len(ticks)} ticks")
        shared.app_ctx.last_equity_tick_time = time.time()
        for tick in ticks:
            self.tick_queue.put(tick)

    def on_reconnect(self, ws, attempts_count):
        self.reconnect_attempts = attempts_count
        logger.info(f"Reconnected to Zerodha WebSocket. Attempt: {self.reconnect_attempts}")
        # Only re-subscribe options if this looks like a genuine reconnect (not a 403 loop).
        # The 403 re-auth path in on_error handles subscription after token refresh.
        if self.live_options_engine is not None and self._kt and self._kt.is_connected():
            import threading
            def _resubscribe():
                self.subscribe_live_options(wait_for_ticks=True)
            threading.Thread(target=_resubscribe, name="ws-resubscribe", daemon=True).start()
