"""
Unit tests for the resource monitor service and /sysstats bot command.

Covers:
- Collector: system, process, redis metrics (mocked psutil + redis)
- Storage: latest snapshot, time-series, daily rollup (mocked Redis)
- Alerts: CPU high, RAM high, core imbalance, service offline, RSS leak
- Sysstats command: live, history, redis views (mocked Redis reads)
- Sparkline rendering
"""
import json
import time
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, date


# ═══════════════════════════════════════════════════════════════════════════
# Collector tests
# ═══════════════════════════════════════════════════════════════════════════

class TestCollectSystem:
    """Test collect_system() with mocked psutil."""

    def test_returns_all_expected_fields(self):
        from services.resource_monitor.main import collect_system

        mock_vm = MagicMock(percent=65.2, used=5_200_000_000, total=8_000_000_000,
                            available=2_800_000_000)
        mock_sm = MagicMock(percent=0.0, used=0)
        mock_du = MagicMock(percent=45.3, used=45_300_000_000, total=100_000_000_000,
                            free=54_700_000_000)
        mock_net = MagicMock(bytes_sent=125_000_000, bytes_recv=430_000_000)

        def cpu_percent_side_effect(interval=None, percpu=False):
            if percpu:
                return [12.5, 3.2, 8.1, 0.5]
            return 12.5

        with patch("services.resource_monitor.main.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = mock_vm
            mock_psutil.swap_memory.return_value = mock_sm
            mock_psutil.disk_usage.return_value = mock_du
            mock_psutil.getloadavg.return_value = (0.8, 1.2, 1.0)
            mock_psutil.net_io_counters.return_value = mock_net
            mock_psutil.boot_time.return_value = time.time() - 86400
            mock_psutil.cpu_percent.side_effect = cpu_percent_side_effect
            mock_psutil.pids.return_value = list(range(142))

            result = collect_system()

        assert "cpu_percent" in result
        assert "cpu_core_count" in result
        assert "cpu_core_max" in result
        assert "cpu_core_avg" in result
        assert "ram_percent" in result
        assert "ram_used_mb" in result
        assert "disk_percent" in result
        assert "load_1m" in result
        assert "timestamp" in result

        # Per-core fields
        assert "cpu_core_0" in result
        assert "cpu_core_1" in result
        assert "cpu_core_2" in result
        assert "cpu_core_3" in result

    def test_cpu_core_max_is_hottest(self):
        from services.resource_monitor.main import collect_system

        def cpu_percent_side_effect(interval=None, percpu=False):
            if percpu:
                return [5.0, 80.0, 10.0, 2.0]
            return 24.25

        with patch("services.resource_monitor.main.psutil") as mock_psutil:
            mock_psutil.virtual_memory.return_value = MagicMock(
                percent=50, used=4e9, total=8e9, available=4e9)
            mock_psutil.swap_memory.return_value = MagicMock(percent=0, used=0)
            mock_psutil.disk_usage.return_value = MagicMock(
                percent=40, used=40e9, total=100e9, free=60e9)
            mock_psutil.getloadavg.return_value = (0.5, 0.5, 0.5)
            mock_psutil.net_io_counters.return_value = MagicMock(
                bytes_sent=0, bytes_recv=0)
            mock_psutil.boot_time.return_value = time.time() - 100
            mock_psutil.cpu_percent.side_effect = cpu_percent_side_effect
            mock_psutil.pids.return_value = [1, 2, 3]

            result = collect_system()

        assert float(result["cpu_core_max"]) == 80.0
        assert float(result["cpu_core_avg"]) == pytest.approx(24.25, rel=0.1)


class TestCollectProcess:
    """Test collect_process() with mocked psutil.Process."""

    def test_returns_process_metrics(self):
        from services.resource_monitor.main import collect_process

        mock_proc = MagicMock()
        mock_proc.memory_info.return_value = MagicMock(rss=250_000_000, vms=512_000_000)
        mock_proc.cpu_percent.return_value = 3.2
        mock_proc.num_threads.return_value = 12
        mock_proc.num_fds.return_value = 45
        mock_proc.create_time.return_value = time.time() - 11700
        mock_proc.status.return_value = "running"
        mock_proc.children.return_value = []
        mock_proc.cpu_affinity.return_value = [0, 1]

        with patch("services.resource_monitor.main.psutil") as mock_psutil:
            mock_psutil.Process.return_value = mock_proc
            mock_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
            mock_psutil.AccessDenied = type("AccessDenied", (Exception,), {})

            result = collect_process(1234)

        assert result["cpu_percent"] == "3.2"
        assert float(result["rss_mb"]) == pytest.approx(238.4, rel=0.1)
        assert result["threads"] == "12"
        assert result["fds"] == "45"
        assert result["status"] == "running"
        assert json.loads(result["cpu_affinity"]) == [0, 1]

    def test_returns_empty_on_no_such_process(self):
        from services.resource_monitor.main import collect_process

        with patch("services.resource_monitor.main.psutil") as mock_psutil:
            mock_psutil.Process.side_effect = mock_psutil.NoSuchProcess()
            mock_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
            mock_psutil.AccessDenied = type("AccessDenied", (Exception,), {})

            result = collect_process(99999)

        assert result == {}


class TestCollectRedis:
    """Test collect_redis() with mocked Redis."""

    def test_returns_redis_metrics(self):
        from services.resource_monitor.main import collect_redis

        rc = MagicMock()
        rc.info.side_effect = lambda section: {
            "memory": {"used_memory": 156_000_000, "used_memory_peak": 162_000_000,
                       "maxmemory": 512_000_000, "mem_fragmentation_ratio": 1.02,
                       "evicted_keys": 0},
            "clients": {"connected_clients": 6, "blocked_clients": 0},
            "stats": {"instantaneous_ops_per_sec": 1240, "keyspace_hits": 15420,
                      "keyspace_misses": 950, "evicted_keys": 0},
            "server": {"uptime_in_seconds": 172800},
        }[section]
        rc.dbsize.return_value = 2150
        rc.slowlog_get.return_value = []

        result = collect_redis(rc)

        assert float(result["used_memory_mb"]) == pytest.approx(148.9, rel=0.1)
        assert result["connected_clients"] == "6"
        assert result["ops_per_sec"] == "1240"
        assert float(result["hit_rate"]) == pytest.approx(94.2, rel=0.1)
        assert result["total_keys"] == "2150"
        assert result["slowlog_count"] == "0"
        assert result["uptime_secs"] == "172800"


# ═══════════════════════════════════════════════════════════════════════════
# Storage tests
# ═══════════════════════════════════════════════════════════════════════════

class TestStorage:
    """Test storage functions with mocked Redis."""

    def test_store_latest_writes_hash(self):
        from services.resource_monitor.main import store_latest

        rc = MagicMock()
        store_latest(rc, "sys:latest:system", {"cpu_percent": "12.5"})

        rc.hset.assert_called_once_with("sys:latest:system", mapping={"cpu_percent": "12.5"})
        rc.expire.assert_called_once_with("sys:latest:system", 120)

    def test_store_timeseries_zadd_and_prune(self):
        from services.resource_monitor.main import store_timeseries

        rc = MagicMock()
        pipe = MagicMock()
        rc.pipeline.return_value = pipe

        store_timeseries(rc, "sys:ts:cpu", 1000.0, 12.5)

        pipe.zadd.assert_called_once_with("sys:ts:cpu", {"1000.000000:12.5": 1000.0})
        pipe.zremrangebyscore.assert_called_once()
        pipe.expire.assert_called_once()
        pipe.execute.assert_called_once()

    def test_daily_rollup_first_sample(self):
        from services.resource_monitor.main import update_daily_rollup

        rc = MagicMock()
        rc.hgetall.return_value = {}  # no existing data

        metrics = {"cpu_percent": "15.0", "ram_percent": "65.0",
                   "cpu_core_max": "22.0", "redis_used_memory_mb": "150.0"}

        update_daily_rollup(rc, metrics)

        rc.hset.assert_called_once()
        call_args = rc.hset.call_args
        key = call_args.args[0]
        mapping = call_args.kwargs["mapping"]

        assert key.startswith("sys:daily:")
        assert mapping["sample_count"] == "1"
        assert mapping["cpu_avg"] == "15.0"
        assert mapping["cpu_max"] == "15.0"


# ═══════════════════════════════════════════════════════════════════════════
# Alert tests
# ═══════════════════════════════════════════════════════════════════════════

class TestAlerts:
    """Test alert threshold checking."""

    def test_cpu_high_alert_fires_after_consecutive_samples(self):
        from services.resource_monitor import main as rm

        rm._cpu_high_streak = 0
        rc = MagicMock()
        rc.set.return_value = True  # cooldown OK

        sys_metrics = {"cpu_percent": "95.0", "cpu_core_count": "4",
                       "cpu_core_max": "95.0", "load_1m": "3.0",
                       "load_5m": "2.0", "load_15m": "1.0"}

        # First 2 samples: streak builds but no alert
        for i in range(2):
            rm.check_alerts(rc, sys_metrics, {}, {})
        assert rm._cpu_high_streak == 2
        rc.xadd.assert_not_called()

        # 3rd sample: alert fires
        rm.check_alerts(rc, sys_metrics, {}, {})
        assert rm._cpu_high_streak == 3
        rc.xadd.assert_called_once()

    def test_cpu_streak_resets_on_normal_cpu(self):
        from services.resource_monitor import main as rm

        rm._cpu_high_streak = 2
        rc = MagicMock()

        rm.check_alerts(rc, {"cpu_percent": "50.0", "cpu_core_count": "4",
                             "cpu_core_max": "50.0"}, {}, {})

        assert rm._cpu_high_streak == 0

    def test_ram_high_alert_fires(self):
        from services.resource_monitor import main as rm

        rc = MagicMock()
        rc.set.return_value = True

        sys_metrics = {"ram_percent": "90.0", "ram_used_mb": "7200",
                       "ram_total_mb": "8000", "ram_available_mb": "800",
                       "swap_percent": "0.0", "cpu_percent": "10.0",
                       "cpu_core_count": "4"}

        rm.check_alerts(rc, sys_metrics, {}, {})

        rc.xadd.assert_called_once()
        msg = rc.xadd.call_args.args[1]["message"]
        assert "RAM" in msg

    def test_core_imbalance_alert_fires(self):
        from services.resource_monitor import main as rm

        rm._core_imbalance_streak = 0
        rc = MagicMock()
        rc.set.return_value = True

        # Simulate 10+ samples of imbalance
        sys_metrics = {
            "cpu_percent": "25.0",
            "cpu_core_count": "4",
            "cpu_core_0": "85.0",
            "cpu_core_1": "5.0",
            "cpu_core_2": "5.0",
            "cpu_core_3": "5.0",
            "cpu_core_max": "85.0",
            "ram_percent": "50.0",
        }

        for _ in range(11):
            rm.check_alerts(rc, sys_metrics, {}, {})

        # Should have fired at least one alert
        xadd_calls = [c for c in rc.xadd.call_args_list
                      if "Imbalance" in str(c)]
        assert len(xadd_calls) >= 1

    def test_service_offline_alert_fires(self):
        from services.resource_monitor import main as rm

        rc = MagicMock()
        rc.set.return_value = True

        old_ts = time.time() - 120  # 120s ago
        services = {"data-gateway": {"pid": 1234, "status": "healthy",
                                      "last_heartbeat": str(old_ts)}}
        sys_metrics = {"cpu_percent": "10.0", "cpu_core_count": "4",
                       "ram_percent": "50.0"}
        redis_metrics = {"hit_rate": "95.0", "used_memory_mb": "100",
                         "maxmemory_mb": "0", "slowlog_count": "0"}

        rm.check_alerts(rc, sys_metrics, services, redis_metrics)

        xadd_calls = [c for c in rc.xadd.call_args_list
                      if "Offline" in str(c)]
        assert len(xadd_calls) >= 1

    def test_alert_cooldown_prevents_duplicate(self):
        from services.resource_monitor.main import _alert_cooldown_ok

        rc = MagicMock()
        rc.set.return_value = None  # key already exists → in cooldown

        result = _alert_cooldown_ok(rc, "cpu_high", 1800)
        assert result is False

    def test_alert_cooldown_allows_first_fire(self):
        from services.resource_monitor.main import _alert_cooldown_ok

        rc = MagicMock()
        rc.set.return_value = True  # key set successfully

        result = _alert_cooldown_ok(rc, "cpu_high", 1800)
        assert result is True


# ═══════════════════════════════════════════════════════════════════════════
# Sysstats command tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSparkline:
    """Test sparkline rendering."""

    def test_flat_values(self):
        from notification.commands.sysstats import _sparkline
        result = _sparkline([5.0, 5.0, 5.0])
        assert len(result) == 3
        assert all(c == result[0] for c in result)

    def test_ascending_values(self):
        from notification.commands.sysstats import _sparkline
        result = _sparkline([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        assert len(result) == 8
        # Should be monotonically non-decreasing
        chars = "▁▂▃▄▅▆▇█"
        indices = [chars.index(c) for c in result]
        assert indices == sorted(indices)

    def test_empty_values(self):
        from notification.commands.sysstats import _sparkline
        assert _sparkline([]) == ""


class TestSysstatsLive:
    """Test the live dashboard view."""

    def test_shows_monitor_not_running_when_no_data(self):
        from notification.commands.sysstats import _build_live_text

        rc = MagicMock()
        rc.hgetall.return_value = {}

        text = _build_live_text(rc)
        assert "not running" in text

    def test_shows_system_metrics(self):
        from notification.commands.sysstats import _build_live_text

        rc = MagicMock()

        def hgetall_side_effect(key):
            if key == "sys:latest:system":
                return {
                    "cpu_percent": "12.5", "cpu_core_count": "4",
                    "cpu_core_0": "12.5", "cpu_core_1": "3.2",
                    "cpu_core_2": "8.1", "cpu_core_3": "0.5",
                    "cpu_core_max": "12.5", "cpu_core_avg": "6.1",
                    "ram_percent": "65.2", "ram_used_mb": "5216",
                    "ram_total_mb": "8192", "swap_percent": "0.0",
                    "disk_percent": "45.3", "disk_used_gb": "45.3",
                    "disk_total_gb": "100.0", "load_1m": "0.8",
                    "load_5m": "1.2", "load_15m": "1.0",
                    "process_count": "142", "uptime_secs": "86400",
                }
            elif key == "sys:latest:redis":
                return {"used_memory_mb": "156.0", "maxmemory_mb": "512.0",
                        "hit_rate": "94.2", "connected_clients": "6",
                        "ops_per_sec": "1240", "total_keys": "2150",
                        "slowlog_count": "0", "evicted_keys": "0",
                        "peak_memory_mb": "162.0", "fragmentation": "1.02",
                        "uptime_secs": "172800"}
            elif key == "service:registry:resource-monitor":
                return {"status": "healthy", "last_heartbeat": str(time.time())}
            return {}

        rc.hgetall.side_effect = hgetall_side_effect
        rc.keys.return_value = []
        rc.zrangebyscore.return_value = []

        text = _build_live_text(rc)

        assert "System Resource Dashboard" in text
        assert "12.5%" in text
        assert "Core 0" in text
        assert "65.2%" in text
        assert "Redis" in text


class TestSysstatsHistory:
    """Test the history view."""

    def test_shows_no_data_when_empty(self):
        from notification.commands.sysstats import _build_history_text

        rc = MagicMock()
        rc.zrangebyscore.return_value = []
        rc.hgetall.return_value = {}

        text = _build_history_text(rc)

        assert "24-Hour Trends" in text
        assert "7-Day Summary" in text

    def test_shows_sparkline_when_data_exists(self):
        from notification.commands.sysstats import _build_history_text

        rc = MagicMock()

        # Mock ZSET data: 24 hourly values
        now = time.time()
        ts_data = [(f"{10.0 + i}", now - (24 - i) * 3600) for i in range(24)]

        def zrangebyscore_side_effect(key, min_score, max_score, withscores=True):
            if "cpu" in key and "core" not in key:
                return ts_data
            return []

        rc.zrangebyscore.side_effect = zrangebyscore_side_effect
        rc.hgetall.return_value = {}

        text = _build_history_text(rc)

        assert "CPU Total" in text
        assert "avg" in text


class TestSysstatsRedis:
    """Test the Redis deep dive view."""

    def test_shows_no_data_when_empty(self):
        from notification.commands.sysstats import _build_redis_text

        rc = MagicMock()
        rc.hgetall.return_value = {}

        text = _build_redis_text(rc)
        assert "No Redis metrics" in text

    def test_shows_redis_details(self):
        from notification.commands.sysstats import _build_redis_text

        rc = MagicMock()
        rc.hgetall.return_value = {
            "used_memory_mb": "156.0", "maxmemory_mb": "512.0",
            "peak_memory_mb": "162.0", "fragmentation": "1.02",
            "evicted_keys": "0", "connected_clients": "6",
            "blocked_clients": "0", "ops_per_sec": "1240",
            "keyspace_hits": "15420", "keyspace_misses": "950",
            "hit_rate": "94.2", "total_keys": "2150",
            "slowlog_count": "0", "uptime_secs": "172800",
        }
        rc.zrangebyscore.return_value = []

        text = _build_redis_text(rc)

        assert "Redis Health" in text
        assert "156 MB" in text
        assert "94.2%" in text
        assert "2150" in text
