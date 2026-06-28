"""Debug commands — restricted to the debug chat (TELEGRAM_DEBUG_CHAT_ID).

Each handler calls a pure inspect_* function and formats the result as HTML.
Command names use no spaces (Telegram limitation): /debugstock RELIANCE, etc.
"""
from __future__ import annotations

from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from notification.commands._guard import guard, debug_chat_only
from notification.commands.debug_inspect import (
    inspect_overview,
    inspect_stock,
    inspect_signals,
    inspect_cycle,
    inspect_redis,
    inspect_counters,
    inspect_memory,
    inspect_analyzers,
)


async def _send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Send text, splitting into chunks if it exceeds Telegram's 4096-char limit."""
    if len(text) <= 4096:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
        )
        return
    chunks = []
    current = ""
    for line in text.split("\n"):
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


# ─── /debug — overview dashboard ─────────────────────────────────────────────

@guard
async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not debug_chat_only(update):
        return
    d = inspect_overview()
    ws_icon = "🟢" if d["ws_connected"] else "🔴"
    lines = [
        "🔧 <b>Debug Overview</b>",
        f"⏰ {datetime.now().strftime('%H:%M:%S')}",
        "",
        f"<b>Mode:</b> {d['mode']}  |  <b>Prod:</b> {d['production']}",
        f"<b>Cycle:</b> {d['intraday_cycle_count']}  (last: {d['last_cycle_time']})",
        f"<b>Errors:</b> {d['error_count']}",
        "",
        f"<b>Instruments:</b> 📈{d['stocks']}  🏦{d['indices']}  🛢{d['commodities']}  🌍{d['global_indices']}",
        "",
        f"{ws_icon} <b>WebSocket</b>",
        f"  Ticks: {d['ws_tick_count']}  |  Reconnects: {d['ws_reconnects']}",
        f"  Queue: {d['tick_queue_depth']}  |  Unknown tokens: {d['unknown_tokens']}",
        "",
        "🧠 <b>Intelligence</b>",
        f"  Signals: {d['signals_emitted']}  |  Confluences: {d['confluences']}",
        "",
        "🤖 <b>LLM</b>",
        f"  Tokens: {d['llm_tokens_used']:,} / {d['llm_token_limit']:,} ({d['llm_budget_pct']}%)",
        "",
        f"💾 <b>Memory:</b> {d['memory_rss_mb']} MB RSS",
        f"📡 <b>Feed:</b> {d['options_source']}  |  Last equity tick: {d['last_equity_tick']}",
        "",
        f"<b>Monitor Results:</b> {d['monitor_results']}",
    ]
    await _send(update, context, "\n".join(lines))


# ─── /debugstock <SYM> — deep stock dump ────────────────────────────────────

@guard
async def cmd_debug_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not debug_chat_only(update):
        return
    symbol = context.args[0].upper().strip() if context.args else ""
    if not symbol:
        await _send(update, context, "Usage: /debugstock <code>&lt;SYMBOL&gt;</code>")
        return

    d = inspect_stock(symbol)
    if "error" in d:
        await _send(update, context, f"❌ {d['error']}")
        return

    pd = d.get("priceData", {})
    an = d.get("analysis", {})
    sb = d.get("sensibull", {})
    oa = d.get("options_aggregate", {})
    zd = d.get("zerodha_data", {})

    lines = [
        f"🔧 <b>{d['symbol']}</b>  ({d['name']})",
        f"  is_index: {d['is_index']}",
        "",
        "<b>Price</b>",
        f"  LTP: <code>{d['ltp']}</code>  ({d['ltp_change_perc']})",
        f"  Daily HV: <code>{d['daily_hv']}</code>",
        f"  PrevDayOHLCV: <code>{d['prevDayOHLCV']}</code>",
        f"  priceData: <code>{pd.get('rows', 0)} rows</code>, "
        f"<code>{pd.get('columns', [])}</code>",
        f"  Date range: {pd.get('first_date', '?')} → {pd.get('last_date', '?')}",
        f"  Last close: <code>{pd.get('last_close', '?')}</code>",
        "",
        "<b>Analysis</b>",
        f"  Timestamp: {an.get('timestamp')}",
        f"  BULLISH ({len(an.get('bullish_trends', []))}): {an.get('bullish_trends', [])}",
        f"  BEARISH ({len(an.get('bearish_trends', []))}): {an.get('bearish_trends', [])}",
        f"  NEUTRAL ({len(an.get('neutral_trends', []))}): {an.get('neutral_trends', [])}",
        f"  NoOfTrends: {an.get('no_of_trends', 0)}",
        f"  PriorityOverride: {an.get('priority_override')}",
        "",
        "<b>Sensibull</b>",
        f"  Last fetch: {sb.get('last_fetch_time', '?')}",
        f"  Has current: {sb.get('has_current', False)}",
        f"  OI chain history: {sb.get('oi_chain_history_count', 0)}",
        f"  Historical: {sb.get('has_historical_data', False)}  "
        f"IV chart: {sb.get('has_iv_chart_history', False)}  "
        f"OI history: {sb.get('has_oi_history', False)}",
        "",
    ]
    if oa:
        lines += [
            "<b>Options Aggregate</b>",
            f"  ATM: <code>{oa.get('atm_strike')}</code>  PCR: <code>{oa.get('live_pcr')}</code>",
            f"  CE OI: <code>{oa.get('total_ce_oi')}</code>  PE OI: <code>{oa.get('total_pe_oi')}</code>",
            f"  CE Wall: <code>{oa.get('ce_wall')}</code>  PE Wall: <code>{oa.get('pe_wall')}</code>",
            f"  Last updated: {oa.get('last_updated', '?')}",
            "",
        ]
    if zd:
        lines += [
            "<b>Zerodha (live)</b>",
            f"  Last price: {zd.get('last_price')}  Volume: {zd.get('volume_traded')}  Change: {zd.get('change')}",
        ]
    await _send(update, context, "\n".join(lines))


# ─── /debugsignals [SYM] ─────────────────────────────────────────────────────

@guard
async def cmd_debug_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not debug_chat_only(update):
        return
    symbol = context.args[0].upper().strip() if context.args else None
    d = inspect_signals(symbol)

    lines = [
        "🔧 <b>Signals & Confluences</b>",
        f"  Total emitted: <b>{d['signals_emitted']}</b>",
        f"  Total confluences: <b>{d['confluences']}</b>",
        "",
    ]

    if "active_signals" in d:
        lines.append(f"<b>{d['symbol']}</b> — {d['active_signal_count']} active signal(s):")
        for s in d["active_signals"]:
            lines.append(
                f"  {s['direction']} {s['source']} [{s['layer']}] "
                f"{s['strength']} ({s['age_seconds']}s old)"
            )
    elif "symbols_with_signals" in d:
        sws = d["symbols_with_signals"]
        if sws:
            lines.append(f"<b>Symbols with active signals ({len(sws)}):</b>")
            for sym, count in sorted(sws.items(), key=lambda x: -x[1]):
                lines.append(f"  {sym}: {count}")
        else:
            lines.append("No active signals in buffer.")

    await _send(update, context, "\n".join(lines))


# ─── /debugcycle ─────────────────────────────────────────────────────────────

@guard
async def cmd_debug_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not debug_chat_only(update):
        return
    d = inspect_cycle()

    lines = [
        "🔧 <b>Cycle Sync</b>",
        f"  Monolith cycle: <b>{d['monolith_cycle_count']}</b>  (last: {d['last_cycle_time']})",
        "",
    ]

    if "data_gateway" in d:
        gw = d["data_gateway"]
        lines += [
            "<b>Data-Gateway</b>",
            f"  Status: <code>{gw['status']}</code>",
            f"  Cycle count: <code>{gw['cycle_count']}</code>",
            f"  Price symbols: <code>{gw['price_symbols']}</code>",
            f"  Last heartbeat: {gw['last_heartbeat']}",
            "",
        ]

    if "recent_cycles" in d:
        lines.append("<b>Recent cycle stream entries:</b>")
        for entry in d["recent_cycles"]:
            lines.append(f"  {entry['id']}: {entry['fields']}")

    if "redis_error" in d:
        lines.append(f"⚠️ Redis error: {d['redis_error']}")

    await _send(update, context, "\n".join(lines))


# ─── /debugredis <SYM> ───────────────────────────────────────────────────────

@guard
async def cmd_debug_redis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not debug_chat_only(update):
        return
    symbol = context.args[0].upper().strip() if context.args else ""
    if not symbol:
        await _send(update, context, "Usage: /debugredis <code>&lt;SYMBOL&gt;</code>")
        return

    d = inspect_redis(symbol)
    lines = [f"🔧 <b>Redis: {d['symbol']}</b>", ""]

    for prefix in ("data:price", "data:sensibull", "data:zerodha"):
        info = d.get(prefix, {})
        exists = "✅" if info.get("exists") else "❌"
        lines.append(f"{exists} <b>{prefix}:{d['symbol']}</b> — {info.get('field_count', 0)} fields")
        if info.get("fields"):
            lines.append(f"  Fields: <code>{info['fields']}</code>")

    if "cycle_stream_length" in d:
        lines.append(f"\n📊 <b>cycle_stream</b> length: {d['cycle_stream_length']}")

    if "error" in d:
        lines.append(f"⚠️ Error: {d['error']}")

    await _send(update, context, "\n".join(lines))


# ─── /debugcounters ──────────────────────────────────────────────────────────

@guard
async def cmd_debug_counters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not debug_chat_only(update):
        return
    d = inspect_counters()
    lines = [
        "🔧 <b>Counters</b>",
        f"⏰ {datetime.now().strftime('%H:%M:%S')}",
        "",
        f"<b>Intraday cycle:</b> {d['intraday_cycle_count']}  (last: {d['last_cycle_time']})",
        f"<b>Signals emitted:</b> {d['signals_emitted']}",
        f"<b>Confluences:</b> {d['confluences']}",
        f"<b>Monitor results:</b> {d['monitor_results']}",
        f"<b>Errors:</b> {d['error_count']}",
        "",
        "<b>WebSocket</b>",
        f"  Ticks: {d['ws_tick_count']}",
        f"  Reconnects: {d['ws_reconnects']}",
        f"  Queue depth: {d['tick_queue_depth']}",
        f"  Unknown tokens: {d['unknown_tokens']}",
        "",
        f"<b>LLM tokens:</b> {d['llm_tokens_used']:,}",
        f"<b>Memory RSS:</b> {d['memory_rss_mb']} MB",
    ]
    await _send(update, context, "\n".join(lines))


# ─── /debugmemory ────────────────────────────────────────────────────────────

@guard
async def cmd_debug_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not debug_chat_only(update):
        return
    d = inspect_memory()
    lines = [
        "🔧 <b>Memory & AppContext</b>",
        f"⏰ {datetime.now().strftime('%H:%M:%S')}",
        "",
        f"<b>Mode:</b> {d['mode']}",
        f"<b>Options source:</b> {d['options_source']}",
        f"<b>Sensibull feed active:</b> {d['sensibull_feed_active']}",
        "",
        "<b>Token-Obj Dicts</b>",
        f"  Stocks: {d['stock_token_obj_dict']}",
        f"  Indices: {d['index_token_obj_dict']}",
        f"  Commodities: {d['commodity_token_obj_dict']}",
        f"  Global Indices: {d['global_indices_token_obj_dict']}",
        "",
        "<b>Symbol Lists</b>",
        f"  stocks_list: {d['stocks_list']}",
        f"  index_list: {d['index_list']}",
        f"  commodity_list: {d['commodity_list']}",
        f"  global_indices_list: {d['global_indices_list']}",
        "",
        f"<b>Expiries:</b> <code>{d['stockExpires']}</code>",
        f"<b>Last equity tick:</b> {d['last_equity_tick_time']}",
        f"<b>LLM budget warned:</b> {d['llm_budget_warned']}",
        f"<b>52W high list:</b> {d['ticker_52w_high_count']}",
        f"<b>52W low list:</b> {d['ticker_52w_low_count']}",
        "",
        f"<b>Memory RSS:</b> {d['memory_rss_mb']} MB",
    ]
    await _send(update, context, "\n".join(lines))


# ─── /debuganalyzers ─────────────────────────────────────────────────────────

@guard
async def cmd_debug_analyzers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not debug_chat_only(update):
        return
    d = inspect_analyzers()
    if "error" in d:
        await _send(update, context, f"❌ {d['error']}")
        return

    lines = [
        "🔧 <b>Analyzer Orchestrator</b>",
        f"  Registered analysers: <b>{d['analyser_count']}</b>",
        "",
    ]
    for a in d["analysers"]:
        active = "✅" if a.get("is_active", True) else "🔴"
        lines.append(f"  {active} {a['class']}")

    await _send(update, context, "\n".join(lines))


HANDLERS = [
    ("debug",           cmd_debug),
    ("debugstock",      cmd_debug_stock),
    ("debugsignals",    cmd_debug_signals),
    ("debugcycle",      cmd_debug_cycle),
    ("debugredis",      cmd_debug_redis),
    ("debugcounters",   cmd_debug_counters),
    ("debugmemory",     cmd_debug_memory),
    ("debuganalyzers",  cmd_debug_analyzers),
]
