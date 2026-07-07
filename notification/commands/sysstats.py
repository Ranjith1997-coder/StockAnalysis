"""System resource stats command — /sysstats for system + service + Redis resource monitoring.

Usage:
  /sysstats          — live system + all services + Redis dashboard
  /sysstats history  — 24h sparklines + 7-day summary table
  /sysstats redis    — Redis health deep dive

Restricted to the debug chat (same as /debugstats).

Reads from Redis keys written by the resource-monitor service:
  sys:latest:system, sys:latest:{service}, sys:latest:redis
  sys:ts:* (ZSET time-series), sys:daily:{date} (daily rollup)
"""
from __future__ import annotations

import os
import time
import json
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from notification.commands._guard import guard, debug_chat_only
from common.logging_util import logger


# ── Redis access ─────────────────────────────────────────────────────────────

_REDIS_CLIENT = None


def _get_redis():
    """Get a sync Redis client for reading sys:* keys."""
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    try:
        import redis as _sync_redis
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        _REDIS_CLIENT = _sync_redis.from_url(redis_url, decode_responses=True)
        _REDIS_CLIENT.ping()
    except Exception as e:
        logger.debug(f"[sysstats] Redis unavailable: {e}")
        _REDIS_CLIENT = None
    return _REDIS_CLIENT


# ── Helpers ──────────────────────────────────────────────────────────────────

_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[float]) -> str:
    """Render a list of floats as a unicode sparkline string."""
    if not values:
        return ""
    vmin, vmax = min(values), max(values)
    if vmax == vmin:
        return _SPARK_CHARS[3] * len(values)
    scale = (len(_SPARK_CHARS) - 1) / (vmax - vmin)
    return "".join(
        _SPARK_CHARS[min(len(_SPARK_CHARS) - 1, int((v - vmin) * scale))]
        for v in values
    )


def _ts_to_zset_values(rc, key: str, count: int = 24) -> list[float]:
    """Read the last N values from a time-series ZSET (sorted by score=timestamp)."""
    try:
        raw = rc.zrange(key, -count, -1, withscores=False)
        return [float(v.split(":", 1)[-1]) for v in raw]
    except Exception:
        return []


def _ts_to_hourly_avg(rc, key: str, hours: int = 24) -> list[float]:
    """Read time-series and downsample to hourly averages."""
    try:
        now = time.time()
        raw = rc.zrangebyscore(key, now - hours * 3600, now, withscores=True)
        if not raw:
            return []
        # Group by hour
        hourly: dict[int, list[float]] = {}
        for val_str, ts in raw:
            val = float(val_str.split(":", 1)[-1])
            hour_bucket = int(ts) // 3600
            hourly.setdefault(hour_bucket, []).append(val)
        # Build ordered list of hourly averages
        buckets = sorted(hourly.keys())
        return [sum(hourly[b]) / len(hourly[b]) for b in buckets[-hours:]]
    except Exception:
        return []


def _trend_arrow(rc, key: str, now_val: float) -> str:
    """Compare current value vs value 5min ago (10 samples)."""
    try:
        now = time.time()
        raw = rc.zrangebyscore(key, now - 300, now - 290, withscores=False)
        if raw:
            old = float(raw[0].split(":", 1)[-1])
            if old > 0:
                pct_change = ((now_val - old) / old) * 100
                if pct_change > 5:
                    return "↑"
                elif pct_change < -5:
                    return "↓"
        return "→"
    except Exception:
        return "→"


def _color_icon(val: float, green: float = 60.0, yellow: float = 80.0) -> str:
    if val < green:
        return "🟢"
    elif val < yellow:
        return "🟡"
    return "🔴"


def _fmt_uptime(secs_str: str) -> str:
    try:
        secs = int(float(secs_str))
    except (ValueError, TypeError):
        return "?"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


def _fmt_age(ts_str: str) -> str:
    try:
        ts = float(ts_str)
    except (ValueError, TypeError):
        return "?"
    secs = time.time() - ts
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.1f}m"
    return f"{secs / 3600:.1f}h"


# ── Service discovery for display ───────────────────────────────────────────

_KNOWN_SERVICES = [
    ("monolith", "stockanalysis"),
    ("data-gateway", "stockanalysis-data-gateway"),
    ("market-data", "stockanalysis-market-data"),
    ("analysis-engine", "stockanalysis-analysis-engine"),
    ("notification-service", "stockanalysis-notification"),
]


def _discover_services(rc) -> list[str]:
    """Find all services that have sys:latest:{name} keys."""
    try:
        keys = rc.keys("sys:latest:*")
        names = []
        for k in keys:
            name = k.replace("sys:latest:", "")
            if name not in ("system", "redis"):
                names.append(name)
        return sorted(names)
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════════════
# View 1: Live dashboard
# ═══════════════════════════════════════════════════════════════════════════

def _build_live_text(rc) -> str:
    sys_raw = rc.hgetall("sys:latest:system") or {}
    redis_raw = rc.hgetall("sys:latest:redis") or {}

    if not sys_raw:
        return "⚠️ <b>Resource monitor not running</b>\nNo data in <code>sys:latest:system</code>. Start with:\n<code>sudo systemctl start stockanalysis-resource-monitor</code>"

    now_str = datetime.now().strftime("%H:%M:%S")
    lines = [
        "📊 <b>System Resource Dashboard</b>",
        f"⏰ {now_str} | Server uptime: {_fmt_uptime(sys_raw.get('uptime_secs', '0'))}",
        "",
    ]

    # ── System ──────────────────────────────────────────────────────────────
    cpu = float(sys_raw.get("cpu_percent", 0))
    ram = float(sys_raw.get("ram_percent", 0))
    core_count = int(sys_raw.get("cpu_core_count", 0))
    core_max = float(sys_raw.get("cpu_core_max", 0))
    core_avg = float(sys_raw.get("cpu_core_avg", 0))

    cpu_arrow = _trend_arrow(rc, "sys:ts:cpu", cpu)
    ram_arrow = _trend_arrow(rc, "sys:ts:ram", ram)

    lines.append("🖥️ <b>System</b>")
    lines.append(
        f"  CPU: {_color_icon(cpu)} <b>{cpu:.1f}%</b> ({cpu_arrow} from 5m ago)"
    )

    # Per-core display
    if core_count > 1:
        core_values = []
        for i in range(core_count):
            v = sys_raw.get(f"cpu_core_{i}")
            if v is not None:
                core_values.append((i, float(v)))

        if core_values:
            hottest_idx, hottest_val = max(core_values, key=lambda x: x[1])
            core_strs = []
            for idx, val in core_values:
                icon = _color_icon(val)
                marker = " ← hottest" if idx == hottest_idx and hottest_val > _color_icon.__defaults__[0] else ""
                core_strs.append(f"    Core {idx}: {icon} {val:.1f}%{marker}")
            lines.append(f"  Cores ({core_count}):")
            lines.extend(core_strs)

    lines.append(
        f"  RAM: {_color_icon(ram)} <b>{ram:.1f}%</b> "
        f"({sys_raw.get('ram_used_mb', '?')} / {sys_raw.get('ram_total_mb', '?')} MB) {ram_arrow}"
    )
    swap = float(sys_raw.get("swap_percent", 0))
    lines.append(f"  Swap: {'🟢' if swap < 10 else '🟡' if swap < 40 else '🔴'} {swap:.1f}%")
    disk = float(sys_raw.get("disk_percent", 0))
    lines.append(
        f"  Disk: {_color_icon(disk)} {disk:.1f}% "
        f"({sys_raw.get('disk_used_gb', '?')} / {sys_raw.get('disk_total_gb', '?')} GB)"
    )
    lines.append(
        f"  Load: {sys_raw.get('load_1m', '?')} / {sys_raw.get('load_5m', '?')} / {sys_raw.get('load_15m', '?')} (1/5/15m)"
    )
    lines.append(f"  Procs: {sys_raw.get('process_count', '?')}")

    # ── Services ────────────────────────────────────────────────────────────
    service_names = _discover_services(rc)
    lines.append("")
    lines.append(f"🔧 <b>Services</b> ({len(service_names)} active)")

    # Per-core usage from services
    core_totals = [0.0] * core_count if core_count > 0 else []

    for name in service_names:
        svc_raw = rc.hgetall(f"sys:latest:{name}") or {}
        if not svc_raw:
            lines.append(f"  📦 {name:<22} ⚪ no data")
            continue

        svc_cpu = float(svc_raw.get("cpu_percent", 0))
        rss = float(svc_raw.get("rss_mb", 0))
        threads = svc_raw.get("threads", "?")
        uptime = _fmt_uptime(svc_raw.get("uptime_secs", "0"))

        # CPU affinity
        affinity_str = ""
        try:
            affinity = json.loads(svc_raw.get("cpu_affinity", "[]"))
            if affinity:
                affinity_str = f"  core{affinity}"
        except Exception:
            pass

        cpu_icon = _color_icon(svc_cpu)
        lines.append(
            f"  📦 {name:<22} {cpu_icon} {svc_cpu:>5.1f}% CPU{affinity_str:<12} "
            f"{rss:>6.0f} MB  {threads:>3} thr  {uptime}"
        )

    # ── Redis ───────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("🟥 <b>Redis</b>")
    if redis_raw:
        redis_mem = float(redis_raw.get("used_memory_mb", 0))
        redis_max = float(redis_raw.get("maxmemory_mb", 0))
        redis_pct = (redis_mem / redis_max * 100) if redis_max > 0 else 0
        hit_rate = float(redis_raw.get("hit_rate", 100))

        mem_icon = _color_icon(redis_pct)
        hr_icon = "🟢" if hit_rate > 90 else "🟡" if hit_rate > 80 else "🔴"

        if redis_max > 0:
            lines.append(
                f"  Memory: {mem_icon} {redis_mem:.0f} MB / {redis_max:.0f} MB ({redis_pct:.0f}%)"
            )
        else:
            lines.append(f"  Memory: {mem_icon} {redis_mem:.0f} MB (no maxmemory limit)")
        lines.append(f"  Peak: {redis_raw.get('peak_memory_mb', '?')} MB | Frag: {redis_raw.get('fragmentation', '?')}x")
        lines.append(
            f"  Clients: {redis_raw.get('connected_clients', '?')} | "
            f"Ops/s: {redis_raw.get('ops_per_sec', '?')} | "
            f"Hit rate: {hr_icon} {hit_rate:.1f}%"
        )
        lines.append(
            f"  Keys: {redis_raw.get('total_keys', '?')} | "
            f"Slowlog: {redis_raw.get('slowlog_count', '?')} | "
            f"Evicted: {redis_raw.get('evicted_keys', '?')}"
        )
        lines.append(f"  Uptime: {_fmt_uptime(redis_raw.get('uptime_secs', '0'))}")
    else:
        lines.append("  ⚪ No Redis metrics available")

    # ── Monitor status ──────────────────────────────────────────────────────
    lines.append("")
    mon_raw = rc.hgetall("service:registry:resource-monitor") or {}
    if mon_raw.get("status") == "healthy":
        lines.append(f"  📡 Monitor: 🟢 healthy (last: {_fmt_age(mon_raw.get('last_heartbeat', '0'))} ago)")
    else:
        lines.append("  📡 Monitor: ⚪ not running")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# View 2: History — 24h sparklines + 7-day table
# ═══════════════════════════════════════════════════════════════════════════

def _build_history_text(rc) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📈 <b>24-Hour Trends</b> ({now_str})", ""]

    # ── Sparklines ──────────────────────────────────────────────────────────
    sparkline_specs = [
        ("sys:ts:cpu", "CPU Total", "%"),
        ("sys:ts:cpu_core_max", "CPU Core Max (hottest)", "%"),
        ("sys:ts:cpu_core_avg", "CPU Core Avg", "%"),
        ("sys:ts:ram", "RAM", "%"),
        ("sys:ts:redis_mem", "Redis Memory", "MB"),
    ]

    # Per-service RSS sparklines
    service_names = _discover_services(rc)
    for name in service_names:
        sparkline_specs.append((f"sys:ts:rss:{name}", f"RSS ({name})", "MB"))

    for key, label, unit in sparkline_specs:
        hourly = _ts_to_hourly_avg(rc, key, 24)
        if not hourly:
            lines.append(f"  {label}: ⚪ no data")
            continue

        avg_val = sum(hourly) / len(hourly)
        max_val = max(hourly)
        now_val = hourly[-1]
        spark = _sparkline(hourly)

        # Memory leak detection
        leak_flag = ""
        if unit == "MB" and len(hourly) >= 6:
            first_half = sum(hourly[:len(hourly)//2]) / (len(hourly)//2)
            second_half = sum(hourly[len(hourly)//2:]) / (len(hourly) - len(hourly)//2)
            if second_half > first_half * 1.15:
                leak_flag = " ⚠️ growing"

        lines.append(
            f"  {label}: avg {avg_val:.1f} {unit} | max {max_val:.1f} | now {now_val:.1f}{leak_flag}"
        )
        lines.append(f"    {spark}")

    # ── 7-day summary ───────────────────────────────────────────────────────
    lines.append("")
    lines.append("📅 <b>7-Day Summary</b>")
    lines.append(f"<code>{'Date':<10} {'CPU_avg':>8} {'CPU_max':>8} {'RAM_max':>8} {'Redis_MB':>9} {'Alerts':>7}</code>")

    from datetime import date as _date
    today = _date.today()
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        day_key = f"sys:daily:{d}"
        raw = rc.hgetall(day_key) or {}
        if not raw:
            lines.append(f"<code>{d.strftime('%m-%d'):<10} {'—':>8} {'—':>8} {'—':>8} {'—':>9} {'—':>7}</code>")
            continue

        cpu_avg = raw.get("cpu_avg", "—")
        cpu_max = raw.get("cpu_max_val", raw.get("cpu_max", "—"))
        ram_max = raw.get("ram_max_val", raw.get("ram_max", "—"))
        redis_max = raw.get("redis_mem_max", "—")
        alerts = raw.get("alert_count", "0")

        lines.append(
            f"<code>{d.strftime('%m-%d'):<10} {cpu_avg:>8} {cpu_max:>8} {ram_max:>8} {redis_max:>9} {alerts:>7}</code>"
        )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# View 3: Redis deep dive
# ═══════════════════════════════════════════════════════════════════════════

def _build_redis_text(rc) -> str:
    redis_raw = rc.hgetall("sys:latest:redis") or {}

    if not redis_raw:
        return "⚠️ <b>No Redis metrics available</b>\nResource monitor may not be running."

    lines = ["🟥 <b>Redis Health</b>", ""]

    # Memory
    used = float(redis_raw.get("used_memory_mb", 0))
    peak = float(redis_raw.get("peak_memory_mb", 0))
    maxmem = float(redis_raw.get("maxmemory_mb", 0))
    frag = float(redis_raw.get("fragmentation", 0))
    evicted = redis_raw.get("evicted_keys", "0")

    lines.append("💾 <b>Memory:</b>")
    if maxmem > 0:
        pct = used / maxmem * 100
        lines.append(f"  Used: {_color_icon(pct)} {used:.0f} MB / {maxmem:.0f} MB ({pct:.0f}%)")
    else:
        lines.append(f"  Used: {used:.0f} MB (no maxmemory limit)")
    lines.append(f"  Peak: {peak:.0f} MB")
    frag_icon = "🟢" if 0.9 <= frag <= 1.5 else "🟡"
    lines.append(f"  Fragmentation: {frag_icon} {frag:.2f}x")
    lines.append(f"  Evicted keys: {evicted}")

    # Connections
    lines.append("")
    lines.append("🔗 <b>Connections:</b>")
    lines.append(
        f"  Connected: {redis_raw.get('connected_clients', '?')} | "
        f"Blocked: {redis_raw.get('blocked_clients', '?')}"
    )

    # Throughput
    lines.append("")
    lines.append("⚡ <b>Throughput:</b>")
    ops = redis_raw.get("ops_per_sec", "?")
    hits = redis_raw.get("keyspace_hits", "?")
    misses = redis_raw.get("keyspace_misses", "?")
    hit_rate = float(redis_raw.get("hit_rate", 100))
    hr_icon = "🟢" if hit_rate > 90 else "🟡" if hit_rate > 80 else "🔴"
    lines.append(f"  Ops/sec: {ops}")
    lines.append(f"  Hits: {hits} | Misses: {misses}")
    lines.append(f"  Hit rate: {hr_icon} {hit_rate:.1f}%")

    # Keys
    lines.append("")
    lines.append("🔑 <b>Keys:</b>")
    lines.append(f"  Total: {redis_raw.get('total_keys', '?')}")

    # Slowlog
    lines.append("")
    slowlog = int(redis_raw.get("slowlog_count", 0))
    sl_icon = "🟢" if slowlog == 0 else "🟡" if slowlog < 10 else "🔴"
    lines.append(f"🐌 <b>Slowlog:</b> {sl_icon} {slowlog} entries")

    # Uptime
    lines.append("")
    lines.append(f"⏱️ Uptime: {_fmt_uptime(redis_raw.get('uptime_secs', '0'))}")

    # Redis memory sparkline
    lines.append("")
    hourly = _ts_to_hourly_avg(rc, "sys:ts:redis_mem", 24)
    if hourly:
        avg_val = sum(hourly) / len(hourly)
        max_val = max(hourly)
        lines.append(f"📊 24h Memory: avg {avg_val:.0f} MB | max {max_val:.0f} MB")
        lines.append(f"   {_sparkline(hourly)}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Handler
# ═══════════════════════════════════════════════════════════════════════════

@guard
async def cmd_sysstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not debug_chat_only(update):
        logger.debug(f"[sysstats] Ignored from non-debug chat {update.effective_chat.id}")
        return

    rc = _get_redis()
    if rc is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ Redis unavailable — cannot read resource metrics.",
        )
        return

    args = context.args or []
    if args and args[0].lower() == "history":
        text = _build_history_text(rc)
    elif args and args[0].lower() == "redis":
        text = _build_redis_text(rc)
    else:
        text = _build_live_text(rc)

    if len(text) <= 4096:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, parse_mode="HTML"
        )
    else:
        # Split into chunks for Telegram's 4096 char limit
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
    ("sysstats", cmd_sysstats),
]
