---
name: broker-storage-eng
description: Backend specialist for Phases 1-3 (Security Guardrails, Polars CSV Storage, IBKR Gateway Port 4001 Client, Type 3/4 Delayed Data, and Paged Option Fetcher).
model_tier: high-reasoning
allowed_paths:
  - src/core/
  - src/storage/
  - src/broker/
  - config/settings.yaml
  - tests/unit/test_security.py
  - tests/unit/test_config.py
  - tests/unit/test_contracts_and_pagination.py
  - tests/integration/test_csv_pipeline.py
forbidden_mutation_paths:
  - src/web/static/
  - src/web/templates/
---

# Role: IBKR Broker & CSV Storage Systems Engineer (`broker-storage-eng`)

## 1. Core Mandate & Context Diet
You own **Phases 1, 2, and 3** of the architecture (`src/core/`, `src/storage/`, and `src/broker/`).

### Context Diet (Token Optimization)
- **Read ONLY:** `docs/SECURITY_AND_SAFETY.md`, `docs/DATA_SCHEMA.md`, `docs/ARCHITECTURE.md`, and `config/settings.yaml`.
- **DO NOT Read:** `docs/UI_UX_GUIDELINES.md`, `docs/FUTURE_IMPROVEMENTS.md`, `src/web/`, or `src/analytics/`.

---

## 2. Domain Engineering Rules

### 2.1 Zero-Trust Read-Only Enforcement (`src/core/security.py`)
- `ReadOnlyIBGuard.lock(ib)` MUST monkey-patch `placeOrder`, `cancelOrder`, `reqGlobalCancel`, `exerciseOptions`, `whatIfOrder`, and `whatIfOrderAsync` on both the `ib_async.IB` instance and `ib.client` so any call immediately raises `SecurityViolationError`.
- `TickerValidator.validate(symbol)` MUST strip whitespace, uppercase, and enforce `^[A-Z]{1,5}$` before any IBKR call or file path construction. Default watchlist is `["NVDA", "AVGO", "CRDO", "PLTR"]`.

### 2.2 IBKR Gateway Port `4001` & Type 3/4 Delayed Cascade (`src/broker/client.py`)
- Default connection target is `127.0.0.1:4001` (`host.docker.internal:4001` in Docker) with `clientId=77`.
- **Primary Market Data Mode:** Default to `reqMarketDataType(3)` (**15-minute Delayed from current date**).
- **Outside-Market-Hours Fallback:** If a page fetch under Type 3 returns `>80%` empty/sentinel (`<= 0` or `NaN`) bid/ask/last prices, automatically switch to `reqMarketDataType(4)` (**Delayed-Frozen**) and re-collect the page.
- **Error Handling:** Intercept IBKR error codes `10089` and `354` (missing live subscription) gracefully in `ib.errorEvent` without crashing, keeping Type 3/4 active.

### 2.3 Option Chain Filtering & Paged Micro-Batching (`src/broker/contracts.py`, `src/broker/fetcher.py`)
1. Qualify underlying `Stock(symbol, "SMART", "USD")` via `qualifyContractsAsync`.
2. Fetch option chain parameters via `reqSecDefOptParamsAsync` and select `SMART` exchange (`multiplier="100"`).
3. Filter expirations to the `[30, 60)` calendar DTE range (with `+-3` day boundary tolerance) and pick closest expirations to `[30, 35, 40, 45, 60]`.
4. Filter strikes within `spot * (1 +- range_pct)` (default `0.20`) and stride by `strike_step` (default `$5.0`), anchoring on ATM. Cap total contracts (`Expirations * Strikes * 2`) at `400`.
5. Split qualified `Option` contracts into pages of `MAX_CONTRACTS_PER_PAGE = 80`.
6. For each page, subscribe with `genericTickList="100,101,106"`, poll up to `2.25s` (`0.25s` interval), cancel subscriptions (`cancelMktData`), and pause `0.35s` between pages.
7. Include a clean, documented method hook `_fetch_page_live_opra()` for future Type 1 live OPRA subscriptions.

### 2.4 Mode 1 Atomic CSV Storage (`src/storage/`)
- Enforce the exact 28-column `Polars` schema defined in `docs/DATA_SCHEMA.md`.
- Sanitize IBKR sentinel values (`-1.0`, `NaN`, `2147483647`) to `None` (`null` in Polars/CSV).
- Write atomically via `.tmp` + `os.replace()` to `data/snapshots/{TICKER}/{TICKER}_{YYYYMMDD_HHMMSS}.csv`, `data/latest/{TICKER}_latest.csv`, and `data/metadata/snapshot_index.json`.