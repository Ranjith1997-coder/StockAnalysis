"""Market data commands: /ltp, /gainers, /losers, /watchlist, /holidays, /straddle, /walls."""
from __future__ import annotations

import time
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

import common.shared as shared
from common.logging_util import logger
from notification.commands._helpers import find_stock_by_symbol, build_gainers_losers

# Supported symbols for options commands
_OPTIONS_SYMBOLS = {"NIFTY", "BANKNIFTY"}
_STALE_THRESHOLD_SECS = 30


def _fmt_oi(oi: int) -> str:
    """Format OI in compact Indian units."""
    if oi >= 10_000_000:
        return f"{oi / 10_000_000:.2f}Cr"
    if oi >= 100_000:
        return f"{oi / 100_000:.2f}L"
    return f"{oi:,}"


def _staleness_tag(last_updated: float) -> str:
    """Return an inline staleness warning if data is older than threshold."""
    if last_updated == 0.0:
        return ""
    lag = time.time() - last_updated
    if lag > _STALE_THRESHOLD_SECS:
        return f"  ⚠️ <i>Data is {lag:.0f}s old — may not reflect current market</i>"
    return ""


def _session_wall_delta(symbol: str, wall_type: str, minutes: int = 375) -> tuple[int, int] | None:
    """
    Returns (open_oi, current_oi) for the dominant OI wall since session open.
    Uses LiveOptionsHistory via live_options_engine. Returns None if no history yet
    or if the wall strike migrated during the window.
    """
    import common.shared as _shared
    tm = _shared.app_ctx.zd_ticker_manager
    if tm is None:
        return None
    engine = getattr(tm, "live_options_engine", None)
    if engine is None:
        return None
    history = engine.get_history(symbol)
    if history is None or history.size() < 2:
        return None
    return history.wall_oi_trend(wall_type, minutes)


def _resolve_options_stock(symbol: str):
    """
    Returns (stock, error_text). stock is None when error_text is set.
    Validates symbol is in _OPTIONS_SYMBOLS and has live options data.
    """
    if not symbol:
        return None, (
            "Usage: <code>/straddle NIFTY</code> or <code>/straddle BANKNIFTY</code>\n"
            f"Supported: {', '.join(sorted(_OPTIONS_SYMBOLS))}"
        )
    if symbol not in _OPTIONS_SYMBOLS:
        return None, (
            f"❌ Options data is only available for: "
            f"<b>{', '.join(sorted(_OPTIONS_SYMBOLS))}</b>"
        )
    stock = find_stock_by_symbol(symbol)
    if stock is None:
        return None, f"❌ <b>{symbol}</b> not found in tracked instruments."
    agg = getattr(stock, "options_aggregate", None)
    if agg is None or agg.get("last_updated", 0.0) == 0.0:
        return None, (
            f"⚠️ No options data yet for <b>{symbol}</b>.\n"
            "WebSocket may not be connected or options not yet subscribed."
        )
    return stock, None


async def cmd_ltp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Usage: /ltp <code>&lt;SYMBOL&gt;</code>\nExample: /ltp RELIANCE",
            parse_mode="HTML",
        )
        return

    symbol = context.args[0].upper().strip()
    stock = find_stock_by_symbol(symbol)

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


async def cmd_gainers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    gainers, _ = build_gainers_losers()
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


async def cmd_losers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, losers = build_gainers_losers()
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


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = shared.app_ctx
    parts = []

    stocks = sorted(ctx.stocks_list)
    indices = sorted(ctx.index_list)
    commodities = sorted(ctx.commodity_list)
    globals_list = sorted(ctx.global_indices_list)

    parts.append("📋 <b>Subscription Overview</b>\n")

    parts.append(f"<b>🏦 Indices ({len(indices)})</b>")
    parts.append(f"<code>{', '.join(indices) if indices else '—'}</code>\n")

    parts.append(f"<b>📈 F&amp;O Stocks ({len(stocks)})</b>")
    if stocks:
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

    registry = ctx.token_registry
    if registry is not None:
        try:
            from common.token_registry import TokenType, OptionZone
            from common.constants import LIVE_OPTIONS_INDICES

            stats = registry.get_stats()
            total_reg = stats.get("total_registered", 0)
            total_sub = stats.get("subscribed", 0)
            by_type = stats.get("by_type", {})
            parts.append("")
            parts.append("<b>📊 Token Registry</b>")
            parts.append(f"  Registered: <b>{total_reg}</b> | Subscribed: <b>{total_sub}</b>")
            if by_type:
                type_parts = [f"{k}: {v}" for k, v in sorted(by_type.items())]
                parts.append(f"  By type: {', '.join(type_parts)}")

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
        parts.append("<b>📑 Futures (live via Zerodha)</b>")
        for f in futures_symbols:
            parts.append(f"  {f}")

    if ctx.stockExpires:
        parts.append("")
        parts.append("<b>📆 Tracked Expiries</b>")
        for i, exp in enumerate(ctx.stockExpires):
            label = "Current" if i == 0 else "Next"
            parts.append(f"  {label}: <code>{exp}</code>")

    total = (
        len(ctx.stock_token_obj_dict)
        + len(ctx.index_token_obj_dict)
        + len(ctx.commodity_token_obj_dict)
        + len(ctx.global_indices_token_obj_dict)
    )
    parts.append(f"\n<b>Total instruments: {total}</b>")

    text = "\n".join(parts)

    if len(text) <= 4096:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
        )
    else:
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


async def cmd_holidays(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


# ═══════════════════════════════════════════════════════════════════════════
# /straddle <SYMBOL> — ATM straddle premium + expected 1-SD range
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_straddle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbol = context.args[0].upper().strip() if context.args else ""
    stock, err = _resolve_options_stock(symbol)
    if err:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=err, parse_mode="HTML"
        )
        return

    agg = stock.options_aggregate
    spot = stock.ltp or 0.0
    atm = agg.get("atm_strike")
    straddle = agg.get("atm_straddle_premium", 0.0)
    pcr = agg.get("live_pcr", 0.0)
    total_ce_oi = agg.get("total_ce_oi", 0)
    total_pe_oi = agg.get("total_pe_oi", 0)
    last_updated = agg.get("last_updated", 0.0)
    stale = _staleness_tag(last_updated)

    if atm is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ ATM strike not yet determined for <b>{symbol}</b>. Spot price may not have arrived yet.",
            parse_mode="HTML",
        )
        return

    # Individual ATM leg LTPs from options_live
    options_live = getattr(stock, "options_live", {})
    atm_data = options_live.get(atm, {})
    ce_ltp = atm_data.get("CE", {}).get("ltp", 0.0)
    pe_ltp = atm_data.get("PE", {}).get("ltp", 0.0)

    # 1-SD expected range = spot ± straddle premium
    lower = spot - straddle
    upper = spot + straddle
    straddle_pct = (straddle / spot * 100) if spot > 0 else 0.0

    age_str = f"{time.time() - last_updated:.0f}s ago" if last_updated > 0 else ""

    lines = [
        f"📊 <b>{symbol}</b>  —  Straddle Snapshot",
        f"⏱ {datetime.now().strftime('%H:%M:%S')}  ({age_str})",
        "",
        f"Spot:      <code>{spot:,.2f}</code>",
        f"ATM:       <code>{atm:,.0f}</code>",
        "",
        f"Straddle:  <b><code>{straddle:.2f} pts</code></b>  ({straddle_pct:.2f}% of spot)",
        f"Expected Range:  <code>{lower:,.2f}</code>  ↔  <code>{upper:,.2f}</code>",
        "",
        f"CE leg (ATM {atm:,.0f}):  <code>{ce_ltp:.2f}</code>",
        f"PE leg (ATM {atm:,.0f}):  <code>{pe_ltp:.2f}</code>",
        "",
        f"PCR: <code>{pcr:.2f}</code>  |  CE OI: <code>{_fmt_oi(total_ce_oi)}</code>  |  PE OI: <code>{_fmt_oi(total_pe_oi)}</code>",
    ]
    if stale:
        lines.append(stale)

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n".join(lines),
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════════════════════
# /walls <SYMBOL> — institutional OI walls (support & resistance)
# ═══════════════════════════════════════════════════════════════════════════

async def cmd_walls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbol = context.args[0].upper().strip() if context.args else ""
    stock, err = _resolve_options_stock(symbol)
    if err:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=err, parse_mode="HTML"
        )
        return

    agg = stock.options_aggregate
    spot = stock.ltp or 0.0
    ce_wall = agg.get("max_oi_ce_strike")
    pe_wall = agg.get("max_oi_pe_strike")
    last_updated = agg.get("last_updated", 0.0)
    stale = _staleness_tag(last_updated)

    if ce_wall is None or pe_wall is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ OI wall data not yet computed for <b>{symbol}</b>. Options ticks may still be arriving.",
            parse_mode="HTML",
        )
        return

    # OI and net tick change for each wall from options_live
    options_live = getattr(stock, "options_live", {})

    ce_wall_data = options_live.get(ce_wall, {}).get("CE", {})
    ce_wall_oi = ce_wall_data.get("oi", 0)
    ce_wall_tick_net = ce_wall_oi - ce_wall_data.get("prev_oi", ce_wall_oi)

    pe_wall_data = options_live.get(pe_wall, {}).get("PE", {})
    pe_wall_oi = pe_wall_data.get("oi", 0)
    pe_wall_tick_net = pe_wall_oi - pe_wall_data.get("prev_oi", pe_wall_oi)

    # Session-level delta from LiveOptionsHistory (open-of-day vs now)
    ce_session = _session_wall_delta(symbol, "CE")
    pe_session = _session_wall_delta(symbol, "PE")

    # Gaps from spot to each wall
    ce_gap = ce_wall - spot if spot > 0 else 0.0
    pe_gap = spot - pe_wall if spot > 0 else 0.0
    ce_gap_pct = (ce_gap / spot * 100) if spot > 0 else 0.0
    pe_gap_pct = (pe_gap / spot * 100) if spot > 0 else 0.0

    def _tick_str(net: int) -> str:
        """Tick-to-tick delta label."""
        if net == 0:
            return "no change"
        return f"{'+' if net > 0 else ''}{_fmt_oi(abs(net))} {'added' if net > 0 else 'shed'} (last tick)"

    def _session_str(pair: tuple[int, int] | None) -> str:
        """Session delta label: open-of-day vs now."""
        if pair is None:
            return ""
        open_oi, now_oi = pair
        delta = now_oi - open_oi
        if delta == 0:
            return "  Session: no change vs open"
        sign = "+" if delta > 0 else ""
        return f"  Session: <b>{sign}{_fmt_oi(abs(delta))} {'built' if delta > 0 else 'unwound'}</b> vs open ({_fmt_oi(open_oi)} → {_fmt_oi(now_oi)})"

    age_str = f"{time.time() - last_updated:.0f}s ago" if last_updated > 0 else ""

    lines = [
        f"🧱 <b>{symbol}</b>  —  OI Walls",
        f"⏱ {datetime.now().strftime('%H:%M:%S')}  ({age_str})",
        "",
        f"Spot:  <code>{spot:,.2f}</code>",
        "",
        f"🔴 CE Wall (Resistance):  <b><code>{ce_wall:,.0f}</code></b>",
        f"   OI: <code>{_fmt_oi(ce_wall_oi)}</code>  |  {_tick_str(ce_wall_tick_net)}",
    ]
    if ce_session:
        lines.append(_session_str(ce_session))
    lines += [
        "",
        f"🟢 PE Wall (Support):     <b><code>{pe_wall:,.0f}</code></b>",
        f"   OI: <code>{_fmt_oi(pe_wall_oi)}</code>  |  {_tick_str(pe_wall_tick_net)}",
    ]
    if pe_session:
        lines.append(_session_str(pe_session))
    lines += [
        "",
        f"Gap to CE wall:  <code>{ce_gap:,.0f} pts</code>  ({ce_gap_pct:.2f}%)",
        f"Gap to PE wall:  <code>{pe_gap:,.0f} pts</code>  ({pe_gap_pct:.2f}%)",
        "",
        f"💡 Iron Condor zone:  Sell CE above <b>{ce_wall:,.0f}</b>  |  Sell PE below <b>{pe_wall:,.0f}</b>",
    ]
    if stale:
        lines.append(stale)

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="\n".join(lines),
        parse_mode="HTML",
    )


HANDLERS = [
    ("ltp", cmd_ltp),
    ("gainers", cmd_gainers),
    ("losers", cmd_losers),
    ("watchlist", cmd_watchlist),
    ("holidays", cmd_holidays),
    ("straddle", cmd_straddle),
    ("walls", cmd_walls),
]
