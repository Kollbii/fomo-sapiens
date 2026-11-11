# AGENTS.md — Master Operational Manual for AI Coding Agents

## 1. Project Identity & Core Mandate
- **System Name:** IBKR Options Analyzer & Dashboard
- **Architecture:** Local, strictly **READ-ONLY** quantitative options analytics platform connecting to **IBKR Gateway** (default port `4001`), persisting immutable local CSV snapshots, and serving a dual-theme (**NYT Editorial + High-Density Financial Terminal**) web UI via FastAPI.
- **Primary Market Data Mode:** **IBKR Type 3 (15-Minute Delayed, current trading day)** is the default operational mode (`MARKET_DATA_TYPE=3`), with automatic fallback to **Type 4 (Delayed-Frozen)** outside market hours, and a pre-scaffolded toggle for **Type 1 (Live OPRA)** when subscription permissions are added later.
- **Default Watchlist:** `NVDA`, `AVGO`, `CRDO`, `PLTR`, plus a strictly validated dynamic US Stock (`STK`) ticker input box.

---

## 2. Locked Technology Stack
Coding agents MUST use the following stack and MUST NOT introduce alternative frameworks without explicit user approval:
- **Runtime:** Python `3.11+` managed via `uv` (with standard `venv` + `pip` fallback in `Makefile`).
- **Broker Integration:** `ib-async` (connecting to `127.0.0.1:4001` natively or `host.docker.internal:4001` inside Docker).
- **Data Processing & Schema Validation:** `polars` (for all dataframe analytics and CSV I/O) and `pydantic>=2.0` + `pydantic-settings` (for boundary validation and configuration). **Do NOT use `pandas` in core analytics or storage pipelines.**
- **Quantitative Fallback Math:** `numpy` and `scipy.stats` / `scipy.optimize` (for vectorized Black-Scholes-Merton implied volatility and Greeks calculation when Type 3/4 delayed feeds omit `modelGreeks`, using default `risk_free_rate: 0.0425`).
- **Backend & Web Server:** `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`.
- **Frontend UI:** Semantic HTML5, CSS custom properties (`tokens.css`), Tailwind CSS utilities, lightweight interactivity (`HTMX` / vanilla ES6 modules), and `Plotly.js` for multi-DTE interactive charts.
- **Quality & Testing:** `pytest`, `pytest-asyncio`, `mypy` (strict mode), `ruff` (linting and formatting).

---

## 3. MANDATORY DOCUMENTATION-SYNC POLICY (HARD GATE)
**Every coding agent task is incomplete until documentation is synchronized.**
Before concluding any task, PR, or implementation phase, the agent MUST perform a **Documentation Audit**:
1. Did you add, rename, or modify a module, CLI command, environment variable, or API route? -> Update `AGENTS.md` and `docs/ARCHITECTURE.md`.
2. Did you alter CSV columns, data types, or file naming logic? -> Update `docs/DATA_SCHEMA.md`.
3. Did you modify ticker validation, socket connection parameters, or read-only guards? -> Update `docs/SECURITY_AND_SAFETY.md`.
4. Did you change CSS tokens, font pairings, layout grids, or Plotly templates? -> Update `docs/UI_UX_GUIDELINES.md`.
5. Did you change coding conventions, typing rules, or test commands? -> Update `docs/CODING_STANDARDS.md`.
6. Did you implement a feature listed in `docs/FUTURE_IMPROVEMENTS.md`? -> Move its specification from `docs/FUTURE_IMPROVEMENTS.md` into `docs/ARCHITECTURE.md` and mark its status as `[IMPLEMENTED]`.

*Never leave documentation out of sync with code.*

---

## 4. Phased Implementation Protocol
Agents MUST build the system incrementally following the 5 phases below. **Do NOT skip ahead or write monolithic untested code.** Each phase must pass `make check` (`ruff`, `mypy`, `pytest`) and a documentation sync check before moving to the next phase.

### Phase 1: Scaffolding, Configuration & Security Guardrails
- **Scope:** Verify project tree, `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `Makefile`, `config/settings.yaml`, `.env.example`, and implement `src/core/config.py`, `src/core/exceptions.py`, and `src/core/security.py`.
- **Key Deliverables:**
  - `TickerValidator`: Enforces hardcoded watchlist (`NVDA`, `AVGO`, `CRDO`, `PLTR`) + strict regex validation (`^[A-Z]{1,5}$`) for custom stock symbols.
  - `ReadOnlyIBGuard`: Wraps `ib_async.IB` and hard-blocks all order/mutation methods (`placeOrder`, `cancelOrder`, `reqGlobalCancel`, `exerciseOptions`, `whatIfOrder`) by raising `SecurityViolationError`.
- **Exit Gate:** 100% passing unit tests in `tests/unit/test_security.py` and `tests/unit/test_config.py`.

### Phase 2: CSV Storage Engine & Offline Cache Layer
- **Scope:** Implement `src/storage/schema.py` and `src/storage/csv_repository.py`.
- **Key Deliverables:**
  - Pydantic contract row validator (`OptionContractRow`) and Polars strict schema (`OPTION_CHAIN_POLARS_SCHEMA`).
  - **Mode 1 Archival Writer:** Saves immutable timestamped snapshots (`data/snapshots/{TICKER}/{TICKER}_{YYYYMMDD_HHMMSS}.csv`), updates `data/latest/{TICKER}_latest.csv` atomically, and updates `data/metadata/snapshot_index.json`.
  - Offline reader capable of serving historical and latest snapshots when IBKR Gateway is closed.
- **Exit Gate:** Unit and integration tests verifying atomic writes, schema enforcement, and corrupt-file rejection using synthetic fixture data.

### Phase 3: IBKR Broker Client, Type 3/4 Handler & Paged Fetcher
- **Scope:** Implement `src/broker/client.py`, `src/broker/contracts.py`, `src/broker/fetcher.py`, and `src/broker/parser.py`.
- **Key Deliverables:**
  - Connection manager targeting port `4001` with `ReadOnlyIBGuard` enforced.
  - Market data mode selector defaulting to **Type 3 (15-min Delayed from current date)** during trading hours with automatic cascade to **Type 4 (Delayed-Frozen)** when delayed bid-ask ticks are unpopulated after market close, plus a scaffolded placeholder for **Type 1 (Live OPRA)**.
  - DTE filter selecting closest expirations in the `[30, 60)` calendar-day window (matching targets `30, 35, 40, 45, 60` DTE).
  - Moneyness filter (`+-20%` default around underlying spot, adjustable in UI) and strike step striding (`$5.00` default, adjustable in UI).
  - Paged micro-batch downloader (`MAX_CONTRACTS_PER_PAGE = 80`) respecting IBKR's 100-line ceiling and pacing limits, plus a modular collector/placeholder for generic tick `101` (Open Interest).
- **Exit Gate:** Mocked `ib-async` integration tests proving pagination math, pacing compliance, and graceful disconnect recovery.

### Phase 4: Quantitative Analytics & BSM Fallback Engine
- **Scope:** Implement `src/analytics/bsm_fallback.py`, `src/analytics/walls.py`, `src/analytics/anomalies.py`, and `src/analytics/term_greeks.py`.
- **Key Deliverables:**
  - Vectorized Black-Scholes-Merton (BSM) solver that populates Implied Volatility, Delta, Gamma, Theta, and Vega whenever IBKR Type 3/4 delayed ticks omit `modelGreeks` (using configurable `risk_free_rate: 0.0425`).
  - Call Wall and Put Wall identification (by Open Interest and by Volume, with automatic Volume Wall fallback when Type 3 delayed data omits OI).
  - Unusual activity detector (`Volume > Open Interest` and cross-strike Volume Z-score outliers across strikes and DTEs).
  - Multi-DTE aggregation profiles overlaying `[30, 35, 40, 45, 60]` DTE structures.
- **Exit Gate:** Deterministic mathematical unit tests verifying BSM Greeks accuracy and wall/anomaly rankings.

### Phase 5: FastAPI Backend & NYT/Terminal Dual-Theme UI
- **Scope:** Implement `src/main.py`, `src/web/api.py`, `src/web/templates/`, and `src/web/static/`.
- **Key Deliverables:**
  - FastAPI endpoints for on-demand refresh (`POST /api/refresh`), cached snapshot loading (`GET /api/chain/{ticker}`), and historical archive switching (`GET /api/snapshots/{ticker}`).
  - Reserved top-level **Editorial Banner Container** (structural slot kept ready for future content).
  - Dual-theme switcher (**Variant 1: NYT Print Edition** vs. **Variant 2: Obsidian Terminal**).
  - Interactive Plotly multi-DTE charts and high-density monospaced tabular option chains.
- **Exit Gate:** End-to-end smoke test rendering UI from offline CSV fixtures and live/mocked refresh triggers.

---

## 5. Standard Commands Reference
All workflows are standardized through `Makefile` and `docker compose`:

    # Bootstrap local Python environment (uv or venv)
    make setup

    # Run the application locally on host (http://127.0.0.1:8000 -> IBKR 127.0.0.1:4001)
    make run

    # Build and run inside Docker container (Windows / macOS cross-platform)
    make docker-up

    # Stop Docker container
    make docker-down

    # Run full verification suite (Ruff lint + format check + Mypy strict + Pytest + Security guard)
    make check

    # Run unit & integration tests only
    make test

---

## 6. Hard Architectural Anti-Patterns (NEVER DO THIS)
1. **NEVER** invoke or expose `ib.placeOrder()`, `ib.cancelOrder()`, `ib.reqGlobalCancel()`, `ib.exerciseOptions()`, or `ib.whatIfOrder()`.
2. **NEVER** request an unfiltered option chain (`reqTickers` or `reqMktData` on all strikes returned by `reqSecDefOptParams`). Always filter by DTE `[30, 60)`, moneyness percentage (`range_pct`), and strike stride (`strike_step`) before batching into pages of `<= 80` contracts.
3. **NEVER** pass raw user strings into file paths or IBKR contract constructors without passing through `TickerValidator.validate(symbol)`.
4. **NEVER** block the FastAPI `asyncio` event loop with synchronous `time.sleep()` or synchronous socket calls; always use `asyncio.sleep()` and async `ib-async` coroutines (`qualifyContractsAsync`, `reqSecDefOptParamsAsync`).
5. **NEVER** overwrite historical CSV snapshots in `data/snapshots/`. Every manual refresh must create a new timestamped CSV (Mode 1 Archive) and atomically update `data/latest/{TICKER}_latest.csv`.
6. **NEVER** finish a coding task without updating `AGENTS.md` and the affected files in `docs/`.

---

## 7. Specialized Sub-Agents & Token Optimization (`.agent/agents/`)
To minimize token usage and keep context windows small, delegate tasks to the specialized profiles in `.agent/agents/` (see `.agent/agents/README.md` for full routing):
- **`.agent/agents/broker-storage-eng.md`**: Use for Phases 1–3 (`src/core/`, `src/storage/`, `src/broker/`). Reads only architecture, security, and schema docs.
- **`.agent/agents/quant-analytics-eng.md`**: Use for Phase 4 (`src/analytics/`). Pure Polars/NumPy/SciPy math; reads only schema and analytics specs.
- **`.agent/agents/ui-frontend-eng.md`**: Use for Phase 5 (`src/main.py`, `src/web/`). Reads only `docs/UI_UX_GUIDELINES.md`.
- **`.agent/agents/doc-sync-auditor.md`**: Lightweight agent that inspects `git diff` and updates `AGENTS.md` and `docs/*.md` without reading full source trees.
- **`.agent/agents/qa-git-runner.md`**: Ultra-low-token runner that executes `make check` (`ruff`, `mypy`, security grep, `pytest`), reports compact pass/fail status, creates conventional commits, and pushes.