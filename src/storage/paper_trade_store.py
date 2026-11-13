"""Generic multi-strategy Paper Trade registry, quote log, and immutable leg-quote CSV archive.

- Registry: `data/metadata/paper_trades.json` (atomic `.tmp` + `os.replace()`).
- Quote run log: `data/metadata/paper_quotes.jsonl` (append-only JSON lines).
- Leg quotes: `{tracked_dir}/{TICKER}/{TICKER}_{YYYYMMDD_HHMMSS}.csv` in the canonical
  27-column `OptionContractRow` / `OPTION_CHAIN_POLARS_SCHEMA` contract; atomic and immutable.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Literal, Self

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.core.exceptions import InvalidTickerError, SnapshotNotFoundError, StorageSchemaError
from src.core.security import TickerValidator
from src.storage.csv_repository import validate_frame
from src.storage.schema import (
    COLUMNS,
    OPTION_CHAIN_POLARS_SCHEMA,
    SNAPSHOT_ID_RE,
    OptionContractRow,
)

logger = logging.getLogger(__name__)

REGISTRY_FILENAME: Final = "paper_trades.json"
QUOTES_LOG_FILENAME: Final = "paper_quotes.jsonl"
MAX_PAPER_TRADE_LEGS: Final = 8

PaperTradeStatus = Literal["ACTIVE", "EXPIRED", "STOPPED"]
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _validate_symbol(value: str) -> str:
    try:
        return TickerValidator.validate(value)
    except InvalidTickerError as exc:
        raise ValueError(str(exc)) from exc


def _validate_iso_date(value: str) -> str:
    if _ISO_DATE_RE.fullmatch(value) is None:
        raise ValueError(f"expected YYYY-MM-DD, got {value!r}")
    date.fromisoformat(value)
    return value


def _validate_utc_ts(value: str) -> str:
    if not value.endswith("Z"):
        raise ValueError(f"timestamp must be ISO-8601 UTC ending in 'Z': {value!r}")
    datetime.fromisoformat(value[:-1] + "+00:00")
    return value


class PaperTradeLeg(BaseModel):
    """One option contract leg inside a paper trade (supports 1..8 legs per trade)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    side: Literal["BUY", "SELL"]
    right: Literal["C", "P"]
    expiry: str
    strike: float = Field(gt=0.0)
    con_id: int = Field(ge=0)

    @field_validator("expiry")
    @classmethod
    def _expiry(cls, value: str) -> str:
        return _validate_iso_date(value)

    @field_validator("strike")
    @classmethod
    def _finite_strike(cls, value: float) -> float:
        if math.isnan(value) or math.isinf(value):
            raise ValueError("strike must be finite")
        return value

    def key(self) -> tuple[str, str, float, str]:
        return (self.side, self.right, round(self.strike, 4), self.expiry)


class PaperTrade(BaseModel):
    """Registered multi-leg paper trade persisted in `paper_trades.json`."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    ticker: str
    strategy_kind: str = Field(min_length=1)
    status: PaperTradeStatus = "ACTIVE"
    legs: list[PaperTradeLeg] = Field(min_length=1, max_length=MAX_PAPER_TRADE_LEGS)
    qty: int = Field(default=1, ge=1)
    underlying_conid: int = Field(ge=0)
    created_from_snapshot_id: str = Field(min_length=1)
    created_utc: str
    market_date: str
    expiry: str = ""
    entry_net_cash: float
    status_changed_utc: str | None = None
    last_quote_utc: str | None = None

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return _validate_symbol(value)

    @field_validator("market_date")
    @classmethod
    def _market_date(cls, value: str) -> str:
        return _validate_iso_date(value)

    @field_validator("created_utc")
    @classmethod
    def _created_utc(cls, value: str) -> str:
        return _validate_utc_ts(value)

    @field_validator("status_changed_utc", "last_quote_utc")
    @classmethod
    def _optional_utc(cls, value: str | None) -> str | None:
        return None if value is None else _validate_utc_ts(value)

    @field_validator("entry_net_cash")
    @classmethod
    def _finite_cash(cls, value: float) -> float:
        if math.isnan(value) or math.isinf(value):
            raise ValueError("entry_net_cash must be finite")
        return value

    @model_validator(mode="after")
    def _sync_earliest_expiry(self) -> Self:
        earliest = min(leg.expiry for leg in self.legs)
        if not self.expiry:
            self.expiry = earliest
        else:
            _validate_iso_date(self.expiry)
            if self.expiry != earliest:
                raise ValueError(
                    f"expiry must match earliest leg expiry {earliest}, got {self.expiry!r}"
                )
        return self

    def legs_key(self) -> tuple[tuple[str, str, float, str], ...]:
        """Canonical sorted leg tuple used for duplicate-active-trade detection."""
        return tuple(sorted(leg.key() for leg in self.legs))


class PaperQuoteRecord(BaseModel):
    """One append-only quote run entry persisted in `paper_quotes.jsonl`."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    quote_id: str = Field(min_length=1)
    quoted_at_utc: str
    market_date: str
    ticker: str
    trade_ids: list[str] = Field(default_factory=list)
    status: str = Field(min_length=1)
    tracked_snapshot_id: str | None = None
    spot: float | None = None
    spot_source: str | None = None
    market_data_type: int | None = None
    error: str | None = None

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return _validate_symbol(value)

    @field_validator("market_date")
    @classmethod
    def _market_date(cls, value: str) -> str:
        return _validate_iso_date(value)

    @field_validator("quoted_at_utc")
    @classmethod
    def _quoted_at_utc(cls, value: str) -> str:
        return _validate_utc_ts(value)


class PaperTradeStore:
    """Thread-safe persistence for the Paper Trade registry, quote log, and quote CSVs."""

    def __init__(self, metadata_dir: Path, tracked_dir: Path) -> None:
        self.metadata_dir = Path(metadata_dir)
        self.tracked_dir = Path(tracked_dir)
        self.registry_path = self.metadata_dir / REGISTRY_FILENAME
        self.quotes_log_path = self.metadata_dir / QUOTES_LOG_FILENAME
        self._lock = threading.RLock()

    def ensure_layout(self) -> None:
        """Create metadata and tracked directories idempotently without touching existing files."""
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.tracked_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ registry
    def load_trades(self) -> list[PaperTrade]:
        """Load all paper trades; a missing file returns `[]`, an unreadable file errors."""
        with self._lock:
            if not self.registry_path.is_file():
                return []
            try:
                data = json.loads(self.registry_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("trades"), list):
                    raise ValueError("expected JSON object with 'trades' list")
                return [PaperTrade.model_validate(item) for item in data["trades"]]
            except (OSError, ValueError, ValidationError, AttributeError) as exc:
                raise StorageSchemaError(f"{REGISTRY_FILENAME} unreadable: {exc}") from exc

    def save_trades(self, trades: Sequence[PaperTrade]) -> None:
        """Atomically overwrite `paper_trades.json` via `.tmp` + `os.replace()`."""
        payload = json.dumps(
            {"trades": [t.model_dump() for t in trades]},
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8")
        with self._lock:
            self.metadata_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.registry_path.with_name(f".{self.registry_path.name}.tmp")
            try:
                tmp.write_bytes(payload)
                os.replace(tmp, self.registry_path)
            finally:
                if tmp.exists():
                    tmp.unlink()

    # ------------------------------------------------------------------ quote run log
    def append_quote_records(self, records: Sequence[PaperQuoteRecord]) -> None:
        """Append one or more quote records to `paper_quotes.jsonl`."""
        if not records:
            return
        lines = "".join(r.model_dump_json() + "\n" for r in records)
        with self._lock:
            self.metadata_dir.mkdir(parents=True, exist_ok=True)
            with self.quotes_log_path.open("a", encoding="utf-8") as fh:
                fh.write(lines)
                fh.flush()
                os.fsync(fh.fileno())

    def append_quote_record(self, record: PaperQuoteRecord) -> None:
        """Append a single quote record to `paper_quotes.jsonl`."""
        self.append_quote_records((record,))

    def read_quote_records(self, *, ticker: str | None = None) -> list[PaperQuoteRecord]:
        """Read `paper_quotes.jsonl` (returns `[]` if missing; skips corrupt lines)."""
        symbol = TickerValidator.validate(ticker) if ticker is not None else None
        with self._lock:
            if not self.quotes_log_path.is_file():
                return []
            text = self.quotes_log_path.read_text(encoding="utf-8")
        records: list[PaperQuoteRecord] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rec = PaperQuoteRecord.model_validate_json(line)
            except ValidationError:
                logger.warning("%s line %d unreadable; skipped", QUOTES_LOG_FILENAME, lineno)
                continue
            if symbol is None or rec.ticker == symbol:
                records.append(rec)
        return records

    # ------------------------------------------------------------------ tracked quote CSVs
    def quote_path(self, ticker: str, snapshot_id: str) -> Path:
        """Return `{tracked_dir}/{TICKER}/{TICKER}_{YYYYMMDD_HHMMSS}.csv` after validation."""
        symbol = TickerValidator.validate(ticker)
        if SNAPSHOT_ID_RE.fullmatch(snapshot_id) is None or not snapshot_id.startswith(
            f"{symbol}_"
        ):
            raise StorageSchemaError(f"Invalid tracked snapshot id {snapshot_id!r} for {symbol}")
        return self.tracked_dir / symbol / f"{snapshot_id}.csv"

    def save_quotes(
        self,
        ticker: str,
        rows: Sequence[OptionContractRow | Mapping[str, Any]],
    ) -> Path:
        """Persist one immutable tracked-quote CSV (`.tmp` + `os.replace()`, never overwrites)."""
        symbol = TickerValidator.validate(ticker)
        if not rows:
            raise StorageSchemaError("Refusing to save an empty tracked-quote file")
        try:
            models = [
                r if isinstance(r, OptionContractRow) else OptionContractRow.model_validate(r)
                for r in rows
            ]
        except ValidationError as exc:
            raise StorageSchemaError(f"Tracked quote row validation failed: {exc}") from exc

        ids = {m.snapshot_id for m in models}
        if len(ids) != 1 or {m.underlying for m in models} != {symbol}:
            raise StorageSchemaError("Tracked rows must share one snapshot_id and ticker")
        target = self.quote_path(symbol, ids.pop())
        df = validate_frame(
            pl.DataFrame([m.model_dump() for m in models], schema=OPTION_CHAIN_POLARS_SCHEMA),
            target.name,
            OPTION_CHAIN_POLARS_SCHEMA,
        )
        with self._lock:
            if target.exists():
                raise StorageSchemaError(f"Tracked-quote file already exists: {target.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.tmp")
            try:
                df.write_csv(tmp)
                os.replace(tmp, target)
            finally:
                if tmp.exists():
                    tmp.unlink()
        return target

    def load_quotes(self, ticker: str, snapshot_id: str) -> pl.DataFrame:
        """Load and schema-validate one tracked-quote CSV."""
        path = self.quote_path(ticker, snapshot_id)
        if not path.is_file():
            raise SnapshotNotFoundError(f"Tracked-quote snapshot not found: {snapshot_id}")
        try:
            with path.open("r", encoding="utf-8") as fh:
                header = fh.readline().strip().split(",")
            if tuple(header) != COLUMNS:
                raise StorageSchemaError(
                    f"{path.name}: header does not match the {len(COLUMNS)}-column schema"
                )
            df = pl.read_csv(path, schema=OPTION_CHAIN_POLARS_SCHEMA)
        except StorageSchemaError:
            raise
        except (pl.exceptions.PolarsError, UnicodeDecodeError, OSError) as exc:
            raise StorageSchemaError(f"{path.name}: unreadable or corrupt CSV: {exc}") from exc
        return validate_frame(df, path.name, OPTION_CHAIN_POLARS_SCHEMA)

    def list_quote_files(self, ticker: str) -> list[Path]:
        """List valid `{TICKER}_{YYYYMMDD_HHMMSS}.csv` files without creating directories."""
        symbol = TickerValidator.validate(ticker)
        ticker_dir = self.tracked_dir / symbol
        if not ticker_dir.is_dir():
            return []
        return sorted(
            p
            for p in ticker_dir.glob("*.csv")
            if not p.name.startswith(".")
            and SNAPSHOT_ID_RE.fullmatch(p.stem) is not None
            and p.stem.startswith(f"{symbol}_")
        )
