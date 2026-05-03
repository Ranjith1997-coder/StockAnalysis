"""Account & connection commands: /start, /enctoken."""
from __future__ import annotations

import time
import urllib.parse
import os

from telegram import Update
from telegram.ext import ContextTypes

import common.shared as shared
from common.logging_util import logger


def _is_urlencoded(s: str) -> bool:
    decoded = urllib.parse.unquote(s)
    return decoded != s


def _update_enctoken_in_env(new_enctoken: str) -> None:
    env_path = ".env"
    lines = []
    updated = False
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.startswith("ZERODHA_ENC_TOKEN="):
                    lines.append(f"ZERODHA_ENC_TOKEN={new_enctoken}\n")
                    updated = True
                else:
                    lines.append(line)
    if not updated:
        lines.append(f"ZERODHA_ENC_TOKEN={new_enctoken}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)
    os.environ["ZERODHA_ENC_TOKEN"] = new_enctoken
    logger.info(f"Updated ZERODHA_ENC_TOKEN in .env and environment")


def _subscribe_registered_options(ticker_manager) -> None:
    """After WebSocket connects, subscribe to option tokens for LIVE_OPTIONS_INDICES only."""
    registry = shared.app_ctx.token_registry
    if registry is None:
        return

    from common.token_registry import TokenType, OptionZone
    from common.constants import LIVE_OPTIONS_INDICES
    from notification.Notification import TELEGRAM_NOTIFICATIONS

    # Wait briefly for the first index ticks to arrive with spot prices
    time.sleep(2)

    total_option_tokens = 0
    option_lines = []

    for token, index_obj in shared.app_ctx.index_token_obj_dict.items():
        symbol = index_obj.stock_symbol

        if symbol not in LIVE_OPTIONS_INDICES:
            continue

        option_tokens = registry.get_tokens_by_type(symbol, TokenType.OPTION)
        if not option_tokens:
            continue

        spot = index_obj.zerodha_data.get("last_price") or index_obj.ltp
        if not spot or spot <= 0:
            logger.warning(f"No spot price for {symbol}, skipping option subscription")
            continue

        ticker_manager.subscribe_options_for_symbol(symbol, spot)
        logger.info(f"Option subscription initiated for {symbol} at spot {spot}")

        subscribed_count = sum(
            len(registry.get_option_tokens_by_zone(symbol, zone))
            for zone in OptionZone
        )
        total_option_tokens += subscribed_count
        option_lines.append(f"  {symbol}: {subscribed_count} tokens (spot {spot:.0f})")

    index_count = len(shared.app_ctx.index_token_obj_dict)
    equity_count = len(shared.app_ctx.stock_token_obj_dict)
    base_count = index_count + (0 if ticker_manager.index_only_mode else equity_count)
    total = base_count + total_option_tokens

    mode_note = "index-only" if ticker_manager.index_only_mode else f"{equity_count} equity + {index_count} index"
    lines = [
        "WebSocket connected",
        f"Base: {base_count} ({mode_note})",
        f"Options: {total_option_tokens}",
    ]
    lines.extend(option_lines)
    lines.append(f"Total: {total} / 500")

    TELEGRAM_NOTIFICATIONS.send_notification("\n".join(lines))
    logger.info(f"WebSocket subscription summary — total {total} tokens")


# ─── Handlers ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="I'm a bot, please talk to me!",
    )


async def cmd_enctoken(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    zerodha_ticker_manager = shared.app_ctx.zd_ticker_manager
    zerodha_kite_connect = shared.app_ctx.zd_kc

    if zerodha_ticker_manager is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Zerodha Ticker Manager is not initialized.",
        )
        return

    if zerodha_ticker_manager.connected:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Zerodha Ticker Manager is already connected.",
        )
        return

    if len(context.args) < 1:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Please provide the enctoken.",
        )
        return

    enc_token = context.args[0]
    decoded_enctoken = urllib.parse.unquote(enc_token) if _is_urlencoded(enc_token) else enc_token

    zerodha_ticker_manager.update_enctoken(enc_token)
    zerodha_kite_connect.update_enctoken(decoded_enctoken)

    if not zerodha_ticker_manager.connect():
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Failed to connect to Zerodha Ticker",
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Connected to Zerodha Ticker",
        )
        _update_enctoken_in_env(decoded_enctoken)
        _subscribe_registered_options(zerodha_ticker_manager)


HANDLERS = [
    ("start", cmd_start),
    ("enctoken", cmd_enctoken),
]
