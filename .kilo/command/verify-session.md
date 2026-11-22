---
description: Trading-day smoke test of the live system (after deploy or before trusting paper P&L)
---

Verify a live trading session. Report a short table of checks with PASS/FAIL
and anything anomalous. Do not change code unless asked.

Run over SSH to `hacker@100.92.21.31` (Tailscale) and report:

1. **Time & market state** — `date`; is it a trading day; market open
   (09:15–15:30 IST)?
2. **Services** — `systemctl is-active` for: stockanalysis, notification,
   data-gateway, market-data, analysis-engine, signal-intelligence, auth,
   resource-monitor, paper-trading, pinn-trainer, volatility-engine.
   Also `systemctl --failed`.
3. **Heartbeats** — for each `service:registry:*` key show `status` and age of
   `last_heartbeat` (flag > 120 s).
4. **Live data freshness**
   - `data:options_agg:{NIFTY,BANKNIFTY,SENSEX}` → `last_updated` age, `expiry`,
     `atm_strike`, `live_pcr`, `pcr_window_pct`.
   - `data:options_live:{SYM}` → strike count, and that `bid`/`ask` exist.
   - `data:tick:INDIA_VIX` present.
5. **Paper trading** — open positions count, today's realised P&L
   (`paper:daily_pnl:{date}`), margin blocked, any positions with DTE < 0
   (should be none), cooldown keys.
6. **Signal health** — last 30 lines of `journalctl -u stockanalysis` and
   `-u stockanalysis-analysis-engine`; grep for "format error", Traceback,
   "Conflict", "REJECTED".
7. **Cost sanity** — take the most recent closed trade and recompute charges
   with `services/paper_trading/models.compute_transaction_costs`; flag a
   mismatch.

Finish with: what is healthy, what is not, and the single most important thing
to fix.
