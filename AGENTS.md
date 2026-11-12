# AGENTS.md — Master Operational Manual for AI Coding Agents

## 1. Project Identity & Core Mandate
- **System Name:** IBKR Options Analyzer & Dashboard
- **Architecture:** Local, strictly **READ-ONLY** quantitative options analytics platform connecting to **IBKR Gateway / TWS** (`IBKR_HOST:IBKR_PORT`, default `127.0.0.1:4001`), persisting immutable local CSV snapshots, and serving a dual-theme (**NYT Editorial + High-Density Financial Terminal**) web UI via FastAPI.
- **Primary Market Data Mode:** Configured dynamically via `MARKET_DATA_MODE`, `DEFAULT_MARKET_DATA_TYPE`, and `FALLBACK_MARKET_DATA_TYPE` in `.env` / `config/settings.yaml` — defaulting to `DELAYED` (**Type 3** 15-min Delayed with automatic fallback to **Type 4** Delayed-Frozen outside market hours) and supporting `LIVE` (**Type 1** Live OPRA with fallback to **Type 2** Frozen, plus automatic `10089`/`354` downgrade to Delayed when OPRA permissions are missing).
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

- **Status: `[IMPLEMENTED]`** — `src/core/{config,exceptions,security}.py` with `tests/unit/test_config.py` and `tests/unit/test_security.py`. Config is `config/settings.yaml` (new key `storage.logs_dir`) + env overrides `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, `MARKET_DATA_MODE`, `DEFAULT_MARKET_DATA_TYPE`, `FALLBACK_MARKET_DATA_TYPE`, `RISK_FREE_RATE`, `LOG_LEVEL`, `APP_HOST`, `APP_PORT` with automatic mode/type reconciliation across YAML and `.env`.

### Phase 2: CSV Storage Engine & Offline Cache Layer
- **Scope:** Implement `src/storage/schema.py` and `src/storage/csv_repository.py`.
- **Key Deliverables:**
  - Pydantic contract row validator (`OptionContractRow`) and Polars strict schema (`OPTION_CHAIN_POLARS_SCHEMA`).
  - **Mode 1 Archival Writer:** Saves immutable timestamped snapshots (`data/snapshots/{TICKER}/{TICKER}_{YYYYMMDD_HHMMSS}.csv`), updates `data/latest/{TICKER}_latest.csv` atomically, and updates `data/metadata/snapshot_index.json`.
  - Offline reader capable of serving historical and latest snapshots when IBKR Gateway is closed.
- **Exit Gate:** Unit and integration tests verifying atomic writes, schema enforcement, and corrupt-file rejection using synthetic fixture data.
- **Status: `[IMPLEMENTED]`** — `src/storage/{schema,csv_repository}.py`, `tests/integration/test_csv_pipeline.py`; new exception `SnapshotNotFoundError` (see `docs/DATA_SCHEMA.md` §5).
- **Snapshot quality `[IMPLEMENTED]`:** `src/storage/quality.py` (`evaluate_snapshot`, `is_usable_snapshot`, `MIN_QUOTE_COVERAGE = 0.90`) with `tests/unit/test_snapshot_quality.py`. `SnapshotMeta`, `snapshot_index.json`, `GET /api/snapshots/{ticker}` and chain `meta` carry optional `spot_source`, `quoted_contract_count`, `quote_coverage`, `prior_close_only_count`, `collection_mode` (null for older entries; CSV schema unchanged; see `docs/DATA_SCHEMA.md` §4.1). `POST /api/refresh` stops running corridor streams first.

### Phase 3: IBKR Broker Client, Type 3/4 Handler & Paged Fetcher
- **Scope:** Implement `src/broker/client.py`, `src/broker/contracts.py`, `src/broker/fetcher.py`, and `src/broker/parser.py`.
- **Key Deliverables:**
  - Connection manager targeting configured `IBKR_HOST:IBKR_PORT` (default `127.0.0.1:4001`) with `ReadOnlyIBGuard` enforced.
  - Dynamic market data mode & primary/fallback selector derived from config (`DELAYED`: Type 3 -> 4; `LIVE`: Type 1 -> 2, with `Error 10089`/`354` live-subscription downgrade and ticker-level `marketDataType` observation).
  - DTE filter selecting every weekly expiration inside `[30, 120]` DTE (`dte_min <= dte <= dte_max`, one per ISO week preferring Friday).
  - Moneyness filter (`+-20%` default around underlying spot, adjustable in UI) and grid-aligned canonical strike step striding (`Auto` default, or `$1` / `$2.5` / `$5` / `$10` / `$25` adjustable in UI), backed by bounded (`asyncio.Semaphore(3)` in active event loops) TTL-cached per-expiration `reqContractDetailsAsync` exact-strike pre-fetching (with pooled `reqSecDefOptParams` fallback) so monthly/weekly differences avoid `Error 200` phantom strikes and redundant qualification.
  - Paged micro-batch downloader (`MAX_CONTRACTS_PER_PAGE = 80`) respecting IBKR's 100-line ceiling and pacing limits (subscribe/cancel in bursts of 20 with a `0.55s` pause, `<= 40` msg/s under the `ib_async` 45/s throttle, page ready only when every contract has a `bid` or `ask`; spot source `LAST`/`MID`/`CLOSE` with a 2% last-vs-mid check; and mid-batch `GatewayConnectionError` transport recovery), plus a modular collector/placeholder for generic tick `101` (Open Interest).
  - Targeted **Wall Strike Corridor & Live Streaming (`src/broker/corridor.py`)**: selects `±k` strikes (`±5` default) around Primary Put & Call Walls plus active Strategy Lab legs and ATM strikes, strictly capped at `<= 60` contracts (default `56`), reusing CSV/cached `conId > 0` without re-qualification, with one-shot pull (`fetch_corridor_quotes`) and throttled continuous streaming (`stream_corridor_quotes`) enforcing `finally: cancelMktData` 100% teardown.
- **Exit Gate:** Mocked `ib-async` integration tests proving pagination math, pacing compliance, `<= 60` corridor subscription cap, 100% `cancelMktData` cleanup, and graceful disconnect recovery.
- **Status: `[IMPLEMENTED]`** — `src/broker/{client,contracts,parser,fetcher,corridor}.py` with `tests/unit/test_contracts_and_pagination.py` and `tests/unit/test_corridor.py` (see `docs/ARCHITECTURE.md` §7).

### Phase 4: Quantitative Analytics & BSM Fallback Engine
- **Scope:** Implement `src/analytics/bsm_fallback.py`, `src/analytics/walls.py`, `src/analytics/anomalies.py`, and `src/analytics/term_greeks.py`.
- **Key Deliverables:**
  - Vectorized Black-Scholes-Merton (BSM) solver that populates Implied Volatility, Delta, Gamma, Theta, and Vega whenever IBKR Type 3/4 delayed ticks omit `modelGreeks` (using configurable `risk_free_rate: 0.0425`).
  - Call Wall and Put Wall identification (by Open Interest and by Volume, with automatic Volume Wall fallback when Type 3 delayed data omits OI).
  - Unusual activity detector (`Volume > Open Interest` and cross-strike Volume Z-score outliers across strikes and DTEs).
  - Multi-DTE aggregation profiles overlaying weekly expirations inside `[30, 120]` DTE.
  - Multi-leg strategy evaluator & Wall-anchored auto-builder (`src/analytics/strategies.py`): supports `Bull Put Spread`, `Bull Call Spread`, `Diagonal Spread`, `Iron Condor`, and arbitrary `CUSTOM` legs with `Mid`/`Natural`/Slippage/Limit Ladder, Risk/RoR/Breakevens/POP%/`±1σ` Expected Move, net Greeks, and BSM `T+n` / `ΔIV` curves.
- **Exit Gate:** Deterministic mathematical unit tests verifying BSM Greeks accuracy, wall/anomaly rankings, and strategy pricing/payoffs.
- **Status: `[IMPLEMENTED]`** — `src/analytics/{bsm_fallback,walls,anomalies,term_greeks,strategies}.py` with `tests/unit/test_bsm_and_analytics.py` and `tests/unit/test_strategies.py` (see `docs/ARCHITECTURE.md` §8).
- **Bull put backtest engine `[IMPLEMENTED]`:** `src/analytics/backtest.py` (`load_backtest_steps`, `run_bull_put_backtest`, `backtest_bull_put_from_repo`) with `tests/unit/test_backtest.py`. Pure and read-only (no IBKR, no writes, index never rebuilt, no corridor overlay): replays a bull put spread over saved snapshots (mode `LEGS` or `AUTO_BUILD` via the Strategy Lab `BULL_PUT_SPREAD` preset), marks the same contracts at every later snapshot with the Strategy Lab price fallback chain, flags every step (`OK` / `UNRELIABLE` / `INCOMPLETE`, never skipped), settles at intrinsic value from a 16:15 ET+ expiry-day snapshot, and records constants + a `strategies.py` SHA-256. No API endpoint or UI yet (see `docs/ARCHITECTURE.md` §11).

### Phase 5: FastAPI Backend & NYT/Terminal Dual-Theme UI
- **Scope:** Implement `src/main.py`, `src/web/api.py`, `src/web/templates/`, and `src/web/static/`.
- **Key Deliverables:**
  - FastAPI endpoints for server status (`GET /api/status`), on-demand refresh (`POST /api/refresh`), cached snapshot loading (`GET /api/chain/{ticker}`), historical archive switching (`GET /api/snapshots/{ticker}`), CSV download (`GET /api/csv/{ticker}`), on-demand cached Strategy Lab auto-build/evaluation (`POST /api/strategy/suggest`, `POST /api/strategy/evaluate`), and targeted Wall Strike Corridor quotes/SSE live streaming (`POST /api/corridor/quotes`, `POST /api/corridor/stop`, `GET /api/corridor/stream/{ticker}`).
  - Reserved top-level **Editorial Banner Container** (`#editorial-banner-slot`, structural slot kept ready for future content).
  - Dual-theme switcher (**Variant 1: NYT Print Edition** vs. **Variant 2: Obsidian Terminal**).
  - Interactive snap-to-grid 12-column corner-resizable Plotly multi-DTE charts (with `#walls-copy-ai-btn` Polish-language AI prompt export for the active DTE scope and `Step $` / `Range %` telemetry sync), dynamic config/gateway-driven `#editorial-banner-copy` and `#app-bootstrap` metadata (`Live`/`Delayed` feed labels, dynamic `host:port`, `• Snap T<N>` chain badge), dismissible context-rich `#alert-banner` (`#N • HH:MM:SS`, repeat badge, `.is-fresh` pulse, `timeoutMs` + non-JSON `textSnippet` + isolated Stage 1 HTTP vs. Stage 2 `UI_RENDER` error reporting), per-ticker saved pull preferences, click-only default-enabled `#wall-corridor-card` (`data-panel-id="corridor"`, `±5` strikes around Walls, `[Refresh Corridor Quotes]`, `[ ] Enable Live Constant Streaming` switch, Bid/Ask ladder + Plotly chart syncing live quotes into Strategy Lab without mutating Mode 1 CSVs), full-width `#strategy-lab-card` (`data-panel-id="strategy"`, enabled by default in `PANELS:`, cache-only `[x] Auto-Build on Load` `#strategy-auto-populate` toggle, nearest-DTE expiry snap, `CUSTOM` leg preservation on same-ticker reload, Wall auto-build + pickable legs + BSM `T+n` payoff diagram), and high-density monospaced 18-column tabular option chains with sortable headers, sticky per-column filter controls, and `+Leg` row buttons (`#chain-table`).
- **Exit Gate:** End-to-end smoke test rendering UI from offline CSV fixtures and live/mocked refresh, corridor SSE, and disconnect cleanup triggers.
- **Status: `[IMPLEMENTED]`** — `src/main.py`, `src/web/api.py`, `src/web/templates/{base,index}.html`, `src/web/static/css/tokens.css`, `src/web/static/js/dashboard.js` with `tests/integration/test_web_api.py` and `tests/integration/test_corridor_api.py` (see `docs/ARCHITECTURE.md` §9).

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
2. **NEVER** request an unfiltered option chain (`reqTickers` or `reqMktData` on all strikes returned by `reqSecDefOptParams`). Always filter to weekly expirations inside `[30, 120]` DTE, moneyness percentage (`range_pct`), and strike stride (`strike_step`) before batching into pages of `<= 80` contracts.
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