from zerodha.zerodha_ticker import KiteTicker
import common.shared as shared
import time
from common.Stock import Stock
from common.logging_util import logger
from common.token_registry import (
    TokenType, OptionZone, TokenRegistry, TokenInfo,
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
    RECENTER_THRESHOLD_STRIKES = 2

    def __init__(self, userName, password, encToken):
        self.username = userName
        self.password = password
        self.encToken = encToken
        self.apiKey = "kitefront"
        self.root = "wss://ws.zerodha.com"

        # WS1: equity + index (215 tokens)
        self._base_connected = False
        self._kt_base: KiteTicker | None = None

        # WS2: options only (366 tokens)
        self._options_connected = False
        self._kt_options: KiteTicker | None = None

        self.max_retries = 50
        self.retry_delay = 5
        self.tick_queue = queue.Queue(maxsize=5000)
        self.processor_thread = None
        self.stop_processor = False
        self.notification_cooldown = 300
        self.last_notification_time = defaultdict(float)
        self.is_enctoken_updated = False
        self.reconnect_attempts = 0
        self._unknown_tokens: set = set()
        self._reauth_lock = threading.Lock()
        self._is_reauthing = False
        self._tick_count = 0

        self._last_atm: dict = {}
        self._last_aggregate_time: dict = defaultdict(float)

        self.live_options_engine = None
        self.live_stock_engine: object | None = None

        # When True: skip equity tokens in WS1 (LIVE_OPTIONS_ONLY mode).
        # No longer needed for the 500-limit workaround since options are on WS2.
        self.index_only_mode = False

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._base_connected

    @property
    def options_connected(self) -> bool:
        return self._options_connected

    @property
    def fully_connected(self) -> bool:
        return self._base_connected and self._options_connected

    @property
    def token_registry(self) -> TokenRegistry:
        return shared.app_ctx.token_registry

    def _needs_options_ws(self) -> bool:
        """True if a separate options WS is needed (not when OPTIONS_SOURCE=sensibull alone)."""
        return bool(
            self.live_options_engine is not None
            and shared.app_ctx.options_source in ("zerodha", "both")
        )

    # ─── Connection Management ──────────────────────────────────────────

    def _init_kite_ticker_base(self):
        self._kt_base = KiteTicker(self.apiKey, self.username, self.encToken, root=self.root,
                                    reconnect=True, reconnect_max_tries=self.max_retries, reconnect_max_delay=60)
        self._kt_base.on_connect = self.on_connect_base
        self._kt_base.on_close = self.on_close_base
        self._kt_base.on_error = self.on_error_base
        self._kt_base.on_ticks = self.on_ticks
        self._kt_base.on_reconnect = self.on_reconnect_base
        self._kt_base.on_noreconnect = self.on_noreconnect_base

    def _init_kite_ticker_options(self):
        self._kt_options = KiteTicker(self.apiKey, self.username, self.encToken, root=self.root,
                                       reconnect=True, reconnect_max_tries=self.max_retries, reconnect_max_delay=60)
        self._kt_options.on_connect = self.on_connect_options
        self._kt_options.on_close = self.on_close_options
        self._kt_options.on_error = self.on_error_options
        self._kt_options.on_ticks = self.on_ticks
        self._kt_options.on_reconnect = self.on_reconnect_options
        self._kt_options.on_noreconnect = self.on_noreconnect_options

    def connect(self):
        """Connect both WS (base + options). Returns True if base connected."""
        try:
            self._init_kite_ticker_base()
            self._kt_base.connect(threaded=True)
            if self._needs_options_ws():
                self._init_kite_ticker_options()
                self._kt_options.connect(threaded=True)

            connection_timeout = 30
            start_time = time.time()
            while time.time() - start_time < connection_timeout:
                if self._base_connected:
                    logger.info("Zerodha WS1 (base) connected — equity + index")
                    if not self._needs_options_ws() or self._options_connected:
                        break
                time.sleep(0.5)

            if not self._base_connected:
                logger.error("Zerodha WS1 (base) failed to connect after timeout")
                self.close_connection()
                return False
            return True
        except Exception as e:
            logger.error(f"Error connecting Zerodha WebSockets: {e}")
            self._base_connected = False
            self._options_connected = False
            self.is_enctoken_updated = False
            return False

    def close_connection(self):
        for kt, label in [(self._kt_base, "base"), (self._kt_options, "options")]:
            if kt:
                try:
                    kt.close()
                    logger.info(f"Zerodha WS {label} closed")
                except Exception as e:
                    logger.warning(f"Error closing WS {label}: {e}")
        self._base_connected = False
        self._options_connected = False
        self.is_enctoken_updated = False

    def update_enctoken(self, new_enctoken):
        self.encToken = new_enctoken
        self._init_kite_ticker_base()
        self._init_kite_ticker_options()
        logger.info("Enctoken updated for both WebSockets")
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
        if self.processor_thread and self.processor_thread.is_alive():
            return
        self.stop_processor = False
        self.processor_thread = threading.Thread(target=self.process_ticks, daemon=True)
        self.processor_thread.start()

    def signal_tick_processor_stop(self):
        self.stop_processor = True

    def stop_tick_processor(self):
        self.stop_processor = True
        if self.processor_thread:
            self.processor_thread.join(timeout=5)

    def process_ticks(self):
        while not self.stop_processor:
            try:
                tick = self.tick_queue.get(timeout=1)
                self._route_tick(tick)
                self._tick_count += 1
                if self._tick_count % 500 == 0:
                    logger.debug(f"[ZerodhaWS] processed {self._tick_count} ticks, queue depth={self.tick_queue.qsize()}")
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing tick: {e}")

    def _route_tick(self, tick):
        token = tick.get("instrument_token")
        if token is None:
            return

        registry = self.token_registry
        if registry is None:
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
        registry = self.token_registry
        parent = registry.get_parent_object(info.parent_symbol)
        if parent:
            return parent

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
        parent = self._get_parent_object(info)
        if parent is None:
            if info.token not in self._unknown_tokens:
                self._unknown_tokens.add(info.token)
                logger.warning(f"[ZerodhaWS] No parent object for equity token {info.token} ({info.parent_symbol}) — tick dropped")
            return
        parent.update_zerodha_data(tick)

        if self.live_stock_engine and info.token_type in (TokenType.EQUITY, TokenType.INDEX):
            self.live_stock_engine.on_tick(parent)

        if info.token_type == TokenType.INDEX:
            spot = tick.get("last_price")
            if spot and spot > 0:
                self._check_recentering(info.parent_symbol, spot)

    def _process_equity_tick_legacy(self, tick, token):
        if token in shared.app_ctx.stock_token_obj_dict:
            stock = shared.app_ctx.stock_token_obj_dict[token]
            stock.update_zerodha_data(tick)
        elif token in shared.app_ctx.index_token_obj_dict:
            index = shared.app_ctx.index_token_obj_dict[token]
            index.update_zerodha_data(tick)

    def _process_option_tick(self, tick, info: TokenInfo):
        parent = self._get_parent_object(info)
        if parent is None:
            if info.token not in self._unknown_tokens:
                self._unknown_tokens.add(info.token)
                logger.warning(f"[ZerodhaWS] No parent object for option token {info.token} ({info.parent_symbol} {info.strike} {info.option_type}) — tick dropped")
            return

        parent.update_option_tick(info.strike, info.option_type, tick)

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

            if self.live_options_engine and spot:
                self.live_options_engine.on_aggregate_updated(parent, spot)

    def _process_future_tick(self, tick, info: TokenInfo):
        parent = self._get_parent_object(info)
        if parent is None:
            if info.token not in self._unknown_tokens:
                self._unknown_tokens.add(info.token)
                logger.warning(f"[ZerodhaWS] No parent object for futures token {info.token} ({info.parent_symbol}) — tick dropped")
            return

        expiry_key = "current" if info.expiry == self._get_current_expiry(info.parent_symbol) else "next"
        parent.update_futures_tick(expiry_key, tick)

    def _get_current_expiry(self, parent_symbol):
        if shared.app_ctx.stockExpires:
            return shared.app_ctx.stockExpires[0]
        return None

    # ─── Thread-safe WS send helper ───────────────────────────────────

    def _ws_call(self, fn, *args):
        try:
            from twisted.internet import reactor as _reactor
            if _reactor.running:
                _reactor.callFromThread(fn, *args)
            else:
                fn(*args)
        except Exception as e:
            logger.error(f"[ZerodhaWS] _ws_call failed for {fn.__name__}: {e}")

    # ─── Dynamic Re-centering (uses WS2 — options) ─────────────────────

    def _check_recentering(self, parent_symbol: str, spot_price: float):
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

        ws = self._kt_options
        if unsub and ws and ws.is_connected():
            self._ws_call(ws.unsubscribe, unsub)
        if new_sub and ws and ws.is_connected():
            self._ws_call(ws.subscribe, new_sub)
        for ws_mode, tokens in mode_changes.items():
            if tokens and ws and ws.is_connected():
                self._ws_call(ws.set_mode, ws_mode, tokens)

    def subscribe_options_for_symbol(self, parent_symbol: str, spot_price: float):
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

        ws = self._kt_options
        if ws and ws.is_connected():
            self._ws_call(ws.subscribe, subscribe_tokens)
            for ws_mode, tokens in mode_map.items():
                self._ws_call(ws.set_mode, ws_mode, tokens)
            self._last_atm[parent_symbol] = registry.round_to_strike(spot_price, parent_symbol)
            logger.info(f"Subscribed {len(subscribe_tokens)} option tokens for {parent_symbol} (WS2)")

    # ─── Live Options Subscription ────────────────────────────────────

    def subscribe_live_options(self, wait_for_ticks: bool = True) -> None:
        """Subscribe Zerodha option tokens on WS2."""
        from common.token_registry import TokenType
        from common.constants import LIVE_OPTIONS_INDICES
        from common.logging_util import logger
        from notification.Notification import TELEGRAM_NOTIFICATIONS

        registry = shared.app_ctx.token_registry
        if registry is None:
            logger.warning("[subscribe_live_options] token_registry not initialised, skip")
            return

        if not self._needs_options_ws():
            logger.info("[subscribe_live_options] options WS not needed (OPTIONS_SOURCE != zerodha/both)")
            return

        if wait_for_ticks:
            import time as _time
            _time.sleep(2)

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

        index_count = len(shared.app_ctx.index_token_obj_dict)
        equity_count = len(shared.app_ctx.stock_token_obj_dict)
        base_count = index_count + (0 if self.index_only_mode else equity_count)
        mode_note = "index-only" if self.index_only_mode else f"{equity_count} equity + {index_count} index"

        summary_lines = [
            "Zerodha WS connected (dual)",
            f"WS1 (base): {base_count} ({mode_note})",
            f"WS2 (options): {total_option_tokens}",
        ]
        summary_lines.extend(option_lines)
        summary_lines.append(f"Total: {base_count + total_option_tokens} (split across 2 WS)")

        TELEGRAM_NOTIFICATIONS.send_notification("\n".join(summary_lines))
        logger.info(f"[subscribe_live_options] subscription complete — WS1: {base_count}, WS2: {total_option_tokens}")

    # ─── Notification ──────────────────────────────────────────────────

    def send_notification(self, stock, direction, buy_quantity, sell_quantity):
        message = f"Alert for {stock.stockName} ({stock.stock_symbol}): "
        message += f"High {direction} pressure. "
        message += f"Buy Quantity: {buy_quantity}, Sell Quantity: {sell_quantity}"
        logger.info(message)
        TELEGRAM_NOTIFICATIONS.send_notification(message)

    # ─── Subscribe / Unsubscribe ───────────────────────────────────────

    def unsubscribe(self, instrument_tokens, ws_name="options"):
        ws = self._kt_options if ws_name == "options" else self._kt_base
        if not ws or not ws.is_connected():
            logger.error(f"Kite Ticker ({ws_name}) is not connected. Cannot unsubscribe.")
            return False
        try:
            ws.unsubscribe(instrument_tokens)
            logger.info(f"Unsubscribed {len(instrument_tokens)} tokens from {ws_name}")
            return True
        except Exception as e:
            logger.error(f"Error unsubscribing from {ws_name}: {e}")
            return False

    # ─── WS1 Callbacks (base — equity + index) ─────────────────────────

    def on_connect_base(self, ws, response):
        self._base_connected = True
        logger.info(f"WS1 (base) connected. Response: {response}")

        index_tokens = list(shared.app_ctx.index_token_obj_dict.keys())
        equity_tokens = [] if self.index_only_mode else list(shared.app_ctx.stock_token_obj_dict.keys())
        all_base_tokens = equity_tokens + index_tokens

        self.start_tick_processor()

        if all_base_tokens:
            ws.subscribe(all_base_tokens)
            ws.set_mode(ws.MODE_FULL, all_base_tokens)
            mode_label = "indices only" if self.index_only_mode else f"{len(equity_tokens)} stocks + {len(index_tokens)} indices"
            logger.info(f"WS1 (base) subscribed to {len(all_base_tokens)} tokens ({mode_label})")

    def on_close_base(self, ws, code, reason):
        self._base_connected = False
        logger.info(f"WS1 (base) closed. Code: {code}, Reason: {reason}")
        self.signal_tick_processor_stop()

    def on_error_base(self, ws, code, reason):
        logger.error(f"WS1 (base) error. Code: {code}, Reason: {reason}")
        self.signal_tick_processor_stop()
        if "403" in str(reason):
            self._trigger_reauth()

    def on_reconnect_base(self, ws, attempts_count):
        self.reconnect_attempts = attempts_count
        logger.info(f"WS1 (base) reconnected. Attempt: {attempts_count}")

    def on_noreconnect_base(self, ws):
        self._base_connected = False
        logger.error(f"WS1 (base) — all {self.max_retries} reconnect attempts exhausted")
        try:
            TELEGRAM_NOTIFICATIONS.send_notification(
                f"🚨 <b>Zerodha WS1 (base) DEAD</b> — {self.max_retries} reconnect attempts exhausted.\n"
                "Equity/index ticks have stopped. Options WS may still be alive.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    # ─── WS2 Callbacks (options only) ──────────────────────────────────

    def on_connect_options(self, ws, response):
        self._options_connected = True
        logger.info(f"WS2 (options) connected. Response: {response}")
        # No tokens subscribed here — subscribe_live_options() handles it
        # after spot prices arrive from WS1 index ticks.

    def on_close_options(self, ws, code, reason):
        self._options_connected = False
        logger.info(f"WS2 (options) closed. Code: {code}, Reason: {reason}")

    def on_error_options(self, ws, code, reason):
        logger.error(f"WS2 (options) error. Code: {code}, Reason: {reason}")
        if "403" in str(reason):
            self._trigger_reauth()

    def on_reconnect_options(self, ws, attempts_count):
        logger.info(f"WS2 (options) reconnected. Attempt: {attempts_count}")
        # Re-subscribe options after reconnect
        if self.live_options_engine is not None:
            import threading
            threading.Thread(target=self.subscribe_live_options, args=(False,), daemon=True).start()

    def on_noreconnect_options(self, ws):
        self._options_connected = False
        logger.error(f"WS2 (options) — all {self.max_retries} reconnect attempts exhausted")
        try:
            TELEGRAM_NOTIFICATIONS.send_notification(
                f"🚨 <b>Zerodha WS2 (options) DEAD</b> — {self.max_retries} reconnect attempts exhausted.\n"
                "Live option ticks have stopped. Option analysis may be stale.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    # ─── Shared Callbacks ──────────────────────────────────────────────

    def on_ticks(self, ws, ticks):
        import time
        import common.shared as shared
        logger.debug(f"Received {len(ticks)} ticks")
        shared.app_ctx.last_equity_tick_time = time.time()
        for tick in ticks:
            try:
                self.tick_queue.put_nowait(tick)
            except queue.Full:
                token = tick.get("instrument_token")
                info = self.token_registry.lookup(token) if self.token_registry else None
                symbol = info.parent_symbol if info else "unknown"
                logger.warning(f"[ZerodhaWS] tick_queue full — dropping tick for token {token} ({symbol})")

    # ─── Re-auth (shared — both WS share the same enctoken) ────────────

    def _trigger_reauth(self):
        """Get a fresh enctoken and reconnect both WS."""
        if not self._reauth_lock.acquire(blocking=False):
            logger.warning("[ZerodhaWS] re-auth already in progress — skipping duplicate")
            return
        self._is_reauthing = True
        try:
            logger.warning("[ZerodhaWS] 403 Forbidden — enctoken expired, re-authing")
            for kt, label in [(self._kt_base, "base"), (self._kt_options, "options")]:
                if kt:
                    try:
                        kt.stop_retry()
                    except Exception:
                        pass

            TELEGRAM_NOTIFICATIONS.send_notification(
                "⚠️ <b>Zerodha WS 403 — enctoken expired</b>\n"
                "Attempting automatic re-login via auth_login.py…",
                parse_mode="HTML",
            )

            import subprocess
            import sys
            import os
            script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "auth", "auth_login.py")
            result = subprocess.run(
                [sys.executable, script, "--force"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                logger.info("[ZerodhaWS] re-auth succeeded — new enctoken written to .env")
                from dotenv import load_dotenv
                load_dotenv(override=True)
                import common.constants as _c
                from urllib.parse import quote as _quote
                new_token = os.getenv(_c.ENV_ZERODHA_ENC_TOKEN, "")
                if new_token:
                    self.update_enctoken(_quote(new_token, safe=""))
                    self.connect()
                    self.subscribe_live_options(wait_for_ticks=True)
            else:
                logger.error(f"[ZerodhaWS] re-auth failed: {result.stderr[:200]}")
                TELEGRAM_NOTIFICATIONS.send_notification(
                    "🚨 <b>Zerodha re-auth FAILED</b> — manual intervention required",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"[ZerodhaWS] re-auth exception: {e}")
        finally:
            self._is_reauthing = False
            self._reauth_lock.release()
