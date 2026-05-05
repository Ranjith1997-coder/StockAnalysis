"""
test_sensibull_integration.py
─────────────────────────────
Integration test for the Sensibull WS feed + adapter pipeline.

Three test phases:
  1. SUBSCRIPTIONS  — which symbols/tokens/expiries will be subscribed.
  2. ADAPTER offline — applies ws_decoded.json through the full adapter path.
  3. WS CONNECTIVITY — verifies the handshake with wsrelay.sensibull.com.

Usage:
    .venv/bin/python scripts/test_sensibull_integration.py

Env overrides:
    SENSIBULL_EXPIRY_NIFTY=YYYY-MM-DD
    SENSIBULL_EXPIRY_BANKNIFTY=YYYY-MM-DD
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

# ── repo root on sys.path ─────────────────────────────────────────────────────
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from fno.sensibull_feed import SensibullFeed          # noqa: E402
from fno.sensibull_adapter import SensibullAdapter    # noqa: E402
from zerodha.tick_store import TickStore              # noqa: E402


# ── subscription config ───────────────────────────────────────────────────────

_SYMBOL_UNDERLYING = {
    "NIFTY":     256265,
    "BANKNIFTY": 260105,
    "SENSEX":    265,
}
_SENSIBULL_SYMBOLS = set(_SYMBOL_UNDERLYING)

# Known expiries — override via SENSIBULL_EXPIRY_<SYMBOL>=YYYY-MM-DD
_KNOWN_EXPIRY = {
    "NIFTY":     "2026-05-05",
    "BANKNIFTY": "2026-05-26",
    "SENSEX":    "2026-05-29",   # BSE SENSEX monthly (last Friday of month)
}

def _nearest_expiry(symbol: str) -> str:
    return os.getenv(f"SENSIBULL_EXPIRY_{symbol}", _KNOWN_EXPIRY.get(symbol, ""))

SUBSCRIPTIONS = {
    sym: {"underlying": token, "expiry": _nearest_expiry(sym)}
    for sym, token in _SYMBOL_UNDERLYING.items()
    if _nearest_expiry(sym)  # skip symbols with no expiry configured
}


# ── minimal Stock stub ────────────────────────────────────────────────────────

class _StubStock:
    def __init__(self, symbol: str) -> None:
        self.stock_symbol = symbol
        self._tick_store = TickStore()

    @property
    def options_live(self):
        return self._tick_store.options_live

    @property
    def options_aggregate(self):
        return self._tick_store.options_aggregate

    def update_option_tick(self, strike, option_type, tick):
        self._tick_store.update_option_tick(strike, option_type, tick)

    def recompute_options_aggregate(self, spot=None):
        self._tick_store.recompute_options_aggregate(spot)


# ── print helpers ─────────────────────────────────────────────────────────────

def _print_aggregate(symbol: str, agg: dict) -> None:
    print(f"\n  options_aggregate [{symbol}]")
    print(f"  {'─'*56}")
    for k, v in agg.items():
        if k == "last_updated" and v:
            import datetime as _dt
            v = _dt.datetime.fromtimestamp(v).strftime("%H:%M:%S")
        tag = ""
        if isinstance(v, float):
            print(f"    {k:<28} {v:.4f}{tag}")
        else:
            print(f"    {k:<28} {v}{tag}")


def _print_options_live_sample(symbol: str, live: dict, agg: dict, n: int = 5) -> None:
    if not live:
        print(f"\n  [WARN] options_live is EMPTY for {symbol}")
        return
    atm = agg.get("atm_strike")
    strikes = sorted(live.keys())
    if atm and atm in live:
        idx = strikes.index(atm)
        sample = strikes[max(0, idx-n): idx+n+1]
    else:
        mid = len(strikes) // 2
        sample = strikes[max(0, mid-n): mid+n+1]

    print(f"\n  options_live sample [{symbol}]  ATM={atm}  total_strikes={len(strikes)}")
    print(f"  {'─'*100}")
    print(f"  {'Strike':>8}  {'Side':4}  {'LTP':>8}  {'OI':>10}  {'Prev OI':>10}  "
          f"{'Volume':>8}  {'Delta':>6}  {'IV':>6}  {'IV Chg':>7}  {'Theta':>7}  {'Vega':>6}")
    for s in sample:
        for side in ("CE", "PE"):
            e = live[s].get(side)
            if not e:
                continue
            marker = " ← ATM" if s == atm else ""
            delta_str = f"{e['delta']:>+6.3f}" if "delta" in e else "     -"
            iv_str    = f"{e['iv']:>6.3f}"     if "iv"    in e else "     -"
            ivc_str   = f"{e['iv_change']:>+7.4f}" if "iv_change" in e else "      -"
            theta_str = f"{e['theta']:>7.2f}"  if "theta" in e else "      -"
            vega_str  = f"{e['vega']:>6.2f}"   if "vega"  in e else "     -"
            print(
                f"  {s:>8.0f}  {side:4}  "
                f"{e.get('ltp', 0):>8.2f}  "
                f"{e.get('oi', 0):>10,}  "
                f"{e.get('prev_oi', 0):>10,}  "
                f"{e.get('volume', 0):>8,}  "
                f"{delta_str}  {iv_str}  {ivc_str}  {theta_str}  {vega_str}"
                f"{marker}"
            )


def _run_checks(symbol: str, agg: dict, live: dict, n_snaps: int) -> bool:
    checks = [
        ("options_live not empty",         len(live) > 0),
        ("total_ce_oi > 0",                agg["total_ce_oi"] > 0),
        ("total_pe_oi > 0",                agg["total_pe_oi"] > 0),
        ("live_pcr > 0",                   agg["live_pcr"] > 0),
        ("atm_strike set",                 agg["atm_strike"] is not None),
        ("atm_straddle_premium > 0",       agg["atm_straddle_premium"] > 0),
        ("net_ce_oi_change is int",        isinstance(agg["net_ce_oi_change"], int)),
        ("net_pe_oi_change is int",        isinstance(agg["net_pe_oi_change"], int)),
        ("net_ce_oi_change != 0 (2nd snap +10)", agg["net_ce_oi_change"] != 0),
        ("net_pe_oi_change != 0 (2nd snap +10)", agg["net_pe_oi_change"] != 0),
        ("atm_iv > 0  (Sensibull field)",  agg["atm_iv"] > 0),
        ("atm_iv_percentile > 0",          agg["atm_iv_percentile"] > 0),
        ("atm_iv_ce > 0  (OTM call IV)",   agg["atm_iv_ce"] > 0),
        ("atm_iv_pe > 0  (OTM put IV)",    agg["atm_iv_pe"] > 0),
        ("iv_skew computed",               agg["iv_skew"] != 0.0),
        ("future_price > 0",               agg["future_price"] > 0),
        ("max_oi_ce_strike set",           agg["max_oi_ce_strike"] is not None),
        ("max_oi_pe_strike set",           agg["max_oi_pe_strike"] is not None),
        ("max_pain_strike set",            agg.get("max_pain_strike") is not None),
    ]
    all_ok = True
    for label, ok in checks:
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"    [{status}] {label}")
    return all_ok


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — subscription info
# ══════════════════════════════════════════════════════════════════════════════

def phase1_subscriptions() -> None:
    print("\n" + "═"*60)
    print("  PHASE 1 — Subscriptions")
    print("═"*60)
    print(f"\n  {'Symbol':<12} {'Underlying Token':>16}   {'Expiry':>12}")
    print(f"  {'─'*44}")
    for sym, info in SUBSCRIPTIONS.items():
        print(f"  {sym:<12} {info['underlying']:>16}   {info['expiry']:>12}")
    print(f"\n  These are the exact values that will be sent in the")
    print(f"  'underlyingExpiry' field of the subscribe message.")


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — offline adapter test using ws_decoded.json
# ══════════════════════════════════════════════════════════════════════════════

def phase2_adapter_offline() -> bool:
    print("\n" + "═"*60)
    print("  PHASE 2 — Adapter (offline, using ws_decoded.json)")
    print("═"*60)

    decoded_path = _REPO / "ws_decoded.json"
    if not decoded_path.exists():
        print(f"\n  [SKIP] ws_decoded.json not found at {decoded_path}")
        print("         Run ws_test.py first to capture a live snapshot.")
        return True  # not a failure

    with open(decoded_path) as f:
        data = json.load(f)

    symbol = "BANKNIFTY"  # ws_decoded.json is BANKNIFTY data
    stub = _StubStock(symbol)
    adapter = SensibullAdapter()

    print(f"\n  Loaded ws_decoded.json — {len(data.get('chain', {}))} strikes")
    print(f"  future_price={data.get('future_price')}  atm_strike={data.get('atm_strike')}")
    print(f"  atm_iv={data.get('atm_iv', 0)*100:.1f}%  atm_iv_percentile={data.get('atm_iv_percentile', 0)*100:.0f}th")

    # First call — establishes prev_oi from oi_change_quantity baseline
    adapter.apply(stub, data, live_options_engine=None)

    # Second call (same data) — prev_oi should now come from cache
    import copy
    data2 = copy.deepcopy(data)
    # Simulate a slight OI change
    chain = data2["chain"]
    for strike_data in chain.values():
        for side in ("call", "put"):
            leg = strike_data.get(side, {})
            leg["oi"] = leg.get("oi", 0) + 10   # +10 lots change
    adapter.apply(stub, data2, live_options_engine=None)

    agg = stub.options_aggregate
    live = stub.options_live

    print(f"\n  After 2 apply() calls (2nd had +10 OI per leg):")
    all_ok = _run_checks(symbol, agg, live, n_snaps=2)

    # Verify prev_oi tracking
    atm = agg.get("atm_strike")
    if atm and atm in live:
        ce = live[atm].get("CE", {})
        pe = live[atm].get("PE", {})
        print(f"\n  ATM {atm} after 2 snapshots:")
        print(f"    CE: oi={ce.get('oi')}  prev_oi={ce.get('prev_oi')}  "
              f"(delta={ce.get('oi',0)-ce.get('prev_oi',0)})")
        print(f"    PE: oi={pe.get('oi')}  prev_oi={pe.get('prev_oi')}  "
              f"(delta={pe.get('oi',0)-pe.get('prev_oi',0)})")
        oi_tracking_ok = (ce.get("oi", 0) != ce.get("prev_oi", 0))
        status = "PASS" if oi_tracking_ok else "WARN"
        print(f"    [{status}] prev_oi tracking across snapshots")
        if not oi_tracking_ok:
            print("           (prev_oi == oi is valid if data was identical)")

    _print_aggregate(symbol, agg)
    _print_options_live_sample(symbol, live, agg)
    return all_ok


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — WS connectivity check (handshake + first frame)
# ══════════════════════════════════════════════════════════════════════════════

def phase3_ws_connectivity() -> bool:
    print("\n" + "═"*60)
    print("  PHASE 3 — WebSocket connectivity check (30s timeout)")
    print("═"*60)
    print("\n  Expected sequence: [1] heartbeat 0xFD  →  [2] full chain snapshot")

    connected = threading.Event()
    data_frame = threading.Event()
    errors: list[str] = []
    frames: list[dict] = []   # one entry per frame received: {idx, size, is_heartbeat}

    import websocket as _ws
    from fno.sensibull_feed import _decode_frame
    _WS_HEADERS = [
        "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Cache-Control: no-cache",
        "Pragma: no-cache",
    ]
    WS_URL = "wss://wsrelay.sensibull.com/broker/1?consumerType=platform_no_plan"
    sub_msg = json.dumps({
        "msgCommand": "subscribe", "dataSource": "option-chain",
        "brokerId": 1, "tokens": [],
        "underlyingExpiry": [list(SUBSCRIPTIONS.values())[0]],
        "uniqueId": "",
    })

    def on_open(ws):
        connected.set()
        ws.send(sub_msg)
        print(f"  [OPEN] connected, subscribe sent")

    def on_message(ws, raw):
        raw = raw if isinstance(raw, bytes) else raw.encode()
        n = len(raw)
        is_hb = n <= 2
        idx = len(frames) + 1
        frames.append({"idx": idx, "size": n, "is_heartbeat": is_hb})
        print(f"  [{idx}] {'heartbeat 0x{:02x}'.format(raw[0]) if is_hb else f'data frame  {n:,} bytes'}")

        if not is_hb:
            # Decode and summarise the chain
            decoded = _decode_frame(raw)
            if decoded:
                chain = decoded.get("chain", {})
                strikes = sorted(chain.keys(), key=lambda x: float(x))
                atm = decoded.get("atm_strike")
                print(f"       strikes={len(strikes)}  ({strikes[0]}–{strikes[-1]})")
                print(f"       atm={atm}  future_price={decoded.get('future_price')}")
                print(f"       atm_iv={decoded.get('atm_iv',0)*100:.1f}%  "
                      f"atm_iv_percentile={decoded.get('atm_iv_percentile',0)*100:.0f}th  "
                      f"pcr={decoded.get('pcr')}")
                data_frame.set()
            ws.close()

    def on_error(ws, err):
        errors.append(str(err))
        data_frame.set()  # unblock on error

    def on_close(ws, code, msg):
        data_frame.set()  # unblock if closed before data arrived

    ws_app = _ws.WebSocketApp(WS_URL, header=_WS_HEADERS,
                               on_open=on_open, on_message=on_message,
                               on_error=on_error, on_close=on_close)
    t = threading.Thread(
        target=lambda: ws_app.run_forever(origin="https://web.sensibull.com",
                                          ping_interval=20, ping_timeout=10),
        daemon=True,
    )
    t.start()

    got_data = data_frame.wait(timeout=30)
    ws_app.close()

    handshake_ok = connected.is_set() and not errors
    has_heartbeat = any(f["is_heartbeat"] for f in frames)
    has_data = any(not f["is_heartbeat"] for f in frames)

    print(f"\n  Handshake:        {'OK' if handshake_ok else 'FAIL'}")
    print(f"  Heartbeat:        {'YES' if has_heartbeat else 'NO'}")
    print(f"  Data frame:       {'YES' if has_data else 'NO (market closed or timeout)'}")
    if errors:
        for e in errors:
            print(f"  [ERROR] {e}")
    if not has_data:
        print("  NOTE: data frames only arrive during market hours (09:15–15:30 IST).")

    print(f"\n  [{'PASS' if handshake_ok else 'FAIL'}] WS handshake")
    print(f"  [{'PASS' if has_heartbeat else 'FAIL'}] heartbeat (frame 1)")
    print(f"  [{'PASS' if has_data else 'INFO '}] data frame (frame 2)  "
          f"{'— requires market hours' if not has_data else ''}")
    return handshake_ok and has_heartbeat


# ══════════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print("\n" + "═"*60)
    print("  SENSIBULL FEED INTEGRATION TEST")
    print("═"*60)

    phase1_subscriptions()
    ok2 = phase2_adapter_offline()
    ok3 = phase3_ws_connectivity()

    overall = ok2 and ok3
    print("\n" + "═"*60)
    print(f"  OVERALL: {'ALL CHECKS PASSED ✓' if overall else 'SOME CHECKS FAILED ✗'}")
    print("═"*60 + "\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()



# ── minimal Stock stub ────────────────────────────────────────────────────────

class _StubStock:
    """Minimal stand-in for common.Stock that only needs a TickStore."""

    def __init__(self, symbol: str) -> None:
        self.stock_symbol = symbol
        self._tick_store = TickStore()

    @property
    def options_live(self):
        return self._tick_store.options_live

    @property
    def options_aggregate(self):
        return self._tick_store.options_aggregate

    def update_option_tick(self, strike, option_type, tick):
        self._tick_store.update_option_tick(strike, option_type, tick)

    def recompute_options_aggregate(self, spot=None):
        self._tick_store.recompute_options_aggregate(spot)


# ── report helpers ────────────────────────────────────────────────────────────

def _print_aggregate(symbol: str, agg: dict) -> None:
    print(f"\n{'─'*60}")
    print(f"  options_aggregate  [{symbol}]")
    print(f"{'─'*60}")
    for k, v in agg.items():
        if k == "last_updated" and v:
            import datetime as _dt
            v = _dt.datetime.fromtimestamp(v).strftime("%H:%M:%S")
        if isinstance(v, float):
            print(f"  {k:<25} {v:.4f}")
        else:
            print(f"  {k:<25} {v}")


def _print_options_live_sample(symbol: str, live: dict, agg: dict) -> None:
    if not live:
        print(f"\n  [WARN] options_live is EMPTY for {symbol}")
        return

    atm = agg.get("atm_strike")
    strikes_sorted = sorted(live.keys())
    n = len(strikes_sorted)

    if atm and atm in live:
        idx = strikes_sorted.index(atm)
        sample = strikes_sorted[max(0, idx-5) : idx+6]
    else:
        sample = strikes_sorted[n//2-5 : n//2+6]

    print(f"\n{'─'*60}")
    print(f"  options_live sample [{symbol}]  (ATM={atm}, total strikes={n})")
    print(f"{'─'*60}")
    print(f"  {'Strike':>8}  {'Side':4}  {'LTP':>8}  {'OI':>10}  {'Prev OI':>10}  {'Volume':>8}  {'BuyQ':>8}  {'SellQ':>8}")
    for strike in sample:
        for side in ("CE", "PE"):
            entry = live[strike].get(side)
            if not entry:
                continue
            marker = " ← ATM" if strike == atm else ""
            print(
                f"  {strike:>8.0f}  {side:4}  "
                f"{entry.get('ltp', 0):>8.2f}  "
                f"{entry.get('oi', 0):>10,}  "
                f"{entry.get('prev_oi', 0):>10,}  "
                f"{entry.get('volume', 0):>8,}  "
                f"{entry.get('buy_qty', 0):>8.2f}  "
                f"{entry.get('sell_qty', 0):>8.2f}"
                f"{marker}"
            )


# ── main test ─────────────────────────────────────────────────────────────────

def main() -> None:
    print("\n" + "═"*60)
    print("  SENSIBULL FEED INTEGRATION TEST")
    print("═"*60)

    # ── 1. Show subscriptions ─────────────────────────────────────────────────
    print("\n[1] Subscriptions that will be sent to wsrelay.sensibull.com\n")
    for sym, info in SUBSCRIPTIONS.items():
        print(f"  {sym:<12} underlying_token={info['underlying']}   expiry={info['expiry']}")
    print()

    # ── 2. Wire up stubs + adapter ────────────────────────────────────────────
    stubs: dict[str, _StubStock] = {sym: _StubStock(sym) for sym in SUBSCRIPTIONS}
    adapter = SensibullAdapter()

    received: dict[str, threading.Event] = {sym: threading.Event() for sym in SUBSCRIPTIONS}
    snapshot_count: dict[str, int] = {sym: 0 for sym in SUBSCRIPTIONS}

    def make_callback(symbol: str):
        stub = stubs[symbol]

        def on_snapshot(token: int, data: dict) -> None:
            snapshot_count[symbol] += 1
            # Apply to TickStore (no LiveOptionsEngine in this test)
            adapter.apply(stub, data, live_options_engine=None)
            if not received[symbol].is_set():
                received[symbol].set()

        return on_snapshot

    # ── 3. Start one feed per symbol ──────────────────────────────────────────
    print("[2] Starting SensibullFeed for each symbol …")
    feeds: list[SensibullFeed] = []
    for sym, info in SUBSCRIPTIONS.items():
        feed = SensibullFeed(
            subscriptions=[info],
            on_snapshot=make_callback(sym),
        )
        feed.start()
        feeds.append(feed)

    # ── 4. Wait for first snapshot (up to 60s each) ───────────────────────────
    print("[3] Waiting for first snapshot from each symbol (timeout=60s) …\n")
    for sym, event in received.items():
        ok = event.wait(timeout=60)
        if ok:
            print(f"  ✓  {sym} — first snapshot received")
        else:
            print(f"  ✗  {sym} — TIMEOUT: no snapshot within 60s")

    # Give adapter 2 more seconds to finish any in-flight second snapshot
    time.sleep(2)

    # Stop feeds
    for feed in feeds:
        feed.stop()

    # ── 5. Print results ──────────────────────────────────────────────────────
    print("\n" + "═"*60)
    print("  RESULTS")
    print("═"*60)

    all_ok = True
    for sym, stub in stubs.items():
        agg = stub.options_aggregate
        live = stub.options_live
        n_snapshots = snapshot_count[sym]

        print(f"\n[{sym}]  snapshots received: {n_snapshots}   strikes populated: {len(live)}")

        # Aggregate checks
        checks = [
            ("options_live not empty",          len(live) > 0),
            ("total_ce_oi > 0",                 agg["total_ce_oi"] > 0),
            ("total_pe_oi > 0",                 agg["total_pe_oi"] > 0),
            ("live_pcr > 0",                    agg["live_pcr"] > 0),
            ("atm_strike set",                  agg["atm_strike"] is not None),
            ("atm_straddle_premium > 0",        agg["atm_straddle_premium"] > 0),
            ("net_ce_oi_change set (int)",      isinstance(agg["net_ce_oi_change"], int)),
            ("net_pe_oi_change set (int)",      isinstance(agg["net_pe_oi_change"], int)),
            ("atm_iv populated (Sensibull)",    agg["atm_iv"] > 0),
            ("atm_iv_percentile populated",     agg["atm_iv_percentile"] > 0),
            ("future_price > 0",                agg["future_price"] > 0),
            ("max_oi_ce_strike set",            agg["max_oi_ce_strike"] is not None),
            ("max_oi_pe_strike set",            agg["max_oi_pe_strike"] is not None),
        ]

        for label, result in checks:
            status = "PASS" if result else "FAIL"
            if not result:
                all_ok = False
            print(f"    [{status}] {label}")

        _print_aggregate(sym, agg)
        _print_options_live_sample(sym, live, agg)

        # prev_oi check: after 2+ snapshots prev_oi should differ from oi
        if n_snapshots >= 2 and live:
            atm = agg.get("atm_strike")
            sample_strike = atm if atm in live else next(iter(live))
            ce_entry = live[sample_strike].get("CE", {})
            oi_differs = ce_entry.get("oi", 0) != ce_entry.get("prev_oi", 0)
            # Note: OI may legitimately not change between snapshots — just report
            print(f"\n    [INFO] ATM CE  oi={ce_entry.get('oi')}  prev_oi={ce_entry.get('prev_oi')}  "
                  f"(differ={oi_differs})")

    print("\n" + "═"*60)
    print(f"  OVERALL: {'ALL CHECKS PASSED ✓' if all_ok else 'SOME CHECKS FAILED ✗'}")
    print("═"*60 + "\n")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
