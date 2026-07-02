"""Stats command — /debugstats for per-stock and system-wide counters.

Usage:
  /debugstats            — system health counters
  /debugstats RELIANCE   — per-stock counters for a symbol
  /debugstats all        — all stocks sorted by alerts_total desc
  /debugstats all ticks  — sorted by tick_count
  /debugstats all errors — sorted by analysis_errors
  /debugstats all stale  — sorted by last_analysis_time
  /debugstats all nodata — filter to last_analysis_result=NO_DATA

Restricted to the debug chat.
"""
from __future__ import annotations

import time

from telegram import Update
from telegram.ext import ContextTypes

from notification.commands._guard import guard, debug_chat_only
from common.logging_util import logger
from services.common.metrics import (
    get_system_stats,
    get_stock_stats,
    get_all_stock_stats,
    get_top_stocks,
)


def _fmt_age(ts_str: str) -> str:
    if not ts_str:
        return "never"
    try:
        ts = float(ts_str)
    except (ValueError, TypeError):
        return "never"
    if ts <= 0:
        return "never"
    secs = time.time() - ts
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.1f}m"
    return f"{secs / 3600:.1f}h"


def _bool_icon(val: str, threshold_good: float = 30.0) -> str:
    try:
        v = float(val)
        if v <= threshold_good:
            return "🟢"
        if v <= threshold_good * 3:
            return "🟡"
        return "🔴"
    except (ValueError, TypeError):
        return "⚪"


def _build_system_text(stats: dict) -> str:
    if not stats:
        return "⚠️ No system stats found. Has the system started?"

    lines = ["📊 <b>System Metrics</b>", ""]

    # Tick Pipeline
    lines.append("── Tick Pipeline ──")
    ticks = stats.get("total_ticks", "0")
    rate = stats.get("tick_rate", "0")
    rate_icon = "🟢" if float(rate or 0) > 500 else ("🟡" if float(rate or 0) > 100 else "🔴")
    snap_age = _fmt_age(stats.get("snapshot_age_s", "0"))
    ws2_rec = stats.get("ws2_reconnects", "0")
    ws2_icon = "🟢" if int(ws2_rec or 0) == 0 else "🔴"
    lines.append(f"  Total ticks:     <code>{int(float(ticks)):,}</code>")
    lines.append(f"  Tick rate:       {rate_icon} <code>{rate}</code>/s")
    lines.append(f"  Snapshots:       <code>{snap_age}</code> old")
    lines.append(f"  WS2 reconnects:  {ws2_icon} <code>{ws2_rec}</code>")

    # Analysis Pipeline
    lines.append("")
    lines.append("── Analysis Pipeline ──")
    cycle = stats.get("intraday_cycle_count", "0")
    cycle_age = _fmt_age(stats.get("last_cycle_time", "0"))
    dispatched = int(float(stats.get("total_jobs_dispatched", "0")))
    completed = int(float(stats.get("total_jobs_completed", "0")))
    pending = max(0, dispatched - completed)
    pending_icon = "🟢" if pending == 0 else "🔴"
    avg_ms = stats.get("avg_analysis_ms", "—")
    success_c = stats.get("result_success_count", "0")
    no_data_c = stats.get("result_no_data_count", "0")
    error_c = stats.get("result_error_count", "0")
    lines.append(f"  Cycle:            <code>{cycle}</code> (last: {cycle_age} ago)")
    lines.append(f"  Jobs dispatched:  <code>{dispatched:,}</code>")
    lines.append(f"  Jobs completed:   <code>{completed:,}</code>")
    lines.append(f"  Pending:          {pending_icon} <code>{pending}</code>")
    lines.append(f"  Avg duration:     <code>{avg_ms}</code>ms")
    lines.append(f"  Results:  ✅{int(float(success_c)):,}  ⚪{int(float(no_data_c)):,}  ❌{int(float(error_c)):,}")

    # Alerts
    lines.append("")
    lines.append("── Alerts & Signals ──")
    alerts_sent = int(float(stats.get("alerts_attempted", "0")))
    alerts_delivered = int(float(stats.get("alerts_delivered", "0")))
    alerts_failed = int(float(stats.get("alerts_failed", "0")))
    fail_icon = "🟢" if alerts_failed == 0 else "🔴"
    confluences = stats.get("total_confluences", "0")
    trends = stats.get("trends_found", "0")
    stale = stats.get("stale_stocks_count", "0")
    stale_icon = "🟢" if int(stale or 0) == 0 else "🔴"
    lines.append(f"  Alerts attempted: <code>{alerts_sent:,}</code>")
    lines.append(f"  Alerts delivered: <code>{alerts_delivered:,}</code>")
    lines.append(f"  Alerts failed:    {fail_icon} <code>{alerts_failed}</code>")
    lines.append(f"  Trends found:     <code>{int(float(trends)):,}</code>")
    lines.append(f"  Confluences:      <code>{int(float(confluences)):,}</code>")
    lines.append(f"  Stale stocks:     {stale_icon} <code>{stale}</code>")

    # Auth
    lines.append("")
    lines.append("── Auth ──")
    auth = stats.get("auth_refresh_count", "0")
    auth_icon = "🟢" if int(auth or 0) <= 2 else "🟡"
    lines.append(f"  Enctoken refreshes: {auth_icon} <code>{auth}</code>")

    return "\n".join(lines)


def _build_stock_text(symbol: str, stats: dict) -> str:
    if not stats:
        return f"⚠️ No stats found for <b>{symbol}</b>. Try /debugstats to verify the system is running."

    lines = [f"📊 <b>Stats: {symbol}</b>", ""]

    # Data Pipeline
    lines.append("── Data ──")
    tc = stats.get("tick_count", "0")
    otc = stats.get("option_tick_count", "0")
    age_s = stats.get("last_tick_age_s", "")
    age_str = _fmt_age(age_s) if age_s else "—"
    age_icon = _bool_icon(age_s, 30.0) if age_s else "⚪"
    pcr = stats.get("last_pcr", "—")
    lines.append(f"  Tick count:      <code>{int(tc):,}</code>")
    if otc != "0":
        lines.append(f"  Option ticks:    <code>{int(otc):,}</code>")
    lines.append(f"  Last tick:       {age_icon} <code>{age_str}</code>")
    if pcr != "—" and pcr:
        lines.append(f"  PCR:             <code>{float(pcr):.2f}</code>")

    # Analysis Pipeline
    lines.append("")
    lines.append("── Analysis ──")
    ac = int(float(stats.get("analysis_count", "0")))
    last_result = stats.get("last_analysis_result", "—")
    last_dur = stats.get("last_analysis_duration_ms", "—")
    last_time = _fmt_age(stats.get("last_analysis_time", ""))
    tf = int(float(stats.get("trends_found", "0")))
    ae = int(float(stats.get("analysis_errors", "0")))
    result_icon = {"SUCCESS": "🟢", "NO_DATA": "⚪", "ERROR": "🔴", "SKIPPED": "🟡"}.get(last_result, "⚪")
    lines.append(f"  Analysis count:  <code>{ac:,}</code>")
    lines.append(f"  Last result:     {result_icon} <code>{last_result}</code>")
    lines.append(f"  Last duration:   <code>{last_dur}</code>ms")
    lines.append(f"  Last analysed:   <code>{last_time}</code> ago")
    lines.append(f"  Trends found:    <code>{tf:,}</code>")
    if ae > 0:
        lines.append(f"  ❌ Errors:        <code>{ae:,}</code>")

    # Alerts
    lines.append("")
    lines.append("── Alerts ──")
    a_att = int(float(stats.get("alerts_attempted", "0")))
    a_del = int(float(stats.get("alerts_delivered", "0")))
    a_fail = int(float(stats.get("alerts_failed", "0")))
    a_trend = int(float(stats.get("alerts_trend", "0")))
    a_conf = int(float(stats.get("alerts_confluence", "0")))
    a_lo = int(float(stats.get("alerts_live_options", "0")))
    a_narr = int(float(stats.get("alerts_narrative", "0")))
    a_stale = int(float(stats.get("alerts_stale_data", "0")))
    lines.append(f"  Trend alerts:      <code>{a_trend:,}</code>")
    lines.append(f"  Confluence alerts: <code>{a_conf:,}</code>")
    if a_lo > 0:
        lines.append(f"  Live options:      <code>{a_lo:,}</code>")
    if a_narr > 0:
        lines.append(f"  Narratives:        <code>{a_narr:,}</code>")
    if a_stale > 0:
        lines.append(f"  Stale data:        <code>{a_stale:,}</code>")
    lines.append(f"  ──────────────────")
    lines.append(f"  Attempted:         <code>{a_att:,}</code>")
    lines.append(f"  Delivered:         <code>{a_del:,}</code>")
    lines.append(f"  Failed:            <code>{a_fail:,}</code>")

    # Derivatives
    gex = stats.get("gex_regime", "")
    if gex:
        lines.append("")
        lines.append("── Derivatives ──")
        gex_icon = "🟢" if gex == "POSITIVE" else ("🔴" if gex == "NEGATIVE" else "⚪")
        lines.append(f"  GEX regime:      {gex_icon} <code>{gex}</code>")

    return "\n".join(lines)


def _build_all_text(sort_by: str) -> str:
    all_stats = get_all_stock_stats()
    if not all_stats:
        return "⚠️ No stock stats found."

    rows = []
    for symbol, s in all_stats.items():
        tc = int(float(s.get("tick_count", "0")))
        ac = int(float(s.get("analysis_count", "0")))
        tf = int(float(s.get("trends_found", "0")))
        ad = int(float(s.get("alerts_delivered", "0")))
        lr = s.get("last_analysis_result", "—")
        ae = int(float(s.get("analysis_errors", "0")))

        if sort_by == "nodata" and lr != "NO_DATA":
            continue

        if sort_by == "ticks":
            key = tc
        elif sort_by == "errors":
            key = ae
        elif sort_by == "stale":
            k = s.get("last_analysis_time", "0")
            key = float(k) if k else 0
        else:
            key = ad  # default: alerts delivered

        rows.append((key, symbol, tc, ac, tf, ad, lr, ae))

    rows.sort(key=lambda r: r[0], reverse=True)

    # Emoji sort indicators
    sort_label = {
        "ticks": "tick_count",
        "errors": "analysis_errors",
        "stale": "last_analysis_time",
        "nodata": "last_analysis_result=NO_DATA",
    }.get(sort_by, "alerts_delivered")

    lines = [f"📊 <b>All Stocks</b> (sorted by {sort_label})", ""]
    lines.append(f"<code>{'Symbol':<14} {'Ticks':>8} {'Ana':>5} {'Trend':>6} {'Alerts':>7} {'Result':<9} {'Err':>4}</code>")

    for key, symbol, tc, ac, tf, ad, lr, ae in rows[:30]:
        r_icon = {"SUCCESS": "✅", "NO_DATA": "⚪", "ERROR": "❌", "SKIPPED": "🟡"}.get(lr, "⚪")
        warning = " ⚠️" if tc == 0 and ac > 0 else (" ⚠️⚠️" if ac == 0 else "")
        lines.append(
            f"<code>{symbol:<14} {tc:>8,} {ac:>5} {tf:>6} {ad:>7} {r_icon} {lr:<7}</code>{warning}"
        )

    return "\n".join(lines)


@guard
async def cmd_debug_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not debug_chat_only(update):
        logger.debug(f"[stats] Ignored from non-debug chat {update.effective_chat.id}")
        return

    args = context.args or []
    if not args:
        text = _build_system_text(get_system_stats())
    elif args[0].upper() == "ALL":
        sort_by = args[1].lower() if len(args) > 1 else "alerts"
        text = _build_all_text(sort_by)
    else:
        symbol = args[0].upper()
        text = _build_stock_text(symbol, get_stock_stats(symbol))

    if len(text) <= 4096:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
        )
    else:
        chunks = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > 4000:
                chunks.append(current)
                current = line
            else:
                current += "\n" + line if current else line
        if current:
            chunks.append(current)
        for chunk in chunks[:4]:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=chunk, parse_mode="HTML"
            )


HANDLERS = [
    ("debugstats", cmd_debug_stats),
]
