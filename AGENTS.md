# AGENTS.md — Master Operational Manual for AI Coding Agents

## 1. Project Identity & Core Mandate
- **System Name:** IBKR Options Analyzer & Dashboard
- **Architecture:** Local, strictly **READ-ONLY** quantitative options analytics platform connecting to **IBKR Gateway / TWS** (`IBKR_HOST:IBKR_PORT`, default `127.0.0.1:4001`), persisting immutable local CSV snapshots, and serving a dual-theme (**NYT Editorial + High-Density Financial Terminal**) web UI via FastAPI.
- **Primary Market Data Mode:** Configured dynamically via `MARKET_DATA_MODE`, `DEFAULT_MARKET_DATA_TYPE`, and `FALLBACK_MARKET_DATA_TYPE` in `.env` / `config/settings.yaml` — defaulting to `DELAYED` (**Type 3** 15-min Delayed with automatic fallback to **Type 4** Delayed-Frozen outside market hours) and supporting `LIVE` (**Type 1** Live OPRA with fallback to **Type 2** Frozen, plus automatic `10089`/`354` downgrade to Delayed when OPRA permissions are missing).
- **Default Watchlist:** `NVDA`, `AVGO`, `CRDO`, `PLTR`, plus a strictly validated dynamic US Stock (`STK`) ticker input box.

---

## 2. Locked Technology Stack
Coding agents MUST use the following stack and MUST NOT introduce alternative frameworks without explicit user approval:
- **Runtime:** Python `3.11+` managed via `uv`; every environment (`make setup`, Docker image) is installed strictly from `uv.lock` (`uv sync --locked`).
- **Broker Integration:** `ib-async` (connecting to `127.0.0.1:4001` natively or `host.docker.internal:4001` inside Docker).
- **Data Processing & Schema Validation:** `polars` (for all dataframe analytics and CSV I/O) and `pydantic>=2.0` + `pydantic-settings` (for boundary validation and configuration). **Do NOT use `pandas` in core analytics or storage pipelines.**
- **Quantitative Fallback Math:** `numpy` vectorization plus `scipy.special.erf` for the normal CDF (hand-rolled vectorized Newton-Raphson + bisection IV solver; no `scipy.optimize`) for Black-Scholes-Merton implied volatility and Greeks calculation when Type 3/4 delayed feeds omit `modelGreeks`, using default `risk_free_rate: 0.0425`.
- **Backend & Web Server:** `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`.
- **Frontend UI:** Semantic server-rendered HTML5 (Jinja2), hand-written CSS driven by custom properties (`tokens.css`; no CSS framework), vanilla JavaScript with `fetch` / `EventSource` (no framework, no build step), and `Plotly.js` for multi-DTE interactive charts.
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
7. Did you add or change classes, helpers, element inventories, or cache-buster versions? -> Update the matching layer section of `docs/reference/IMPLEMENTATION_NOTES.md` (contracts stay in the files above).
8. Is the change user-visible (feature, fix, command, config key)? -> Add a line under `## [Unreleased]` in `CHANGELOG.md`.

*Never leave documentation out of sync with code.*

---

## 4. Module Map, Status & Change Protocol
All five core phases are `[IMPLEMENTED]`; their delivery history lives in `CHANGELOG.md` and per-module details in `docs/reference/IMPLEMENTATION_NOTES.md`.

| Phase | Layer | Modules | Tests | Contract docs |
| :--- | :--- | :--- | :--- | :--- |
| 1 | Core: config, exceptions, read-only guard, ticker validation | `src/core/{config,exceptions,security}.py` | `test_config.py`, `test_security.py`, `test_security_guard.py` | `docs/SECURITY_AND_SAFETY.md` |
| 2 | Storage: 27-column schema, Mode 1 atomic CSV archive, snapshot index | `src/storage/{schema,csv_repository}.py` | `test_csv_pipeline.py`, `test_offline_fixture_smoke.py` | `docs/DATA_SCHEMA.md` |
| 3 | Broker: ib-async client, `DELAYED 3->4` / `LIVE 1->2` cascade, weekly `[30, 120]` DTE discovery, `<= 80`/page fetcher, `<= 60` Wall Corridor | `src/broker/{client,contracts,parser,fetcher,corridor}.py` | `test_contracts_and_pagination.py`, `test_corridor.py` | `docs/ARCHITECTURE.md` §3, §5 |
| 4 | Analytics: BSM IV/Greeks fallback, walls, anomalies, multi-DTE profiles, Strategy Lab | `src/analytics/{bsm_fallback,walls,anomalies,term_greeks,strategies}.py` | `test_bsm_and_analytics.py`, `test_strategies.py` | `docs/ARCHITECTURE.md` §4 |
| 5 | Web: FastAPI routes + SSE, NYT Print / Obsidian Terminal UI, Plotly panels, Corridor & Strategy Lab cards, chain table | `src/main.py`, `src/web/` | `test_web_api.py`, `test_corridor_api.py` | `docs/UI_UX_GUIDELINES.md` |

**API surface:** `GET /api/status`, `POST /api/refresh`, `GET /api/chain/{ticker}`, `GET /api/snapshots/{ticker}`, `GET /api/csv/{ticker}`, `POST /api/strategy/suggest`, `POST /api/strategy/evaluate`, `POST /api/corridor/quotes`, `POST /api/corridor/stop`, `GET /api/corridor/stream/{ticker}`.

**Change protocol for new work** (roadmap items in `docs/FUTURE_IMPROVEMENTS.md` or fixes):
1. Work one layer at a time in dependency order (`core -> storage -> broker -> analytics -> web`); never write monolithic untested code.
2. Every change ships with tests, passes `make check`, and completes the §3 Documentation Audit before the next step.
3. Record user-visible changes under `## [Unreleased]` in `CHANGELOG.md`.

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

    # Run full verification suite (Ruff lint + format check + Mypy strict + Security guard + JS syntax check + Agent-spec sync + Pytest)
    make check

    # Regenerate Claude Code / Gemini CLI sub-agents from .agent/ (run after editing .agent/)
    make agents-sync

    # Run unit & integration tests only
    make test

    # Pytest with branch coverage report over src/
    make coverage

---

## 6. Hard Architectural Anti-Patterns (NEVER DO THIS)
1. **NEVER** invoke or expose `ib.placeOrder()`, `ib.cancelOrder()`, `ib.reqGlobalCancel()`, `ib.exerciseOptions()`, or `ib.whatIfOrder()`.
2. **NEVER** request an unfiltered option chain (`reqTickers` or `reqMktData` on all strikes returned by `reqSecDefOptParams`). Always filter to weekly expirations inside `[30, 120]` DTE, moneyness percentage (`range_pct`), and strike stride (`strike_step`) before batching into pages of `<= 80` contracts.
3. **NEVER** pass raw user strings into file paths or IBKR contract constructors without passing through `TickerValidator.validate(symbol)`.
4. **NEVER** block the FastAPI `asyncio` event loop with synchronous `time.sleep()` or synchronous socket calls; always use `asyncio.sleep()` and async `ib-async` coroutines (`qualifyContractsAsync`, `reqSecDefOptParamsAsync`).
5. **NEVER** overwrite historical CSV snapshots in `data/snapshots/`. Every manual refresh must create a new timestamped CSV (Mode 1 Archive) and atomically update `data/latest/{TICKER}_latest.csv`.
6. **NEVER** finish a coding task without updating `AGENTS.md` and the affected files in `docs/`.

---

## 7. Specialized Sub-Agents, Model Routing & Multi-Platform Support (`.agent/`)
Delegate work to the specialized profiles to keep context windows small (full routing, model rationale, and workflow in `.agent/agents/README.md`):
- **Single source of truth:** canonical specs in `.agent/agents/*.md`, model/reasoning profiles and tool mapping in `.agent/models.yaml`, shared invariants + `[HANDOFF]` format in `.agent/agents/_shared/protocol.md`.
- **Generated adapters (never hand-edit):** `.claude/agents/*.md` (Claude Code) and `.gemini/agents/*.md` (Gemini CLI), rendered by `scripts/sync_agents.py` via `make agents-sync`; `make agents-check` (part of `make check`) fails when they drift. Entry files `CLAUDE.md` and `GEMINI.md` import this `AGENTS.md`; `.gemini/settings.json` carries generated per-agent Gemini `thinkingConfig` overrides.
- **Agents:**
  - **`broker-storage-eng`** (`deep-reasoning`: Claude `opus` / Gemini `gemini-4-argon` `thinkingLevel: HIGH`): Phases 1–3 (`src/core/` except `security.py`, `src/storage/`, `src/broker/`).
  - **`quant-analytics-eng`** (`deep-reasoning`): Phase 4 (`src/analytics/`), pure Polars/NumPy/SciPy math and Strategy Lab.
  - **`ui-frontend-eng`** (`balanced-coding`: `sonnet` / `gemini-4-argon` `thinkingLevel: LOW`): Phase 5 (`src/main.py`, `src/web/`).
  - **`doc-sync-auditor`** (`precise-writer`: `sonnet` / `gemini-3.8-flash` `thinkingLevel: HIGH`): diff-driven sync of `AGENTS.md`, `docs/*.md`, `.agent/agents/*.md`.
  - **`safety-reviewer`** (`deep-reasoning`, read-only tools): APPROVE / REQUEST_CHANGES review gate for diffs touching `src/core/`, `src/broker/`, `src/storage/`, `src/web/api.py`.
  - **`qa-git-runner`** (`fast-executor`: `haiku` / `gemini-3.8-flash` `thinkingLevel: HIGH`): `make check`, compact report, conventional commits; tags/pushes only on explicit request.
- **Workflow:** domain agent → `doc-sync-auditor` → `safety-reviewer` (when required) → `qa-git-runner`, passing only the `[HANDOFF]` block between agents.
