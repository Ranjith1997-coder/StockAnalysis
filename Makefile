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
	@echo "    run-dev            Dev intraday — PRODUCTION=0 DEV_INTRADAY=1"
	@echo "    run-dev-positional Dev positional (EOD) — PRODUCTION=0 DEV_POSITIONAL=1"
	@echo "                       Append DEV_NOTIFY=1 to either run-dev target to send Telegram alerts in dev mode"
	@echo "    run-dev-stock-intraday   Dev single stock intraday:   make run-dev-stock-intraday STOCK=RELIANCE"
	@echo "    run-dev-stock-positional Dev single stock positional: make run-dev-stock-positional STOCK=RELIANCE"
	@echo "    run-dev-index-intraday   Dev single index intraday:   make run-dev-index-intraday INDEX=NIFTY"
	@echo "    run-dev-index-positional Dev single index positional: make run-dev-index-positional INDEX=NIFTY"
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
	@echo "    logs          Tail stock_monitor.log (last 50 lines)"
	@echo "    logs-500      Tail stock_monitor.log (last 500 lines)"
	@echo "    logs-follow   Follow stock_monitor.log live"
	@echo "    logs-grep     Grep stock_monitor.log: make logs-grep Q=RELIANCE"
	@echo "    clean         Remove __pycache__, .pyc, pytest cache"
	@echo "    clean-all     clean + remove .venv"
	@echo ""
	@echo "  Server (hacker@100.92.21.31)"
	@echo "    server-ssh          Open interactive SSH session"
	@echo "    server-logs         Tail last 50 lines of stock_monitor.log on server"
	@echo "    server-logs-500     Tail last 500 lines of stock_monitor.log on server"
	@echo "    server-logs-follow  Live-follow stock_monitor.log on server"
	@echo "    server-status       Show stock_analysis.service status"
	@echo "    server-restart      Restart stock_analysis.service"
	@echo "    server-stop         Stop stock_analysis.service"
	@echo "    server-pull         git pull on server repo"
	@echo "    server-df           Disk usage on server"
	@echo "    update-enctoken     Update ZERODHA_ENC_TOKEN on server .env:"
	@echo "                          make update-enctoken TOKEN=<your_enc_token>"
	@echo "    auth-run            Run auth_login.py locally (skips if already ran today)"
	@echo "    auth-force          Force-refresh enctoken locally (bypasses once-per-day guard)"
	@echo "    server-auth-force   Force-refresh enctoken on server"
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
	@echo "env-check complete."

# ──────────────────────────────────────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────────────────────────────────────
.PHONY: run-prod
run-prod:
	PRODUCTION=1 PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py

.PHONY: run-dev
run-dev:
	PRODUCTION=0 DEV_INTRADAY=1 PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py

.PHONY: run-dev-positional
run-dev-positional:
	PRODUCTION=0 DEV_POSITIONAL=1 PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py

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
run-premarket:
	PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py --premarket

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

.PHONY: logs-follow
logs-follow:
	@tail -f $(LOG_FILE) 2>/dev/null || echo "No $(LOG_FILE) found."

.PHONY: logs-grep
logs-grep:
	@test -n "$(Q)" || { echo "ERROR: Q is required. Usage: make logs-grep Q=RELIANCE"; exit 1; }
	@grep -i "$(Q)" $(LOG_FILE) 2>/dev/null | tail -100 || echo "No matches for '$(Q)' in $(LOG_FILE)."

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

.PHONY: server-logs-follow
server-logs-follow:
	ssh $(SERVER) "tail -f $(SERVER_LOG)"

.PHONY: server-status
server-status:
	ssh $(SERVER) "systemctl status stockanalysis.service"

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
