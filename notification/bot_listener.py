from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
from common.constants import TELEGRAM_INTRADAY_TOKEN
from  common.logging_util import logger
import common.shared as shared
import os
import urllib.parse
from datetime import datetime

def is_urlencoded(s: str) -> bool:
    decoded = urllib.parse.unquote(s)
    return decoded != s

def update_enctoken_in_env(new_enctoken):
    env_path = ".env"
    lines = []
    updated = False
    # Read existing .env file
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if line.startswith("ZERODHA_ENC_TOKEN="):
                    lines.append(f"ZERODHA_ENC_TOKEN={new_enctoken}\n")
                    updated = True
                else:
                    lines.append(line)
    # If not present, add it
    if not updated:
        lines.append(f"ZERODHA_ENC_TOKEN={new_enctoken}\n")
    # Write back to .env file
    with open(env_path, "w") as f:
        f.writelines(lines)
    # Update in current environment
    os.environ["ZERODHA_ENC_TOKEN"] = new_enctoken
    logger.info(f"Updated ZERODHA_ENC_TOKEN in .env and current environment to {new_enctoken}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=update.effective_chat.id, text="I'm a bot, please talk to me!")

async def update_enctoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    zerodha_ticker_manager = shared.app_ctx.zd_ticker_manager
    zerodha_kite_connect = shared.app_ctx.zd_kc
    if zerodha_ticker_manager is None:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Zerodha Ticker Manager is not initialized.")
        return

    if zerodha_ticker_manager.connected:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Zerodha Ticker Manager is already connected.")
        return


    if len(context.args) < 1:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Please provide the 2FA code.")
        return
    enc_token = context.args[0]
    
    # if not zerodha_ticker_manager.refresh_enctoken(two_fa_code):
    #     await context.bot.send_message(chat_id=update.effective_chat.id, text="Failed to refresh encToken. Please check your credentials and try again.")
    #     return
    zerodha_ticker_manager.update_enctoken(enc_token)
    if is_urlencoded(enc_token):
        decoded_enctoken = urllib.parse.unquote(enc_token)
    else:
        decoded_enctoken = enc_token
    zerodha_kite_connect.update_enctoken(decoded_enctoken)
    
    if not zerodha_ticker_manager.connect():
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Failed to connect to Zerodha Ticker")
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Connected to Zerodha Ticker")
        update_enctoken_in_env(decoded_enctoken)

        # Subscribe to option tokens for indices that have them registered
        _subscribe_registered_options(zerodha_ticker_manager)


# ═══════════════════════════════════════════════════════════════════════════
# /help — list all available bot commands
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 <b>StockAnalysis Bot — Commands</b>\n\n"
        "/help — Show this help message\n"
        "/status — System health (mode, WebSocket, stock count, trading day)\n"
        "/ltp <code>&lt;SYMBOL&gt;</code> — Last traded price + % change\n"
        "/gainers — Top 5 gainers by % change\n"
        "/losers — Top 5 losers by % change\n"
        "/watchlist — Full subscription overview\n"
        "/holidays — Upcoming NSE market holidays\n"
        "/enctoken <code>&lt;token&gt;</code> — Update Zerodha enctoken\n"
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════════════
# /status — system health snapshot
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = shared.app_ctx

    # Mode
    mode_str = ctx.mode.name if ctx.mode else "NOT SET"

    # WebSocket
    ws_connected = False
    if ctx.zd_ticker_manager is not None:
        ws_connected = ctx.zd_ticker_manager.connected
    ws_icon = "🟢" if ws_connected else "🔴"

    # Counts
    stocks = len(ctx.stock_token_obj_dict)
    indices = len(ctx.index_token_obj_dict)

    # Trading day
    try:
        from common.market_calendar import is_trading_day
        trading = is_trading_day()
        trading_str = "Yes ✅" if trading else "No (Holiday) 🚫"
    except Exception:
        trading_str = "Unknown"

    # Env flags
    production = os.getenv("PRODUCTION", "0") == "1"
    prod_icon = "🟢 ON" if production else "⚪ OFF"

    now = datetime.now().strftime("%H:%M:%S")
    text = (
        "📊 <b>System Status</b>\n\n"
        f"⏰ Time: <code>{now}</code>\n"
        f"📡 Mode: <b>{mode_str}</b>\n"
        f"{ws_icon} WebSocket: <b>{'Connected' if ws_connected else 'Disconnected'}</b>\n"
        f"🏭 Production: {prod_icon}\n"
        f"📈 Stocks tracked: <b>{stocks}</b>\n"
        f"🏦 Indices tracked: <b>{indices}</b>\n"
        f"📅 Trading day: <b>{trading_str}</b>\n"
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════════════
# /ltp <SYMBOL> — last traded price for a stock or index
# ═══════════════════════════════════════════════════════════════════════════

def _find_stock_by_symbol(symbol: str):
    """Look up a Stock object by symbol across all tracked dicts."""
    symbol_upper = symbol.upper().strip()
    for d in (shared.app_ctx.index_token_obj_dict,
              shared.app_ctx.stock_token_obj_dict,
              shared.app_ctx.commodity_token_obj_dict,
              shared.app_ctx.global_indices_token_obj_dict):
        for obj in d.values():
            if obj.stock_symbol.upper() == symbol_upper:
                return obj
    return None


async def cmd_ltp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Usage: /ltp <code>&lt;SYMBOL&gt;</code>\nExample: /ltp RELIANCE",
            parse_mode="HTML",
        )
        return

    symbol = context.args[0].upper().strip()
    stock = _find_stock_by_symbol(symbol)

    if stock is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Symbol <b>{symbol}</b> not found in tracked instruments.",
            parse_mode="HTML",
        )
        return

    ltp = stock.ltp
    change = stock.ltp_change_perc

    if ltp is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ No price data available yet for <b>{symbol}</b>.",
            parse_mode="HTML",
        )
        return

    icon = "🟢" if (change and change > 0) else "🔴" if (change and change < 0) else "⚪"
    change_str = f"{change:+.2f}%" if change is not None else "N/A"

    prev_close = stock.prevDayOHLCV.get("CLOSE") if stock.prevDayOHLCV else None
    prev_str = f"\nPrev Close: <code>{prev_close:.2f}</code>" if prev_close else ""

    text = (
        f"{icon} <b>{stock.stock_symbol}</b>\n"
        f"LTP: <code>{ltp:.2f}</code>  ({change_str})"
        f"{prev_str}"
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════════════
# /gainers & /losers — top 5 movers
# ═══════════════════════════════════════════════════════════════════════════

def _build_gainers_losers():
    """Compute top 5 gainers and losers from live stock data."""
    from common.helperFunctions import percentageChange
    gainers, losers = [], []

    for _, stock in shared.app_ctx.stock_token_obj_dict.items():
        try:
            if stock.ltp is not None and stock.prevDayOHLCV is not None:
                prev_close = stock.prevDayOHLCV.get("CLOSE")
                if prev_close and prev_close > 0:
                    change = percentageChange(stock.ltp, prev_close)
                    if isinstance(change, float) and change == change:  # NaN guard
                        if change > 0:
                            gainers.append((stock.stock_symbol, change))
                        else:
                            losers.append((stock.stock_symbol, change))
        except Exception:
            continue

    gainers.sort(key=lambda x: x[1], reverse=True)
    losers.sort(key=lambda x: x[1])
    return gainers[:5], losers[:5]


async def cmd_gainers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gainers, _ = _build_gainers_losers()
    if not gainers:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text="No gainer data available yet."
        )
        return

    text = "📈 <b>Top 5 Gainers</b>\n\n"
    for i, (sym, pct) in enumerate(gainers, 1):
        text += f"  🟢 {i}. <b>{sym}</b>: <code>{pct:+.2f}%</code>\n"
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
    )


async def cmd_losers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, losers = _build_gainers_losers()
    if not losers:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text="No loser data available yet."
        )
        return

    text = "📉 <b>Top 5 Losers</b>\n\n"
    for i, (sym, pct) in enumerate(losers, 1):
        text += f"  🔴 {i}. <b>{sym}</b>: <code>{pct:.2f}%</code>\n"
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════════════
# /watchlist — full subscription overview
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ctx = shared.app_ctx
    parts = []

    # ── 1. yFinance stocks (from final_derivatives_list.json) ────────────
    stocks = sorted(ctx.stocks_list)
    indices = sorted(ctx.index_list)
    commodities = sorted(ctx.commodity_list)
    globals_list = sorted(ctx.global_indices_list)

    parts.append("📋 <b>Subscription Overview</b>\n")

    parts.append(f"<b>🏦 Indices ({len(indices)})</b>")
    parts.append(f"<code>{', '.join(indices) if indices else '—'}</code>\n")

    parts.append(f"<b>📈 F&amp;O Stocks ({len(stocks)})</b>")
    if stocks:
        # Show first/last few to keep message manageable
        if len(stocks) <= 20:
            parts.append(f"<code>{', '.join(stocks)}</code>")
        else:
            preview = stocks[:10] + ["…"] + stocks[-5:]
            parts.append(f"<code>{', '.join(preview)}</code>")
    else:
        parts.append("<code>—</code>")
    parts.append("")

    parts.append(f"<b>🛢 Commodities ({len(commodities)})</b>")
    parts.append(f"<code>{', '.join(commodities) if commodities else '—'}</code>\n")

    parts.append(f"<b>🌍 Global Indices ({len(globals_list)})</b>")
    parts.append(f"<code>{', '.join(globals_list) if globals_list else '—'}</code>\n")

    # ── 2. Zerodha WebSocket subscriptions ───────────────────────────────
    ws_connected = False
    tm = ctx.zd_ticker_manager
    if tm is not None:
        ws_connected = tm.connected
    ws_icon = "🟢" if ws_connected else "🔴"
    parts.append(f"{ws_icon} <b>Zerodha WebSocket</b>: {'Connected' if ws_connected else 'Disconnected'}")

    if ws_connected and tm is not None:
        idx_only = getattr(tm, "index_only_mode", False)
        eq_tokens = len(ctx.stock_token_obj_dict) if not idx_only else 0
        idx_tokens = len(ctx.index_token_obj_dict)
        parts.append(f"  Mode: <b>{'Index-only' if idx_only else 'Full (Equity + Index)'}</b>")
        parts.append(f"  Equity tokens: <b>{eq_tokens}</b>")
        parts.append(f"  Index tokens: <b>{idx_tokens}</b>")

    # ── 3. Options subscriptions via TokenRegistry ───────────────────────
    registry = ctx.token_registry
    if registry is not None:
        try:
            stats = registry.get_stats()
            total_reg = stats.get("total_registered", 0)
            total_sub = stats.get("subscribed", 0)
            by_type = stats.get("by_type", {})
            parts.append("")
            parts.append(f"<b>📊 Token Registry</b>")
            parts.append(f"  Registered: <b>{total_reg}</b> | Subscribed: <b>{total_sub}</b>")
            if by_type:
                type_parts = [f"{k}: {v}" for k, v in sorted(by_type.items())]
                parts.append(f"  By type: {', '.join(type_parts)}")

            # Per-index option subscription details
            from common.constants import LIVE_OPTIONS_INDICES
            from common.token_registry import TokenType, OptionZone
            for symbol in LIVE_OPTIONS_INDICES:
                opt_tokens = registry.get_tokens_by_type(symbol, TokenType.OPTION)
                if not opt_tokens:
                    continue
                zone_counts = {}
                for zone in OptionZone:
                    count = len(registry.get_option_tokens_by_zone(symbol, zone))
                    if count:
                        zone_counts[zone.name] = count
                total_opt = len(opt_tokens)
                index_obj = registry.get_parent_object(symbol)
                spot = ""
                if index_obj:
                    spot_val = getattr(index_obj, "ltp", None)
                    if spot_val:
                        spot = f" (spot {spot_val:.0f})"
                zone_str = ", ".join(f"{z}: {c}" for z, c in zone_counts.items())
                parts.append(f"  <b>{symbol}</b>{spot}: {total_opt} options [{zone_str}]")
        except Exception as exc:
            parts.append(f"  ⚠️ Registry error: {exc}")

    # ── 4. Futures data from Zerodha API ─────────────────────────────────
    futures_symbols = []
    for token, stock in ctx.index_token_obj_dict.items():
        zctx = getattr(stock, "zerodha_ctx", None)
        if zctx and zctx.get("futures_mdata", {}).get("current"):
            live = getattr(stock, "futures_live", {})
            cur = live.get("current", {})
            ltp_val = cur.get("ltp")
            oi_val = cur.get("oi")
            info = f"<b>{stock.stock_symbol}</b>"
            if ltp_val:
                info += f" LTP: <code>{ltp_val:.2f}</code>"
            if oi_val:
                info += f" OI: <code>{oi_val:,}</code>"
            futures_symbols.append(info)

    if futures_symbols:
        parts.append("")
        parts.append(f"<b>📑 Futures (live via Zerodha)</b>")
        for f in futures_symbols:
            parts.append(f"  {f}")

    # ── 5. Expiry info ───────────────────────────────────────────────────
    if ctx.stockExpires:
        parts.append("")
        parts.append("<b>📆 Tracked Expiries</b>")
        for i, exp in enumerate(ctx.stockExpires):
            label = "Current" if i == 0 else "Next"
            parts.append(f"  {label}: <code>{exp}</code>")

    # ── 6. Total summary line ────────────────────────────────────────────
    total = len(ctx.stock_token_obj_dict) + len(ctx.index_token_obj_dict) + \
            len(ctx.commodity_token_obj_dict) + len(ctx.global_indices_token_obj_dict)
    parts.append(f"\n<b>Total instruments: {total}</b>")

    text = "\n".join(parts)

    # Telegram messages have a 4096-char limit; split if needed
    if len(text) <= 4096:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
        )
    else:
        # Split at double newlines
        chunks = []
        current = ""
        for line in parts:
            candidate = current + "\n" + line if current else line
            if len(candidate) > 4000:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        for chunk in chunks:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=chunk, parse_mode="HTML"
            )


# ═══════════════════════════════════════════════════════════════════════════
# /holidays — upcoming NSE market holidays
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_holidays(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        from common.market_calendar import is_trading_day, get_upcoming_holidays

        today = datetime.now().date()
        trading_today = is_trading_day(today)
        today_str = today.strftime("%A, %d %b %Y")

        holidays = get_upcoming_holidays(days_ahead=30)
        text = "📅 <b>NSE Market Holidays</b>\n\n"
        text += f"Today ({today_str}): <b>{'Open ✅' if trading_today else 'Closed 🚫'}</b>\n\n"

        if holidays:
            text += f"<b>{len(holidays)} holiday(s) in the next 30 days:</b>\n"
            for h in holidays:
                day_name = h.strftime("%A")
                date_str = h.strftime("%d %b %Y")
                text += f"  ⚠️ {day_name}, {date_str}\n"
        else:
            text += "No holidays in the next 30 days. 🟢"

    except Exception as exc:
        logger.error(f"Error in /holidays command: {exc}")
        text = f"❌ Could not fetch holiday data: {exc}"

    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
    )


def _subscribe_registered_options(ticker_manager):
    """After WebSocket connects, subscribe to option tokens for LIVE_OPTIONS_INDICES only."""
    registry = shared.app_ctx.token_registry
    if registry is None:
        return

    from common.token_registry import TokenType, OptionZone
    from common.constants import LIVE_OPTIONS_INDICES
    from notification.Notification import TELEGRAM_NOTIFICATIONS
    import time

    # Wait briefly for the first index ticks to arrive with spot prices
    time.sleep(2)

    total_option_tokens = 0
    option_lines = []

    for token, index_obj in shared.app_ctx.index_token_obj_dict.items():
        symbol = index_obj.stock_symbol

        # Only subscribe options for high-liquidity indices (NIFTY, BANKNIFTY)
        if symbol not in LIVE_OPTIONS_INDICES:
            continue

        option_tokens = registry.get_tokens_by_type(symbol, TokenType.OPTION)
        if not option_tokens:
            continue

        # Get spot price from zerodha_data or ltp
        spot = index_obj.zerodha_data.get("last_price") or index_obj.ltp
        if not spot or spot <= 0:
            logger.warning(f"No spot price for {symbol}, skipping option subscription")
            continue

        ticker_manager.subscribe_options_for_symbol(symbol, spot)
        logger.info(f"Option subscription initiated for {symbol} at spot {spot}")

        # Count only actually-subscribed tokens (zone-filtered ≤5% of spot)
        subscribed_count = sum(
            len(registry.get_option_tokens_by_zone(symbol, zone))
            for zone in OptionZone
        )
        total_option_tokens += subscribed_count
        option_lines.append(f"  {symbol}: {subscribed_count} tokens (spot {spot:.0f})")

    # Send subscription summary to Telegram
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


def init_telegram_bot():
    logger.info("Initializing Telegram Bot...")
    application = ApplicationBuilder().token(TELEGRAM_INTRADAY_TOKEN).build()
    
    start_handler = CommandHandler('start', start)
    caps_handler = CommandHandler('enctoken', update_enctoken)
    application.add_handler(start_handler)
    application.add_handler(caps_handler)
    application.add_handler(CommandHandler('help', cmd_help))
    application.add_handler(CommandHandler('status', cmd_status))
    application.add_handler(CommandHandler('ltp', cmd_ltp))
    application.add_handler(CommandHandler('gainers', cmd_gainers))
    application.add_handler(CommandHandler('losers', cmd_losers))
    application.add_handler(CommandHandler('watchlist', cmd_watchlist))
    application.add_handler(CommandHandler('holidays', cmd_holidays))
    
    application.run_polling()

