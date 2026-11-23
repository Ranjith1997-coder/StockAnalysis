"""Integration-test fixtures (T4).

No network, no threads beyond the two the test itself starts, no real sleeps.
Redis is fakeredis behind the production ``RedisProxy``; time is frozen and
moved explicitly.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
import time_machine

from common.market_calendar import is_trading_day


@pytest.fixture
def clock():
    """Frozen clock: use ``clock.shift(seconds=…)`` to move it forward."""
    with time_machine.travel(datetime.now(), tick=False) as traveller:
        yield traveller


def a_trading_day(start: date = date(2026, 11, 24)) -> date:
    """First trading day on/after ``start`` (holidays are configurable)."""
    day = start
    for _ in range(60):
        if is_trading_day(day):
            return day
        day += timedelta(days=1)
    pytest.skip("no trading day found in the next 60 days")


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)
