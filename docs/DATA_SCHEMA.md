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
    ├── latest/
    │   ├── NVDA_latest.csv
    │   ├── AVGO_latest.csv
    │   ├── CRDO_latest.csv
    │   └── PLTR_latest.csv
    └── metadata/
        ├── snapshot_index.json
        ├── collector_settings.json   # scheduled collection settings (atomic JSON)
        └── collector_runs.jsonl      # one line per scheduled attempt (append-only)

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
              "prior_close_only_count": 0,
              "collection_mode": "MANUAL"
            }
          ]
        }
      }
    }

If `snapshot_index.json` is missing or corrupted, `CSVRepository.rebuild_index()` scans `data/snapshots/*/*.csv` on startup and reconstructs the index automatically (inferring `strike_step` and `range_pct` from the CSV strike grid when possible).

### 4.1 Snapshot quality metadata and the usable-snapshot rule
Every index entry (and `GET /api/snapshots/{ticker}`, chain `meta`) carries five optional quality fields; entries written before they existed load with `null`:

| Field | Meaning | Source |
|---|---|---|
| `spot_source` | `LAST`, `MID` or `CLOSE`: which underlying field became `underlying_price` | refresh only (not in the CSV; `null` after `rebuild_index()`) |
| `quoted_contract_count` | rows with a current quote: valid positive `bid` **or** `ask` | derived from the CSV rows |
| `quote_coverage` | `quoted_contract_count / contract_count`, 4 decimals | derived from the CSV rows |
| `prior_close_only_count` | rows with no `bid`, `ask` or `last` but a `close` (previous session; `mark_price` then falls back to `close`) | derived from the CSV rows |
| `collection_mode` | `MANUAL` (every `POST /api/refresh`) or `SCHEDULED` (reserved) | refresh only (`null` after `rebuild_index()`) |

**Quote definition.** Prices are sanitized before storage (`0`, `-1`, `>= 1e9` become empty), so a far out-of-the-money option with a zero bid and a valid ask counts as quoted. `last` and `close` alone never count. The CSV schema is unchanged; these counts can always be recomputed from the rows, so a rebuilt index fills them for old snapshots without rewriting any CSV.

**Usable for history** (`src/storage/quality.py`, `evaluate_snapshot` / `is_usable_snapshot`) requires all of:
1. `quote_coverage >= MIN_QUOTE_COVERAGE` (`0.90`, provisional),
2. `spot_source` is `LAST` or `MID`,
3. `market_data_type` is `1` (live) or `3` (delayed); frozen `2` / `4` is not usable.

Unknown values make a snapshot not usable. Low-quality snapshots are still saved (never refused); `POST /api/refresh` adds a notice listing the failed conditions.

---

## 5. Phase 2 Implementation Notes — `[IMPLEMENTED]`
- **`src/storage/schema.py`:** `OptionContractRow` (field order = CSV column order, 27 columns), `COLUMNS`, `OPTION_CHAIN_POLARS_SCHEMA` (`pl.Schema`), `SnapshotMeta` (index entry including `requested_strike_step` plus optional exchange discovery fields `exchange_validated_strike_count`, `available_strike_steps`, `available_range_pcts`, `exchange_native_verified`, and the quality fields `spot_source`, `quoted_contract_count`, `quote_coverage`, `prior_close_only_count`, `collection_mode` (see §4.1)), the `SpotSource` / `CollectionMode` literals, and `infer_chain_step_and_range(df)` (infers effective canonical strike spacing and moneyness span directly from a chain `DataFrame`). The row model also sanitizes sentinels (prices `<= 0`/NaN/`>= 1e9` → null; counts `< 0`/NaN/`>= 2147483647` → null; `iv <= 0` or `> 10`, `abs(delta) > 1.001`, `gamma < 0` → null), derives `mid` and `mark_price` (`mid` → `last` → `close`), and requires `snapshot_id` prefix == `underlying`.
- **`src/storage/csv_repository.py`:** `CSVRepository(storage_settings, root)`; `make_snapshot_id(ticker, ts)`; `save_snapshot` (refuses to overwrite an existing snapshot id, raising `StorageSchemaError`), `load_latest` (falls back to the newest valid archive if the cache is missing/corrupt), `load_snapshot`, `list_snapshots` (newest first), `load_index` / `rebuild_index` (self-healing with `infer_missing=True`; corrupt or stray CSVs are skipped with a warning). Readers raise `StorageSchemaError` on header/dtype/null violations and `SnapshotNotFoundError` when nothing exists.
- **`src/storage/quality.py`:** `measure_quote_quality(df)` (quoted / close-only counts), `evaluate_snapshot(meta)` -> `UsabilityVerdict(usable, reasons)`, `is_usable_snapshot(meta)`, `MIN_QUOTE_COVERAGE = 0.90`.
- **Index notes:** `file_path` / `latest_file` are root-relative POSIX paths. After `rebuild_index()`, `strike_step` and `range_pct` are inferred from CSV strikes via `infer_chain_step_and_range()` while `requested_strike_step` is `null` (since request metadata lives only in `snapshot_index.json`).

## 6. Scheduled Collection Files (`data/metadata/`)
- Scheduled snapshots are ordinary immutable snapshots with `collection_mode: "SCHEDULED"` in the index; the CSV schema is unchanged.
- `collector_settings.json`: `enabled`, `interval_min`, `tickers` (`symbol`, `range_pct`, `strike_step`, `dte_min`, `dte_max`, `pinned_since_utc`), `updated_at_utc`, `enabled_since_utc`. Written atomically (`.tmp` + `os.replace`).
- `collector_runs.jsonl`: one JSON object per ticker attempt: `planned_slot_utc`, `slot_kind` (`INTERVAL` / `CLOSING`), `actual_run_utc`, `ticker`, `attempt`, `status` (`OK` / `LOW_QUALITY` / `FAILED` / `SKIPPED` / `MISSED`), `snapshot_id`, `quote_coverage`, `usable`, `quality_reasons`, `requested_range_pct`, `requested_strike_step`, `effective_strike_step`, `dte_min`, `dte_max`, `pin_drift`, `error`, `note`. Unreadable lines are skipped with a warning.
