# Coding Standards, Typing & Testing Protocol (`CODING_STANDARDS.md`)

## 1. Modular Separation of Concerns
Coding agents must enforce strict boundaries between the 6 layers in `src/` (enforced by `tests/unit/test_layering.py`):

    core <- storage <- broker          analytics -> {core, storage}
    services -> {core, storage, broker, analytics}
    web -> {core, services}            main.py (composition root) -> anything

1. **`src/core/`:** Pure configuration (`config.py`), domain exceptions (`exceptions.py`), security guards (`security.py`), and NYSE trading calendar (`calendar.py`). Imports only `src.core`.
2. **`src/storage/`:** Schema definitions (`schema.py`, incl. shared domain literals `GreeksSource`, `OptionRight`, `WallBasis`, `SpotSource`; plus `ledger_schema.py` execution/position models), snapshot usability (`quality.py`), daily-bar selector (`backtest_store.py`), and CSV/JSON persistence (`csv_repository.py`, `paper_trade_store.py`, `ledger_repository.py`). Must never import `ib_async` or `fastapi`.
3. **`src/broker/`:** `ib-async` socket management, contract qualification, paged/exact-leg fetching, raw tick parsing, corridor subscriptions, Flex Web Service v3 (`flex.py`), and opt-in scoped ledger sync (`ledger_sync.py`). Never imports `analytics`, `services`, or `fastapi`: wall anchors (`WallAnchors`) are passed in and Greeks fallback is injected (`GreeksFallback` protocol).
4. **`src/analytics/`:** Pure, deterministic quantitative functions operating on `polars.DataFrame` inputs and returning typed Pydantic/dict payloads or `polars.DataFrame` outputs (`bsm_fallback.py`, `walls.py`, `anomalies.py`, `term_greeks.py`, `strategies/`, `ledger/`, `backtest/`). Zero network or disk I/O; may import `src.storage.{schema,ledger_schema,quality,backtest_store}` types/pure helpers only.
5. **`src/services/`:** Application orchestration (`analyzer.py` `AnalyzerService`, `corridor.py` glue, `ledger.py` `LedgerService`, `paper_trades.py` `PaperTradeService`, `backtest.py`, `schemas.py` request/response models). The only layer composing broker + storage + analytics. Framework-agnostic: raises domain exceptions, never `HTTPException`.
6. **`src/web/`:** FastAPI routes (`api.py`, `backtest_api.py`), exception-to-JSON mapping (`errors.py`), Jinja2 templates, and static assets. Imports only `src.core` and `src.services`.

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
- Never log stack traces raw to the browser UI; translate all domain exceptions in `src/web/errors.py` into structured JSON/HTML alert banners using `src/core/exceptions.py`.

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

### 4.0.1 Module Size & Frontend Module Rules
- Keep source files focused (guideline: `< ~800` lines). When a module grows past that, split it along responsibility seams into a package (e.g. `src/broker/corridor/`, `src/analytics/strategies/`) whose `__init__.py` re-exports the public API with `__all__`; cross-module helpers drop the leading underscore.
- Dashboard JS is native ES modules without a bundler: `main.js` is the only entry (`<script type="module">` in `index.html`). Module top-level code may only declare bindings or read bindings from modules evaluated earlier; `init()` runs from `main.js` after the whole graph is evaluated. Never reassign an imported binding (mutate `state` / `dom` instead). `make js-check` verifies every relative import resolves to an exported name.
- CSS is split per dashboard row; `base.html` links the files in cascade order (`tokens.css` first, `responsive.css` last). Static files are served with `Cache-Control: no-cache` (`RevalidatingStaticFiles`) so imported modules are always revalidated; still bump the `?v=` busters on `main.js` and the stylesheet loop after frontend edits.

### 4.1 Required Test Modules
Every feature phase requires automated tests in `pytest`:
1. **`tests/unit/test_security.py`:**
   - Tests `TickerValidator.validate()` against valid symbols (`NVDA`, `AVGO`, `CRDO`, `PLTR`, `SPY`) and malicious inputs (`../etc`, `NVDA;rm`, `BRK.A`, `12345`, `""`, `"ABCDEFG"`).
   - Tests `ReadOnlyIBGuard.lock()` ensuring `placeOrder`, `cancelOrder`, `reqGlobalCancel`, `exerciseOptions`, and `whatIfOrder` raise `SecurityViolationError`.
2. **`tests/unit/test_broker_client.py`, `test_contracts.py`, `test_parser.py`, `test_fetcher.py`** (shared fake `FakeChainIB` in `tests/support/ib_fakes.py`):
   - Verify read-only client locking and error-code handling, strict `[30, 120]` weekly DTE matching, `+-X%` moneyness filtering, grid-aligned `$ strike_step` striding (and Auto step selection), and `<= 80` contracts/page splitting.
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
   - Keep JavaScript strings inside `src/web/static/js/dashboard/*.js` free of HTML entities `&bull;` / `&mdash;` (use `\u2022` / `\u2014`).
7. **Dynamic Config & UI/API Verification (`tests/unit/test_config.py`, `tests/integration/test_web_api.py`):**
   - Verifies `.env` `MARKET_DATA_MODE` / `DEFAULT_MARKET_DATA_TYPE` / `FALLBACK_MARKET_DATA_TYPE` and `LedgerSettings` (`IBKR_FLEX_*`, `ENABLE_GATEWAY_LEDGER_SYNC`, `LEDGER_*`) overrides, `LIVE` (`1 -> 2` & mid-page `10089` fallback), server-reported `Ticker.marketDataType` observation, and non-default `IBKR_HOST:IBKR_PORT`, `LIVE`, and Flex/Gateway ledger status flags across HTML, `/api/status`, and offline `503` responses.

8. **`tests/unit/test_security_guard.py`:**
   - Verifies `scripts/security_guard.py` passes on `src/`, enforces `SCOPED_ALLOWLIST` (`.reqExecutionsAsync(` and `.reqPositionsAsync(` allowed only inside `src/broker/ledger_sync.py`), and flags order/mutation APIs (`placeOrder`, `whatIfOrderAsync`, …), sync/disallowed account/position queries (`reqPositions`, `reqExecutions`, `reqAccountSummary`, …), `time.sleep`, and `pandas` imports.
9. **`tests/unit/test_agent_specs.py`:**
   - Verifies `.claude/agents/` and `.gemini/agents/` are in sync with `.agent/agents/` + `.agent/models.yaml`, read-only agents (`safety-reviewer`, `qa-git-runner`) have no `edit`/`write` tools, generated `.gemini/settings.json` `agents.overrides` thinking configs match `.agent/models.yaml` (`gemini-4-argon` at `thinkingLevel: HIGH`, `gemini-4-hydrogen` at `MEDIUM`/`LOW`, legacy `gemini-3.8-flash` at `HIGH`) while preserving unrelated settings, and invalid specs are rejected.
10. **`tests/integration/test_offline_fixture_smoke.py`:**
   - Loads the golden `NVDA_20261111_155004` snapshot with the current schema and renders `GET /` + `GET /api/chain/NVDA` offline (Gateway refused) end to end.
11. **`tests/unit/test_docs_consistency.py`:**
   - Fails when `AGENTS.md`, `docs/*.md`, or `.agent/agents/*.md` reference a `` `#<id>` `` missing from `src/web/` templates/JS/CSS, or a `GET|POST /api/...` route the app does not serve (checked against the OpenAPI schema).
12. **`tests/unit/test_layering.py`:**
   - AST-scans every module in `src/{core,storage,broker,analytics,services,web}` and fails on imports that violate the §1 dependency rules (including banned third-party imports such as `fastapi` outside `web`).
13. **`tests/unit/test_flex_and_ledger_storage.py`:**
   - Verifies Flex XML & Activity CSV parsing (`parse_flex_xml`, `parse_activity_csv`), `redact_flex_secrets()`, `select_flex_query_ids()` (`auto`/`monthly`/`full` + `FLEX_TRADE_CONF`), `LedgerExecutionRow`/`LedgerPositionRow` normalization (positive commission cost, UTC ISO-8601 timestamps, deterministic `synthesize_exec_id`), and `LedgerRepository` atomic upserts + `GATEWAY_LIVE` -> `FLEX_*` upgrades.
14. **`tests/unit/test_ledger_sync.py`:**
   - Verifies `ENABLE_GATEWAY_LEDGER_SYNC` gating (`ConfigurationError` when disabled), account-attribute stripping in `fetch_gateway_ledger()`, bounded `<= 60` `refresh_open_option_marks()` polling, `DELAYED 3->4` / `LIVE 1->2` / `10089` mark fallback, and 100% `cancelMktData` cleanup.
15. **`tests/unit/test_ledger_analytics.py`:**
   - Verifies order/time clustering (`<= 2.0s`), 17-strategy classification (including uncovered-stock checks distinguishing `COVERED_CALL` vs `NAKED_CALL`), FIFO matching across trades/expirations/assignments/exercises, pre-window orphan-close safety, `roll_max_gap_hours` roll stitching, strict `realized` vs `open_mtm` separation, ex-fee P&L with sidecar `fees`, `include_stock_gain`, and live MTM mark precedence.
16. **`tests/unit/test_ledger_service.py`:**
   - Verifies `LedgerService` status reporting, Flex/Gateway sync coordination, XML/CSV upload bounds/validation, and in-memory live MTM mark caching without disk CSV mutation.
17. **`tests/integration/test_ledger_api.py`:**
   - Verifies `GET /api/ledger`, `POST /api/ledger/sync`, `POST /api/ledger/import`, `POST /api/ledger/mtm`, structured `422` (`CONFIGURATION_ERROR` / `INVALID_REQUEST`), `502` (`FLEX_SERVICE_ERROR`), `503` (`GATEWAY_OFFLINE`), and rendered `#trade-ledger-card` HTML smoke checks.
18. **`tests/unit/test_calendar.py`:**
   - Verifies the 2026–2027 NYSE trading calendar (`is_trading_day`, `NYSE_HOLIDAYS`, `NYSE_EARLY_CLOSES`, `session_for`, `et_date`, `trading_days_between`, `next_trading_day`, `fmt_et`) and 15-minute `DATA_DELAY` session cutoffs.
19. **`tests/unit/test_snapshot_quality.py`, `test_paper_trade_store.py`, `test_backtest_store.py`:**
   - Verifies `SpotSource` / quote-coverage usability scoring (`quality.py`), atomic `paper_trades.json`, append-only `paper_quotes.jsonl`, immutable `data/tracked/{TICKER}/` writes, and `O(window)` canonical daily-bar + tracked-quote merging (`backtest_store.py`).
20. **`tests/unit/test_backtest_pricing.py` & `test_backtest_engine.py`:**
   - Verifies contract-filtered BSM enrichment, chain + tracked fallback leg pricing (`PriceBasis`, `ClosePriceMode`), `TpSlBasis` rules, expiry settlement cutoffs (`$0` closing fees on expired legs), `SYSTEMATIC` cadences, `SINGLE_TRADE` replay, and portfolio metrics.
21. **`tests/unit/test_analyzer_concurrency.py`, `test_paper_trades_service.py`, `test_backtest_service.py`:**
   - Verifies `AnalyzerService.refresh_lock` (`RefreshBusyError` on overlapping refreshes/corridor/MTM/SSE), pre-refresh `stop_all_streams()`, `fetch_exact_contracts` / `PaperTradeService` follow-up quoting, and `services/backtest.py` entry listing + `SYSTEMATIC`/`SINGLE_TRADE` orchestration.
22. **`tests/integration/test_backtest_api.py`:**
   - Verifies `GET /api/backtest/entries/{ticker}`, `POST /api/backtest/run`, `GET /api/backtest/paper-trades`, `POST /api/backtest/paper-trades`, `POST /api/backtest/paper-trades/{trade_id}/stop`, structured `409`/`422` error codes, and rendered `#backtest-card` + `#snapshot-picker` HTML smoke checks.

---

## 5. Mandatory Documentation-Sync Checklist (Definition of Done)
Before completing any task, coding agents must verify:
- [ ] `make check` (`ruff check`, `ruff format --check`, `mypy` over `src tests scripts`, `scripts/security_guard.py`, `scripts/js_check.py`, `scripts/sync_agents.py --check`, and `pytest`) passes with 0 errors.
- [ ] `AGENTS.md` reflects any new commands, phases, or structural rules.
- [ ] `docs/ARCHITECTURE.md`, `docs/DATA_SCHEMA.md`, `docs/SECURITY_AND_SAFETY.md`, `docs/UI_UX_GUIDELINES.md`, `docs/FUTURE_IMPROVEMENTS.md`, and `docs/reference/IMPLEMENTATION_NOTES.md` are updated to match the exact state of the codebase.
- [ ] User-visible changes are listed under `## [Unreleased]` in `CHANGELOG.md`.