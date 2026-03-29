"""
Market Calendar — Single Source of Truth for NSE Trading Days
=============================================================
Provides holiday-awareness to all entry points in the system:
  - intraday_monitor.py  (fail-fast gatekeeper at startup)
  - premarket_report.py  (holiday warning banner in morning report)

Data-source hierarchy (highest priority first):
  1. NSE holiday-master API (live, CM segment) — primary
     https://www.nseindia.com/api/holiday-master?type=trading
  2. pandas_market_calendars XNSE — historical fallback (no data post-2024)
  3. configs/custom_holidays.json — ad-hoc/state holidays (always merged on top)

Custom-holidays JSON schema (array of ISO-8601 date strings):
    ["2025-10-02", "2026-01-26"]
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CUSTOM_HOLIDAYS_PATH = _REPO_ROOT / "configs" / "custom_holidays.json"

_NSE_HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
_NSE_DATE_FORMAT = "%d-%b-%Y"   # e.g. "15-Jan-2026"
_NSE_CM_SEGMENT = "CM"          # Capital Market segment

_XNSE_CALENDAR_ID = "XNSE"

_DEFAULT_DAYS_AHEAD = 7

# In-process dict cache: year (int) → frozenset[date]
_nse_holiday_cache: Dict[int, frozenset] = {}


# ---------------------------------------------------------------------------
# Layer 1 — Custom JSON overlay
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_custom_holidays() -> frozenset:
    """
    Read configs/custom_holidays.json and return a frozenset of dates.
    Result is cached — call _load_custom_holidays.cache_clear() to force reload.
    """
    if not _CUSTOM_HOLIDAYS_PATH.exists():
        logger.debug("Custom holidays file not found at %s — skipping overlay.", _CUSTOM_HOLIDAYS_PATH)
        return frozenset()

    try:
        raw = _CUSTOM_HOLIDAYS_PATH.read_text(encoding="utf-8").strip()
        if not raw:
            return frozenset()

        entries = json.loads(raw)
        if not isinstance(entries, list):
            logger.warning(
                "custom_holidays.json must contain a JSON array — found %s, ignoring.",
                type(entries).__name__,
            )
            return frozenset()

        parsed: set[date] = set()
        for entry in entries:
            try:
                parsed.add(date.fromisoformat(str(entry).strip()))
            except ValueError:
                logger.warning("Invalid date %r in custom_holidays.json — skipping.", entry)

        logger.info("Loaded %d custom holiday(s) from %s.", len(parsed), _CUSTOM_HOLIDAYS_PATH)
        return frozenset(parsed)

    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read custom_holidays.json (%s) — no custom holidays applied.", exc)
        return frozenset()


# ---------------------------------------------------------------------------
# Layer 2 — NSE holiday-master API  (dict cache per year)
# ---------------------------------------------------------------------------

def _fetch_nse_holidays(year: int) -> frozenset:
    """
    Call the NSE holiday-master API and return trading holidays for `year`.

    Uses nse_urlfetch (from nse.nse_utils) for proper NSE cookie/session handling.
    Parses the CM (Capital Market) segment and filters to the requested year.
    Returns an empty frozenset on any network or parse failure.
    """
    try:
        from nse.nse_utils import nse_urlfetch  # local import — avoids circular deps

        resp = nse_urlfetch(_NSE_HOLIDAY_URL)
        data = resp.json() if hasattr(resp, "json") else {}
        cm_holidays = data.get(_NSE_CM_SEGMENT, [])

        parsed: set[date] = set()
        for entry in cm_holidays:
            try:
                d = datetime.strptime(entry["tradingDate"], _NSE_DATE_FORMAT).date()
                if d.year == year:
                    parsed.add(d)
            except (KeyError, ValueError):
                continue

        logger.info("NSE API: fetched %d holidays for %d.", len(parsed), year)
        return frozenset(parsed)

    except Exception as exc:
        logger.warning("NSE holiday API unavailable for year %d (%s). Using fallback.", year, exc)
        return frozenset()


def _get_nse_holiday_set(year: int) -> frozenset:
    """Return cached NSE API holidays for `year`. Fetches once per year per process."""
    if year not in _nse_holiday_cache:
        _nse_holiday_cache[year] = _fetch_nse_holidays(year)
    return _nse_holiday_cache[year]


def clear_nse_cache(year: Optional[int] = None) -> None:
    """
    Invalidate the in-process NSE holiday cache.

    Args:
        year: If given, clear only that year's entry. If None, clear all years.
    """
    global _nse_holiday_cache
    if year is None:
        _nse_holiday_cache = {}
        logger.debug("NSE holiday cache cleared for all years.")
    else:
        _nse_holiday_cache.pop(year, None)
        logger.debug("NSE holiday cache cleared for year %d.", year)


# ---------------------------------------------------------------------------
# Layer 3 — pandas_market_calendars XNSE  (historical fallback)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_xnse_calendar():
    """Return (and cache) the XNSE MarketCalendar instance, or None on failure."""
    try:
        import pandas_market_calendars as mcal
        return mcal.get_calendar(_XNSE_CALENDAR_ID)
    except Exception as exc:
        logger.warning("Could not load XNSE calendar (%s).", exc)
        return None


def _get_xnse_holiday_set(year: int) -> frozenset:
    """
    Return NSE weekday holidays for `year` from pandas_market_calendars (XNSE).
    Only used as a fallback when the NSE API returns nothing for the year.
    """
    cal = _get_xnse_calendar()
    if cal is None:
        return frozenset()

    try:
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        schedule = cal.schedule(start_date=start.isoformat(), end_date=end.isoformat())

        if schedule.empty:
            logger.info("XNSE calendar: no schedule data for %d.", year)
            return frozenset()

        # Build the set of all weekdays in the year
        all_weekdays: set[date] = set()
        current = start
        while current <= end:
            if current.weekday() < 5:
                all_weekdays.add(current)
            current += timedelta(days=1)

        trading_days: set[date] = {ts.date() for ts in schedule.index}
        holidays = frozenset(all_weekdays - trading_days)
        logger.info("XNSE calendar: %d holidays for %d.", len(holidays), year)
        return holidays

    except Exception as exc:
        logger.warning("XNSE schedule lookup failed for %d (%s).", year, exc)
        return frozenset()


# ---------------------------------------------------------------------------
# Merged holiday set — combines all layers
# ---------------------------------------------------------------------------

def _get_holiday_set(year: int) -> frozenset:
    """
    Return the merged set of NSE holidays for `year`.

    Priority: NSE API | XNSE fallback (when NSE returns nothing) | custom overlay.
    """
    nse = _get_nse_holiday_set(year)
    if nse:
        base = nse
    else:
        # NSE API returned nothing — try the XNSE historical calendar
        base = _get_xnse_holiday_set(year)
        if base:
            logger.info("Using XNSE fallback for year %d (NSE API returned no data).", year)

    custom = _load_custom_holidays()
    # Merge only the custom dates that belong to this year
    return base | frozenset(d for d in custom if d.year == year)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_trading_day(check_date: Optional[date] = None) -> bool:
    """
    Return True if `check_date` is a valid NSE trading day.

    A day is NOT a trading day if:
      - It falls on a weekend, OR
      - It appears in the NSE holiday-master API (CM segment), OR
      - It is listed in configs/custom_holidays.json

    Args:
        check_date: The date to evaluate. Defaults to today (local wall-clock).

    Returns:
        bool: True → market is open; False → market is closed.
    """
    if check_date is None:
        check_date = datetime.now().date()

    # Fast-path: weekends are never trading days
    if check_date.weekday() >= 5:  # 5=Saturday, 6=Sunday
        logger.debug("is_trading_day(%s) → False (weekend)", check_date)
        return False

    holiday_set = _get_holiday_set(check_date.year)
    if check_date in holiday_set:
        logger.info("is_trading_day(%s) → False (NSE holiday)", check_date)
        return False

    logger.debug("is_trading_day(%s) → True", check_date)
    return True


def get_upcoming_holidays(days_ahead: int = _DEFAULT_DAYS_AHEAD) -> List[date]:
    """
    Return a sorted list of NSE market-holiday dates within the next `days_ahead` days.

    Weekends are excluded — only *additional* holiday closures are returned.

    Args:
        days_ahead: How many calendar days ahead to scan (default: 7).

    Returns:
        List[date]: Sorted list of upcoming holiday dates. Empty if none found.
    """
    if days_ahead < 1:
        return []

    today = datetime.now().date()
    scan_start = today + timedelta(days=1)
    scan_end = today + timedelta(days=days_ahead)

    # Gather holiday sets for every year that falls in the scan window
    years = set(range(scan_start.year, scan_end.year + 1))
    all_holidays: set[date] = set()
    for yr in years:
        all_holidays |= _get_holiday_set(yr)

    holidays: List[date] = []
    current = scan_start
    while current <= scan_end:
        # Only include weekday holidays (weekends are structural non-trading days)
        if current.weekday() < 5 and current in all_holidays:
            holidays.append(current)
        current += timedelta(days=1)

    holidays.sort()
    if holidays:
        logger.info(
            "Upcoming holidays in next %d days: %s",
            days_ahead,
            [d.isoformat() for d in holidays],
        )
    return holidays
