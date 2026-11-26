# Sensibull auth — manual rotation runbook

Sensibull's API needs a **pair**: `access_token` + `client_info` (a JWT with an
`exp` claim). Both live in the Redis hash `auth:sensibull`. When the JWT expires
the API returns **401** even though the token looks fine, and `data:sensibull`
goes stale — which silently degrades every analyser that reads the chain
context (IV rank, max pain, OI chain, option-seller composite).

## Why it is manual (since 2026-11-22)

`auth-service` automates the Kite login and 2FA, but Kite now stops at the
**consent page** (`/connect/authorize?...&sess_id=…`). That page is a Vue app;
every automated approval attempt returns `405 Request method not allowed`
(`GET ?action=allow`, `?action=authorize`, `POST/PUT/PATCH` with the form
fields). A human click is required to obtain a new `request_token`.

## Rotating the pair (~2 minutes)

1. In a browser, open <https://web.sensibull.com> and log in through Kite.
   Click **Allow** on the consent page.
2. DevTools → Application → Cookies → `https://oxide.sensibull.com`.
   Copy the values of:
   - `access_token` (starts `pb:`)
   - `client_info` (a JWT starting `eyJ…`, ~180 chars)
3. Set them on the server (the data-gateway reads Redis on every fetch):

```bash
redis-cli HSET auth:sensibull access_token '<access_token>' \
  client_info '<client_info>' issued_at "$(date +%s)" last_reason manual
```

4. Verify:

```bash
make smoke          # sensibull:auth must show a future expiry
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Cookie: access_token=<access_token>; client_info=<client_info>" \
  'https://oxide.sensibull.com/v1/compute/cache/stock_info?tradingsymbol=NIFTY'
# expect 200
```

The data-gateway cycles every 310 s, so `data:sensibull:{NIFTY,BANKNIFTY,SENSEX}`
refreshes within ~5 minutes. `make smoke` then shows `sensibull:*` passing.

## Guard rails that now exist

- `make smoke` → `sensibull:auth` decodes the JWT and fails when it is expired;
  `sensibull:{SYMBOL}` fails when the last fetch is older than 30 min in market
  hours.
- `check_entry_gate` refuses index entries with `stale_sensibull` when the last
  fetch is older than 30 min (`PAPER_MAX_SENSIBULL_AGE_SECONDS=1800`; `0`
  disables). This prevents trades decided on days-old chain data.
- `data:sensibull`'s `last_fetch_time` is the observable used by both checks.

## Notes

- The token pair outlives a trading day but not indefinitely; treat expiry as a
  normal maintenance event (the JWT `exp` is the signal).
- A fresh Kite login invalidates the enctoken the WebSocket uses — rotate
  Sensibull **after the close**, or immediately re-run `_do_refresh` so
  market-data reconnects with the new enctoken.
- `SENSIBULL_ACCESS_TOKEN` / `SENSIBULL_CLIENT_INFO` in `.env` are a fallback
  when Redis is empty; Redis wins.
