"""System commands: /help, /status (System Health Dashboard).

Also provides the LLM budget alert job for the telegram JobQueue.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

import common.shared as shared
from common.logging_util import logger
from notification.commands._guard import guard

# ─── Thresholds ──────────────────────────────────────────────────────────────

_RAM_WARN_PCT = 60      # yellow
_RAM_CRIT_PCT = 80      # red
_FEED_STALE_SECS = 30   # lag threshold during market hours
_LLM_WARN_PCT = 0.80    # 80 % of daily token budget
_MARKET_OPEN_IST = (9 * 60 + 15)   # 09:15 in minutes since midnight
_MARKET_CLOSE_IST = (15 * 60 + 30) # 15:30


def _is_market_hours() -> bool:
    """Return True if current IST time is within market hours."""
    try:
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.now(tz=ist)
    except ImportError:
        # Fallback: assume UTC+5:30
        from datetime import timedelta
        now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    minutes = now_ist.hour * 60 + now_ist.minute
    return _MARKET_OPEN_IST <= minutes <= _MARKET_CLOSE_IST


def _feed_health_lines() -> list[str]:
    """Build feed health section for /status."""
    lines = ["", "📡 <b>Feed Health</b>"]
    ctx = shared.app_ctx
    now = time.time()

    # Read equity tick time from market-data service registry (fallback to monolith)
    last_equity_tick = 0.0
    try:
        from notification.commands._helpers import _get_redis
        _r = _get_redis()
        if _r is not None:
            _md = _r.hgetall("service:registry:market-data")
            if _md.get("status") == "healthy":
                last_equity_tick = float(_md.get("last_equity_tick", 0))
    except Exception:
        pass
    if last_equity_tick == 0.0:
        last_equity_tick = ctx.last_equity_tick_time

    # Equity tick lag
    if last_equity_tick == 0.0:
        lines.append("  Equity ticks: ⚪ No ticks received yet")
    else:
        lag = now - last_equity_tick
        if not _is_market_hours():
            lines.append(f"  Equity ticks: ⚪ Market closed (last: {lag:.0f}s ago)")
        elif lag <= _FEED_STALE_SECS:
            lines.append(f"  Equity ticks: 🟢 {lag:.0f}s ago")
        else:
            lines.append(f"  Equity ticks: 🔴 <b>{lag:.0f}s ago — STALE!</b>")

    # Options aggregate lag — read from Redis hashes (market-data service snapshots)
    opt_lags = []
    try:
        from notification.commands._helpers import _get_redis
        _r = _get_redis()
        if _r is not None:
            for _, index_obj in ctx.index_token_obj_dict.items():
                agg_raw = _r.hgetall(f"data:options_agg:{index_obj.stock_symbol}")
                if agg_raw:
                    last_val = agg_raw.get("last_updated")
                    if last_val:
                        try:
                            last_opt = float(last_val)
                            if last_opt > 0:
                                opt_lags.append((index_obj.stock_symbol, now - last_opt))
                        except (ValueError, TypeError):
                            pass
    except Exception:
        pass

    if not opt_lags:
        # Fallback: read from in-memory TickStore
        for _, index_obj in ctx.index_token_obj_dict.items():
            ts_obj = getattr(index_obj, "_tick_store", None)
            if ts_obj is None:
                ts_obj = index_obj
            last_opt = getattr(ts_obj, "options_aggregate", {}).get("last_updated", 0.0)
            if last_opt > 0:
                opt_lags.append((index_obj.stock_symbol, now - last_opt))

    if not opt_lags:
        lines.append("  Options feed: ⚪ No options data yet")
    else:
        for sym, lag in opt_lags:
            if not _is_market_hours():
                lines.append(f"  Options ({sym}): ⚪ Market closed (last: {lag:.0f}s ago)")
            elif lag <= _FEED_STALE_SECS:
                lines.append(f"  Options ({sym}): 🟢 {lag:.0f}s ago")
            else:
                lines.append(f"  Options ({sym}): 🔴 <b>{lag:.0f}s ago — STALE!</b>")

    return lines


def _ram_health_lines() -> list[str]:
    """Build RAM section for /status using psutil."""
    lines = ["", "💾 <b>Memory (RAM)</b>"]
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        rss_bytes = proc.memory_info().rss
        rss_mb = rss_bytes / (1024 ** 2)
        vm = psutil.virtual_memory()
        total_mb = vm.total / (1024 ** 2)
        used_pct = (rss_bytes / vm.total) * 100

        if used_pct < _RAM_WARN_PCT:
            icon = "🟢"
        elif used_pct < _RAM_CRIT_PCT:
            icon = "🟡"
        else:
            icon = "🔴"

        lines.append(
            f"  Process RSS: {icon} <code>{rss_mb:.0f} MB</code> / "
            f"<code>{total_mb / 1024:.1f} GB</code> "
            f"(<code>{used_pct:.1f}%</code>)"
        )
        lines.append(
            f"  System used: <code>{vm.percent:.1f}%</code> "
            f"(avail <code>{vm.available / (1024**3):.1f} GB</code>)"
        )
    except ImportError:
        lines.append("  ⚠️ psutil not installed — run: pip install psutil")
    except Exception as exc:
        lines.append(f"  ⚠️ Could not read memory: {exc}")
    return lines


def _llm_budget_lines() -> list[str]:
    """Build LLM budget section for /status."""
    lines = ["", "🤖 <b>LLM Budget (Gemini Flash)</b>"]
    ctx = shared.app_ctx
    narrator = ctx.narrator
    if narrator is None:
        lines.append("  Narrator: ⚪ Not initialized")
        return lines

    client = getattr(narrator, "_client", None)
    if client is None:
        lines.append("  Client: ⚪ Not available")
        return lines

    used = getattr(client, "_daily_tokens", 0)
    limit = getattr(client, "DAILY_TOKEN_LIMIT", 900_000)
    pct = (used / limit * 100) if limit else 0

    if pct < 60:
        icon = "🟢"
    elif pct < _LLM_WARN_PCT * 100:
        icon = "🟡"
    else:
        icon = "🔴"

    lines.append(
        f"  Used today: {icon} <code>{used:,}</code> / <code>{limit:,}</code> tokens "
        f"(<code>{pct:.1f}%</code>)"
    )
    if pct >= _LLM_WARN_PCT * 100:
        lines.append("  ⚠️ <b>Budget running low — narrator may be silenced soon</b>")
    return lines


# ─── Handlers ────────────────────────────────────────────────────────────────

@guard
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📋 <b>StockAnalysis Bot — Commands</b>\n\n"
        "<b>General</b>\n"
        "/help — Show this help message\n"
        "/status — System health dashboard\n"
        "/ltp <code>&lt;SYMBOL&gt;</code> — Last traded price + % change\n"
        "/gainers — Top 5 gainers by % change\n"
        "/losers — Top 5 losers by % change\n"
        "/straddle <code>&lt;NIFTY|BANKNIFTY&gt;</code> — ATM straddle + expected 1-SD range\n"
        "/walls <code>&lt;NIFTY|BANKNIFTY&gt;</code> — Institutional OI walls (S/R levels)\n"
        "/watchlist — Full subscription overview\n"
        "/holidays — Upcoming NSE market holidays\n"
        "/enctoken <code>&lt;token&gt;</code> — Update Zerodha enctoken\n\n"
        "<b>Debug (debug chat only)</b>\n"
        "/debug — Overview dashboard\n"
        "/debugstock <code>&lt;SYM&gt;</code> — Deep stock object dump\n"
        "/debugsignals <code>[SYM]</code> — SignalBus + correlator state\n"
        "/debugcycle — Cycle sync (data-gateway + monolith)\n"
        "/debugredis <code>&lt;SYM&gt;</code> — Raw Redis hash inspection\n"
        "/debugcounters — All runtime counters\n"
        "/debugmemory — AppContext + memory layout\n"
        "/debuganalyzers — Registered analyser list\n"
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
    )


@guard
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = shared.app_ctx
    now_str = datetime.now().strftime("%H:%M:%S")

    # ── Core ──────────────────────────────────────────────────────────────
    mode_str = ctx.mode.name if ctx.mode else "NOT SET"

    ws_connected = False
    if ctx.zd_ticker_manager is not None:
        ws_connected = ctx.zd_ticker_manager.connected
    ws_icon = "🟢" if ws_connected else "🔴"

    stocks = len(ctx.stock_token_obj_dict)
    indices = len(ctx.index_token_obj_dict)

    try:
        from common.market_calendar import is_trading_day
        trading = is_trading_day()
        trading_str = "Yes ✅" if trading else "No (Holiday) 🚫"
    except Exception:
        trading_str = "Unknown"

    production = os.getenv("PRODUCTION", "0") == "1"
    prod_icon = "🟢 ON" if production else "⚪ OFF"

    lines = [
        "📊 <b>System Health Dashboard</b>",
        "",
        f"⏰ Time: <code>{now_str}</code>  |  📅 Trading Day: <b>{trading_str}</b>",
        f"📡 Mode: <b>{mode_str}</b>  |  🏭 Production: {prod_icon}",
        "",
        f"{ws_icon} <b>WebSocket</b>: {'Connected' if ws_connected else 'Disconnected'}",
        f"   📈 Equity: <b>{stocks}</b>  |  🏦 Indices: <b>{indices}</b>",
    ]

    # ── Feed Health ───────────────────────────────────────────────────────
    lines.extend(_feed_health_lines())

    # ── RAM ───────────────────────────────────────────────────────────────
    lines.extend(_ram_health_lines())

    # ── LLM Budget ────────────────────────────────────────────────────────
    lines.extend(_llm_budget_lines())

    text = "\n".join(lines)
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
    )


# ─── Background job: unsolicited LLM budget alert at 80% ─────────────────────

async def job_llm_budget_alert(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: send one alert per day when LLM budget crosses 80%."""
    ctx = shared.app_ctx
    if ctx.llm_budget_warned:
        return

    narrator = ctx.narrator
    if narrator is None:
        return

    client = getattr(narrator, "_client", None)
    if client is None:
        return

    used = getattr(client, "_daily_tokens", 0)
    limit = getattr(client, "DAILY_TOKEN_LIMIT", 900_000)
    if limit == 0:
        return

    pct = used / limit * 100
    if pct < _LLM_WARN_PCT * 100:
        return

    ctx.llm_budget_warned = True
    logger.warning(f"[BudgetAlert] LLM daily budget at {pct:.1f}% ({used:,}/{limit:,} tokens)")

    # Reset flag at midnight (next calendar day the narrator resets its counter)
    from intelligence.llm_client import GeminiClient
    if isinstance(client, GeminiClient):
        # Register callback so the counter reset also resets our warned flag
        original_cb = client._budget_alert_callback
        def _reset_on_new_day(used_tokens, limit_tokens):
            ctx.llm_budget_warned = False
            if callable(original_cb):
                original_cb(used_tokens, limit_tokens)
        client._budget_alert_callback = _reset_on_new_day

    try:
        from notification.Notification import TELEGRAM_NOTIFICATIONS
        TELEGRAM_NOTIFICATIONS.send_notification(
            f"⚠️ LLM Budget Alert\n"
            f"Gemini Flash daily budget: {pct:.1f}% used ({used:,} / {limit:,} tokens).\n"
            f"Narrator may stop responding before market close."
        )
    except Exception as exc:
        logger.error(f"[BudgetAlert] Failed to send Telegram notification: {exc}")


HANDLERS = [
    ("help", cmd_help),
    ("status", cmd_status),
]
