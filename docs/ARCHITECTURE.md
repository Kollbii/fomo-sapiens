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
    │   ├── tracked/
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
    │   │   ├── corridor.py
    │   │   ├── fetcher.py
    │   │   └── parser.py
    │   ├── storage/
    │   │   ├── __init__.py
    │   │   ├── schema.py
    │   │   ├── quality.py
    │   │   └── csv_repository.py
    │   ├── analytics/
    │   │   ├── __init__.py
    │   │   ├── bsm_fallback.py
    │   │   ├── walls.py
    │   │   ├── anomalies.py
    │   │   ├── term_greeks.py
    │   │   ├── strategies.py
    │   │   ├── backtest.py
    │   │   └── tracked_history.py
    │   ├── collector/
    │   │   ├── calendar.py
    │   │   ├── store.py
    │   │   └── service.py
    │   ├── tracker/
    │   │   ├── store.py
    │   │   └── service.py
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
   - **Hard Safety Cap:** Total option contracts (`Expirations * Strikes * 2`) cannot exceed `MAX_TOTAL_CONTRACTS = 400` (`5 pages * 80 contracts/page`). If a wide range + small step exceeds 400 contracts, the strider widens along canonical steps and returns an informational notice to the UI.
5. **Stage 5 — Paged Micro-Batch Downloader & Future OPRA Streaming Placeholder:**
   - Construct `Option` contracts (`Right='C'` and `Right='P'`) for the filtered grid and qualify them in chunks via `qualifyContractsAsync`.
   - Partition qualified contracts into pages of **`MAX_CONTRACTS_PER_PAGE = 80`**.
   - **Execution Modes in `fetcher.py` (via unified `_collect_page_with_fallback`):**
     - **`DELAYED` Collector (`_fetch_page_delayed`):** Calls `reqMktData(contract, genericTickList="100,101,106", snapshot=False, regulatorySnapshot=False)` for the page of `<= 80` contracts in bursts of `20` with a `0.55s` pause before each burst (at most `40` messages per second, below the `ib_async` throttle of `45` per second, so nothing waits in its send queue), then polls every `0.25s` up to `2.25s` until every contract has a `bid` or `ask` (`has_real_quote`; `close` does not count), cascades to `fallback_market_data_type` (`4`) when `>80%` of the page has no `bid`/`ask` (`empty_real_quote_ratio`, page `0` only so a snapshot keeps one data type), cancels subscriptions (`cancelMktData`) in `finally:`, and waits `0.35s` before the next page.
     - **`LIVE_OPRA` Collector (`_fetch_page_live_opra`):** Requests live OPRA generic ticks `"100,101,104,106"` under configured live primary/fallback (`1 -> 2`) and waits for both `modelGreeks` and `callOpenInterest`/`putOpenInterest` (tick `101`) confirmation; if `Error 10089`/`354` triggers during collection, seamlessly delegates to `_fetch_page_delayed`.

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
   - Computes top Call Walls and Put Walls per weekly expiration (`by_dte`) and across the aggregate `[30, 120]` DTE window using both **Open Interest** and **Volume**.
   - If `open_interest` is null across the snapshot (common on unsubscribed Type 3 feeds), the module automatically promotes **Volume Walls** as primary and sets `wall_metric_used = "VOLUME"`.
2. **Unusual Activity Scanner (`src/analytics/anomalies.py`):**
   - Flags `volume > open_interest` anomalies when `open_interest > 0`.
   - Computes intra-expiration Volume Z-Scores (`(volume - mean_vol) / std_vol`) so unusual strike activity is always surfaced even when Open Interest is null.
3. **Multi-DTE Comparative Structure (`src/analytics/term_greeks.py`):**
   - Aligns option chains across all weekly expirations in `[30, 120]` DTE over a unified strike and moneyness axis for Plotly overlays (IV Smile overlay, Delta/Gamma/Theta/Vega profile by DTE, Volume/OI concentration).

---

## 5. Resilience & Error Handling Matrix

| Failure Scenario | Detection Mechanism | System Recovery & UI Behavior |
| :--- | :--- | :--- |
| **IBKR Gateway Closed (`ConnectionRefusedError`)** | Caught in `IBKRBrokerClient.ensure_connected()` within `3.0s` timeout. | Returns status `GATEWAY_OFFLINE` reporting dynamic `{IBKR_HOST}:{IBKR_PORT}`. UI loads `data/latest/{TICKER}_latest.csv` with dynamic status badge `OFFLINE CACHE • PORT {IBKR_PORT}`. |
| **Missing Live Subscription (`Error 10089 / 354`)** | Intercepted in `ib.errorEvent` callback (`_on_error`). | Marks `live_subscription_missing=True`, safely downgrades `reqMarketDataType(3)` (even during page/corridor fetches), emits fallback notice, and continues delayed collection without crashing. |
| **Outside Market Hours / Unpopulated Primary Ticks** | Page/corridor collector detects `>80%` (chain page `0`: no `bid`/`ask`) or `100%` (corridor) of contracts without usable quotes. | Automatically switches to configured `fallback_market_data_type` (`4` in `DELAYED`, `2` in `LIVE`) and re-polls so frozen closing prices populate. |
| **Invalid Custom Ticker** | Caught in `TickerValidator` or empty `qualifyContractsAsync` result. | Rejects request with HTTP `422 Unprocessable Entity` and descriptive error banner before touching option chains or disk. |
| **API Pacing Warning (`Error 100 / 162`)** | Intercepted in `ib.errorEvent`. | Paged fetcher increases inter-page cooldown (`0.35s -> 1.0s`) and retries the page once. |
| **Mid-Batch Gateway Drop / HTTP Timeout / Post-Refresh Render Fault** | `IBKRBrokerClient.run_stage` / `handle_stage_error` catches socket drops (`OSError`, `ConnectionError`, `DISCONNECT_CODES`: `502/504/1100/1300/2110`) during discovery/collection; frontend `apiJson` enforces `timeoutMs: 120000` (`AbortController`) + captures non-JSON `textSnippet`; `refreshFromIBKR` separates Stage 1 HTTP pull from Stage 2 UI render. | Socket drops surface structured `503 GatewayConnectionError` with stage context (while always running `cancelMktData` cleanup). Stage 2 chart/DOM warnings preserve the archived CSV and display `Snapshot saved, UI render warning` (`UI_RENDER`) in a dismissible `.is-fresh` alert banner instead of blaming gateway/network connectivity. |


---

## 6. Phase 1 Implementation Notes (`src/core/`) — `[IMPLEMENTED]`
- **`src/core/exceptions.py`:** `AnalyzerError` base + `SecurityViolationError`, `InvalidTickerError`, `ConfigurationError`, `GatewayConnectionError`, `UnderlyingPriceUnavailableError`, `OptionChainFetchError`, `PacingViolationError`, `StorageSchemaError`.
- **`src/core/config.py`:** Frozen, `extra="forbid"` Pydantic v2 models (`AppSettings`, `IBKRSettings`, `WatchlistSettings`, `OptionsChainSettings` — including `prefetch_exact_strikes`, `contract_details_timeout_sec`, `contract_details_cooldown_sec`, and `contract_details_cache_ttl_sec`, `CorridorSettings` — `default_window_strikes=5`, `default_max_contracts=56`, `hard_max_contracts=60`, `stream_poll_interval_sec=0.75`, `oneshot_collect_timeout_sec=2.0`, `heartbeat_interval_sec=5.0`, `AnalyticsSettings`, `StorageSettings`, root `Settings`). `load_settings(yaml_path, env_file)` loads `config/settings.yaml`, then applies flat env overrides (process env beats `.env`): `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, `MARKET_DATA_MODE`, `DEFAULT_MARKET_DATA_TYPE`, `FALLBACK_MARKET_DATA_TYPE`, `RISK_FREE_RATE`, `LOG_LEVEL`, `APP_HOST`, `APP_PORT`, with `_reconcile_ibkr_env_overrides` + `IBKRSettings._align_defaults` keeping mode/primary/fallback types aligned. Validation failures raise `ConfigurationError`. `ibkr.read_only_enforced: false` is rejected. `get_settings()` is `lru_cache`d; `configure_logging()` is idempotent (stdout + rotating `storage.logs_dir/analyzer.log`, 5 MB x 5).
- **`src/core/security.py`:** `TickerValidator` (`validate` classmethod = universal gate; instance `is_watchlist` / `validate_selection` honour `allow_custom_ticker`) and `ReadOnlyIBGuard.lock/verify_locked` (traps installed on the `IB` instance and `ib.client`).
- **Cross-platform tooling:** `Makefile` uses `uv run --no-sync` and Python one-liners, so `make check` works natively offline on Windows, macOS and Linux.

---

## 7. Phase 3 Implementation Notes (`src/broker/`) — `[IMPLEMENTED]`
- **`src/broker/client.py` (`IBKRBrokerClient`):** Immediately locks `ib_async.IB` with `ReadOnlyIBGuard.lock` + `verify_locked`. Connects via `ib.client.connectAsync` (bypassing `IB.connectAsync`'s automatic position/order/account queries so zero account queries ever execute). Exposes `LIVE_TYPES`, `ALL_MARKET_DATA_TYPES`, and `market_data_type_label()`. Absorbs `Error 10089`/`354` (setting `live_subscription_missing`, emitting notice via `consume_subscription_fallback_notice()`, and falling back to Type 3 when in `LIVE` mode), ignores info codes `2104/2106/2107/2108/2158`, flags pacing codes `100/162`, tracks `DISCONNECT_CODES` (`502/504/1100/1300/2110`) and `_was_connected` state (`was_connected`, `ensure_still_connected`, `run_stage`, `handle_stage_error` translating post-handshake transport/socket drops into `GatewayConnectionError`), observes server-reported `Ticker.marketDataType` via `observe_tickers_market_data_type()` (while guarding unpopulated default `1` values in `DELAYED` mode) for `get_active_market_data_type()`, and drives configured Type `3 -> 4` (`DELAYED`) or `1 -> 2` (`LIVE`) market data cascades.
- **`src/broker/contracts.py` (`ContractDiscoveryService`, `ExpirationSpec`):** Qualifies `Stock(symbol, "SMART", "USD")`, resolves reference spot with `select_spot` / `resolve_spot_with_source` (waits up to `2.25s` for `last` + `bid`/`ask`; uses `last` unless it deviates more than `SPOT_LAST_MID_MAX_DEVIATION = 2%` from the midpoint, then `MID`; the previous `close` only on timeout, flagged `CLOSE`; logs the raw fields; cascades Type 3 -> 4 when nothing arrives; the source is stored as `UnderlyingInfo.spot_source`), inspects all `SMART` chains (`fetch_chains` / `select_smart_chains`) to match each weekly expiration inside `30 <= dte <= 120` (`match_expirations`, preferring Friday) to its exchange descriptor (`select_chain_for_expiration`), pre-fetches exact per-expiration `OPT` contracts (`_prefetch_exact_contracts_by_exp` using bounded `asyncio.Semaphore(EXACT_PREFETCH_MAX_CONCURRENCY = 3)` concurrency when inside a running event loop with deterministic sequential fallback outside, calling `reqContractDetailsAsync` with `strike=0.0, right=""`, cooldown pacing, timeout guard that preserves fallback to `reqSecDefOptParams` on `TimeoutError` while translating socket drops into `GatewayConnectionError`, and 15-min TTL cache), intersects candidate strikes strictly against each expiration's verified strikes (`select_strikes`) while adapting non-standard steps via `detect_native_strike_step`, computes `choose_auto_strike_step` when `strike_step=None`, derives `detect_available_strike_steps` / `detect_available_range_pcts`, auto-widens along canonical steps if total contracts would exceed `400`, and records per-expiration `ExpirationSpec`s (with `qualified_contracts` and `exact_strikes_verified`) plus `exchange_validated_strike_count` and `exchange_native_verified` notices on `ChainPlan`.
- **`src/broker/parser.py` (`TickerParser`):** Sanitizes IBKR sentinels, maps `callOpenInterest`/`putOpenInterest` (tick `101`) by option right, validates `modelGreeks` bounds (`0 < iv <= 10.0`, `abs(delta) <= 1.001`, `gamma >= 0`) to classify `greeks_source` as `"IBKR_MODEL"` or `"NONE"`, and returns validated 27-field `OptionContractRow` models.
- **`src/broker/fetcher.py` (`PagedOptionChainFetcher`):** Builds per-expiration `Option` contracts from `ChainPlan.expiration_specs` (reusing exact pre-qualified `Contract`s from `spec.qualified_contracts` when `exact_strikes_verified=True` and skipping phantom missing sides, or preserving each expiration's intersected strikes, `trading_class`, and `multiplier`), skips `qualifyContractsAsync` for any contract already holding `conId > 0` while qualifying remaining fallback candidates via `client.run_stage` in `<= 80`-contract batches (surfacing explicit dropped-contract warning notices if IBKR rejects any candidates), partitions into pages (`<= 80` contracts/page, max `5` pages = `400` contracts), paces subscribe and cancel calls in bursts of `PAGE_PACING_CHUNK_SIZE = 20` with `await self.sleep(PAGE_PACING_PAUSE_SEC = 0.55)` before each burst in `_collect_page_once` (the poll window starts after the last request is sent; `_cancel_paced` never skips a cancel and re-raises `CancelledError` afterwards) while translating mid-batch socket drops via `handle_stage_error` + guaranteed `finally` `cancelMktData`, drives `_fetch_page_delayed` (`genericTickList="100,101,106"`) and `_fetch_page_live_opra` (`genericTickList="100,101,104,106"`) through unified `_collect_page_with_fallback(...)` (`try/finally` `cancelMktData` leak guard, `observe_tickers_market_data_type`, `>80%` empty quote dynamic primary -> fallback cascade, `10089/354` live->delayed downgrade, and pacing cooldown widening `0.35s -> 1.0s`), and logs the structured `refresh_summary` line (including `page_quote_counts` and `spot_source`; `FetchResult.page_quote_counts`). Root cause fixed in 0.2.2: the old `0.01s` yields let each page queue 160 messages behind the `ib_async` 45/s throttle while the poll timer was already running, so later pages were cancelled before their requests were sent. A refresh of 5 pages now takes about `34s` (dashboard timeout `120s`; uvicorn has no request timeout).
- **`src/broker/corridor.py` (`select_wall_corridor`, `CorridorQuoteService`) — `[IMPLEMENTED — MODULE 2]`:** Builds a deterministic targeted strike corridor (`±window_strikes`, default `±5`) around Primary Put & Call Walls plus active Strategy Lab legs (Tier 0, even across secondary DTEs for Diagonals), primary Wall-side contracts (Tier 1), opposite rights at Wall strikes (Tier 2), and nearest ATM strikes (Tier 3), strictly enforcing `len(specs) <= max_contracts <= CORRIDOR_MAX_CONTRACTS (60)` (raising `PacingViolationError` if violated). Reuses `option_conid > 0` and `underlying_conid > 0` from CSV/cache (`Option(conId=...)`, `Stock(conId=...)`) with zero redundant `qualifyContractsAsync` calls, shares `_poll_initial_corridor` (with `_resubscribe` and cleanup safety) so both `fetch_corridor_quotes()` and `stream_corridor_quotes()` cascade dynamically across `DELAYED` (`Type 3 -> 4`), `LIVE` (`Type 1 -> 2`), and `10089/354` live->delayed drops while observing ticker `marketDataType` on every poll/frame, fills missing delayed `modelGreeks` via vectorized BSM fallback (`bsm_greeks`, `solve_implied_volatility`), provides one-shot `fetch_corridor_quotes()` and single-active continuous `stream_corridor_quotes()` (throttled change + heartbeat frames with `stop_all_streams()`), and guarantees 100% `ib.cancelMktData` teardown in `finally:` on stop, SSE disconnect, ticker/expiry/window switch, gateway drop, or FastAPI lifespan exit.


---

## 8. Phase 4 Implementation Notes (`src/analytics/`) — `[IMPLEMENTED]`
- **`src/analytics/bsm_fallback.py` (`BSMFallbackEngine`, `enrich_missing_greeks`):** Vectorized 25-iteration hybrid Newton-Raphson + Bisection IV solver on `[0.01, 5.00]` ($T = \max(\text{dte}, 0.5) / 365.0$) and analytical BSM Greeks (`delta`, `gamma`, daily `theta`, `vega` per 1% IV), preserving native `"IBKR_MODEL"` rows and leaving unsolvable rows `"NONE"`.
- **`src/analytics/walls.py` (`WallsAnalyzer`, `compute_walls`):** Returns `ChainWallsReport` (`WallLevel`, `DTEWallSummary`, `by_dte`) ranking aggregate and per-DTE Put/Call walls by `OPEN_INTEREST` (tie-breaking on `volume`) with automatic fallback to `VOLUME` when total Open Interest is `0`.
- **`src/analytics/anomalies.py` (`AnomalyScanner`, `scan_anomalies`):** Returns `AnomaliesReport` (`ContractAnomaly`) flagging `volume > open_interest` (`vol_oi_ratio`) and intra-`(expiry, right)` Volume Z-score outliers (`volume_zscore >= zscore_threshold`), sorted by `(is_vol_gt_oi desc, vol_oi_ratio desc nulls last, volume_zscore desc)`.
- **`src/analytics/term_greeks.py` (`MultiDTEStructureAnalyzer`, `build_multi_dte_profiles`):** Returns `MultiDTEProfilesReport` (`DTEProfile`, `StrikeProfilePoint`, `expiries`) aligning Calls and Puts by strike across weekly `[30, 120]` expirations, computing per-expiry `atm_strike` and `atm_iv`, and honoring configured `default_market_data_type` when frames are empty.
- **`src/analytics/strategies.py` (`evaluate_strategy`, `suggest_strategy_legs_from_walls`, `StrategyEvaluationResponse`):** Pure Polars + NumPy/BSM strategy engine supporting `BULL_PUT_SPREAD`, `BULL_CALL_SPREAD`, `DIAGONAL_SPREAD`, `IRON_CONDOR`, and user-picked `CUSTOM` multi-leg definitions over cached option chains. Computes `Mid`/`Natural`/Slippage/Limit Ladder execution metrics, exact grid risk bounds (`max_profit`, `max_loss`, `max_loss_unbounded`, `return_on_risk_pct`, `breakevens`), log-normal `prob_of_profit_pct`, `±1σ` expected move, net position Greeks, and interactive `Today (T+0)` / `Midpoint` / `At Expiration` / `Simulated (T+n ΔIV)` BSM P&L curves.


---

## 9. Phase 5 Implementation Notes (`src/main.py`, `src/web/`) — `[IMPLEMENTED]`
- **`src/main.py` (`create_app`, `lifespan`):** Configures idempotent logging, mounts `/static` (`StaticFiles`), wires Jinja2 templates (`src/web/templates/`), manages the singleton `IBKRBrokerClient` + `AnalyzerWebService` on `app.state` without crashing when the configured IBKR socket is closed at boot, awaits `service.stop_corridor_streams()` before disconnecting `broker_client` on shutdown, and serves `GET /` with settings-driven watchlist, `range_pct`, `strike_step`, `dte_min`/`dte_max`, `ibkr_host`/`ibkr_port`, `market_data_mode`, primary/fallback market data types, and dynamic `feed_summary`.
- **`src/web/api.py` (`AnalyzerWebService`, `router`):** Exposes typed endpoints `GET /api/status` (accepting optional `?ticker=` and returning `GatewayStatusResponse` / `SystemStatusResponse` with dynamic `host`/`port`, `market_data_mode`, `default_market_data_type`, `fallback_market_data_type`, live `active_market_data_type`, `live_subscription_missing`, `dte_min` / `dte_max`, `available_strike_steps`, `available_range_pcts`, and `active_capabilities_ticker` cached from live IBKR discovery), `GET /api/snapshots/{ticker}`, `GET /api/chain/{ticker}?snapshot_id=...&dte_min=...&dte_max=...` (supporting DTE filtering up to `120`), `POST /api/refresh` (accepting optional `dte_min`/`dte_max` up to `120`, with `BSMFallbackEngine` enrichment, effective strike step verification/inferred fallback from `infer_chain_step_and_range`, per-ticker capability caching, and immutable `CSVRepository` archival storing `requested_strike_step`), `GET /api/csv/{ticker}?snapshot_id=...` (`FileResponse` CSV download), cache-only click-triggered Strategy Lab endpoints `POST /api/strategy/suggest` (`StrategySuggestRequest` -> `StrategySuggestResponse`) and `POST /api/strategy/evaluate` (`StrategyEvaluateRequest` -> `StrategyEvaluationResponse`), plus Module 2 Wall Corridor endpoints `POST /api/corridor/quotes` (`CorridorQuotesRequest` -> `CorridorQuoteFrame`), `POST /api/corridor/stop` (`CorridorStopResponse`), and `GET /api/corridor/stream/{ticker}` (SSE `StreamingResponse` yielding `event: frame` & `event: error` on disconnect with `finally: await gen.aclose()` and pre-stream `ensure_connected()` check returning HTTP `503` if Gateway is offline). Live Corridor quote frames update an in-memory `_corridor_cache` overlaid onto `/api/strategy/suggest` and `/api/strategy/evaluate` (`apply_corridor_overlay=True`) while keeping Mode 1 CSV archives on disk strictly immutable. Structured JSON exception handlers map validation/ticker/legs (`422`), missing snapshots (`404`), and offline/fetch/pacing (`503`, dynamically formatting configured `{host}:{port}`).
- **Editorial + Terminal UI (`src/web/templates/{base,index}.html`, `src/web/static/css/tokens.css`, `src/web/static/js/dashboard.js`):** Implements the 6-row 12-column responsive layout with NYT Print Edition (`data-theme="print"`) vs. Obsidian Terminal (`data-theme="terminal"`) `localStorage` switching + dynamic Plotly re-skinning, cache-busted static assets (`tokens.css?v=20261111i`, `dashboard.js?v=20261112t`), dynamic gateway/feed bootstrapping via `#app-bootstrap` (`ibkrHost`, `ibkrPort`, `marketDataMode`, `defaultMarketDataType`, `fallbackMarketDataType`, `dteMin`, `dteMax`), `#editorial-banner-slot` with config-derived feed mode/DTE banner, pull-scope and exchange-verified strike count telemetry, dismissible `#alert-banner` (`#alert-banner-close`, `#N • HH:MM:SS` badge, `×K` repeat counter, `.is-fresh` pulse, pull/phase/HTTP/elapsed chips, `<details>` traceback + non-JSON `textSnippet`) backed by `apiJson` (`timeoutMs`, `elapsedMs`) and isolated Stage 1 HTTP vs. Stage 2 `UI_RENDER` error boundaries (with hoisted `schedulePlotlyResize`), `• Snap T<N>` chain badge attribution when offline or snapshot type differs from live feed, per-ticker pull preferences (`localStorage("ibkr_pull_prefs_v1")`) + `Step $` / `Range %` control synchronization + dynamic exchange capability disabling (`N/A`) + snapshot dropdown scope metadata, 4 KPI cards, corner-resizable **Snap-to-Grid** (`span 4 / 6 / 8 / 12` CSS Grid columns & quantized heights persisted in `localStorage`) multi-DTE Plotly panels (`#chart-walls`, `#chart-iv-smile`, `#chart-anomalies`, `#chart-greeks`, click-only default-enabled `#wall-corridor-card` with one-shot/SSE stream toggle, Bid-Ask corridor ladder table & `#chart-corridor-ladder` synced live into Strategy Lab legs without overwriting open `<select>` dropdowns, and `#strategy-lab-card` with `[x] Auto-Build on Load` `#strategy-auto-populate` cache-only auto-population, cross-panel DTE nearest-expiry snap via `snapDteValueToExpiries`, `CUSTOM` leg preservation across same-ticker reloads, Wall auto-builder presets, locally populated editable multi-leg picker, `Days Forward`/`IV Shock` sliders, and `#chart-strategy-payoff`), and the 18-column monospaced `#chain-table` featuring DTE/Expiry, Right, Unusual filters, interactive per-column `aria-sort` headers (nulls-last, `localStorage`-persisted), sticky `<tr class="table-filter-row">` per-column dropdowns/numeric expression filters, `+Leg` row buttons (`.chain-add-leg-btn` -> `addSpecificStrategyLeg`), and `#table-reset-filters-btn`.

## 10. Snapshot Quality (`src/storage/quality.py`) — `[IMPLEMENTED]`
- Every saved snapshot records `spot_source`, `quoted_contract_count`, `quote_coverage`, `prior_close_only_count` and `collection_mode` in `SnapshotMeta` (see `docs/DATA_SCHEMA.md` §4.1). `ChainMetadataPayload` returns the same fields.
- `evaluate_snapshot(meta)` is the single usable-for-history rule (coverage `>= 0.90`, spot `LAST`/`MID`, market data type `1` or `3`). `POST /api/refresh` always saves and adds a notice when the snapshot is not usable.
- `POST /api/refresh` stops any running corridor stream first (`stop_all_streams`), because a page of `80` plus a stream of up to `60` would exceed IBKR's default `100` market-data lines.

## 11. Bull Put Spread Backtest Engine (`src/analytics/backtest.py`) — `[IMPLEMENTED]`
Base for the future option backtest analyzer. No API endpoint or UI yet.

- **Inputs:** ticker (`TickerValidator`), entry snapshot id, mode `LEGS` (user legs, validated as a bull put: 2 puts, one expiry, one SELL above one BUY, different strikes, equal positive quantity; messages name the problem) or `AUTO_BUILD` (Strategy Lab `BULL_PUT_SPREAD` preset on the entry snapshot), quantity multiplier, optional end snapshot.
- **Loading (`load_backtest_steps`):** lists `data/snapshots/{TICKER}/*.csv` and reads each with `CSVRepository.load_snapshot`; reads `snapshot_index.json` directly for `spot_source` and never calls `list_snapshots()` (which rebuilds a missing index). Since engine version 2 it also reads, read-only, `collector_runs.jsonl`, `tracker_runs.jsonl`, `tracked_spreads.json` and `data/tracked/{TICKER}/*.csv` (`src/analytics/tracked_history.py`; folder from the `tracked_dir` argument, default `data/tracked` under the repository root). Nothing is written, no folder is created, no index is rebuilt. Steps are ordered by time.
- **Entry:** `evaluate_strategy` on the entry snapshot (Greeks filled with BSM like the API, no corridor overlay): mid entry cash, natural slippage as information, max risk.
- **Each step:** the same contracts (expiry, strike, right) are priced with `resolve_price_with_source` (same value as the Strategy Lab chain bid/ask mid -> mid -> mark -> last -> close, plus its source; a parity test guards it). Per step: UTC and ET time, short-leg DTE, spot, spread value per share, position value, P&L in $ and % of max risk, net Greeks.
- **Quality (flag, never skip):** `INCOMPLETE` when a leg is missing (`MISSING_OUTSIDE_DTE_WINDOW` / `MISSING_OUTSIDE_STRIKE_RANGE` / `MISSING_NOT_LISTED` / `MISSING_NO_DATA`) or has no price (P&L left empty, never estimated); `UNRELIABLE` for `LEG_FALLBACK_PRICE`, `LOW_QUOTE_COVERAGE` (< 0.90), `SPOT_SOURCE_CLOSE`, `FROZEN_DATA` (Type 2/4), `OFF_SESSION` (outside the NYSE session of that date: 09:30-16:00 ET, 13:00 on early closes, none on holidays; version 1: 09:30-16:00 Mon-Fri), `SPREAD_VALUE_OUT_OF_BOUNDS` (value < 0 or > width); informational `SPOT_SOURCE_UNKNOWN`, `FILTER_CHANGED`. Summaries use `OK` steps only. Thresholds are constants at the top of the module and are recorded in the result.
- **Expiry:** hold to expiry. Steps at/after the expiry-date close (NYSE calendar: 16:00 ET, 13:00 ET on early closes) are not marked; settlement uses the last expiry-day normal snapshot at/after close + 15 min (16:15 / 13:15 ET) with a non-`CLOSE` spot, else the tracker run-log spot of such a slot (`SETTLED`, intrinsic value; the result records `spot_source`, `market_data_type`, `spot_from` = `SNAPSHOT` / `TRACKER_RUN_LOG` and the run-log line), else `UNRESOLVED`; `NOT_REACHED` when the data ends first.
- **Reproducibility:** engine version, SHA-256 of `strategies.py` (line endings normalized), Strategy Lab constants, quality thresholds, risk-free rate, walls `top_n`, entry and step snapshot ids. Same inputs give the same output.
- **Known limit (solved by tracked quotes, §13):** snapshots only hold 30-120 DTE contracts, so without the position tracker a ~38 DTE spread becomes `INCOMPLETE` about 8 days after entry.
- **Engine version 2: tracked quotes (`ENGINE_VERSION = "2"`).**
  - *Slot pairing:* a slot is `(ticker, planned_slot_utc)`, carried by both `collector_runs.jsonl` (with `snapshot_id`, `attempt`) and `tracker_runs.jsonl` (with `tracked_snapshot_id`). A normal snapshot is paired with the tracked file of its slot only if it is the slot's **first** snapshot (lowest attempt with a snapshot): the tracker runs right after the first attempt that reached the Gateway, so a later **retry snapshot of the same slot gets no tracked fallback** (marked from its own rows only). Manual snapshots have no slot and are never paired. Timestamps are never used for pairing.
  - *Per leg:* marked from the normal snapshot when the leg is there (also when it has no price); only a `MISSING_*` leg is marked from the paired tracked row. `LegMark.quote_source` is `SNAPSHOT` or `TRACKED`; a step with both gets the informational `MIXED_QUOTE_SOURCES`. The price fallback chain is unchanged (same Strategy Lab parity test).
  - *Step kinds (`StepResult.kind`):* `SNAPSHOT`; `TRACKED_ONLY` (a slot with a tracked file but no normal snapshot: spot, spot source and data type from the tracker run log, so no `SPOT_SOURCE_UNKNOWN`; `TRACKED_ONLY_STEP` flag; no `FILTER_CHANGED`; legs absent from the file are `MISSING_NO_DATA`); `GAP` (a tracker `MISSED` / `FAILED` / `SKIPPED` entry of a spread holding these legs and no file at all: spot, data type and coverage empty, every leg `MISSING_NO_DATA`, always `INCOMPLETE`, listed but **not counted** in the summary, `summary.steps_gap`). Tracker steps are kept only when they concern the backtested legs (a relevant spread's run-log entry, or a tracked row of a leg); a relevant spread is a tracked spread whose legs include every backtested leg contract.
  - *Coverage of a `TRACKED_ONLY` step:* the same quote definition (`measure_quote_quality`: valid positive bid or ask) over the rows of the tracked file, i.e. every tracked leg of that ticker in that slot. It is not comparable to chain coverage (2-20 contracts instead of a chain; one unquoted leg can drop it below 0.90) but feeds the same `LOW_QUOTE_COVERAGE` rule; the other `UNRELIABLE` rules (frozen data, off-session, fallback price, out-of-bounds value) are unchanged. Mixed steps keep the normal snapshot's coverage, spot and data type.
  - *Tracker gap flags (step `INCOMPLETE`, never estimated):* `TRACKER_MISSED`, `TRACKER_FAILED`, `TRACKER_SKIPPED`, `TRACKER_NO_QUOTE`, `TRACKER_PARTIAL` (from the relevant spread's run-log status), `TRACKER_FILE_MISSING` (file named in the run log is missing or unreadable), `TRACKER_STOPPED` (the relevant spread was stopped before the step and has no entry in that slot). Informational: `MIXED_QUOTE_SOURCES`, `TRACKED_ONLY_STEP`, `CALENDAR_UNKNOWN` (date beyond the calendar table; weekdays then count as trading days).
  - *Calendar:* `OFF_SESSION`, the expiry close and the settlement time use `session_for()` from `src/collector/calendar.py` (holidays: no session, so a step on a holiday is `OFF_SESSION`; early closes: session 09:30-13:00 ET, settlement at/after 13:15 ET).
  - *Robust logs:* a run-log line that does not parse (e.g. half-written while the app appends) is skipped; it is listed in `notes` and `reproducibility.skipped_run_log_lines`. Tracked files without a run-log entry are not used (their slot is unknown) and are reported in `notes`.
  - *Reproducibility:* `tracked_data_used`, `tracked_files`, `tracker_run_entries` (`tracker_runs.jsonl:L<n>`, stable because the log is append-only), `skipped_run_log_lines`; per step `slot_utc`, `tracked_snapshot_id`, `tracker_run_entries`.
  - *Registry helper:* `backtest_tracked_spread(repo, spread_id)` reads the legs, quantity and entry snapshot from `tracked_spreads.json` and runs mode `LEGS` (equal to a direct mode-A call; an AUTO pick's legs are the Auto-Builder's, and mode `LEGS` with them equals `AUTO_BUILD`, so no new entry mode).
  - *Regression:* without tracker data the output equals engine version 1 (`tests/fixtures/backtest_v1_golden.json`, generated before the change) except `engine_version`, the new empty-by-default fields and **one deliberate difference: early-close and holiday dates** (version 1 used 09:30-16:00 Mon-Fri and a fixed 16:15 ET settlement time, so a 13:15 ET early-close snapshot was an open step and settlement was `UNRESOLVED`; the calendar texts in `quality_thresholds` changed accordingly).

## 12. Scheduled Snapshot Collection (`src/collector/`) — `[IMPLEMENTED]`
- **Schedule (`calendar.py`, pure):** slots per trading day in America/New_York time (`zoneinfo`, DST-aware): interval slots from 09:45 ET (open + 15 min Type 3 delay) to 15:45 ET every `interval_min` (15 / 30 / 60 / 120; `0` = closing only; default 60), plus one `CLOSING` slot at close + 15 min (16:15 ET; 13:15 ET on 13:00 early closes). Weekends and NYSE holidays have no slots. Holiday and early-close dates for 2026-2027 are copied from the official NYSE calendar (https://www.nyse.com/markets/hours-calendars); after 2027-12-31 weekdays are treated as trading days and the status shows a warning.
- **Loop (`service.py`, `CollectorService`):** started and stopped in the FastAPI lifespan (`src/main.py`). It waits for the next slot (woken early by a settings change), then runs one cycle: tickers one after another through `AnalyzerWebService.refresh_chain_locked` with the ticker's pinned `range_pct`, numeric `strike_step` and DTE window and `collection_mode="SCHEDULED"` — the same code, filters, pages of `<= 80` and pacing as a manual refresh. Each ticker has a 240 s timeout.
- **One refresh at a time:** `AnalyzerWebService.refresh_lock` (an `asyncio.Lock`) is shared by manual refreshes and the collector. A cycle waits up to 120 s for a manual refresh, then records the slot as `SKIPPED`. A manual refresh during a cycle returns HTTP `409` `COLLECTOR_BUSY` (`RefreshBusyError`, with `retry_after_sec`); a second manual refresh waits up to 90 s, then `409 REFRESH_BUSY`.
- **Corridor streams:** a cycle is postponed while a corridor stream is active (polled every 30 s, at most 10 min); then the refresh stops the stream (IBKR 100-line budget). The run log keeps both the planned slot and the actual run time; `last_corridor_stop` in the status lets the dashboard explain the stop.
- **Failures and quality:** Gateway down, timeout or a fetch error -> `FAILED`, retried once 5 min after the pass, then the next slot (no tight loop). Low quote coverage (< 0.90) -> the snapshot is kept and flagged `LOW_QUALITY` and that ticker is retried once after 2 min (both snapshots kept); frozen / off-session data is flagged but not retried. Nothing is dropped. `pin_drift` is set when the effective step or range differs from the pin (e.g. widened to fit the 400-contract cap). Snapshot files are written atomically by `CSVRepository`, so a failed fetch leaves no file.
- **Settings and log (`store.py`):** `data/metadata/collector_settings.json` (atomic write: `enabled`, `interval_min`, `tickers` with `pinned_since_utc`, `enabled_since_utc`) and `data/metadata/collector_runs.jsonl` (one `RunRecord` per ticker attempt: planned slot, actual run time, status `OK` / `LOW_QUALITY` / `FAILED` / `SKIPPED` / `MISSED`, snapshot id, coverage, requested vs effective step, error). On startup the last on/off state is restored (default off on a fresh install) and slots that passed while the app was down are recorded as `MISSED` (up to 7 days back).
- **API:** `GET /api/collector/status` (state, next run and next closing snapshot in UTC / ET / Poland time, today's session, last cycle, last result per ticker, last error, snapshots and gaps today, missed slots, calendar source and validity, `last_corridor_stop`) and `PUT /api/collector/settings` (`enabled`, `interval_min`, up to 6 `tickers` with `symbol`, `range_pct` from `allowed_range_pcts`, `strike_step` from `allowed_strike_steps`, `dte_min` / `dte_max` within 0-120; invalid input -> 422).

## 13. Position Tracker (`src/tracker/`) — `[IMPLEMENTED]`
Keeps quoting the legs of open bull put spreads on every collection slot, so a spread can be followed to expiry after its contracts leave the 30-120 DTE window of the normal snapshots. Hold to expiry only; no exit rules.

- **Sources:**
  - `AUTO`: after a collected snapshot, `TrackerService.auto_pick` takes the first **usable** snapshot (`evaluate_snapshot`) of the market date whose time is at/after `tracker.auto_pick_not_before_et` (default `10:45` ET; opening quotes are noisy) and runs the Strategy Lab Auto-Builder exactly like backtest mode `AUTO_BUILD`: `_enrich` (BSM Greeks), `compute_walls(top_n=WALLS_TOP_N)`, `evaluate_strategy("BULL_PUT_SPREAD", target_expiry=None)`, no corridor overlay (helpers imported from `src/analytics/backtest.py`; neither module is changed). One decision per ticker per market date (kept across restarts via the run log): `REGISTERED`, `AUTO_DUPLICATE` (same legs as an ACTIVE spread of the ticker), `AUTO_SKIPPED_CAP` or, at the closing slot, `AUTO_SKIPPED_NO_USABLE`. A new pick is quoted in the same slot.
  - `USER`: `POST /api/tracker/spreads` with `ticker`, `snapshot_id` and two legs, validated like backtest mode `LEGS` (`validate_bull_put_legs`: 2 puts, one expiry, SELL above BUY, different strikes, equal positive quantity), `TickerValidator`, expiry not in the past, and both contracts present in that saved snapshot (their `conId` is taken from it). Duplicate legs and the cap give HTTP `409`.
- **Quoting (`track_ticker`):** the collector calls the tracker once per slot per ticker, after the first attempt that reached the Gateway (attempt 1, or the retry when attempt 1 failed with `GATEWAY_OFFLINE`), while it still holds `refresh_lock`. Tracked spreads on tickers the collector does not snapshot are quoted after the collected tickers. Contracts shared by several spreads are requested once; the broker call is `PagedOptionChainFetcher.fetch_exact_contracts` (spot + exact `conId`s, same pacing and fallback as a chain page). One immutable file per ticker per slot in `data/tracked/{TICKER}/`, plus one `tracker_runs.jsonl` line per spread with the spot, spot source and market data type even when no leg has a quote (`OK` / `PARTIAL` / `NO_QUOTE` / `FAILED`). Spreads not reached in a slot get `FAILED` (Gateway down all slot) or `SKIPPED` (lock busy). No tracker retries; a tracker failure never triggers collector retries or stops other tickers.
- **Lifecycle:** `ACTIVE` until the expiry date; on that date the closing slot (16:15 ET, 13:15 ET on early closes) is still quoted, then the spread becomes `EXPIRED`. `POST /api/tracker/spreads/{id}/stop` sets `STOPPED` and keeps the recorded files. On startup, slots missed while the app was down get `MISSED` records per spread active at that time (only when collection was on), and spreads whose expiry passed are set to `EXPIRED`. The tracker only runs inside collector slots: with collection off nothing is quoted.
- **Cap:** `tracker.max_active_spreads` (default `120`, `1-200`). `GET /api/collector/status` gains a `tracker` object (`active_count`, `max_active_spreads`, `auto_pick`, `auto_pick_not_before_et`, `warnings`, `last_result`, `last_auto`, `registry_error`); a warning is shown above 80% of the cap and when an AUTO pick was skipped at the cap in the last 7 days.
- **Cost per slot and ticker:** with `C` distinct leg contracts: 2 spot messages + `2C` market-data messages (no qualification: `conId`s come from the snapshot), paced 20 per 0.55 s; about 3.5-6 s (spot poll `<= 2.25` s, one page poll `<= 2.25` s, two pacing pauses), about 11 s with a Type 3 -> 4 fallback; a second page only above 80 contracts.
- **API:** `GET /api/tracker/spreads?status=ACTIVE|EXPIRED|STOPPED` (`active_count`, `max_active_spreads`, `spreads` with `last_quote_utc`), `POST /api/tracker/spreads` (`201`), `POST /api/tracker/spreads/{id}/stop`. The full management UI comes later with the backtest panel; the collector panel shows the tracked count, last tracker result and warnings.
- **Config (`tracker:` in `config/settings.yaml`, `TrackerSettings`):** `max_active_spreads`, `auto_pick`, `auto_pick_not_before_et` (`HH:MM`), `tracked_dir` (inside `data/`, never `data/snapshots`).
- **Backtest engine:** reads the tracked quotes since engine version 2 (see §11): legs missing from a slot's first normal snapshot are marked from the tracked file of the same slot, slots with only tracked data become `TRACKED_ONLY` steps, tracker gaps become `GAP` steps, and `backtest_tracked_spread(repo, spread_id)` backtests a tracked spread by registry id.
