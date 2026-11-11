# System Architecture Specification (`ARCHITECTURE.md`)

## 1. Architectural Overview
The **IBKR Options Analyzer & Dashboard** is a local, single-user, strictly read-only analytical web application. It decouples broker data acquisition (`ib-async`), local persistence (`Polars` CSV store), quantitative computation (`Polars` + `NumPy`/`SciPy`), and visualization (`FastAPI` + `Plotly.js`) so the UI remains 100% functional offline using cached CSV snapshots even when IBKR Gateway is closed.

    +-----------------------------------------------------------------------------------+
    |                        BROWSER UI (NYT Editorial + Terminal)                      |
    |  [Reserved Editorial Banner Slot]  [Watchlist: NVDA|AVGO|CRDO|PLTR + Custom Box]  |
    |  [Range % | Strike Step $ | Refresh Button | Historical Snapshot Selector]        |
    |  [Multi-DTE Plotly Overlays (Walls, IV, Greeks)]  [High-Density Tabular Chain]    |
    +-----------------------------------------+-----------------------------------------+
                                              | HTTP / JSON / HTMX (127.0.0.1:8000)
                                              v
    +-----------------------------------------------------------------------------------+
    |                         FASTAPI APPLICATION SERVER (src/main.py)                  |
    |                                                                                   |
    |  +-----------------------------+               +-------------------------------+  |
    |  |   Security & Input Layer    |               |    Analytics Engine (Polars)  |  |
    |  | - TickerValidator (Regex)   |               | - BSM IV/Greeks Fallback      |  |
    |  | - ReadOnlyIBGuard (No Order)|               | - Put/Call Walls (OI & Vol)   |  |
    |  +--------------+--------------+               | - Unusual Activity (Vol > OI) |  |
    |                 |                              | - Multi-DTE (30,60) Weekly    |  |
    |                 v                              +---------------+---------------+  |
    |  +-----------------------------+                               ^                  |
    |  |   Broker Layer (ib-async)   |                               |                  |
    |  | - MarketDataType (3 -> 4)   |      +------------------------+---------------+  |
    |  | - Weekly (30,60) & Step $   |----->|      CSV Storage Layer (Mode 1)        |  |
    |  | - Paged Fetcher (<=80/page) |      | - data/snapshots/{TICKER}/{TS}.csv     |  |
    |  | - OPRA Tick 101 Placeholder |      | - data/latest/{TICKER}_latest.csv      |  |
    |  +--------------+--------------+      | - data/metadata/snapshot_index.json    |  |
    +-----------------|---------------------+----------------------------------------+--+
                      | TCP Socket (Port 4001 Default)
                      v
    +-----------------------------------------------------------------------------------+
    |               IBKR GATEWAY (Local Host 127.0.0.1:4001 / Docker Bridge)            |
    +-----------------------------------------------------------------------------------+

---

## 2. Target Repository Structure

    ibkr-options-analyzer/
    ├── AGENTS.md
    ├── Makefile
    ├── Dockerfile
    ├── docker-compose.yml
    ├── pyproject.toml
    ├── .env.example
    ├── config/
    │   └── settings.yaml
    ├── docs/
    │   ├── ARCHITECTURE.md
    │   ├── UI_UX_GUIDELINES.md
    │   ├── CODING_STANDARDS.md
    │   ├── SECURITY_AND_SAFETY.md
    │   ├── DATA_SCHEMA.md
    │   └── FUTURE_IMPROVEMENTS.md
    ├── data/
    │   ├── snapshots/
    │   ├── latest/
    │   └── metadata/
    ├── src/
    │   ├── __init__.py
    │   ├── main.py
    │   ├── core/
    │   │   ├── __init__.py
    │   │   ├── config.py
    │   │   ├── exceptions.py
    │   │   └── security.py
    │   ├── broker/
    │   │   ├── __init__.py
    │   │   ├── client.py
    │   │   ├── contracts.py
    │   │   ├── fetcher.py
    │   │   └── parser.py
    │   ├── storage/
    │   │   ├── __init__.py
    │   │   ├── schema.py
    │   │   └── csv_repository.py
    │   ├── analytics/
    │   │   ├── __init__.py
    │   │   ├── bsm_fallback.py
    │   │   ├── walls.py
    │   │   ├── anomalies.py
    │   │   └── term_greeks.py
    │   └── web/
    │       ├── __init__.py
    │       ├── api.py
    │       ├── templates/
    │       │   ├── base.html
    │       │   ├── index.html
    │       │   └── partials/
    │       └── static/
    │           ├── css/
    │           │   └── tokens.css
    │           └── js/
    │               └── dashboard.js
    └── tests/
        ├── unit/
        ├── integration/
        └── fixtures/

---

## 3. Core Component Responsibilities

### 3.1 FastAPI Lifespan & Shared Event Loop (`src/main.py`, `src/broker/client.py`)
- `ib-async` requires an active `asyncio` event loop. FastAPI's `lifespan` context manager initializes a singleton `IBKRBrokerClient` bound to the Uvicorn event loop.
- **Offline-First Boot Policy:** If IBKR Gateway is closed at startup, the server still boots cleanly in **Offline Cache Mode**, serving data from `data/latest/` and `data/snapshots/`.
- When the user clicks **Refresh**, `IBKRBrokerClient.ensure_connected()` connects to `IBKR_HOST:IBKR_PORT` (default `127.0.0.1:4001` locally, `host.docker.internal:4001` in Docker) with a dedicated read-only `clientId` (default `77`).

### 3.2 Market Data Mode Architecture: Type 3 (Delayed) Primary & Live OPRA Scaffold
To operate primarily on **Type 3 (15-minute Delayed from current date)** without an OPRA subscription while supporting seamless future upgrades to Live OPRA:

| Config Mode (`MARKET_DATA_MODE`) | Primary IBKR Code | Auto-Fallback Code | Operational Behavior |
| :--- | :--- | :--- | :--- |
| **`DELAYED` (Default)** | `Type 3` (Delayed 15m) | `Type 4` (Delayed-Frozen) | Requests 15-minute delayed quotes from the **current trading session** (`reqMarketDataType(3)`). If queried outside regular trading hours and $>80\%$ of quotes return `-1`/`NaN`, automatically cascades to `Type 4` (`reqMarketDataType(4)`) so closing quotes populate. |
| **`LIVE` (Future OPRA)** | `Type 1` (Live OPRA) | `Type 2` (Frozen) | Scaffolded placeholder mode. When enabled in `settings.yaml` or `.env`, requests real-time OPRA streaming ticks (`reqMarketDataType(1)`) and cascades to `Type 2` outside market hours. |

### 3.3 Option Chain Discovery, Strike Striding & Paged Fetching (`src/broker/contracts.py`, `src/broker/fetcher.py`)
Every manual refresh executes a bounded **5-Stage Pipeline** to stay well below IBKR Gateway's 100-line active market data limit and 50 msg/sec pacing ceiling:

1. **Stage 1 — Underlying Qualification & Spot Resolution:**
   - Validate ticker via `TickerValidator.validate(symbol)`.
   - Qualify `Stock(symbol, 'SMART', 'USD')` via `qualifyContractsAsync`.
   - Fetch underlying reference price (`last`, `mid`, or `close` under `Type 3` / `Type 4`). Abort with `UnderlyingPriceUnavailableError` if spot `<= 0`.
2. **Stage 2 — Chain Definition Lookup:**
   - Call `reqSecDefOptParamsAsync(underlying.symbol, '', underlying.secType, underlying.conId)` and select the primary `SMART` exchange definition with multiplier `'100'`.
3. **Stage 3 — Weekly Expiration Matching (`30 < DTE < 60`):**
   - Filter available expiration dates where calendar DTE from current market date satisfies strictly `30 < dte < 60` (`dte_min < dte < dte_max`).
   - Group remaining expirations by ISO calendar week `(year, week)` and select one expiration per week—preferring standard Friday (`weekday == 4`) over mid-week listings, otherwise choosing the latest trading day in that week (covering holiday Thursdays)—so every weekly expiry inside `(30, 60)` is included without target buckets.
4. **Stage 4 — Moneyness Window & Grid-Aligned Strike Step Striding:**
   - **Moneyness Filter:** Keep strikes within `[spot * (1 - range_pct), spot * (1 + range_pct)]` (default `range_pct = 0.20` for `+-20%`, adjustable in UI: `10%`, `15%`, `20%`, `25%`, `35%`, `50%`).
   - **Grid-Aligned Strike Step & Auto Step:** When `strike_step` is `None` (`Auto`), `choose_auto_strike_step` picks the finest canonical step `(0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0)` yielding `<= min(28, max_strikes)` strikes (skipping fractional `$0.5`/`$2.5` steps unless native half-grid strikes exist). `_pool_for_step` prefers strikes aligned to multiples of `strike_step` when genuine grid coverage exists, preventing off-grid short-DTE `.50` / `$1` strikes from trapping the ATM anchor (`$5.00` default, UI options: `Auto`, `$1.00`, `$2.50`, `$5.00`, `$10.00`, `$25.00`).
   - **Hard Safety Cap:** Total option contracts (`Expirations * Strikes * 2`) cannot exceed `MAX_TOTAL_CONTRACTS = 400` (`5 pages * 80 contracts/page`). If a wide range + small step exceeds 400 contracts, the strider widens along canonical steps and returns an informational notice to the UI.
5. **Stage 5 — Paged Micro-Batch Downloader & Future OPRA Streaming Placeholder:**
   - Construct `Option` contracts (`Right='C'` and `Right='P'`) for the filtered grid and qualify them in chunks via `qualifyContractsAsync`.
   - Partition qualified contracts into pages of **`MAX_CONTRACTS_PER_PAGE = 80`**.
   - **Execution Modes in `fetcher.py`:**
     - **Default `DELAYED` Collector (`_fetch_page_delayed`):** Calls `reqMktData(contract, genericTickList="100,101,106", snapshot=False, regulatorySnapshot=False)` for the page of `<= 80` contracts, polls every `0.25s` up to `2.25s` until delayed price ticks populate, cancels subscriptions (`cancelMktData`), and waits `0.35s` before the next page.
     - **Scaffolded `LIVE_OPRA` Collector (`_fetch_page_live_opra`):** Pre-wired method hook that uses `reqMktData` with live OPRA generic ticks `100,101,104,106` and waits specifically for both `modelGreeks` and `callOpenInterest`/`putOpenInterest` (tick `101`) confirmation before page teardown.

---

## 4. Quantitative Analytics & BSM Fallback Engine (`src/analytics/`)

### 4.1 Vectorized Black-Scholes-Merton (BSM) Fallback (`src/analytics/bsm_fallback.py`)
Under IBKR **Type 3 (15-min Delayed)** data without an OPRA subscription, IBKR often populates delayed prices (`bid`, `ask`, `last`, `close`) while omitting `modelGreeks`.
- For every row where `modelGreeks` is missing/null and a valid option `mark_price` (`mid` -> fallback `last` -> fallback `close`) exceeds intrinsic value:
  1. Time to expiry: `T = max(dte, 0.5) / 365.0`.
  2. Implied Volatility `iv`: Solved via vectorized Brent/Newton root-finding on `[0.01, 5.00]` using `risk_free_rate = 0.0425` (from `config/settings.yaml`).
  3. Analytical Greeks: `delta`, `gamma`, `theta` (per calendar day), and `vega` (per 1% IV change) are computed from the solved `iv`.
  4. Provenance column `greeks_source` is set to `"BSM_FALLBACK"` (vs. `"IBKR_MODEL"` when native Greeks are returned).

### 4.2 Core Analytical Modules
1. **Put Walls & Call Walls (`src/analytics/walls.py`):**
   - Computes top Call Walls and Put Walls per weekly expiration (`by_dte`) and across the aggregate `(30, 60)` DTE window using both **Open Interest** and **Volume**.
   - If `open_interest` is null across the snapshot (common on unsubscribed Type 3 feeds), the module automatically promotes **Volume Walls** as primary and sets `wall_metric_used = "VOLUME"`.
2. **Unusual Activity Scanner (`src/analytics/anomalies.py`):**
   - Flags `volume > open_interest` anomalies when `open_interest > 0`.
   - Computes intra-expiration Volume Z-Scores (`(volume - mean_vol) / std_vol`) so unusual strike activity is always surfaced even when Open Interest is null.
3. **Multi-DTE Comparative Structure (`src/analytics/term_greeks.py`):**
   - Aligns option chains across all weekly expirations in `(30, 60)` DTE over a unified strike and moneyness axis for Plotly overlays (IV Smile overlay, Delta/Gamma/Theta/Vega profile by DTE, Volume/OI concentration).

---

## 5. Resilience & Error Handling Matrix

| Failure Scenario | Detection Mechanism | System Recovery & UI Behavior |
| :--- | :--- | :--- |
| **IBKR Gateway Closed (`ConnectionRefusedError`)** | Caught in `IBKRBrokerClient.ensure_connected()` within `3.0s` timeout. | Returns status `GATEWAY_OFFLINE`. UI loads `data/latest/{TICKER}_latest.csv` with status badge `OFFLINE CACHE — PORT 4001 UNREACHABLE`. |
| **Missing Live Subscription (`Error 10089 / 354`)** | Intercepted in `ib.errorEvent` callback. | Logs informational notice, enforces `reqMarketDataType(3)`, and continues delayed collection without crashing. |
| **Outside Market Hours / Unpopulated Type 3 Ticks** | Page collector detects `>80%` of contracts have empty `bid`/`ask`/`last`. | Automatically switches to `reqMarketDataType(4)` (Delayed-Frozen) and re-polls the page so last closing prices populate. |
| **Invalid Custom Ticker** | Caught in `TickerValidator` or empty `qualifyContractsAsync` result. | Rejects request with HTTP `422 Unprocessable Entity` and descriptive error banner before touching option chains or disk. |
| **API Pacing Warning (`Error 100 / 162`)** | Intercepted in `ib.errorEvent`. | Paged fetcher increases inter-page cooldown (`0.35s -> 1.0s`) and retries the page once. |


---

## 6. Phase 1 Implementation Notes (`src/core/`) — `[IMPLEMENTED]`
- **`src/core/exceptions.py`:** `AnalyzerError` base + `SecurityViolationError`, `InvalidTickerError`, `ConfigurationError`, `GatewayConnectionError`, `UnderlyingPriceUnavailableError`, `OptionChainFetchError`, `PacingViolationError`, `StorageSchemaError`.
- **`src/core/config.py`:** Frozen, `extra="forbid"` Pydantic v2 models (`AppSettings`, `IBKRSettings`, `WatchlistSettings`, `OptionsChainSettings`, `AnalyticsSettings`, `StorageSettings`, root `Settings`). `load_settings(yaml_path, env_file)` loads `config/settings.yaml`, then applies flat env overrides (process env beats `.env`): `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, `MARKET_DATA_MODE`, `DEFAULT_MARKET_DATA_TYPE`, `RISK_FREE_RATE`, `LOG_LEVEL`, `APP_HOST`, `APP_PORT`. Validation failures raise `ConfigurationError`. `ibkr.read_only_enforced: false` is rejected. `get_settings()` is `lru_cache`d; `configure_logging()` is idempotent (stdout + rotating `storage.logs_dir/analyzer.log`, 5 MB x 5).
- **`src/core/security.py`:** `TickerValidator` (`validate` classmethod = universal gate; instance `is_watchlist` / `validate_selection` honour `allow_custom_ticker`) and `ReadOnlyIBGuard.lock/verify_locked` (traps installed on the `IB` instance and `ib.client`).
- **Cross-platform tooling:** `Makefile` uses `uv run --no-sync` and Python one-liners, so `make check` works natively offline on Windows, macOS and Linux.

---

## 7. Phase 3 Implementation Notes (`src/broker/`) — `[IMPLEMENTED]`
- **`src/broker/client.py` (`IBKRBrokerClient`):** Immediately locks `ib_async.IB` with `ReadOnlyIBGuard.lock` + `verify_locked`. Connects via `ib.client.connectAsync` (bypassing `IB.connectAsync`'s automatic position/order/account queries so zero account queries ever execute). Absorbs `Error 10089`/`354` (falling back to Type 3 if in `LIVE` mode), ignores info codes `2104/2106/2107/2108/2158`, flags pacing codes `100/162`, and manages Type `3 -> 4` (`DELAYED`) and `1 -> 2` (`LIVE`) market data cascades.
- **`src/broker/contracts.py` (`ContractDiscoveryService`):** Qualifies `Stock(symbol, "SMART", "USD")`, resolves reference spot (`last` -> `mid` -> `close`, cascading Type 3 -> 4 when needed), selects the primary `SMART` multiplier-`100` chain, selects one weekly expiration per ISO week strictly inside `30 < dte < 60` (`match_expirations`, preferring Friday), computes `choose_auto_strike_step` when `strike_step=None`, and runs grid-aligned strike selection (`select_strikes` via `_pool_for_step`) within `+-range_pct` while auto-widening along canonical steps if total contracts would exceed `400`.
- **`src/broker/parser.py` (`TickerParser`):** Sanitizes IBKR sentinels, maps `callOpenInterest`/`putOpenInterest` (tick `101`) by option right, validates `modelGreeks` bounds (`0 < iv <= 10.0`, `abs(delta) <= 1.001`, `gamma >= 0`) to classify `greeks_source` as `"IBKR_MODEL"` or `"NONE"`, and returns validated 27-field `OptionContractRow` models.
- **`src/broker/fetcher.py` (`PagedOptionChainFetcher`):** Qualifies `Option` contracts in `<= 80`-contract batches, partitions into pages (`<= 80` contracts/page, max `5` pages = `400` contracts), implements `_fetch_page_delayed` (`genericTickList="100,101,106"`, `try/finally` `cancelMktData` leak guard, `>80%` empty quote Type 3 -> Type 4 auto-fallback, and pacing cooldown widening `0.35s -> 1.0s`) and scaffolded `_fetch_page_live_opra` (`genericTickList="100,101,104,106"`), and logs the structured `refresh_summary` line.


---

## 8. Phase 4 Implementation Notes (`src/analytics/`) — `[IMPLEMENTED]`
- **`src/analytics/bsm_fallback.py` (`BSMFallbackEngine`, `enrich_missing_greeks`):** Vectorized 25-iteration hybrid Newton-Raphson + Bisection IV solver on `[0.01, 5.00]` ($T = \max(\text{dte}, 0.5) / 365.0$) and analytical BSM Greeks (`delta`, `gamma`, daily `theta`, `vega` per 1% IV), preserving native `"IBKR_MODEL"` rows and leaving unsolvable rows `"NONE"`.
- **`src/analytics/walls.py` (`WallsAnalyzer`, `compute_walls`):** Returns `ChainWallsReport` (`WallLevel`, `DTEWallSummary`, `by_dte`) ranking aggregate and per-DTE Put/Call walls by `OPEN_INTEREST` (tie-breaking on `volume`) with automatic fallback to `VOLUME` when total Open Interest is `0`.
- **`src/analytics/anomalies.py` (`AnomalyScanner`, `scan_anomalies`):** Returns `AnomaliesReport` (`ContractAnomaly`) flagging `volume > open_interest` (`vol_oi_ratio`) and intra-`(expiry, right)` Volume Z-score outliers (`volume_zscore >= zscore_threshold`), sorted by `(is_vol_gt_oi desc, vol_oi_ratio desc nulls last, volume_zscore desc)`.
- **`src/analytics/term_greeks.py` (`MultiDTEStructureAnalyzer`, `build_multi_dte_profiles`):** Returns `MultiDTEProfilesReport` (`DTEProfile`, `StrikeProfilePoint`, `expiries`) aligning Calls and Puts by strike across weekly `(30, 60)` expirations and computing per-expiry `atm_strike` and `atm_iv`.


---

## 9. Phase 5 Implementation Notes (`src/main.py`, `src/web/`) — `[IMPLEMENTED]`
- **`src/main.py` (`create_app`, `lifespan`):** Configures idempotent logging, mounts `/static` (`StaticFiles`), wires Jinja2 templates (`src/web/templates/`), manages the singleton `IBKRBrokerClient` + `AnalyzerWebService` on `app.state` without crashing when port `4001` is closed at boot, disconnects cleanly on shutdown, and serves `GET /` with settings-driven watchlist, `range_pct`, and `strike_step` options.
- **`src/web/api.py` (`AnalyzerWebService`, `router`):** Exposes typed endpoints `GET /api/status`, `GET /api/snapshots/{ticker}`, `GET /api/chain/{ticker}?snapshot_id=...`, `POST /api/refresh` (with `BSMFallbackEngine` enrichment, effective strike step verification/inferred fallback from `infer_chain_step_and_range`, and immutable `CSVRepository` archival storing `requested_strike_step`), and `GET /api/csv/{ticker}?snapshot_id=...` (`FileResponse` CSV download), exposing `range_pct`, `strike_step`, and `requested_strike_step` in `ChainMetadataPayload` alongside structured JSON exception handlers (`422` invalid ticker, `404` missing snapshot, `503` gateway offline / fetch / pacing error).
- **Editorial + Terminal UI (`src/web/templates/{base,index}.html`, `src/web/static/css/tokens.css`, `src/web/static/js/dashboard.js`):** Implements the 6-row 12-column responsive layout with NYT Print Edition (`data-theme="print"`) vs. Obsidian Terminal (`data-theme="terminal"`) `localStorage` switching + dynamic Plotly re-skinning, `#editorial-banner-slot` with pull-scope telemetry, `Step $` / `Range %` control synchronization + snapshot dropdown scope metadata, 4 KPI cards, corner-resizable **Snap-to-Grid** (`span 4 / 6 / 8 / 12` CSS Grid columns & quantized heights persisted in `localStorage`) multi-DTE Plotly panels (`#chart-walls`, `#chart-iv-smile`, `#chart-anomalies`, `#chart-greeks` with framed `SPOT`/Wall markers, anomaly overlay toggle, and `#walls-copy-ai-btn` copying a concise, strictly structured Polish-language AI prompt preloaded with active DTE walls, ATM IV, anomalies, `Step $`/`Range %` strike ladder, current-day context directive, and Top 3 option strategy synthesis), and the 17-column monospaced `#chain-table` with DTE/Expiry, Right, and Unusual filters.
