# Security & Read-Only Safety Specification (`SECURITY_AND_SAFETY.md`)

## 1. Zero-Trust Read-Only Execution Mandate
This application is strictly a **read-only analytical terminal**. Because the default configuration targets **Port `4001` (IBKR Gateway Live Session)** — or any configured `IBKR_HOST:IBKR_PORT` socket (e.g., `7496`/`7497`) — read-only execution is enforced at the code level through **three independent defense-in-depth layers**.

### Layer 1: Runtime Method Blocking (`ReadOnlyIBGuard` in `src/core/security.py`)
- Feature modules MUST NEVER instantiate `ib_async.IB()` directly.
- Immediately upon instantiating `ib_async.IB()` inside `src/broker/client.py`, `ReadOnlyIBGuard.lock(ib)` overwrites all order-routing and state-mutating methods on both the `IB` instance and its underlying `ib.client` socket object with a trap function that raises `SecurityViolationError`:
  - `placeOrder`
  - `cancelOrder`
  - `reqGlobalCancel`
  - `exerciseOptions`
  - `whatIfOrder`
  - `whatIfOrderAsync`
- `ReadOnlyIBGuard.verify_locked(ib)` returns `True` only if every blocked method on both targets is a trap. Config additionally refuses `ibkr.read_only_enforced: false` (`ConfigurationError`).
- **Mandatory Unit Test (`tests/unit/test_security.py`):** Must invoke every blocked method on a guarded `IB` instance and verify that `SecurityViolationError` is raised with zero network socket writes.

### Layer 2: IBKR Gateway Configuration Checklist
Users running IBKR Gateway on `IBKR_PORT` (default `4001`, or TWS/custom port) should verify in **Configure -> Settings -> API -> Settings**:
1. **Read-Only API:** Checked (`[x]`).
2. **Socket Port:** Matches configured `IBKR_PORT` (default `4001`).
3. **Master API client ID:** Blank (or not conflicting with `CLIENT_ID=77`).

### Layer 3: Static Analysis CI Gate (`Makefile`)
- `make check` runs a static grep verification confirming that `.placeOrder`, `.cancelOrder`, and `.exerciseOptions` do not appear anywhere in `src/` outside `src/core/security.py`.

---

## 2. Ticker Input Validation & Injection Prevention

### 2.1 Dual-Source Ticker Policy
The application supports two ticker selection methods:
1. **Hardcoded Watchlist:** `["NVDA", "AVGO", "CRDO", "PLTR"]` (defined in `config/settings.yaml`).
2. **Dynamic Custom Stock Input Box:** Allows typing any US Stock/ETF symbol in the UI.

### 2.2 Strict Sanitization Pipeline (`TickerValidator` in `src/core/security.py`)
Every ticker input from any source (UI form, URL parameter, query string, or config) MUST pass through `TickerValidator.validate(raw_input: str) -> str` (a classmethod) before being used in file paths or IBKR calls. When `watchlist.allow_custom_ticker` is `false`, use the instance method `TickerValidator(watchlist, allow_custom).validate_selection(raw)`, which additionally enforces watchlist membership.
1. **Normalization:** Confirm input is a `str`, strip whitespace, require pure ASCII (rejects Unicode look-alikes such as full-width letters), convert to uppercase.
2. **Strict Regex Gate:** Must match `^[A-Z]{1,5}$` (1 to 5 uppercase ASCII letters only).
   - Rejects path traversal (`..`, `/`, `\`), shell metacharacters (`;`, `|`, `$`, `&`), whitespace, numbers, and punctuation with `InvalidTickerError` (HTTP `422`).
3. **IBKR Qualification Gate:** Before creating any directory under `data/snapshots/{TICKER}/` or querying option parameters, the broker client must qualify the contract as a US Stock:

    Contract(secType="STK", symbol=validated_symbol, exchange="SMART", currency="USD")

   If `qualifyContractsAsync` returns empty or fails, execution aborts with `InvalidTickerError` and nothing is written to disk.

---

## 3. Cross-Platform Docker & Localhost Port Safety (Windows & macOS)

### 3.1 Localhost-Only Web Binding
- Native execution (`make run`) binds Uvicorn strictly to `127.0.0.1:8000`.
- Docker execution (`docker-compose.yml`) binds host port mapping strictly to `127.0.0.1:8000:8000` so the web UI is never exposed to external network interfaces.

### 3.2 Docker-to-Host IBKR Gateway Networking (`Port 4001`)
When running inside Docker on Windows or macOS (`make docker-up`):
- The container connects to IBKR Gateway on the host machine via `IBKR_HOST=host.docker.internal` and `IBKR_PORT=4001`.
- Because traffic from `host.docker.internal` arrives at IBKR Gateway from the local Docker bridge subnet (e.g., `172.17.0.1` or `192.168.65.1` / `192.168.65.254`) rather than `127.0.0.1`, users running via Docker must either:
  1. Uncheck *"Allow connections from localhost only"* in IBKR Gateway API Settings and add `127.0.0.1` plus the local Docker bridge IP to **Trusted IP Addresses** (while keeping **Read-Only API** checked), OR
  2. Run the app natively on the host via `make run`, which connects directly over `127.0.0.1:4001`.

---

## 4. Credential & Account Privacy Isolation
- **No Credentials Stored:** The application never requests, stores, or logs IBKR usernames, passwords, or 2FA codes.
- **No Account Queries:** The application never calls `reqAccountSummary`, `reqPositions`, `reqPnL`, or `managedAccounts`. Local CSV files contain exclusively market option chain data.

---

## 5. Market-Data Pacing and Line Budget (`src/broker/fetcher.py`, `src/web/api.py`)
- **Message rate:** `ib_async` sends at most `45` messages per `1s` window (`Client.MaxRequests`) and queues the rest; IBKR allows half the account's market-data lines per second (`50` with the default `100` lines). The chain fetcher sends `reqMktData` and `cancelMktData` in bursts of `20` with a `0.55s` pause before each burst (`<= 40` per second), so its requests never wait in the `ib_async` queue.
- **Line budget:** IBKR grants `100` simultaneous market-data lines by default, shared with TWS watchlists and every API client (error `101` when exceeded). One chain page uses `<= 80` lines; the underlying spot subscription is cancelled before the first page; all page lines are cancelled before the next page starts. `POST /api/refresh` stops any corridor stream (`<= 60` lines) before fetching. Lines used in TWS itself still count against the same `100`.
- **Cancels are guaranteed:** every subscription is cancelled in `finally`; an interrupted pause never skips a cancel.

---

## 6. Scheduled Collection Background Task (`src/collector/`)
- **Same read-only path:** the collector only calls `AnalyzerWebService.refresh_chain_locked`, the code behind `POST /api/refresh`: `ReadOnlyIBGuard`, chain filtering (DTE window, `range_pct`, `strike_step`), pages of `<= 80`, pacing and `finally` cancels all apply unchanged. It never places, modifies or cancels orders.
- **Tickers:** every collected ticker is validated by `TickerValidator` when the settings are saved (`TickerPin`) and again before each refresh (`validate_ticker`).
- **No overlap:** one `asyncio.Lock` serializes manual and scheduled refreshes, so only one page (`<= 80` lines) is subscribed at a time; corridor streams are stopped before a refresh (after up to 10 min of postponement).
- **Event loop:** the task uses `asyncio` waits only; no blocking calls. Errors are caught per ticker and logged; the loop never stops on an error.
- **Files:** collector state lives in `data/metadata/collector_settings.json` and `data/metadata/collector_runs.jsonl`; nothing is written to `data/snapshots/` except new immutable snapshots through `CSVRepository`.

---

## 7. Position Tracker (`src/tracker/`, `PagedOptionChainFetcher.fetch_exact_contracts`)
- **Only market data for specific contracts:** the tracker's one broker call is `fetch_exact_contracts(ticker, specs, underlying_conid=...)`: one stock `reqMktData` for the spot (cancelled in `finally`), then `reqMktData` / `cancelMktData` for the listed leg contracts through the same paced pages as a chain refresh (bursts of `20`, `0.55s` pauses, pages of `<= 80`, cap `80 x 5 = 400`, `finally` cancels, Type 3 -> 4 / 1 -> 2 fallback). `qualifyContractsAsync` is called only for a contract without a known `conId` (tracked legs always carry the `conId` from the snapshot). It never calls `reqSecDefOptParams` or requests a chain, and it never places, modifies or cancels orders: all calls go through `ReadOnlyIBGuard`.
- **Tickers:** validated with `TickerValidator` when a spread is registered, again before every quote request and in `make_snapshot_id` / `TrackerStore.quote_path` before any file path.
- **No overlap:** quoting runs inside the collector's slot while it holds `refresh_lock`, after the ticker's snapshot (all chain-page lines are already cancelled), so line use stays under the `100`-line budget.
- **Failures:** a tracker error is caught and logged (`FAILED` record); it never stops the collector, never triggers a collector or tracker retry and never leaves a partial file.
- **Files:** `data/tracked/` (validated to be inside `data/` and outside `data/snapshots/`) plus `data/metadata/tracked_spreads.json` and `tracker_runs.jsonl`; tracked-quote files are immutable.
