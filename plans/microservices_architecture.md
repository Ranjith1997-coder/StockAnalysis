# StockAnalysis — Distributed Microservices Architecture Design

> **Last Updated**: June 2026
> **Implementation Status**: Phase 1A COMPLETE. Notification-service + data-gateway extracted. Monolith is always-running (24/7, `Restart=always`) with self-scheduling daily loop. Cycle sync via Redis Pub/Sub + stream. Parallel Sensibull fetch (10 workers). Unified logging. No systemd timers or auth service. See `docs/DESIGN.md` and `README.md` for current state.
> **Purpose**: Complete design for decomposing the monolithic StockAnalysis application into independently scalable services, solving the 12 PM thread-pool saturation stall and enabling horizontal scaling.
> **Constraint**: Initial deployment on a **single spare laptop** (Intel i5-6200U, 2 physical cores / 4 threads, 8 GB RAM, Ubuntu 24.04). Must scale out to additional machines later **without code changes** — just by changing `REDIS_URL` and starting services on the new node.
>
> **Note**: Sections below describe the ORIGINAL plan. The systemd topology (auth service, timers) has been superseded by the always-running architecture. Phase 1A implementation differs from the original plan — see implementation notes inline.

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Design Principles](#2-design-principles)
3. [Technology Stack](#3-technology-stack)
4. [Service Topology](#4-service-topology)
5. [Service Definitions](#5-service-definitions)
6. [Communication Contracts](#6-communication-contracts)
7. [Redis Data Schema](#7-redis-data-schema)
8. [Data Flow Diagrams](#8-data-flow-diagrams)
9. [Scaling Strategy](#9-scaling-strategy)
10. [Deployment Strategy](#10-deployment-strategy)
11. [Reliability & Fault Tolerance](#11-reliability--fault-tolerance)
12. [Observability](#12-observability)
13. [Migration Path](#13-migration-path)
14. [Service Stub Specifications](#14-service-stub-specifications)

---

## 1. Problem Statement

### Current Monolith Issues

The current system runs as a single Python process (`intraday/intraday_monitor.py`) with all responsibilities interleaved:

| Issue | Root Cause | Symptom |
|-------|-----------|---------|
| **12 PM stall** | 20-worker `ThreadPoolExecutor` saturated by blocking Sensibull HTTP calls | Process freezes, no analysis runs, ticks dropped |
| **No independent scaling** | I/O-bound data fetching, CPU-bound analysis, and real-time tick processing share one process and one thread pool | Can't add more CPU for analysis without adding more I/O workers |
| **Single point of failure** | Crash in any analyser kills WebSocket, data fetching, notifications, and the bot | Full system outage from one bug |
| **Memory fragmentation** | `pd.concat()` churn, `oi_chain_history` list slicing, 500KB JSON payloads in gen-2 GC | Unpredictable 5-15s full GC pauses |
| **Enctoken expiry disruption** | 403 re-auth runs subprocess, kills WebSocket, re-subscribes everything in the same process that's also doing analysis | Tick loss + analysis disruption simultaneously |
| **Tight coupling** | `shared.app_ctx` global singleton holds Stock objects, WebSocket manager, SignalBus, Correlator, Narrator all in one namespace | Can't test or deploy any component in isolation |

### Goals

1. **Independent scaling**: Add more analysis workers without adding more data fetchers
2. **Fault isolation**: A crash in the analysis engine doesn't kill the WebSocket feed
3. **Resource separation**: I/O-bound, CPU-bound, and real-time work in separate processes
4. **Zero-downtime deployment**: Update one service without restarting the whole system
5. **Observable**: Per-service health, throughput, latency, and error rates
6. **Backward compatible**: Existing Telegram bot commands, alert format, and analyser logic preserved

---

## 2. Design Principles

| Principle | Application |
|-----------|-------------|
| **Service per resource type** | I/O services, CPU services, and real-time services are separate processes so OS scheduling isolates them |
| **Redis as the backbone** | Single external dependency for pub/sub, streams, and shared state. Fits 4-core/8GB constraint (Redis uses ~50MB) |
| **Idempotent consumers** | Stream consumers can re-process messages safely (analysis results overwrite, don't append) |
| **Backpressure via streams** | Redis Streams with consumer groups + XCLAIM for stuck messages. No unbounded in-process queues |
| **Shared schema, not shared code** | Each service has its own `pyproject.toml` but imports a shared `common/` package for data contracts |
| **Health-first** | Every service exposes `/health` and writes heartbeat to Redis. Dead-man's switch per service |
| **Config via env, not shared globals** | `shared.app_ctx` replaced by Redis-backed state. Services read their config from env vars only |

---

## 3. Technology Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| **Message broker / shared state** | Redis 7.x (Streams + Pub/Sub + Hash/JSON) | Lightweight (~50MB RAM), single binary, fits 4-core server. Streams provide reliable delivery with consumer groups. No separate broker needed |
| **Inter-service communication** | Redis Streams (reliable) + Redis Pub/Sub (fire-and-forget) | Streams for analysis jobs, results, notifications. Pub/Sub for real-time tick broadcast |
| **Service framework** | Plain Python asyncio + `redis.asyncio` | No heavy framework (no Celery, no FastAPI for internal services). Keeps memory low. Each service is a `while True` loop reading from Redis |
| **HTTP API (bot + health)** | `aiohttp` or `uvicorn + Starlette` (only for bot-service) | Only the bot-service needs HTTP (Telegram webhook). Other services are Redis-only |
| **Process management** | `systemd` (Phase 1) → Docker Compose (Phase 2) → K8s (Phase 3) | systemd for 4-core server. Docker when adding a second node. K8s for multi-node |
| **Service discovery** | Redis key `service:registry:{name}` with TTL heartbeat | No Consul/Eureka needed for 2-5 services |
| **Monitoring** | Redis-backed health checks + structured JSON logs to stdout | systemd journal captures logs. No separate monitoring infra in Phase 1 |
| **Language** | Python 3.13 (same as current) | No language change. Existing analysers ported as-is |

### Why Redis Instead of RabbitMQ / Kafka

| Factor | Redis | RabbitMQ | Kafka |
|--------|-------|----------|-------|
| RAM footprint | ~50MB | ~150MB | ~512MB+ |
| Setup complexity | 1 binary | Erlang runtime | JVM + ZooKeeper |
| Stream semantics | Consumer groups, XCLAIM, pending entries | Channels, prefetch | Partitions, consumer groups |
| Shared state | Yes (Hash/JSON) | No | No |
| Fit for 4-core/8GB | Excellent | OK | Too heavy |

---

## 4. Service Topology

### Phase 1 — Single Laptop (i5-6200U, 2 cores/4 threads, 8 GB RAM)

**The reality**: Your spare laptop is the only machine. All 7 services + Redis run on it. The design must be lean enough to not OOM-kill during peak market hours, but structured so that when you add a second machine, you just move services across — zero code changes.

**Two deployment modes on the same laptop:**

| Mode | When to use | Process count | RAM |
|------|-------------|---------------|-----|
| **Compact** (default) | Normal market days, single laptop | 5 processes | ~3.5 GB |
| **Full** | When you need bot + intelligence on same machine | 8 processes | ~5 GB |

#### Compact mode (recommended for single laptop)

Consolidates the 3 lightest services into a single process with separate asyncio tasks. Same code, same Redis streams — just one process entry point that boots all three. This halves context-switch overhead on a 4-thread CPU.

```
┌─────────────────────────────────────────────────────┐
│                  LAPTOP (single node)                 │
│                                                       │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │  Redis 7 │  │ data-gateway │  │ analysis-engine│  │
│  │ (~50 MB) │  │  (WS + HTTP) │  │   (1 worker)   │  │
│  └────┬─────┘  └──────┬───────┘  └───────┬────────┘  │
│       │               │                  │           │
│       │    ┌──────────┴──────────────────┘           │
│       │    │                                        │
│       ▼    ▼                                        │
│  ┌──────────────────────┐  ┌─────────────────────┐  │
│  │  orchestrator +      │  │  notification +     │  │
│  │  intelligence + bot  │  │  (merged process)   │  │
│  │  (merged process)    │  │                     │  │
│  └──────────────────────┘  └─────────────────────┘  │
│                                                       │
│  auth-service runs as systemd oneshot (cron-like)     │
└─────────────────────────────────────────────────────┘
```

5 processes: Redis, data-gateway, analysis-engine, merged-coordinator (orchestrator+intelligence+bot), notification-service.

#### Full mode (when adding features or debugging)

All services as separate processes. Use this when debugging a specific service or when you have the second machine and can spread the load.

```
┌─────────────────────────────────────────────────────┐
│                  LAPTOP (single node)                 │
│                                                       │
│  Redis ─── data-gateway ─── orchestrator              │
│              │                │                       │
│              │          analysis-engine@1             │
│              │                │                       │
│              │     intelligence-service               │
│              │                │                       │
│              │     notification-service               │
│              │                │                       │
│              │          bot-service                   │
│              │                                        │
│         auth-service (oneshot/timer)                  │
└─────────────────────────────────────────────────────┘
```

### Resource Allocation (Single Laptop — 8 GB RAM)

| Service | Threads | RAM Budget | Process Count | Notes |
|---------|---------|------------|---------------|-------|
| Redis | 1 | 128 MB | 1 | `maxmemory 128mb`, LRU eviction |
| data-gateway | 1+2 WS | 1.2 GB | 1 | Largest: holds TickStore, option chain, WS buffers |
| analysis-engine | 1 | 600 MB | 1 worker | `MemoryMax=800M` in systemd |
| merged-coordinator | 1 | 500 MB | 1 | orchestrator + intelligence + bot in one process |
| notification-service | 1 | 200 MB | 1 | Lightweight: just reads streams, sends HTTP |
| auth-service | — | 50 MB | 0 (oneshot) | Runs only at boot / on 403 |
| **OS + overhead** | — | 2 GB | — | Ubuntu desktop + background |
| **Total** | **~5** | **~4.7 GB** | **5** | **3.3 GB headroom for spikes** |

> The i5-6200U has 4 hardware threads. With 5 processes doing mostly I/O (waiting on Redis/network), the OS scheduler handles this well. The CPU bottleneck is analysis-engine during the 5-min cycle burst — that's the only service that spikes to 100% on one core for ~2 seconds per stock.

### Laptop-Specific Operational Concerns

| Concern | Mitigation |
|---------|-----------|
| **Thermal throttling** | i5-6200U throttles at 80°C. analysis-engine bursts are short (2s/stock). Set `CPUQuota=75%` in systemd to leave thermal headroom. If laptop has no active cooling, reduce to 1 analysis worker |
| **Lid close / suspend** | Disable suspend when on AC: `systemd-inhibit --what=handle-lid-switch` or set `HandleLidSwitch=ignore` in `/etc/systemd/logind.conf` |
| **Not always-on** | Laptop may be powered off overnight. auth-service + orchestrator are triggered by systemd timers at 9:00 AM. If laptop is off at 9 AM, nothing runs — this is acceptable (same as current monolith behavior) |
| **WiFi instability** | Use ethernet if possible. If WiFi drops, data-gateway auto-reconnects WS. Other services are unaffected (they talk to Redis on localhost) |
| **Disk I/O** | Laptop HDD/SSD is fine — Redis is configured `save ""` (no disk persistence, all in-RAM). Logs go to systemd journal (ramfs) |
| **Battery / power outage** | If laptop dies, all services die. systemd `Restart=always` brings them back when power returns. Redis loses in-memory state (acceptable — next cycle refetches everything) |

### Phase 2 — Add a Second Machine (any old PC/laptop/mini-PC)

**When you're ready**: Buy or repurpose any machine (even a Raspberry Pi 4 with 4GB RAM can run notification-service + bot-service). The transition is **zero code changes**:

1. Install Redis client + Python on the new machine
2. Clone the repo, set `REDIS_URL=redis://<laptop-ip>:6379` in `.env`
3. Stop the service on the laptop: `systemctl stop stockanalysis-notification`
4. Start it on the new machine: `systemctl start stockanalysis-notification`
5. Done. The service now reads from Redis on the laptop over the network.

```
LAPTOP (Node 1 — data + Redis)          NEW MACHINE (Node 2 — compute + output)
┌────────────────────────┐              ┌────────────────────────┐
│ Redis                  │◄──── network ── REDIS_URL=laptop:6379  │
│ data-gateway           │              │ analysis-engine ×2     │
│ orchestrator           │              │ intelligence-service   │
│ bot-service            │              │ notification-service   │
│ auth-service (timer)   │              │                        │
└────────────────────────┘              └────────────────────────┘
```

**Which services to move first** (in priority order):
1. **analysis-engine** — CPU-heavy, benefits most from dedicated cores. Move to a machine with more cores.
2. **notification-service** — Lightweight but I/O-bound. Can go anywhere.
3. **intelligence-service** — LLM calls are I/O-bound. Can go anywhere.

**Redis stays on the laptop** (Node 1) because it's close to data-gateway (the producer). Redis on localhost = <1ms latency for HGET/HSET. Moving Redis to Node 2 would add 1-5ms network latency to every tick publish.

### Phase 3 — Three+ Machines (when you have a small cluster)

```
Node 1 (laptop)          Node 2 (old PC)         Node 3 (cloud VPS / mini-PC)
┌──────────────┐        ┌──────────────┐        ┌──────────────────┐
│ Redis        │        │ analysis ×3  │        │ analysis ×2      │
│ data-gateway │        │ intelligence │        │ notification     │
│ orchestrator │        │              │        │ bot-service      │
│ auth-service │        │              │        │                  │
└──────────────┘        └──────────────┘        └──────────────────┘
```

- Analysis workers can be spread across any number of machines — they all join the same Redis consumer group
- A cloud VPS (e.g., AWS t3.micro free tier) can run notification + bot for 24/7 uptime even when laptop is off
- Redis Sentinel or Redis Cluster can be added for HA if the laptop becomes unreliable

### Phase 4 — Kubernetes (optional, only if cluster grows to 5+ nodes)

- `analysis-engine` → `Deployment` with HPA (CPU > 70% → scale)
- `data-gateway` → `StatefulSet` (1 replica, sticky WebSocket)
- Redis → `StatefulSet` with persistence + Sentinel
- All other services → `Deployment`s

> **Kubernetes is overkill until you have 5+ machines.** systemd + Redis works perfectly for 1-3 nodes. Don't add complexity you don't need.

---

## 5. Service Definitions

### 5.1 data-gateway

**Responsibility**: All market data ingestion. Fetches from yfinance, Zerodha Kite REST + WebSocket, Sensibull REST + WebSocket. Publishes raw data to Redis.

**Solves**: The 12 PM stall — data fetching is now isolated from analysis. If Sensibull API slows down, only this service is affected; analysis workers continue processing the last cycle's data.

```
Inputs:
  - Redis: orchestrator:cycle_trigger stream (start new cycle)
  - Env: ZerODHA credentials, Sensibull endpoints, yfinance config

Outputs:
  - Redis Pub/Sub channel "ticks:equity:{symbol}" — live equity ticks
  - Redis Pub/Sub channel "ticks:index:{symbol}" — live index ticks
  - Redis Pub/Sub channel "ticks:option:{symbol}" — live option ticks
  - Redis Pub/Sub channel "ticks:future:{symbol}" — live futures ticks
  - Redis Hash "data:price:{symbol}" — latest priceData DataFrame (serialized)
  - Redis Hash "data:sensibull:{symbol}" — latest sensibull_ctx (JSON)
  - Redis Hash "data:zerodha:{symbol}" — latest zerodha_ctx (JSON)
  - Redis Hash "data:options_live:{symbol}" — live options tick data (JSON)
  - Redis Hash "data:options_agg:{symbol}" — live options aggregate (JSON)
  - Redis Hash "data:futures_live:{symbol}" — live futures tick data (JSON)
  - Redis Stream "data:cycle_complete" — signal that all data for a cycle is fetched

Key behaviours:
  - Maintains Zerodha WebSocket connection independently
  - Handles enctoken 403 re-auth internally (doesn't disrupt other services)
  - Maintains Sensibull WebSocket connection independently
  - On cycle trigger: fetches yfinance data for all symbols, Sensibull REST data
  - Publishes each symbol's data as soon as fetched (streaming, not batch)
  - Token registry management (option zone recentering) lives here
  - TickStore lives here — publishes aggregate updates to Redis every 1s

Process model:
  - 1 main process (asyncio event loop)
  - 2 background threads: Zerodha WS reactor, Sensibull WS reader
  - HTTP fetches via aiohttp (async, non-blocking) — NO thread pool

Restart policy:
  - systemd Restart=always
  - On restart: re-connects WebSocket, re-subscribes tokens, resumes fetching
  - Other services continue using last-published data until new data arrives
```

### 5.2 orchestrator

**Responsibility**: Cycle coordination, mode selection (intraday/positional/pre-market), scheduling.

```
Inputs:
  - System clock (time-based mode selection)
  - Env: PRODUCTION, DEV_INTRADAY, DEV_POSITIONAL, SHUTDOWN
  - Redis: data:cycle_complete stream (data-gateway signals fetch done)

Outputs:
  - Redis Stream "orchestrator:cycle_trigger" — {cycle_id, mode, timestamp, symbols[]}
  - Redis Stream "orchestrator:analysis_jobs" — one job per symbol per cycle
  - Redis Hash "orchestrator:state" — current mode, cycle count, last cycle time

Key behaviours:
  - Every 310s (intraday) or once (positional): emits cycle_trigger
  - On cycle_trigger: waits for data-gateway to publish cycle_complete
  - Then emits one analysis_job per symbol to analysis_jobs stream
  - Tracks cycle completion: waits for all analysis results before notifying
  - Handles pre-market and post-market triggers
  - Holiday gatekeeper: checks market calendar before triggering
  - Healthcheck ping at end of each cycle

Process model:
  - 1 process, single-threaded asyncio loop
  - No CPU-intensive work — pure coordination

Restart policy:
  - systemd Restart=always
  - Stateless — reads cycle count from Redis on restart
```

### 5.3 analysis-engine

**Responsibility**: Runs all 12+ analysers on a symbol's data. This is the CPU-intensive service that scales horizontally.

```
Inputs:
  - Redis Stream "orchestrator:analysis_jobs" — {cycle_id, symbol, mode, data_ref}
  - Redis Hash "data:price:{symbol}" — priceData
  - Redis Hash "data:sensibull:{symbol}" — sensibull_ctx
  - Redis Hash "data:zerodha:{symbol}" — zerodha_ctx
  - Redis Hash "data:options_live:{symbol}" — live options (for live-mode analysers)
  - Redis Hash "data:options_agg:{symbol}" — live options aggregate

Outputs:
  - Redis Stream "analysis:results" — {cycle_id, symbol, analysis_dict, score_result}
  - Redis Hash "analysis:latest:{symbol}" — latest analysis result (for bot queries)

Key behaviours:
  - Consumer group "analysis-workers" on analysis_jobs stream
  - Each worker picks one job, deserializes data, runs all analysers, publishes result
  - Analysers are unchanged — same code, same decorators, same scoring
  - Stock object is reconstructed from Redis hashes (lightweight, no WebSocket)
  - After analysis: publishes result, ACKs the stream message
  - On crash: unacked message is XCLAIMed by another worker after timeout
  - gc.collect() after each symbol analysis (prevents gen-2 accumulation)

Process model:
  - N independent processes (systemd template unit: analysis-engine@.service)
  - Each process is single-threaded (no GIL contention, no thread pool)
  - Scale: start 1-2 on 4-core node, add more on second node

Restart policy:
  - systemd Restart=always (per-instance)
  - Crashed worker's pending messages are reclaimed by live workers
```

### 5.4 intelligence-service

**Responsibility**: SignalBus + SignalCorrelator + MarketNarrator (Gemini LLM). Cross-layer confluence detection and trade thesis generation.

```
Inputs:
  - Redis Stream "analysis:results" — subscribes to all analysis results
  - Redis Pub/Sub "ticks:option:{symbol}" — live option tick signals
  - Redis Pub/Sub "ticks:equity:{symbol}" — live equity tick signals
  - Redis Hash "data:*" — for context building (ContextBuilder reads from Redis)

Outputs:
  - Redis Stream "intelligence:narratives" — LLM-generated trade theses
  - Redis Stream "intelligence:confluences" — detected confluence events
  - Redis Pub/Sub "intelligence:signal" — real-time signal broadcast

Key behaviours:
  - SignalBus replaced by Redis Streams: all signals published to "intelligence:signals"
  - SignalCorrelator subscribes to signal stream, detects confluence
  - On HIGH confluence: calls Narrator (Gemini) asynchronously
  - ContextBuilder reads from Redis hashes (replaces shared.app_ctx reads)
  - Per-symbol cooldown (30 min) for narratives
  - LLM budget tracking in Redis (daily token count, resets at midnight)
  - EOD positional briefing after positional analysis completes

Process model:
  - 1 process, asyncio event loop
  - LLM calls via aiohttp (non-blocking)
  - Narrator runs in asyncio task (non-blocking)

Restart policy:
  - systemd Restart=always
  - Signal buffer is in Redis (sorted set with timestamps) — survives restart
```

### 5.5 notification-service

**Responsibility**: Consumes analysis results, confluences, narratives, and sends Telegram/Discord alerts. Handles retry, rate limiting, formatting.

```
Inputs:
  - Redis Stream "analysis:results" — for score-gated notifications
  - Redis Stream "intelligence:narratives" — for LLM trade theses
  - Redis Stream "intelligence:confluences" — for confluence alerts
  - Redis Stream "premarket:reports" — pre-market report messages
  - Redis Stream "postmarket:reports" — post-market report messages

Outputs:
  - Telegram API (3 channels: intraday, positional, live-options)
  - Discord webhooks (3 channels)
  - Redis Stream "notification:log" — audit trail of sent messages

Key behaviours:
  - Consumer group "notifier" on each input stream
  - Score gating: checks should_notify() before sending (same logic as monolith)
  - Winner-takes-all: if PRIORITY_OVERRIDE set, sends only composite card
  - Retry with exponential backoff (3 attempts, 2/4/8s)
  - Rate limiting: max 20 messages/min per channel (Telegram limit)
  - Message formatting: same MessageFormatter.py logic, serialized result → HTML
  - Dead letter queue: failed notifications after 3 retries → "notification:dead"

Process model:
  - 1 process, asyncio event loop
  - aiohttp for Telegram/Discord HTTP calls (non-blocking)

Restart policy:
  - systemd Restart=always
  - Unacked messages reclaimed after 60s timeout
```

### 5.6 bot-service

**Responsibility**: Telegram bot for interactive commands. Reads live state from Redis.

```
Inputs:
  - Telegram Bot API (long polling or webhook)
  - Redis Hash "data:*" — live market data for /ltp, /straddle, /walls
  - Redis Hash "analysis:latest:{symbol}" — for analysis queries
  - Redis Hash "orchestrator:state" — for /status
  - Redis key "service:registry:*" — for /status health dashboard
  - Redis key "llm:budget" — for /status LLM budget display

Outputs:
  - Telegram Bot API responses
  - Redis command "data-gateway:enctoken_update" — for /enctoken command

Key behaviours:
  - All bot commands ported from notification/commands/
  - find_stock_by_symbol() → Redis HSCAN across data:price:* keys
  - /straddle, /walls → reads data:options_agg:{symbol} and data:options_live:{symbol}
  - /status → reads service registry + feed health from Redis
  - /enctoken → publishes to data-gateway:commands stream, data-gateway reconnects
  - No analysis logic — pure read-only from Redis

Process model:
  - 1 process, python-telegram-bot (asyncio mode)
  - JobQueue for scheduled LLM budget alerts

Restart policy:
  - systemd Restart=always
  - Stateless — all state is in Redis
```

### 5.7 auth-service (optional, can be cron)

**Responsibility**: Automated Zerodha TOTP login. Writes fresh enctoken to Redis + .env.

```
Inputs:
  - Env: ZERODHA_USER, ZERODHA_PASS, ZERODHA_TOTP_SECRET
  - Timer (cron / systemd timer)

Outputs:
  - Redis Hash "auth:zerodha" — {enctoken, issued_at, expires_at}
  - Redis Pub/Sub "auth:enctoken_refreshed" — notifies data-gateway

Key behaviours:
  - Runs once at boot (systemd timer at 9:00 AM IST)
  - Once-per-day guard (lock file, same as current)
  - On 403 from data-gateway: data-gateway publishes to "auth:commands" stream
    → auth-service runs fresh login → publishes new token
  - data-gateway subscribes to "auth:enctoken_refreshed" and reconnects

Process model:
  - 1 process, runs on-demand (systemd oneshot) or as long-running listener
  - Can be a cron job in Phase 1, promoted to a service in Phase 2

Restart policy:
  - systemd oneshot (Phase 1) / always (Phase 2)
```

---

## 6. Communication Contracts

### 6.1 Redis Streams (reliable, consumer groups)

| Stream | Producer | Consumer(s) | Message Format |
|--------|----------|-------------|----------------|
| `orchestrator:cycle_trigger` | orchestrator | data-gateway | `{"cycle_id": "2026-06-26-32", "mode": "intraday", "timestamp": 1234567890, "symbols": ["NIFTY", "RELIANCE", ...]}` |
| `orchestrator:analysis_jobs` | orchestrator | analysis-engine (consumer group) | `{"job_id": "uuid", "cycle_id": "2026-06-26-32", "symbol": "NIFTY", "mode": "intraday", "priority": "normal"}` |
| `analysis:results` | analysis-engine | notification-service, intelligence-service | `{"job_id": "uuid", "cycle_id": "...", "symbol": "NIFTY", "analysis": {...}, "score_result": {...}, "timestamp": ...}` |
| `intelligence:narratives` | intelligence-service | notification-service | `{"symbol": "NIFTY", "direction": "BULLISH", "level": "HIGH", "narrative": "...", "timestamp": ...}` |
| `intelligence:confluences` | intelligence-service | notification-service, bot-service | `{"symbol": "NIFTY", "direction": "BULLISH", "level": "HIGH", "score": 18.5, "layers": ["live", "intraday"], "timestamp": ...}` |
| `intelligence:signals` | all services | intelligence-service | `{"symbol": "NIFTY", "direction": "BULLISH", "source": "rsi_divergence", "layer": "intraday", "strength": "STRONG", "timestamp": ...}` |
| `premarket:reports` | data-gateway | notification-service | `{"type": "global_cues", "message": "<html>...", "timestamp": ...}` |
| `postmarket:reports` | analysis-engine | notification-service | `{"type": "fii_dii", "message": "<html>...", "timestamp": ...}` |
| `notification:dead` | notification-service | (audit / manual retry) | `{"original_stream": "...", "message": "...", "error": "...", "attempts": 3, "timestamp": ...}` |
| `data-gateway:commands` | bot-service | data-gateway | `{"command": "enctoken_update", "enctoken": "...", "timestamp": ...}` |
| `auth:commands` | data-gateway | auth-service | `{"command": "refresh_enctoken", "reason": "403", "timestamp": ...}` |

### 6.2 Redis Pub/Sub (fire-and-forget, low latency)

| Channel | Producer | Subscriber(s) | Message Format |
|---------|----------|---------------|----------------|
| `ticks:equity:{symbol}` | data-gateway | intelligence-service | `{"token": 12345, "last_price": 2456.5, "ohlc": {...}, "volume": ..., "timestamp": ...}` |
| `ticks:index:{symbol}` | data-gateway | intelligence-service | same as equity |
| `ticks:option:{symbol}` | data-gateway | intelligence-service | `{"strike": 24500, "type": "CE", "ltp": 120.5, "oi": 1234567, "timestamp": ...}` |
| `ticks:future:{symbol}` | data-gateway | (none currently, future use) | `{"expiry": "current", "ltp": ..., "oi": ..., "timestamp": ...}` |
| `data:options_updated:{symbol}` | data-gateway | intelligence-service | `{"symbol": "NIFTY", "aggregate": {...}, "timestamp": ...}` (fired every 1s after aggregate recompute) |
| `auth:enctoken_refreshed` | auth-service | data-gateway | `{"enctoken": "...", "issued_at": ...}` |

### 6.3 Redis Hashes (shared state, latest-value-wins)

| Key Pattern | Type | Owner | Contents |
|-------------|------|-------|----------|
| `data:price:{symbol}` | Hash | data-gateway | `{"priceData_json": "...", "ltp": 2456.5, "ltp_change_perc": 1.23, "prevDayOHLCV_json": "...", "daily_hv": 18.5}` |
| `data:sensibull:{symbol}` | Hash | data-gateway | `{"current_json": "...", "historical_data_json": "...", "oi_chain_json": "...", "oi_chain_history_json": "...", "iv_chart_history_json": "...", "oi_history_json": "..."}` |
| `data:zerodha:{symbol}` | Hash | data-gateway | `{"option_chain_current_json": "...", "futures_mdata_json": "...", "futures_data_current_json": "..."}` |
| `data:options_live:{symbol}` | Hash | data-gateway | `{"{strike}_{CE|PE}": "{ltp, oi, volume, ...}", ...}` (one field per strike+type) |
| `data:options_agg:{symbol}` | Hash | data-gateway | `{"live_pcr": 1.23, "atm_strike": 24500, "max_oi_ce_strike": 24700, "max_oi_pe_strike": 24300, "atm_straddle_premium": 185.5, ...}` |
| `data:futures_live:{symbol}` | Hash | data-gateway | `{"current_ltp": ..., "current_oi": ..., "next_ltp": ..., "next_oi": ...}` |
| `data:zerodha_tick:{symbol}` | Hash | data-gateway | `{"last_price": ..., "ohlc": ..., "volume": ..., "buy_qty": ..., "sell_qty": ..., "average_traded_price": ...}` |
| `analysis:latest:{symbol}` | Hash | analysis-engine | `{"analysis_json": "...", "score_result_json": "...", "timestamp": ...}` |
| `orchestrator:state` | Hash | orchestrator | `{"mode": "intraday", "cycle_count": 32, "last_cycle_time": ..., "last_cycle_id": "..."}` |
| `service:registry:{name}` | Hash | each service | `{"name": "data-gateway", "pid": 12345, "status": "healthy", "last_heartbeat": ..., "version": "1.0.0", "stats_json": "..."}` (TTL 30s) |
| `llm:budget` | Hash | intelligence-service | `{"daily_tokens": 35000, "daily_limit": 900000, "date": "2026-06-26", "budget_warned": "false"}` |
| `auth:zerodha` | Hash | auth-service | `{"enctoken": "...", "issued_at": ..., "expires_at": ...}` |
| `intelligence:signal_buffer:{symbol}` | ZSet | intelligence-service | `(timestamp, signal_json)` — time-windowed signal buffer for correlator (TTL 6h) |

### 6.4 Serialization Strategy

| Data Type | Serialization | Rationale |
|-----------|---------------|-----------|
| DataFrames (priceData, historical_data) | `pandas.to_json(orient="split")` | Compact, preserves dtypes, fast |
| Nested dicts (sensibull_ctx, analysis) | `json.dumps()` with `default=str` | Standard, debuggable |
| Live tick data (high frequency) | `orjson.dumps()` if available, else `json.dumps()` | Speed for pub/sub |
| Large oi_chain_history | Store only latest 5 in Redis, keep 15 in data-gateway memory | Reduces Redis memory |

---

## 7. Redis Data Schema

### 7.1 Memory Estimation (50 stocks + 4 indices)

| Key Pattern | Count | Size per key | Total |
|-------------|-------|-------------|-------|
| `data:price:*` | 54 | ~50 KB (5d × 5m DataFrame JSON) | ~2.7 MB |
| `data:sensibull:*` | 54 | ~200 KB (oi_chain + insights + history) | ~10.8 MB |
| `data:zerodha:*` | 54 | ~30 KB (option chain metadata) | ~1.6 MB |
| `data:options_live:*` | 4 (indices only) | ~50 KB (200 strikes × CE/PE) | ~200 KB |
| `data:options_agg:*` | 4 | ~2 KB | ~8 KB |
| `data:zerodha_tick:*` | 54 | ~1 KB | ~54 KB |
| `analysis:latest:*` | 54 | ~5 KB | ~270 KB |
| Streams (retained 1h) | 8 streams | ~500 KB each (600 msg/hr × ~1KB) | ~4 MB |
| Service registry | 5 (compact mode) | ~1 KB | ~5 KB |
| **Total** | | | **~20 MB** |

> Redis maxmemory configured at **128 MB** for the laptop. 20 MB usage leaves ~108 MB headroom for stream growth. If Redis hits 128 MB, LRU eviction automatically removes oldest stream entries — analysis continues with slightly shorter history.

### 7.2 Stream Retention Policy

```redis
# Each stream trimmed to last 1 hour of messages
XADD analysis:results MAXLEN ~ 360 * 60  # ~360 results per hour (50 stocks / 5 min)
XADD orchestrator:analysis_jobs MAXLEN ~ 360
XADD intelligence:signals MAXLEN ~ 2000  # ~2000 signals per hour
XADD intelligence:narratives MAXLEN ~ 30  # ~30 narratives per hour (cooldown-limited)
```

### 7.3 Consumer Group Setup

```redis
# analysis-engine workers
XGROUP CREATE orchestrator:analysis_jobs analysis-workers $ MKSTREAM

# notification-service
XGROUP CREATE analysis:results notifier $ MKSTREAM
XGROUP CREATE intelligence:narratives notifier $ MKSTREAM
XGROUP CREATE intelligence:confluences notifier $ MKSTREAM

# intelligence-service
XGROUP CREATE analysis:results intelligence $ MKSTREAM
XGROUP CREATE intelligence:signals intelligence $ MKSTREAM
```

---

## 8. Data Flow Diagrams

### 8.1 Intraday Cycle (5-min loop)

```
orchestrator                    data-gateway                    analysis-engine
     │                               │                               │
     │ 1. XADD cycle_trigger         │                               │
     │──────────────────────────────>│                               │
     │                               │                               │
     │                               │ 2. Fetch yfinance (async)     │
     │                               │    Fetch Sensibull REST       │
     │                               │    HSET data:price:*          │
     │                               │    HSET data:sensibull:*      │
     │                               │                               │
     │                               │ 3. XADD cycle_complete        │
     │<──────────────────────────────│                               │
     │                               │                               │
     │ 4. XADD analysis_jobs         │                               │
     │    (one per symbol)           │                               │
     │──────────────────────────────────────────────────────────────>│
     │                               │                               │
     │                               │                  5. XREADGROUP │
     │                               │                               │
     │                               │  6. HGET data:price:{symbol}  │
     │                               │<──────────────────────────────│
     │                               │  7. HGET data:sensibull:*     │
     │                               │<──────────────────────────────│
     │                               │                               │
     │                               │          8. Run 12 analysers  │
     │                               │             + scoring         │
     │                               │                               │
     │                               │          9. XADD results      │
     │                               │──────────────────────────────>│ (to notification + intelligence)
     │                               │                               │
     │                               │          10. XACK job         │
     │                               │                               │
     │ 11. Monitor results stream    │                               │
     │     (wait for all symbols)    │                               │
     │                               │                               │
     │ 12. Ping healthcheck          │                               │
     │     Sleep 310s                │                               │
```

### 8.2 Live Options Tick (real-time, ~1s)

```
Zerodha WebSocket
       │
       ▼
data-gateway
       │
       ├── TickStore.update_option_tick()  (in-memory, same as now)
       ├── TickStore.recompute_options_aggregate()  (throttled 1s)
       │
       ├── PUBLISH ticks:option:{symbol}  (raw tick, for intelligence)
       ├── HSET data:options_live:{symbol}  (per-strike data)
       ├── HSET data:options_agg:{symbol}  (aggregate: PCR, max pain, walls)
       └── PUBLISH data:options_updated:{symbol}  (aggregate update notification)
                │
                ▼
       intelligence-service
                │
                ├── SignalBus.emit() → XADD intelligence:signals
                ├── LiveOIAnalyser checks (PCR crossover, wall breach)
                ├── LiveStraddleAnalyser checks (IV change, skew)
                ├── SignalCorrelator.on_signal() → check confluence
                │
                ├── If confluence detected:
                │   ├── XADD intelligence:confluences
                │   └── Narrator.narrate_async() → Gemini LLM
                │       └── XADD intelligence:narratives
                │
                ▼
       notification-service
                │
                ├── XREADGROUP confluences → send Telegram alert
                └── XREADGROUP narratives → send LLM thesis to live-options channel
```

### 8.3 Enctoken Expiry Recovery (403 at noon)

```
Zerodha WebSocket (403 error)
       │
       ▼
data-gateway
       │
       ├── Log 403, mark WS disconnected
       ├── HSET service:registry:data-gateway status=degraded
       ├── XADD auth:commands {command: refresh_enctoken, reason: 403}
       │        │
       │        ▼
       │   auth-service
       │        │
       │        ├── Run TOTP login flow
       │        ├── HSET auth:zerodha {enctoken, issued_at}
       │        └── PUBLISH auth:enctoken_refreshed
       │                 │
       │                 ▼
       │   data-gateway (subscribed)
       │        │
       │        ├── Update enctoken
       │        ├── Reconnect WebSocket
       │        ├── Re-subscribe all tokens
       │        ├── HSET service:registry:data-gateway status=healthy
       │        └── Resume tick publishing
       │
       └── (Other services continue using last-published data.
            No stall. No disruption to analysis or notifications.)
```

### 8.4 Bot Command (/straddle NIFTY)

```
User sends /straddle NIFTY to Telegram
       │
       ▼
bot-service
       │
       ├── Parse command
       ├── HGET data:options_agg:NIFTY  → PCR, ATM, straddle premium
       ├── HGET data:options_live:NIFTY  → CE/PE leg LTPs
       ├── HGET data:zerodha_tick:NIFTY  → spot price
       ├── Format response (same as current cmd_straddle)
       └── Send via Telegram Bot API
```

---

## 9. Scaling Strategy

### 9.1 Which Services Scale Horizontally

| Service | Scales? | Mechanism | Limit |
|---------|---------|-----------|-------|
| data-gateway | **No** (singleton) | 1 instance — WebSocket is stateful | N/A |
| orchestrator | **No** (singleton) | 1 instance — cycle coordination | N/A |
| analysis-engine | **Yes** | Add more consumer processes (same or new machine) | Redis throughput (~100K ops/s) |
| intelligence-service | **Limited** | 1-2 instances (correlator state in Redis) | LLM API rate (15 RPM) |
| notification-service | **Yes** | Add more consumer processes | Telegram rate (30 msg/min) |
| bot-service | **No** (singleton) | 1 instance — Telegram long-polling | N/A |
| auth-service | **No** (singleton) | 1 instance | N/A |

### 9.2 Gradual Scaling Path (from 1 laptop to N machines)

The design is future-proof: **scaling = starting more processes**, whether on the same machine or a new one. The code never changes — only `REDIS_URL` and `systemctl start`.

```
Step 0: Single laptop (compact mode)
  └─ 1 analysis worker, ~4.7 GB RAM used

Step 1: Single laptop (full mode) — if you have RAM headroom
  └─ 1 analysis worker, all services separate, ~5 GB RAM

Step 2: Single laptop + a second worker
  └─ systemctl start stockanalysis-analysis@2
  └─ 2 workers, ~5.3 GB RAM. Only do this if laptop has thermal headroom.

Step 3: Add a second machine (any old PC/laptop/RPi)
  └─ Move analysis-engine to new machine:
     - laptop: systemctl stop stockanalysis-analysis@1
     - new machine: REDIS_URL=redis://laptop:6379 systemctl start stockanalysis-analysis@1
  └─ Zero code changes. Analysis jobs flow through Redis over network.

Step 4: Move more services to the second machine
  └─ Move notification-service, intelligence-service to new machine
  └─ Laptop now only runs: Redis + data-gateway + orchestrator + bot + auth
  └─ Laptop RAM drops to ~2 GB, freeing resources for data-gateway

Step 5: Add a third machine (cloud VPS for 24/7 uptime)
  └─ Move bot-service + notification-service to VPS
  └─ VPS stays online even when laptop is powered off
  └─ Laptop only runs during market hours (9 AM - 8 PM IST)

Step 6: Kubernetes (only if 5+ machines)
  └─ Convert systemd units to K8s manifests
  └─ HPA for analysis-engine (auto-scale on CPU)
```

### 9.3 analysis-engine Scaling (same machine or across machines)

```bash
# Single laptop: 1 worker (default, safe for thermal)
sudo systemctl start stockanalysis-analysis@1

# Single laptop: 2 workers (if thermal allows — watch `sensors` output)
sudo systemctl start stockanalysis-analysis@2

# New machine: point at laptop's Redis, start workers
REDIS_URL=redis://laptop-ip:6379 sudo systemctl start stockanalysis-analysis@1
REDIS_URL=redis://laptop-ip:6379 sudo systemctl start stockanalysis-analysis@2
REDIS_URL=redis://laptop-ip:6379 sudo systemctl start stockanalysis-analysis@3
```

Each worker joins the same consumer group `analysis-workers`. Redis distributes jobs round-robin. If a worker crashes, its pending jobs are XCLAIMed by a live worker after 120s. Workers on different machines work identically — they just read from a remote Redis instead of localhost.

### 9.4 Load-Based Auto-Scaling (future, when multi-node)

```python
# Simple scaler script (can run on any machine, or as a cron job)
# Checks Redis stream lag and starts/stops workers via SSH

import subprocess

PENDING_THRESHOLD = 20  # jobs waiting > 1 cycle = scale up
IDLE_THRESHOLD = 5      # pending < 5 for 3 cycles = scale down

# XPENDING orchestrator:analysis_jobs analysis-workers
# If pending > 20: SSH to spare machine, start another worker
# If pending < 5 for 3 cycles: SSH to spare machine, stop a worker
```

### 9.5 Throughput Targets

| Metric | Monolith (now) | 1 Laptop (compact) | 1 Laptop + 2nd machine | 3 machines |
|--------|---------------|--------------------|-----------------------|------------|
| Analysis cycle time (50 stocks) | 60-90s (stalls at noon) | 40-55s (1 worker, no stall) | 20-30s (3 workers) | 10-15s (5 workers) |
| Max stocks per cycle | ~60 (thread pool) | ~150 (Redis throughput) | ~300 | ~500+ |
| WebSocket tick latency | 50-200ms (shared CPU) | 10-50ms (dedicated) | 10-50ms | 10-50ms |
| Enctoken recovery | 60-120s (disrupts all) | 15-30s (isolated) | 15-30s | 15-30s |
| RAM used (laptop) | ~3 GB (monolith) | ~4.7 GB (5 processes) | ~2 GB (3 services left) | ~2 GB |
| 12 PM stall | **Yes** | **No** (data-gateway isolates I/O) | **No** | **No** |

---

## 10. Deployment Strategy

### 10.1 Phase 1 — systemd on Laptop (single machine)

**Compact mode (default — 5 processes):**

```
/etc/systemd/system/
├── stockanalysis-redis.service              # Redis server (maxmemory 128mb)
├── stockanalysis-data-gateway.service       # Data ingestion (WS + HTTP)
├── stockanalysis-analysis@.service          # Analysis workers (template, start @1)
├── stockanalysis-coordinator.service        # Merged: orchestrator + intelligence + bot
├── stockanalysis-notification.service       # Telegram/Discord sender
├── stockanalysis-auth.service               # Zerodha TOTP login (oneshot)
├── stockanalysis-auth.timer                 # Mon-Fri 09:00 IST
├── stockanalysis.timer                      # Mon-Fri 09:00 IST (starts coordinator)
├── stockanalysis-positional.timer           # Mon-Fri 20:00 IST
```

**Full mode (when ready — 8 processes):**

```
/etc/systemd/system/
├── stockanalysis-redis.service
├── stockanalysis-data-gateway.service
├── stockanalysis-orchestrator.service       # Separate orchestrator
├── stockanalysis-analysis@.service          # Workers
├── stockanalysis-intelligence.service       # Separate intelligence
├── stockanalysis-notification.service
├── stockanalysis-bot.service                # Separate bot
├── stockanalysis-auth.service + .timer
├── stockanalysis.timer + positional.timer
```

Switch between compact and full by enabling/disabling the merged vs separate units. The code is the same — `coordinator.service` just imports and runs all three entry points in one asyncio loop.

**Service dependency graph (compact mode):**
```
redis.service
    ├── data-gateway.service       (Requires=redis)
    ├── coordinator.service        (Requires=redis, data-gateway)
    ├── analysis@%.service         (Requires=redis)
    └── notification.service       (Requires=redis)
```

**Laptop-specific systemd hardening:**
```ini
# In each service unit file:

# Thermal protection — don't let any service pin the CPU at 100%
CPUQuota=80%
CPUWeight=50

# Memory protection — OOM-kill this service, not the whole laptop
MemoryMax=1200M           # adjust per service
MemorySwapMax=512M        # allow limited swap usage

# Don't restart too aggressively on a laptop (prevents thermal spiral)
Restart=always
RestartSec=10             # 10s between restarts (was 5s for server)
StartLimitBurst=5
StartLimitIntervalSec=300 # max 5 restarts per 5 min, then stop

# Priority: data-gateway is most important, give it higher CPU priority
# (set CPUWeight=100 in data-gateway, 50 in others)
```

**Preventing laptop suspend during market hours:**
```ini
# /etc/systemd/system/stockanalysis-data-gateway.service
[Unit]
# Block system suspend while this service is active
Conflicts=suspend.target hibernate.target hybrid-sleep.target
```

Or globally:
```bash
# /etc/systemd/logind.conf
HandleLidSwitch=ignore        # don't suspend on lid close
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
```

### 10.2 Phase 2 — Add a Second Machine (zero code changes)

**On the laptop (Node 1):**
```bash
# Allow Redis to accept connections from the new machine
# /etc/redis/redis.conf
bind 127.0.0.1 <laptop-lan-ip>
requirepass <a-strong-password>    # IMPORTANT: set a password if exposing Redis

# Restart Redis
sudo systemctl restart stockanalysis-redis

# Stop the services you're moving to Node 2
sudo systemctl stop stockanalysis-analysis@1
sudo systemctl stop stockanalysis-notification
```

**On the new machine (Node 2):**
```bash
# Install Python + Redis client + clone repo
git clone <repo> ~/StockAnalysis
cd ~/StockAnalysis
make venv && make install

# Configure .env
REDIS_URL=redis://:<password>@<laptop-lan-ip>:6379
# ... other env vars (Telegram tokens, etc.)

# Install systemd units (same files as laptop)
sudo cp scripts/system_config/stockanalysis-analysis@.service /etc/systemd/system/
sudo cp scripts/system_config/stockanalysis-notification.service /etc/systemd/system/

# Start the services — they connect to Redis on the laptop
sudo systemctl start stockanalysis-analysis@1
sudo systemctl start stockanalysis-analysis@2
sudo systemctl start stockanalysis-notification
```

**Verify:**
```bash
# On Node 2: check it's connected to Redis
redis-cli -u "redis://:<password>@<laptop-lan-ip>:6379" PING
# → PONG

# Check the worker joined the consumer group
redis-cli -u "..." XINFO GROUPS orchestrator:analysis_jobs
# → Should show analysis-workers group with 2 consumers

# Check Node 2 worker is processing jobs
redis-cli -u "..." XINFO CONSUMERS orchestrator:analysis_jobs analysis-workers
# → Should show worker-1 and worker-2 with pending count
```

### 10.3 Phase 3 — Docker Compose (when you have 2+ machines and want containerization)

Only adopt Docker when you have a second machine and want reproducible deploys. On a single laptop, systemd is lighter (no Docker daemon, no container runtime overhead).

```yaml
# docker-compose.yml (Node 1: laptop — data + coordination + Redis)
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    command: redis-server --maxmemory 128mb --maxmemory-policy allkeys-lru --save ""
    volumes: ["redis-data:/data"]
    restart: always

  data-gateway:
    build: ./services/data-gateway
    env_file: .env
    depends_on: [redis]
    restart: always
    deploy:
      resources:
        limits: { memory: 1.2G }

  coordinator:
    build: ./services/coordinator
    env_file: .env
    depends_on: [redis, data-gateway]
    restart: always
    deploy:
      resources:
        limits: { memory: 500M }

volumes:
  redis-data:
```

```yaml
# docker-compose.yml (Node 2: compute node)
services:
  analysis-engine:
    build: ./services/analysis-engine
    env_file: .env
    environment:
      - REDIS_URL=redis://:<password>@<laptop-ip>:6379
    deploy:
      replicas: 3
      resources:
        limits: { memory: 800M }
    restart: always

  notification-service:
    build: ./services/notification-service
    env_file: .env
    environment:
      - REDIS_URL=redis://:<password>@<laptop-ip>:6379
    restart: always
```

### 10.4 Phase 4 — Kubernetes (only when 5+ nodes)

Only when the cluster grows beyond what Docker Compose can manage. systemd + Docker Compose covers 1-4 machines perfectly.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: analysis-engine
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: analysis-engine
        image: stockanalysis/analysis-engine:latest
        env:
        - name: REDIS_URL
          value: "redis://redis-service:6379"
        resources:
          requests: { cpu: "500m", memory: "512Mi" }
          limits: { cpu: "1000m", memory: "800Mi" }
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: analysis-engine-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: analysis-engine
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
```

> **Don't jump to K8s prematurely.** The same Python code runs under systemd, Docker Compose, and K8s. Start with systemd, add Docker when you get a second machine, add K8s only when you have 5+ machines and need auto-scaling/orchestration.

---

## 11. Reliability & Fault Tolerance

### 11.1 Failure Modes & Mitigations

| Failure | Monolith Behavior | Microservices Behavior |
|---------|-------------------|----------------------|
| Sensibull API timeout | Thread pool stalls, all analysis stops | data-gateway retries, analysis-engine uses last cached data from Redis |
| Analysis worker crash | Entire process dies, all ticks lost | Worker's pending job XCLAIMed by another worker in 120s |
| Enctoken expiry (403) | Subprocess + WS reconnect in same process, 60-120s disruption | auth-service refreshes token, data-gateway reconnects in 15-30s, other services unaffected |
| Redis crash | N/A (no Redis in monolith) | All services degrade gracefully — data-gateway buffers in-memory, orchestrator pauses cycles, bot-service shows "degraded" |
| Telegram API down | Retry in same thread, blocks analysis | notification-service retries independently, analysis continues, messages queued in Redis stream |
| OOM kill | Entire process killed | Only the affected service restarted by systemd/Docker |
| Network partition (node 2 unreachable) | N/A | Node 1 services continue (data + bot), analysis jobs queue in Redis until node 2 returns |

### 11.2 Graceful Degradation

Each service implements a degradation ladder:

```
data-gateway:
  1. Normal: fetch all symbols, publish to Redis
  2. Degraded (Sensibull down): publish yfinance + Zerodha data only, skip Sensibull
  3. Degraded (Zerodha WS down): publish REST data only, mark WS as disconnected
  4. Critical (all data sources down): publish heartbeat only, alert via notification-service

analysis-engine:
  1. Normal: run all 12 analysers
  2. Degraded (missing sensibull data): skip options/PCR/IV/OI analysers, run technical only
  3. Degraded (missing price data): skip all, publish empty result

notification-service:
  1. Normal: send all alerts
  2. Degraded (Telegram down): queue in Redis stream, retry every 60s
  3. Critical (Redis down): log to stdout, systemd journal captures
```

### 11.3 Circuit Breakers

```python
# Each service implements a circuit breaker for external calls
class CircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_timeout=60):
        self.failures = 0
        self.threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.last_failure_time = 0
        self.state = "closed"  # closed, open, half-open

    def call(self, func, *args, **kwargs):
        if self.state == "open":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "half-open"
            else:
                raise CircuitBreakerOpenError()

        try:
            result = func(*args, **kwargs)
            if self.state == "half-open":
                self.state = "closed"
                self.failures = 0
            return result
        except Exception:
            self.failures += 1
            self.last_failure_time = time.time()
            if self.failures >= self.threshold:
                self.state = "open"
            raise
```

---

## 12. Observability

### 12.1 Per-Service Health Checks

Each service writes to Redis every 10 seconds:

```redis
HSET service:registry:data-gateway
  name "data-gateway"
  pid 12345
  status "healthy"          # healthy | degraded | critical | starting
  last_heartbeat 1234567890
  version "2.0.0"
  stats_json '{"ticks_received": 45678, "ticks_published": 45000, "ws_connected": true, "sensibull_ws_connected": true, "cycle_count": 32}'
EXPIRE service:registry:data-gateway 30
```

### 12.2 Stream Lag Monitoring

```redis
# Pending messages per consumer group (high = workers can't keep up)
XPENDING orchestrator:analysis_jobs analysis-workers
XPENDING analysis:results notifier
XPENDING intelligence:signals intelligence

# Stream length (high = not being consumed)
XLEN orchestrator:analysis_jobs
XLEN analysis:results
XLEN intelligence:signals
```

### 12.3 Structured Logging

Each service logs JSON to stdout (captured by systemd journal):

```json
{
  "service": "analysis-engine",
  "worker_id": 1,
  "level": "INFO",
  "event": "analysis_complete",
  "symbol": "NIFTY",
  "cycle_id": "2026-06-26-32",
  "duration_ms": 1250,
  "signals_fired": 5,
  "score": 145,
  "timestamp": "2026-06-26T12:00:00+05:30"
}
```

### 12.4 /status Bot Command (Updated)

The `/status` command now reads from Redis service registry:

```
📊 System Health Dashboard

⏰ Time: 12:00:00  |  📅 Trading Day: Yes ✅
📡 Mode: INTRADAY  |  🏭 Production: 🟢 ON

🟢 Services (compact mode — 5 processes):
  🟢 redis: 22 MB / 128 MB
  🟢 data-gateway: 32 cycles, WS connected, 45K ticks, 850 MB
  🟢 coordinator: orchestrator + intelligence + bot, 15 confluences, 3 narratives, 500 MB
  🟢 analysis-engine@1: 50 jobs/cycle, avg 1.2s/job, 420 MB
  🟢 notification-service: 8 alerts sent, 0 failed, 180 MB

📡 Feed Health:
  Equity ticks: 🟢 2s ago
  Options (NIFTY): 🟢 1s ago
  Options (BANKNIFTY): 🟢 1s ago

💾 Memory (laptop total):
  Process RSS: 🟢 2.0 GB / 8.0 GB
  System used: 🟡 52% (avail 3.8 GB)

🤖 LLM Budget:
  Used today: 🟢 35,000 / 900,000 tokens (3.9%)

📈 Stream Lag:
  analysis_jobs: 🟢 0 pending
  analysis:results: 🟢 0 pending
  intelligence:signals: 🟢 2 pending
```

---

## 13. Migration Path

### Phase 0: Laptop Prerequisites (1 day)

1. Install Redis on the laptop: `sudo apt install redis-server`
2. Disable suspend on lid close: edit `/etc/systemd/logind.conf`
3. Create `services/` directory structure
4. Create shared `services/common/` package with Redis client, serialization utils, health check mixin
5. Configure Redis: `configs/redis.conf` (128mb maxmemory, no persistence)
6. Install systemd units for Redis

### Phase 1A: Extract data-gateway (3-5 days)

**Goal**: Move all data fetching to a separate process that publishes to Redis. This alone solves the 12 PM stall — analysis stays in the monolith but reads data from Redis instead of fetching it inline.

Steps:
1. Create `services/data_gateway/main.py` — copies ZerodhaTickerManager, SensibullFetcher, yfinance fetch logic
2. Replace in-process Stock objects with Redis HSET writes
3. Keep intraday_monitor.py reading from Redis instead of calling fetchers directly
4. Run data-gateway + monolith in parallel; verify data matches
5. Remove data fetching code from intraday_monitor.py

**Risk**: TickStore serialization to Redis. Solution: publish only aggregate data (not raw ticks) to Redis; keep TickStore in data-gateway memory.

**After this phase**: The 12 PM stall is fixed. The monolith no longer makes HTTP calls — it just reads from Redis and runs analysers.

### Phase 1B: Extract notification-service (2-3 days)

**Goal**: Move all Telegram/Discord sending to a separate process.

Steps:
1. Create `services/notification-service/main.py`
2. Monolith writes to `analysis:results` stream instead of calling TELEGRAM_NOTIFICATIONS directly
3. notification-service reads stream, applies score gating, formats, sends
4. Port MessageFormatter.py logic to notification-service

### Phase 1C: Extract analysis-engine (3-5 days)

**Goal**: Move all 12 analysers to worker processes. This is the big one — it replaces the ThreadPoolExecutor.

Steps:
1. Create `services/analysis-engine/main.py`
2. Worker reads job from `orchestrator:analysis_jobs` stream
3. Reconstructs Stock object from Redis hashes (priceData, sensibull_ctx, zerodha_ctx)
4. Runs AnalyserOrchestrator.run_all_intraday() — same code, no changes to analysers
5. Publishes result to `analysis:results` stream
6. Orchestrator in monolith writes jobs to stream instead of running ThreadPoolExecutor

**Key**: Stock object reconstruction. Create a `Stock.from_redis(redis_client, symbol)` classmethod that reads all Redis hashes and populates the Stock fields. The analysers don't know the difference.

### Phase 1D: Extract coordinator (compact mode) (3-5 days)

**Goal**: Replace intraday_monitor.py's main loop with a coordinator service that runs orchestrator + intelligence + bot in one process (compact mode for laptop).

Steps:
1. Create `services/coordinator/main.py` — runs 3 asyncio tasks concurrently:
   - Orchestrator task: cycle timing, mode selection, holiday check, emits `cycle_trigger` + `analysis_jobs`
   - Intelligence task: subscribes to `analysis:results` + `intelligence:signals`, runs correlator + narrator
   - Bot task: Telegram bot polling, reads from Redis for commands
2. All three share the same Redis connection (reduces socket count on laptop)
3. If any task crashes, only that task restarts (asyncio task cancellation + recreation)
4. When you get a second machine, you split this into 3 separate services (zero code changes — just run the 3 main.py entry points separately)

### Phase 1E: Extract auth-service (1 day)

**Goal**: Move TOTP login to separate process.

Steps:
1. Create `services/auth-service/main.py`
2. auth-service listens on `auth:commands` stream for refresh requests
3. On refresh: runs TOTP login, writes enctoken to Redis, publishes to `auth:enctoken_refreshed`
4. data-gateway subscribes to `auth:enctoken_refreshed` and reconnects

### Phase 1 Complete: Decommission Monolith

After all services are extracted:
1. Delete `intraday/intraday_monitor.py` (or keep as legacy reference)
2. Delete `common/shared.py` AppContext (replaced by Redis)
3. Update Makefile targets to manage systemd services
4. Update CLAUDE.md with new architecture
5. **5 processes running on laptop**: Redis, data-gateway, analysis-engine@1, coordinator, notification-service
6. **3.3 GB RAM headroom**: enough for a second analysis worker if needed

### Future: Split coordinator into separate services (when you get a 2nd machine)

When you add a second machine, split the coordinator into 3 separate services:
1. `services/orchestrator/main.py` — standalone orchestrator
2. `services/intelligence-service/main.py` — standalone intelligence
3. `services/bot-service/main.py` — standalone bot

The coordinator's `main.py` already imports these modules — splitting is just running them as separate processes instead of asyncio tasks. Zero code changes to the individual modules.

### Migration Timeline

| Phase | Duration | Deliverable | Risk | 12 PM stall fixed? |
|-------|----------|------------|------|-------------------|
| 0: Laptop setup | 1 day | Redis + service scaffold | Low | No |
| 1A: data-gateway | 3-5 days | Data fetching isolated | Medium (TickStore serialization) | **Yes** |
| 1B: notification | 2-3 days | Notifications isolated | Low | Yes |
| 1C: analysis-engine | 3-5 days | Analysis isolated (no ThreadPoolExecutor) | Medium (Stock reconstruction) | Yes |
| 1D: coordinator | 3-5 days | Monolith decommissioned | Medium (3 asyncio tasks) | Yes |
| 1E: auth-service | 1 day | Auth isolated | Low | Yes |
| **Total** | **~15 days** | **Full microservices on laptop** | | |

> **Phase 1A alone fixes the stall.** The remaining phases are about clean architecture and future-proofing. You can stop after 1A and the 12 PM problem is solved — the monolith just reads from Redis instead of doing HTTP calls.

---

## 14. Service Stub Specifications

### 14.1 Directory Structure

```
StockAnalysis/
├── services/
│   ├── __init__.py
│   ├── common/                   # Shared service infrastructure
│   │   ├── __init__.py
│   │   ├── redis_client.py       # Redis connection manager (async)
│   │   ├── serialization.py      # DataFrame/dict serialization helpers
│   │   ├── health.py             # Health check mixin (heartbeat to Redis)
│   │   ├── circuit_breaker.py    # Circuit breaker for external calls
│   │   ├── stream_consumer.py    # Base class for stream consumers
│   │   └── stock_proxy.py        # Stock.from_redis() / Stock.to_redis()
│   │
│   ├── data-gateway/
│   │   ├── __init__.py
│   │   ├── main.py               # Entry point: asyncio loop
│   │   ├── yfinance_fetcher.py   # Async yfinance data fetcher
│   │   ├── zerodha_manager.py    # WebSocket + REST (ported from zerodha/)
│   │   ├── sensibull_fetcher.py  # REST fetcher (ported from fno/)
│   │   ├── sensibull_feed.py     # WebSocket feed (ported from fno/)
│   │   ├── tick_store.py         # TickStore (ported from zerodha/)
│   │   ├── token_registry.py     # Token registry (ported from common/)
│   │   └── publisher.py          # Redis publish logic
│   │
│   ├── coordinator/                  # COMPACT MODE: merged orchestrator + intelligence + bot
│   │   ├── __init__.py
│   │   ├── main.py               # Entry point: runs 3 asyncio tasks in one process
│   │   ├── cycle_manager.py      # Cycle trigger + completion tracking (from orchestrator)
│   │   ├── mode_selector.py      # Intraday/positional/premarket routing
│   │   ├── signal_bus.py         # Redis-backed SignalBus (from intelligence)
│   │   ├── correlator.py         # Ported from intelligence/correlator.py
│   │   ├── narrator.py           # Ported from intelligence/narrator.py
│   │   ├── llm_client.py         # Ported from intelligence/llm_client.py
│   │   ├── context_builder.py    # Ported from intelligence/context_builder.py (reads Redis)
│   │   ├── commands/             # Ported from notification/commands/
│   │   └── redis_reader.py       # Reads live data from Redis for bot commands
│   │
│   ├── orchestrator/                 # FULL MODE: separate orchestrator
│   │   ├── __init__.py
│   │   ├── main.py               # Entry point: cycle scheduler
│   │   ├── cycle_manager.py      # Cycle trigger + completion tracking
│   │   └── mode_selector.py      # Intraday/positional/premarket routing
│   │
│   ├── analysis-engine/
│   │   ├── __init__.py
│   │   ├── main.py               # Entry point: stream consumer loop
│   │   ├── worker.py             # Job processor: reconstruct Stock, run analysers
│   │   ├── analysers/            # Symlink or import from ../../analyser/
│   │   └── scoring.py            # Import from ../../common/scoring.py
│   │
│   ├── intelligence-service/         # FULL MODE: separate intelligence
│   │   ├── __init__.py
│   │   ├── main.py               # Entry point: signal consumer + correlator
│   │   ├── signal_bus.py         # Redis-backed SignalBus
│   │   ├── correlator.py         # Ported from intelligence/correlator.py
│   │   ├── narrator.py           # Ported from intelligence/narrator.py
│   │   ├── llm_client.py         # Ported from intelligence/llm_client.py
│   │   └── context_builder.py    # Ported from intelligence/context_builder.py (reads Redis)
│   │
│   ├── notification-service/
│   │   ├── __init__.py
│   │   ├── main.py               # Entry point: multi-stream consumer
│   │   ├── formatter.py          # Ported from analyser/MessageFormatter.py
│   │   ├── telegram_sender.py    # Ported from notification/Notification.py
│   │   ├── discord_sender.py     # Discord webhook sender
│   │   └── score_gate.py         # Ported from common/scoring.py should_notify()
│   │
│   ├── bot-service/                  # FULL MODE: separate bot
│   │   ├── __init__.py
│   │   ├── main.py               # Entry point: Telegram bot
│   │   ├── commands/             # Ported from notification/commands/
│   │   └── redis_reader.py       # Reads live data from Redis for commands
│   │
│   └── auth-service/
│       ├── __init__.py
│       └── main.py               # Entry point: TOTP login + command listener
│
├── analyser/                     # UNCHANGED — imported by analysis-engine
├── common/                       # SHARED — constants, scoring, Stock model
├── configs/
│   ├── redis.conf                # Redis configuration
│   └── service_env/              # Per-service .env files
│       ├── data-gateway.env
│       ├── orchestrator.env
│       ├── analysis-engine.env
│       └── ...
├── scripts/
│   ├── system_config/            # systemd unit files
│   │   ├── stockanalysis-redis.service
│   │   ├── stockanalysis-data-gateway.service
│   │   ├── stockanalysis-coordinator.service       # COMPACT mode (merged)
│   │   ├── stockanalysis-orchestrator.service       # FULL mode (separate)
│   │   ├── stockanalysis-analysis@.service          # Workers (template)
│   │   ├── stockanalysis-intelligence.service       # FULL mode (separate)
│   │   ├── stockanalysis-notification.service
│   │   ├── stockanalysis-bot.service                # FULL mode (separate)
│   │   ├── stockanalysis-auth.service
│   │   ├── stockanalysis-auth.timer
│   │   ├── stockanalysis.timer
│   │   └── stockanalysis-positional.timer
│   ├── deploy_services.py        # Deploy script for multi-service setup
│   └── laptop_setup.sh           # One-time laptop setup (suspend disable, Redis, etc)
└── Makefile                      # Updated with service management targets
```

### 14.2 Shared Service Base Class

```python
# services/common/stream_consumer.py
import asyncio
import json
import time
import signal
from abc import ABC, abstractmethod
from redis.asyncio import Redis
from common.logging_util import logger


class StreamConsumer(ABC):
    """Base class for Redis Stream consumers with consumer groups."""

    def __init__(
        self,
        redis: Redis,
        stream: str,
        group: str,
        consumer_name: str,
        batch_size: int = 10,
        block_ms: int = 5000,
        ack_timeout: int = 120,
    ):
        self.redis = redis
        self.stream = stream
        self.group = group
        self.consumer = consumer_name
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.ack_timeout = ack_timeout
        self._running = False

    async def start(self):
        self._running = True
        await self._ensure_group()
        await self._reclaim_stale_messages()

        while self._running:
            try:
                messages = await self.redis.xreadgroup(
                    groupname=self.group,
                    consumername=self.consumer,
                    streams={self.stream: ">"},
                    count=self.batch_size,
                    block=self.block_ms,
                )
                for stream, msg_id, fields in messages:
                    await self._process(msg_id, fields)
                    await self.redis.xack(self.stream, self.group, msg_id)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.consumer}] Stream error: {e}")
                await asyncio.sleep(1)

    async def stop(self):
        self._running = False

    async def _ensure_group(self):
        try:
            await self.redis.xgroup_create(self.stream, self.group, "$", mkstream=True)
        except Exception:
            pass  # Group already exists

    async def _reclaim_stale_messages(self):
        """XCLAIM messages from dead consumers."""
        pending = await self.redis.xpending_range(
            self.stream, self.group, min="-", max="+", count=100
        )
        now = time.time()
        for entry in pending:
            if entry["time_since_delivered"] > self.ack_timeout * 1000:
                claimed = await self.redis.xclaim(
                    self.stream, self.group, self.consumer,
                    min_idle_time=self.ack_timeout * 1000,
                    message_ids=[entry["message_id"]],
                )
                for msg_id, fields in claimed:
                    logger.warning(f"[{self.consumer}] Reclaimed stale message {msg_id}")
                    await self._process(msg_id, fields)
                    await self.redis.xack(self.stream, self.group, msg_id)

    @abstractmethod
    async def _process(self, msg_id: str, fields: dict):
        """Process a single message. Override in subclass."""
        ...
```

### 14.3 Stock Proxy (Redis ↔ Stock Object)

```python
# services/common/stock_proxy.py
import json
import pandas as pd
from redis.asyncio import Redis
from common.Stock import Stock


class StockProxy:
    """Reconstructs Stock objects from Redis for analysis-engine workers."""

    @staticmethod
    async def from_redis(redis: Redis, symbol: str, is_index: bool = False) -> Stock:
        stock = Stock(symbol, symbol, is_index=is_index)

        # Price data
        price_data = await redis.hgetall(f"data:price:{symbol}")
        if price_data:
            stock.priceData = pd.read_json(
                price_data["priceData_json"], orient="split"
            )
            stock.ltp = float(price_data.get("ltp", 0))
            stock.ltp_change_perc = float(price_data.get("ltp_change_perc", 0))
            if "prevDayOHLCV_json" in price_data:
                stock.prevDayOHLCV = json.loads(price_data["prevDayOHLCV_json"])
            if "daily_hv" in price_data:
                stock.daily_hv = float(price_data["daily_hv"])

        # Sensibull context
        sensibull_data = await redis.hgetall(f"data:sensibull:{symbol}")
        if sensibull_data:
            stock.sensibull_ctx = {
                "last_fetch_time": sensibull_data.get("last_fetch_time"),
                "current": json.loads(sensibull_data.get("current_json", "{}")),
                "historical_data": pd.read_json(
                    sensibull_data.get("historical_data_json", "{}"), orient="split"
                ) if "historical_data_json" in sensibull_data else pd.DataFrame(),
                "oi_chain": json.loads(sensibull_data.get("oi_chain_json", "null")),
                "oi_chain_history": json.loads(
                    sensibull_data.get("oi_chain_history_json", "[]")
                ),
                "iv_chart_history": pd.read_json(
                    sensibull_data.get("iv_chart_history_json", "{}"), orient="split"
                ) if "iv_chart_history_json" in sensibull_data else pd.DataFrame(),
                "oi_history": pd.read_json(
                    sensibull_data.get("oi_history_json", "{}"), orient="split"
                ) if "oi_history_json" in sensibull_data else pd.DataFrame(),
            }

        # Zerodha context
        zerodha_data = await redis.hgetall(f"data:zerodha:{symbol}")
        if zerodha_data:
            stock.zerodha_ctx = {
                "last_notification_time": None,
                "option_chain": {
                    "current": pd.read_json(
                        zerodha_data["option_chain_current_json"], orient="split"
                    ) if "option_chain_current_json" in zerodha_data else None,
                    "next": None,
                },
                "futures_mdata": json.loads(
                    zerodha_data.get("futures_mdata_json", "{}")
                ),
                "futures_data": {
                    "current": pd.read_json(
                        zerodha_data.get("futures_data_current_json", "{}"),
                        orient="split",
                    ) if "futures_data_current_json" in zerodha_data else pd.DataFrame(),
                    "next": pd.DataFrame(),
                },
            }

        return stock

    @staticmethod
    async def get_options_live(redis: Redis, symbol: str) -> dict:
        """Read live options data from Redis."""
        raw = await redis.hgetall(f"data:options_live:{symbol}")
        options_live = {}
        for key, value in raw.items():
            # key format: "{strike}_{CE|PE}"
            parts = key.rsplit("_", 1)
            if len(parts) == 2:
                strike = float(parts[0])
                opt_type = parts[1]
                if strike not in options_live:
                    options_live[strike] = {}
                options_live[strike][opt_type] = json.loads(value)
        return options_live

    @staticmethod
    async def get_options_aggregate(redis: Redis, symbol: str) -> dict:
        """Read live options aggregate from Redis."""
        raw = await redis.hgetall(f"data:options_agg:{symbol}")
        return {k: json.loads(v) if v.startswith("{") else v for k, v in raw.items()}
```

### 14.4 analysis-engine Worker Stub

```python
# services/analysis-engine/main.py
import asyncio
import gc
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from redis.asyncio import Redis
from common.logging_util import logger
from services.common.stream_consumer import StreamConsumer
from services.common.stock_proxy import StockProxy
from analyser.Analyser import AnalyserOrchestrator
from analyser.VolumeAnalyser import VolumeAnalyser
from analyser.TechnicalAnalyser import TechnicalAnalyser
from analyser.candleStickPatternAnalyser import CandleStickAnalyser
from analyser.IVAnalyser import IVAnalyser
from analyser.Futures_Analyser import FuturesAnalyser
from analyser.PCRAnalyser import PCRAnalyser
from analyser.MaxPainAnalyser import MaxPainAnalyser
from analyser.OIChainAnalyser import OIChainAnalyser
from analyser.GEXAnalyser import GEXAnalyser
from analyser.PanicModeAnalyser import PanicModeAnalyser
from analyser.OptionSellerCompositeAnalyser import OptionSellerCompositeAnalyser
from common.scoring import should_notify, calculate_score


class AnalysisWorker(StreamConsumer):
    def __init__(self, redis: Redis, worker_id: str):
        super().__init__(
            redis=redis,
            stream="orchestrator:analysis_jobs",
            group="analysis-workers",
            consumer_name=f"worker-{worker_id}",
            batch_size=1,
            block_ms=2000,
            ack_timeout=120,
        )
        self.orchestrator = self._build_orchestrator()

    def _build_orchestrator(self) -> AnalyserOrchestrator:
        orch = AnalyserOrchestrator()
        orch.register(VolumeAnalyser())
        orch.register(TechnicalAnalyser())
        orch.register(CandleStickAnalyser())
        orch.register(IVAnalyser())
        orch.register(FuturesAnalyser())
        orch.register(PCRAnalyser())
        orch.register(MaxPainAnalyser())
        orch.register(OIChainAnalyser())
        orch.register(GEXAnalyser())
        orch.register(PanicModeAnalyser())
        orch.register(OptionSellerCompositeAnalyser())
        return orch

    async def _process(self, msg_id: str, fields: dict):
        symbol = fields["symbol"]
        mode = fields["mode"]
        cycle_id = fields["cycle_id"]

        logger.info(f"[worker] Processing {symbol} for cycle {cycle_id}")

        try:
            stock = await StockProxy.from_redis(
                self.redis, symbol, is_index=(fields.get("is_index") == "true")
            )

            if mode == "intraday":
                self.orchestrator.run_all_intraday(stock)
            else:
                self.orchestrator.run_all_positional(stock)

            score_result = calculate_score(stock.analysis)
            should_send, score_result = should_notify(stock.analysis)

            result = {
                "job_id": fields.get("job_id", msg_id),
                "cycle_id": cycle_id,
                "symbol": symbol,
                "mode": mode,
                "analysis": json.dumps(stock.analysis, default=str),
                "score_result": json.dumps({
                    "total_score": score_result.total_score,
                    "priority": score_result.priority.value,
                    "dominant_sentiment": score_result.dominant_sentiment,
                    "confidence_pct": score_result.confidence_pct,
                    "should_notify": should_send,
                }, default=str),
                "timestamp": str(asyncio.get_event_loop().time()),
            }

            await self.redis.xadd("analysis:results", result)
            await self.redis.hset(
                f"analysis:latest:{symbol}",
                mapping={
                    "analysis_json": result["analysis"],
                    "score_result_json": result["score_result"],
                    "timestamp": result["timestamp"],
                },
            )

            logger.info(
                f"[worker] {symbol} done: score={score_result.total_score}, "
                f"priority={score_result.priority.value}, notify={should_send}"
            )

        except Exception as e:
            logger.error(f"[worker] {symbol} failed: {e}", exc_info=True)
            await self.redis.xadd("analysis:errors", {
                "job_id": fields.get("job_id", msg_id),
                "symbol": symbol,
                "error": str(e),
                "timestamp": str(asyncio.get_event_loop().time()),
            })

        finally:
            gc.collect()  # Prevent gen-2 accumulation across symbols


async def main():
    worker_id = os.environ.get("WORKER_ID", "1")
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = Redis.from_url(redis_url, decode_responses=True)

    worker = AnalysisWorker(redis, worker_id)

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    logger.info(f"[worker-{worker_id}] Starting analysis worker")

    # Start consumer and health check concurrently
    consumer_task = asyncio.create_task(worker.start())
    stop_task = asyncio.create_task(stop_event.wait())

    await asyncio.wait([consumer_task, stop_task], return_when=asyncio.FIRST_COMPLETED)
    await worker.stop()
    consumer_task.cancel()
    await redis.aclose()
    logger.info(f"[worker-{worker_id}] Shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
```

### 14.5 Coordinator Service Stub (Compact Mode — Laptop)

```python
# services/coordinator/main.py
"""
Compact mode entry point — runs orchestrator + intelligence + bot
in a single asyncio process. Saves ~500MB RAM vs 3 separate processes.

When you get a second machine, split this into 3 separate services:
  - services/orchestrator/main.py
  - services/intelligence-service/main.py
  - services/bot-service/main.py

The modules (cycle_manager, correlator, narrator, commands) are the
SAME code — just run as separate processes instead of asyncio tasks.
Zero code changes needed to split.
"""
import asyncio
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from redis.asyncio import Redis
from common.logging_util import logger

# Import the three sub-modules (shared code, same files as standalone mode)
from services.coordinator.cycle_manager import CycleManager
from services.coordinator.signal_bus import RedisSignalBus
from services.coordinator.correlator import RedisCorrelator
from services.coordinator.narrator import MarketNarrator
from services.coordinator.llm_client import GeminiClient
from services.coordinator.context_builder import ContextBuilder
from services.coordinator.commands import register_all as register_bot_commands


async def main():
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = Redis.from_url(redis_url, decode_responses=True)

    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("[coordinator] Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # ── Task 1: Orchestrator (cycle scheduling) ──────────────────────────
    cycle_mgr = CycleManager(redis)
    orchestrator_task = asyncio.create_task(
        cycle_mgr.run(stop_event), name="orchestrator"
    )

    # ── Task 2: Intelligence (signal correlation + LLM) ──────────────────
    signal_bus = RedisSignalBus(redis)
    llm_client = GeminiClient()
    context_builder = ContextBuilder(redis)
    correlator = RedisCorrelator(redis, signal_bus)
    narrator = MarketNarrator(llm_client, context_builder)
    intelligence_task = asyncio.create_task(
        _run_intelligence(redis, signal_bus, correlator, narrator, stop_event),
        name="intelligence"
    )

    # ── Task 3: Bot (Telegram commands) ──────────────────────────────────
    bot_task = asyncio.create_task(
        _run_bot(redis, stop_event), name="bot"
    )

    logger.info("[coordinator] Started: orchestrator + intelligence + bot")

    # Wait for any task to fail or stop signal
    all_tasks = [orchestrator_task, intelligence_task, bot_task]
    done, pending = await asyncio.wait(
        all_tasks + [asyncio.create_task(stop_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel remaining tasks
    for task in pending:
        task.cancel()

    await asyncio.gather(*pending, return_exceptions=True)
    await redis.aclose()
    logger.info("[coordinator] Shut down cleanly")


async def _run_intelligence(redis, signal_bus, correlator, narrator, stop_event):
    """Consume signals, detect confluences, generate narratives."""
    while not stop_event.is_set():
        try:
            signals = await signal_bus.read_batch(count=10, block=2000)
            for signal in signals:
                confluence = correlator.on_signal(signal)
                if confluence and confluence.level == "HIGH":
                    narrator.narrate_async(confluence)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[intelligence] Error: {e}")
            await asyncio.sleep(1)


async def _run_bot(redis, stop_event):
    """Run Telegram bot polling — reads from Redis for command data."""
    from telegram.ext import ApplicationBuilder
    from common.constants import TELEGRAM_INTRADAY_TOKEN

    app = ApplicationBuilder().token(TELEGRAM_INTRADAY_TOKEN).build()
    register_bot_commands(app, redis)

    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await stop_event.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
```

### 14.6 Updated Makefile Targets

```makefile
# ─── Service Management ─────────────────────────────────────────────────────

# Redis
.PHONY: redis-start redis-stop redis-status redis-cli
redis-start:
	sudo systemctl start stockanalysis-redis
redis-stop:
	sudo systemctl stop stockanalysis-redis
redis-status:
	sudo systemctl status stockanalysis-redis
redis-cli:
	redis-cli -h localhost -p 6379

# Individual services
.PHONY: svc-start svc-stop svc-restart svc-status svc-logs
svc-start:
	@test -n "$(SVC)" || { echo "Usage: make svc-start SVC=data-gateway"; exit 1; }
	sudo systemctl start stockanalysis-$(SVC)
svc-stop:
	@test -n "$(SVC)" || { echo "Usage: make svc-stop SVC=data-gateway"; exit 1; }
	sudo systemctl stop stockanalysis-$(SVC)
svc-restart:
	@test -n "$(SVC)" || { echo "Usage: make svc-restart SVC=data-gateway"; exit 1; }
	sudo systemctl restart stockanalysis-$(SVC)
svc-status:
	@test -n "$(SVC)" || { echo "Usage: make svc-status SVC=data-gateway"; exit 1; }
	sudo systemctl status stockanalysis-$(SVC)
svc-logs:
	@test -n "$(SVC)" || { echo "Usage: make svc-logs SVC=data-gateway"; exit 1; }
	journalctl -u stockanalysis-$(SVC) -n 50 --no-pager

# Analysis workers (template unit)
.PHONY: workers-start workers-stop workers-scale
workers-start:
	sudo systemctl start stockanalysis-analysis@1
	sudo systemctl start stockanalysis-analysis@2
workers-stop:
	sudo systemctl stop stockanalysis-analysis@1
	sudo systemctl stop stockanalysis-analysis@2
workers-scale:
	@test -n "$(N)" || { echo "Usage: make workers-scale N=3"; exit 1; }
	@for i in $$(seq 1 $(N)); do sudo systemctl start stockanalysis-analysis@$$i; done

# All services — COMPACT mode (single laptop, 5 processes)
.PHONY: svc-start-all svc-stop-all svc-status-all
svc-start-all:
	sudo systemctl start stockanalysis-redis
	sudo systemctl start stockanalysis-data-gateway
	sudo systemctl start stockanalysis-coordinator
	sudo systemctl start stockanalysis-analysis@1
	sudo systemctl start stockanalysis-notification
svc-stop-all:
	sudo systemctl stop stockanalysis-notification
	sudo systemctl stop stockanalysis-analysis@1
	sudo systemctl stop stockanalysis-coordinator
	sudo systemctl stop stockanalysis-data-gateway
	sudo systemctl stop stockanalysis-redis
svc-status-all:
	@for svc in redis data-gateway coordinator analysis@1 notification; do
		echo "--- stockanalysis-$$svc ---"
		systemctl is-active stockanalysis-$$svc 2>/dev/null || echo "not loaded"
	done

# All services — FULL mode (when splitting coordinator into separate services)
.PHONY: svc-start-full svc-stop-full svc-status-full
svc-start-full:
	sudo systemctl start stockanalysis-redis
	sudo systemctl start stockanalysis-data-gateway
	sudo systemctl start stockanalysis-orchestrator
	sudo systemctl start stockanalysis-analysis@1
	sudo systemctl start stockanalysis-intelligence
	sudo systemctl start stockanalysis-notification
	sudo systemctl start stockanalysis-bot
svc-stop-full:
	sudo systemctl stop stockanalysis-bot
	sudo systemctl stop stockanalysis-notification
	sudo systemctl stop stockanalysis-intelligence
	sudo systemctl stop stockanalysis-analysis@1
	sudo systemctl stop stockanalysis-orchestrator
	sudo systemctl stop stockanalysis-data-gateway
	sudo systemctl stop stockanalysis-redis
svc-status-full:
	@for svc in redis data-gateway orchestrator analysis@1 intelligence notification bot; do
		echo "--- stockanalysis-$$svc ---"
		systemctl is-active stockanalysis-$$svc 2>/dev/null || echo "not loaded"
	done

# Redis stream inspection
.PHONY: stream-info stream-pending stream-lag
stream-info:
	@echo "Stream lengths:"
	@for s in orchestrator:cycle_trigger orchestrator:analysis_jobs \
	          analysis:results intelligence:signals intelligence:narratives \
	          intelligence:confluences; do
		echo "  $$s: $$(redis-cli XLEN $$s)"
	done
stream-pending:
	@for s in orchestrator:analysis_jobs analysis:results intelligence:signals; do
		echo "=== $$s ==="
		redis-cli XPENDING $$s analysis-workers 2>/dev/null || \
		redis-cli XPENDING $$s notifier 2>/dev/null || \
		redis-cli XPENDING $$s intelligence 2>/dev/null || echo "  no group"
	done

# Deploy (updated for multi-service)
.PHONY: deploy-services
deploy-services:
	PYTHONPATH=$(CURDIR) $(PYTHON) scripts/deploy_services.py

# Laptop setup (one-time)
.PHONY: laptop-setup
laptop-setup:
	@echo "Configuring laptop for 24/7 service operation..."
	sudo cp configs/redis.conf /etc/redis/redis.conf
	sudo systemctl restart redis
	@echo "Disabling suspend on lid close..."
	sudo sed -i 's/^#HandleLidSwitch=.*/HandleLidSwitch=ignore/' /etc/systemd/logind.conf
	sudo sed -i 's/^#HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/' /etc/systemd/logind.conf
	sudo systemctl restart systemd-logind
	@echo "Installing systemd units..."
	sudo cp scripts/system_config/stockanalysis-*.service /etc/systemd/system/
	sudo cp scripts/system_config/stockanalysis-*.timer /etc/systemd/system/
	sudo systemctl daemon-reload
	@echo "Done. Run 'make svc-start-all' to start."
```

---

## Appendix A: Monolith → Microservices Mapping

| Monolith Component | Microservices Destination | Change |
|--------------------|--------------------------|--------|
| `intraday/intraday_monitor.py` main loop | `services/coordinator/main.py` (compact) or `services/orchestrator/main.py` (full) | Cycle logic extracted |
| `intraday/intraday_monitor.py` crash handler | Each service's `main.py` | Per-service exception handling |
| `intraday/intraday_monitor.py` zombie watchdog | `services/coordinator/` or `services/orchestrator/` | Reads `data:options_agg:*` timestamps from Redis |
| `common/shared.py` AppContext | Redis hashes + per-service env vars | Global singleton eliminated |
| `common/Stock.py` | `services/common/stock_proxy.py` | Redis-backed reconstruction |
| `zerodha/zerodha_analysis.py` | `services/data_gateway/zerodha_manager.py` | Publishes ticks to Redis |
| `zerodha/tick_store.py` | `services/data_gateway/tick_store.py` | In-memory in data-gateway, aggregate published to Redis |
| `zerodha/futures_fetcher.py` | `services/data_gateway/` | Called by data-gateway, results to Redis |
| `zerodha/live_options_engine.py` | `services/coordinator/` (compact) or `services/intelligence-service/` (full) | Reads from Redis pub/sub, emits signals to stream |
| `zerodha/live_stock_engine.py` | `services/coordinator/` (compact) or `services/intelligence-service/` (full) | Reads from Redis pub/sub, emits signals to stream |
| `fno/sensibull_fetcher.py` | `services/data_gateway/sensibull_fetcher.py` | Async HTTP, results to Redis |
| `fno/sensibull_feed.py` | `services/data_gateway/sensibull_feed.py` | Publishes to Redis |
| `analyser/*.py` (12 analysers) | `services/analysis-engine/` | **Unchanged** — imported as-is |
| `analyser/Analyser.py` | `services/analysis-engine/` | **Unchanged** |
| `common/scoring.py` | `services/analysis-engine/` + `services/notification-service/` | Imported as-is |
| `common/constants.py` | Shared `common/` package | **Unchanged** |
| `intelligence/signal_bus.py` | `services/coordinator/signal_bus.py` (compact) or `services/intelligence-service/` (full) | In-memory → Redis Stream |
| `intelligence/correlator.py` | `services/coordinator/correlator.py` (compact) or `services/intelligence-service/` (full) | Buffer in Redis ZSet |
| `intelligence/narrator.py` | `services/coordinator/narrator.py` (compact) or `services/intelligence-service/` (full) | Async LLM calls |
| `intelligence/context_builder.py` | `services/coordinator/context_builder.py` (compact) or `services/intelligence-service/` (full) | Reads from Redis instead of shared.app_ctx |
| `intelligence/llm_client.py` | `services/coordinator/llm_client.py` (compact) or `services/intelligence-service/` (full) | Budget in Redis |
| `notification/Notification.py` | `services/notification-service/telegram_sender.py` | Consumes from stream |
| `notification/bot_listener.py` | `services/coordinator/main.py` (compact) or `services/bot-service/main.py` (full) | Reads from Redis |
| `notification/commands/*.py` | `services/coordinator/commands/` (compact) or `services/bot-service/commands/` (full) | Reads from Redis |
| `premarket/premarket_report.py` | `services/data_gateway/` | Publishes to `premarket:reports` stream |
| `post_market_analysis/` | `services/analysis-engine/` (positional mode) | Publishes to `postmarket:reports` stream |
| `auth/auth_login.py` | `services/auth-service/main.py` | Listens on `auth:commands` stream |
| `scripts/deploy.py` | `scripts/deploy_services.py` | Multi-service deployment |
| `scripts/service_stop.py` | Updated for multi-service | Stop all services |
| `backtest/` | Stays standalone | Not part of real-time services |
| `ml_pipeline/` | Stays standalone | Not part of real-time services |
| `sentiment/` | Stays standalone or → `services/sentiment-service/` | Phase 2 |

## Appendix B: Redis Configuration

```redis
# configs/redis.conf — tuned for 8 GB laptop
bind 127.0.0.1                    # localhost only in Phase 1 (no password needed)
# When exposing to network (Phase 2): uncomment these:
# bind 127.0.0.1 <laptop-lan-ip>
# requirepass <a-strong-password>
port 6379
maxmemory 128mb                   # Tight budget for laptop (was 256mb for server)
maxmemory-policy allkeys-lru
save ""                           # No disk persistence (state is ephemeral, saves SSD wear)
appendonly no                     # No AOF (saves disk + CPU on laptop)
timeout 0                         # No client timeout
tcp-keepalive 60                  # Keepalive for long connections
hz 10                             # Background task frequency
lazyfree-lazy-eviction yes        # Async eviction for large keys
lazyfree-lazy-expire yes          # Async expiry
notify-keyspace-events ""         # Disable keyspace notifications (not needed)
io-threads 1                      # Single IO thread (laptop has few cores)
```

## Appendix C: Systemd Unit Templates

### Analysis worker (template — scales by starting @1, @2, @3...)

```ini
# /etc/systemd/system/stockanalysis-analysis@.service
[Unit]
Description=StockAnalysis Analysis Worker (%i)
Requires=stockanalysis-redis.service
After=stockanalysis-redis.service

[Service]
Type=simple
User=hacker
WorkingDirectory=/home/hacker/StockAnalysis
Environment=PYTHONPATH=/home/hacker/StockAnalysis
Environment=WORKER_ID=%i
Environment=REDIS_URL=redis://localhost:6379
EnvironmentFile=/home/hacker/StockAnalysis/.env
ExecStart=/home/hacker/StockAnalysis/.venv/bin/python -m services.analysis_engine.main
Restart=always
RestartSec=10
StartLimitBurst=5
StartLimitIntervalSec=300
StandardOutput=journal
StandardError=journal

# Laptop thermal protection — don't pin CPU at 100%
CPUQuota=75%
CPUWeight=50

# Memory protection — OOM-kill this service, not the laptop
MemoryMax=800M
MemorySwapMax=256M

[Install]
WantedBy=multi-user.target
```

### Data gateway (highest priority — holds WebSocket connections)

```ini
# /etc/systemd/system/stockanalysis-data-gateway.service
[Unit]
Description=StockAnalysis Data Gateway
Requires=stockanalysis-redis.service
After=stockanalysis-redis.service
# Prevent laptop suspend while data-gateway is running
Conflicts=suspend.target hibernate.target hybrid-sleep.target

[Service]
Type=simple
User=hacker
WorkingDirectory=/home/hacker/StockAnalysis
Environment=PYTHONPATH=/home/hacker/StockAnalysis
Environment=REDIS_URL=redis://localhost:6379
EnvironmentFile=/home/hacker/StockAnalysis/.env
ExecStart=/home/hacker/StockAnalysis/.venv/bin/python -m services.data_gateway.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Data gateway gets higher CPU priority (WebSocket ticks are latency-sensitive)
CPUQuota=80%
CPUWeight=100

# Largest memory consumer — TickStore + option chains
MemoryMax=1500M
MemorySwapMax=512M

[Install]
WantedBy=multi-user.target
```

### Merged coordinator (compact mode — orchestrator + intelligence + bot)

```ini
# /etc/systemd/system/stockanalysis-coordinator.service
[Unit]
Description=StockAnalysis Coordinator (Orchestrator + Intelligence + Bot)
Requires=stockanalysis-redis.service stockanalysis-data-gateway.service
After=stockanalysis-redis.service stockanalysis-data-gateway.service

[Service]
Type=simple
User=hacker
WorkingDirectory=/home/hacker/StockAnalysis
Environment=PYTHONPATH=/home/hacker/StockAnalysis
Environment=REDIS_URL=redis://localhost:6379
Environment=COMPACT_MODE=1
EnvironmentFile=/home/hacker/StockAnalysis/.env
ExecStart=/home/hacker/StockAnalysis/.venv/bin/python -m services.coordinator.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

CPUQuota=60%
CPUWeight=50
MemoryMax=600M
MemorySwapMax=256M

[Install]
WantedBy=multi-user.target
```

### Redis (laptop-tuned)

```ini
# /etc/systemd/system/stockanalysis-redis.service
[Unit]
Description=StockAnalysis Redis Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/redis-server /home/hacker/StockAnalysis/configs/redis.conf
ExecStop=/usr/bin/redis-cli shutdown
Restart=always
RestartSec=5
LimitNOFILE=10032

# Redis is tiny but critical — protect it
CPUWeight=80
MemoryMax=200M

[Install]
WantedBy=multi-user.target
```
