"""
Resource Monitor — lightweight system + per-service + Redis metrics collector.

Runs as a standalone systemd daemon. Every 30 seconds:
  1. Collects system-wide metrics via psutil (CPU per-core, RAM, swap, disk, load, net)
  2. Discovers running services via Redis service:registry:* and collects per-process metrics
  3. Collects Redis INFO + SLOWLOG metrics
  4. Writes latest snapshots to sys:latest:* (HASH)
  5. Appends time-series to sys:ts:* (ZSET, 24h retention)
  6. Updates daily rollup sys:daily:{date} (30-day TTL)
  7. Checks alert thresholds and sends proactive alerts via notification:jobs
  8. Writes own heartbeat to service:registry:resource-monitor

All Redis writes use sync redis (not async) — this is a simple poll-write loop.

Usage:
    python services/resource_monitor/main.py
"""
from __future__ import annotations

import os
import sys
import time
import json
import signal
import subprocess
import argparse
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from dotenv import load_dotenv
load_dotenv()

import redis as sync_redis
from services.common.logging import get_logger
logger = get_logger("resource-monitor")

try:
    import psutil
except ImportError:
    psutil = None
    logger.error("psutil not installed — resource monitor cannot run")

# ── Constants ──────────────────────────────────────────────────────────────

SAMPLING_INTERVAL = 30          # seconds between samples
TS_RETENTION_SECS = 86400       # 24h — prune ZSET entries older than this
DAILY_TTL = 30 * 86400          # 30 days for daily rollup keys
TS_TTL = 25 * 3600              # 25h TTL on time-series keys (auto-expire if monitor dies)
HEARTBEAT_TTL = 60              # service registry heartbeat TTL

# Alert thresholds
ALERT_CPU_HIGH = 90.0           # system CPU % for 3+ consecutive samples
ALERT_CPU_HIGH_CONSEC = 3       # consecutive samples needed
ALERT_RAM_HIGH = 85.0           # system RAM %
ALERT_RSS_LEAK_MB_PER_HR = 10   # RSS growth rate for leak detection
ALERT_RSS_LEAK_SAMPLES = 120    # 1h worth of samples (120 × 30s)
ALERT_RSS_CRIT_PCT = 80.0       # RSS > 80% of MemoryMax
ALERT_REDIS_MEM_PCT = 80.0      # Redis used_memory > 80% of maxmemory
ALERT_REDIS_HITRATE = 80.0      # hit rate below this
ALERT_SERVICE_OFFLINE_SECS = 60 # no heartbeat in 60s
ALERT_SLOWLOG_COUNT = 10        # slowlog entries
ALERT_CORE_IMBALANCE_HIGH = 80.0
ALERT_CORE_IMBALANCE_LOW = 10.0
ALERT_CORE_IMBALANCE_SAMPLES = 10  # 5 min (10 × 30s)

# Alert cooldowns (seconds)
COOLDOWN_CPU_HIGH = 1800        # 30 min
COOLDOWN_RAM_HIGH = 1800
COOLDOWN_RSS_LEAK = 3600        # 1 hour
COOLDOWN_RSS_CRIT = 900         # 15 min
COOLDOWN_REDIS_MEM = 1800
COOLDOWN_REDIS_HITRATE = 3600
COOLDOWN_SERVICE_OFFLINE = 300  # 5 min
COOLDOWN_SLOWLOG = 3600
COOLDOWN_CORE_IMBALANCE = 3600

# Known service names for discovery
KNOWN_SERVICES = [
    "data-gateway",
    "market-data",
    "analysis-engine",
    "notification-service",
]

# CPU % color thresholds (for bot formatting)
_CPU_GREEN = 60.0
_CPU_YELLOW = 80.0
_RAM_GREEN = 60.0
_RAM_YELLOW = 80.0

_running = True


def signal_handler(signum, frame):
    global _running
    logger.info("[resource-monitor] Received signal, shutting down...")
    _running = False


# ═══════════════════════════════════════════════════════════════════════════
# Collector — psutil + Redis INFO
# ═══════════════════════════════════════════════════════════════════════════

def collect_system() -> dict:
    """Collect system-wide metrics via psutil."""
    if psutil is None:
        return {}
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    du = psutil.disk_usage("/")
    load = psutil.getloadavg()
    net = psutil.net_io_counters()
    boot_time = psutil.boot_time()

    cpu_total = psutil.cpu_percent(interval=0.5)
    cpu_percpu = psutil.cpu_percent(interval=0.5, percpu=True)
    core_count = len(cpu_percpu) if cpu_percpu else 1
    core_max = max(cpu_percpu) if cpu_percpu else cpu_total
    core_avg = (sum(cpu_percpu) / core_count) if cpu_percpu else cpu_total

    result = {
        "cpu_percent": f"{cpu_total:.1f}",
        "cpu_core_count": str(core_count),
        "cpu_core_max": f"{core_max:.1f}",
        "cpu_core_avg": f"{core_avg:.1f}",
        "ram_percent": f"{vm.percent:.1f}",
        "ram_used_mb": f"{vm.used / 1048576:.1f}",
        "ram_total_mb": f"{vm.total / 1048576:.1f}",
        "ram_available_mb": f"{vm.available / 1048576:.1f}",
        "swap_percent": f"{sm.percent:.1f}",
        "swap_used_mb": f"{sm.used / 1048576:.1f}",
        "disk_percent": f"{du.percent:.1f}",
        "disk_used_gb": f"{du.used / 1073741824:.1f}",
        "disk_total_gb": f"{du.total / 1073741824:.1f}",
        "disk_free_gb": f"{du.free / 1073741824:.1f}",
        "load_1m": f"{load[0]:.2f}",
        "load_5m": f"{load[1]:.2f}",
        "load_15m": f"{load[2]:.2f}",
        "net_sent_mb": f"{net.bytes_sent / 1048576:.1f}",
        "net_recv_mb": f"{net.bytes_recv / 1048576:.1f}",
        "process_count": str(len(psutil.pids())),
        "uptime_secs": str(int(time.time() - boot_time)),
        "timestamp": str(time.time()),
    }
    for i, v in enumerate(cpu_percpu):
        result[f"cpu_core_{i}"] = f"{v:.1f}"
    return result


def collect_per_core() -> list[float]:
    """Return per-core CPU percentages for imbalance detection."""
    if psutil is None:
        return []
    return psutil.cpu_percent(interval=0.5, percpu=True)


def collect_process(pid: int) -> dict:
    """Collect per-process metrics via psutil.Process."""
    if psutil is None:
        return {}
    try:
        proc = psutil.Process(pid)
        mi = proc.memory_info()
        try:
            cpu_affinity = proc.cpu_affinity()
        except Exception:
            cpu_affinity = []
        try:
            fds = proc.num_fds()
        except Exception:
            fds = -1
        try:
            children = len(proc.children(recursive=True))
        except Exception:
            children = 0
        return {
            "cpu_percent": f"{proc.cpu_percent(interval=0.3):.1f}",
            "rss_mb": f"{mi.rss / 1048576:.1f}",
            "vms_mb": f"{mi.vms / 1048576:.1f}",
            "threads": str(proc.num_threads()),
            "fds": str(fds),
            "uptime_secs": str(int(time.time() - proc.create_time())),
            "status": proc.status(),
            "children": str(children),
            "cpu_affinity": json.dumps(cpu_affinity),
            "timestamp": str(time.time()),
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {}
    except Exception as e:
        logger.debug(f"[collector] Failed to collect process {pid}: {e}")
        return {}


def collect_redis(rc: sync_redis.Redis) -> dict:
    """Collect Redis server metrics via INFO + SLOWLOG."""
    result = {}
    try:
        info = rc.info(section="memory")
        used_mb = info.get("used_memory", 0) / 1048576
        peak_mb = info.get("used_memory_peak", 0) / 1048576
        maxmemory = info.get("maxmemory", 0)
        frag_ratio = info.get("mem_fragmentation_ratio", 0.0)
        result["used_memory_mb"] = f"{used_mb:.1f}"
        result["peak_memory_mb"] = f"{peak_mb:.1f}"
        result["maxmemory_mb"] = f"{maxmemory / 1048576:.1f}" if maxmemory else "0"
        result["fragmentation"] = f"{frag_ratio:.2f}"
        result["evicted_keys"] = str(info.get("evicted_keys", 0))
    except Exception as e:
        logger.debug(f"[collector] Redis memory info failed: {e}")

    try:
        info = rc.info(section="clients")
        result["connected_clients"] = str(info.get("connected_clients", 0))
        result["blocked_clients"] = str(info.get("blocked_clients", 0))
    except Exception:
        pass

    try:
        info = rc.info(section="stats")
        hits = info.get("keyspace_hits", 0)
        misses = info.get("keyspace_misses", 0)
        total = hits + misses
        hit_rate = (hits / total * 100) if total > 0 else 0.0
        result["ops_per_sec"] = str(info.get("instantaneous_ops_per_sec", 0))
        result["keyspace_hits"] = str(hits)
        result["keyspace_misses"] = str(misses)
        result["hit_rate"] = f"{hit_rate:.1f}"
        result["evicted_keys_stats"] = str(info.get("evicted_keys", 0))
    except Exception:
        pass

    try:
        result["total_keys"] = str(rc.dbsize())
    except Exception:
        pass

    try:
        result["slowlog_count"] = str(len(rc.slowlog_get(10)))
    except Exception:
        result["slowlog_count"] = "0"

    try:
        info = rc.info(section="server")
        result["uptime_secs"] = str(info.get("uptime_in_seconds", 0))
    except Exception:
        pass

    result["timestamp"] = str(time.time())
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Service discovery
# ═══════════════════════════════════════════════════════════════════════════

def discover_services(rc: sync_redis.Redis) -> dict[str, dict]:
    """Discover running services from Redis service:registry:* keys.

    Returns: {service_name: {pid, status, last_heartbeat, ...}}
    """
    services = {}
    try:
        keys = rc.keys("service:registry:*")
        for key in keys:
            name = key.replace("service:registry:", "")
            raw = rc.hgetall(key)
            if not raw:
                continue
            pid = 0
            try:
                pid = int(raw.get("pid", 0))
            except (ValueError, TypeError):
                pass
            services[name] = {
                "pid": pid,
                "status": raw.get("status", "unknown"),
                "last_heartbeat": raw.get("last_heartbeat", "0"),
            }
    except Exception as e:
        logger.debug(f"[discovery] Failed to scan service registry: {e}")

    # Discover monolith (doesn't write to registry)
    monolith_pid = _discover_monolith_pid()
    if monolith_pid:
        services["monolith"] = {
            "pid": monolith_pid,
            "status": "healthy",
            "last_heartbeat": str(time.time()),
        }

    return services


def _discover_monolith_pid() -> int:
    """Find the monolith PID via systemctl or pgrep."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "stockanalysis.service", "--property=MainPID"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            line = result.stdout.strip()
            if "=" in line:
                pid = int(line.split("=")[1])
                if pid > 0:
                    return pid
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["pgrep", "-f", "intraday_monitor"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            return int(pids[0])
    except Exception:
        pass

    return 0


# ═══════════════════════════════════════════════════════════════════════════
# Storage — Redis writes
# ═══════════════════════════════════════════════════════════════════════════

def store_latest(rc: sync_redis.Redis, key: str, data: dict):
    """Write a latest snapshot HASH."""
    try:
        rc.hset(key, mapping=data)
        rc.expire(key, 120)
    except Exception as e:
        logger.debug(f"[storage] hset {key} failed: {e}")


def store_timeseries(rc: sync_redis.Redis, key: str, ts: float, value: float):
    """Append a value to a time-series ZSET and prune old entries."""
    try:
        pipe = rc.pipeline()
        pipe.zadd(key, {f"{ts:.6f}:{value}": ts})
        pipe.zremrangebyscore(key, 0, ts - TS_RETENTION_SECS)
        pipe.expire(key, TS_TTL)
        pipe.execute()
    except Exception as e:
        logger.debug(f"[storage] zadd {key} failed: {e}")


def update_daily_rollup(rc: sync_redis.Redis, metrics: dict):
    """Update sys:daily:{date} with running max/avg for key metrics."""
    today = str(date.today())
    key = f"sys:daily:{today}"
    now = time.time()

    try:
        existing = rc.hgetall(key)
        count = int(existing.get("sample_count", "0")) + 1

        def _running_max(field, new_val):
            old = float(existing.get(field, "0"))
            return f"{max(old, new_val):.1f}"

        def _running_avg(field, new_val):
            old_avg = float(existing.get(field, "0"))
            new_avg = ((old_avg * (count - 1)) + new_val) / count
            return f"{new_avg:.1f}"

        def _max_time(field, new_val):
            old = float(existing.get(field.replace("_max", "_max_val"), "0"))
            if new_val > old:
                return datetime.now().strftime("%H:%M:%S")
            return existing.get(field, "00:00:00")

        mapping = {
            "sample_count": str(count),
            "timestamp": str(now),
        }

        cpu = float(metrics.get("cpu_percent", 0))
        mapping["cpu_avg"] = _running_avg("cpu_avg", cpu)
        mapping["cpu_max"] = _running_max("cpu_max", cpu)
        if cpu > float(existing.get("cpu_max_val", "0")):
            mapping["cpu_max_val"] = f"{cpu:.1f}"
            mapping["cpu_max_time"] = datetime.now().strftime("%H:%M:%S")
        else:
            mapping["cpu_max_val"] = existing.get("cpu_max_val", f"{cpu:.1f}")
            mapping["cpu_max_time"] = existing.get("cpu_max_time", "00:00:00")

        ram = float(metrics.get("ram_percent", 0))
        mapping["ram_avg"] = _running_avg("ram_avg", ram)
        mapping["ram_max"] = _running_max("ram_max", ram)
        if ram > float(existing.get("ram_max_val", "0")):
            mapping["ram_max_val"] = f"{ram:.1f}"
            mapping["ram_max_time"] = datetime.now().strftime("%H:%M:%S")
        else:
            mapping["ram_max_val"] = existing.get("ram_max_val", f"{ram:.1f}")
            mapping["ram_max_time"] = existing.get("ram_max_time", "00:00:00")

        core_max = float(metrics.get("cpu_core_max", 0))
        mapping["cpu_core_max_avg"] = _running_avg("cpu_core_max_avg", core_max)
        if core_max > float(existing.get("cpu_core_max_peak", "0")):
            mapping["cpu_core_max_peak"] = f"{core_max:.1f}"
            mapping["cpu_core_max_time"] = datetime.now().strftime("%H:%M:%S")
        else:
            mapping["cpu_core_max_peak"] = existing.get("cpu_core_max_peak", f"{core_max:.1f}")
            mapping["cpu_core_max_time"] = existing.get("cpu_core_max_time", "00:00:00")

        redis_mem = float(metrics.get("redis_used_memory_mb", 0))
        mapping["redis_mem_avg"] = _running_avg("redis_mem_avg", redis_mem)
        mapping["redis_mem_max"] = _running_max("redis_mem_max", redis_mem)

        rc.hset(key, mapping=mapping)
        rc.expire(key, DAILY_TTL)
    except Exception as e:
        logger.debug(f"[storage] daily rollup failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Alert engine
# ═══════════════════════════════════════════════════════════════════════════

_cpu_high_streak = 0
_core_imbalance_streak = 0


def _alert_cooldown_ok(rc: sync_redis.Redis, alert_name: str, cooldown: int) -> bool:
    """Check if alert cooldown is active. Returns True if alert can fire (not in cooldown)."""
    key = f"sys:alert_cooldown:{alert_name}"
    try:
        result = rc.set(key, "1", ex=cooldown, nx=True)
        return result is not None
    except Exception:
        return True  # fail-safe: send alert


def _send_alert(rc: sync_redis.Redis, message: str, alert_name: str = ""):
    """Send an alert via the notification:jobs stream."""
    try:
        rc.xadd("notification:jobs", {
            "chat_type": "intraday",
            "message": message,
            "parse_mode": "HTML",
            "message_type": "resource_alert",
            "symbol": "",
            "timestamp": str(datetime.now()),
        }, maxlen=1000)
        # Increment alert count in daily rollup
        today = str(date.today())
        rc.hincrby(f"sys:daily:{today}", "alert_count", 1)
        logger.warning(f"[alert] {alert_name}: {message[:80]}...")
    except Exception as e:
        logger.error(f"[alert] Failed to send alert: {e}")


def check_alerts(rc: sync_redis.Redis, sys_metrics: dict, services: dict, redis_metrics: dict):
    """Check all alert thresholds and fire proactive alerts."""
    global _cpu_high_streak, _core_imbalance_streak

    # ── CPU High ────────────────────────────────────────────────────────────
    cpu = float(sys_metrics.get("cpu_percent", 0))
    if cpu > ALERT_CPU_HIGH:
        _cpu_high_streak += 1
    else:
        _cpu_high_streak = 0

    if _cpu_high_streak >= ALERT_CPU_HIGH_CONSEC:
        if _alert_cooldown_ok(rc, "cpu_high", COOLDOWN_CPU_HIGH):
            cores = sys_metrics.get("cpu_core_count", "?")
            core_max = sys_metrics.get("cpu_core_max", "?")
            msg = (
                f"🔴 <b>RESOURCE ALERT: CPU High</b>\n"
                f"Server CPU at <b>{cpu:.1f}%</b> for {ALERT_CPU_HIGH_CONSEC}+ samples\n"
                f"Hottest core: <b>{core_max}%</b> ({cores} cores)\n"
                f"Load: {sys_metrics.get('load_1m', '?')} / {sys_metrics.get('load_5m', '?')} / {sys_metrics.get('load_15m', '?')}"
            )
            _send_alert(rc, msg, "cpu_high")

    # ── RAM High ────────────────────────────────────────────────────────────
    ram = float(sys_metrics.get("ram_percent", 0))
    if ram > ALERT_RAM_HIGH:
        if _alert_cooldown_ok(rc, "ram_high", COOLDOWN_RAM_HIGH):
            used = sys_metrics.get("ram_used_mb", "?")
            total = sys_metrics.get("ram_total_mb", "?")
            avail = sys_metrics.get("ram_available_mb", "?")
            msg = (
                f"🔴 <b>RESOURCE ALERT: RAM Critical</b>\n"
                f"System RAM at <b>{ram:.1f}%</b> ({used} MB / {total} MB)\n"
                f"Available: <b>{avail} MB</b>\n"
                f"Swap: {sys_metrics.get('swap_percent', '?')}%"
            )
            _send_alert(rc, msg, "ram_high")

    # ── Core Imbalance ──────────────────────────────────────────────────────
    core_count = int(sys_metrics.get("cpu_core_count", 0))
    if core_count >= 2:
        core_values = []
        for i in range(core_count):
            v = sys_metrics.get(f"cpu_core_{i}")
            if v is not None:
                core_values.append(float(v))
        if core_values:
            hottest = max(core_values)
            coolest = min(core_values)
            if hottest > ALERT_CORE_IMBALANCE_HIGH and coolest < ALERT_CORE_IMBALANCE_LOW:
                _core_imbalance_streak += 1
            else:
                _core_imbalance_streak = 0

            if _core_imbalance_streak >= ALERT_CORE_IMBALANCE_SAMPLES:
                if _alert_cooldown_ok(rc, "core_imbalance", COOLDOWN_CORE_IMBALANCE):
                    hot_idx = core_values.index(hottest)
                    cool_idx = core_values.index(coolest)
                    msg = (
                        f"🟡 <b>RESOURCE ALERT: CPU Core Imbalance</b>\n"
                        f"Core {hot_idx} at <b>{hottest:.1f}%</b> while core {cool_idx} at <b>{coolest:.1f}%</b>\n"
                        f"Lasting {ALERT_CORE_IMBALANCE_SAMPLES * SAMPLING_INTERVAL}s+\n"
                        f"Consider adjusting CPUAffinity in systemd units."
                    )
                    _send_alert(rc, msg, "core_imbalance")

    # ── Per-service RSS leak + critical ─────────────────────────────────────
    for name, info in services.items():
        pid = info.get("pid", 0)
        if pid <= 0:
            continue
        rss_key = f"sys:ts:rss:{name}"
        try:
            ts_now = time.time()
            # Get samples from last hour
            samples = rc.zrangebyscore(rss_key, ts_now - 3600, ts_now, withscores=True)
            if len(samples) >= ALERT_RSS_LEAK_SAMPLES:
                # Calculate slope (MB/hour)
                # Member format: "{ts}:{value}" — parse value after colon
                first_val = float(samples[0][0].split(":", 1)[-1])
                first_ts = samples[0][1]
                last_val = float(samples[-1][0].split(":", 1)[-1])
                last_ts = samples[-1][1]
                if last_ts > first_ts:
                    time_diff_hr = (last_ts - first_ts) / 3600
                    if time_diff_hr > 0:
                        slope = (last_val - first_val) / time_diff_hr
                        if slope > ALERT_RSS_LEAK_MB_PER_HR:
                            if _alert_cooldown_ok(rc, f"rss_leak:{name}", COOLDOWN_RSS_LEAK):
                                msg = (
                                    f"🟡 <b>RESOURCE ALERT: Memory Leak Suspected</b>\n"
                                    f"Service <b>{name}</b> RSS growing at <b>{slope:.1f} MB/hour</b>\n"
                                    f"Current: {last_val:.0f} MB | 1h ago: {first_val:.0f} MB\n"
                                    f"PID: {pid}"
                                )
                                _send_alert(rc, msg, f"rss_leak:{name}")

            # RSS critical (vs MemoryMax from systemd — rough check: > 80% of system RAM / 5 services)
            latest = rc.hgetall(f"sys:latest:{name}")
            if latest:
                rss_mb = float(latest.get("rss_mb", 0))
                # Check against systemd MemoryMax via cgroup
                memmax = _get_service_memmax(name)
                if memmax and rss_mb > (memmax * ALERT_RSS_CRIT_PCT / 100):
                    if _alert_cooldown_ok(rc, f"rss_crit:{name}", COOLDOWN_RSS_CRIT):
                        msg = (
                            f"🔴 <b>RESOURCE ALERT: Service Memory Critical</b>\n"
                            f"Service <b>{name}</b> RSS at <b>{rss_mb:.0f} MB</b>\n"
                            f"MemoryMax: {memmax:.0f} MB ({rss_mb / memmax * 100:.0f}% used)\n"
                            f"PID: {pid}"
                        )
                        _send_alert(rc, msg, f"rss_crit:{name}")
        except Exception as e:
            logger.debug(f"[alert] RSS check for {name} failed: {e}")

    # ── Redis memory high ───────────────────────────────────────────────────
    redis_used = float(redis_metrics.get("used_memory_mb", 0))
    redis_max = float(redis_metrics.get("maxmemory_mb", 0))
    if redis_max > 0 and redis_used > (redis_max * ALERT_REDIS_MEM_PCT / 100):
        if _alert_cooldown_ok(rc, "redis_mem_high", COOLDOWN_REDIS_MEM):
            msg = (
                f"🔴 <b>RESOURCE ALERT: Redis Memory High</b>\n"
                f"Redis at <b>{redis_used:.0f} MB</b> / {redis_max:.0f} MB "
                f"({redis_used / redis_max * 100:.0f}%)\n"
                f"Evicted keys: {redis_metrics.get('evicted_keys', '0')}"
            )
            _send_alert(rc, msg, "redis_mem_high")

    # ── Redis hit rate low ──────────────────────────────────────────────────
    hit_rate = float(redis_metrics.get("hit_rate", 100))
    if hit_rate < ALERT_REDIS_HITRATE:
        if _alert_cooldown_ok(rc, "redis_hitrate_low", COOLDOWN_REDIS_HITRATE):
            hits = redis_metrics.get("keyspace_hits", "?")
            misses = redis_metrics.get("keyspace_misses", "?")
            msg = (
                f"🟡 <b>RESOURCE ALERT: Redis Hit Rate Low</b>\n"
                f"Hit rate: <b>{hit_rate:.1f}%</b>\n"
                f"Hits: {hits} | Misses: {misses}\n"
                f"Consider reviewing TTLs and key patterns."
            )
            _send_alert(rc, msg, "redis_hitrate_low")

    # ── Slowlog spike ───────────────────────────────────────────────────────
    slowlog = int(redis_metrics.get("slowlog_count", 0))
    if slowlog > ALERT_SLOWLOG_COUNT:
        if _alert_cooldown_ok(rc, "slowlog_spike", COOLDOWN_SLOWLOG):
            msg = (
                f"🟡 <b>RESOURCE ALERT: Redis Slowlog Spike</b>\n"
                f"<b>{slowlog}</b> slow queries detected\n"
                f"Check: SLOWLOG GET 10"
            )
            _send_alert(rc, msg, "slowlog_spike")

    # ── Service offline ─────────────────────────────────────────────────────
    now = time.time()
    for name, info in services.items():
        last_hb = float(info.get("last_heartbeat", 0))
        if last_hb > 0 and (now - last_hb) > ALERT_SERVICE_OFFLINE_SECS:
            if name == "monolith":
                continue  # monolith doesn't write heartbeat
            if _alert_cooldown_ok(rc, f"service_offline:{name}", COOLDOWN_SERVICE_OFFLINE):
                lag = now - last_hb
                msg = (
                    f"🔴 <b>RESOURCE ALERT: Service Offline</b>\n"
                    f"Service <b>{name}</b> — no heartbeat for <b>{lag:.0f}s</b>\n"
                    f"Last seen: {datetime.fromtimestamp(last_hb).strftime('%H:%M:%S')}"
                )
                _send_alert(rc, msg, f"service_offline:{name}")


def _get_service_memmax(name: str) -> float:
    """Get the MemoryMax (in MB) for a service from systemd."""
    unit_map = {
        "monolith": "stockanalysis.service",
        "data-gateway": "stockanalysis-data-gateway.service",
        "market-data": "stockanalysis-market-data.service",
        "analysis-engine": "stockanalysis-analysis-engine.service",
        "notification-service": "stockanalysis-notification.service",
    }
    unit = unit_map.get(name)
    if not unit:
        return 0.0
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=MemoryMax"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            line = result.stdout.strip()
            if "=" in line:
                val = line.split("=")[1]
                if val and val != "[not set]":
                    return int(val) / 1048576  # bytes → MB
    except Exception:
        pass
    return 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════════

def write_heartbeat(rc: sync_redis.Redis):
    """Write own service registry heartbeat."""
    try:
        from services.common.version import BUILD_LABEL, GIT_COMMIT, GIT_DIRTY

        rc.hset("service:registry:resource-monitor", mapping={
            "name": "resource-monitor",
            "pid": str(os.getpid()),
            "status": "healthy",
            "last_heartbeat": str(time.time()),
            "version": BUILD_LABEL,
            "commit": GIT_COMMIT,
            "dirty": str(GIT_DIRTY),
        })
        rc.expire("service:registry:resource-monitor", HEARTBEAT_TTL)
    except Exception:
        pass


def main():
    global _running

    parser = argparse.ArgumentParser(description="StockAnalysis Resource Monitor")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    if psutil is None:
        logger.error("psutil is required. Install with: pip install psutil")
        sys.exit(1)

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    rc = sync_redis.from_url(redis_url, decode_responses=True)

    try:
        rc.ping()
        logger.info(f"[resource-monitor] Started (pid={os.getpid()}, redis={redis_url})")
        from services.common.version import BUILD_LABEL
        logger.info(f"[resource-monitor] v{BUILD_LABEL} starting")
    except Exception as e:
        logger.error(f"[resource-monitor] Cannot connect to Redis: {e}")
        sys.exit(1)

    # Warm up psutil CPU stats (first call returns 0.0)
    if psutil:
        psutil.cpu_percent(interval=0.5)
        psutil.cpu_percent(interval=0.5, percpu=True)

    cycle = 0
    while _running:
        try:
            cycle += 1
            ts = time.time()

            # 1. Collect system metrics
            sys_metrics = collect_system()
            store_latest(rc, "sys:latest:system", sys_metrics)

            # Time-series for system
            store_timeseries(rc, "sys:ts:cpu", ts, float(sys_metrics.get("cpu_percent", 0)))
            store_timeseries(rc, "sys:ts:ram", ts, float(sys_metrics.get("ram_percent", 0)))
            store_timeseries(rc, "sys:ts:cpu_core_avg", ts, float(sys_metrics.get("cpu_core_avg", 0)))
            store_timeseries(rc, "sys:ts:cpu_core_max", ts, float(sys_metrics.get("cpu_core_max", 0)))

            # 2. Discover services
            services = discover_services(rc)

            # 3. Collect per-service metrics
            for name, info in services.items():
                pid = info.get("pid", 0)
                if pid <= 0:
                    continue
                proc_metrics = collect_process(pid)
                if proc_metrics:
                    store_latest(rc, f"sys:latest:{name}", proc_metrics)
                    store_timeseries(rc, f"sys:ts:rss:{name}", ts, float(proc_metrics.get("rss_mb", 0)))
                    store_timeseries(rc, f"sys:ts:cpu:{name}", ts, float(proc_metrics.get("cpu_percent", 0)))

            # 4. Collect Redis metrics
            redis_metrics = collect_redis(rc)
            store_latest(rc, "sys:latest:redis", redis_metrics)
            store_timeseries(rc, "sys:ts:redis_mem", ts, float(redis_metrics.get("used_memory_mb", 0)))

            # 5. Update daily rollup
            rollup_data = {**sys_metrics, **{
                "redis_used_memory_mb": redis_metrics.get("used_memory_mb", "0"),
            }}
            update_daily_rollup(rc, rollup_data)

            # 6. Check alerts
            check_alerts(rc, sys_metrics, services, redis_metrics)

            # 7. Write heartbeat
            write_heartbeat(rc)

            elapsed = time.time() - ts
            if cycle % 120 == 0:  # log every hour
                logger.info(
                    f"[resource-monitor] cycle={cycle} "
                    f"cpu={sys_metrics.get('cpu_percent', '?')}% "
                    f"ram={sys_metrics.get('ram_percent', '?')}% "
                    f"services={len(services)} "
                    f"elapsed={elapsed:.1f}s"
                )

        except Exception as e:
            logger.error(f"[resource-monitor] Cycle error: {e}")

        # Sleep
        for _ in range(SAMPLING_INTERVAL):
            if not _running:
                break
            time.sleep(1)

    logger.info("[resource-monitor] Shutting down...")
    rc.close()


if __name__ == "__main__":
    main()
