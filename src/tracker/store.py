"""Tracked spread registry (atomic JSON), tracker run log (JSONL) and tracked-quote files.

- Registry: `data/metadata/tracked_spreads.json` (atomic replace, like `collector_settings.json`).
- Run log: `data/metadata/tracker_runs.jsonl` (append-only, one line per spread per slot and
  one per AUTO decision / lifecycle change).
- Quotes: `{tracked_dir}/{TICKER}/{TICKER}_{YYYYMMDD_HHMMSS}.csv` in the normal 27-column
  `OptionContractRow` schema; written to a temporary file and renamed, never overwritten.
  Nothing here touches `data/snapshots/` or `snapshot_index.json`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.core.exceptions import InvalidTickerError, StorageSchemaError
from src.core.security import TickerValidator
from src.storage.csv_repository import validate_frame
from src.storage.schema import (
    OPTION_CHAIN_POLARS_SCHEMA,
    SNAPSHOT_ID_RE,
    OptionContractRow,
    SpotSource,
)

logger = logging.getLogger(__name__)

REGISTRY_FILENAME: Final = "tracked_spreads.json"
TRACKER_RUNS_FILENAME: Final = "tracker_runs.jsonl"

SpreadSource = Literal["AUTO", "USER"]
SpreadStatus = Literal["ACTIVE", "EXPIRED", "STOPPED"]
TrackerRunStatus = Literal[
    # one per ACTIVE spread per slot
    "OK",  # every leg priced from bid/ask
    "PARTIAL",  # at least one leg has no price or was not returned
    "NO_QUOTE",  # no leg priced (spot and data type are still recorded)
    "FAILED",  # the tracker step raised (Gateway down, timeout, ...)
    "SKIPPED",  # the collector could not take the refresh lock for this slot
    "MISSED",  # the app was not running at the planned time
    # AUTO decisions
    "REGISTERED",
    "AUTO_DUPLICATE",
    "AUTO_SKIPPED_CAP",
    "AUTO_SKIPPED_NO_USABLE",
    "AUTO_FAILED",
    # lifecycle
    "EXPIRED",
    "STOPPED",
]
RunKind = Literal["QUOTE", "AUTO", "LIFECYCLE"]


def _symbol(value: str) -> str:
    try:
        return TickerValidator.validate(value)
    except InvalidTickerError as exc:
        raise ValueError(str(exc)) from exc


class TrackedLeg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    side: Literal["BUY", "SELL"]
    right: Literal["C", "P"]
    expiry: str
    strike: float = Field(gt=0.0)
    option_conid: int = Field(ge=0)

    def key(self) -> tuple[str, str, float, str]:
        return (self.side, self.right, round(self.strike, 4), self.expiry)


class TrackedSpread(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ticker: str
    source: SpreadSource
    strategy_kind: Literal["BULL_PUT_SPREAD"] = "BULL_PUT_SPREAD"
    legs: list[TrackedLeg] = Field(min_length=2, max_length=2)
    qty: int = Field(ge=1, le=100)
    underlying_conid: int = Field(ge=0)
    created_from_snapshot_id: str
    created_at_utc: str
    created_market_date: str
    entry_net_credit: float | None = None  # mid credit per share at registration (information)
    status: SpreadStatus = "ACTIVE"
    status_changed_utc: str | None = None
    last_quote_utc: str | None = None

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        return _symbol(value)

    @property
    def expiry(self) -> str:
        return self.legs[0].expiry

    def legs_key(self) -> tuple[tuple[str, str, float, str], ...]:
        return tuple(sorted(leg.key() for leg in self.legs))


class TrackerRunRecord(BaseModel):
    """One tracker log line."""

    model_config = ConfigDict(extra="forbid")

    kind: RunKind
    planned_slot_utc: str | None
    slot_kind: Literal["INTERVAL", "CLOSING"] | None = None
    actual_run_utc: str | None
    ticker: str
    market_date: str | None = None  # ET session date of the slot / source snapshot
    spread_id: str | None = None
    status: TrackerRunStatus
    tracked_snapshot_id: str | None = None
    underlying_price: float | None = None
    spot_source: SpotSource | None = None
    market_data_type: int | None = None
    priced_legs: int | None = None
    missing_legs: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    legs: list[TrackedLeg] = Field(default_factory=list)
    source_snapshot_id: str | None = None
    error: str | None = None
    note: str | None = None


class TrackerStore:
    def __init__(self, metadata_dir: Path, tracked_dir: Path) -> None:
        self.metadata_dir = Path(metadata_dir)
        self.tracked_dir = Path(tracked_dir)
        self.registry_path = self.metadata_dir / REGISTRY_FILENAME
        self.runs_path = self.metadata_dir / TRACKER_RUNS_FILENAME
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ registry
    def load_spreads(self) -> list[TrackedSpread]:
        """All tracked spreads; a missing file is empty, an unreadable one is an error."""
        if not self.registry_path.is_file():
            return []
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            return [TrackedSpread.model_validate(item) for item in data.get("spreads", [])]
        except (OSError, ValueError, ValidationError, AttributeError) as exc:
            # Never silently replace a registry we cannot read (it would drop open spreads).
            raise StorageSchemaError(f"{REGISTRY_FILENAME} unreadable: {exc}") from exc

    def save_spreads(self, spreads: Sequence[TrackedSpread]) -> None:
        payload = json.dumps(
            {"spreads": [s.model_dump() for s in spreads]}, indent=2, ensure_ascii=False
        ).encode("utf-8")
        with self._lock:
            self.metadata_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.registry_path.with_name(f".{self.registry_path.name}.tmp")
            tmp.write_bytes(payload)
            os.replace(tmp, self.registry_path)

    # ------------------------------------------------------------------ run log
    def append_runs(self, records: Sequence[TrackerRunRecord]) -> None:
        if not records:
            return
        lines = "".join(r.model_dump_json() + "\n" for r in records)
        with self._lock:
            self.metadata_dir.mkdir(parents=True, exist_ok=True)
            with self.runs_path.open("a", encoding="utf-8") as fh:
                fh.write(lines)
                fh.flush()
                os.fsync(fh.fileno())

    def read_runs(self) -> list[TrackerRunRecord]:
        if not self.runs_path.is_file():
            return []
        with self._lock:
            text = self.runs_path.read_text(encoding="utf-8")
        out: list[TrackerRunRecord] = []
        for n, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                out.append(TrackerRunRecord.model_validate_json(line))
            except ValidationError:
                logger.warning("tracker run log line %d unreadable; skipped", n)
        return out

    # ------------------------------------------------------------------ tracked quotes
    def quote_path(self, ticker: str, snapshot_id: str) -> Path:
        symbol = TickerValidator.validate(ticker)
        if SNAPSHOT_ID_RE.fullmatch(snapshot_id) is None or not snapshot_id.startswith(
            f"{symbol}_"
        ):
            raise StorageSchemaError(f"Invalid tracked snapshot id {snapshot_id!r} for {symbol}")
        return self.tracked_dir / symbol / f"{snapshot_id}.csv"

    def save_quotes(self, ticker: str, rows: Sequence[OptionContractRow]) -> Path:
        """Write one immutable tracked-quote file (temp file + rename; never overwrites)."""
        symbol = TickerValidator.validate(ticker)
        if not rows:
            raise StorageSchemaError("Refusing to save an empty tracked-quote file")
        ids = {r.snapshot_id for r in rows}
        if len(ids) != 1 or {r.underlying for r in rows} != {symbol}:
            raise StorageSchemaError("Tracked rows must share one snapshot id and ticker")
        target = self.quote_path(symbol, ids.pop())
        df = validate_frame(
            pl.DataFrame([r.model_dump() for r in rows], schema=OPTION_CHAIN_POLARS_SCHEMA),
            target.name,
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
