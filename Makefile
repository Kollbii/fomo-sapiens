.PHONY: help setup run docker-up docker-down test coverage lint typecheck security-guard js-check agents-sync agents-check check

VENV_DIR ?= .venv
ifeq ($(OS),Windows_NT)
export PATH := $(abspath $(VENV_DIR)/Scripts);$(PATH)
else
export PATH := $(abspath $(VENV_DIR)/bin):$(PATH)
endif
UV ?= uv
UV_RUN ?= $(UV) run --no-sync

help:
	@$(UV_RUN) python -c "print('IBKR Options Analyzer - Commands:\n  make setup          Create .venv and install dependencies via uv\n  make run            Run FastAPI server on http://127.0.0.1:8000\n  make docker-up      Build and start Docker container\n  make docker-down    Stop Docker container\n  make test           Run pytest suite\n  make coverage       Run pytest with branch coverage report\n  make check          Run lint + typecheck + security-guard + js-check + agents-check + test\n  make agents-sync    Regenerate .claude/ and .gemini/ agents from .agent/')"

setup:
	$(UV_RUN) python -c "import pathlib, shutil; pathlib.Path('.env').exists() or shutil.copy('.env.example', '.env')"
	$(UV) venv $(VENV_DIR) --allow-existing
	$(UV) pip install -e ".[dev]"

run:
	$(UV_RUN) uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

lint:
	$(UV_RUN) ruff check src tests scripts
	$(UV_RUN) ruff format --check src tests scripts

typecheck:
	$(UV_RUN) mypy src tests scripts

security-guard:
	$(UV_RUN) python scripts/security_guard.py

js-check:
	$(UV_RUN) python scripts/js_check.py

agents-sync:
	$(UV_RUN) python scripts/sync_agents.py

agents-check:
	$(UV_RUN) python scripts/sync_agents.py --check

test:
	$(UV_RUN) pytest -q --tb=short

coverage:
	$(UV_RUN) pytest -q --tb=short --cov --cov-report=term

check: lint typecheck security-guard js-check agents-check test
	@$(UV_RUN) python -c "print('All quality and security gates passed!')"