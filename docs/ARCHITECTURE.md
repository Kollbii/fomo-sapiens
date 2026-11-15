# System Architecture Specification (`ARCHITECTURE.md`)

## 1. Architectural Overview
The **IBKR Options Analyzer & Dashboard** is a local, single-user, strictly read-only analytical web application. It decouples broker data acquisition (`ib-async`), local persistence (`Polars` CSV store), quantitative computation (`Polars` + `NumPy`/`SciPy`), and visualization (`FastAPI` + `Plotly.js`) so the UI remains 100% functional offline using cached CSV snapshots even when IBKR Gateway is closed.

    +-----------------------------------------------------------------------------------+
    |                        BROWSER UI (NYT Editorial + Terminal)                      |
    |  [Reserved Editorial Banner Slot]  [Collapsible Watchlist Panel + Add Box]  |
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
    │   │   ├── corridor/         # <= 90 Wall Corridor (88 opts + 1 spot, 10-line buffer < 100); lines.py = shared line primitives
    │   │   ├── live_legs.py      # LiveLegQuoteService: screener live leg stream (<= 88 opts, <= 90 lines)
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
    │   │   ├── watchlist_store.py    # atomic data/watchlist.json (lazy seed, cap, corrupt backup)
    │   │   ├── ledger_schema.py      # 23-col execution & 15-col open position contracts
    │   │   └── ledger_repository.py  # atomic data/ledger/ archive & Gateway -> Flex upgrade
    │   ├── analytics/
    │   │   ├── __init__.py
    │   │   ├── bsm_fallback.py
    │   │   ├── walls.py
    │   │   ├── anomalies.py
    │   │   ├── term_greeks.py
    │   │   ├── strategies/       # models, chain, builders (wall presets), engine (evaluate), repick (pure ±1-strike re-pick)
    │   │   ├── ledger/           # models, classifier, rolls (FIFO + roll chains), engine
    │   │   └── backtest/         # models, context, pricing, rules, metrics, engine
    │   ├── services/
    │   │   ├── __init__.py
    │   │   ├── analyzer.py       # AnalyzerService: refresh_lock, refresh, analytics, Lab, corridor
    │   │   ├── screener.py       # ScreenerService: cached cross-watchlist best-strategy screen (zero IBKR)
    │   │   ├── screener_live.py  # ScreenerLiveService: live leg stream, allocation & re-pick
    │   │   ├── corridor.py       # WallAnchors / BSM GreeksFallback glue, live-quote overlay cache
    │   │   ├── ledger.py         # LedgerService: Flex/Gateway sync, XML/CSV import, MTM overlay
    │   │   ├── paper_trades.py   # PaperTradeService: register/stop, ledger sync, post-refresh leg quoting
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
    │           │                 # ledger, snapshot_picker, backtest, screener, chain_table, responsive
    │           └── js/dashboard/ # 21 native ES modules: main.js entry + state, helpers, badges,
    │                             # charts, panels, chain_table, snapshots, strategy_lab,
    │                             # corridor, ledger, backtest, backtest_charts, backtest_paper,
    │                             # backtest_tables, network, events, ai_prompt, grid_layout, watchlist, screener
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
- **Watchlist & Batch Wiring:** the lifespan primes the watchlist cache (`await service.watchlist.get_watchlist()`) at startup and exposes `BatchRefreshService` as `app.state.batch_refresh` (dependency `get_batch_service`). Shutdown order is `batch.aclose()` -> `service.stop_corridor_streams()` -> `service.screener_live.stop_live_streams(reason="shutdown")` -> `broker_client.disconnect()` (the outer `finally` also calls `screener_live.stop_now(reason="shutdown")` right before the disconnect as a backstop). The index bootstrap payload takes the watchlist from the store (`cached_tickers()`, plus `watchlist_max_size`, `watchlist_allow_custom`).
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

- **Request Time Limits, Fast-Fail & Partial Capture (`src/broker/client.py`, `src/broker/fetcher.py`):** `IBKRBrokerClient.run_stage(stage, awaitable, *, timeout_sec=None)` (built on `await_guarded`) bounds every IBKR request by `ibkr.request_timeout_sec` (default `20.0`, `0 < x <= 120`) and raises `GatewayTimeoutError` (`GatewayConnectionError` subclass, message `IBKR Gateway did not answer during <stage> within <n> s`). A `DISCONNECT_CODES` notice (`502/504/1100/1300/2110`) fails pending stages immediately (`IBKR Gateway lost its upstream connection during <stage> (IBKR <code>: <msg>)`), while `RESTORE_CODES` (`1101/1102`) are only logged. `link_watch()` / `link_checkpoint()` / `link_lost_since()` / `ensure_link_alive()` let polling loops (spot resolution, page collection) abort on upstream loss; exact-contract prefetch is bounded by `contract_details_timeout_sec` (`5.0`) and propagates link loss instead of degrading to the `reqSecDefOptParams` fallback; Corridor qualification goes through `run_stage`. The local socket is not torn down on a timeout or fast-fail (real socket drops still disconnect). `fetch_pages(*, on_page)` and `fetch_option_chain(*, progress)` record finished pages in `ChainFetchProgress`; `PagedOptionChainFetcher.build_partial_result(progress)` returns `FetchResult.partial` rows (finished pages only) for callers that archive an interrupted pull.

### 3.4 Hybrid Trade Ledger Acquisition (`src/broker/flex.py`, `src/broker/ledger_sync.py`, `src/services/ledger.py`)
Trade Ledger data is ingested from up to three complementary read-only channels (`POST /api/ledger/sync`, `POST /api/ledger/import`, `POST /api/ledger/mtm`). The Gateway channel's `reqExecutionsAsync` / `reqPositionsAsync` calls run through `client.run_stage`, so they are bounded by `ibkr.request_timeout_sec` (`20 s`, `GatewayTimeoutError`) and fail fast on upstream loss (§3.3).

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
   - **Post-save quoting fault isolation:** In both single-ticker refresh (`AnalyzerService.refresh_chain()`) and batch refresh (`BatchRefreshService._run_one()`), paper-trade quoting occurs strictly *after* the complete chain snapshot is safely saved to disk. Quoting or quote-log write exceptions are caught defensively and surfaced as non-fatal notices; in batch execution, `run.saved` presence ensures the completed snapshot is marked `done` rather than `failed`, so chain snapshot completion is never reverted to failed.
3. **Trade Ledger -> Paper Trades Sync (`LedgerService`, `PaperTradeService`, `AnalyzerService`):**
   - `AnalyzerService.ensure_layout()` on startup (when `open_positions.csv` has rows), `LedgerService.sync_ledger()` / `import_ledger_file()` (via `on_ledger_changed` -> `AnalyzerService.sync_paper_trades_from_ledger()`), and manual `POST /api/backtest/paper-trades/sync-ledger` invoke `LedgerService.build_full_open_report()` + `PaperTradeService.sync_from_ledger_report()`.
   - Open `OPT` positions (`abs(quantity) > 1e-6`) are grouped by `campaign_id` (falling back to `position_key`; groups `> MAX_PAPER_TRADE_LEGS = 8` or non-option stock lots are skipped), scaled via `qty = gcd` (`qty_ratio = count_i // qty`), anchored with per-leg `entry_price = avg_cost_price` and `entry_net_cash = -sum(cost_basis_ex_fee)`, mapped via `map_ledger_strategy()`, and reconciled atomically in `PaperTradeStore.sync_ledger_trades()` — leaving `MANUAL` trades untouched and transitioning absent active `LEDGER` trades to `status="CLOSED"`.
   - On subsequent manual `POST /api/refresh` calls, `PaperTradeService.quote_active_trades_for_refresh()` quotes active `LEDGER` legs as well (qualifying `con_id = 0` legs via `ExactContractSpec` when needed).

### 3.6 Live Wall Strike Corridor & Subscription Pacing (`src/broker/corridor/`, `src/services/corridor.py`)
1. **Window & Subscription Ceiling (`CorridorSettings`, `src/broker/corridor/models.py`):**
   - Defaults to a `±10` strike corridor (`DEFAULT_WINDOW_STRIKES = 10`, UI/API `window_strikes` in `1..15`) around the Primary Put Wall and Primary Call Wall, bounded by `DEFAULT_CORRIDOR_MAX_CONTRACTS = 88` option contracts plus `1` underlying spot line (`89 <= CORRIDOR_MAX_CONTRACTS = 90`), preserving a 10-line safety buffer below IBKR's 100-line ceiling (`max_contracts` capped by `min(settings.corridor.hard_max_contracts, CORRIDOR_MAX_CONTRACTS)` in `AnalyzerService.open_corridor_stream` / `fetch_corridor_quotes`, raising `PacingViolationError` above `90`).
2. **Deterministic Tier Selection (`select_wall_corridor`, `src/broker/corridor/selection.py`):**
   - `select_wall_corridor()` enforces `eff_win = min(max(1, int(window_strikes)), 15)` and `include_opposite_right = True`, prioritizing: Tier `0` active Strategy Lab legs (including off-expiry diagonals), Tier `1` Primary Wall-side contracts (`±window_strikes`), Tier `2` opposite-right Call+Put pairs at the Wall strikes (plus secondary expiry `±min(2, window_strikes)` wall windows when space remains), and Tier `3` nearest ATM strikes.
3. **20-Call Burst Pacing & Cleanup (`CorridorQuoteService`, `src/broker/corridor/service.py`):**
   - Line primitives (`MarketDataSession`, `BurstPacer` / `pace_and_guard` at 20 calls per 0.55 s, `subscribe_lines`, `poll_initial_quotes`, `build_quote_rows`, `contract_from_spec`) live in `src/broker/corridor/lines.py` and are shared with `LiveLegQuoteService` (section 3.8); `CorridorQuoteService` behavior is unchanged and `stream_corridor_quotes` takes an optional `on_register` callback. Starting a Corridor or screener live feed stops the other (newer wins). Both SSE routes (Corridor and screener) return `_ClosingStreamingResponse` (`src/web/api.py`), which always closes the body iterator in a `finally`, so live lines are released even if the client disconnects while a send is pending.
   - `_subscribe_corridor()` paces `reqMktData` submissions using `PAGE_PACING_CHUNK_SIZE = 20` and `PAGE_PACING_PAUSE_SEC = 0.55` (imported from `src/broker/fetcher.py`) and reuses CSV/cache `conId`s (`_prepare_contracts()`) without redundant `qualifyContractsAsync` calls.
   - `_poll_initial_corridor()` handles `DELAYED` (`3 -> 4`), `LIVE` (`1 -> 2`), and `10089/354` live-to-delayed fallback, enriches missing delayed `modelGreeks` via injected BSM `GreeksFallback`, and guarantees 100% `cancelMktData` cleanup in `finally:`.
   - **Subscribe-time busy re-check:** `stream_corridor_quotes(..., is_busy=...)` subscribes lazily on first read, so `AnalyzerService.open_corridor_stream` injects `refresh_lock.locked` and the generator re-checks it at start and right after session registration. If a refresh or batch took the lock in between, it raises `CorridorBusyError` (`CORRIDOR_BUSY_MESSAGE`) with zero lines subscribed, surfacing as SSE `event: error` with `error_code` `REFRESH_BUSY` and `status` `409` (not `CORRIDOR_STREAM_ERROR`).
   - **SSE error events (`GET /api/corridor/stream/{ticker}`, `src/web/api.py`):** `GatewayTimeoutError` -> `event: error` `GATEWAY_TIMEOUT` (`503`, `describe_error()` wording); other `GatewayConnectionError` -> `GATEWAY_OFFLINE` (`503`); `CorridorBusyError` -> `REFRESH_BUSY` (`409`).
   - Paired UI (`#wall-corridor-card`, title `"Wall Strike Corridor"`) displays an 11-column `CALLS | STRIKE | PUTS` T-Chain table (`#corridor-table` / `#corridor-tbody`) side-by-side with the Plotly Bid-Ask Ladder (`#chart-corridor-ladder`).

---

### 3.7 Watchlist & Batch Refresh Orchestration (`src/services/watchlist.py`, `src/services/batch_refresh.py`, `src/services/error_messages.py`)
- **`WatchlistService(store, repo, *, clock, runner)` (`allow_custom` is a property derived from the store's validator):** async `get_watchlist()`, `add_ticker()`, `remove_ticker()` return `WatchlistResponse` (`tickers`, `entries`, `count`, `max_size`, `is_full`, `allow_custom`, `writable`, `notice`, `today_et`, `ticker`, `changed`). Store I/O runs through `runner` (default `asyncio.to_thread`); per-ticker freshness (`WatchlistEntry.latest_snapshot_id`, `latest_snapshot_utc`, `is_today_et`; "today" = ET calendar date of `clock`) comes from one `CSVRepository.latest_snapshots()` index read and never calls IBKR. `cached_tickers()` is the sync view behind `AnalyzerService.get_status().watchlist`. Domain errors (`InvalidTickerError`, `WatchlistCapError`, `StorageUnavailableError`) propagate unchanged.
- **`BatchRefreshService(analyzer, *, timer, id_factory, cancel_cleanup_sec, ticker_timeout_sec)`:** `start(BatchRefreshRequest)`, `get(id)`, `current()`, `cancel(id)`, `aclose()`; `max_tickers` = `watchlist.max_size`. One batch at a time; only the latest batch is kept, in memory (unknown id -> `BatchNotFoundError`; ids match `BATCH_ID_PATTERN`). Tickers run strictly sequentially through `AnalyzerService.refresh_ticker_locked()` (the per-ticker pipeline `refresh_chain` delegates to: new Mode 1 snapshot, `latest` update, paper-trade quoting) with `strike_step` forced to Auto.
- **Lock & cancel semantics:** `refresh_lock` is acquired before `start()` returns and held for the whole batch under owner `batch:<i>/<n>:<SYM>` (`batch_owner()`), so every other IBKR operation gets `RefreshBusyError`. Active Corridor streams are stopped once at start (if that fails, all tickers are marked failed). A failing ticker with an ordinary error records a sanitized `error_code` / `error` (`describe_error()` -> `SafeError`), saves nothing, and the batch continues. `cancel()` stops IMMEDIATELY: the running ticker's child task is cancelled, the fetcher `finally` closes subscriptions (`cancelMktData`; the wait is bounded by `BATCH_CANCEL_CLEANUP_SEC` = `10.0`, injectable as `cancel_cleanup_sec`; on expiry a warning is logged and the lock is still released), the FINISHED pages are saved as a partial snapshot (`AnalyzerService.save_partial_ticker_locked(raw, progress, reason)`, reason `cancelled`, no IBKR calls, no paper-trade quoting), and the current plus remaining tickers become `cancelled`. A per-ticker cap (`watchlist.batch_ticker_timeout_sec`, `240` s, `30..1800`; `ticker_timeout_sec` override) cancels the child, saves a partial (`timeout`), marks the ticker `failed` with `GATEWAY_TIMEOUT` / `Timed out after 240 s`, marks the remaining tickers `not_started` and ends the batch `aborted` with a notice. `GatewayConnectionError` saves a partial (`connection_lost`) and aborts the same way; `GatewayTimeoutError` saves a partial (`timeout`). Cancel wins over abort. A ticker interrupted during paper-trade quoting is already saved, so it ends `done` with no duplicate partial. Shutdown `aclose()` still cancels mid-ticker (its wait is bounded by `BATCH_SHUTDOWN_TIMEOUT_SEC` = `15.0`, injectable as `shutdown_timeout_sec`; a stuck task is logged).
- **Routes (`src/web/api.py`):** `GET /api/watchlist`, `POST /api/watchlist/tickers` (`{ticker}`; a duplicate returns `200` with `changed: false`), `DELETE /api/watchlist/tickers/{ticker}` (all return `WatchlistResponse`); `POST /api/refresh/batch` (`BatchRefreshRequest` -> `202` `BatchRefreshStatus`; unknown fields such as `strike_step` -> `422`), `GET /api/refresh/batch/current` (latest batch even after it finished, or `null`), `GET /api/refresh/batch/{batch_id}`, `POST /api/refresh/batch/{batch_id}/cancel` (no-op on a finished batch). `batch_id` must match `BATCH_ID_PATTERN` (`^[0-9a-f]{32}$`), else `422`.
- **Error codes (`src/web/errors.py`):** `WatchlistCapError` -> `409 WATCHLIST_FULL`; `StorageUnavailableError` -> `503 STORAGE_UNAVAILABLE`; `BatchNotFoundError` -> `404 BATCH_NOT_FOUND`; `RefreshBusyError` -> `409 REFRESH_BUSY` (`Retry-After`, owner `batch:<i>/<n>:<SYM>`) for a second batch or a single refresh, Corridor, or ledger call during a batch; invalid tickers -> `422 INVALID_TICKER`.
- States: batch `running` | `cancelling` | `completed` | `cancelled` | `aborted`; per ticker `queued` | `running` | `done` | `failed` | `cancelled` | `not_started`. `BatchRefreshStatus.not_started_count` counts the latter; `BatchTickerResult.partial` marks a ticker whose finished pages were archived as a partial snapshot (`data/latest` untouched); `ChainAnalyticsResponse.partial` / `partial_reason` mirror the loaded snapshot's index entry. Watchlist freshness (`WatchlistEntry`) ignores partial snapshots.

### 3.8 Best Option Screener & Live Leg Stream (`src/services/screener.py`, `src/services/screener_live.py`, `src/broker/live_legs.py`)
- **`ScreenerService(source, watchlist, repo, *, settings, clock, runner=asyncio.to_thread, cache_max_entries=240)` (`AnalyzerService.screener`):** `async run(ScreenerRunRequest) -> ScreenerRunResponse`. Zero IBKR calls and never takes `refresh_lock`. Data = the user watchlist, newest COMPLETE snapshot of any date per ticker (`latest_snapshots(include_partial=False)`); rows carry `snapshot_id`, `snapshot_ts_utc`, `is_today_et`, `market_data_type`. Candidates come from `AnalyzerService.build_strategy_candidates(...)` (the Autobuild path, also used by `suggest_strategies`) over `load_enriched_snapshot(raw_ticker, snapshot_id)`.
- **Modes:** `selected` (needs `strategy_kind`), `best` (default: highest RoR, tie -> higher POP, then preset order), `all` (only `ok` rows). Filters are built as in Autobuild; min RoR / min POP then drop candidates of every strategy, min credit only applies to `CREDIT_STRATEGY_KINDS` (Bull Put, Bear Call, Iron Condor). Status per ticker (selected/best): `ok` | `filtered` (`failed_filters`; builder `BELOW_MIN_*` skips count as filtered) | `not_buildable` (sanitized reason) | `no_data`. Rows hold legs (`EvaluatedLeg`), `legs_label`, pricing, risk (incl. `expected_return_on_risk_pct`, `nearest_breakeven_distance_pct`), net Greeks, `min_leg_open_interest` (`None` if any leg OI is unknown), rationale and primary walls, no payoff curves; default order RoR desc, `None` last.
- **Cache:** in-memory LRU keyed by ticker + snapshot id + DTE + min credit + min RoR, pruned when the newest complete snapshot changes or the ticker leaves the watchlist; failures are never cached; a run lock serializes runs.
- **Broker line layer (`LiveLegQuoteService.stream_live_legs(...) -> LiveLegsFrame`, `src/broker/live_legs.py`):** 1 s cadence (`LIVE_LEGS_INTERVAL_SEC`, interval >= 0.55 s). `LiveLegsSpec.validate(budget, *, option_budget)` allows <= 88 option lines (`corridor.default_max_contracts`) AND <= 90 total lines incl. spots, else `PacingViolationError`. The spec is re-read every tick and the diff applied cancels-first; a subscribe-time busy re-check raises `ScreenerLiveBusyError` (`SCREENER_LIVE_BUSY_MESSAGE`); registering cancels older leg sessions first; `DISCONNECT_CODES` end the stream with `GatewayConnectionError`; every line is cancelled in `finally:`.
- **`ScreenerLiveService` (`AnalyzerService.screener_live`):** `open_live_stream(ScreenerLiveRequest)` (`RefreshBusyError` when `refresh_lock` is held), `reallocate(ScreenerLiveReallocateRequest)` (`LiveStreamNotFoundError`), `stop_live_streams`, `stop_now`. First-fit allocation of `ok` rows in the order the client sends them (shared strikes subscribed once, the rest `not_live_budget`; `ScreenerLiveRequest.row_ids` <= 120). Each tick runs `repick_strategy_legs` on snapshot rows overlaid with live quotes (only IBKR-priced lines can win; a row is `live` only when all current legs are priced, else `no_quotes`); `adjusted` persists while legs differ from the screener row; windows re-centre at re-allocation; a server-side stop ends with one stopped frame. Display-only. UI: `#best-option-card` (`js/dashboard/screener.js`, `css/screener.css`; see `docs/UI_UX_GUIDELINES.md` and `docs/reference/IMPLEMENTATION_NOTES.md` section 5.1).
- **Mutual exclusion:** starting either live feed (Corridor or screener) stops the other. `AnalyzerService._stop_live_feeds` (both feeds) is called by every `refresh_lock` holder (`refresh_chain`, batch run, ledger sync/MTM, `fetch_corridor_quotes`); legacy `_stop_corridor` now stops both; `LedgerService(screener_stopper=)`.
- **Routes (`src/web/api.py`):** `POST /api/screener/run` (`ScreenerRunRequest` -> `ScreenerRunResponse`); `GET /api/screener/stream` (SSE; required query `q` = JSON-encoded `ScreenerLiveRequest`, validated only by `_parse_screener_live_query` (no FastAPI length bounds): more than `SCREENER_STREAM_QUERY_MAX_CHARS = 12_000` or fewer than 2 chars, invalid JSON, or invalid/unexpected fields -> `422 INVALID_REQUEST` with fixed text that never echoes the input (extra fields -> "has an unexpected field", locations limited to known field names); row ids formatted `{TICKER}:{KIND}`, <= 120; optional `max_frames` 1..200 for tests); `POST /api/screener/stream/reallocate` (`{stream_id, row_ids}` -> `ScreenerLiveReallocateResponse`); `POST /api/screener/stop` (-> `ScreenerLiveStopResponse` `{stopped_subscriptions, active_streams}`).
- **SSE events:** `open` `{stream_id, lines_budget, option_lines_budget}` first, then `frame` (`ScreenerLiveFrame`); `stopped` (`stopped=true`, `stop_reason` in `manual_stop`, `superseded`, `corridor_stream`, `corridor_snapshot`, `refresh_chain`, `batch_refresh`, `ledger`, `ledger_sync`, `ledger_mtm`, `shutdown`) ends the stream; `error` `{error_code, status, message}` ends the stream: `GATEWAY_TIMEOUT` 503, `GATEWAY_OFFLINE` 503, `REFRESH_BUSY` 409, `SCREENER_STREAM_ERROR` 502, `INTERNAL_ERROR` 500 (fixed generic message).
- **HTTP errors:** pre-stream `409 REFRESH_BUSY` (lock held), `422`, `503`; reallocate `404 LIVE_STREAM_NOT_FOUND` (`LiveStreamNotFoundError`) or `409 REFRESH_BUSY`. Lifespan shutdown stops the screener live streams (`reason="shutdown"`) after the Corridor streams and before the broker disconnect.

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
4. **Strategy Lab (`src/analytics/strategies/`):**
   - Six wall-anchored presets (`BULL_PUT_SPREAD`, `BULL_CALL_SPREAD`, `DIAGONAL_SPREAD`, `IRON_CONDOR`, `BEAR_CALL_SPREAD`, `BEAR_PUT_SPREAD`) plus `CUSTOM`. Bear Call is a credit call spread with the short call at/above the Call Wall (honours min credit / min RoR floors; skip reason `NO_OTM_CALL_CANDIDATES`); Bear Put is a debit put spread (long put near ATM, short put toward the Put Wall; ignores the floors like Bull Call).
   - Missing contracts raise `StrategyFilterError` instead of a raw Python error: `NO_CALL_CONTRACTS` / `NO_PUT_CONTRACTS` (Iron Condor expiry lacks that side), `EMPTY_CHAIN` (neither side, or no Put/Call rows at all); the Strategy Lab 422 shows the message and the backtest records the skip code.
   - `StrategyRiskMetrics` adds `expected_return_on_risk_pct` (EV/risk %) and `nearest_breakeven_distance_pct` (`min |BE - spot| / spot x 100`), both `None` when not computable.
   - `repick.py` (`repick_strategy_legs`) is a pure metrics-only helper: each leg moves -1/0/+1 strike inside the window, structure and expiries stay valid, strictly higher reported RoR wins (ties: fewer moved legs, then higher POP), otherwise the current legs are kept with a `RepickKeepReason`.

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
  - `SINGLE_TRADE`: replays one `AUTO_BUILD` preset, manual `LEGS`, or saved `PAPER_TRADE` forward from its entry snapshot/bar (`_ensure_snapshot_bar` injects the exact requested entry snapshot when needed). When replaying `source="LEDGER"` trades, trades with `created_from_snapshot_id is None`, or trades with per-leg `entry_price`, `services/backtest.py` (`_run_single_trade`, honoring `leg.qty_ratio` and proportionally scaling `entry_net_cash` when `qty` overrides `paper_trade.qty`) passes `BrokerFillAnchor` (`build_broker_fill_entry()` / `resolve_broker_leg_prices()` in `rules.py`, solving missing leg fills from `entry_net_cash`) and `trade_id` into `run_single_trade_replay()`, where `_prepare_anchor_bars()` filters out pre-fill intraday bars (`ts_utc < entry_ts_utc`, or retains the latest same/later-day bar with aligned Day-0 timestamp) and emits a Day-0 anchor step tagged `FLAG_BROKER_FILL_ENTRY` with `StepMarkSource`/`LegPriceSource`/`LegQuoteSource` = `"BROKER_FILL"`. If no local chain snapshots exist on or after `paper_trade.market_date`, `_build_synthetic_ledger_mtm_bar()` synthesizes a single `SYNTHETIC_LEDGER_MTM` bar (`{SYMBOL}_{YYYY-MM-DD}_LEDGER_MTM`) from live/Flex Ledger `mark_price` -> `leg.entry_price` -> residual `entry_net_cash`. Strategy Lab (`strategies/engine.py`) separates 0-DTE `Today (T+0)` / `Simulated` intraday BSM curves (`0.5d` floor) from `At Expiration` intrinsic payoff curves (`at_expiration=True`).
  - Evaluates bar `i` exits strictly before bar `i` entries in deterministic priority: `EXPIRY_SETTLED` -> missing-price hold -> `EXIT_DTE` -> `PROFIT_TARGET` -> `STOP_LOSS` (via `resolve_tp_sl_thresholds()`), recording unclosed positions at window end (`END_OF_DATA`), attaching `profit_target_dollars`, `stop_loss_dollars`, and `selection_notes` to `BacktestTradeRecord`, splitting `realized_pnl_dollars` and `unrealized_pnl_dollars` in `PortfolioSummary`, and surfacing `BACKTEST_ASSUMPTIONS` on every response.
- **Endpoints (`src/web/backtest_api.py`):** `GET /api/backtest/entries/{ticker}`, `POST /api/backtest/run`, `GET /api/backtest/paper-trades`, `POST /api/backtest/paper-trades`, `POST /api/backtest/paper-trades/{trade_id}/stop`, `POST /api/backtest/paper-trades/sync-ledger` (zero IBKR/network side effects).

---

## 5. Resilience & Error Handling Matrix

| Failure Scenario | Detection Mechanism | System Recovery & UI Behavior |
| :--- | :--- | :--- |
| **Overlapping IBKR Request (`Refresh` / `Corridor` / `Ledger MTM`)** | `AnalyzerService.refresh_lock` detects an in-flight `refresh_chain`, `fetch_corridor_quotes`, Gateway `LedgerService.sync_ledger`, `refresh_open_mtm`, or active lock during SSE open. | Raises `RefreshBusyError` (HTTP `409 REFRESH_BUSY`, `Retry-After: 10`), protecting IBKR's 100-line session cap; `#alert-banner` surfaces the active owner so the user retries once finished. |
| **IBKR Gateway Closed (`ConnectionRefusedError`)** | Caught in `IBKRBrokerClient.ensure_connected()` within `3.0s` timeout. | Returns status `GATEWAY_OFFLINE` reporting dynamic `{IBKR_HOST}:{IBKR_PORT}`. UI loads `data/latest/{TICKER}_latest.csv` with dynamic status badge `OFFLINE CACHE • PORT {IBKR_PORT}`. |
| **Missing Live Subscription (`Error 10089 / 354`)** | Intercepted in `ib.errorEvent` callback (`_on_error`). | Marks `live_subscription_missing=True`, safely downgrades `reqMarketDataType(3)` (even during page/corridor fetches), emits fallback notice, and continues delayed collection without crashing. |
| **Outside Market Hours / Unpopulated Primary Ticks** | Page/corridor collector detects `>80%` (chain) or `100%` (corridor) of contracts have empty `bid`/`ask`/`last`. | Automatically switches to configured `fallback_market_data_type` (`4` in `DELAYED`, `2` in `LIVE`) and re-polls so frozen closing prices populate. |
| **Watchlist Full / Saved Watchlist File Locked** | `WatchlistStore` raises `WatchlistCapError` (list at `watchlist.max_size`) or `StorageUnavailableError` (existing file cannot be opened/read). | `409 WATCHLIST_FULL` / `503 STORAGE_UNAVAILABLE`; nothing is written and the saved list is never overwritten. |
| **Batch Refresh Conflicts** | `BatchRefreshService` holds `refresh_lock` for the whole batch; unknown batch id raises `BatchNotFoundError`. | Other IBKR operations (second batch, single refresh, Corridor, ledger) get `409 REFRESH_BUSY`; unknown id -> `404 BATCH_NOT_FOUND`; a failed ticker is recorded with a sanitized error and the batch continues. |
| **Screener Live Feed Conflict** | `ScreenerLiveBusyError` (`SCREENER_LIVE_BUSY_MESSAGE`) at subscribe time, `RefreshBusyError` at open when `refresh_lock` is held, `LiveStreamNotFoundError` on an unknown stream. | `404 LIVE_STREAM_NOT_FOUND` (reallocate), in-stream SSE `error` `SCREENER_STREAM_ERROR` (`502`) or `INTERNAL_ERROR` (`500`, generic message). No lines are left open; the newer of Corridor / screener live feed wins and every `refresh_lock` holder stops both. |
| **Invalid Custom Ticker** | Caught in `TickerValidator` or empty `qualifyContractsAsync` result. | Rejects request with HTTP `422 Unprocessable Entity` and descriptive error banner before touching option chains or disk. |
| **API Pacing Warning (`Error 100 / 162`)** | Intercepted in `ib.errorEvent`. | Paged fetcher increases inter-page cooldown (`0.35s -> 1.0s`) and retries the page once. |
| **Batch Interrupted (cancel, per-ticker `240 s` cap, `GatewayConnectionError`)** | `BatchRefreshService` cancels the ticker child task; `describe_error()` maps `GatewayTimeoutError` to `GATEWAY_TIMEOUT` (batch rows and, via `src/web/errors.py`, single refresh responses as `503 GATEWAY_TIMEOUT`; other `GatewayConnectionError`s stay `503 GATEWAY_OFFLINE`). | Finished pages archived as a partial snapshot (`cancelled` / `timeout` / `connection_lost`), remaining tickers `cancelled` or `not_started`, batch `cancelled` or `aborted`, `refresh_lock` released. |
| **Gateway Silent / Upstream Loss (`GatewayTimeoutError`, `DISCONNECT_CODES` notice)** | `run_stage` / `await_guarded` enforce `ibkr.request_timeout_sec` (`20 s`) and fail pending stages at once on an upstream-loss notice, without closing the local socket. | Structured `GatewayConnectionError` naming the stage; `cancelMktData` still runs in `finally:`; finished pages remain available via `FetchResult.partial`. |
| **Mid-Batch Gateway Drop / HTTP Timeout / Post-Refresh Render Fault** | `IBKRBrokerClient.run_stage` / `handle_stage_error` catches socket drops (`OSError`, `ConnectionError`, `DISCONNECT_CODES`: `502/504/1100/1300/2110`) during discovery/collection; frontend `refreshFromIBKR` (`apiJson`) enforces `timeoutMs: 150000` (`AbortController`) + captures non-JSON `textSnippet`; `refreshFromIBKR` separates Stage 1 HTTP pull from Stage 2 UI render. | Socket drops surface structured `503 GatewayConnectionError` with stage context (while always running `cancelMktData` cleanup). Stage 2 chart/DOM warnings preserve the archived CSV and display `Snapshot saved, UI render warning` (`UI_RENDER`) in a dismissible `.is-fresh` alert banner instead of blaming gateway/network connectivity. |
| **Paper-Trade Quoting Failure Post-Snapshot Save** | Defensively caught in `PaperTradeService.quote_active_trades_for_refresh()` (`(AnalyzerError, OSError, ValueError)`) and isolated in `BatchRefreshService._run_one()` (`run.saved` check). | Complete chain snapshot is saved to disk and marked `done`; leg quote warning is appended as a non-fatal notice; snapshot completion is never reverted to failed. |
| **Flex Web Service Failure / Malformed Upload** | `FlexWebServiceClient` catches HTTP/network errors (`http.client.HTTPException`, `URLError`, retries `1018`/`1019`); `parse_flex_xml`/`parse_activity_csv` reject NUL bytes or `(ValueError, csv.Error)` payloads; `api_ledger_import` enforces `MAX_LEDGER_IMPORT_BYTES` and `.xml`/`.csv`. | Flex errors raise `FlexServiceError` (`502 FLEX_SERVICE_ERROR`); malformed uploads raise `StorageSchemaError` (`422 STORAGE_SCHEMA_ERROR`) or `InvalidRequestError` (`422`), with `redact_flex_secrets()` stripping tokens and account IDs; existing `data/ledger/` archives stay intact offline. |
| **Unconfigured Flex / Monthly / Gateway Sync** | `LedgerService.sync_ledger` or `select_flex_query_ids` detects missing token/query ID or `ENABLE_GATEWAY_LEDGER_SYNC=false`. | Raises `ConfigurationError` (`422 CONFIGURATION_ERROR`) surfaced in `#alert-banner`; `GET /api/ledger` still serves persisted local executions and open positions offline. |


---

## 6. Implementation Notes
Per-module implementation details (class/function inventories, helper names, cache-buster versions) live in `docs/reference/IMPLEMENTATION_NOTES.md` (§1 core, §2 storage, §3 broker, §4 analytics, §5 web incl. §5.1 UI element inventory). This file only holds architectural contracts.
