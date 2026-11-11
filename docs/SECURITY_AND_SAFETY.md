# Security & Read-Only Safety Specification (`SECURITY_AND_SAFETY.md`)

## 1. Zero-Trust Read-Only Execution Mandate
This application is strictly a **read-only analytical terminal**. Because the default configuration targets **Port `4001` (IBKR Gateway Live Session)**, read-only execution is enforced at the code level through **three independent defense-in-depth layers**.

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
Users running IBKR Gateway on Port `4001` should verify in **Configure -> Settings -> API -> Settings**:
1. **Read-Only API:** Checked (`[x]`).
2. **Socket Port:** `4001`.
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