"""Paper trading commands: /paper_positions, /paper_pnl, /paper_trades, /paper_close, /paper_config, /paper_reset."""
from __future__ import annotations

import json
import time
from datetime import date

from telegram import Update
from telegram.ext import ContextTypes

from lib.logging_util import get_logger
logger = get_logger("notification")
from ._guard import guard
from ._helpers import _get_redis

COMMANDS_STREAM = "paper:commands"
ACCOUNT_KEY = "paper:account"
POSITIONS_OPEN_KEY = "paper:positions:open"


# ─── /paper_positions ──────────────────────────────────────────────────────

@guard
async def cmd_paper_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all open paper trading positions."""
    rc = _get_redis()
    if rc is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Redis unavailable — cannot read positions.",
        )
        return

    raw = rc.hgetall(POSITIONS_OPEN_KEY) or {}

    if not raw:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📊 No open paper positions.",
        )
        return

    lines = ["📊 <b>Open Paper Positions</b>", ""]
    for pid, raw_json in raw.items():
        try:
            pos = json.loads(raw_json) if isinstance(raw_json, (str, bytes)) else raw_json
        except (json.JSONDecodeError, TypeError):
            lines.append(f"  ⚠️ <code>{str(pid)[:12]}</code>: unparseable")
            continue

        symbol = pos.get("symbol", "?")
        strategy = pos.get("strategy", "?")
        direction = pos.get("direction", "?")
        mode = pos.get("mode", "?")
        entry_credit = pos.get("entry_credit", 0)
        margin = pos.get("margin_blocked", 0)
        expiry = pos.get("expiry", "")[:10] if pos.get("expiry") else "?"

        legs_str = _fmt_legs(pos.get("legs", []))
        lines.append(
            f"🔹 <b>{symbol}</b>  {strategy}  {direction}\n"
            f"   Expiry: {expiry}  Mode: {mode}\n"
            f"   Credit: ₹{entry_credit:,.0f}  Margin: ₹{margin:,.0f}\n"
            f"   Legs: {legs_str}\n"
            f"   <code>{str(pid)[:20]}</code>"
        )

    text = "\n".join(lines)
    if len(text) > 4096:
        text = text[:4000] + "\n\n⚠️ <i>Output truncated</i>"

    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, parse_mode="HTML",
    )


def _fmt_legs(legs: list[dict]) -> str:
    parts = []
    for leg in legs:
        side = leg.get("side", "?")
        ot = leg.get("option_type", "?")
        strike = leg.get("strike", 0)
        lots = leg.get("lots", 0)
        premium = leg.get("entry_premium", 0)
        parts.append(f"{side} {strike:.0f}{ot} @₹{premium:.1f}×{lots}")
    return " | ".join(parts) if parts else "—"


# ─── /paper_pnl ────────────────────────────────────────────────────────────

@guard
async def cmd_paper_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show paper trading account summary."""
    rc = _get_redis()
    if rc is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Redis unavailable.",
        )
        return

    acct = rc.hgetall(ACCOUNT_KEY)
    if not acct:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📊 Paper account not initialised — wait for service startup.",
        )
        return

    capital = float(acct.get("capital", 0))
    realized = float(acct.get("realized_pnl", 0))
    unrealized = float(acct.get("unrealized_pnl", 0))
    total_pnl = realized + unrealized
    margin_used = float(acct.get("margin_used", 0))
    available = float(acct.get("available_margin", capital))
    open_pos = int(acct.get("open_positions", 0))
    daily_rpnl = float(acct.get("daily_realized_pnl", 0))
    daily_trades = int(acct.get("daily_trades", 0))
    daily_wins = int(acct.get("daily_wins", 0))
    daily_losses = int(acct.get("daily_losses", 0))
    max_dd = float(acct.get("max_drawdown", 0))

    total_pct = (total_pnl / capital * 100) if capital > 0 else 0.0
    pnl_icon = "🟢" if total_pnl >= 0 else "🔴"
    margin_pct = (margin_used / capital * 100) if capital > 0 else 0.0
    win_rate = (daily_wins / daily_trades * 100) if daily_trades > 0 else 0.0

    lines = [
        "📊 <b>Paper Trading Account</b>",
        "",
        f"  Starting:  <code>₹{capital:,.0f}</code>",
        f"  Current:   <b><code>₹{capital + total_pnl:,.0f}</code></b>",
        f"  Total P&L: {pnl_icon} <b><code>₹{total_pnl:+,.0f}</code></b>  ({total_pct:+.1f}%)",
        "",
        f"  Realized:  <code>₹{realized:+,.0f}</code>",
        f"  Unrealized: <code>₹{unrealized:+,.0f}</code>",
        "",
        f"  Margin used:  <code>₹{margin_used:,.0f}</code>  ({margin_pct:.1f}%)",
        f"  Available:   <code>₹{available:,.0f}</code>",
        f"  Open positions:  <b>{open_pos}</b>",
        "",
        "── <i>Today</i> ──",
        f"  Realized:  <code>₹{daily_rpnl:+,.0f}</code>",
        f"  Trades:  <b>{daily_trades}</b>  |  Wins: 🟢{daily_wins}  |  Losses: 🔴{daily_losses}  |  WR: {win_rate:.0f}%",
    ]

    if max_dd < 0:
        dd_pct = (max_dd / capital * 100) if capital > 0 else 0.0
        lines.append(f"\n  Max drawdown:  <code>₹{max_dd:,.0f}</code>  ({dd_pct:.1f}%)")

    text = "\n".join(lines)
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, parse_mode="HTML",
    )


# ─── /paper_trades ─────────────────────────────────────────────────────────

@guard
async def cmd_paper_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent closed trades."""
    rc = _get_redis()
    if rc is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Redis unavailable.",
        )
        return

    args = context.args or []
    limit = min(int(args[0]) if args else 10, 50)

    try:
        entries = rc.xrevrange("paper:trades", "+", "-", limit)
    except Exception as e:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ Cannot read trade stream: {e}",
        )
        return

    if not entries:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📊 No closed trades yet.",
        )
        return

    lines = [f"📊 <b>Recent Trades</b> (last {limit})", ""]
    for msg_id, fields in entries:
        symbol = _decode_field(fields, "symbol", "?")
        strategy = _decode_field(fields, "strategy", "?")
        pnl_str = _decode_field(fields, "pnl", "0")
        exit_reason = _decode_field(fields, "exit_reason", "?")
        ts = _decode_field(fields, "timestamp", "0")

        try:
            pnl = float(pnl_str)
        except (ValueError, TypeError):
            pnl = 0.0
        icon = "✅" if pnl >= 0 else "❌"
        ts_str = _format_ts(ts)

        lines.append(
            f"{icon} <b>{symbol}</b>  {strategy}  |  P&L: ₹{pnl:+,.0f}  |  {exit_reason}\n"
            f"   <i>{ts_str}</i>"
        )

    text = "\n".join(lines)
    if len(text) > 4096:
        text = text[:4000] + "\n\n⚠️ <i>Output truncated</i>"

    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=text, parse_mode="HTML",
    )


def _decode_field(fields: dict, key: str, default: str = "") -> str:
    val = fields.get(key.encode() if isinstance(next(iter(fields), b""), bytes) else key, b"")
    if isinstance(val, bytes):
        val = val.decode()
    return val if val else default


def _format_ts(ts: str) -> str:
    try:
        return time.strftime("%d %b %H:%M", time.localtime(float(ts)))
    except (ValueError, TypeError):
        return ts[:19] if ts else "?"


# ─── /paper_close ──────────────────────────────────────────────────────────

@guard
async def cmd_paper_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Close paper positions: /paper_close [position_id|all]."""
    rc = _get_redis()
    if rc is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Redis unavailable.",
        )
        return

    args = context.args or []
    target = args[0] if args else "all"

    open_pos = rc.hgetall(POSITIONS_OPEN_KEY) or {}
    if not open_pos:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="📊 No open positions to close.",
        )
        return

    if target == "all":
        await _send_paper_command(rc, {"command": "close", "position_id": "all"})
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"📤 Closing <b>all {len(open_pos)}</b> positions — check /paper_pnl for results.",
            parse_mode="HTML",
        )
        return

    matched = {k: v for k, v in open_pos.items() if k.startswith(target[:20])}
    if not matched:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ No position matching <code>{target[:20]}</code>. Try /paper_positions for IDs.",
            parse_mode="HTML",
        )
        return

    pid = list(matched.keys())[0]
    await _send_paper_command(rc, {"command": "close", "position_id": pid})
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"📤 Closing position <code>{pid[:16]}</code>...",
        parse_mode="HTML",
    )


# ─── /paper_config ─────────────────────────────────────────────────────────

@guard
async def cmd_paper_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View/set paper trading config: /paper_config [key] [value]."""
    rc = _get_redis()
    if rc is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Redis unavailable.",
        )
        return

    args = context.args or []

    if not args:
        cfg = rc.hgetall("paper:config") or {}
        if not cfg:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="📊 No paper config set (using build-time defaults).",
            )
            return
        lines = ["⚙ <b>Paper Trading Config</b>", ""]
        for k, v in sorted(cfg.items()):
            k_str = k.decode() if isinstance(k, bytes) else k
            v_str = v.decode() if isinstance(v, bytes) else v
            lines.append(f"  {k_str}: <code>{v_str}</code>")
        text = "\n".join(lines)
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, parse_mode="HTML",
        )
        return

    if len(args) == 1:
        key = args[0]
        val = rc.hget("paper:config", key)
        if val is None:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ Config key <code>{key}</code> not set.",
                parse_mode="HTML",
            )
            return
        v_str = val.decode() if isinstance(val, bytes) else val
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚙ {key}: <code>{v_str}</code>",
            parse_mode="HTML",
        )
        return

    key, value = args[0], args[1]
    await _send_paper_command(rc, {"command": "config_set", "key": key, "value": value})
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"⚙ Set <code>{key}</code> = <code>{value}</code>",
        parse_mode="HTML",
    )


# ─── /paper_reset ──────────────────────────────────────────────────────────

@guard
async def cmd_paper_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset paper trading account: /paper_reset [confirm]."""
    rc = _get_redis()
    if rc is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Redis unavailable.",
        )
        return

    args = context.args or []
    if not args or args[0].lower() != "confirm":
        acct = rc.hgetall(ACCOUNT_KEY) or {}
        capital = float(acct.get("capital", "1,000,000"))
        total_pnl = float(acct.get("realized_pnl", 0)) + float(acct.get("unrealized_pnl", 0))
        open_n = len(rc.hgetall(POSITIONS_OPEN_KEY) or {})
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"⚠️ <b>Reset paper account?</b>\n\n"
                f"  Capital: ₹{capital:,.0f}\n"
                f"  Total P&L: ₹{total_pnl:+,.0f}\n"
                f"  Open positions: {open_n}\n\n"
                f"Type <code>/paper_reset confirm</code> to proceed."
            ),
            parse_mode="HTML",
        )
        return

    await _send_paper_command(rc, {"command": "reset"})
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔄 Paper account reset — all positions closed, P&L zeroed, capital restored to ₹10,00,000.",
    )


# ─── Internal helpers ──────────────────────────────────────────────────────

def _send_paper_command(rc, fields: dict) -> None:
    """Send a command to the paper-trading service via Redis stream."""
    rc.xadd(COMMANDS_STREAM, fields, maxlen=100)


# ─── HANDLERS export ───────────────────────────────────────────────────────

HANDLERS = [
    ("paper_positions", cmd_paper_positions),
    ("paper_pnl",       cmd_paper_pnl),
    ("paper_trades",    cmd_paper_trades),
    ("paper_close",     cmd_paper_close),
    ("paper_config",    cmd_paper_config),
    ("paper_reset",     cmd_paper_reset),
]
