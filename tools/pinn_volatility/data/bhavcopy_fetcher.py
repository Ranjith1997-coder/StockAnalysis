"""
NSE F&O Bhavcopy fetcher — end-of-day derivatives settlement data.

IMPORTANT — schema verified live on 2026-08-16, NOT the schema in
.kilo/plans/pinn-volatility-engine.md section 4.1. NSE has since moved to a
new archive domain and a new column schema. The plan's assumed
`archives.nseindia.com/.../fo{DD}{MON}{YYYY}bhav.csv.zip` URL 404s; the
current, working one is below. Column names also changed completely (old
INSTRUMENT/SYMBOL/EXPIRY_DT/STRIKE_PR/SETTLE_PR/CONTRACTS -> new
FinInstrmTp/TckrSymb/XpryDt/StrkPric/SttlmPric/TtlTradgVol). See the mapping
table in conversation / PR description for the full old->new column mapping.

Also verified: SENSEX is NOT in this file (SENSEX options/futures trade on
BSE, not NSE) -- NIFTY and BANKNIFTY only. A BSE Bhavcopy fetcher would be a
separate, later piece of work if SENSEX support is wanted.
"""
from __future__ import annotations

import io
import os
import time
import zipfile
from datetime import date, timedelta

import pandas as pd
import requests

from common.market_calendar import is_trading_day
from lib.logging_util import get_logger
logger = get_logger("pinn")

# Symbols currently supported -- see module docstring re: SENSEX.
SYMBOLS = ["NIFTY", "BANKNIFTY"]

# FinInstrmTp values for the instrument types we care about.
INDEX_OPTION_TYPE = "IDO"
INDEX_FUTURE_TYPE = "IDF"

BHAVCOPY_URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)

# NSE blocks requests without a browser-like User-Agent + Referer -- same
# headers convention as nse/nse_utils.py's default_header/header.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
    "Referer": "https://www.nseindia.com/",
}

DEFAULT_CACHE_DIR = "data/pinn_training/bhavcopy"
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2  # linear backoff: RETRY_DELAY * (attempt + 1)
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 20


def bhavcopy_url(trade_date: date) -> str:
    return BHAVCOPY_URL_TEMPLATE.format(yyyymmdd=trade_date.strftime("%Y%m%d"))


def _cache_path(trade_date: date, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"{trade_date.strftime('%Y%m%d')}.csv")


def fetch_bhavcopy(trade_date: date, cache_dir: str = DEFAULT_CACHE_DIR) -> pd.DataFrame | None:
    """Download + unzip + parse NSE F&O Bhavcopy for one trading day.

    Checks the on-disk cache first (data/pinn_training/bhavcopy/{YYYYMMDD}.csv)
    -- only downloads if not already cached, so a 7-day rolling window only
    ever re-fetches the one new day.

    Returns:
        Full Bhavcopy DataFrame (all F&O instruments, not just our indices --
        use extract_index_rows() to narrow it down), or None if the file
        isn't published yet (weekend/holiday/before EOD/NSE outage after
        retries exhausted).
    """
    cache_file = _cache_path(trade_date, cache_dir)
    if os.path.exists(cache_file):
        logger.debug("[pinn] Bhavcopy cache hit for %s", trade_date)
        return pd.read_csv(cache_file)

    url = bhavcopy_url(trade_date)
    csv_bytes = _download_with_retry(url, trade_date)
    if csv_bytes is None:
        return None

    df = pd.read_csv(io.BytesIO(csv_bytes))

    os.makedirs(cache_dir, exist_ok=True)
    with open(cache_file, "wb") as f:
        f.write(csv_bytes)
    logger.info("[pinn] Bhavcopy fetched and cached for %s (%d rows)", trade_date, len(df))

    return df


def _download_with_retry(url: str, trade_date: date) -> bytes | None:
    """Download+unzip a single Bhavcopy file, with retry on transient errors.

    A 404 is treated as "not yet published" (expected on weekends/holidays/
    before EOD settlement) and returns None immediately without retrying --
    retrying a 404 wastes time since it won't become a 200 within this call.
    """
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=_HEADERS,
                                 timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            if resp.status_code == 404:
                logger.info("[pinn] Bhavcopy not yet published for %s (404)", trade_date)
                return None
            resp.raise_for_status()

            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                names = zf.namelist()
                if len(names) != 1:
                    logger.warning("[pinn] Unexpected Bhavcopy zip contents for %s: %s",
                                   trade_date, names)
                return zf.read(names[0])

        except (requests.RequestException, zipfile.BadZipFile) as e:
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAY_SECONDS * (attempt + 1)
                logger.debug("[pinn] Bhavcopy fetch attempt %d/%d failed for %s: %s -- retrying in %ds",
                             attempt + 1, MAX_RETRIES, trade_date, e, delay)
                time.sleep(delay)
            else:
                logger.error("[pinn] Bhavcopy fetch failed for %s after %d attempts: %s",
                             trade_date, MAX_RETRIES, e)
    return None


def fetch_recent_bhavcopies(
    n_days: int = 7,
    cache_dir: str = DEFAULT_CACHE_DIR,
    end_date: date | None = None,
    symbols: list[str] = SYMBOLS,
    max_days_back: int = 20,
) -> pd.DataFrame:
    """Fetch the last `n_days` TRADING days of Bhavcopy, pre-filtered to
    index option/future rows for `symbols`, concatenated into one DataFrame.

    Walks backward one calendar day at a time from `end_date` (default:
    today). Weekends AND known NSE holidays are skipped without a network
    call, via common.market_calendar.is_trading_day() -- the repo's existing
    shared source of truth for NSE trading days (already used by
    intraday_monitor.py and premarket_report.py; three-layer fallback: live
    NSE holiday API -> XNSE historical calendar -> configs/custom_holidays.json
    overlay). This is an optimization on top of, not a replacement for,
    fetch_bhavcopy's existing 404-tolerant handling -- if is_trading_day()
    ever misses a date (unscheduled exchange closure, holiday list not yet
    published, etc.), a 404 for that day is still handled gracefully and the
    walk just continues.

    Note: is_trading_day() checks NSE's CM (Capital Market/equity) holiday
    segment, not FO specifically -- in practice NSE closes uniformly across
    segments on holidays, so this is equivalent for our purposes, but flagging
    the distinction since this fetcher is F&O-specific.

    Args:
        n_days: number of successfully-fetched trading days to collect.
        max_days_back: safety cap on total calendar days walked, in case
            of an extended NSE outage or an unreasonable n_days -- prevents
            walking back indefinitely.

    Returns:
        Concatenated DataFrame of index option/future rows across however
        many trading days were actually found (may be < n_days if
        max_days_back is hit first -- callers should check how much data
        they actually got rather than assume exactly n_days).
    """
    end_date = end_date or date.today()
    collected: list[pd.DataFrame] = []
    current = end_date
    scanned = 0

    while len(collected) < n_days and scanned < max_days_back:
        if is_trading_day(current):
            df = fetch_bhavcopy(current, cache_dir=cache_dir)
            if df is not None:
                collected.append(extract_index_rows(df, symbols=symbols))
        current -= timedelta(days=1)
        scanned += 1

    if len(collected) < n_days:
        logger.warning("[pinn] Only found %d/%d trading days of Bhavcopy in the last %d calendar days",
                       len(collected), n_days, scanned)

    if not collected:
        return pd.DataFrame()
    return pd.concat(collected, ignore_index=True)


def extract_index_rows(bhavcopy: pd.DataFrame, symbols: list[str] = SYMBOLS) -> pd.DataFrame:
    """Filter a full Bhavcopy DataFrame down to index options + futures rows
    for the given symbols.

    Returns rows with FinInstrmTp in {IDO, IDF} and TckrSymb in `symbols` --
    both options (for IV inversion) and futures (for the forward price F)
    are kept; dataset.py splits them apart per-expiry when building samples.
    """
    mask = bhavcopy["FinInstrmTp"].isin([INDEX_OPTION_TYPE, INDEX_FUTURE_TYPE]) & \
           bhavcopy["TckrSymb"].isin(symbols)
    return bhavcopy[mask].reset_index(drop=True)
