"""Read-only access to position-tracker data for the backtest engine.

Reads (never writes, never creates folders, never rebuilds anything):
- `data/metadata/collector_runs.jsonl`: which scheduled slot produced each normal snapshot;
- `data/metadata/tracker_runs.jsonl`: per tracked spread per slot: status, tracked file, spot,
  spot source, market data type;
- `data/metadata/tracked_spreads.json`: tracked spreads (legs, status, stop time);
- `{tracked_dir}/{TICKER}/{TICKER}_{YYYYMMDD_HHMMSS}.csv`: tracked leg quotes (row schema).

A slot is identified by `(ticker, planned_slot_utc)`, which both run logs carry. A normal
snapshot is paired with the tracked file of the same slot only when it is the slot's first
snapshot (lowest collector attempt with a snapshot): the tracker runs right after the first
attempt that reached the Gateway, so a later retry snapshot gets no tracked fallback.

Lines that do not parse (e.g. a half-written last line while the app is appending) are skipped
and reported in `TrackedHistory.issues`.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import polars as pl
from pydantic import ValidationError

from src.core.config import TrackerSettings
from src.core.exceptions import StorageSchemaError
from src.core.security import TickerValidator
from src.storage.collector_store import RUNS_FILENAME as COLLECTOR_RUNS_FILENAME
from src.storage.collector_store import RunRecord
from src.storage.csv_repository import CSVRepository, validate_frame
from src.storage.schema import COLUMNS, OPTION_CHAIN_POLARS_SCHEMA, SNAPSHOT_ID_RE
from src.storage.tracker_store import (
    REGISTRY_FILENAME,
    TRACKER_RUNS_FILENAME,
    TrackedSpread,
    TrackerRunRecord,
)

Contract = tuple[str, float, str]  # (expiry, strike rounded to 4 dp, right)

# Tracker statuses that leave a spread without quotes in that slot.
TRACKER_GAP_STATUSES: Final = frozenset({"MISSED", "FAILED", "SKIPPED"})


def contract_key(expiry: str, strike: float, right: str) -> Contract:
    return (expiry, round(float(strike), 4), right)


def _parse_utc(text: str) -> datetime:
    value = text[:-1] + "+00:00" if text.endswith("Z") else text
    dt = datetime.fromisoformat(value)
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class TrackerEntry:
    """One `QUOTE` line of `tracker_runs.jsonl` (one tracked spread in one slot)."""

    ref: str  # "tracker_runs.jsonl:L<n>"
    spread_id: str | None
    status: str
    tracked_snapshot_id: str | None
    spot: float | None
    spot_source: str | None
    market_data_type: int | None


@dataclass(frozen=True)
class TrackedSpreadInfo:
    spread_id: str
    contracts: frozenset[Contract]
    status: str
    status_changed_utc: datetime | None


@dataclass(frozen=True)
class TrackedSlot:
    """Tracker data of one scheduled slot of one ticker."""

    slot_utc: datetime
    slot_kind: str | None
    entries: tuple[TrackerEntry, ...]
    tracked_snapshot_id: str | None = None
    df: pl.DataFrame | None = None  # tracked rows (None: no file, or file missing on disk)
    file_ts_utc: datetime | None = None
    file_missing: bool = False

    def best_entry(self, spread_ids: frozenset[str] | None = None) -> TrackerEntry | None:
        """An entry with a spot, preferring the given spreads (all entries share the file)."""
        pool = [e for e in self.entries if e.spot is not None]
        if spread_ids:
            preferred = [e for e in pool if e.spread_id in spread_ids]
            if preferred:
                return preferred[0]
        return pool[0] if pool else None


@dataclass
class TrackedHistory:
    slots: dict[datetime, TrackedSlot] = field(default_factory=dict)
    # normal snapshot id -> (slot, is the slot's first snapshot)
    snapshot_slots: dict[str, tuple[datetime, bool]] = field(default_factory=dict)
    spreads: tuple[TrackedSpreadInfo, ...] = ()
    issues: list[str] = field(default_factory=list)
    skipped_lines: list[str] = field(default_factory=list)


def default_tracked_dir(repo: CSVRepository) -> Path:
    """`tracker.tracked_dir` default resolved against the repository root."""
    path = Path(TrackerSettings().tracked_dir)
    return path if path.is_absolute() else repo.root / path


def _read_jsonl(path: Path, model: type, history: TrackedHistory) -> list[tuple[int, object]]:
    if not path.is_file():
        return []
    out: list[tuple[int, object]] = []
    text = path.read_text(encoding="utf-8")
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            out.append((n, model.model_validate_json(line)))  # type: ignore[attr-defined]
        except ValidationError:
            ref = f"{path.name}:L{n}"
            history.skipped_lines.append(ref)
            history.issues.append(f"Skipped unreadable run-log line {ref} (e.g. half-written).")
    return out


def read_tracked_spreads(metadata_dir: Path) -> tuple[list[TrackedSpread], str | None]:
    """Registry entries (read-only). Returns `(spreads, problem)`; never raises."""
    path = metadata_dir / REGISTRY_FILENAME
    if not path.is_file():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [TrackedSpread.model_validate(s) for s in data.get("spreads", [])], None
    except (OSError, ValueError, ValidationError, AttributeError) as exc:
        return [], f"{REGISTRY_FILENAME} unreadable ({exc}); stopped spreads are not known."


def _read_tracked_csv(path: Path) -> pl.DataFrame:
    with path.open("r", encoding="utf-8") as fh:
        header = fh.readline().strip().split(",")
    if tuple(header) != COLUMNS:
        raise StorageSchemaError(f"{path.name}: header does not match the row schema")
    return validate_frame(pl.read_csv(path, schema=OPTION_CHAIN_POLARS_SCHEMA), path.name)


def load_tracked_history(
    repo: CSVRepository, ticker: str, *, tracked_dir: Path | None = None
) -> TrackedHistory:
    """Everything the engine needs from the tracker for `ticker` (read-only)."""
    symbol = TickerValidator.validate(ticker)
    history = TrackedHistory()
    metadata_dir = repo.index_path.parent
    base = tracked_dir if tracked_dir is not None else default_tracked_dir(repo)
    ticker_dir = base / symbol

    # Normal snapshot -> slot (collector run log).
    first_attempt: dict[datetime, tuple[int, str]] = {}
    for _n, rec in _read_jsonl(metadata_dir / COLLECTOR_RUNS_FILENAME, RunRecord, history):
        assert isinstance(rec, RunRecord)
        if rec.ticker != symbol or not rec.snapshot_id:
            continue
        slot = _parse_utc(rec.planned_slot_utc)
        history.snapshot_slots[rec.snapshot_id] = (slot, False)
        best = first_attempt.get(slot)
        if best is None or (rec.attempt, rec.snapshot_id) < best:
            first_attempt[slot] = (rec.attempt, rec.snapshot_id)
    for slot, (_attempt, sid) in first_attempt.items():
        history.snapshot_slots[sid] = (slot, True)

    # Tracker run log -> slots.
    grouped: dict[datetime, list[tuple[int, TrackerRunRecord]]] = defaultdict(list)
    for n, rec in _read_jsonl(metadata_dir / TRACKER_RUNS_FILENAME, TrackerRunRecord, history):
        assert isinstance(rec, TrackerRunRecord)
        if rec.kind != "QUOTE" or rec.ticker != symbol or not rec.planned_slot_utc:
            continue
        grouped[_parse_utc(rec.planned_slot_utc)].append((n, rec))

    used_files: set[str] = set()
    for slot, items in sorted(grouped.items()):
        entries = tuple(
            TrackerEntry(
                ref=f"{TRACKER_RUNS_FILENAME}:L{n}",
                spread_id=rec.spread_id,
                status=rec.status,
                tracked_snapshot_id=rec.tracked_snapshot_id,
                spot=rec.underlying_price,
                spot_source=rec.spot_source,
                market_data_type=rec.market_data_type,
            )
            for n, rec in items
        )
        file_ids = sorted({e.tracked_snapshot_id for e in entries if e.tracked_snapshot_id})
        df: pl.DataFrame | None = None
        file_id: str | None = None
        file_ts: datetime | None = None
        missing = False
        for fid in file_ids:  # normally exactly one file per ticker per slot
            if SNAPSHOT_ID_RE.fullmatch(fid) is None or not fid.startswith(f"{symbol}_"):
                history.issues.append(f"Ignored invalid tracked file id {fid!r} in the run log.")
                continue
            path = ticker_dir / f"{fid}.csv"
            if not path.is_file():
                missing = True
                history.issues.append(f"Tracked file {fid}.csv is in the run log but missing.")
                continue
            try:
                part = _read_tracked_csv(path)
            except (StorageSchemaError, pl.exceptions.PolarsError, OSError) as exc:
                missing = True
                history.issues.append(f"Tracked file {fid}.csv unreadable: {exc}")
                continue
            used_files.add(fid)
            df = part if df is None else pl.concat([df, part])
            file_id = file_id or fid
            file_ts = file_ts or _parse_utc(str(part["snapshot_ts_utc"][0]))
        history.slots[slot] = TrackedSlot(
            slot_utc=slot,
            slot_kind=items[0][1].slot_kind,
            entries=entries,
            tracked_snapshot_id=file_id,
            df=df,
            file_ts_utc=file_ts,
            file_missing=missing,
        )

    if ticker_dir.is_dir():
        for path in sorted(ticker_dir.glob("*.csv")):
            if path.name.startswith(".") or path.stem in used_files:
                continue
            history.issues.append(
                f"Tracked file {path.name} has no run-log entry; not used (its slot is unknown)."
            )

    spreads, problem = read_tracked_spreads(metadata_dir)
    if problem:
        history.issues.append(problem)
    history.spreads = tuple(
        TrackedSpreadInfo(
            spread_id=s.id,
            contracts=frozenset(contract_key(leg.expiry, leg.strike, leg.right) for leg in s.legs),
            status=s.status,
            status_changed_utc=_parse_utc(s.status_changed_utc) if s.status_changed_utc else None,
        )
        for s in spreads
        if s.ticker == symbol
    )
    return history
