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
- `make check` runs `make security-guard` (`scripts/security_guard.py`), a static scan of `src/**/*.py` (excluding `src/core/security.py`) that fails on:
  - Order/mutation calls: `.placeOrder(`, `.cancelOrder(`, `.reqGlobalCancel(`, `.exerciseOptions(`, `.whatIfOrder(`, `.whatIfOrderAsync(`.
  - Account/position queries: `.reqAccountSummary*(`, `.reqAccountUpdates*(`, `.reqPositions*(`, `.reqPnL*(`, `.managedAccounts(`, `.reqExecutions*(`, `.reqOpenOrders*(`, `.reqAllOpenOrders*(`.
  - **Strict `SCOPED_ALLOWLIST` in `scripts/security_guard.py`:** permits **only** `.reqExecutionsAsync(` and `.reqPositionsAsync(` inside the single file `src/broker/ledger_sync.py`. Sync variants (`.reqExecutions(`, `.reqPositions(`), all other account/order APIs, and `reqExecutionsAsync`/`reqPositionsAsync` in every other `src/` module remain blocked and fail CI (`tests/unit/test_security_guard.py`).
  - **Runtime Ledger Gating & Mark Cap:** `fetch_gateway_ledger()` in `src/broker/ledger_sync.py` refuses to execute unless `ENABLE_GATEWAY_LEDGER_SYNC=true` (`settings.ledger.gateway_sync_enabled`, default `false`, raising `ConfigurationError` -> HTTP `422 CONFIGURATION_ERROR`); `refresh_open_option_marks()` is capped at `MAX_MARK_CONTRACTS = 60` (`conid`-reused `reqMktData` snapshots with `1 -> 3 -> 4` / `LIVE 1 -> 2` / `DELAYED 3 -> 4` fallback, cancelling every contract in `finally:` even when a pacing sleep raises `CancelledError`).
  - **Exact-Contract Follow-Up & Burst Pacing (`src/broker/fetcher.py`):** `fetch_exact_contracts()` shares the same read-only `reqMktData`/`cancelMktData` collector path (`snapshot=False`, zero order/account calls, keeping `scripts/security_guard.py` unchanged), deduplicates active paper-trade legs, skips expired contracts (`expiry < market_day`), and caps requests at `max_contracts_per_page * max_pages_per_refresh` (`400` contracts, `PacingViolationError`). Both paged chain pulls and exact-contract pulls pace IBKR bursts via `PAGE_PACING_CHUNK_SIZE = 20` and `PAGE_PACING_PAUSE_SEC = 0.55`, cancelling every subscribed ticker in `finally: await self._cancel_paced(...)` while deferring `BaseException` (including `CancelledError`) until 100% of cancels execute.
  - **Single-Flight Concurrency & 100-Line Ceiling Coordination (`src/services/analyzer.py`, `src/services/ledger.py`):** IBKR enforces a 100-simultaneous market-data-line ceiling per session. `AnalyzerService.refresh_lock` (`asyncio.Lock`) serializes all IBKR market-data pulls (`refresh_chain`, `fetch_corridor_quotes`, Gateway-backed `LedgerService.sync_ledger`, and `LedgerService.refresh_open_mtm`), rejecting overlapping calls with `RefreshBusyError` (HTTP `409 REFRESH_BUSY`, `Retry-After: 10`), and explicitly calls `corridor.stop_all_streams()` (cancelling up to 60 streaming corridor lines and awaiting brief drain) before subscribing new chain, ledger MTM, or paper-trade lines. `open_corridor_stream` also rejects SSE stream opens with `RefreshBusyError` while `refresh_lock` is locked.
  - Event-loop blocking `time.sleep(` and `pandas` imports.
- Diffs touching `src/core/`, `src/broker/`, `src/storage/`, `src/services/`, `src/web/{api,backtest_api,errors}.py`, or `scripts/security_guard.py` are additionally reviewed by the read-only `safety-reviewer` sub-agent (`.agent/agents/safety-reviewer.md`) before commit.

---

## 2. Ticker Input Validation & Injection Prevention

### 2.1 Dual-Source Ticker Policy
The application supports two ticker selection methods:
1. **Hardcoded Watchlist:** `["NVDA", "AVGO", "CRDO", "PLTR"]` (defined in `config/settings.yaml`).
2. **Dynamic Custom Stock Input Box:** Allows typing any US Stock/ETF symbol in the UI.

### 2.2 Strict Sanitization Pipeline (`TickerValidator` in `src/core/security.py`)
Every ticker input from any source (UI form, URL parameter, query string, JSON payload, or config) MUST pass through `TickerValidator.validate(raw_input: str) -> str` (a classmethod) before being used in file paths or IBKR calls — including `PaperTradeStore`, `PaperTradeService`, `backtest_store`, and `/api/backtest/*` endpoints (where `trade_id` is additionally constrained by `PAPER_TRADE_ID_RE` and snapshot IDs by `SNAPSHOT_ID_RE`). When `watchlist.allow_custom_ticker` is `false`, use the instance method `TickerValidator(watchlist, allow_custom).validate_selection(raw)`, which additionally enforces watchlist membership.
1. **Normalization:** Confirm input is a `str`, strip whitespace, require pure ASCII (rejects Unicode look-alikes such as full-width letters), convert to uppercase.
2. **Strict Regex Gate:** Must match `^[A-Z]{1,5}$` (1 to 5 uppercase ASCII letters only).
   - Rejects path traversal (`..`, `/`, `\`), shell metacharacters (`;`, `|`, `$`, `&`), whitespace, numbers, and punctuation with `InvalidTickerError` (HTTP `422`).
3. **Storage Path Boundary Guard (`Settings._check_tracked_dir` in `src/core/config.py`):** `PaperTrackerSettings.tracked_dir` (default `data/tracked`) is resolved via `resolve_project_path()` and strictly validated to remain inside `storage.data_dir` and never equal to or inside `storage.snapshots_dir`, raising `ConfigurationError` on violation so tracked leg quotes can neither escape the project data tree nor pollute full-chain snapshot discovery.
4. **IBKR Qualification Gate:** Before creating any directory under `data/snapshots/{TICKER}/` or querying option parameters, the broker client must qualify the contract as a US Stock:

    Contract(secType="STK", symbol=validated_symbol, exchange="SMART", currency="USD")

   If `qualifyContractsAsync` returns empty or fails, execution aborts with `InvalidTickerError` and nothing is written to disk.

---

## 3. Cross-Platform Docker & Localhost Port Safety (Windows & macOS)

### 3.1 Localhost-Only Web Binding
- Native execution (`make run`) binds Uvicorn strictly to `127.0.0.1:8000`.
- Docker execution (`docker-compose.yml`) binds host port mapping strictly to `127.0.0.1:8000:8000` so the web UI is never exposed to external network interfaces.

### 3.1.1 Container Hardening (`Dockerfile`, `.dockerignore`)
- Multi-stage build: dependencies resolved only from `uv.lock` (`uv sync --locked --no-dev --no-install-project`); no dev/test tooling, compilers, or `curl` in the runtime image.
- Runs as the unprivileged `app` user (UID/GID `10001`); only `/app/data` (bind-mounted `./data`) is writable, and `./config` is mounted read-only.
- `.dockerignore` is an allow-list (`pyproject.toml`, `uv.lock`, `config/`, `src/`), so `.env`, `data/`, `.git`, and local virtualenvs never enter the build context or image layers.
- `HEALTHCHECK` polls `GET /api/status` via the Python standard library (works while the Gateway is offline).

### 3.2 Docker-to-Host IBKR Gateway Networking (`Port 4001`)
When running inside Docker on Windows or macOS (`make docker-up`):
- The container connects to IBKR Gateway on the host machine via `IBKR_HOST=host.docker.internal` and `IBKR_PORT=4001`.
- Because traffic from `host.docker.internal` arrives at IBKR Gateway from the local Docker bridge subnet (e.g., `172.17.0.1` or `192.168.65.1` / `192.168.65.254`) rather than `127.0.0.1`, users running via Docker must either:
  1. Uncheck *"Allow connections from localhost only"* in IBKR Gateway API Settings and add `127.0.0.1` plus the local Docker bridge IP to **Trusted IP Addresses** (while keeping **Read-Only API** checked), OR
  2. Run the app natively on the host via `make run`, which connects directly over `127.0.0.1:4001`.

---

## 4. Credential & Account Privacy Isolation
- **No Credentials Stored on Disk or in Responses:** The application never requests, stores, or logs IBKR usernames, passwords, or 2FA codes. `IBKR_FLEX_TOKEN` (`LedgerSettings.flex_token`, `Field(repr=False)`) is supplied exclusively via `.env` / environment variables (never `config/settings.yaml`, `data/ledger/sync_state.json`, or `/api/status` payloads) and is redacted along with URL `t=...` query strings by `redact_flex_secrets()` (`src/broker/flex.py`) before any `FlexServiceError` (`502 FLEX_SERVICE_ERROR`), `StorageSchemaError` (`422 STORAGE_SCHEMA_ERROR`), or `ConfigurationError` (`422 CONFIGURATION_ERROR`) detail is logged or returned.
- **Scoped Account-Free Ledger Sync & Redaction:** Core option-chain workflows never call `reqAccountSummary`, `reqPositions`, `reqPnL`, `managedAccounts`, or `reqOpenOrders`, and `ib.client.connectAsync` always bypasses `IB.connectAsync` account/order hydration. The opt-in ledger path (`ENABLE_GATEWAY_LEDGER_SYNC=true` in `src/broker/ledger_sync.py`) drops every `account` attribute immediately, and `parse_flex_xml`, `parse_activity_csv`, and `redact_flex_secrets` strip/redact XML tags and both quoted and unquoted IBKR account patterns (`acctId`, `accountId`, `acctCode`, `account`, `ClientAccountID`) before persistence or error surfacing (`data/ledger/` is excluded from git via `.gitignore`).
- **Safe Manual File Uploads:** `POST /api/ledger/import` performs a bounded read (`await file.read(MAX_LEDGER_IMPORT_BYTES + 1)` with `finally: await file.close()`, where `MAX_LEDGER_IMPORT_BYTES = 20 * 1024 * 1024`), strips POSIX `/` and Windows `\` path separators and requires `.xml` / `.csv` (`InvalidRequestError` `422`), rejects NUL bytes and `(ValueError, csv.Error)` / malformed payloads (`StorageSchemaError` `422 STORAGE_SCHEMA_ERROR`), and validates every underlying symbol via `normalize_underlying` (`TickerValidator`).