# 🚨 CRITICAL: BEHAVIOR & TOKEN EFFICIENCY
- **ABSOLUTE NO YAPPING:** You are interacting with an automated pipeline. Do not explain the code you write. Do not summarize changes. Do not output pleasantries, introductory, or concluding phrases. 
- **ALLOWED OUTPUT:** Output ONLY raw code, terminal commands, or necessary file patch modifications. Any conversational text is a strict violation of system constraints.
- **NO FULL-FILE REWRITES:** Use your edit/patch tools to modify specific functions. NEVER output an entire file into the chat.
- **TESTING AUTONOMY:** Run tests (e.g., `make test-fast`) to verify work. If a test fails, fix it autonomously. Do not ask for permission.

# Semantic Search (CRITICAL)
- You are connected to the `cocoindex-code` MCP server.
- **DO NOT** use `grep`, `find`, or read entire directories. 
- Use the semantic search tool to locate classes/functions first, then read specific files.


# Architecture & Constraints
- Indian NSE/BSE equity & derivatives analysis.
- Zerodha KiteConnect WebSocket + Sensibull REST/WS + yfinance + Telegram.
- **Entry point:** `intraday/intraday_monitor.py`
- **Signal Correlation:** `SignalBus` → `SignalCorrelator` → `MarketNarrator` (Gemini Flash LLM).
- **Registration Order:** `OptionSellerCompositeAnalyser` MUST be registered last.
- **Options Source:** When `OPTIONS_SOURCE=both`, Zerodha is authoritative. Sensibull enriches ONLY `{delta, gamma, theta, vega, iv, iv_change}` via `TickStore.update_option_tick(merge=True)`.


# Communication & Token Efficiency
- **No Yapping:** Do not explain the code you write unless I explicitly ask "why?". Output only the code, terminal commands, or necessary file modifications. Skip all pleasantries and introductory phrases.
- **No Full-File Rewrites:** Use your edit/patch tools to modify specific functions or lines. **NEVER** output or rewrite an entire file into the chat.
- **Testing Autonomy:** When writing or modifying features, run the relevant tests (e.g., `make test-fast`) to verify your work. If a test fails, read the error and iterate autonomously until it passes. Do not stop to ask me for permission to fix your own errors.

# Coding Conventions
- **No `print()`:** Always use `logger.info/debug/warning/error` from `common/logging_util.py`.
- **HTTP Timeouts:** Always split connect/read (e.g., `timeout=(5, 10)`).
- **Environment Variables:** Read via `os.getenv()` using constants in `common/constants.py`.
- **Logging Format:** Must use exactly: `[SIGNAL_KEY] <symbol> | SOURCE <raw_field>=<value>`.
- **Log Levels:** `DEBUG` for gates/conditions. `INFO` strictly for `EMITTED` events. `ERROR` with `traceback.format_exc()` for exceptions.

# Commands
- `make run-dev` : Dev intraday loop
- `make run-dev-positional` : Dev EOD run
- `make test-fast` : Pytest suite (stop on first fail)
- `make lint` / `make format` / `make typecheck` : Ruff / Pyright
- `make deploy` : rsync + SSH to production
- `make server-logs-500` : Check production `monolith.log`