# Data Persistence & CSV Schema Specification (`DATA_SCHEMA.md`)

## 1. Storage Architecture & Mode 1 Archival Strategy
All option chain snapshots are persisted locally as structured UTF-8 CSV files managed by `Polars` and validated by `Pydantic`. This satisfies two requirements simultaneously:
1. **Mode 1 Historical Archive:** Every manual refresh creates a permanent, immutable timestamped CSV file so historical snapshots can be browsed or compared later.
2. **Offline Cache:** The UI boots and renders immediately from local CSVs even when IBKR Gateway is closed.

### 1.1 Directory Hierarchy

    data/
    ├── snapshots/
    │   ├── NVDA/
    │   │   ├── NVDA_20261111_143512.csv
    │   │   └── NVDA_20261111_155004.csv
    │   ├── AVGO/
    │   │   └── AVGO_20261111_160219.csv
    │   ├── CRDO/
    │   └── PLTR/
    ├── tracked/
    │   └── NVDA/
    │       └── NVDA_20261112_153000.csv   # immutable paper-trade leg quotes (outside snapshots/)
    ├── latest/
    │   ├── NVDA_latest.csv
    │   ├── AVGO_latest.csv
    │   ├── CRDO_latest.csv
    │   └── PLTR_latest.csv
    ├── metadata/
    │   ├── snapshot_index.json
    │   ├── paper_trades.json
    │   └── paper_quotes.jsonl
    └── ledger/
        ├── executions.csv
        ├── open_positions.csv
        ├── sync_state.json
        └── imports/

### 1.2 File Naming & Atomic Write Protocol
- **Archive Filename Pattern:** `data/snapshots/{TICKER}/{TICKER}_{YYYYMMDD_HHMMSS}.csv` (UTC timestamp).
- **Latest Cache Filename Pattern:** `data/latest/{TICKER}_latest.csv` (full file copy rather than OS symlink to ensure 100% compatibility across Windows, macOS, and Docker bind mounts).
- **Atomic Write Sequence (`CSVRepository.save_snapshot`):**
  1. Validate rows via `Pydantic` and build a strict `polars.DataFrame`.
  2. Write to temporary hidden file `data/snapshots/{TICKER}/.{TICKER}_{YYYYMMDD_HHMMSS}.csv.tmp`.
  3. Call `os.replace()` to atomically move the temporary file to `data/snapshots/{TICKER}/{TICKER}_{YYYYMMDD_HHMMSS}.csv`.
  4. Write to `data/latest/.{TICKER}_latest.csv.tmp` and `os.replace()` to `data/latest/{TICKER}_latest.csv`.
  5. Update `data/metadata/snapshot_index.json` atomically via `.tmp` + `os.replace()`.

---

## 2. Canonical Option Chain CSV Schema (`27 Columns`)
To minimize IBKR API calls and prevent multi-file sync bugs, each refresh writes **one unified 27-column CSV** containing normalized contract quotes and Greeks. Derived analytics (Put/Call Walls, Unusual Activity, Multi-DTE curves) are computed in memory by `Polars` in `<5ms` when loading the CSV.

| # | Column Name | Polars Dtype | Nullable | Description & Domain Rules |
| :- | :--- | :--- | :--- | :--- |
| 1 | `snapshot_id` | `Utf8` | No | Unique snapshot identifier: `{TICKER}_{YYYYMMDD_HHMMSS}`. |
| 2 | `snapshot_ts_utc` | `Utf8` | No | ISO-8601 UTC timestamp (`2026-11-11T15:30:00Z`). |
| 3 | `market_date` | `Utf8` | No | US Eastern market session date (`YYYY-MM-DD`). |
| 4 | `market_data_type` | `Int64` | No | IBKR market data code used: `3` (Delayed 15m), `4` (Delayed-Frozen), `1` (Live), `2` (Frozen). |
| 5 | `underlying` | `Utf8` | No | Validated stock symbol (`NVDA`, `AVGO`, `CRDO`, `PLTR`, etc.). |
| 6 | `underlying_conid` | `Int64` | No | IBKR `conId` of the underlying stock. |
| 7 | `underlying_price` | `Float64` | No | Underlying spot/reference price (`> 0.0`) at snapshot time. |
| 8 | `option_conid` | `Int64` | No | IBKR unique `conId` for this specific option contract. |
| 9 | `expiry` | `Utf8` | No | Option expiration date normalized to `YYYY-MM-DD`. |
| 10 | `dte` | `Int64` | No | Calendar days to expiration from `market_date` (`0 <= dte <= 400` in schema; the broker filter selects weekly expirations strictly `30 <= dte <= 120`). |
| 11 | `strike` | `Float64` | No | Option strike price (`> 0.0`). |
| 12 | `moneyness_pct` | `Float64` | No | Signed percentage from spot: `((strike - underlying_price) / underlying_price) * 100.0`. |
| 13 | `right` | `Utf8` | No | Option type: strictly `"C"` (Call) or `"P"` (Put). |
| 14 | `bid` | `Float64` | Yes | Best bid price (`null` if IBKR returns `<= 0.0`, `-1.0`, or `NaN`). |
| 15 | `ask` | `Float64` | Yes | Best ask price (`null` if IBKR returns `<= 0.0`, `-1.0`, or `NaN`). |
| 16 | `mid` | `Float64` | Yes | `(bid + ask) / 2.0` when both `bid > 0` and `ask > 0`, else `null`. |
| 17 | `last` | `Float64` | Yes | Last traded price (`null` if `<= 0.0`, `-1.0`, or `NaN`). |
| 18 | `close` | `Float64` | Yes | Closing price (`null` if `<= 0.0`, `-1.0`, or `NaN`). |
| 19 | `mark_price` | `Float64` | Yes | Canonical option price: `mid` -> fallback `last` -> fallback `close`. |
| 20 | `volume` | `Int64` | Yes | Session contract volume (`>= 0`; `null` if unpopulated). |
| 21 | `open_interest` | `Int64` | Yes | Contract Open Interest from tick `101` (`null` if omitted by Type 3 delayed feed). |
| 22 | `iv` | `Float64` | Yes | Implied Volatility as decimal (`0.4520` = `45.20%`). |
| 23 | `delta` | `Float64` | Yes | Option Delta (`-1.0` to `1.0`). |
| 24 | `gamma` | `Float64` | Yes | Option Gamma (`>= 0.0`). |
| 25 | `theta` | `Float64` | Yes | Option daily Theta. |
| 26 | `vega` | `Float64` | Yes | Option Vega. |
| 27 | `greeks_source` | `Utf8` | No | `"IBKR_MODEL"`, `"BSM_FALLBACK"`, or `"NONE"`. |

---

## 3. Boundary Sanitization Rules (`src/broker/parser.py`)
IBKR uses sentinel numbers (`-1.0`, `float('nan')`, `2147483647`) for missing values on Type 3/4 delayed ticks. Before constructing `OptionContractRow`:
1. **Prices (`bid`, `ask`, `last`, `close`):** Any value `<= 0.0`, `NaN`, or `>= 1e9` MUST be converted to `None` (`null` in CSV).
2. **Integers (`volume`, `open_interest`):** Any value `< 0`, `NaN`, or `>= 2147483647` MUST be converted to `None`.
3. **Greeks (`iv`, `delta`, `gamma`, `theta`, `vega`):** If IBKR `modelGreeks` is `None` or contains out-of-range values (`iv <= 0` or `iv > 10.0`, `abs(delta) > 1.001`), set native Greeks to `None` and invoke `bsm_fallback.compute_missing_greeks()` using `mark_price`, `underlying_price`, `strike`, `dte`, and `risk_free_rate = 0.0425`.

---

## 4. Snapshot Metadata Index (`data/metadata/snapshot_index.json`)
Maintained automatically by `CSVRepository` for instant UI history dropdown rendering:

    {
      "updated_at_utc": "2026-11-11T15:50:04Z",
      "tickers": {
        "NVDA": {
          "latest_snapshot_id": "NVDA_20261111_155004",
          "latest_file": "data/latest/NVDA_latest.csv",
          "snapshots": [
            {
              "snapshot_id": "NVDA_20261111_155004",
              "file_path": "data/snapshots/NVDA/NVDA_20261111_155004.csv",
              "snapshot_ts_utc": "2026-11-11T15:50:04Z",
              "market_date": "2026-11-11",
              "market_data_type": 3,
              "underlying_price": 142.65,
              "contract_count": 120,
              "expirations": ["2026-12-18", "2026-12-24", "2026-12-31", "2027-01-08"],
              "range_pct": 0.20,
              "strike_step": 5.0,
              "requested_strike_step": 5.0,
              "spot_source": "LAST",
              "quoted_contract_count": 118,
              "quote_coverage": 0.9833,
              "prior_close_only_count": 2
            }
          ]
        }
      }
    }

`SnapshotMeta` defines optional quality/provenance fields alongside pull scope: `spot_source` (`SpotSource = Literal["LAST", "MID", "CLOSE"]`), `quoted_contract_count` (contracts with `bid > 0` or `ask > 0` or `last > 0`), `quote_coverage` (`quoted_contract_count / contract_count`), and `prior_close_only_count` (contracts with `close > 0` but no `bid`/`ask`/`last`). `src/storage/quality.py` uses these fields (`MIN_QUOTE_COVERAGE = 0.90`, `USABLE_SPOT_SOURCES = {"LAST", "MID"}`, `USABLE_MARKET_DATA_TYPES = {1, 3}`) to score each snapshot for daily-bar backtest selection and off-hours UI indicators.

If `snapshot_index.json` is missing or corrupted, `CSVRepository.rebuild_index()` scans `data/snapshots/*/*.csv` on startup and reconstructs the index automatically (inferring `strike_step` and `range_pct` from the CSV strike grid when possible, recomputing quote quality from each CSV, and preserving any prior `spot_source`).

---

## 5. Hybrid Trade Ledger Storage Contract (`data/ledger/`)

The Trade Ledger persists normalized execution fills and current open positions under `LEDGER_DIR` (default `data/ledger/`, excluded by `.gitignore`). `LedgerRepository.ensure_layout()` (`src/storage/ledger_repository.py`) provisions `data/ledger/` and `data/ledger/imports/` idempotently on startup, and writes `executions.csv`, `open_positions.csv`, and `sync_state.json` atomically via `.tmp` + `os.replace()`.

### 5.1 Executions Archive (`data/ledger/executions.csv`, `LedgerExecutionRow`)
Sorted by `(executed_at, exec_id)` with 23 columns (`LEDGER_EXECUTION_COLUMNS`, `LEDGER_EXECUTION_POLARS_SCHEMA` in `src/storage/ledger_schema.py`):

| # | Column | Polars Type | Null? | Validation & Semantics |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `exec_id` | `pl.Utf8` | No | IBKR `ibExecID`/`execID`/`execId`/`transactionID`/`tradeID`, or deterministic `sha256:{24hex}` via `synthesize_exec_id(...)` when absent. |
| 2 | `ib_order_id` | `pl.Utf8` | Yes | Optional IBKR order ID used for multi-leg order clustering. |
| 3 | `conid` | `pl.Int64` | Yes | Option/stock IBKR `conId` (`> 0` and `< 2147483647`, sanitized via `sanitize_optional_conid`). |
| 4 | `underlying_conid` | `pl.Int64` | Yes | Underlying stock `conId`. |
| 5 | `symbol` | `pl.Utf8` | No | Contract/OSI symbol string. |
| 6 | `underlying` | `pl.Utf8` | No | Normalized uppercase underlying ticker (`TickerValidator` / ASCII alnum fallback via `normalize_underlying`). |
| 7 | `asset_class` | `pl.Utf8` | No | `"OPT"` or `"STK"`. |
| 8 | `expiry` | `pl.Utf8` | Yes | Normalized `YYYYMMDD` (required for `OPT`, forbidden for `STK`). |
| 9 | `strike` | `pl.Float64` | Yes | Positive strike price (`> 0` required for `OPT`, `None` for `STK`). |
| 10 | `right` | `pl.Utf8` | Yes | `"C"` or `"P"` for `OPT`; `None` for `STK`. |
| 11 | `multiplier` | `pl.Int64` | No | Contract multiplier (`> 0`; defaults to `100` for `OPT`, `1` for `STK`). |
| 12 | `side` | `pl.Utf8` | No | `"BOT"` or `"SLD"` (`BUY`/`SELL` normalized on ingress). |
| 13 | `quantity` | `pl.Float64` | No | Absolute fill quantity (`> 0.0`). |
| 14 | `price` | `pl.Float64` | No | Execution unit price (`>= 0.0`). |
| 15 | `proceeds` | `pl.Float64` | No | Signed ex-fee cashflow (`-quantity * price * multiplier` for `BOT`, `+quantity * price * multiplier` for `SLD` via `compute_signed_proceeds`). |
| 16 | `commission` | `pl.Float64` | No | Positive cost convention (`abs(commission) >= 0.0`, default `0.0`); always stored non-negative so ex-fee and after-fee math never double-subtracts fees. |
| 17 | `realized_pnl_ib` | `pl.Float64` | Yes | Optional IBKR-reported realized P&L from Flex statements. |
| 18 | `open_close` | `pl.Utf8` | Yes | `"O"`, `"C"`, or `None`. |
| 19 | `event_code` | `pl.Utf8` | No | `"TRADE"`, `"EXPIRED"`, `"ASSIGNED"`, or `"EXERCISED"` (default `"TRADE"`). |
| 20 | `currency` | `pl.Utf8` | No | Settlement currency code (default `"USD"`). |
| 21 | `fx_rate_to_base` | `pl.Float64` | No | FX conversion rate to base currency (`> 0.0`, default `1.0`). |
| 22 | `executed_at` | `pl.Utf8` | No | Normalized UTC ISO-8601 timestamp `YYYY-MM-DDTHH:MM:SSZ` (`normalize_utc_timestamp`). |
| 23 | `source` | `pl.Utf8` | No | `"FLEX_ACTIVITY_365"`, `"FLEX_MONTHLY"`, `"FLEX_TRADE_CONF"`, `"FLEX_IMPORT"`, or `"GATEWAY_LIVE"`. |

### 5.2 Open Positions Snapshot (`data/ledger/open_positions.csv`, `LedgerPositionRow`)
Atomically replaced via `LedgerRepository.replace_positions()`, sorted by `(underlying, asset_class, expiry, strike, right, symbol)` with 15 columns (`LEDGER_POSITION_COLUMNS`, `LEDGER_POSITION_POLARS_SCHEMA`):

| # | Column | Polars Type | Null? | Validation & Semantics |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `conid` | `pl.Int64` | Yes | Sanitized contract `conId` (`> 0` and `< 2147483647`). |
| 2 | `symbol` | `pl.Utf8` | No | Contract/OSI symbol string. |
| 3 | `underlying` | `pl.Utf8` | No | Normalized uppercase underlying ticker. |
| 4 | `asset_class` | `pl.Utf8` | No | `"OPT"` or `"STK"`. |
| 5 | `expiry` | `pl.Utf8` | Yes | Normalized `YYYYMMDD` for `OPT`; `None` for `STK`. |
| 6 | `strike` | `pl.Float64` | Yes | Positive strike (`> 0`) for `OPT`; `None` for `STK`. |
| 7 | `right` | `pl.Utf8` | Yes | `"C"` or `"P"` for `OPT`; `None` for `STK`. |
| 8 | `multiplier` | `pl.Int64` | No | Positive contract multiplier (`100` `OPT` / `1` `STK`). |
| 9 | `position` | `pl.Float64` | No | Signed open quantity (`> 0` long, `< 0` short); when omitted in Flex `OpenPosition`, recovered as `round(market_value / (mark_price * multiplier), 6)` with `side="Short"` negation (zero/dust `<= 1e-6` skipped). |
| 10 | `avg_cost` | `pl.Float64` | No | Average position cost basis (default `0.0`; falls back from `costBasisPrice` to `abs(costBasisMoney / (position * multiplier))`). |
| 11 | `mark_price` | `pl.Float64` | Yes | Statement mark price (`>= 0.0` or `None`). |
| 12 | `market_value` | `pl.Float64` | Yes | Reported mark market value. |
| 13 | `unrealized_pnl` | `pl.Float64` | Yes | Statement unrealized P&L. |
| 14 | `source` | `pl.Utf8` | No | Same source enum as `executions.csv`. |
| 15 | `updated_at` | `pl.Utf8` | No | Normalized UTC ISO-8601 timestamp `YYYY-MM-DDTHH:MM:SSZ`. |

### 5.3 Sync Metadata (`data/ledger/sync_state.json`) & Deduplication Rules
- `sync_state.json` (`LedgerSyncState`) records `last_sync_at`, `last_sync_mode`, `source_counts`, `total_executions`, `open_positions_count`, and `recommended_incremental_mode` (`"full"` | `"monthly"`). Credentials and IBKR account identifiers are never written.
- **Deduplication & Authoritative Flex Upgrade (`LedgerRepository.upsert_executions`):**
  1. Authoritative `FLEX_SOURCES` (`FLEX_ACTIVITY_365`, `FLEX_MONTHLY`, `FLEX_TRADE_CONF`, `FLEX_IMPORT`) take precedence over provisional `GATEWAY_LIVE` fills.
  2. Matching primary key `exec_id` updates metadata cleanly without ever letting `GATEWAY_LIVE` overwrite an existing Flex row.
  3. Incoming `FLEX_*` rows matching an existing provisional `GATEWAY_LIVE` row via `_matches_secondary()` (same `underlying`, `asset_class`, `expiry`, `right`, `side`, `strike`, `quantity`, `price` within tolerance, and same UTC minute or `_exec_id_suffix_match` length `>= 8`) upgrade/replace the `GATEWAY_LIVE` row with the authoritative Flex row while backfilling any missing `ib_order_id`, `conid`, `underlying_conid`, `realized_pnl_ib`, or `open_close` (`_merge_execution_metadata`). Conversely, incoming `GATEWAY_LIVE` fills matching an existing Flex record are skipped.
  4. **Lifecycle Self-Healing (`_lifecycle_key`):** For lifecycle events (`EXPIRED`, `ASSIGNED`, `EXERCISED`) matched on `(underlying, asset_class, expiry, round(strike, 6), right, side, round(quantity, 4), executed_at[:10], event_code)`, an incoming non-`sha256:` `FLEX_*` row replaces any stale `sha256:` synthetic row (preserving/backfilling `ib_order_id`, `conid`, `underlying_conid`, `realized_pnl_ib`, `open_close` via `_merge_execution_metadata`); conversely, an incoming `sha256:` lifecycle row backfills null metadata onto an existing non-`sha256:` row without duplicating it (across upserts or within the same batch).

### 5.4 Flex XML/CSV Normalization & Filtering (`src/broker/flex.py`)
- **Asset-Class Inference & FX Filtering (`_resolve_asset_class`):** Explicit `assetCategory`/`assetClass` maps `OPT` or `STK` (skipping `CASH`, `FUT`, `BOND`, `FUND`, etc.). When omitted, `_has_option_attrs()` (`putCall`, `right`, `expiry`, `lastTradeDateOrContractMonth`, or `strike > 0`) or `parse_option_symbol()` infers `OPT`; FX pairs (`_FX_PAIR_RE`, e.g., `USD.PLN`, `eur.usd`, `GBP.USD`) with blank/`FX`/currency-matching `underlyingSymbol` and `AFX` rows (`code`/`notes`) without an equity underlying are skipped, while dotted stocks (`BRK.B`, `BF.B`) resolve to `STK`.
- **`notes`/`Code` Lifecycle Parsing & `<OptionEAE>` Deduplication:** `_parse_open_close_and_codes()` inspects `openCloseIndicator` plus `code`/`notes`/`Notes`/`Notes/Codes`/`transactionType` (`O`/`OPEN`, `C`/`CLOSE`, `Ep`/`EXPIR`, `A`/`ASSIGN`, `EX`/`EXERCIS`), ignoring non-lifecycle Flex tokens (`IA`, `FP`, `RP`, `P`) and defaulting lifecycle events to `open_close="C"`. `parse_flex_xml()` skips `_SUMMARY_DETAIL_LEVELS` and `ORDER` rows when `EXECUTION` detail exists, and records `_trade_lifecycle_sig` across `<Trade>`/`<TradeConfirm>` lifecycle rows to suppress duplicate `<OptionEAE>` entries while keeping standalone `<OptionEAE>` rows.

---

## 6. Paper Trade Registry & Tracked Leg-Quote Archive (`data/metadata/`, `data/tracked/`)

Forward paper trades registered via `POST /api/backtest/paper-trades` and leg quotes captured on manual refresh follow-ups are stored independently of full-chain archives so `CSVRepository.rebuild_index()` never treats narrow leg-only CSVs as option chain snapshots (`Settings._check_tracked_dir()` enforces `tracker.tracked_dir` strictly inside `storage.data_dir` and never inside `storage.snapshots_dir`):

1. **Paper Trade Registry (`data/metadata/paper_trades.json`, `PaperTradeStore` in `src/storage/paper_trade_store.py`):**
   - Stores registered `PaperTrade` records keyed inside `trades`, guarded by `threading.RLock` and written atomically via `.tmp` + `os.replace()`.
   - Each `PaperTrade` holds `trade_id`, `ticker` (`TickerValidator`-validated), `strategy_type`, `status` (`"ACTIVE" | "EXPIRED" | "STOPPED"`), `legs` (`1..8` `PaperTradeLeg` items with `conid`, `right`, `strike`, `expiry`, `side`, `qty`, `entry_mid`, `entry_bid`, `entry_ask`), optional `entry_snapshot_id` / `entry_snapshot_ts_utc` / `entry_spot`, `entry_net_cashflow`, `created_at_utc`, optional `stopped_at_utc` / `expired_at_utc`, exit config (`tp_pct`, `sl_pct`, `exit_dte`, `tp_sl_basis`), and optional `notes`. Active trade count is bounded by `tracker.max_active_trades` (default `120`, max `200`).
2. **Tracked Quote Follow-Up Log (`data/metadata/paper_quotes.jsonl`):**
   - Append-only JSON Lines ledger of `PaperQuoteRecord` events (`ts_utc`, `ticker`, `snapshot_id`, `tracked_csv`, `trade_ids`, `contracts_requested`, `contracts_quoted`, `contracts_failed`, `status`: `"QUOTED" | "ERROR"`, `error_code`, `error_message`), flushed with `os.fsync()` after each manual-refresh follow-up.
3. **Immutable Tracked Leg-Quote Snapshots (`data/tracked/{TICKER}/{TICKER}_{YYYYMMDD_HHMMSS}.csv`):**
   - Uses the exact canonical **27-column `OPTION_CHAIN_POLARS_SCHEMA`** (`src/storage/schema.py`), written atomically via `.tmp` + `os.replace()` with overwrite refusal (`StorageSchemaError` on duplicate path).
   - Populated solely during manual `POST /api/refresh` follow-ups (`PaperTradeService.quote_active_trades_for_refresh`) for non-expired active paper-trade legs, ensuring `< 30 DTE` legs falling outside `[30, 120]` chain snapshots remain markable down to expiration.
4. **Daily Bar Selection & Merging (`src/storage/backtest_store.py`):**
   - `select_daily_bar_snapshots()` pre-filters archive filenames in `O(window)` by `[start_date, end_date]` before parsing metadata/CSVs, selects one canonical snapshot per NYSE trading day (preferring the latest usable snapshot per `quality.is_usable_snapshot()`, otherwise the latest snapshot with `NO_USABLE_DAILY_SNAPSHOT`), and `load_daily_chain_bars()` attaches same-ET-date `data/tracked/{TICKER}/` CSV slices (`load_tracked_quotes_by_date()`) for leg-level fallback pricing.

---

## 7. Implementation Notes
Repository/schema implementation details live in `docs/reference/IMPLEMENTATION_NOTES.md` §2.
