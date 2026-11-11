.PHONY: help setup run docker-up docker-down test lint typecheck security-guard check

PYTHON ?= python3
VENV_DIR ?= .venv

help:
	@echo "IBKR Options Analyzer — Command Reference"
	@echo "  make setup          Create virtual environment and install dependencies"
	@echo "  make run            Run FastAPI dashboard locally on http://127.0.0.1:8000 (IBKR 127.0.0.1:4001)"
	@echo "  make docker-up      Build and start container via Docker Compose (Windows/macOS/Linux)"
	@echo "  make docker-down    Stop Docker container"
	@echo "  make test           Run pytest unit and integration suite"
	@echo "  make check          Run full verification gate (lint + types + security-guard + tests)"

setup:
	@mkdir -p data/snapshots data/latest data/metadata data/logs
	@if command -v uv >/dev/null 2>&1; then \
		uv venv $(VENV_DIR) && uv pip install --python $(VENV_DIR)/bin/python -e ".[dev]"; \
	else \
		$(PYTHON) -m venv $(VENV_DIR) && $(VENV_DIR)/bin/pip install --upgrade pip && $(VENV_DIR)/bin/pip install -e ".[dev]"; \
	fi
	@if [ ! -f .env ]; then cp .env.example .env; fi
	@echo "Setup complete. Activate with: source $(VENV_DIR)/bin/activate"

run:
	@mkdir -p data/snapshots data/latest data/metadata data/logs
	$(VENV_DIR)/bin/uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload

docker-up:
	@mkdir -p data/snapshots data/latest data/metadata data/logs
	docker compose up --build -d
	@echo "Dashboard running at http://127.0.0.1:8000"

docker-down:
	docker compose down

lint:
	$(VENV_DIR)/bin/ruff check src tests
	$(VENV_DIR)/bin/ruff format --check src tests

typecheck:
	$(VENV_DIR)/bin/mypy src tests

security-guard:
	@echo "Verifying zero order-placement calls outside src/core/security.py..."
	@! grep -rnE "\.(placeOrder|cancelOrder|reqGlobalCancel|exerciseOptions)\(" src/ --exclude="security.py"

test:
	$(VENV_DIR)/bin/pytest

check: lint typecheck security-guard test
	@echo "All quality and security gates passed. Remember to verify AGENTS.md and docs/ sync!"