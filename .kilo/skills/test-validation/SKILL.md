---
name: test-validation
description: "How to run and validate changes in StockAnalysis: test layers, markers, commands, CI split, reading failures, adding a Redis field, post-deploy smoke. USE FOR: run tests, which test to write, validate a change, test failure triage, coverage, contract/property/integration/e2e test, silent except allowlist, smoke check, pre-PR check. DO NOT USE FOR: deploying (stockanalysis-deploy), strategy rules (options-data-contract), writing analysers (add-analyser)."
---

# Validating a change

Full design: `docs/testing-strategy.md`. This skill is the operator's shortcut.

## 1. Pick the command

| Situation | Command | Budget |
|---|---|---|
| Before every commit | `make test-fast` | ~30 s |
| Exactly what PR CI runs | `make test-layers` | ~30 s |
| Deep layers (e2e/soak) | `make test-deep` | nightly |
| After a deploy | `make smoke` | needs SSH |
| One module | `make test-module MODULE=services` | fast |
| Lint only changed files | `git diff --name-only -- '*.py' \| xargs .venv/bin/ruff check --ignore E402` | fast |

`PYTHONPATH=.` is required for bare pytest; the Makefile targets set it.

## 2. Layers and what a failure means

| Layer | Where | Marker | Catches | A failure means |
|---|---|---|---|---|
| unit | `tests/**` minus below | (none) | logic | a function's contract changed |
| contract | `tests/contract/` | `contract` | Redis field drift, torch leakage, unit-file/ExecStart errors, services that crash without Redis, heartbeat wiring, silent excepts, `time` shadowing | **stop**: production wiring is wrong |
| property | `tests/property/` | `property` | cost/exit/cap/parquet invariants over many inputs | an invariant does not hold for some input (show the example) |
| integration | `tests/integration/` | `integration` | publisher→consumer, heartbeat TTL, concurrent close, daily reset, capture flush, PINN loop resilience | components disagree, or state is booked twice |
| e2e | `tests/e2e/` (T5, not built yet) | `e2e` | a whole simulated trading day | scenario oracle failed (see its detail string) |
| smoke | `tests/smoke/`, `scripts/smoke_check.py` | `smoke` | live server health | production is unhealthy **now** |

Rules:
- Deterministic only: fakeredis (`fake_redis` fixture), `time-machine` (`clock`
  fixture), fixed seeds, no network. `market_calendar.is_trading_day()` calls the
  NSE API — patch it in tests (`monkeypatch.setattr(pt_main, "is_trading_day", lambda _d: True)`).
- Never `git add -A` (it once swept 254 MB of data into history).

## 3. Contract layer specifics

- `tests/contract/schema.py` is the **source of truth** for `data:options_live`,
  `data:options_agg`, `data:futures_live`, `data:tick` and `service:registry`.
- `test_redis_schema.py` runs the **real** `SnapshotPublisher._publish_all()` on
  fakeredis, so an undeclared/lost field fails immediately.
- `.kilo/skills/options-data-contract/SKILL.md` must list every declared field
  (a test enforces schema ⊆ skill).
- `tests/contract/silent_pass_allowlist.txt` is a **ratchet** of legacy
  `except: pass`. New ones fail. Fix by logging, or regenerate deliberately:
  `PYTHONPATH=. .venv/bin/python scripts/gen_silent_allowlist.py`
  (then justify why silence is correct in review).
- `test_systemd_units.py` parses `configs/*.service`; unit list comes from
  `scripts/deploy.py: RESTART_SERVICES`. A new service must be added to **both**.
- `test_import_isolation.py` imports every service in a subprocess with `torch`
  blocked. `ML_SERVICES = {volatility_engine, pinn_trainer}` (in
  `tests/contract/unit_files.py`) may import it; everything else must not.

## 4. Adding or renaming a Redis field — 4 places, one PR

1. Producer (`lib/zerodha/tick_store.py` / `snapshot_publisher.py` /
   `signal_emitter.py`).
2. Consumer(s) — grep the field name across `services/` and `lib/`.
3. `tests/contract/schema.py` (declared field).
4. `.kilo/skills/options-data-contract/SKILL.md` (documented field).

Then `make test-layers`. `test_redis_schema` and
`test_skill_documents_every_declared_field` must pass.

## 5. Reading a failure

1. Read the assertion message — contract tests name the file and the field.
2. Reproduce one test: `PYTHONPATH=. .venv/bin/python -m pytest <file>::<test> -q`.
3. Property failure: the falsifying example is the input; decide whether the
   code or the property is wrong. Never widen the domain to hide a real bug;
   skipping inside `@given` is banned (use bounded strategies instead).
4. Integration failure with threads: check the exactly-once claim
   (`hd el` returned 1) before suspecting the P&L maths.
5. Smoke failure: run `make smoke` for the table; then
   `make server-svcs-status` and `journalctl -u <svc> -n 50`.

## 6. Current baselines (2026-11-23)

- 2,094 tests pass / 20 skipped; PR suite ~30 s.
- Coverage 58 % overall (`--cov=services --cov=lib --cov=common`).
  Not yet enforced; raise it, never lower it.
- `ws2_subs` must be ≤ 480 (`common/token_registry.py:
  WS_MAX_OPTION_SUBSCRIPTIONS`); >500 is rejected by Kite.

## 7. What CI does

`.github/workflows/ci.yml` (PR/push): lint changed `.py` files with ruff
(`--ignore E402`) → `pytest` with
`-m "not e2e and not slow and not perf and not smoke"` + coverage.
`.github/workflows/nightly.yml` (06:00 IST weekdays): `-m "e2e or slow or perf"`.

## 8. Definition of done for a change

- [ ] New behaviour has a test in the **lowest** layer that can fail for it
      (unit → contract → property → integration → e2e).
- [ ] `make test-layers` green and `ruff` clean on changed files.
- [ ] If a Redis field, unit file, or deploy list changed: contract tests updated.
- [ ] If it affects the server: `make deploy`, then `make smoke` green.
- [ ] `docs/testing-strategy.md` §11 records anything surprising you found.
