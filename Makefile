SHELL := /bin/bash
PYTHON := .venv/bin/python
PIP    := .venv/bin/pip
SERVER := hacker@100.92.21.31

.DEFAULT_GOAL := help

# ──────────────────────────────────────────────────────────────────────────────
# Help
# ──────────────────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@echo ""
	@echo "StockAnalysis — available targets"
	@echo "────────────────────────────────────────────────────────────"
	@echo "  Setup"
	@echo "    venv           Create virtual environment (.venv/)"
	@echo "    install        Install production dependencies (requirements.txt)"
	@echo "    install-dev    Install prod + dev/test tools (requirements-dev.txt)"
	@echo "    install-deploy Install deploy tools on laptop (requirements-deploy.txt)"
	@echo "    env-check      Verify required .env variables are set"
	@echo ""
	@echo "  Run"
	@echo "    run-prod           Intraday monitor — PRODUCTION=1"
	@echo "    run-dev            Dev intraday + notification service (auto start+stop)"
	@echo "    run-dev-positional Dev positional (EOD) — PRODUCTION=0 DEV_POSITIONAL=1"
	@echo "                       Append DEV_NOTIFY=1 to either run-dev target to send Telegram alerts in dev mode"
	@echo "    run-dev-stock-intraday   Dev single stock intraday:   make run-dev-stock-intraday STOCK=RELIANCE"
	@echo "    run-dev-stock-positional Dev single stock positional: make run-dev-stock-positional STOCK=RELIANCE"
	@echo "    run-dev-index-intraday   Dev single index intraday:   make run-dev-index-intraday INDEX=NIFTY"
	@echo "    run-dev-index-positional Dev single index positional: make run-dev-index-positional INDEX=NIFTY"
	@echo "    run-dev-stop       Stop notification service (auto-stops with Ctrl+C on run-dev)"
	@echo "    run-premarket      Global cues + pre-open reports (--premarket shortcut)"
	@echo "    run-postmarket     Post-market analysis pipeline"
	@echo "    deploy             Deploy to EC2 via SSH"
	@echo "    service-stop       Start EC2 (if stopped), stop stock_analysis.service"
	@echo "                        On holidays/weekends: exits if instance stopped, skips 15s wait if running"
	@echo "    service-stop-force Dev: same but bypasses holiday guard (SSH retry-poll instead of fixed sleep)"
	@echo ""
	@echo "  Test"
	@echo "    test          Run full test suite"
	@echo "    test-fast     Run tests, stop on first failure"
	@echo "    test-cov      Run tests with coverage report"
	@echo "    test-module   Run tests for a specific module:"
	@echo "                    make test-module MODULE=premarket"
	@echo ""
	@echo "  Code quality"
	@echo "    lint          Run ruff linter"
	@echo "    format        Auto-format with ruff"
	@echo "    typecheck     Run pyright type checker"
	@echo ""
	@echo "  Maintenance"
	@echo "    update-derivatives  Refresh final_derivatives_list.json from Zerodha + NSE"
	@echo "    logs          Monolith log — tail stock_monitor.log (legacy)"
	@echo "    logs-500      Tail stock_monitor.log (last 500 lines)"
	@echo "    logs-1000     Tail stock_monitor.log (last 1000 lines)"
	@echo "    logs-follow   Follow stock_monitor.log live"
	@echo "    logs-grep     Grep stock_monitor.log: make logs-grep Q=RELIANCE"
	@echo "    logs-all      View last 20 lines of every service log"
	@echo "    logs-all-follow  Follow all service logs live"
	@echo "    logs-svc      View one service: make logs-svc SVC_LOG=data-gateway"
	@echo "    logs-svc-follow  Follow one service live"
	@echo "    logs-service  List all available service logs"
	@echo "    clean         Remove __pycache__, .pyc, pytest cache"
	@echo "    clean-all     clean + remove .venv"
	@echo ""
	@echo "  Server (hacker@100.92.21.31)"
	@echo "    server-ssh          Open interactive SSH session"
	@echo "    server-logs         Tail last 50 lines of stock_monitor.log on server"
	@echo "    server-logs-500     Tail last 500 lines of stock_monitor.log on server"
	@echo "    server-logs-1000    Tail last 1000 lines of stock_monitor.log on server"
	@echo "    server-logs-follow  Live-follow stock_monitor.log on server"
	@echo "    server-logs-copy    Copy stock_monitor.log from server to local logs/"
	@echo "    server-status       Show stock_analysis.service status"
	@echo "    server-start        Start stock_analysis.service"
	@echo "    server-restart      Restart stock_analysis.service"
	@echo "    server-stop         Stop stock_analysis.service"
	@echo "    server-pull         git pull on server repo"
	@echo "    server-df           Disk usage on server"
	@echo "    server-redis-status     Redis status + memory on server"
	@echo "    server-redis-start      Start Redis on server"
	@echo "    server-redis-stop       Stop Redis on server"
	@echo "    server-redis-restart    Restart Redis on server"
	@echo "    server-redis-config     Apply Redis config on server"
	@echo "    server-notification-start     Start notification service on server"
	@echo "    server-notification-stop      Stop notification service on server"
	@echo "    server-notification-status    Notification service status + dead letters"
	@echo "    server-notification-logs      Notification service logs on server"
	@echo "    server-notify-test       Send test notification via server Redis"
	@echo "    server-dead-letter       View dead letters on server"
	@echo "    server-svcs-status       All StockAnalysis service statuses"
	@echo "    update-enctoken     Update ZERODHA_ENC_TOKEN on server .env:"
	@echo "                          make update-enctoken TOKEN=<your_enc_token>"
	@echo "    auth-run            Run auth_login.py locally (skips if already ran today)"
	@echo "    auth-force          Force-refresh enctoken locally (bypasses once-per-day guard)"
	@echo "    server-auth-force   Force-refresh enctoken on server"
	@echo ""
	@echo "  Redis"
	@echo "    redis-install  Install Redis via Homebrew"
	@echo "    redis-start    Start Redis service"
	@echo "    redis-stop     Stop Redis service"
	@echo "    redis-restart  Restart Redis service"
	@echo "    redis-status   Check Redis status + memory usage"
	@echo "    redis-cli      Open interactive Redis CLI"
	@echo "    redis-config   Apply production config (128MB, LRU, no persistence)"
	@echo ""
	@echo "  Services"
	@echo "    run-notification       Start notification service (stream consumer)"
	@echo "────────────────────────────────────────────────────────────"

# ──────────────────────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────────────────────
.PHONY: venv
venv:
	python3 -m venv .venv
	@echo "Virtual environment created. Run: source .venv/bin/activate"

.PHONY: install
install:
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

.PHONY: install-dev
install-dev:
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

.PHONY: install-deploy
install-deploy:
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-deploy.txt

.PHONY: env-check
env-check:
	@echo "Checking required environment variables..."
	@test -f .env || { echo "ERROR: .env file not found. Copy .env.template → .env"; exit 1; }
	@for var in TELEGRAM_INTRADAY_TOKEN TELEGRAM_INTRADAY_CHAT_ID \
	            TELEGRAM_POSITIONAL_TOKEN TELEGRAM_POSITIONAL_CHAT_ID \
	            EC2_INSTANCE_ID SSH_KEY_PATH \
	            AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do \
	    grep -q "^$$var=.\+" .env || \
	        { echo "WARNING: $$var is not set in .env"; }; \
	done
	@grep -q "^REDIS_URL=" .env 2>/dev/null || echo "WARNING: REDIS_URL is not set in .env (defaults to redis://localhost:6379)"
	@echo "env-check complete."

# ──────────────────────────────────────────────────────────────────────────────
# Redis
# ──────────────────────────────────────────────────────────────────────────────
.PHONY: redis-install redis-start redis-stop redis-restart redis-status redis-cli redis-config

redis-install:
	@which redis-server >/dev/null 2>&1 && echo "Redis is already installed ($(shell redis-cli --version))" || brew install redis

redis-start:
	brew services start redis
	@sleep 1
	@redis-cli ping || echo "Redis failed to start"

redis-stop:
	brew services stop redis

redis-restart:
	brew services restart redis
	@sleep 1
	@redis-cli ping || echo "Redis failed to restart"

redis-status:
	@redis-cli ping 2>/dev/null && echo "Redis: RUNNING" || echo "Redis: NOT RUNNING"
	@redis-cli INFO server 2>/dev/null | grep redis_version
	@redis-cli INFO memory 2>/dev/null | grep used_memory_human

redis-cli:
	redis-cli

redis-config:
	redis-cli CONFIG SET maxmemory 128mb
	redis-cli CONFIG SET maxmemory-policy allkeys-lru
	redis-cli CONFIG SET save ""
	@redis-cli CONFIG GET maxmemory maxmemory-policy save

# ──────────────────────────────────────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────────────────────────────────────
.PHONY: run-prod
run-prod:
	PRODUCTION=1 PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py

NOTIFICATION_PID_FILE := .notification.pid
NOTIFICATION_LOG := logs/notification-service.log

.PHONY: run-dev
run-dev:
	@echo "Starting notification service (background)..."
	@mkdir -p logs
	REDIS_URL=redis://localhost:6379 PYTHONPATH=$(CURDIR) nohup $(PYTHON) \
		services/notification-service/main.py --consumer-name dev-1 \
		> /dev/null 2>$(NOTIFICATION_LOG) & \
		echo $$! > $(NOTIFICATION_PID_FILE)
	@sleep 2
	@if kill -0 $$(cat $(NOTIFICATION_PID_FILE) 2>/dev/null) 2>/dev/null; then \
		echo "Notification service started (pid $$(cat $(NOTIFICATION_PID_FILE)))."; \
		echo "Starting intraday monitor... Ctrl+C to stop everything."; \
	else \
		echo "Notification service failed. Check $(NOTIFICATION_LOG)"; \
		exit 1; \
	fi
	@trap '' INT; \
	PRODUCTION=0 DEV_INTRADAY=1 PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py; \
	EXIT_CODE=$$?; \
	$(MAKE) run-dev-stop >/dev/null 2>&1; \
	exit $$EXIT_CODE

.PHONY: run-dev-positional
run-dev-positional:
	@echo "Starting notification service (background)..."
	@mkdir -p logs
	REDIS_URL=redis://localhost:6379 PYTHONPATH=$(CURDIR) nohup $(PYTHON) \
		services/notification-service/main.py --consumer-name dev-1 \
		> /dev/null 2>$(NOTIFICATION_LOG) & \
		echo $$! > $(NOTIFICATION_PID_FILE)
	@sleep 2
	@if kill -0 $$(cat $(NOTIFICATION_PID_FILE) 2>/dev/null) 2>/dev/null; then \
		echo "Notification service started (pid $$(cat $(NOTIFICATION_PID_FILE)))."; \
		echo "Running positional analysis..."; \
	else \
		echo "Notification service failed. Check $(NOTIFICATION_LOG)"; \
		exit 1; \
	fi
	@trap '' INT; \
	PRODUCTION=0 DEV_POSITIONAL=1 PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py; \
	EXIT_CODE=$$?; \
	$(MAKE) run-dev-stop >/dev/null 2>&1; \
	exit $$EXIT_CODE

.PHONY: run-dev-stop
run-dev-stop:
	@if [ -f $(NOTIFICATION_PID_FILE) ]; then \
		PID=$$(cat $(NOTIFICATION_PID_FILE)); \
		echo "Stopping notification service (pid $$PID)..."; \
		kill $$PID 2>/dev/null && echo "Stopped." || echo "Already stopped."; \
		rm -f $(NOTIFICATION_PID_FILE); \
	fi

# Usage: make run-dev-stock-intraday STOCK=RELIANCE
#        make run-dev-stock-positional STOCK=RELIANCE
STOCK ?= 
.PHONY: run-dev-stock-intraday
run-dev-stock-intraday:
	@test -n "$(STOCK)" || { echo "ERROR: STOCK is required. Usage: make run-dev-stock-intraday STOCK=RELIANCE"; exit 1; }
	PRODUCTION=0 DEV_INTRADAY=1 PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py --stock $(STOCK)

.PHONY: run-dev-stock-positional
run-dev-stock-positional:
	@test -n "$(STOCK)" || { echo "ERROR: STOCK is required. Usage: make run-dev-stock-positional STOCK=RELIANCE"; exit 1; }
	PRODUCTION=0 DEV_POSITIONAL=1 PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py --stock $(STOCK)

# Usage: make run-dev-index-intraday INDEX=NIFTY
#        make run-dev-index-positional INDEX=NIFTY
INDEX ?= 
.PHONY: run-dev-index-intraday
run-dev-index-intraday:
	@test -n "$(INDEX)" || { echo "ERROR: INDEX is required. Usage: make run-dev-index-intraday INDEX=NIFTY"; exit 1; }
	PRODUCTION=0 DEV_INTRADAY=1 PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py --index $(INDEX)

.PHONY: run-dev-index-positional
run-dev-index-positional:
	@test -n "$(INDEX)" || { echo "ERROR: INDEX is required. Usage: make run-dev-index-positional INDEX=NIFTY"; exit 1; }
	PRODUCTION=0 DEV_POSITIONAL=1 PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py --index $(INDEX)

.PHONY: run-premarket

.PHONY: run-postmarket
run-postmarket:
	PYTHONPATH=$(CURDIR) $(PYTHON) -c "\
from post_market_analysis.runner import run_and_summarize; \
run_and_summarize()"

.PHONY: deploy
deploy:
	PYTHONPATH=$(CURDIR) $(PYTHON) scripts/deploy.py

.PHONY: service-stop
service-stop:
	@echo "Starting EC2 instance (if needed) and stopping stock_analysis.service..."
	PYTHONPATH=$(CURDIR) $(PYTHON) scripts/service_stop.py

# Dev: bypass holiday guard — start instance even on weekends/holidays.
# Uses SSH retry-poll to connect ASAP and stop the service before
# intraday_monitor.py can trigger an OS shutdown.
.PHONY: service-stop-force
service-stop-force:
	@echo "[DEV] Starting EC2 + stopping service (holiday guard bypassed)..."
	PYTHONPATH=$(CURDIR) $(PYTHON) scripts/service_stop.py --force

# ──────────────────────────────────────────────────────────────────────────────
# Test
# ──────────────────────────────────────────────────────────────────────────────
.PHONY: test
test:
	PYTHONPATH=$(CURDIR) $(PYTHON) -m pytest tests/

.PHONY: test-fast
test-fast:
	PYTHONPATH=$(CURDIR) $(PYTHON) -m pytest tests/ -x

.PHONY: test-cov
test-cov:
	PYTHONPATH=$(CURDIR) $(PYTHON) -m pytest tests/ \
	    --cov=. \
	    --cov-report=term-missing \
	    --cov-omit=".venv/*,tests/*,scripts/*,data/*,logs/*"

# Usage: make test-module MODULE=premarket
MODULE ?= tests
.PHONY: test-module
test-module:
	PYTHONPATH=$(CURDIR) $(PYTHON) -m pytest tests/$(MODULE)/

# ──────────────────────────────────────────────────────────────────────────────
# Code quality
# ──────────────────────────────────────────────────────────────────────────────
.PHONY: lint
lint:
	.venv/bin/ruff check .

.PHONY: format
format:
	.venv/bin/ruff format .

.PHONY: typecheck
typecheck:
	.venv/bin/pyright

# ──────────────────────────────────────────────────────────────────────────────
# Maintenance
# ──────────────────────────────────────────────────────────────────────────────
LOG_FILE := logs/stock_monitor.log

.PHONY: logs
logs:
	@tail -50 $(LOG_FILE) 2>/dev/null || echo "No $(LOG_FILE) found."

.PHONY: logs-500
logs-500:
	@tail -500 $(LOG_FILE) 2>/dev/null || echo "No $(LOG_FILE) found."

.PHONY: logs-1000
logs-1000:
	@tail -1000 $(LOG_FILE) 2>/dev/null || echo "No $(LOG_FILE) found."

.PHONY: logs-follow
logs-follow:
	@tail -f $(LOG_FILE) 2>/dev/null || echo "No $(LOG_FILE) found."

.PHONY: logs-grep
logs-grep:
	@test -n "$(Q)" || { echo "ERROR: Q is required. Usage: make logs-grep Q=RELIANCE"; exit 1; }
	@grep -i "$(Q)" $(LOG_FILE) 2>/dev/null | tail -100 || echo "No matches for '$(Q)' in $(LOG_FILE)."

# ─── Per-service log targets ──────────────────────────────────────────────────
LOG_DIR := logs
SVC_LOG ?= notification-service

.PHONY: logs-all logs-all-follow logs-svc logs-svc-follow logs-service
logs-all:
	@echo "=== All service logs (last 20 lines each) ==="
	@for f in $(LOG_DIR)/*service*.log; do \
		if [ -f "$$f" ]; then \
			name=$$(basename "$$f" .log); \
			echo ""; \
			echo "─── $$name ───"; \
			tail -20 "$$f"; \
		fi; \
	done
	@echo ""; \
	echo "=== Monolith log (last 5 lines) ==="; \
	tail -5 $(LOG_FILE) 2>/dev/null || echo "(no monolith log)"

logs-all-follow:
	@echo "Following all service logs (Ctrl+C to stop)..."
	@for f in $(LOG_DIR)/*service*.log; do \
		if [ -f "$$f" ]; then \
			basename "$$f" .log; \
		fi; \
	done | xargs tail -F 2>/dev/null || echo "No service logs found."

logs-svc:
	@test -n "$(SVC_LOG)" || { echo "Usage: make logs-svc SVC_LOG=data-gateway"; exit 1; }
	@tail -50 $(LOG_DIR)/$(SVC_LOG).log 2>/dev/null || echo "No log found for $(SVC_LOG)."

logs-svc-follow:
	@test -n "$(SVC_LOG)" || { echo "Usage: make logs-svc-follow SVC_LOG=notification-service"; exit 1; }
	@tail -f $(LOG_DIR)/$(SVC_LOG).log 2>/dev/null || echo "No log found for $(SVC_LOG)."

logs-service:
	@echo "Available service logs:"
	@ls -lh $(LOG_DIR)/*service*.log $(LOG_DIR)/data-gateway.log 2>/dev/null || echo "(no service logs yet)"
	@echo ""
	@echo "  make logs-svc SVC_LOG=notification-service    View a specific service"
	@echo "  make logs-all                                  View all services"
	@echo "  make logs-all-follow                           Live-follow all services"
	@echo "  make logs                                      Monolith log (legacy)"

.PHONY: clean
clean:
	find . -type d -name __pycache__ -not -path "./.venv/*" | xargs rm -rf
	find . -type f -name "*.pyc"     -not -path "./.venv/*" | xargs rm -f
	rm -rf .pytest_cache .coverage htmlcov

.PHONY: update-derivatives
update-derivatives:
	PYTHONPATH=$(CURDIR) $(PYTHON) scripts/stock_derivative_list.py

.PHONY: clean-all
clean-all: clean
	rm -rf .venv
	@echo ".venv removed. Run 'make venv && make install' to rebuild."

# ──────────────────────────────────────────────────────────────────────────────
# Server
# ──────────────────────────────────────────────────────────────────────────────
.PHONY: server-ssh
server-ssh:
	ssh $(SERVER)

SERVER_LOG := ~/StockAnalysis/logs/stock_monitor.log

.PHONY: server-logs
server-logs:
	ssh $(SERVER) "tail -50 $(SERVER_LOG)"

.PHONY: server-logs-500
server-logs-500:
	ssh $(SERVER) "tail -500 $(SERVER_LOG)"

.PHONY: server-logs-1000
server-logs-1000:
	ssh $(SERVER) "tail -1000 $(SERVER_LOG)"

.PHONY: server-logs-follow
server-logs-follow:
	ssh $(SERVER) "tail -f $(SERVER_LOG)"

.PHONY: server-logs-copy
server-logs-copy:
	scp $(SERVER):$(SERVER_LOG) logs/stock_monitor.log
	@echo "Copied to logs/stock_monitor.log"

.PHONY: server-status
server-status:
	ssh $(SERVER) "systemctl status stockanalysis.service"

.PHONY: server-start
server-start:
	ssh $(SERVER) "sudo systemctl start stockanalysis.service"

.PHONY: server-restart
server-restart:
	ssh $(SERVER) "sudo systemctl restart stockanalysis.service"

.PHONY: server-stop
server-stop:
	ssh $(SERVER) "sudo systemctl stop stockanalysis.service"

.PHONY: server-pull
server-pull:
	ssh $(SERVER) "cd ~/StockAnalysis && git pull"

.PHONY: server-df
server-df:
	ssh $(SERVER) "df -h"

# ─── Server Redis ─────────────────────────────────────────────────────────────
.PHONY: server-redis-status server-redis-start server-redis-stop server-redis-restart server-redis-config

server-redis-status:
	@echo "=== Server Redis Status ==="
	ssh $(SERVER) "redis-cli ping 2>/dev/null || echo 'NOT RUNNING'"
	ssh $(SERVER) "redis-cli INFO memory 2>/dev/null | grep used_memory_human"
	ssh $(SERVER) "redis-cli CONFIG GET maxmemory maxmemory-policy save 2>/dev/null"

server-redis-start:
	ssh $(SERVER) "sudo systemctl start redis-server && redis-cli ping && echo 'Redis started'"

server-redis-stop:
	ssh $(SERVER) "sudo systemctl stop redis-server && echo 'Redis stopped'"

server-redis-restart:
	ssh $(SERVER) "sudo systemctl restart redis-server && redis-cli ping && echo 'Redis restarted'"

server-redis-config:
	ssh $(SERVER) "redis-cli CONFIG SET maxmemory 128mb \
		&& redis-cli CONFIG SET maxmemory-policy allkeys-lru \
		&& redis-cli CONFIG SET save '' \
		&& redis-cli CONFIG GET maxmemory maxmemory-policy save"

# ─── Server Notification Service ──────────────────────────────────────────────
.PHONY: server-notification-status server-notification-start server-notification-stop server-notification-logs
.PHONY: server-notify-test server-dead-letter server-svcs-status

server-notification-status:
	ssh $(SERVER) "systemctl status stockanalysis-notification --no-pager 2>/dev/null | head -10"
	@echo ""
	ssh $(SERVER) "echo 'Stream: notification:jobs — '; redis-cli XLEN notification:jobs; echo 'Dead letters: '; redis-cli XLEN notification:dead"
	@echo ""

server-notification-start:
	ssh $(SERVER) "sudo systemctl start stockanalysis-notification \
		&& echo 'Notification service started' \
		|| echo 'FAILED — check: journalctl -u stockanalysis-notification'"

server-notification-stop:
	ssh $(SERVER) "sudo systemctl stop stockanalysis-notification && echo 'Notification service stopped'"

server-notification-logs:
	ssh $(SERVER) "journalctl -u stockanalysis-notification --no-pager -n 50"

server-notify-test:
	ssh $(SERVER) "redis-cli XADD notification:jobs '*' \
		chat_type intraday \
		message '<b>🧪 Server Test</b>\n\nFrom: $(shell whoami)@$(shell hostname)\nTime: $(shell date)' \
		parse_mode HTML \
		message_type test \
		timestamp '$(shell date -u +%Y-%m-%dT%H:%M:%S)'"
	@echo "Test notification sent to server Redis."

server-dead-letter:
	@echo "Dead letters: $$(ssh $(SERVER) "redis-cli XLEN notification:dead 2>/dev/null || echo 0")"
	@ssh $(SERVER) "redis-cli XREAD COUNT 5 STREAMS notification:dead 0 2>/dev/null || true"

server-svcs-status:
	@for svc in redis-server stockanalysis stockanalysis-auth stockanalysis-notification; do \
		STATUS=$$(ssh $(SERVER) "systemctl is-active $$svc 2>/dev/null || echo not loaded"); \
		printf "  %-32s %s\n" "$$svc" "$$STATUS"; \
	done
	@ssh $(SERVER) "redis-cli INFO memory 2>/dev/null | grep used_memory_human" || true

# Usage: make update-enctoken TOKEN=<your_enc_token>
TOKEN ?=
.PHONY: update-enctoken
update-enctoken:
	@test -n "$(TOKEN)" || { echo "ERROR: TOKEN is required. Usage: make update-enctoken TOKEN=<enc_token>"; exit 1; }
	ssh $(SERVER) "sed -i 's|^ZERODHA_ENC_TOKEN=.*|ZERODHA_ENC_TOKEN=$(TOKEN)|' ~/StockAnalysis/.env && echo 'ZERODHA_ENC_TOKEN updated on server'"

.PHONY: auth-run
auth-run:
	PYTHONPATH=$(CURDIR) $(PYTHON) auth/auth_login.py

.PHONY: auth-force
auth-force:
	@echo "Force-refreshing Zerodha enctoken (bypassing once-per-day guard)..."
	PYTHONPATH=$(CURDIR) $(PYTHON) auth/auth_login.py --force

.PHONY: server-auth-force
server-auth-force:
	@echo "Force-refreshing enctoken on server (bypassing once-per-day guard)..."
	ssh $(SERVER) "cd ~/StockAnalysis && .venv/bin/python auth/auth_login.py --force"

# ──────────────────────────────────────────────────────────────────────────────
# Notification Service
# ──────────────────────────────────────────────────────────────────────────────
.PHONY: run-notification

run-notification:
	@echo "Starting notification-service..."
	@echo "Press Ctrl+C to stop."
	REDIS_URL=redis://localhost:6379 PYTHONPATH=$(CURDIR) $(PYTHON) services/notification-service/main.py --consumer-name local-1

.PHONY: svc-notification-logs svc-notification-check svc-notify-test svc-dead-letter

svc-notification-logs:
	@tail -50 logs/notification-service.log 2>/dev/null || echo "No notification service logs found."

svc-notification-check:
	@redis-cli XINFO GROUPS notification:jobs 2>/dev/null || echo "No consumer group yet (start notification service first)"
	@redis-cli XLEN notification:jobs 2>/dev/null || echo "0"

svc-notify-test:
	@redis-cli XADD notification:jobs '*' \
		chat_type intraday \
		message "<b>🧪 Test Notification</b>\n\nMakefile test — redis://localhost:6379" \
		parse_mode HTML \
		message_type test \
		symbol TEST \
		timestamp "$$(date -u +%Y-%m-%dT%H:%M:%S)" 2>/dev/null || \
	redis-cli XADD notification:jobs '*' \
		chat_type intraday \
		message "<b>Test Notification</b>" \
		parse_mode HTML \
		message_type test
	@echo "Test notification sent to Redis stream."
	@echo "Check with: make svc-notification-check"

svc-dead-letter:
	@redis-cli XREAD COUNT 10 STREAMS notification:dead 0 2>/dev/null || echo "No dead letters"
