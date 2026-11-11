.PHONY: help setup run docker-up docker-down test lint typecheck security-guard check

VENV_DIR ?= .venv
ifeq ($(OS),Windows_NT)
export PATH := $(abspath $(VENV_DIR)/Scripts);$(PATH)
else
export PATH := $(abspath $(VENV_DIR)/bin):$(PATH)
endif
UV ?= uv
UV_RUN ?= $(UV) run --no-sync

help:
	@$(UV_RUN) python -c "print('IBKR Options Analyzer - Commands:\n  make setup          Create .venv and install dependencies via uv\n  make run            Run FastAPI server on http://127.0.0.1:8000\n  make docker-up      Build and start Docker container\n  make docker-down    Stop Docker container\n  make test           Run pytest suite\n  make check          Run lint + typecheck + security-guard + test')"

setup:
	$(UV_RUN) python -c "import pathlib, shutil; [pathlib.Path(p).mkdir(parents=True, exist_ok=True) for p in ('data/snapshots', 'data/latest', 'data/metadata', 'data/logs')]; pathlib.Path('.env').exists() or shutil.copy('.env.example', '.env')"
	$(UV) venv $(VENV_DIR) --allow-existing
	$(UV) pip install -e ".[dev]"

run:
	$(UV_RUN) python -c "import pathlib; [pathlib.Path(p).mkdir(parents=True, exist_ok=True) for p in ('data/snapshots', 'data/latest', 'data/metadata', 'data/logs')]"
	$(UV_RUN) uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload

docker-up:
	$(UV_RUN) python -c "import pathlib; [pathlib.Path(p).mkdir(parents=True, exist_ok=True) for p in ('data/snapshots', 'data/latest', 'data/metadata', 'data/logs')]"
	docker compose up --build -d

docker-down:
	docker compose down

lint:
	$(UV_RUN) ruff check src tests
	$(UV_RUN) ruff format --check src tests

typecheck:
	$(UV_RUN) mypy src tests

security-guard:
	$(UV_RUN) python -c "import pathlib, re, sys; pat = re.compile(r'\.(placeOrder|cancelOrder|reqGlobalCancel|exerciseOptions)\('); hits = [f'{p}:{i}:{line.strip()}' for p in pathlib.Path('src').rglob('*.py') if p.name != 'security.py' for i, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1) if pat.search(line)]; sys.exit('\n'.join(['SECURITY VIOLATION:'] + hits)) if hits else print('Read-Only Security Guard: PASS')"

test:
	$(UV_RUN) pytest -q --tb=short

check: lint typecheck security-guard test
	@$(UV_RUN) python -c "print('All quality and security gates passed!')"