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
Every feature phase requires automated tests in `pytest`:
1. **`tests/unit/test_security.py`:**
   - Tests `TickerValidator.validate()` against valid symbols (`NVDA`, `AVGO`, `CRDO`, `PLTR`, `SPY`) and malicious inputs (`../etc`, `NVDA;rm`, `BRK.A`, `12345`, `""`, `"ABCDEFG"`).
   - Tests `ReadOnlyIBGuard.lock()` ensuring `placeOrder`, `cancelOrder`, `reqGlobalCancel`, `exerciseOptions`, and `whatIfOrder` raise `SecurityViolationError`.
2. **`tests/unit/test_contracts_and_pagination.py`:**
   - Verifies strict `(30, 60)` weekly DTE matching, `+-X%` moneyness filtering, grid-aligned `$ strike_step` striding (and Auto step selection), and `<= 80` contracts/page splitting.
3. **`tests/unit/test_bsm_and_analytics.py`:**
   - Verifies vectorized BSM implied volatility and Greeks against known analytical option values.
   - Verifies Put/Call Wall detection (both OI mode and Volume fallback mode) and `Volume > OI` anomaly detection.
4. **`tests/integration/test_csv_pipeline.py`:**
   - Verifies Mode 1 archival writes, `latest` updates, `snapshot_index.json` rebuilds, and full offline pipeline execution from CSV -> Analytics -> FastAPI response.

---

## 5. Mandatory Documentation-Sync Checklist (Definition of Done)
Before completing any task, coding agents must verify:
- [ ] `make check` (`ruff check`, `ruff format --check`, `mypy`, `pytest`, and read-only grep check) passes with 0 errors.
- [ ] `AGENTS.md` reflects any new commands, phases, or structural rules.
- [ ] `docs/ARCHITECTURE.md`, `docs/DATA_SCHEMA.md`, `docs/SECURITY_AND_SAFETY.md`, `docs/UI_UX_GUIDELINES.md`, and `docs/FUTURE_IMPROVEMENTS.md` are updated to match the exact state of the codebase.