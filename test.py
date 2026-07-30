"""
Quick test: generate premarket reports and send to Telegram, or inspect futures data.

Usage:
  python test.py                # Send both premarket reports
  python test.py --global       # Only global cues & FII/DII
  python test.py --preopen      # Only NSE pre-open session
  python test.py --futures      # Print futures data (NSE + Zerodha) for all stocks
  python test.py --futures --stock RELIANCE   # Single stock
  python test.py --futures --mode intraday    # Use intraday (5min) instead of positional (daily)
  python test.py --holidays                  # Test market_calendar: is_trading_day + upcoming holidays
  python test.py --holidays --date 2026-04-14  # Check a specific date
  python test.py --holidays --days 14          # Scan 14 days ahead instead of 7
  python test.py --holidays --inject 2026-04-14  # Inject a custom holiday and verify detection
"""
import sys, os
sys.path.append(os.getcwd())

import argparse
from dotenv import load_dotenv
load_dotenv()

from notification.Notification import TELEGRAM_NOTIFICATIONS
from premarket.premarket_report import run_global_cues_report, run_preopen_report

# Enable sending (production flag must be on)
TELEGRAM_NOTIFICATIONS.is_production = True
# Send to positional chat (same as real premarket flow)
TELEGRAM_NOTIFICATIONS.is_intraday = False


# ──────────────────────────────────────────────────────────────────────────────
# Futures data inspection
# ──────────────────────────────────────────────────────────────────────────────

def _print_section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print('─' * 60)


def _print_df(label: str, df):
    import pandas as pd
    if df is None:
        print(f"  {label}: None")
        return
    if isinstance(df, pd.DataFrame):
        if df.empty:
            print(f"  {label}: empty DataFrame")
            return
        print(f"  {label}: {df.shape[0]} rows × {df.shape[1]} cols")
        print(f"  Columns : {df.columns.tolist()}")
        print(f"  Index   : {df.index.name or 'default'}")
        print(df.to_string(max_rows=5, max_cols=None))
    else:
        print(f"  {label}: {df}")


def run_futures_test(stock_filter: str | None, mode: str):
    import common.shared as shared
    import common.constants as constant
    from intraday.intraday_monitor import (
        create_stock_and_index_objects,
        fetch_price_data,
        update_zerodha_option_chain,
    )
    from zerodha.zerodha_connect import KiteConnect

    # ── 1. Mode setup ──
    app_mode = shared.Mode.INTRADAY if mode == "intraday" else shared.Mode.POSITIONAL
    shared.app_ctx.mode = app_mode
    analysis_mode = mode  # "positional" or "intraday"
    print(f"\n[test] Mode: {app_mode.name}")

    # ── 2. Create stock/index objects (fetches prev-day OHLCV) ──
    create_stock_and_index_objects(stockName=stock_filter)
    stock_objs = list(shared.app_ctx.stock_token_obj_dict.values())
    index_objs = list(shared.app_ctx.index_token_obj_dict.values())

    if not stock_objs and not index_objs:
        print("[test] No stocks/indices found. Check stock_filter or JSON config.")
        return

    # ── 3. Fetch price data (needed for underlying_price lookups) ──
    print("[test] Fetching price data via yfinance...")
    fetch_price_data(stock_objs, index_objs)

    # ── 5. Zerodha futures (ENABLE_ZERODHA_DERIVATIVES + ENABLE_ZERODHA_API path) ──
    zerodha_api_enabled = os.getenv(constant.ENV_ENABLE_ZERODHA_API, "0") == "1"
    if zerodha_api_enabled:
        enc_token = os.getenv(constant.ENV_ZERODHA_ENC_TOKEN)
        kite = KiteConnect(
            constant.DUMMY_API_KEY_ZERODHA,
            root="https://kite.zerodha.com/",
            enctoken=enc_token,
        )
        shared.app_ctx.zd_kc = kite
        print("[test] Fetching Zerodha instrument tokens (option_chain + futures_mdata)...")
        update_zerodha_option_chain(stockName=stock_filter)

        for stock in stock_objs[:3]:
            _print_section(f"Zerodha zerodha_ctx — {stock.stock_symbol}")

            # Metadata (instrument tokens, strikes, expiries)
            _print_df("futures_mdata [current]", stock.zerodha_ctx["futures_mdata"]["current"])
            _print_df("futures_mdata [next]   ", stock.zerodha_ctx["futures_mdata"]["next"])
            _print_df("option_chain  [current] (first 5 rows)", stock.zerodha_ctx["option_chain"]["current"])

            # Fetch actual OHLC + OI data from Zerodha historical_data API
            print(f"\n  [test] Calling get_futures_data_for_stock(mode={analysis_mode})...")
            try:
                stock.get_futures_data_for_stock(mode=analysis_mode)
                _print_df("futures_data [current]", stock.zerodha_ctx["futures_data"]["current"])
                _print_df("futures_data [next]   ", stock.zerodha_ctx["futures_data"]["next"])
            except Exception as e:
                print(f"  ERROR fetching Zerodha futures data for {stock.stock_symbol}: {e}")

        for index in index_objs[:2]:
            _print_section(f"Zerodha zerodha_ctx — INDEX {index.stock_symbol}")
            _print_df("futures_mdata [current]", index.zerodha_ctx["futures_mdata"]["current"])
            _print_df("option_chain  [current] (first 5 rows)", index.zerodha_ctx["option_chain"]["current"])
            try:
                index.get_futures_data_for_stock(mode=analysis_mode)
                _print_df("futures_data [current]", index.zerodha_ctx["futures_data"]["current"])
            except Exception as e:
                print(f"  ERROR fetching Zerodha futures data for {index.stock_symbol}: {e}")
    else:
        print("[test] Zerodha API disabled (ENABLE_ZERODHA_API=0). Skipping Zerodha path.")

    print("\n[test] Done.")


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket option chain live test
# ──────────────────────────────────────────────────────────────────────────────

def run_websocket_options_test(index_filter: str | None, duration: int):
    """
    Connect to Zerodha WebSocket, subscribe to NIFTY/BANKNIFTY options,
    and display live option tick data as it arrives.

    Usage:
      python test.py --ws-options                         # NIFTY options for 60s
      python test.py --ws-options --index BANKNIFTY       # BANKNIFTY options
      python test.py --ws-options --duration 120          # Run for 120 seconds
    """
    import time
    import common.shared as shared
    import common.constants as constant
    from common.token_registry import TokenRegistry, TokenType
    from intraday.intraday_monitor import (
        create_stock_and_index_objects,
        update_zerodha_option_chain,
        _register_base_tokens,
    )
    from zerodha.zerodha_connect import KiteConnect
    from zerodha.zerodha_analysis import ZerodhaTickerManager
    from urllib.parse import quote

    # ── 1. Setup mode and registry ──
    shared.app_ctx.mode = shared.Mode.INTRADAY
    shared.app_ctx.token_registry = TokenRegistry()

    print("[ws-options] Creating stock and index objects...")
    create_stock_and_index_objects(indexName=index_filter)
    _register_base_tokens()

    index_objs = list(shared.app_ctx.index_token_obj_dict.values())
    target_indices = [i for i in index_objs if i.stock_symbol in constant.LIVE_OPTIONS_INDICES]

    if not target_indices:
        print("[ws-options] No tradable indices found. Check your config.")
        return

    print(f"[ws-options] Target indices: {[i.stock_symbol for i in target_indices]}")

    # ── 2. Zerodha API setup ──
    enc_token_raw = os.getenv(constant.ENV_ZERODHA_ENC_TOKEN)
    if not enc_token_raw:
        print("[ws-options] ERROR: ZERODHA_ENC_TOKEN not set in .env")
        return

    kite = KiteConnect(
        constant.DUMMY_API_KEY_ZERODHA,
        root="https://kite.zerodha.com/",
        enctoken=enc_token_raw,
    )
    shared.app_ctx.zd_kc = kite

    # ── 3. Fetch instrument tokens and register in registry ──
    print("[ws-options] Fetching instrument list and registering option tokens...")
    update_zerodha_option_chain(indexName=index_filter)

    registry = shared.app_ctx.token_registry
    stats = registry.get_stats()
    print(f"[ws-options] Registry stats: {stats}")

    for idx in target_indices:
        opt_tokens = registry.get_tokens_by_type(idx.stock_symbol, TokenType.OPTION)
        fut_tokens = registry.get_tokens_by_type(idx.stock_symbol, TokenType.FUTURE)
        gap = registry.get_strike_gap(idx.stock_symbol)
        # Show selected expiry from zerodha_ctx
        current_chain = idx.zerodha_ctx.get("option_chain", {}).get("current")
        expiry_info = ""
        if current_chain is not None and not current_chain.empty:
            expiry_info = f", expiry={current_chain['expiry'].iloc[0]}"
        print(f"  {idx.stock_symbol}: {len(opt_tokens)} option tokens, {len(fut_tokens)} future tokens, strike gap={gap}{expiry_info}")

    # ── 4. Connect WebSocket ──
    username = os.getenv(constant.ENV_ZERODHA_USERNAME)
    password = os.getenv(constant.ENV_ZERODHA_PASSWORD)
    enc_token_for_ws = quote(enc_token_raw, safe="")

    print(f"\n[ws-options] Connecting WebSocket as {username}...")
    manager = ZerodhaTickerManager(username, password, enc_token_for_ws)
    shared.app_ctx.zd_ticker_manager = manager

    if not manager.connect():
        print("[ws-options] ERROR: Failed to connect to WebSocket")
        return

    print("[ws-options] WebSocket connected!")

    # ── 5. Wait for initial index tick to get spot price ──
    print("[ws-options] Waiting for initial index ticks (3s)...")
    time.sleep(3)

    # ── 6. Subscribe to option tokens ──
    import math
    for idx in target_indices:
        spot = idx.zerodha_data.get("last_price") or idx.ltp
        if not spot or not math.isfinite(spot) or spot <= 0:
            print(f"[ws-options] WARNING: No spot price for {idx.stock_symbol} (got {spot}), trying LTP from prevDay...")
            spot = None
            if idx.prevDayOHLCV:
                spot = idx.prevDayOHLCV.get("CLOSE", 0)
        if not spot or not math.isfinite(spot) or spot <= 0:
            print(f"[ws-options] SKIP {idx.stock_symbol} — no valid spot price available")
            continue

        print(f"[ws-options] Subscribing options for {idx.stock_symbol} at spot={spot:.2f}")
        manager.subscribe_options_for_symbol(idx.stock_symbol, spot)

        # Show subscription breakdown
        from common.token_registry import OptionZone
        for zone in OptionZone:
            zone_tokens = registry.get_option_tokens_by_zone(idx.stock_symbol, zone)
            if zone_tokens:
                print(f"  {zone.value}: {len(zone_tokens)} tokens")

    # ── 7. Live display loop ──
    print(f"\n{'=' * 80}")
    print(f"  LIVE OPTION DATA — Refreshing every 3s for {duration}s")
    print(f"  Press Ctrl+C to stop early")
    print(f"  Note: OI data requires market hours (Mon-Fri 9:15-15:30 IST)")
    print(f"{'=' * 80}\n")

    start_time = time.time()
    iteration = 0

    try:
        while time.time() - start_time < duration:
            iteration += 1
            elapsed = int(time.time() - start_time)

            for idx in target_indices:
                spot = idx.zerodha_data.get("last_price", 0) or 0

                # Force aggregate recomputation so display reflects all ticks received so far
                display_spot_for_agg = spot if (spot and math.isfinite(spot) and spot > 0) else (idx.prevDayOHLCV.get("CLOSE", 0) if idx.prevDayOHLCV else 0)
                if display_spot_for_agg > 0:
                    idx.recompute_options_aggregate(spot_price=display_spot_for_agg)
                agg = idx.options_aggregate

                # Use prevDay close as fallback for display if live spot not yet available
                display_spot = spot if (spot and math.isfinite(spot) and spot > 0) else (idx.prevDayOHLCV.get("CLOSE", 0) if idx.prevDayOHLCV else 0)

                # Header
                print(f"\n[{elapsed}s] ━━━ {idx.stock_symbol} ━━━ Spot: {display_spot:.2f}" + (" (live)" if spot > 0 else " (prevDay)"))

                # Aggregates
                if agg["last_updated"] > 0:
                    pcr = agg["live_pcr"]
                    straddle = agg["atm_straddle_premium"]
                    atm = agg["atm_strike"]
                    ce_oi = agg["total_ce_oi"]
                    pe_oi = agg["total_pe_oi"]
                    max_ce = agg["max_oi_ce_strike"]
                    max_pe = agg["max_oi_pe_strike"]
                    ce_oi_chg = agg["net_ce_oi_change"]
                    pe_oi_chg = agg["net_pe_oi_change"]

                    print(f"  PCR: {pcr:.3f} | ATM: {atm} | Straddle: {straddle:.2f}")
                    print(f"  CE OI: {ce_oi:,} (chg: {ce_oi_chg:+,}) | Max CE OI @ {max_ce}")
                    print(f"  PE OI: {pe_oi:,} (chg: {pe_oi_chg:+,}) | Max PE OI @ {max_pe}")
                else:
                    print("  (waiting for option ticks...)")

                # Per-strike data (show strikes near ATM)
                if idx.options_live:
                    strikes = sorted(idx.options_live.keys())
                    ref_price = display_spot if display_spot > 0 else (agg.get("atm_strike") or 0)
                    # Filter to strikes within 2% of reference price
                    near_strikes = [s for s in strikes if abs(s - ref_price) / ref_price <= 0.02] if ref_price > 0 else strikes[:10]

                    if near_strikes:
                        print(f"\n  {'Strike':>10} | {'CE LTP':>8} {'CE OI':>10} {'CE Vol':>8} | {'PE LTP':>8} {'PE OI':>10} {'PE Vol':>8}")
                        print(f"  {'─' * 10}-+-{'─' * 28}-+-{'─' * 28}")

                        for strike in near_strikes:
                            data = idx.options_live[strike]
                            ce = data.get("CE", {})
                            pe = data.get("PE", {})

                            ce_ltp = ce.get("ltp", 0)
                            ce_oi_val = ce.get("oi", 0)
                            ce_vol = ce.get("volume", 0)
                            pe_ltp = pe.get("ltp", 0)
                            pe_oi_val = pe.get("oi", 0)
                            pe_vol = pe.get("volume", 0)

                            # Highlight ATM strike
                            marker = " ◄ ATM" if agg.get("atm_strike") == strike else ""
                            print(f"  {strike:>10.0f} | {ce_ltp:>8.2f} {ce_oi_val:>10,} {ce_vol:>8,} | {pe_ltp:>8.2f} {pe_oi_val:>10,} {pe_vol:>8,}{marker}")

                # Futures data
                if idx.futures_live:
                    print(f"\n  Futures:")
                    for exp_key, fdata in idx.futures_live.items():
                        f_ltp = fdata.get("ltp", 0)
                        f_oi = fdata.get("oi", 0)
                        f_vol = fdata.get("volume", 0)
                        f_chg = fdata.get("change", 0)
                        print(f"    {exp_key}: LTP={f_ltp:.2f} OI={f_oi:,} Vol={f_vol:,} Chg={f_chg:+.2f}%")

            time.sleep(3)

    except KeyboardInterrupt:
        print("\n\n[ws-options] Interrupted by user.")

    # ── 8. Cleanup ──
    print("\n[ws-options] Closing WebSocket connection...")
    manager.close_connection()
    print("[ws-options] Done.")

# ──────────────────────────────────────────────────────────────────────────────
# Market Holiday feature test
# ──────────────────────────────────────────────────────────────────────────────

def print_nse_calendar_holidays(year: int | None = None):
    """
    Print all NSE holidays (weekdays only) for the given year from the XNSE
    calendar (or the live NSE API when the library has no data for that year),
    and list what is currently in configs/custom_holidays.json.

    Usage:
      python test.py --nse-holidays            # current year
      python test.py --nse-holidays --year 2027
    """
    import pandas as pd
    import pandas_market_calendars as mcal
    import json
    from datetime import date, datetime
    from common.market_calendar import _CUSTOM_HOLIDAYS_PATH

    if year is None:
        year = date.today().year

    SEP = "─" * 60
    print(f"\n{'═' * 60}")
    print(f"  NSE Calendar Holidays — {year}")
    print(f"{'═' * 60}")

    # ── 1. Try XNSE calendar (pandas_market_calendars) ──
    print(f"\n{SEP}")
    print(f"  Source: XNSE (pandas_market_calendars) — {year}")
    print(SEP)

    cal = mcal.get_calendar("XNSE")
    schedule = cal.schedule(
        start_date=f"{year}-01-01",
        end_date=f"{year}-12-31",
    )
    trading_days = set(schedule.index.date)
    all_bdays = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="B")
    xnse_holidays = sorted(d for d in all_bdays.date if d not in trading_days)

    source_label = "XNSE calendar"

    if not xnse_holidays:
        print(f"  ⚠️  XNSE library has no holiday data for {year}.")
        print(f"  Falling back to live NSE holiday API...")

        # ── 2. Fallback: live NSE holiday-master API ──
        try:
            from nse.nse_utils import nse_urlfetch
            r = nse_urlfetch("https://www.nseindia.com/api/holiday-master?type=trading")
            api_data = r.json()

            # CM = Capital Market (equity) segment
            cm_holidays = api_data.get("CM", [])
            xnse_holidays = []
            for entry in cm_holidays:
                date_str = entry.get("tradingDate", "")
                try:
                    d = datetime.strptime(date_str, "%d-%b-%Y").date()
                    if d.year == year:
                        xnse_holidays.append((d, entry.get("description", "")))
                except ValueError:
                    pass
            xnse_holidays.sort(key=lambda x: x[0])
            source_label = "NSE holiday-master API (live)"

        except Exception as exc:
            print(f"  ❌ NSE API fallback also failed: {exc}")
            xnse_holidays = []

    else:
        # Wrap plain dates in tuples to unify handling below
        xnse_holidays = [(d, "") for d in xnse_holidays]

    if xnse_holidays:
        print(f"  Source: {source_label}")
        print()
        print(f"  {'#':<4} {'Date':<14} {'Day':<12} {'Description'}")
        print(f"  {'─'*3} {'─'*13} {'─'*11} {'─'*35}")
        for i, item in enumerate(xnse_holidays, 1):
            d, desc = item
            marker = "  ← weekend (informational)" if d.weekday() >= 5 else ""
            print(f"  {i:<4} {str(d):<14} {d.strftime('%A'):<12} {desc}{marker}")
        weekday_holidays = [d for d, _ in xnse_holidays if d.weekday() < 5]
        print(f"\n  Total: {len(xnse_holidays)} entries "
              f"({len(weekday_holidays)} weekday market closures)")

        # ── Suggest adding missing ones to custom_holidays.json ──
        try:
            existing_custom: list = []
            if _CUSTOM_HOLIDAYS_PATH.exists():
                raw = _CUSTOM_HOLIDAYS_PATH.read_text(encoding="utf-8").strip()
                if raw and raw != "[]":
                    existing_custom = json.loads(raw)
            existing_custom_set = {date.fromisoformat(s) for s in existing_custom if s}

            # Only weekday holidays not already in XNSE (i.e., API-sourced ones
            # that the library doesn't know about yet)
            if source_label.startswith("NSE"):
                missable = [
                    (d, desc) for d, desc in xnse_holidays
                    if d.weekday() < 5 and d not in existing_custom_set
                ]
                if missable:
                    print(f"\n  💡 Tip: add these to custom_holidays.json so the system")
                    print(f"     recognises them (XNSE library has no 2026 data yet):")
                    print(f'  [')
                    for d, desc in missable:
                        print(f'    "{d.isoformat()}",  // {desc}')
                    print(f'  ]')
        except Exception:
            pass
    else:
        print("  No holiday data available for this year.")

    # ── 3. Custom holidays file ──
    print(f"\n{SEP}")
    print(f"  configs/custom_holidays.json (your ad-hoc overrides)")
    print(SEP)

    if not _CUSTOM_HOLIDAYS_PATH.exists():
        print("  File does not exist yet.")
    else:
        raw = _CUSTOM_HOLIDAYS_PATH.read_text(encoding="utf-8").strip()
        if not raw or raw == "[]":
            print("  File is empty — no custom holidays defined.")
        else:
            try:
                entries = json.loads(raw)
                api_dates = {d for d, _ in xnse_holidays} if xnse_holidays else set()
                print(f"  {len(entries)} custom holiday(s):")
                for entry in sorted(entries):
                    try:
                        d = date.fromisoformat(str(entry))
                        note = " ← already in NSE API list" if d in api_dates else ""
                        print(f"    {d}  ({d.strftime('%A')}){note}")
                    except ValueError:
                        print(f"    {entry}  (⚠️ invalid date format)")
            except json.JSONDecodeError as e:
                print(f"  ⚠️  JSON parse error: {e}")

    print(f"\n{'═' * 60}\n")


def run_holidays_test(
    check_date_str: str | None = None,
    days_ahead: int = 7,
    inject_date_str: str | None = None,
):
    """
    Comprehensive test for common/market_calendar.py.

    Data source hierarchy:
      NSE holiday-master API (primary) → XNSE pandas_market_calendars (fallback)
      → configs/custom_holidays.json (always merged as override layer)

    Tests:
      1. Weekend fast-path
      2. Known NSE holidays from the live API (CM segment, 2026)
      3. Known NSE regular trading days
      4. User-supplied date via --date (optional)
      5. get_upcoming_holidays() return type and count
      6. Custom holiday injection (writes + restores configs/custom_holidays.json)
      7. NSE API direct source verification (_get_nse_holiday_set, clear_nse_cache)
      8. Edge cases (days_ahead=0, days_ahead=1, is_trading_day with no crash)
    """
    import json
    from datetime import date, timedelta
    from pathlib import Path

    # Import AFTER sys.path is set
    from common.market_calendar import (
        is_trading_day,
        get_upcoming_holidays,
        _load_custom_holidays,
        _CUSTOM_HOLIDAYS_PATH,
        _get_nse_holiday_set,  # direct NSE API cache
        clear_nse_cache,        # cache invalidation
    )

    SEP = "─" * 60

    def ok(passed: bool) -> str:
        return "✅ PASS" if passed else "❌ FAIL"

    print(f"\n{'═' * 60}")
    print("  Market Calendar — Holiday Feature Test")
    print(f"{'═' * 60}")

    # ── 1. Weekend fast-path ──────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  1. Weekend fast-path")
    print(SEP)

    # Find the nearest upcoming Saturday and Sunday
    today = date.today()
    days_to_saturday = (5 - today.weekday()) % 7 or 7
    saturday = today + timedelta(days=days_to_saturday)
    sunday = saturday + timedelta(days=1)

    sat_result = is_trading_day(saturday)
    sun_result = is_trading_day(sunday)
    print(f"  {saturday} (Saturday) → is_trading_day = {sat_result}  {ok(not sat_result)}")
    print(f"  {sunday}   (Sunday)   → is_trading_day = {sun_result}  {ok(not sun_result)}")

    # ── 2. Known NSE holidays ────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  2. Known NSE holidays (NSE API — primary source)")
    print(SEP)

    # Confirmed weekday closures from NSE holiday-master API (CM segment, 2026).
    # All should be non-trading days (is_trading_day → False).
    known_holidays = {
        date(2026, 1, 15): "Municipal Corporation Election - Maharashtra",
        date(2026, 1, 26): "Republic Day",
        date(2026, 3, 3):  "Holi",
        date(2026, 3, 26): "Shri Ram Navami",
        date(2026, 3, 31): "Shri Mahavir Jayanti",
        date(2026, 4, 3):  "Good Friday",
        date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
        date(2026, 10, 2): "Mahatma Gandhi Jayanti",
        date(2026, 12, 25): "Christmas",
    }

    for d, name in known_holidays.items():
        result = is_trading_day(d)
        # Weekdays that fall on public holidays → should be False
        expected_closed = d.weekday() < 5  # only meaningful for weekdays
        status = ok(not result) if expected_closed else ok(True)
        print(f"  {d} ({d.strftime('%A'):<9}) [{name}] → {result}  {status}")

    # ── 3. Known trading days ────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  3. Known NSE trading days")
    print(SEP)

    # Pick a handful of regular weekdays that are not public holidays
    known_trading_days = [
        date(2026, 1, 2),   # Friday after New Year
        date(2026, 2, 2),   # Monday, no holiday
        date(2026, 3, 9),   # Monday, no holiday
        date(2026, 4, 6),   # Monday after Good Friday window
        date(2026, 9, 1),   # Tuesday, no holiday
    ]

    for d in known_trading_days:
        result = is_trading_day(d)
        print(f"  {d} ({d.strftime('%A'):<9}) → is_trading_day = {result}  {ok(result)}")

    # ── 4. Specific date supplied via --date ─────────────────────────────────
    if check_date_str:
        print(f"\n{SEP}")
        print(f"  4. User-supplied date: {check_date_str}")
        print(SEP)
        try:
            check_date = date.fromisoformat(check_date_str)
            result = is_trading_day(check_date)
            day_name = check_date.strftime("%A")
            print(f"  {check_date} ({day_name}) → is_trading_day = {result}")
        except ValueError:
            print(f"  ERROR: invalid date format '{check_date_str}'. Use YYYY-MM-DD.")

    # ── 5. get_upcoming_holidays() ───────────────────────────────────────────
    print(f"\n{SEP}")
    print(f"  5. get_upcoming_holidays(days_ahead={days_ahead})")
    print(SEP)

    holidays = get_upcoming_holidays(days_ahead=days_ahead)
    scan_start = today + timedelta(days=1)
    scan_end = today + timedelta(days=days_ahead)
    print(f"  Scan window: {scan_start} → {scan_end}")
    if holidays:
        print(f"  Found {len(holidays)} holiday(s):")
        for h in holidays:
            print(f"    📅 {h} ({h.strftime('%A')})")
    else:
        print("  No holidays found in the scan window.")
    print(f"  {ok(isinstance(holidays, list))} Return type is list")

    # ── 6. Custom holiday injection ──────────────────────────────────────────
    print(f"\n{SEP}")
    print("  6. Custom holiday injection test")
    print(SEP)

    # Read current file content so we can restore it afterwards
    original_content = ""
    if _CUSTOM_HOLIDAYS_PATH.exists():
        original_content = _CUSTOM_HOLIDAYS_PATH.read_text(encoding="utf-8")

    # Pick a weekday that is not already a holiday — inject it and test
    inject_date_str_used = inject_date_str
    if not inject_date_str_used:
        # Default: use 30 days from now (likely a regular weekday trading day)
        inject_candidate = today + timedelta(days=30)
        # Advance to Monday if it lands on weekend
        while inject_candidate.weekday() >= 5:
            inject_candidate += timedelta(days=1)
        inject_date_str_used = inject_candidate.isoformat()

    try:
        inject_date = date.fromisoformat(inject_date_str_used)

        print(f"  Injecting {inject_date} into custom_holidays.json...")

        # Build list including any existing valid dates + the new one
        existing: list = []
        if original_content.strip() and original_content.strip() != "[]":
            try:
                existing = json.loads(original_content)
            except json.JSONDecodeError:
                existing = []

        test_list = list(set(existing + [inject_date_str_used]))
        _CUSTOM_HOLIDAYS_PATH.write_text(
            json.dumps(sorted(test_list), indent=2), encoding="utf-8"
        )

        # Clear lru_cache so the new file is picked up
        _load_custom_holidays.cache_clear()

        result_after_inject = is_trading_day(inject_date)
        print(f"  After injection: is_trading_day({inject_date}) = {result_after_inject}  {ok(not result_after_inject)}")

        # Also verify it appears in get_upcoming_holidays if within 90 days
        days_to_inject = (inject_date - today).days
        if 1 <= days_to_inject <= 90:
            upcoming = get_upcoming_holidays(days_ahead=days_to_inject + 1)
            detected = inject_date in upcoming
            print(f"  Appears in get_upcoming_holidays(days_ahead={days_to_inject + 1}) = {detected}  {ok(detected)}")

    except ValueError:
        print(f"  ERROR: invalid inject date '{inject_date_str_used}'. Use YYYY-MM-DD.")

    finally:
        # Always restore original content
        _CUSTOM_HOLIDAYS_PATH.write_text(original_content, encoding="utf-8")
        _load_custom_holidays.cache_clear()
        print(f"  Restored configs/custom_holidays.json to original content.")

    # ── 7. NSE API direct source verification ───────────────────────────────
    print(f"\n{SEP}")
    print("  7. NSE API direct source verification")
    print(SEP)

    clear_nse_cache(2026)  # force fresh fetch, ignore any cached value
    nse_set = _get_nse_holiday_set(2026)
    print(f"  NSE API returned {len(nse_set)} holidays for 2026  {ok(len(nse_set) > 0)}")
    for chk_date, chk_name in [
        (date(2026, 1, 26), "Republic Day"),
        (date(2026, 4, 3),  "Good Friday"),
        (date(2026, 12, 25), "Christmas"),
    ]:
        in_set = chk_date in nse_set
        print(f"  {chk_date} [{chk_name}] in NSE set: {in_set}  {ok(in_set)}")

    # ── 8. Edge cases ─────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  8. Edge cases")
    print(SEP)

    # days_ahead=0 should return empty list
    result_zero = get_upcoming_holidays(days_ahead=0)
    print(f"  get_upcoming_holidays(days_ahead=0) = {result_zero}  {ok(result_zero == [])}")

    # days_ahead=1 should return at most 1 day
    result_one = get_upcoming_holidays(days_ahead=1)
    print(f"  get_upcoming_holidays(days_ahead=1) count = {len(result_one)} (≤1)  {ok(len(result_one) <= 1)}")

    # is_trading_day with explicit date object
    result_explicit = is_trading_day(today)
    print(f"  is_trading_day(today={today}) = {result_explicit}  ✅ (no crash)")

    print(f"\n{'═' * 60}")
    print("  Holiday feature test complete.")
    print(f"{'═' * 60}\n")
    print("  Data sources: NSE holiday-master API (primary) → XNSE pandas_market_calendars (fallback) → configs/custom_holidays.json (overlay)")


def run_intelligence_test():
    """
    Unit tests for the intelligence module: Signal, SignalBus, SignalCorrelator.

    Usage:
      python test.py --intelligence
    """
    import time as _time
    from intelligence.signal import Signal, Direction, Layer, SignalStrength, weight_to_strength
    from intelligence.signal_bus import SignalBus
    from intelligence.correlator import SignalCorrelator, Confluence

    SEP = "─" * 60
    passed = 0
    failed = 0

    def ok(condition: bool, label: str):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  ✅ {label}")
        else:
            failed += 1
            print(f"  ❌ {label}")

    print(f"\n{'═' * 60}")
    print("  Intelligence Module Tests")
    print(f"{'═' * 60}")

    # ── 1. Signal dataclass & enums ───────────────────────────────────────────
    print(f"\n{SEP}")
    print("  1. Signal dataclass & enums")
    print(SEP)

    sig = Signal(
        symbol="NIFTY", direction=Direction.BULLISH,
        source="rsi_divergence", layer=Layer.INTRADAY,
        strength=SignalStrength.STRONG,
    )
    ok(sig.symbol == "NIFTY", "Signal.symbol")
    ok(sig.direction == Direction.BULLISH, "Signal.direction")
    ok(sig.layer == Layer.INTRADAY, "Signal.layer")
    ok(sig.strength == SignalStrength.STRONG, "Signal.strength")
    ok(sig.key == "intraday.rsi_divergence.BULLISH", f"Signal.key = {sig.key}")
    ok(sig.age_seconds >= 0, f"Signal.age_seconds = {sig.age_seconds:.3f}")

    # Frozen — should not allow mutation
    try:
        sig.symbol = "RELIANCE"
        ok(False, "Signal is frozen (should not allow mutation)")
    except AttributeError:
        ok(True, "Signal is frozen (immutable)")

    # ── 2. weight_to_strength mapping ─────────────────────────────────────────
    print(f"\n{SEP}")
    print("  2. weight_to_strength mapping")
    print(SEP)

    ok(weight_to_strength(5) == SignalStrength.WEAK, "weight 5 → WEAK")
    ok(weight_to_strength(9.9) == SignalStrength.WEAK, "weight 9.9 → WEAK")
    ok(weight_to_strength(10) == SignalStrength.MODERATE, "weight 10 → MODERATE")
    ok(weight_to_strength(15) == SignalStrength.MODERATE, "weight 15 → MODERATE")
    ok(weight_to_strength(16) == SignalStrength.STRONG, "weight 16 → STRONG")
    ok(weight_to_strength(25) == SignalStrength.STRONG, "weight 25 → STRONG")

    # ── 3. SignalBus pub/sub ──────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  3. SignalBus pub/sub")
    print(SEP)

    bus = SignalBus()
    received = []
    bus.subscribe(lambda s: received.append(s))

    ok(bus.total_emitted == 0, "Bus starts with 0 emitted")

    bus.emit(sig)
    ok(bus.total_emitted == 1, "total_emitted increments to 1")
    ok(len(received) == 1, "Subscriber received 1 signal")
    ok(received[0].symbol == "NIFTY", "Subscriber got correct signal")

    # Multiple subscribers
    received2 = []
    bus.subscribe(lambda s: received2.append(s))
    sig2 = Signal("RELIANCE", Direction.BEARISH, "ema_cross", Layer.LIVE, SignalStrength.MODERATE)
    bus.emit(sig2)
    ok(len(received) == 2, "First subscriber got 2nd signal")
    ok(len(received2) == 1, "Second subscriber got 1 signal")
    ok(bus.total_emitted == 2, "total_emitted = 2")

    # Subscriber error should not crash bus
    def bad_subscriber(s):
        raise ValueError("boom")
    bus.subscribe(bad_subscriber)
    bus.emit(sig)  # should not raise
    ok(bus.total_emitted == 3, "Bus survives subscriber exception")
    ok(len(received) == 3, "Good subscribers still receive after bad one throws")

    # ── 4. SignalCorrelator — basic confluence ────────────────────────────────
    print(f"\n{SEP}")
    print("  4. SignalCorrelator — cross-layer confluence")
    print(SEP)

    confluences: list[Confluence] = []
    correlator = SignalCorrelator(on_confluence=lambda c: confluences.append(c))

    now = _time.time()
    # Single signal — no confluence
    correlator.on_signal(Signal("NIFTY", Direction.BULLISH, "rsi", Layer.INTRADAY, SignalStrength.MODERATE, timestamp=now))
    ok(len(confluences) == 0, "Single signal → no confluence")

    # Same layer, different source — still no confluence (need 2 layers)
    correlator.on_signal(Signal("NIFTY", Direction.BULLISH, "ema", Layer.INTRADAY, SignalStrength.WEAK, timestamp=now))
    ok(len(confluences) == 0, "Same layer, different source → no confluence")

    # Second layer — should trigger MODERATE confluence
    correlator.on_signal(Signal("NIFTY", Direction.BULLISH, "pcr", Layer.LIVE, SignalStrength.STRONG, timestamp=now))
    ok(len(confluences) == 1, "2 layers aligned → confluence fired")
    ok(confluences[0].direction == Direction.BULLISH, "Confluence direction = BULLISH")
    ok(confluences[0].level == "MODERATE", f"Confluence level = {confluences[0].level}")
    ok(confluences[0].layer_count == 2, f"Confluence layer_count = {confluences[0].layer_count}")
    ok(not confluences[0].has_contradiction, "No contradiction")

    # ── 5. SignalCorrelator — HIGH confluence (3 layers) ─────────────────────
    print(f"\n{SEP}")
    print("  5. SignalCorrelator — 3-layer HIGH confluence")
    print(SEP)

    confluences_high: list[Confluence] = []
    corr2 = SignalCorrelator(on_confluence=lambda c: confluences_high.append(c))

    now = _time.time()
    corr2.on_signal(Signal("RELIANCE", Direction.BEARISH, "rsi", Layer.POSITIONAL, SignalStrength.STRONG, timestamp=now))
    corr2.on_signal(Signal("RELIANCE", Direction.BEARISH, "macd", Layer.INTRADAY, SignalStrength.MODERATE, timestamp=now))
    # 2nd signal triggers MODERATE confluence
    ok(len(confluences_high) == 1, "2 layers → first confluence")

    # Reset cooldown to allow 3-layer confluence to fire
    corr2._last_confluence.clear()
    corr2.on_signal(Signal("RELIANCE", Direction.BEARISH, "pcr", Layer.LIVE, SignalStrength.WEAK, timestamp=now))
    ok(len(confluences_high) == 2, "3 layers → second confluence fired")
    ok(confluences_high[1].level == "HIGH", f"3-layer level = {confluences_high[1].level}")
    ok(confluences_high[1].layer_count == 3, f"layer_count = {confluences_high[1].layer_count}")

    # ── 6. SignalCorrelator — contradiction detection ─────────────────────────
    print(f"\n{SEP}")
    print("  6. SignalCorrelator — contradiction detection")
    print(SEP)

    confluences_contra: list[Confluence] = []
    corr3 = SignalCorrelator(on_confluence=lambda c: confluences_contra.append(c))

    now = _time.time()
    corr3.on_signal(Signal("SBIN", Direction.BULLISH, "rsi", Layer.INTRADAY, SignalStrength.MODERATE, timestamp=now))
    corr3.on_signal(Signal("SBIN", Direction.BEARISH, "pcr", Layer.POSITIONAL, SignalStrength.STRONG, timestamp=now))
    # No confluence yet — different directions across layers
    corr3.on_signal(Signal("SBIN", Direction.BULLISH, "ema", Layer.LIVE, SignalStrength.WEAK, timestamp=now))
    ok(len(confluences_contra) >= 1, "Bullish confluence fires with contradiction")
    ok(confluences_contra[0].has_contradiction, "has_contradiction = True")

    # ── 7. SignalCorrelator — cooldown ────────────────────────────────────────
    print(f"\n{SEP}")
    print("  7. SignalCorrelator — cooldown prevents re-fire")
    print(SEP)

    confluences_cd: list[Confluence] = []
    corr4 = SignalCorrelator(on_confluence=lambda c: confluences_cd.append(c))

    now = _time.time()
    corr4.on_signal(Signal("TCS", Direction.BULLISH, "rsi", Layer.INTRADAY, SignalStrength.MODERATE, timestamp=now))
    corr4.on_signal(Signal("TCS", Direction.BULLISH, "ema", Layer.LIVE, SignalStrength.WEAK, timestamp=now))
    ok(len(confluences_cd) == 1, "First confluence fires")

    # Emit another signal — cooldown should prevent re-fire
    corr4.on_signal(Signal("TCS", Direction.BULLISH, "macd", Layer.POSITIONAL, SignalStrength.STRONG, timestamp=now))
    ok(len(confluences_cd) == 1, "Cooldown prevents second confluence")

    # ── 8. SignalCorrelator — dedup replaces same key ─────────────────────────
    print(f"\n{SEP}")
    print("  8. SignalCorrelator — dedup (same source+layer+direction replaced)")
    print(SEP)

    corr5 = SignalCorrelator()
    now = _time.time()
    corr5.on_signal(Signal("ITC", Direction.BULLISH, "rsi", Layer.INTRADAY, SignalStrength.WEAK, timestamp=now))
    corr5.on_signal(Signal("ITC", Direction.BULLISH, "rsi", Layer.INTRADAY, SignalStrength.STRONG, timestamp=now))

    snapshot = corr5.get_buffer_snapshot("ITC")
    ok(len(snapshot) == 1, f"Dedup: buffer has 1 signal (not 2)")
    ok(snapshot[0].strength == SignalStrength.STRONG, "Dedup kept latest (STRONG)")

    # ── 9. SignalCorrelator — NEUTRAL signals ignored for confluence ──────────
    print(f"\n{SEP}")
    print("  9. NEUTRAL signals do not form confluence")
    print(SEP)

    confluences_neutral: list[Confluence] = []
    corr6 = SignalCorrelator(on_confluence=lambda c: confluences_neutral.append(c))

    now = _time.time()
    corr6.on_signal(Signal("HDFC", Direction.NEUTRAL, "rsi", Layer.INTRADAY, SignalStrength.WEAK, timestamp=now))
    corr6.on_signal(Signal("HDFC", Direction.NEUTRAL, "pcr", Layer.LIVE, SignalStrength.MODERATE, timestamp=now))
    ok(len(confluences_neutral) == 0, "NEUTRAL signals → no confluence")

    # ── 10. Confluence scoring ────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  10. Confluence scoring logic")
    print(SEP)

    corr7 = SignalCorrelator()
    signals = [
        Signal("X", Direction.BULLISH, "a", Layer.INTRADAY, SignalStrength.MODERATE),  # value=2
        Signal("X", Direction.BULLISH, "b", Layer.LIVE, SignalStrength.STRONG),         # value=3
    ]
    layers = {Layer.INTRADAY, Layer.LIVE}
    # base=2+3=5, layer_bonus=(2-1)*5=5, live_bonus=3, no contradiction → 13
    score = corr7._score(signals, layers, has_contradiction=False)
    ok(score == 13.0, f"Score = {score} (expected 13.0)")

    score_contra = corr7._score(signals, layers, has_contradiction=True)
    ok(score_contra == 10.0, f"Score with contradiction = {score_contra} (expected 10.0)")

    # Without LIVE layer
    signals_no_live = [
        Signal("X", Direction.BULLISH, "a", Layer.INTRADAY, SignalStrength.WEAK),       # value=1
        Signal("X", Direction.BULLISH, "b", Layer.POSITIONAL, SignalStrength.MODERATE),  # value=2
    ]
    layers_no_live = {Layer.INTRADAY, Layer.POSITIONAL}
    score_no_live = corr7._score(signals_no_live, layers_no_live, has_contradiction=False)
    # base=1+2=3, layer_bonus=5, live_bonus=0 → 8
    ok(score_no_live == 8.0, f"Score without LIVE = {score_no_live} (expected 8.0)")

    # ── 11. get_buffer_snapshot ───────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  11. get_buffer_snapshot")
    print(SEP)

    corr8 = SignalCorrelator()
    now = _time.time()
    corr8.on_signal(Signal("INFY", Direction.BULLISH, "rsi", Layer.INTRADAY, SignalStrength.MODERATE, timestamp=now))
    corr8.on_signal(Signal("INFY", Direction.BEARISH, "pcr", Layer.LIVE, SignalStrength.WEAK, timestamp=now))

    snap = corr8.get_buffer_snapshot("INFY")
    ok(len(snap) == 2, f"Snapshot has 2 signals")

    snap_empty = corr8.get_buffer_snapshot("UNKNOWN")
    ok(len(snap_empty) == 0, "Snapshot for unknown symbol is empty")

    # ── 12. Symbol isolation ──────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  12. Signals are isolated per symbol")
    print(SEP)

    confluences_iso: list[Confluence] = []
    corr9 = SignalCorrelator(on_confluence=lambda c: confluences_iso.append(c))

    now = _time.time()
    corr9.on_signal(Signal("NIFTY", Direction.BULLISH, "rsi", Layer.INTRADAY, SignalStrength.MODERATE, timestamp=now))
    corr9.on_signal(Signal("BANKNIFTY", Direction.BULLISH, "pcr", Layer.LIVE, SignalStrength.WEAK, timestamp=now))
    ok(len(confluences_iso) == 0, "Different symbols → no cross-symbol confluence")

    # ── Summary ───────────────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'═' * 60}")
    print(f"  Results: {passed}/{total} passed, {failed} failed")
    print(f"{'═' * 60}\n")


def run_narrator_test(eod_file: str = "intraday/EOD_analysis"):
    """
    Test the MarketNarrator with real EOD data from a file.

    Usage:
      python test.py --narrator
      python test.py --narrator --eod-file path/to/file
    """
    import re
    from intelligence.llm_client import GeminiClient
    from intelligence.narrator import POSITIONAL_SYSTEM_PROMPT, POSITIONAL_PROMPT_TEMPLATE

    # ── 1. Check Gemini API key ──
    gemini = GeminiClient()
    if not gemini.available:
        print("ERROR: GEMINI_API_KEY not set in .env")
        print("Get a free key from https://aistudio.google.com/apikey")
        return

    print(f"[narrator] Gemini API key found: {gemini.api_key[:8]}...")

    # ── 2. Load EOD analysis data from file ──
    if not os.path.exists(eod_file):
        print(f"ERROR: EOD file not found: {eod_file}")
        return

    with open(eod_file, "r") as f:
        stock_alerts = f.read()

    print(f"[narrator] Loaded {len(stock_alerts)} chars from {eod_file}")

    # ── 3. Build mock report data (use real data from your Telegram notifications) ──
    # These are sample values — in production, these come from the report functions
    report_data = {
        "stock_alerts": stock_alerts,
        "index_report": (
            "Index Report\n"
            "  NIFTY: 23114.50 (+0.49%)\n"
            "  BANKNIFTY: 53427.05 (-0.04%)\n"
            "  FINNIFTY: 24781.15 (-0.68%)\n"
            "  SENSEX: 74532.96 (+0.44%)\n"
            "  NIFTYNXT50: 63862.30 (+0.45%)\n"
            "  INDIA_VIX: 22.81 (+0.05%)"
        ),
        "global_report": (
            "Global Indices Report\n"
            "USA\n"
            "  SPX: 6553.68 (-0.80%)\n"
            "  DJI: 45779.25 (-0.53%)\n"
            "  NASDAQ: 21840.08 (-1.13%)\n"
            "Europe\n"
            "  FTSE: 9979.64 (-0.83%)\n"
            "  DAX: 22562.02 (-1.22%)\n"
            "  CAC40: 7725.62 (-1.05%)\n"
            "Asia\n"
            "  NIKKEI: 53372.53 (+0.00%)\n"
            "  HSI: 25277.32 (-0.88%)\n"
            "  SSEC: 3957.05 (-1.24%)"
        ),
        "commodity_report": (
            "Commodity Report\n"
            "  GOLD: $4569.20 (-0.68%)\n"
            "  SILVER: $68.85 (-2.90%)\n"
            "  COPPER: $5.37 (-1.14%)\n"
            "  CRUDEOIL: $95.91 (-0.24%)\n"
            "  USDINR: $93.77 (+0.57%)"
        ),
        "fii_dii_report": (
            "FII/DII Flows (2026-03-20)\n"
            "  FII Cash: -5518.39  DII Cash: 5706.23\n"
            "  5d FII: -29897.67  Idx Fut: -823.8  Idx Opt: -226.16\n\n"
            "Date       FII   DII    IdxFut IdxOpt\n"
            "2026-03-20 -5518 +5706  -824   -226\n"
            "2026-03-19 -7558 +3864  -471   -1093\n"
            "2026-03-18 -2714 +3253  +2358  -3785\n"
            "2026-03-17 -4741 +5225  +1247  -5652\n"
            "2026-03-16 -9366 +12593 +1784  -2780\n\n"
            "F&O Participant OI (last 5 days)\n"
            "  FII: Net -234508 | Long 47246 | Short 281754\n"
            "  Client: Net 147946 | Long 280172 | Short 132226\n"
            "  DII: Net 49651 | Long 71121 | Short 21470"
        ),
        "sector_report": (
            "Sector Performance (2026-03-20)\n"
            "  Advancing: 31  Declining: 10\n\n"
            "Top 5 Gaining Sectors\n"
            "  Mining: +3.51%\n"
            "  Iron & Steel: +2.87%\n"
            "  Information Technology: +1.66%\n\n"
            "Top 5 Losing Sectors\n"
            "  Industrials Gases & Fuels: -1.64%\n"
            "  Ship Building: -1.61%\n"
            "  Realty: -0.68%\n\n"
            "NSE Indices (2026-03-20)\n"
            "  Nifty IT: +2.17%\n"
            "  Nifty PSU Bank: +2.07%\n"
            "  Nifty Pharma: +1.99%\n"
            "  Nifty Capital Markets: -1.74%\n"
            "  Nifty Realty: -0.93%"
        ),
        "week52_report": (
            "52-Week High / Low\n\n"
            "52W Highs (0): None\n\n"
            "52W Lows (12)\n"
            "  PETRONET: 257.65  -4.87%\n"
            "  LODHA: 796.90  -2.57%\n"
            "  HDFCBANK: 780.45  -2.22%\n"
            "  GAIL: 142.87  -0.97%\n"
            "  SBICARD: 688.75  -0.79%\n"
            "  JUBLFOOD: 451.55  -0.58%\n"
            "  KOTAKBANK: 366.75  -0.38%\n"
            "  BAJAJFINSV: 1710.30  -0.27%\n"
            "  BAJFINANCE: 830.55  -0.20%\n"
            "  INDIGO: 4149.10  -0.13%\n"
            "  MANKIND: 2000.00  -0.04%"
        ),
        "movers_summary": (
            "Top Gainers\n"
            "  1. TATAELXSI: +4.91%\n"
            "  2. INOXWIND: +4.83%\n"
            "  3. LAURUSLABS: +4.48%\n"
            "  4. IDEA: +4.47%\n"
            "  5. JINDALSTEL: +4.25%\n\n"
            "Top Losers\n"
            "  1. PETRONET: -4.87%\n"
            "  2. MCX: -4.58%\n"
            "  3. BSE: -3.07%\n"
            "  4. LODHA: -2.57%\n"
            "  5. HINDALCO: -2.54%"
        ),
    }

    # ── 4. Build the prompt (same as narrator._build_positional_prompt) ──
    def strip_html(text):
        return re.sub(r"<[^>]+>", "", text) if text else "No data"

    from datetime import datetime
    prompt = POSITIONAL_PROMPT_TEMPLATE.format(
        date=datetime.now().strftime("%d %b %Y"),
        index_report=strip_html(report_data["index_report"]),
        global_report=strip_html(report_data["global_report"]),
        commodity_report=strip_html(report_data["commodity_report"]),
        fii_dii_report=strip_html(report_data["fii_dii_report"]),
        sector_report=strip_html(report_data["sector_report"]),
        week52_report=strip_html(report_data["week52_report"]),
        stock_alerts=strip_html(report_data["stock_alerts"]),
        movers_summary=strip_html(report_data["movers_summary"]),
    )

    # ── 5. Print the prompt ──
    print(f"\n{'=' * 80}")
    print("  PROMPT SENT TO GEMINI")
    print(f"{'=' * 80}")
    print(prompt)
    print(f"{'=' * 80}")
    print(f"  Prompt length: {len(prompt)} chars (~{len(prompt) // 4} tokens)")
    print(f"{'=' * 80}\n")

    # ── 6. Call Gemini ──
    print("[narrator] Calling Gemini Flash...")
    response = gemini.generate(POSITIONAL_SYSTEM_PROMPT, prompt)

    if response:
        print(f"\n{'=' * 80}")
        print("  GEMINI RESPONSE")
        print(f"{'=' * 80}")
        print(response)
        print(f"{'=' * 80}")
        print(f"  Response length : {len(response)} chars  (~{len(response)//4} tokens)")
        print(f"  Daily tokens    : {gemini.daily_tokens_used}")
        print(f"  MAX_OUTPUT_TOKENS set to: {gemini.MAX_OUTPUT_TOKENS}")
        print(f"{'=' * 80}")
    else:
        print("[narrator] ERROR: No response from Gemini. Check API key and logs.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Pre-Market Report via Telegram, or inspect futures data")
    parser.add_argument("--global", dest="global_only", action="store_true",
                        help="Only send global cues & FII/DII report")
    parser.add_argument("--preopen", action="store_true",
                        help="Only send NSE pre-open session report")
    parser.add_argument("--futures", action="store_true",
                        help="Print futures data from the current NSE + Zerodha implementation")
    parser.add_argument("--ws-options", dest="ws_options", action="store_true",
                        help="Connect WebSocket and display live Nifty/BankNifty option data")
    parser.add_argument("--intelligence", action="store_true",
                        help="Test intelligence module: Signal, SignalBus, SignalCorrelator")
    parser.add_argument("--narrator", action="store_true",
                        help="Test MarketNarrator with EOD data from file")
    parser.add_argument("--nse-holidays", dest="nse_holidays", action="store_true",
                        help="Print all NSE holidays from the XNSE calendar + custom_holidays.json content")
    parser.add_argument("--year", type=int, default=None,
                        help="Year to list holidays for with --nse-holidays (default: current year)")
    parser.add_argument("--holidays", action="store_true",
                        help="Test market_calendar: is_trading_day + get_upcoming_holidays")
    parser.add_argument("--date", type=str, default=None,
                        help="Specific date to check with --holidays (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=7,
                        help="Days ahead to scan for upcoming holidays (default: 7)")
    parser.add_argument("--inject", type=str, default=None,
                        help="Inject this date into custom_holidays.json for testing (YYYY-MM-DD)")
    parser.add_argument("--eod-file", type=str, default="intraday/EOD_analysis",
                        help="Path to EOD analysis file (default: intraday/EOD_analysis)")
    parser.add_argument("--stock", type=str, default=None,
                        help="Filter to a single stock symbol (e.g. RELIANCE)")
    parser.add_argument("--index", type=str, default=None,
                        help="Filter to a single index (e.g. NIFTY, BANKNIFTY)")
    parser.add_argument("--duration", type=int, default=60,
                        help="Duration in seconds for WebSocket test (default: 60)")
    parser.add_argument("--mode", type=str, choices=["positional", "intraday"],
                        default="positional",
                        help="positional=daily data, intraday=5min data (default: positional)")
    args = parser.parse_args()

    if args.intelligence:
        run_intelligence_test()
    elif args.nse_holidays:
        print_nse_calendar_holidays(year=args.year)
    elif args.holidays:
        run_holidays_test(
            check_date_str=args.date,
            days_ahead=args.days,
            inject_date_str=args.inject,
        )
    elif args.narrator:
        run_narrator_test(eod_file=args.eod_file)
    elif args.ws_options:
        run_websocket_options_test(index_filter=args.index, duration=args.duration)
    elif args.futures:
        run_futures_test(stock_filter=args.stock, mode=args.mode)
    elif args.global_only:
        print("Sending global cues report to Telegram...")
        run_global_cues_report()
        print("Done.")
    elif args.preopen:
        print("Sending pre-open session report to Telegram...")
        run_preopen_report()
        print("Done.")
    else:
        print("Sending global cues report to Telegram...")
        run_global_cues_report()
        print()
        print("Sending pre-open session report to Telegram...")
        run_preopen_report()
        print()
        print("Both reports sent.")
