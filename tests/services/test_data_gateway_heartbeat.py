"""Liveness heartbeat for data-gateway must outlast its 310 s cycle."""
from unittest.mock import MagicMock

import services.data_gateway.main as dg


def test_touch_heartbeat_does_not_clobber_cycle_fields():
    redis = MagicMock()
    dg._touch_heartbeat(redis)

    key, kwargs = redis.hset.call_args[0][0], redis.hset.call_args[1]
    assert key == "service:registry:data-gateway"
    mapping = kwargs["mapping"]
    assert "last_heartbeat" in mapping and "pid" in mapping
    assert "cycle_count" not in mapping      # written by the cycle loop
    assert "status" not in mapping

    redis.expire.assert_called_once_with("service:registry:data-gateway", dg.HEARTBEAT_TTL)


def test_heartbeat_has_multiple_intervals_of_headroom():
    # The thread (not the 310s cycle) is the liveness source, so the TTL only
    # needs several intervals of margin — a dead thread should expire visibly.
    assert dg.HEARTBEAT_INTERVAL * 2 <= dg.HEARTBEAT_TTL
