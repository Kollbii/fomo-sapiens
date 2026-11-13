# System Architecture Specification (`ARCHITECTURE.md`)

## 1. Architectural Overview
The **IBKR Options Analyzer & Dashboard** is a local, single-user, strictly read-only analytical web application. It decouples broker data acquisition (`ib-async`), local persistence (`Polars` CSV store), quantitative computation (`Polars` + `NumPy`/`SciPy`), and visualization (`FastAPI` + `Plotly.js`) so the UI remains 100% functional offline using cached CSV snapshots even when IBKR Gateway is closed.

    +-----------------------------------------------------------------------------------+
    |                        BROWSER UI (NYT Editorial + Terminal)                      |
    |  [Reserved Editorial Banner Slot]  [Watchlist: NVDA|AVGO|CRDO|PLTR + Custom Box]  |
    |  [Range % | Strike Step $ | Refresh Button | Historical Snapshot Selector]        |
    |  [Multi-DTE Plotly Overlays (Walls, IV, Greeks)]  [High-Density Tabular Chain]    |
    +-----------------------------------------+-----------------------------------------+
                                              | HTTP / JSON / SSE (127.0.0.1:8000)
                                              v
    +-----------------------------------------------------------------------------------+
    |                         FASTAPI APPLICATION SERVER (src/main.py)                  |
    |                                                                                   |
    |  +-----------------------------+               +-------------------------------+  |
    |  |   Security & Input Layer    |               |    Analytics Engine (Polars)  |  |
    |  | - TickerValidator (Regex)   |               | - BSM IV/Greeks Fallback      |  |
    |  | - ReadOnlyIBGuard (No Order)|               | - Put/Call Walls (OI & Vol)   |  |
    |  +--------------+--------------+               | - Unusual Activity (Vol > OI) |  |
    |                 |                              | - Multi-DTE [30,120] Weekly    |  |
    |                 v                              +---------------+---------------+  |
    |  +-----------------------------+                               ^                  |
    |  |   Broker Layer (ib-async)   |                               |                  |
    |  | - MarketDataType (3 -> 4)   |      +------------------------+---------------+  |
    |  | - Weekly [30,120] & Step $   |----->|      CSV Storage Layer (Mode 1)        |  |
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
    ├── README.md                 # human quickstart
    ├── CHANGELOG.md              # Keep a Changelog; user-visible changes under [Unreleased]
    ├── AGENTS.md
    ├── CLAUDE.md / GEMINI.md     # platform entry files importing AGENTS.md
    ├── .agent/                   # canonical sub-agent specs + models.yaml (source of truth)
    ├── .claude/agents/           # GENERATED Claude Code sub-agents
    ├── .gemini/                  # GENERATED Gemini CLI sub-agents + settings.json
    ├── scripts/
    │   ├── sync_agents.py        # make agents-sync / agents-check
    │   ├── security_guard.py     # make security-guard
    │   └── js_check.py           # make js-check (node --check)
    ├── Makefile
    ├── Dockerfile                # multi-stage, uv.lock-pinned, non-root
    ├── .dockerignore             # build-context allow-list
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
    │   ├── FUTURE_IMPROVEMENTS.md
    │   └── reference/
    │       └── IMPLEMENTATION_NOTES.md  # per-module implementation details (not contracts)
    ├── data/
    │   ├── snapshots/
    │   ├── latest/
    │   ├── metadata/
    │   └── ledger/               # executions.csv, open_positions.csv, sync_state.json, imports/
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
    │   │   ├── corridor/         # models, quotes, selection (pure), service (subscriptions)
    │   │   ├── fetcher.py
    │   │   ├── flex.py           # Flex Web Service v3 client & XML/CSV statement parser
    │   │   ├── ledger_sync.py    # opt-in Gateway execution/position sync & <= 60 MTM marks
    │   │   └── parser.py
    │   ├── storage/
    │   │   ├── __init__.py
    │   │   ├── schema.py
    │   │   ├── csv_repository.py
    │   │   ├── ledger_schema.py      # 23-col execution & 15-col open position contracts
    │   │   └── ledger_repository.py  # atomic data/ledger/ archive & Gateway -> Flex upgrade
    │   ├── analytics/
    │   │   ├── __init__.py
    │   │   ├── bsm_fallback.py
    │   │   ├── walls.py
    │   │   ├── anomalies.py
    │   │   ├── term_greeks.py
    │   │   ├── strategies/       # models, chain, builders (wall presets), engine (evaluate)
    │   │   └── ledger/           # models, classifier, rolls (FIFO + roll chains), engine
    │   ├── services/
    │   │   ├── __init__.py
    │   │   ├── analyzer.py       # AnalyzerService: refresh, chain analytics, Strategy Lab, corridor
    │   │   ├── corridor.py       # WallAnchors / BSM GreeksFallback glue, live-quote overlay cache
    │   │   ├── ledger.py         # LedgerService: Flex/Gateway sync, XML/CSV import, MTM overlay
    │   │   └── schemas.py        # request/response models (public API contract)
    │   └── web/
    │       ├── __init__.py
    │       ├── api.py            # thin FastAPI routes + SSE
    │       ├── errors.py         # domain exception -> JSON mapping
    │       ├── templates/
    │       │   ├── base.html
    │       │   └── index.html
    │       └── static/
    │           ├── css/          # tokens, layout, controls, charts, corridor, strategy_lab,
    │           │                 # ledger, chain_table, responsive (cascade order = base.html)
    │           └── js/dashboard/ # native ES modules: main.js entry + state, helpers, badges,
    │                             # charts, panels, chain_table, snapshots, strategy_lab,
    │                             # corridor, ledger, network, events, ai_prompt, grid_layout
    └── tests/
        ├── conftest.py          # fixtures_dir, fixture_repo
        ├── support/             # aio.py (run_coro, run_virtual_loop, noop_sleep), ib_fakes.py
        ├── fixtures/snapshots/  # golden Mode 1 CSV archives
        ├── unit/
        └── integration/

---

## 3. Core Component Responsibilities

### 3.0 Layering & Service Layer
`src/web/` is a thin HTTP adapter over `src/services/AnalyzerService` (and its composed `LedgerService` in `src/services/ledger.py`), which is the only layer composing broker, storage, and analytics (full dependency rules in `docs/CODING_STANDARDS.md` §1). The broker never imports analytics: `AnalyzerService` derives `WallAnchors` from `compute_walls()` and injects the vectorized BSM `GreeksFallback` into `CorridorQuoteService`. Services raise domain exceptions only; `src/web/errors.py` maps them to structured JSON (`InvalidRequestError` -> `422 INVALID_REQUEST`, `ConfigurationError` -> `422 CONFIGURATION_ERROR`, `FlexServiceError` -> `502 FLEX_SERVICE_ERROR`, echoed as `detail`).

### 3.1 FastAPI Lifespan & Shared Event Loop (`src/main.py`, `src/broker/client.py`)
- `ib-async` requires an active `asyncio` event loop. FastAPI's `lifespan` context manager initializes a singleton `IBKRBrokerClient` bound to the Uvicorn event loop.
- **Storage Layout Ownership:** The lifespan calls `CSVRepository.ensure_layout()` and `LedgerService.ensure_layout()` before loading cached indexes, so `data/snapshots/`, `data/latest/`, `data/metadata/`, and `data/ledger/{imports/}` are created by the application itself (the `Makefile` no longer pre-creates them; `data/logs/` is created by `configure_logging()`).
- **Offline-First Boot Policy:** If IBKR Gateway is closed at startup, the server still boots cleanly in **Offline Cache Mode**, serving data from `data/latest/` and `data/snapshots/`.
- When the user clicks **Refresh**, `IBKRBrokerClient.ensure_connected()` connects to `IBKR_HOST:IBKR_PORT` (default `127.0.0.1:4001` locally, `host.docker.internal:4001` in Docker) with a dedicated read-only `clientId` (default `77`).

### 3.2 Market Data Mode Architecture: Dynamic `DELAYED` / `LIVE` Mode & Auto-Reconciliation
Market data behavior (`LIVE` vs. `DELAYED`), primary code (`default_market_data_type`), and fallback code (`fallback_market_data_type`) are dynamically derived from `config/settings.yaml` and `.env` (`MARKET_DATA_MODE`, `DEFAULT_MARKET_DATA_TYPE`, `FALLBACK_MARKET_DATA_TYPE`) without hardcoding feeds:

| Config Mode (`MARKET_DATA_MODE`) | Primary IBKR Code | Auto-Fallback Code | Operational Behavior |
| :--- | :--- | :--- | :--- |
| **`DELAYED` (Default)** | `Type 3` (Delayed 15m) | `Type 4` (Delayed-Frozen) | Requests 15-minute delayed quotes from the **current trading session** (`reqMarketDataType(3)`). If queried outside regular trading hours and $>80\%$ of quotes return `-1`/`NaN`, automatically cascades to `Type 4` (`reqMarketDataType(4)`) so closing quotes populate. |
| **`LIVE` (OPRA)** | `Type 1` (Live OPRA) | `Type 2` (Frozen) | Requests real-time OPRA streaming ticks (`reqMarketDataType(1)` / configurable `2` for Frozen-first) and cascades to `Type 2` outside market hours when primary quotes are unpopulated. If IBKR rejects with `Error 10089`/`354`, automatically downgrades to `DELAYED` (`Type 3` -> `4`) and flags `live_subscription_missing=true`. |

- Setting only `DEFAULT_MARKET_DATA_TYPE` in `.env` automatically aligns `market_data_mode` (`1|2 -> LIVE`, `3|4 -> DELAYED`) and default `fallback_market_data_type`; setting only `MARKET_DATA_MODE=LIVE` aligns `(1, 2)`.

### 3.3 Option Chain Discovery, Strike Striding & Paged Fetching (`src/broker/contracts.py`, `src/broker/fetcher.py`)
Every manual refresh executes a bounded **5-Stage Pipeline** to stay well below IBKR Gateway's 100-line active market data limit and 50 msg/sec pacing ceiling:

1. **Stage 1 — Underlying Qualification & Spot Resolution:**
   - Validate ticker via `TickerValidator.validate(symbol)`.
   - Qualify `Stock(symbol, 'SMART', 'USD')` via `qualifyContractsAsync`.
   - Fetch underlying reference price (`last`, `mid`, or `close` under configured primary/fallback market data types, e.g. `Type 1/2` or `Type 3/4`). Abort with `UnderlyingPriceUnavailableError` if spot `<= 0`.
2. **Stage 2 — Chain Definition Lookup:**
   - Call `reqSecDefOptParamsAsync(underlying.symbol, '', underlying.secType, underlying.conId)` and select the primary `SMART` exchange definition with multiplier `'100'`.
3. **Stage 3 — Weekly Expiration Matching (`30 <= DTE <= 120`):**
   - Filter available expiration dates where calendar DTE from current market date satisfies strictly `30 <= dte <= 120` (`dte_min <= dte <= dte_max`).
   - Group remaining expirations by ISO calendar week `(year, week)` and select one expiration per week—preferring standard Friday (`weekday == 4`) over mid-week listings, otherwise choosing the latest trading day in that week (covering holiday Thursdays)—so every weekly expiry inside `[30, 120]` is included without target buckets.
4. **Stage 4 — Moneyness Window & Grid-Aligned Strike Step Striding:**
   - **Moneyness Filter:** Keep strikes within `[spot * (1 - range_pct), spot * (1 + range_pct)]` (default `range_pct = 0.20` for `+-20%`, adjustable in UI: `10%`, `15%`, `20%`, `25%`, `35%`, `50%`).
   - **Grid-Aligned Strike Step & Auto Step:** When `strike_step` is `None` (`Auto`, the default), `choose_auto_strike_step` picks the finest canonical step `(0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0)` yielding `<= min(28, max_strikes)` strikes (skipping fractional `$0.5`/`$2.5` steps unless native half-grid strikes exist). `_pool_for_step` prefers strikes aligned to multiples of `strike_step` when genuine grid coverage exists, preventing off-grid short-DTE `.50` / `$1` strikes from trapping the ATM anchor (UI options: `Auto` [default], `$1.00`, `$2.50`, `$5.00`, `$10.00`, `$25.00`).
   - **Hard Safety Cap:** Total option contracts (`Expirations * Strikes * 2`) cannot exceed `ContractDiscoveryService.max_total_contracts` = `options_chain.max_contracts_per_page * max_pages_per_refresh` (`80 * 5 = 400`). If a wide range + small step exceeds 400 contracts, the strider widens along canonical steps and returns an informational notice to the UI.
5. **Stage 5 — Paged Micro-Batch Downloader & Future OPRA Streaming Placeholder:**
   - Construct `Option` contracts (`Right='C'` and `Right='P'`) for the filtered grid and qualify them in chunks via `qualifyContractsAsync`.
   - Partition qualified contracts into pages of **`options_chain.max_contracts_per_page = 80`**.
   - **Execution Modes in `fetcher.py` (via unified `_collect_page_with_fallback`):**
     - **`DELAYED` Collector (`_fetch_page_delayed`):** Calls `reqMktData(contract, genericTickList="100,101,106", snapshot=False, regulatorySnapshot=False)` for the page of `<= 80` contracts, polls every `0.25s` up to `2.25s`, cascades to `fallback_market_data_type` (`4`) when `>80%` quotes remain empty, cancels subscriptions (`cancelMktData`) in `finally:`, and waits `0.35s` before the next page.
     - **`LIVE_OPRA` Collector (`_fetch_page_live_opra`):** Requests live OPRA generic ticks `"100,101,104,106"` under configured live primary/fallback (`1 -> 2`) and waits for both `modelGreeks` and `callOpenInterest`/`putOpenInterest` (tick `101`) confirmation; if `Error 10089`/`354` triggers during collection, seamlessly delegates to `_fetch_page_delayed`.

### 3.4 Hybrid Trade Ledger Acquisition (`src/broker/flex.py`, `src/broker/ledger_sync.py`, `src/services/ledger.py`)
Trade Ledger data is ingested from up to three complementary read-only channels (`POST /api/ledger/sync`, `POST /api/ledger/import`, `POST /api/ledger/mtm`):

1. **IBKR Flex Web Service v3 (`FlexWebServiceClient` in `src/broker/flex.py`):**
   - Performs non-blocking HTTPS `SendRequest` -> polled `GetStatement` calls (via `asyncio.to_thread` strictly against `https://ndcdyn.interactivebrokers.com/`, retrying transient `1018`/`1019` codes) using `IBKR_FLEX_TOKEN`.
   - `select_flex_query_ids(settings, mode, has_existing_history)` chooses between:
     - **Full 365-day Activity Flex Query** (`IBKR_FLEX_ACTIVITY_QUERY_ID` -> `FLEX_ACTIVITY_365`),
     - **Monthly Incremental Placeholder** (`IBKR_FLEX_MONTHLY_QUERY_ID` -> `FLEX_MONTHLY`), selected automatically in `mode="auto"` once `LedgerRepository.has_existing_history()` is `True` (and required explicitly when `mode="monthly"`, else raising `ConfigurationError`), and
     - **Optional Intraday Trade Confirmation Query** (`IBKR_FLEX_TRADE_CONF_QUERY_ID` -> `FLEX_TRADE_CONF`), appended when configured for same-day fills not yet settled in the daily Activity statement.
   - `parse_flex_xml()` and `parse_activity_csv()` also power manual file imports (`POST /api/ledger/import`, bounded `file.read(MAX_LEDGER_IMPORT_BYTES + 1)` + `finally: file.close()`, POSIX/`\` separator stripping, `.xml`/`.csv` check, NUL-byte/`StorageSchemaError` validation) supporting Flex XML (`Trade`, `TradeConfirm`, `OptionEAE`, `OpenPosition`), Activity Statement CSV, multi-section `BOF`/`HEADER`/`DATA` Flex CSV, and flat Flex CSV exports. Parsers infer omitted `assetCategory` (`OPT` via option attrs/symbol, `STK` while skipping FX pairs/`AFX` rows and preserving dotted stocks), extract `Ep`/`A`/`Ex` lifecycle codes from `notes`/`code` while suppressing duplicate `<OptionEAE>` rows, and recover missing `<OpenPosition>` quantities from `positionValue / (markPrice * multiplier)` (`docs/DATA_SCHEMA.md` §5.2–5.4); all IBKR tokens (`t=...`) and XML/quoted/unquoted account identifiers (`acctId`, `accountId`, `acctCode`, `account`, `ClientAccountID`) are stripped/redacted via `redact_flex_secrets()`.
2. **Opt-In Gateway Session & Open-Position Sync (`fetch_gateway_ledger` in `src/broker/ledger_sync.py`):**
   - Disabled by default (`ENABLE_GATEWAY_LEDGER_SYNC=false`, raising `ConfigurationError` if requested while disabled).
   - When explicitly enabled, uses `scripts/security_guard.py`'s `SCOPED_ALLOWLIST` permitting ONLY `reqExecutionsAsync` (`OPT`, plus optional `STK` via `GATEWAY_LEDGER_INCLUDE_STOCK`) and `reqPositionsAsync` inside `src/broker/ledger_sync.py`. Account attributes are discarded immediately; Gateway executions persist with `source="GATEWAY_LIVE"` and auto-upgrade in place when authoritative Flex statements arrive (`docs/DATA_SCHEMA.md` §5.3).
3. **Bounded Open-Position Live MTM (`refresh_open_option_marks`, `POST /api/ledger/mtm`):**
   - Explicit click-only snapshot poll capped at `MAX_MARK_CONTRACTS = 60` open option positions (reusing `conid` when present, yielding every 20 subscriptions, applying `1 -> 3 -> 4` / `LIVE 1->2` / `DELAYED 3->4` fallback, and isolating pacing-sleep `CancelledError` inside `finally:` so 100% of subscriptions call `cancelMktData`). Live quotes populate an in-memory mark overlay on `LedgerService` (`_live_marks`) without modifying `data/ledger/*.csv`.

---

## 4. Quantitative Analytics & BSM Fallback Engine (`src/analytics/`)

### 4.1 Vectorized Black-Scholes-Merton (BSM) Fallback (`src/analytics/bsm_fallback.py`)
Under IBKR **Type 3 (15-min Delayed)** data without an OPRA subscription, IBKR often populates delayed prices (`bid`, `ask`, `last`, `close`) while omitting `modelGreeks`.
- For every row where `modelGreeks` is missing/null and a valid option `mark_price` (`mid` -> fallback `last` -> fallback `close`) exceeds intrinsic value:
  1. Time to expiry: `T = max(dte, 0.5) / 365.0`.
  2. Implied Volatility `iv`: Solved via a vectorized 25-iteration hybrid Newton-Raphson + bisection solver on `[0.01, 5.00]` using `risk_free_rate = 0.0425` (from `config/settings.yaml`).
  3. Analytical Greeks: `delta`, `gamma`, `theta` (per calendar day), and `vega` (per 1% IV change) are computed from the solved `iv`.
  4. Provenance column `greeks_source` is set to `"BSM_FALLBACK"` (vs. `"IBKR_MODEL"` when native Greeks are returned).

### 4.2 Core Analytical Modules
1. **Put Walls & Call Walls (`src/analytics/walls.py`):**
   - Computes top Call Walls and Put Walls per weekly expiration (`by_dte`) and across the aggregate `[30, 120]` DTE window using both **Open Interest** and **Volume**.
   - If `open_interest` is null across the snapshot (common on unsubscribed Type 3 feeds), the module automatically promotes **Volume Walls** as primary and sets `primary_basis = "VOLUME"` on `ChainWallsReport`.
2. **Unusual Activity Scanner (`src/analytics/anomalies.py`):**
   - Flags `volume > open_interest` anomalies when `open_interest > 0`.
   - Computes intra-expiration Volume Z-Scores (`(volume - mean_vol) / std_vol`) so unusual strike activity is always surfaced even when Open Interest is null.
3. **Multi-DTE Comparative Structure (`src/analytics/term_greeks.py`):**
   - Aligns option chains across all weekly expirations in `[30, 120]` DTE over a unified strike and moneyness axis for Plotly overlays (IV Smile overlay, Delta/Gamma/Theta/Vega profile by DTE, Volume/OI concentration).

### 4.3 Trade Ledger Analytics & Roll-Chain Engine (`src/analytics/ledger/`)
Pure, deterministic quantitative P&L and campaign reconstruction over normalized `LedgerExecutionRow` and `LedgerPositionRow` data (`models.py`, `classifier.py`, `rolls.py`, `engine.py`):
- **Order/Time Leg Clustering & Strategy Classification (`classifier.py`):** `cluster_option_episodes()` groups same-underlying fills sharing non-empty `ib_order_id` (preserving multi-second partial fills on one order) plus unlinked fills within `<= 2.0s`, then merges adjacent chronological episodes within `<= 2.0s` that share the same uniform lifecycle kind (`TRADE` vs `EXPIRED`/`ASSIGNED`/`EXERCISED`) so simultaneous multi-leg fills with distinct `ib_order_id` values cluster into one spread/combo episode without mixing trades and lifecycle events. Classifies 17 strategy types (`BULL_PUT_SPREAD`, `BEAR_CALL_SPREAD`, `BULL_CALL_SPREAD`, `BEAR_PUT_SPREAD`, `IRON_CONDOR`, `IRON_BUTTERFLY`, `DIAGONAL_SPREAD`, `CALENDAR_SPREAD`, `STRANGLE`, `STRADDLE`, `COVERED_CALL`, `NAKED_CALL`, `SHORT_PUT`, `LONG_CALL`, `LONG_PUT`, `CUSTOM_COMBO`, `STOCK_LOT`), verifying uncovered stock inventory before labeling short calls as `COVERED_CALL` vs `NAKED_CALL`.
- **FIFO Lot Accounting & Roll Stitching (`rolls.py`):** Matches closing, expiring, assigned, and exercised option/stock fills (`TRADE`, `EXPIRED`, `ASSIGNED`, `EXERCISED`) via FIFO lots (sorting same-second opens before closes) without fabricating opening lots for pre-window orphan closes; orphan closes classify against their implied opening orientation (`_invert_legs_for_classification`) while keeping actual `BTC`/`STC`/`Expired`/`Assigned`/`Exercised` `RollStep` labels. Stitches both simultaneous close+open rolls and sequential rolls within `LEDGER_ROLL_MAX_GAP_HOURS` (default `24.0h`) into multi-step roll campaigns (`RollStep`, `LedgerCampaignRow`, e.g. `Roll Up & Out`, `Roll Down & Out`), and links assignment-driven stock realizations (`_link_stock_realizations_to_assignments`) when `include_stock_gain=True`.
- **Strict View & Fee Separation (`engine.py`):** Builds `realized` view (`1W|1M|3M|YTD|1Y|ALL` window over closed/rolled realized ex-fee P&L, net option premium, sidecar `fees`, and cumulative P&L curve across `campaigns | strategy | ticker | executions` groupings) cleanly separated from `open_mtm` view (open option/stock lots plus unmatched `open_positions.csv` rows priced via `LIVE_GATEWAY` live mark overlay -> `FLEX_MARK` -> `NONE` unrealized MTM).

---

## 5. Resilience & Error Handling Matrix

| Failure Scenario | Detection Mechanism | System Recovery & UI Behavior |
| :--- | :--- | :--- |
| **IBKR Gateway Closed (`ConnectionRefusedError`)** | Caught in `IBKRBrokerClient.ensure_connected()` within `3.0s` timeout. | Returns status `GATEWAY_OFFLINE` reporting dynamic `{IBKR_HOST}:{IBKR_PORT}`. UI loads `data/latest/{TICKER}_latest.csv` with dynamic status badge `OFFLINE CACHE • PORT {IBKR_PORT}`. |
| **Missing Live Subscription (`Error 10089 / 354`)** | Intercepted in `ib.errorEvent` callback (`_on_error`). | Marks `live_subscription_missing=True`, safely downgrades `reqMarketDataType(3)` (even during page/corridor fetches), emits fallback notice, and continues delayed collection without crashing. |
| **Outside Market Hours / Unpopulated Primary Ticks** | Page/corridor collector detects `>80%` (chain) or `100%` (corridor) of contracts have empty `bid`/`ask`/`last`. | Automatically switches to configured `fallback_market_data_type` (`4` in `DELAYED`, `2` in `LIVE`) and re-polls so frozen closing prices populate. |
| **Invalid Custom Ticker** | Caught in `TickerValidator` or empty `qualifyContractsAsync` result. | Rejects request with HTTP `422 Unprocessable Entity` and descriptive error banner before touching option chains or disk. |
| **API Pacing Warning (`Error 100 / 162`)** | Intercepted in `ib.errorEvent`. | Paged fetcher increases inter-page cooldown (`0.35s -> 1.0s`) and retries the page once. |
| **Mid-Batch Gateway Drop / HTTP Timeout / Post-Refresh Render Fault** | `IBKRBrokerClient.run_stage` / `handle_stage_error` catches socket drops (`OSError`, `ConnectionError`, `DISCONNECT_CODES`: `502/504/1100/1300/2110`) during discovery/collection; frontend `apiJson` enforces `timeoutMs: 120000` (`AbortController`) + captures non-JSON `textSnippet`; `refreshFromIBKR` separates Stage 1 HTTP pull from Stage 2 UI render. | Socket drops surface structured `503 GatewayConnectionError` with stage context (while always running `cancelMktData` cleanup). Stage 2 chart/DOM warnings preserve the archived CSV and display `Snapshot saved, UI render warning` (`UI_RENDER`) in a dismissible `.is-fresh` alert banner instead of blaming gateway/network connectivity. |
| **Flex Web Service Failure / Malformed Upload** | `FlexWebServiceClient` catches HTTP/network errors (`http.client.HTTPException`, `URLError`, retries `1018`/`1019`); `parse_flex_xml`/`parse_activity_csv` reject NUL bytes or `(ValueError, csv.Error)` payloads; `api_ledger_import` enforces `MAX_LEDGER_IMPORT_BYTES` and `.xml`/`.csv`. | Flex errors raise `FlexServiceError` (`502 FLEX_SERVICE_ERROR`); malformed uploads raise `StorageSchemaError` (`422 STORAGE_SCHEMA_ERROR`) or `InvalidRequestError` (`422`), with `redact_flex_secrets()` stripping tokens and account IDs; existing `data/ledger/` archives stay intact offline. |
| **Unconfigured Flex / Monthly / Gateway Sync** | `LedgerService.sync_ledger` or `select_flex_query_ids` detects missing token/query ID or `ENABLE_GATEWAY_LEDGER_SYNC=false`. | Raises `ConfigurationError` (`422 CONFIGURATION_ERROR`) surfaced in `#alert-banner`; `GET /api/ledger` still serves persisted local executions and open positions offline. |


---

## 6. Implementation Notes
Per-module implementation details (class/function inventories, helper names, cache-buster versions) live in `docs/reference/IMPLEMENTATION_NOTES.md` (§1 core, §2 storage, §3 broker, §4 analytics, §5 web incl. §5.1 UI element inventory). This file only holds architectural contracts.
