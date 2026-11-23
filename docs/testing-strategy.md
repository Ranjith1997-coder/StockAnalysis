# Test strategy — StockAnalysis

Status: **T0–T4 and T6 implemented; T5 (E2E simulator) design only** · 2026-11-23
Layer status: unit ✅ · contract ✅ · property ✅ · integration ✅ · e2e ✅ ·
smoke ✅ · perf ⏳
Audience: an implementing model working **one task at a time**. Every task below
is self-contained: goal, files, exact interfaces, steps, acceptance criteria and
the command that proves it. Do the tasks in the order of §10.

---

## 0. Why this exists

All ~92 current test files are unit tests with `MagicMock` Redis. Almost every
serious bug found in production in Nov 2026 slipped past them:

| Bug found live | Why unit tests missed it | Layer that catches it |
|---|---|---|
| WS2 held 542 subscriptions vs Kite's 500 cap | No test runs re-centring over a trending day | E2E simulation (T5) |
| data-gateway heartbeat TTL 120 s < 310 s cycle | Timing between two loops; mocks never age | Integration + clock (T4) |
| Orchestrator heartbeat used `time.time()` on `datetime.time` | Swallowed by `except: pass`, never asserted | Entry-point smoke (T1.4) |
| Logger format/arg mismatch | Only surfaced under strict logging | Static guard (done: `tests/test_logging_format.py`) |
| paper-trading crashed importing `torch` | Tests run where torch is installed | Import isolation (T1.2) |
| Wrong `ExecStart` / missing `-m` | Unit files are not tested | Unit-file rules (T1.3) |
| Producer/consumer field drift (`buy_qty` as price, `max_pain` key) | Each side mocked separately | Redis contract (T1.1) |
| VWAP crash when no rows for today | Fixtures always contained today | Property / scenario (T3, T5) |

---

## 1. Ground rules for every task

1. Read `CLAUDE.md` first. Use `PYTHONPATH=. .venv/bin/python -m pytest …`.
2. **Never change production behaviour in a test task.** If a seam is needed
   (T5.1), extract the existing code verbatim into a function and call it from
   the old place; add a test proving the old path still works.
3. Tests must be **deterministic**: fixed seeds, frozen/travelled clocks, no
   network, no real sleeps longer than 0.1 s. Use `time-machine` for time.
4. Never touch the production server, Redis on the server, or `.env`.
5. One task per commit. Before finishing: full suite green and
   `git diff --name-only -- '*.py' | xargs .venv/bin/ruff check`.
6. New helpers go in `tests/<layer>/…`; shared fixtures in the layer's
   `conftest.py`. Do not duplicate helpers from `tests/analyser/conftest.py` /
   `tests/conftest.py` — import them.
7. Every new test file starts with a docstring stating what it protects and
   which bug class it prevents.

---

## 2. Target layout and markers

```
tests/
  (existing unit tests stay where they are — do not move them)
  contract/      T1  schema, import isolation, unit files, entry points
  property/      T3  hypothesis invariants
  integration/   T4  fakeredis + several real components together
  e2e/           T5
    sim/           market generator, fake feed, harness, oracles
    scenarios/     *.yaml scenario specs
  smoke/         T6  post-deploy checks (run against the live server only on demand)
  perf/          T7  soak and latency budgets
```

Markers (register in `pytest.ini`, T0):

| Marker | Runs on | Budget |
|---|---|---|
| (none) = unit | every PR | whole PR job < 2 min |
| `contract` | every PR | fast |
| `property` | every PR | `max_examples=100` default, `settings(deadline=None)` |
| `integration` | every PR | < 60 s total |
| `e2e` | nightly + manual | < 10 min |
| `slow` / `perf` | nightly | < 30 min |
| `smoke` | manual / post-deploy | needs SSH; **never in CI** |

---

## 3. T0 — Tooling foundation (≈0.5 d)

**Goal:** dependencies, markers, coverage and CI split.

**Files**
- `requirements-dev.txt`: add `fakeredis==2.*`, `hypothesis==6.*`,
  `time-machine==2.*`, `pyyaml` (if not already present), keep `pytest-cov`.
  Pin exact versions that install on Python 3.12 and 3.13.
- `pytest.ini`: add
  ```ini
  markers =
      contract: schema/import/config contracts
      property: hypothesis property tests
      integration: multi-component tests on fakeredis
      e2e: market-simulation end-to-end scenarios
      slow: long-running tests
      perf: performance and soak tests
      smoke: live-server checks (manual only)
  ```
  Keep the existing `filterwarnings`.
- `.github/workflows/ci.yml`: PR job runs
  `pytest -m "not e2e and not slow and not perf and not smoke" --cov=services --cov=lib --cov=common --cov-report=term-missing:skip-covered`.
- `.github/workflows/nightly.yml` (new): `schedule: cron "30 0 * * 1-5"`
  (06:00 IST) + `workflow_dispatch`; runs `pytest -m "e2e or slow or perf"`.

**Acceptance**
- `pytest --markers` lists all markers.
- CI passes; coverage percentage is printed. Record the baseline number in this
  doc under §11.
- Do **not** add `--cov-fail-under` yet (T8 adds the ratchet).

---

## 4. T1 — Contract tests (≈2 d, highest value per hour)

### T1.1 Redis schema contract

**Goal:** a single source of truth for every shared Redis key, checked from both
sides.

**Files**
- `tests/contract/schema.py` — a dict `SCHEMAS`:
  ```python
  SCHEMAS = {
      "data:options_agg:{SYM}": {
          "type": "hash",
          "required": {"atm_strike", "live_pcr", "pcr_window_pct", "expiry",
                       "last_updated", "net_ce_oi_change", "net_pe_oi_change"},
          "optional": {"atm_iv", "atm_iv_percentile", "max_pain_strike", ...},
      },
      "data:options_live:{SYM}": {"type": "hash_of_json",
          "field_pattern": r"^\d+(\.\d+)?_(CE|PE)$",
          "json_required": {"ltp", "oi", "has_oi", "timestamp"},
          "json_optional": {"bid", "ask", "open_oi", "iv", "delta", ...}},
      "data:futures_live:{SYM}": {...},
      "pinn:zscore:{SYM}": {...},
      "service:registry:{SVC}": {"type": "hash",
          "required": {"pid", "last_heartbeat", "version"}},
  }
  ```
  Seed it from `.kilo/skills/options-data-contract/SKILL.md`; that skill and this
  file must stay in sync (add a test that the skill lists every key here).
- `tests/contract/test_redis_schema_producers.py` — run each **producer** against
  `fakeredis` and assert it writes only declared fields with the declared types:
  - `services/market_data/snapshot_publisher.py: SnapshotPublisher._publish_all()`
    using a `make_stock(... index=True)` whose `_tick_store` is filled via
    `TickStore.update_option_tick / update_futures_tick / update_zerodha_data`
    and `recompute_options_aggregate`.
  - Each service's heartbeat writer (`update_heartbeat`, `_update_beat`,
    `_touch_heartbeat`, `_write_orchestrator_heartbeat`).
- ~~Static AST consumer check~~ **replaced** after implementation: a literal-name
  AST scan produced too many false positives for too little signal. Consumer
  reads are validated **behaviourally** in T4 (`tests/integration/
  test_publisher_to_consumers.py` drives publisher → `check_entry_gate`/
  `get_pinn_confirmation`), and `test_skill_documents_every_declared_field`
  keeps the schema in sync with the contract skill in both directions.
  `tests/contract/test_static_rules.py` still catches undeclared `data:*` key
  families (typos).

**Acceptance:** renaming a field in the publisher without updating the schema
fails a producer test; reading an undeclared field fails a consumer test.

### T1.2 Import isolation

**File:** `tests/contract/test_import_isolation.py`

Run each entry module in a **subprocess** with a `sitecustomize.py` (written to
`tmp_path`, added to `PYTHONPATH`) that blocks `torch`:
```python
import importlib.abc, sys
class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name == "torch" or name.startswith("torch."):
            raise ImportError("torch blocked by test")
sys.meta_path.insert(0, _Block())
```
Then `python -c "import services.<svc>.main"` must succeed for every service
except an allow-list `ML_SERVICES = {"volatility_engine", "pinn_trainer"}`.

**Acceptance:** re-adding a module-level `from services.volatility_engine...`
import to paper trading fails this test.

### T1.3 Unit-file rules

**File:** `tests/contract/test_systemd_units.py`

For every `configs/stockanalysis*.service`:
1. Parse `ExecStart`. If it is `python -m pkg.mod`, assert
   `importlib.util.find_spec("pkg.mod")` is not None. If it is a file path,
   assert the file exists **and** contains no `^from \.` lines (relative imports
   require `-m`).
2. `Restart=always` units must appear in `scripts/deploy.py: RESTART_SERVICES`
   (import the list).
3. `WorkingDirectory`, `PYTHONPATH`, `EnvironmentFile` are present.
4. Units whose module imports torch (ML_SERVICES) have `MemoryMax >= 1G`.

### T1.4 Entry-point smoke

**File:** `tests/contract/test_entrypoints.py`

For every service module, in a subprocess with `REDIS_URL=redis://127.0.0.1:1`
(nothing listening), run `python -m services.<svc>.main` with a 10 s timeout and
assert it **exits cleanly with a clear "Cannot connect to Redis" error** — not a
`NameError`, `AttributeError`, `ImportError` or traceback. Capture stderr; fail on
`Traceback` unless the last line is the expected connection error.

Also: assert every heartbeat writer can run once against fakeredis without
raising (this is the test that would have caught the `time.time()` bug).

---

## 5. T2 — Static and configuration checks (≈0.5 d)

**File:** `tests/contract/test_static_rules.py`

- No `except Exception: pass` / bare `except: pass` in `services/` and `lib/`
  (allow-list via a `# noqa: silent-ok — <reason>` comment). This is the pattern
  that hid the orchestrator heartbeat bug for months.
- No module-level `datetime.time` shadowing: in files that do
  `from datetime import … time …`, forbid `time.time()` calls (AST check).
- Every Redis key literal (`"data:…"`, `"paper:…"`, `"pinn:…"`,
  `"service:registry:…"`) used in code is declared in `tests/contract/schema.py`
  or an explicit allow-list.

---

## 6. T3 — Property-based tests (≈1 d)

Use `hypothesis`. Mark the module `pytestmark = pytest.mark.property`.

| File | Function under test | Properties |
|---|---|---|
| `tests/property/test_costs.py` | `services/paper_trading/models.py: compute_transaction_costs` | total ≥ 0; STT only on SELL legs; costs monotonic in quantity; BSE vs NSE exchange rate applied by symbol; GST = 18 % of (brokerage + exchange + SEBI) within 1e-6 |
| `tests/property/test_exits.py` | `engine.stop_loss_amount`, `compute_max_loss`, `determine_exit_reason` | for defined-risk structures SL ≤ max loss; exit reason is deterministic for a given state; no reason when PnL inside bands |
| `tests/property/test_token_cap.py` | `TokenRegistry.recenter_and_get_subscription_changes`, `initial_subscribe_options` | for any random walk of spot across 3 symbols: subscribed ≤ `WS_MAX_OPTION_SUBSCRIPTIONS`; CORE tokens never evicted; unsubscribed tokens are not in mode_changes |
| `tests/property/test_option_math.py` | `lib/fno/option_math.py` (when I6 exists) | put-call parity; 0 ≤ P(ITM) ≤ 1; expected move ≥ 0 and increases with DTE |
| `tests/property/test_capture_roundtrip.py` | `ChainRecorder._write` | any generated rows round-trip through parquet with identical columns and dtypes across two flushes (schema stability) |

Building registries: reuse `make_option_info` from
`tests/common/test_token_registry.py` (move it to `tests/common/factories.py` and
import from both places).

---

## 7. T4 — Integration tests on fakeredis (≈2 d) — **implemented**

Files: `tests/integration/test_publisher_to_consumers.py`,
`test_service_liveness.py`, `test_paper_trading_state.py`,
`test_capture_flush.py`, `test_vol_engine_loop.py`. The `fake_redis` fixture
lives in `tests/conftest.py`; the `clock` fixture (time-machine) and
`a_trading_day()` helper in `tests/integration/conftest.py`. **Note:**
`common.market_calendar.is_trading_day()` performs an NSE HTTP fetch on first
use — tests must monkeypatch the service-local import, never rely on the
exchange calendar or the wall clock.


### T4.0 Fixtures (`tests/integration/conftest.py`)

```python
import fakeredis, pytest
from services.common.redis_proxy import RedisProxy

@pytest.fixture
def redis():
    proxy = RedisProxy.__new__(RedisProxy)          # skip real connect
    proxy._client = fakeredis.FakeRedis(decode_responses=True)
    return proxy
```
`RedisProxy.__init__` only does `redis.from_url(url, decode_responses=True)`, so
swapping `_client` is safe. If a method used by code is missing on the proxy,
add it to the proxy (production change → separate commit with a unit test).

Clock: `@pytest.fixture def clock(): with time_machine.travel(start, tick=False) as t: yield t` and use `t.shift(seconds)`.

### T4 tasks

| ID | Test | Wire together | Assert |
|---|---|---|---|
| T4.1 | publisher → consumers | `SnapshotPublisher._publish_all()` then `signal_router.get_pinn_confirmation`, `paper_trading.check_entry_gate` | consumers read published values; stale `last_updated` (shift clock 30 s) makes `check_entry_gate` refuse |
| T4.2 | heartbeat visibility | each service's heartbeat writer + `services/resource_monitor/main.py: discover_services` + `/version` command (`lib/notification/commands/system.py: cmd_version` with fake update/context) | every service visible for ≥ 3 × its loop interval when only its liveness writer runs; key expires after TTL when writer stops; resource-monitor raises offline alert |
| T4.3 | paper-trading concurrency | `persist_new_position`, `apply_mtm_result`, `persist_closed_position` from two threads | no position resurrected after close; realised P&L counted once; account margin consistent |
| T4.4 | daily reset | `get_account(redis, today=...)` across a date change with the clock | daily counters reset, cumulative kept, loss limit evaluated per day |
| T4.5 | capture over several flushes | `ChainRecorder` with fake index objects, 3 flushes | identical parquet schemas; manifest row counts equal written rows; retention prune leaves today |
| T4.6 | vol-engine loop | `volatility_engine.main._run_inference_iteration` with a stub model and fakeredis inputs | writes `pinn:zscore:{SYM}` with TTL; on expiry day (`tau <= 0`) writes nothing and does not raise |

---

## 8. T5 — E2E market simulation (≈6 d)

**Detailed design: `docs/e2e-simulator-design.md`** (architecture, cadences
verified from source, component interfaces, oracle formulas, fault catalogue,
task plan T5.1–T5.10).

A deterministic, in-process "trading day in seconds" that drives the **real**
code paths.

### T5.1 Step seams (production refactor, behaviour-neutral)

Extract one-iteration functions from loops so the harness can step them. Each
extraction: move the loop body verbatim into the new function; the loop calls it;
add a unit test that the loop still calls it.

| Module | New function | Extracted from |
|---|---|---|
| `services/paper_trading/main.py` | `mtm_once(redis, now)` | body of `mtm_engine` |
| `services/paper_trading/main.py` | `process_analysis_message(redis, fields)`, `process_confluence_message(redis, fields)` | bodies of `analysis_consumer`, `confluence_consumer` |
| `services/paper_trading/main.py` | `strategy_step(redis, span_calculator, signal)` | per-item body of `strategy_processor` (wraps `_handle_entry_signal`) |
| `services/market_data/snapshot_publisher.py` | already `_publish_all()` | — |
| `services/market_data/chain_recorder.py` | already `_capture_*`, `_flush_all` | — |
| `services/volatility_engine/main.py` | already `_run_inference_iteration` | — |
| `services/analysis_engine/worker.py` | already `process_job(...)` | — |
| `services/signal_intelligence/…` | `handle_signal(redis, correlator, fields)` | body of the stream consumer in `main.py` |

### T5.2 Market generator — `tests/e2e/sim/market.py`

```python
@dataclass
class MarketState:
    t: datetime; spot: float; vix: float; fut: float

class MarketGenerator:
    def __init__(self, scenario: Scenario, seed: int): ...
    def step(self, dt_seconds: int) -> MarketState: ...
    def option_ticks(self, state, strikes, expiry) -> dict[float, dict[str, dict]]:
        """Black-Scholes price per strike/side from an IV smile, plus
        bid = price*(1-s/2), ask = price*(1+s/2), OI drifting with a
        configurable build-up model, volume, timestamp."""
```
- Spot: GBM with `drift`, `vol`, optional jumps at scheduled times, and an
  opening gap.
- IV smile: `atm_iv + skew * k + smile * k**2` (k = ln K/F), shifted by VIX path.
- OI: base OI per strike decaying from ATM; per step add build-up per
  `scenario.oi_flow` (e.g. `{"pe_writing": 0.02}` per minute near ATM).
- Must reuse a single BS implementation (`tools/pinn_volatility/model/bs_utils.py`
  or `lib/fno/option_math.py` once it exists). Do not write a new BS.

### T5.3 Fake broker feed — `tests/e2e/sim/feed.py`

- Feeds ticks into the real objects: `stock._tick_store.update_zerodha_data`,
  `update_option_tick(strike, side, tick)`, `update_futures_tick("current", tick)`,
  then `recompute_options_aggregate(spot)`.
- Owns a real `TokenRegistry`, calls `recenter_and_get_subscription_changes` as
  spot moves, and **enforces Kite's limit**: if a subscribe would exceed 500 the
  feed raises/records `SubscribeRejected`, exactly like production. Only strikes
  currently subscribed receive ticks.

### T5.4 Harness — `tests/e2e/sim/harness.py`

```python
class Harness:
    def __init__(self, scenario, redis, seed=0): ...
    def run(self) -> RunResult:
        for each simulated second from scenario.start to scenario.end:
            clock.move_to(t)
            feed.push(market.step(1))
            every 1 s : publisher._publish_all(); paper mtm_once(redis, now)
            every 1 s : recorder._capture_futures(now)
            every 30 s: recorder._capture_chain(now)
            every 300 s: analysis process_job(...) for each index → publish results
                         → process_analysis_message / signal-intelligence handle_signal
                         → strategy_step for queued signals
            every 3 s : vol-engine _run_inference_iteration (if a model fixture is given)
        return RunResult(positions, trades, logs, metrics, redis snapshot)
```
- Use fakeredis, `time-machine` for `now`, and capture logs with `caplog` at
  WARNING. No threads.
- Fast mode: allow `step_seconds=5` for CI speed; scenario files may override.

### T5.5 Scenario spec — `tests/e2e/scenarios/*.yaml`

```yaml
name: trend_day_up
date: 2026-11-24          # a Tuesday that is a trading day in the calendar
symbols: [NIFTY, BANKNIFTY, SENSEX]
start: "09:15"
end:   "15:30"
step_seconds: 5
seed: 7
spot: {NIFTY: 25500, BANKNIFTY: 63300, SENSEX: 83200}
drift_pct_per_day: 2.0
vol_annual: 0.14
gap_pct: 0.0
vix: {start: 14, end: 17}
iv_smile: {atm: 0.13, skew: -0.08, smile: 0.4}
oi_flow: {ce_writing: 0.01, pe_writing: 0.03}
expiry: {NIFTY: 2026-11-25, BANKNIFTY: 2026-11-25, SENSEX: 2026-11-27}
faults: []                # see fault catalogue
oracles: [no_errors, subs_within_cap, capture_coverage, entry_gates, pnl_reconciles]
```

Fault catalogue (each is a function in `tests/e2e/sim/faults.py`):
`feed_gap(start, seconds)`, `stale_aggregate(seconds)`, `sensibull_5xx(rate)`,
`redis_down(start, seconds)`, `restart_service(name, at)`, `clock_jump(seconds)`,
`disk_low(at)`.

### T5.6 Oracles — `tests/e2e/sim/oracles.py`

Each returns `(ok: bool, detail: str)`.

| Oracle | Check |
|---|---|
| `no_errors` | no ERROR/Traceback in captured logs (allow-list per fault) |
| `subs_within_cap` | registry subscribed count ≤ `WS_MAX_OPTION_SUBSCRIPTIONS` at every step; zero `SubscribeRejected` |
| `chain_follows_spot` | at every step the ATM strike is subscribed for each symbol |
| `capture_coverage` | manifest chain snapshots ≥ 99 % of expected for the session span |
| `entry_gates` | no entry before 09:20, after 15:00, after 14:00 on expiry day, or on stale data |
| `exits_valid` | every closed position has an exit reason; none closed twice; expired positions settled |
| `pnl_reconciles` | P&L recomputed independently from trades + `compute_transaction_costs` equals ledger within ₹1 |
| `risk_limits` | open max loss ≤ configured limit; positions per index ≤ limit |
| `heartbeats_present` | every service heartbeat exists at every 60 s check |

### T5.7 Scenario library (each = one pytest case, `@pytest.mark.e2e`)

| Scenario file | Validates |
|---|---|
| `range_day.yaml` | IC entries, theta/target exits, daily reset |
| `trend_day_up.yaml` / `trend_day_down.yaml` | re-centring under the cap, stop-loss paths, stand-down (when S9 exists) |
| `gap_open_down.yaml` | gap handling, GIFT gap signal (when X10 exists) |
| `expiry_day.yaml` | `tau=0` PINN skip, 14:00 entry block, expiry settlement |
| `feed_gap.yaml` | freshness gate blocks entries; recovery after gap |
| `sensibull_outage.yaml` | degraded but no crash; no false signals |
| `restart_midday.yaml` | `open_oi` baseline behaviour documented; no resurrected positions |
| `holiday.yaml` | nothing trades, no capture, no alerts |

Test file: `tests/e2e/test_scenarios.py` parametrised over the YAML files.

### T5.8 Replay mode

`tests/e2e/sim/replay.py`: a `ReplayGenerator` with the same interface as
`MarketGenerator` that reads a captured day from
`data/options_history/{chain,context,futures,index}/…` (format:
`docs/options-data-capture.md`). Real days become regression tests. Skip the test
(`pytest.skip`) when the data directory is absent so CI stays green.

---

## 9. T6–T8 — Smoke, resilience/perf, coverage ratchet

### T6 Post-deploy smoke (`tests/smoke/`, marker `smoke`, manual only)

Script `scripts/smoke_check.py` (used by `/verify-session` and optionally at the
end of `scripts/deploy.py`): over SSH, check services active, heartbeat ages
< 120 s, `options_agg.last_updated` age < 10 s during market hours,
`ws2_subs ≤ 480`, capture manifest written today, zero `Traceback` in the last
5 min of each unit's journal. Exit non-zero with a table on failure.
Pytest wrapper `tests/smoke/test_live.py` calls the same functions; it is skipped
unless `RUN_SMOKE=1`.

### T7 Resilience + performance (`tests/perf/`, nightly)

- **Soak:** run the harness for a full session at `step_seconds=1`; assert
  `_publish_all` p99 < 200 ms, memory growth < 50 MB over the run
  (`tracemalloc`), recorder flush time < 2 s.
- **Chaos:** the fault catalogue applied to `range_day` one fault at a time;
  every run must satisfy `no_errors` (with the fault's allow-list) and recover
  (`heartbeats_present`, `chain_follows_spot` after the fault window).

### T8 Coverage ratchet

After T1–T4: set `--cov-fail-under=<baseline rounded down>` in the PR job and add
a check that the number in this file (§11) only increases.

---

## 10. Order of work

| Order | Task | Depends on | Effort |
|---|---|---|---|
| 1 | T0 tooling | — | 0.5 d |
| 2 | T1.2 import isolation, T1.3 unit files, T1.4 entry points | T0 | 1 d |
| 3 | T1.1 Redis schema contract | T0 | 1 d |
| 4 | T2 static rules | T0 | 0.5 d |
| 5 | T3 property tests | T0 | 1 d |
| 6 | T4 integration | T0 | 2 d |
| 7 | T5.1 seams | T4 | 1 d |
| 8 | T5.2–T5.6 simulator + oracles + `range_day`, `trend_day_up`, `expiry_day` | T5.1 | 3 d |
| 9 | T5.7 remaining scenarios, T5.8 replay | T5.6, captured data | 2 d |
| 10 | T6 smoke script | — | 1 d |
| 11 | T7 soak/chaos, T8 ratchet | T5 | 1.5 d |

Handing a task to a model: give it this file, `CLAUDE.md`, and the task ID only.
Ask it to finish with the acceptance checklist and the test command output.

---

## 11. Records (update as tasks land)

| Date | Task | Result |
|---|---|---|
| 2026-11-23 | baseline | 1916 unit tests, coverage not measured |
| 2026-11-23 | T0 | deps pinned (fakeredis 2.33.0, hypothesis 6.152.9, time-machine 3.2.0); 7 markers; CI split + `nightly.yml` |
| 2026-11-23 | T1 | 102 contract tests; schema for 4 data keys + registry; import isolation; unit-file rules; no-Redis startup; heartbeat writers |
| 2026-11-23 | T2 | silent-except ratchet (51 legacy sites frozen); `time` shadowing; `data:*` family typos |
| 2026-11-23 | T3 | 24 property tests (costs, exits, subscription cap, parquet schema) |
| 2026-11-23 | T4 | 23 integration tests (publisher→consumers, heartbeat TTL, exactly-once close, daily reset, capture flush, PINN loop) |
| 2026-11-23 | T6 | `scripts/smoke_check.py` + `make smoke` + `tests/smoke/test_live.py` (opt-in) |
| 2026-11-23 | total | **2094 passed, 20 skipped, ~30 s**; coverage **58 %** |

### Analyser audit (2026-11-23)

Question asked: *is every analyser properly tested?* Measured, not assumed.

**Method.** `pytest --cov=services.analysis_engine.analyser` over the full suite,
then mapped missing lines to methods with an AST pass, and cross-checked which
methods are actually registered (`BaseAnalyzer.intraday/both/index_*`) and used
by production callers.

**Answer: no.** Every analyser has a test file (17 files, 393 tests at audit
time), but coverage per public analysis method ranged from 8 % to 100 %.

Statement coverage of the package: **63 % → 68 %** after the tests added below.

| File | Before | After | Note |
|---|---|---|---|
| TechnicalAnalyser | 64 % | 69 % | 52-week was 8 % (never asserted) |
| MaxPainAnalyser | 42 % | 59 % | alignment never asserted |
| candleStickPatternAnalyser | 46 % | 80 % | all 3 reversal analysers untested |
| Futures_Analyser | 48 % | 48 % | **largest remaining gap** |
| OIChainAnalyser | 55 % | 55 % | **second largest** |
| MessageFormatter | 53 % | 53 % | 2 formatters at 0 % |
| IVAnalyser | 59 % | 75 % | iv-vs-hv zones untested |
| LiveOIAnalyser | 58 % | 82 % | wall breach untested |
| LiveStraddleAnalyser | 73 % | 81 % | IV trend untested |
| GEX / PCR / Panic / Volume / Analyser base | 78-86 % | unchanged | reasonable |
| **package total** | **63 %** | **68 %** | |

**Weak-test pattern found:** 12 tests wrapped their only assertion in
`if result:` and 7 asserted `isinstance(result, bool)`. Those pass whether or not
the analyser ever emits a signal. The four TechnicalAnalyser ones were replaced
with verified-signal tests; **8 `if result:` and 5 `isinstance` remain** in
Futures (2/1), OIChain (3/2), IV (1), PCR (1), Volume (1/2) — see backlog.

**Tests added (38):**
- Technical: two-candle Bollinger breakout, EMA cross with ADX gate (both
  directions of the gate), stochastic oversold cross, supertrend reversal,
  52-week high/low/no-signal (real `priceData`, no mocks).
- MaxPain: ALIGNED / DIVERGENT / undetermined / missing-type.
- LiveOIAnalyser: CE and PE wall-breach, stable OI, wall too far, bad spot.
- LiveStraddleAnalyser: IV rising, falling, flat, sub-threshold, short history.
- Candle: hammer, shooting star, bullish/bearish engulfing, morning/evening star,
  plus trend-gate negatives.
- IVAnalyser: EXTREME, EXPENSIVE, FAIR, CHEAP, zero IV, short history.

**Open questions raised (not changed):**
1. `analyse_ema_crossover` requires `slow_slope >= 0` for a bullish cross (and
   `<= 0` for bearish). A dip-then-recover and its mirror almost never satisfy
   this: a 1 000-combination parameter sweep of piecewise trends produced no
   cross, and the bearish mirror of the fixture that does work does not fire.
   Worth deciding whether the gate is intended.
2. `analyse_is_52_week` appends the stock to the process-global
   `shared.ticker_52_week_high_list` / `_low_list`; tests must snapshot/restore.

**Backlog (priority order):**
1. `Futures_Analyser` positional paths: price/volume/OI pattern, positional OI
   trend, cost of carry, rollover pressure (46-53 % each).
2. `OIChainAnalyser`: `analyse_intraday_oi_sr_shift` (51 %),
   `analyse_oi_capitulation` (63 %), `analyse_oi_wall_migration` (64 %).
3. `MessageFormatter`: `_fmt_future_breakout` and `_fmt_future_oi_trend` are at
   0 %; ~40 formatters are partially covered.
4. `OptionSellerCompositeAnalyser.detect_gamma_trap_warning` (69 %).
5. Replace the remaining `if result:` / `isinstance` assertions with
   verified-signal assertions.

#### Second pass — the ranked backlog (same day)

| Item | Before | After | What was added |
|---|---|---|---|
| `Futures_Analyser` | 48 % | **77 %** | PVO pattern (long-buildup, divergence, 1-row), positional OI trend (all 4 quadrants + expiry suppression), cost of carry (backwardation, high CoC, basis-expanding, unpopulated spot), rollover (ACTIVE, STARTING, no/zero next) |
| `OIChainAnalyser` | 55 % | **79 %** | capitulation (call→bullish, put→bearish, small unwinding ignored), wall migration (put wall raised, unchanged), intraday S/R shift (resistance up, support down, static, short history) |
| `MessageFormatter` | 53 % | **59 %** | `FUTURE_BREAKOUT_PATTERN` and `FUTURE_OI_TREND` (were 0 %) — emoji, ORB range, confirm tags, score/MTF suffix, optional 20d windows |
| `OptionSellerCompositeAnalyser` | 72 % | **76 %** | gamma trap via wall-migration proxy, RETREAT is not a breach, stock volume path |
| `MaxPainAnalyser` | 59 % | **76 %** | positional convergence (BEARISH), intraday convergence via future-price columns, no-history |
| `VolumeAnalyser` | 78 % | **87 %** | buying/selling climax, bearish OBV divergence, sub-threshold cases |
| weak assertions | 8 `if result:` + 6 `isinstance` | **0** | all replaced with verified signal assertions (long/short buildup, IV DOWNWARD, PCR neutral transition, PVO, OI trend, OBV, climax, max-pain trend) |
| **package total** | **63 %** | **77 %** | 2,181 tests (+86 from the first pass) |

Lessons encoded in the new fixtures:
- `analyse_oi_wall` uses mean + 2σ, so a 5-strike fixture with one 7× outlier
  fails by a hair; the working fixture needs 9 baseline strikes.
- `make_oi_chain` sets `prev_ltp = spot × 0.99`; wall-migration tests must
  override it or the yesterday-wall distance gate is wrong.
- Cost-of-carry basis sign: `underlying = close × (1 − basis_gap)`, so
  **negative** gap = futures below spot = backwardation.
- `analyse_max_pain_trend` positional path reads `oi_history` (not
  `historical_data`); the intraday path needs `max_pain_<YYYYMMDD>` +
  `future_price_<YYYYMMDD>` columns.

**Remaining (next pass):**
1. `TechnicalAnalyser` 69 %: `analyse_vwap` 52 %, `analyse_atr` 62 %,
   `analyse_rsi_divergence` 64 %, `analyze_buy_sell_quantity` 60 %.
2. `MessageFormatter` 59 %: ~40 formatters partially covered.
3. Branch depth in `OIChainAnalyser` (79 %) and `Futures_Analyser` (77 %):
   acceleration, retreat, conflicting-wall and S/R-squeeze paths.

#### Third pass — remaining low modules closed

| Module | Before | After | Added |
|---|---|---|---|
| `TechnicalAnalyser` | 69 % | **83 %** | VWAP above/below (52→), ATR expansion, buy/sell dominance, RSI divergence both directions (64→) |
| `MessageFormatter` | 59 % | **97 %** | `test_message_formatter_bulk.py`: AST-discovered keys × two trends with a synthetic payload (128 render cases), plus exact-text tests for cost-of-carry, PCR bias, RSI, future action, OI acceleration/capitulation, max pain, GEX imbalance |
| `OIChainAnalyser` | 79 % | **85 %** | `test_oi_chain_branches.py`: put buildup, put-only/both/equidistant walls, put-writing imbalance, abnormal call writing below price, call-wall LOWER, RETREAT, put/PCR bullish intraday trend, S/R both-down, squeeze, widening |
| `Futures_Analyser` | 77 % | **79 %** | short-covering and long-unwinding quadrants; `analyse_intraday_oi_buildup_from_open` all four actions + flat/short-session |
| **package total** | **77 %** | **83 %** | 2,347 tests (+166) |

Notes for the next implementer:
- `get_dynamic_thresholds` scales with recent OI volatility: a ±2 % move can be
  below threshold. Use ±5 % in FUTURE_ACTION fixtures.
- `analyse_intraday_oi_buildup_from_open` caches `open_oi` per symbol/date in
  analyser state — use a fresh analyser per test, or the second call sees the
  first call's baseline.
- The bulk formatter test proves reachability and registration, not wording;
  wording is pinned by the exact-text class in the same file.

**Remaining long tail (all ≥75 %):** IVAnalyser 75 %, MaxPain 76 %,
OptionSellerComposite 76 %, LiveStraddle 81 %, LiveOI 82 %, Volume 87 %,
GEX 85 %, PCR 86 %, Panic 84 %, base Analyser 82 %.

#### Fourth pass — untested producers + ratchet

| Item | Before | After | Notes |
|---|---|---|---|
| `lib/zerodha/futures_fetcher.py` | **0 %** | **80 %** | 16 tests: spot-map basis, IST normalisation, rate-limit retry/give-up, positional next-expiry, intraday candle matching |
| `services/data_gateway/sensibull_fetcher.py` | **7 %** | **31 %** | 30 tests: max pain, IV percentile bands, historical-row flattening, Redis publish/history caps/5-day filter, deserialize round trip |
| repo total | 63.6 % | **65 %** | 2,393 tests |
| T8 ratchet | absent | **on** | CI fails below 60 % |
| nightly job | selected 0 tests | runs everything except `smoke` | no longer vacuous |

Production fixes found by these tests:
1. `FuturesFetcher._candles_to_df([])` raised `KeyError: 'date'` on an
   empty history — callers guard today, but the helper now returns an empty frame.
2. `sensibull_fetcher` used `pd.read_json(<literal str>)`, which pandas
   deprecates; now `io.StringIO`. Removed two dead locals and two unused imports.

Still open in the producer: the HTTP paths of `fetch_sensibull_data`,
`fetch_sensibull_oi_chain`, `fetch_iv_chart`, `fetch_oi_history` and the two
cycle runners (31 % remaining). Next step is a mocked-`requests` payload test
for each, using the shapes now pinned by the publish tests.

#### Fifth pass — Sensibull HTTP paths + E2E design

| Item | Before | After | Notes |
|---|---|---|---|
| `sensibull_fetcher.py` | 31 % | **84 %** | 22 tests: stock_info parse, 404 → OI-chain+IV fallback, 429 retry, timeouts, OI chain field mapping, IV chart sort/skip, OI history sort/dropna, sequential + parallel cycle failure isolation and positional day-cache |
| repo total | 65 % | **66 %** | 2,415 tests |
| E2E | prose only | **`docs/e2e-simulator-design.md`** | implementable design + T5.1–T5.10 task plan |

Production fix: the fallback took the newest **date** in `iv_ohlc` even when its
`iv` was null (today's chart entry exists but is unpopulated), nulling `atm_iv`
for the whole cycle. It now walks back to the newest non-null IV.

#### Sixth pass — E2E implemented end to end

| Item | Result |
|---|---|
| T5.1 seams | `mtm_once`, `drain_signal_queues`, `process_analysis_fields`, `process_confluence_fields`, `process_pinn_fields`, `handle_signal`; 11 seam tests |
| T5.2–T5.7 | scenario loader/validation, deterministic generator, feed with the 480 cap, Sensibull context builder, harness, 10 oracles + 20 oracle self-tests |
| T5.8/T5.9 | `range_day`, `trend_day_up` (stop-loss exit), `expiry_day` (14:00 cutoff), `holiday`; 4 scenarios green in ~9 s |
| T5.10 | replay reader deferred until captured parquet exists |
| suite | **2,464 passed** (`-m "not smoke"`, includes E2E) · repo coverage **67 %** |

Findings (fixed): cost-of-carry crashed on a string expiry from Redis;
`process_job` computed `ltp` before hydrating the live tick; `publish_to_redis`
dropped an empty caller context; `RedisProxy.xrange` was missing; pandas
literal `read_json` deprecation.

#### Seventh pass — E2E hardening

| Fix | Evidence |
|---|---|
| Log capture attached to every `SA*` logger (propagate=False hid everything) | injected in-run service error now fails `no_errors` |
| Canonical `engine.option_field_key` + float strikes | `trend_day_up` closes **STOP_LOSS −₹2 839** (was a fake **+₹792 THETA_DECAY** profit from stale short legs) |
| New oracles: `legs_priced_from_market`, heartbeat age-vs-TTL, `expect_exit_reason` | 20+ oracle self-tests |
| Scenarios 4 → 8: `cap_pressure`, `feed_gap`, `sensibull_outage`, `restart_midday` | `cap_pressure` reaches 480/480 and evicts; feed-gap entry rejected; no errors |
| Fault mechanics: `clock_jump`, `disk_low` + unit tests | fault-mechanics test class |
| Multi-seed nightly (`E2E_MULTI_SEED=1`, 3 seeds) | PR stays 1 seed |
| Suite | **2 473 passed** PR · **8 E2E scenarios / 50 E2E tests** in ~20 s |

### Findings from implementation

1. **notification-service crashed with a bare traceback when Redis was down** —
   no startup ping; the failure was masked by an `except: pass` around
   `xgroup_create`. Fixed: `rc.ping()` guard with a clear message and exit 1
   (also removed 3 pre-existing lint errors in that file).
2. Three services (paper-trading, volatility-engine, pinn-trainer) log
   `exc_info=True` on startup failure, so their output contains a traceback
   *after* the clear message. Accepted: exit code and message are correct;
   `test_entrypoints` asserts the message, not the absence of a traceback.
3. **Doc drift found:** the publisher mirrors `gex_*` fields into
   `data:options_agg`, while the contract skill said GEX lives only in
   `data:gex_state`. Skill updated to document the stale mirror.
4. `common.market_calendar.is_trading_day()` hits the NSE API on first call —
   a hidden network dependency for any test that gates on trading days.
5. Futures depth fields (`current_buy_px_1..5` etc.) are now contract-tested;
   adding a level or renaming a field fails immediately.
