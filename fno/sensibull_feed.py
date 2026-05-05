"""
SensibullFeed — public Sensibull wsrelay WebSocket client.

Connects to wss://wsrelay.sensibull.com without any authentication and
subscribes to live option-chain snapshots for one or more underlyings.

Binary frame format (reverse-engineered):
  - Single byte 0xFD  → heartbeat, ignored
  - Longer frames     → [1-byte type][4-byte instrument_token BE][8-byte ASCII expiry][gzip JSON]

Usage::

    feed = SensibullFeed(
        subscriptions=[{"underlying": 260105, "expiry": "2026-05-26"}],
        on_snapshot=lambda token, data: handle(token, data),
    )
    feed.start()
    ...
    feed.stop()
"""
from __future__ import annotations

import gzip
import json
import threading
from typing import Callable

import websocket

from common.logging_util import logger


WS_URL = "wss://wsrelay.sensibull.com/broker/1?consumerType=platform_no_plan"
_ORIGIN = "https://web.sensibull.com"
# websocket-client auto-injects an Origin header from the WS URL which causes
# Cloudflare to return 403 "invalid origin".  We suppress the auto-header by
# supplying only the headers we want, and pass origin= to run_forever() so
# websocket-client sets the correct single Origin header during the handshake.
_WS_HEADERS = [
    "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Cache-Control: no-cache",
    "Pragma: no-cache",
]


class SensibullFeed:
    """
    WebSocket client for the Sensibull public option-chain relay.

    Parameters
    ----------
    subscriptions:
        List of dicts with keys ``underlying`` (int NSE token) and
        ``expiry`` (str "YYYY-MM-DD").
    on_snapshot:
        Callback fired on each decoded data frame.
        Signature: ``(underlying_token: int, data: dict) -> None``
    """

    def __init__(
        self,
        subscriptions: list[dict],
        on_snapshot: Callable[[int, dict], None],
    ) -> None:
        self._subscriptions = subscriptions
        self._on_snapshot = on_snapshot
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the WebSocket in a background daemon thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="SensibullFeed")
        self._thread.start()
        logger.info(f"[SensibullFeed] started — {len(self._subscriptions)} subscription(s)")

    def stop(self) -> None:
        """Request a clean shutdown."""
        self._stop_event.set()
        if self._ws:
            self._ws.close()
        logger.info("[SensibullFeed] stop requested")

    # ── internal ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        self._ws = websocket.WebSocketApp(
            WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            header=_WS_HEADERS,
        )
        # run_forever reconnects automatically; ping_interval keeps the connection alive.
        # origin= is passed here (not in header=) to avoid a duplicate Origin header
        # that Cloudflare rejects with 403.
        while not self._stop_event.is_set():
            try:
                self._ws.run_forever(
                    origin=_ORIGIN,
                    reconnect=5,
                    ping_interval=20,
                    ping_timeout=10,
                )
            except Exception as exc:
                logger.error(f"[SensibullFeed] run_forever raised: {exc}")
            if not self._stop_event.is_set():
                logger.warning("[SensibullFeed] disconnected — will reconnect in 5s")

    def _on_open(self, ws) -> None:
        subscribe_msg = {
            "msgCommand": "subscribe",
            "dataSource": "option-chain",
            "brokerId": 1,
            "tokens": [],
            "underlyingExpiry": self._subscriptions,
            "uniqueId": "",
        }
        ws.send(json.dumps(subscribe_msg))
        logger.info(f"[SensibullFeed] subscribed: {self._subscriptions}")

    def _on_message(self, ws, raw: bytes) -> None:
        data = _decode_frame(raw)
        if data is None:
            return

        # The header carries the underlying instrument_token in bytes 1–4
        underlying_token = data.pop("_underlying_token", None)
        if underlying_token is None:
            return

        try:
            self._on_snapshot(underlying_token, data)
        except Exception as exc:
            logger.error(f"[SensibullFeed] on_snapshot error (token={underlying_token}): {exc}")

    def _on_error(self, ws, error) -> None:
        logger.error(f"[SensibullFeed] error: {error}")

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.info(f"[SensibullFeed] closed (code={close_status_code}, msg={close_msg})")


# ── frame decoder (module-level so it can be unit-tested independently) ───────

def _decode_frame(raw: bytes) -> dict | None:
    """
    Decode a Sensibull binary WebSocket frame.

    Frame layout:
      - 1 byte  : 0xFD → heartbeat (return None)
      - 1 byte  : message type
      - 4 bytes : underlying instrument_token (big-endian uint32)
      - 8 bytes : ASCII expiry string "YYYYMMDD"
      - rest    : gzip-compressed JSON payload

    Returns decoded dict with injected keys ``_underlying_token`` and
    ``_header_expiry``, or None for heartbeats / decode failures.
    """
    if len(raw) <= 2:
        return None  # heartbeat

    # Locate the gzip magic bytes to find where the payload starts
    idx = raw.find(b"\x1f\x8b")
    if idx == -1:
        logger.debug(f"[SensibullFeed] no gzip magic in {len(raw)}-byte frame")
        return None

    header = raw[:idx]

    # Extract underlying token from bytes 1–4 (big-endian)
    underlying_token: int | None = None
    if len(header) >= 5:
        underlying_token = int.from_bytes(header[1:5], byteorder="big")

    # Extract ASCII expiry from bytes 5–12
    expiry_str = ""
    if len(header) >= 13:
        expiry_str = header[5:13].decode("ascii", errors="replace")

    try:
        payload = gzip.decompress(raw[idx:])
        data = json.loads(payload)
        data["_underlying_token"] = underlying_token
        data["_header_expiry"] = expiry_str
        return data
    except Exception as exc:
        logger.warning(f"[SensibullFeed] decode failed: {exc}")
        return None
