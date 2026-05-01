SHELL := /bin/bash
PYTHON := .venv/bin/python
PIP    := .venv/bin/pip

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
	@echo "    run-prod      Intraday monitor — PRODUCTION=1"
	@echo "    run-dev       Intraday monitor — PRODUCTION=0 (safe)"
	@echo "    run-premarket Global cues + pre-open reports"
	@echo "    run-postmarket Post-market analysis pipeline"
	@echo "    deploy        Deploy to EC2 via SSH"
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
	@echo "    logs          Tail logs/monitor.log (last 50 lines)"
	@echo "    logs-follow   Follow logs/monitor.log live"
	@echo "    clean         Remove __pycache__, .pyc, pytest cache"
	@echo "    clean-all     clean + remove .venv"
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
	PRODUCTION=0 PYTHONPATH=$(CURDIR) $(PYTHON) intraday/intraday_monitor.py

.PHONY: run-premarket
run-premarket:
	PYTHONPATH=$(CURDIR) $(PYTHON) -c "\
from premarket.premarket_report import run_global_cues_report, run_preopen_report; \
run_global_cues_report(); \
run_preopen_report()"

.PHONY: run-postmarket
run-postmarket:
	PYTHONPATH=$(CURDIR) $(PYTHON) -c "\
from post_market_analysis.runner import run_and_summarize; \
run_and_summarize()"

.PHONY: deploy
deploy:
	PYTHONPATH=$(CURDIR) $(PYTHON) scripts/deploy.py

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
.PHONY: logs
logs:
	@tail -50 logs/monitor.log 2>/dev/null || echo "No logs/monitor.log found."

.PHONY: logs-follow
logs-follow:
	@tail -f logs/monitor.log 2>/dev/null || echo "No logs/monitor.log found."

.PHONY: clean
clean:
	find . -type d -name __pycache__ -not -path "./.venv/*" | xargs rm -rf
	find . -type f -name "*.pyc"     -not -path "./.venv/*" | xargs rm -f
	rm -rf .pytest_cache .coverage htmlcov

.PHONY: clean-all
clean-all: clean
	rm -rf .venv
	@echo ".venv removed. Run 'make venv && make install' to rebuild."
