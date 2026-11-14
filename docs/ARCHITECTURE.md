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
    │   ├── tracked/              # immutable paper-trade leg quotes (outside snapshots/)
    │   ├── latest/
    │   ├── metadata/             # snapshot_index.json, paper_trades.json, paper_quotes.jsonl
    │   └── ledger/               # executions.csv, open_positions.csv, sync_state.json, imports/
    ├── src/
    │   ├── __init__.py
    │   ├── main.py
    │   ├── core/
    │   │   ├── __init__.py
    │   │   ├── calendar.py       # 2026-2027 NYSE trading calendar & 15m data delay cutoff
    │   │   ├── config.py
    │   │   ├── exceptions.py
    │   │   └── security.py
    │   ├── broker/
    │   │   ├── __init__.py
    │   │   ├── client.py
    │   │   ├── contracts.py
    │   │   ├── corridor/         # <= 90 Wall Corridor (88 opts + 1 spot, 10-line buffer < 100)
    │   │   ├── fetcher.py        # paged 80/page + fetch_exact_contracts, 20-burst 0.55s pacing
    │   │   ├── flex.py           # Flex Web Service v3 client & XML/CSV statement parser
    │   │   ├── ledger_sync.py    # opt-in Gateway execution/position sync & <= 60 MTM marks
    │   │   └── parser.py
    │   ├── storage/
    │   │   ├── __init__.py
    │   │   ├── schema.py
    │   │   ├── csv_repository.py
    │   │   ├── quality.py            # SpotSource & quote coverage snapshot usability scoring
    │   │   ├── paper_trade_store.py  # atomic paper_trades.json, paper_quotes.jsonl & data/tracked/
    │   │   ├── backtest_store.py     # O(window) daily bar selection & tracked fallback merge
    │   │   ├── ledger_schema.py      # 23-col execution & 15-col open position contracts
    │   │   └── ledger_repository.py  # atomic data/ledger/ archive & Gateway -> Flex upgrade
    │   ├── analytics/
    │   │   ├── __init__.py
    │   │   ├── bsm_fallback.py
    │   │   ├── walls.py
    │   │   ├── anomalies.py
    │   │   ├── term_greeks.py
    │   │   ├── strategies/       # models, chain, builders (wall presets), engine (evaluate)
    │   │   ├── ledger/           # models, classifier, rolls (FIFO + roll chains), engine
    │   │   └── backtest/         # models, context, pricing, rules, metrics, engine
    │   ├── services/
    │   │   ├── __init__.py
    │   │   ├── analyzer.py       # AnalyzerService: refresh_lock, refresh, analytics, Lab, corridor
    │   │   ├── corridor.py       # WallAnchors / BSM GreeksFallback glue, live-quote overlay cache
    │   │   ├── ledger.py         # LedgerService: Flex/Gateway sync, XML/CSV import, MTM overlay
    │   │   ├── paper_trades.py   # PaperTradeService: register/stop, post-refresh leg quoting
    │   │   ├── backtest.py       # daily-bar entries & systematic/single-trade orchestration
    │   │   └── schemas.py        # request/response models (public API contract)
    │   └── web/
    │       ├── __init__.py
    │       ├── api.py            # thin FastAPI routes + SSE
    │       ├── backtest_api.py   # /api/backtest/* & paper-trades routes
    │       ├── errors.py         # domain exception -> JSON mapping
    │       ├── templates/
    │       │   ├── base.html
    │       │   └── index.html
    │       └── static/
    │           ├── css/          # tokens, layout, controls, charts, corridor, strategy_lab,
    │           │                 # ledger, snapshot_picker, backtest, chain_table, responsive
    │           └── js/dashboard/ # 19 native ES modules: main.js entry + state, helpers, badges,
    │                             # charts, panels, chain_table, snapshots, strategy_lab,
    │                             # corridor, ledger, backtest, backtest_charts, backtest_paper,
    │                             # backtest_tables, network, events, ai_prompt, grid_layout
    └── tests/
        ├── conftest.py          # fixtures_dir, fixture_repo
        ├── support/             # aio.py (run_coro, run_virtual_loop, noop_sleep), ib_fakes.py
        ├── fixtures/snapshots/  # golden Mode 1 CSV archives
        ├── unit/
        └── integration/

---

## 3. Core Component Responsibilities

### 3.0 Layering & Service Layer
`src/web/` (`api.py` and `backtest_api.py`) is a thin HTTP adapter over `src/services/AnalyzerService` and its composed services (`LedgerService`, `PaperTradeService`, and `services/backtest.py`), which is the only layer composing broker, storage, and analytics (full dependency rules in `docs/CODING_STANDARDS.md` §1). The broker never imports analytics: `AnalyzerService` derives `WallAnchors` from `compute_walls()` and injects the vectorized BSM `GreeksFallback` into `CorridorQuoteService`.

- **Single-Flight IBKR Concurrency & Line-Budget Coordination:** Because IBKR enforces a 100-simultaneous market-data-line limit, `AnalyzerService.refresh_lock` (`asyncio.Lock`) serializes all IBKR market-data pulls across `refresh_chain`, `fetch_corridor_quotes`, Gateway-backed `LedgerService.sync_ledger`, and `LedgerService.refresh_open_mtm` (coordinated via injected `acquire_refresh`, `release_refresh`, and `corridor_stopper` callbacks). Every acquired pull calls `stop_all_streams()` to tear down active Corridor streams before subscribing new chain, ledger MTM, or paper-trade follow-up lines; overlapping IBKR calls immediately raise `RefreshBusyError` (`409 REFRESH_BUSY`, `Retry-After: 10`), and `open_corridor_stream` rejects new SSE streams while `refresh_lock` is held. `PaperTradeService.quote_active_trades_for_refresh` runs as a bounded post-refresh follow-up inside `refresh_chain` (zero background polling daemon).
- **Structured Error Mapping:** Services raise domain exceptions only; `src/web/errors.py` maps them to JSON (`RefreshBusyError` -> `409 REFRESH_BUSY`, `DuplicatePaperTradeError` -> `409 DUPLICATE_PAPER_TRADE`, `PaperTradeCapError` -> `409 PAPER_TRADE_CAP`, `PaperTradeStateError` -> `409 PAPER_TRADE_STATE`, `InvalidRequestError`/`BacktestValidationError` -> `422 INVALID_REQUEST`/`422 BACKTEST_VALIDATION_ERROR`, `ConfigurationError` -> `422 CONFIGURATION_ERROR`, `FlexServiceError` -> `502 FLEX_SERVICE_ERROR`, echoed as `detail`).

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
   - Fetch underlying reference price via `select_spot` (`last` cross-checked against `mid` within `SPOT_LAST_MID_MAX_DEVIATION = 0.02`, else `mid`, else `close`) under configured primary/fallback market data types (`Type 1/2` or `Type 3/4`), recording `SpotSource` (`LAST` | `MID` | `CLOSE`). Abort with `UnderlyingPriceUnavailableError` if spot `<= 0`.
2. **Stage 2 — Chain Definition Lookup:**
   - Call `reqSecDefOptParamsAsync(underlying.symbol, '', underlying.secType, underlying.conId)` and select the primary `SMART` exchange definition with multiplier `'100'`.
3. **Stage 3 — Weekly Expiration Matching (`dte_min <= DTE <= dte_max`):**
   - Filter available expiration dates where calendar DTE from current market date satisfies strictly `dte_min <= dte <= dte_max` (primary `[30, 120]`, plus optional sequential Pass 2 `[short_dte_min, min(short_dte_max, eff_dte_min - 1)]`, default `[0, 29]`, when `include_short_dte_for_backtest: true` / `include_short_dte=true`).
   - Group remaining expirations by ISO calendar week `(year, week)` and select one expiration per week—preferring standard Friday (`weekday == 4`) over mid-week listings, otherwise choosing the latest trading day in that week (covering holiday Thursdays)—so every weekly expiry inside the active window is included without target buckets.
4. **Stage 4 — Moneyness Window & Grid-Aligned Strike Step Striding:**
   - **Moneyness Filter:** Keep strikes within `[spot * (1 - range_pct), spot * (1 + range_pct)]` (default `range_pct = 0.20` for `+-20%`, adjustable in UI: `10%`, `15%`, `20%`, `25%`, `35%`, `50%`).
   - **Grid-Aligned Strike Step & Auto Step:** When `strike_step` is `None` (`Auto`, the default), `choose_auto_strike_step` picks the finest canonical step `(0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0)` yielding `<= min(28, max_strikes)` strikes (skipping fractional `$0.5`/`$2.5` steps unless native half-grid strikes exist). `_pool_for_step` prefers strikes aligned to multiples of `strike_step` when genuine grid coverage exists, preventing off-grid short-DTE `.50` / `$1` strikes from trapping the ATM anchor (UI options: `Auto` [default], `$1.00`, `$2.50`, `$5.00`, `$10.00`, `$25.00`).
   - **Hard Safety Cap:** Total option contracts (`Expirations * Strikes * 2`) cannot exceed `ContractDiscoveryService.max_total_contracts` = `options_chain.max_contracts_per_page * max_pages_per_refresh` (`80 * 5 = 400`) per discovery pass. If a wide range + small step exceeds 400 contracts, the strider widens along canonical steps and returns an informational notice to the UI.
5. **Stage 5 — Paged Micro-Batch Downloader & Sequential Short-DTE Pass:**
   - Construct `Option` contracts (`Right='C'` and `Right='P'`) for the filtered grid and qualify them in chunks via `qualifyContractsAsync`.
   - Partition qualified contracts into pages of **`options_chain.max_contracts_per_page = 80`**.
   - **Two-Pass Sequential Short-DTE Collection (`fetcher.py`):** When `include_short_dte=True` and `pass2_min <= pass2_max`, `PagedOptionChainFetcher.fetch_chain()` runs Pass 1 (`[eff_dte_min, eff_dte_max]`) and completes 100% of its `cancelMktData` cleanup inside `_collect_page_once`'s `finally:` before running Pass 2 (`[pass2_min, pass2_max]`, reusing `plan.underlying`, `plan.smart_chains`, and `plan.market_date`, gracefully skipping on `OptionChainFetchError` if no short-DTE weeklies exist) and deduplicating merged rows via `_merge_and_dedupe_rows` into the same Mode 1 CSV (`FetchResult` exposes `primary_contract_count`, `short_dte_contract_count`, `short_dte_range`). When `AnalyzerService` serves `GET /api/chain/{ticker}` or `POST /api/refresh`, `_isolate_dashboard_dte()` keeps default dashboard analytics on `[30, 120]` unless the user explicitly selects `0–29d (Short)` or `0–120d (All)`.
   - **Execution Modes & Burst Pacing in `fetcher.py` (via unified `_collect_page_with_fallback`):**
     - **Burst Pacing & Cleanup:** Both paged pulls and exact-contract pulls pace IBKR API bursts every `PAGE_PACING_CHUNK_SIZE = 20` calls with `PAGE_PACING_PAUSE_SEC = 0.55` before subscribing, and cancel every subscribed ticker in `finally: await self._cancel_paced(...)` (which paces cancels at 20/`0.55s` and defers `BaseException`/`CancelledError` until 100% cancels complete).
     - **`DELAYED` Collector (`_fetch_page_delayed`):** Calls `reqMktData(contract, genericTickList="100,101,106", snapshot=False, regulatorySnapshot=False)` for the page of `<= 80` contracts, polls every `0.25s` up to `2.25s`, cascades to `fallback_market_data_type` (`4`) when `>80%` quotes remain empty, cleans up via `_cancel_paced`, and waits inter-page cooldown (`0.35s`, widened to `1.0s` on pacing warnings) before the next page.
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

### 3.5 Daily Bar Selection & Manual Paper-Trade Follow-Up (`src/storage/backtest_store.py`, `src/services/paper_trades.py`)
1. **O(window) Daily Bar Selection (`backtest_store.py`, `quality.py`):**
   - `select_daily_bar_snapshots()` pre-filters archived filenames in `O(window)` by `[start_date, end_date]` ET dates before parsing metadata or reading CSV files, then selects one canonical snapshot per NYSE trading day (`src/core/calendar.py`): preferring the latest usable snapshot per `quality.is_usable_snapshot()` (`quote_coverage >= 0.90`, `spot_source in {"LAST", "MID"}`, `market_data_type in {1, 3}`), otherwise falling back to the day's latest snapshot tagged `NO_USABLE_DAILY_SNAPSHOT`.
   - `load_daily_chain_bars()` loads each canonical CSV and pairs it with any same-ET-date tracked leg CSV under `data/tracked/{TICKER}/` (`load_tracked_quotes_by_date()`).
2. **Exact-Contract Follow-Up (`fetch_exact_contracts`, `PaperTradeService.quote_active_trades_for_refresh`):**
   - Strictly event-free outside manual refreshes (zero background collector daemon): inside `AnalyzerService.refresh_chain()` under `refresh_lock`, after paged option collection finishes and disconnects subscriptions cleanly, `PaperTradeService` gathers all unique non-expired legs (`expiry >= market_day`) across `ACTIVE` `PaperTrade` entries for the refreshed ticker.
   - `PagedOptionChainFetcher.fetch_exact_contracts()` filters expired legs into `missing`, deduplicates specs `(expiry, round(strike, 4), right)`, enforces `<= max_contracts_per_page * max_pages_per_refresh` (`400`), reuses known `conId` / qualifies unknown contracts, collects quotes using `fetch_pages()`, applies `enrich_missing_greeks()`, writes an immutable 27-col CSV under `data/tracked/{TICKER}/{TICKER}_{YYYYMMDD_HHMMSS}.csv`, and appends a `PaperQuoteRecord` (`QUOTED` or `ERROR`) to `data/metadata/paper_quotes.jsonl` without failing the primary chain refresh if a leg quote fails.

### 3.6 Live Wall Strike Corridor & Subscription Pacing (`src/broker/corridor/`, `src/services/corridor.py`)
1. **Window & Subscription Ceiling (`CorridorSettings`, `src/broker/corridor/models.py`):**
   - Defaults to a `±10` strike corridor (`DEFAULT_WINDOW_STRIKES = 10`, UI/API `window_strikes` in `1..15`) around the Primary Put Wall and Primary Call Wall, bounded by `DEFAULT_CORRIDOR_MAX_CONTRACTS = 88` option contracts plus `1` underlying spot line (`89 <= CORRIDOR_MAX_CONTRACTS = 90`), preserving a 10-line safety buffer below IBKR's 100-line ceiling (`max_contracts` capped by `min(settings.corridor.hard_max_contracts, CORRIDOR_MAX_CONTRACTS)` in `AnalyzerService.open_corridor_stream` / `fetch_corridor_quotes`, raising `PacingViolationError` above `90`).
2. **Deterministic Tier Selection (`select_wall_corridor`, `src/broker/corridor/selection.py`):**
   - `select_wall_corridor()` enforces `eff_win = min(max(1, int(window_strikes)), 15)` and `include_opposite_right = True`, prioritizing: Tier `0` active Strategy Lab legs (including off-expiry diagonals), Tier `1` Primary Wall-side contracts (`±window_strikes`), Tier `2` opposite-right Call+Put pairs at the Wall strikes (plus secondary expiry `±min(2, window_strikes)` wall windows when space remains), and Tier `3` nearest ATM strikes.
3. **20-Call Burst Pacing & Cleanup (`CorridorQuoteService`, `src/broker/corridor/service.py`):**
   - `_subscribe_corridor()` paces `reqMktData` submissions using `PAGE_PACING_CHUNK_SIZE = 20` and `PAGE_PACING_PAUSE_SEC = 0.55` (imported from `src/broker/fetcher.py`) and reuses CSV/cache `conId`s (`_prepare_contracts()`) without redundant `qualifyContractsAsync` calls.
   - `_poll_initial_corridor()` handles `DELAYED` (`3 -> 4`), `LIVE` (`1 -> 2`), and `10089/354` live-to-delayed fallback, enriches missing delayed `modelGreeks` via injected BSM `GreeksFallback`, and guarantees 100% `cancelMktData` cleanup in `finally:`.
   - Paired UI (`#wall-corridor-card`, title `"Wall Strike Corridor"`) displays an 11-column `CALLS | STRIKE | PUTS` T-Chain table (`#corridor-table` / `#corridor-tbody`) side-by-side with the Plotly Bid-Ask Ladder (`#chart-corridor-ladder`).

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

### 4.4 Daily-Bar Options Backtest Engine (`src/analytics/backtest/`, `src/core/calendar.py`, `src/web/backtest_api.py`)
Pure, clock-free simulation over canonical daily chain + tracked leg bars (`models.py`, `context.py`, `pricing.py`, `rules.py`, `metrics.py`, `engine.py`), wired by `src/services/backtest.py` and exposed on `/api/backtest/*` via `asyncio.to_thread`:

- **NYSE Trading Calendar & Settlement Cutoff (`src/core/calendar.py`, `context.py`):** Covers `2026-01-01`..`2027-12-31` (`NYSE_HOLIDAYS`, `NYSE_EARLY_CLOSES`, `SESSION_OPEN_ET = 09:30`, `SESSION_CLOSE_ET = 16:00`, `EARLY_CLOSE_ET = 13:00`, `DATA_DELAY = 15 min`). `expiry_settlement_cutoff_utc()` requires `bar.snapshot_ts_utc >= close_utc + 15 min` on expiration day before settling expiring legs via intrinsic payoff (`$0` closing commission on expired legs); earlier intraday snapshots evaluate remaining intraday prices.
- **Pricing, Contract-Filtered BSM & Tracked Fallback (`pricing.py`, `engine.py`):** `enrich_filtered_greeks()` executes `BSMFallbackEngine` strictly on contract-filtered rows rather than re-solving the entire archive. Leg lookup (`lookup_legs_on_bar`) queries the canonical daily chain bar first and falls back to `bar.tracked_df` (`data/tracked/`) when a leg is absent or lacks usable quotes (e.g., tracked `< 30 DTE` paper-trade legs). Supports `PriceBasis` (`AUTO_MID_LAST_CLOSE` | `LAST_CLOSE_ONLY`), `ClosePriceMode` (`MID` | `NATURAL_SPREAD`), and `TpSlBasis` (`CREDIT_DEBIT_PCT` | `MAX_RISK_PCT`). Each `TradeDailyStep` records `mark_source: StepMarkSource` (`_derive_step_mark_source()`) and `quote_missing: bool`; when quotes are missing on an open step, MTM is omitted (`net_pnl_dollars = None`) and the equity curve carries forward prior unrealized P&L rather than clamping to zero.
- **Modes, Cadences & Exit Priority (`rules.py`, `metrics.py`, `engine.py`):**
  - `SYSTEMATIC`: enters `AUTO_BUILD` wall-anchored strategies (`TARGET_DTE_CENTER = 38` when inside `[target_dte_min, target_dte_max]`, else midpoint `0.5 * (target_dte_min + target_dte_max)` across `BacktestConfig.target_dte_min/max` `ge=0, le=120`, supporting `[0, 29]` / 0-DTE backtests) on `WEEKLY_DAY` (retrying subsequent trading days of the same ISO week via `bar_day >= target` when an entry is skipped, advancing cadence only on accepted entry), `EVERY_N_DAYS`, or `WHEN_FLAT` cadences constrained by `max_open_trades` and `initial_capital`. Auto-built `BULL_PUT_SPREAD` entries apply `BacktestConfig` per-1-spread quality filters (`min_entry_credit_dollars = 100.0`, `min_entry_ror_pct = None`, disabled when `0.0` or `None`) evaluated under `config.price_basis` and re-verified on resolved entry fills, tallying rejections in `PortfolioSummary.skipped_entries_count` / `skipped_by_reason`.
  - `SINGLE_TRADE`: replays one `AUTO_BUILD` preset, manual `LEGS`, or saved `PAPER_TRADE` forward from its entry snapshot/bar (`_ensure_snapshot_bar` injects the exact requested entry snapshot when needed). Strategy Lab (`strategies/engine.py`) separates 0-DTE `Today (T+0)` / `Simulated` intraday BSM curves (`0.5d` floor) from `At Expiration` intrinsic payoff curves (`at_expiration=True`).
  - Evaluates bar `i` exits strictly before bar `i` entries in deterministic priority: `EXPIRY_SETTLED` -> missing-price hold -> `EXIT_DTE` -> `PROFIT_TARGET` -> `STOP_LOSS` (via `resolve_tp_sl_thresholds()`), recording unclosed positions at window end (`END_OF_DATA`), attaching `profit_target_dollars`, `stop_loss_dollars`, and `selection_notes` to `BacktestTradeRecord`, splitting `realized_pnl_dollars` and `unrealized_pnl_dollars` in `PortfolioSummary`, and surfacing `BACKTEST_ASSUMPTIONS` on every response.
- **Endpoints (`src/web/backtest_api.py`):** `GET /api/backtest/entries/{ticker}`, `POST /api/backtest/run`, `GET /api/backtest/paper-trades`, `POST /api/backtest/paper-trades`, `POST /api/backtest/paper-trades/{trade_id}/stop` (zero IBKR/network side effects).

---

## 5. Resilience & Error Handling Matrix

| Failure Scenario | Detection Mechanism | System Recovery & UI Behavior |
| :--- | :--- | :--- |
| **Overlapping IBKR Request (`Refresh` / `Corridor` / `Ledger MTM`)** | `AnalyzerService.refresh_lock` detects an in-flight `refresh_chain`, `fetch_corridor_quotes`, Gateway `LedgerService.sync_ledger`, `refresh_open_mtm`, or active lock during SSE open. | Raises `RefreshBusyError` (HTTP `409 REFRESH_BUSY`, `Retry-After: 10`), protecting IBKR's 100-line session cap; `#alert-banner` surfaces the active owner so the user retries once finished. |
| **IBKR Gateway Closed (`ConnectionRefusedError`)** | Caught in `IBKRBrokerClient.ensure_connected()` within `3.0s` timeout. | Returns status `GATEWAY_OFFLINE` reporting dynamic `{IBKR_HOST}:{IBKR_PORT}`. UI loads `data/latest/{TICKER}_latest.csv` with dynamic status badge `OFFLINE CACHE • PORT {IBKR_PORT}`. |
| **Missing Live Subscription (`Error 10089 / 354`)** | Intercepted in `ib.errorEvent` callback (`_on_error`). | Marks `live_subscription_missing=True`, safely downgrades `reqMarketDataType(3)` (even during page/corridor fetches), emits fallback notice, and continues delayed collection without crashing. |
| **Outside Market Hours / Unpopulated Primary Ticks** | Page/corridor collector detects `>80%` (chain) or `100%` (corridor) of contracts have empty `bid`/`ask`/`last`. | Automatically switches to configured `fallback_market_data_type` (`4` in `DELAYED`, `2` in `LIVE`) and re-polls so frozen closing prices populate. |
| **Invalid Custom Ticker** | Caught in `TickerValidator` or empty `qualifyContractsAsync` result. | Rejects request with HTTP `422 Unprocessable Entity` and descriptive error banner before touching option chains or disk. |
| **API Pacing Warning (`Error 100 / 162`)** | Intercepted in `ib.errorEvent`. | Paged fetcher increases inter-page cooldown (`0.35s -> 1.0s`) and retries the page once. |
| **Mid-Batch Gateway Drop / HTTP Timeout / Post-Refresh Render Fault** | `IBKRBrokerClient.run_stage` / `handle_stage_error` catches socket drops (`OSError`, `ConnectionError`, `DISCONNECT_CODES`: `502/504/1100/1300/2110`) during discovery/collection; frontend `refreshFromIBKR` (`apiJson`) enforces `timeoutMs: 150000` (`AbortController`) + captures non-JSON `textSnippet`; `refreshFromIBKR` separates Stage 1 HTTP pull from Stage 2 UI render. | Socket drops surface structured `503 GatewayConnectionError` with stage context (while always running `cancelMktData` cleanup). Stage 2 chart/DOM warnings preserve the archived CSV and display `Snapshot saved, UI render warning` (`UI_RENDER`) in a dismissible `.is-fresh` alert banner instead of blaming gateway/network connectivity. |
| **Flex Web Service Failure / Malformed Upload** | `FlexWebServiceClient` catches HTTP/network errors (`http.client.HTTPException`, `URLError`, retries `1018`/`1019`); `parse_flex_xml`/`parse_activity_csv` reject NUL bytes or `(ValueError, csv.Error)` payloads; `api_ledger_import` enforces `MAX_LEDGER_IMPORT_BYTES` and `.xml`/`.csv`. | Flex errors raise `FlexServiceError` (`502 FLEX_SERVICE_ERROR`); malformed uploads raise `StorageSchemaError` (`422 STORAGE_SCHEMA_ERROR`) or `InvalidRequestError` (`422`), with `redact_flex_secrets()` stripping tokens and account IDs; existing `data/ledger/` archives stay intact offline. |
| **Unconfigured Flex / Monthly / Gateway Sync** | `LedgerService.sync_ledger` or `select_flex_query_ids` detects missing token/query ID or `ENABLE_GATEWAY_LEDGER_SYNC=false`. | Raises `ConfigurationError` (`422 CONFIGURATION_ERROR`) surfaced in `#alert-banner`; `GET /api/ledger` still serves persisted local executions and open positions offline. |


---

## 6. Implementation Notes
Per-module implementation details (class/function inventories, helper names, cache-buster versions) live in `docs/reference/IMPLEMENTATION_NOTES.md` (§1 core, §2 storage, §3 broker, §4 analytics, §5 web incl. §5.1 UI element inventory). This file only holds architectural contracts.
