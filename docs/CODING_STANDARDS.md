# Coding Standards, Typing & Testing Protocol (`CODING_STANDARDS.md`)

## 1. Modular Separation of Concerns
Coding agents must enforce strict boundaries between the 5 layers in `src/`:
1. **`src/core/`:** Pure configuration (`config.py`), domain exceptions (`exceptions.py`), and security guards (`security.py`). Zero imports from `broker`, `analytics`, `storage`, or `web`.
2. **`src/storage/`:** Schema definitions (`schema.py`) and CSV persistence (`csv_repository.py`). Must never import `ib_async` or `fastapi`.
3. **`src/broker/`:** `ib-async` socket management, contract qualification, paged fetching, and raw tick parsing. Returns validated `OptionContractRow` lists. Must never import `fastapi`.
4. **`src/analytics/`:** Pure, deterministic quantitative functions operating on `polars.DataFrame` inputs and returning typed Pydantic/dict payloads or `polars.DataFrame` outputs. Zero network or disk I/O inside `src/analytics/`.
5. **`src/web/`:** FastAPI route handlers, Jinja2 templates, and static assets. Orchestrates calls to `broker`, `storage`, and `analytics`.

---

## 2. Strict Typing & Validation Rules
- **Python 3.11+ Type Hints:** Every function, method, and coroutine across `src/` and `tests/` MUST have explicit parameter and return type annotations.
- **Mypy Strict Mode:** `mypy src tests --strict` must pass with 0 errors. Avoid `Any`; use `TypedDict`, `Protocol`, or `pydantic.BaseModel` for structured payloads.
- **Dataframes:** Use `polars.DataFrame` and `polars.LazyFrame` exclusively. Do not convert to `pandas`.

---

## 3. Logging & Observability Standards
- Use Python's structured `logging` module (configured in `src/core/config.py`) writing to both `stdout` and `data/logs/analyzer.log` (rotating file handler).
- Every manual refresh must log a structured summary line containing:
  - `ticker`, `market_data_type`, `spot_price`, `dtes_matched`, `range_pct`, `strike_step`, `total_contracts`, `pages_fetched`, `elapsed_ms`, `greeks_source_counts`, and `snapshot_id`.
- Never log stack traces raw to the browser UI; translate all exceptions in `src/web/api.py` into structured JSON/HTML alert banners using `src/core/exceptions.py`.

---

## 4. Testing Protocol (`tests/`)

### 4.0 Test Layout & Shared Helpers
- **`tests/support/`** — import-only helpers (never duplicate them in test files):
  - `aio.py`: `run_coro` (socket-free `send(None)` coroutine driver), `run_virtual_loop` / `VirtualEventLoop` (deterministic virtual-clock loop when code needs `asyncio.get_running_loop()`), `noop_sleep` (inject instead of `asyncio.sleep`).
  - `ib_fakes.py`: `FakeEvent` (`eventkit.Event` stand-in with `+=` / `emit`). Test-specific `IB` mocks stay in their test module.
- **`tests/conftest.py`** — lifecycle fixtures only: `fixtures_dir`, `fixture_repo` (tmp `CSVRepository` seeded with a *copy* of the golden snapshots and a rebuilt index).
- **`tests/fixtures/snapshots/{TICKER}/{SNAPSHOT_ID}.csv`** — committed golden Mode 1 archives. Never edit them to make a test pass: if a schema change can no longer read them, existing user archives break too (add a migration or a new fixture instead).
- Tests never open sockets or real sleeps; inject `noop_sleep`, fixed clocks, and fake `IB` factories.
- Commands: `make test` (quiet), `make coverage` (`pytest-cov`, branch coverage over `src/`, report only), `make js-check` (`node --check` on `src/web/static/js/**/*.js`; skipped when Node.js is absent).

### 4.1 Required Test Modules
Every feature phase requires automated tests in `pytest`:
1. **`tests/unit/test_security.py`:**
   - Tests `TickerValidator.validate()` against valid symbols (`NVDA`, `AVGO`, `CRDO`, `PLTR`, `SPY`) and malicious inputs (`../etc`, `NVDA;rm`, `BRK.A`, `12345`, `""`, `"ABCDEFG"`).
   - Tests `ReadOnlyIBGuard.lock()` ensuring `placeOrder`, `cancelOrder`, `reqGlobalCancel`, `exerciseOptions`, and `whatIfOrder` raise `SecurityViolationError`.
2. **`tests/unit/test_contracts_and_pagination.py`:**
   - Verifies strict `[30, 120]` weekly DTE matching, `+-X%` moneyness filtering, grid-aligned `$ strike_step` striding (and Auto step selection), and `<= 80` contracts/page splitting.
3. **`tests/unit/test_bsm_and_analytics.py`:**
   - Verifies vectorized BSM implied volatility and Greeks against known analytical option values.
   - Verifies Put/Call Wall detection (both OI mode and Volume fallback mode) and `Volume > OI` anomaly detection.
4. **`tests/integration/test_csv_pipeline.py`:**
   - Verifies Mode 1 archival writes, `latest` updates, `snapshot_index.json` rebuilds, and full offline pipeline execution from CSV -> Analytics -> FastAPI response.
5. **`tests/unit/test_strategies.py`:**
   - Verifies Wall-anchored auto-construction (`Bull Put Spread`, `Bull Call Spread`, `Diagonal Spread`, `Iron Condor`), user-picked `CUSTOM` legs, `Mid`/`Natural`/Slippage/Limit Ladder fills, `POP %` / `±1σ` expected move, unbounded risk guards, and `T+n` / `ΔIV` BSM curves.
6. **`tests/unit/test_corridor.py` & `tests/integration/test_corridor_api.py`:**
   - Verifies `select_wall_corridor` Tier `0..3` prioritization and hard `<= 60` subscription cap (`PacingViolationError` above 60), pre-qualified `conId` reuse (`qualifyContractsAsync` skipped), Type `3 -> 4` and `LIVE` `1 -> 2` fallback + BSM Greeks enrichment, and 100% `cancelMktData` cleanup across one-shot pulls, normal stream termination, `stop_all_streams()`, `gen.aclose()`, and mid-stream Gateway disconnects.
   - Verifies REST/SSE Corridor endpoints (`/api/corridor/quotes`, `/api/corridor/stop`, `/api/corridor/stream/{ticker}`), `503 GATEWAY_OFFLINE` handling, Strategy Lab live quote overlay without mutating Mode 1 CSVs, and pure `_run()` coroutine stepping without Windows loopback sockets.
   - Keep JavaScript templates inside `src/web/static/js/dashboard.js` free of HTML entities `&bull;` / `&mdash;` (use `\u2022` / `\u2014`).
7. **Dynamic Config & UI/API Verification (`tests/unit/test_config.py`, `tests/integration/test_web_api.py`):**
   - Verifies `.env` `MARKET_DATA_MODE` / `DEFAULT_MARKET_DATA_TYPE` / `FALLBACK_MARKET_DATA_TYPE` reconciliation, `LIVE` (`1 -> 2` & mid-page `10089` fallback), server-reported `Ticker.marketDataType` observation, and non-default `IBKR_HOST:IBKR_PORT` + `LIVE` rendering across HTML, `/api/status`, and offline `503` responses.

8. **`tests/unit/test_security_guard.py`:**
   - Verifies `scripts/security_guard.py` passes on `src/` and flags order/mutation APIs (`placeOrder`, `whatIfOrderAsync`, …), account/position queries (`reqPositions`, `reqAccountSummary`, …), `time.sleep`, and `pandas` imports.
9. **`tests/unit/test_agent_specs.py`:**
   - Verifies `.claude/agents/` and `.gemini/agents/` are in sync with `.agent/agents/` + `.agent/models.yaml`, read-only agents (`safety-reviewer`, `qa-git-runner`) have no `edit`/`write` tools, generated `.gemini/settings.json` `agents.overrides` thinking configs match `.agent/models.yaml` (`gemini-3.8-flash` always `thinkingLevel: HIGH`) while preserving unrelated settings, and invalid specs are rejected.
10. **`tests/integration/test_offline_fixture_smoke.py`:**
   - Loads the golden `NVDA_20261111_155004` snapshot with the current schema and renders `GET /` + `GET /api/chain/NVDA` offline (Gateway refused) end to end.
11. **`tests/unit/test_docs_consistency.py`:**
   - Fails when `AGENTS.md`, `docs/*.md`, or `.agent/agents/*.md` reference a `` `#<id>` `` missing from `src/web/` templates/JS/CSS, or a `GET|POST /api/...` route the app does not serve (checked against the OpenAPI schema).

---

## 5. Mandatory Documentation-Sync Checklist (Definition of Done)
Before completing any task, coding agents must verify:
- [ ] `make check` (`ruff check`, `ruff format --check`, `mypy` over `src tests scripts`, `scripts/security_guard.py`, `scripts/js_check.py`, `scripts/sync_agents.py --check`, and `pytest`) passes with 0 errors.
- [ ] `AGENTS.md` reflects any new commands, phases, or structural rules.
- [ ] `docs/ARCHITECTURE.md`, `docs/DATA_SCHEMA.md`, `docs/SECURITY_AND_SAFETY.md`, `docs/UI_UX_GUIDELINES.md`, and `docs/FUTURE_IMPROVEMENTS.md` are updated to match the exact state of the codebase.