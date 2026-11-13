"""Read-only `O(window)` Daily Bar selection and chain/tracked quote loading for Backtest.

All disk I/O for backtesting lives here so `src/analytics/backtest/` remains pure and I/O-free.
Never creates folders, never rewrites `snapshot_index.json`, and never reads CSVs outside the
requested `[start_date .. end_date]` ET date window.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import polars as pl
from pydantic import ValidationError

from src.core.calendar import et_date
from src.core.config import PaperTrackerSettings
from src.core.exceptions import BacktestValidationError, SnapshotNotFoundError, StorageSchemaError
from src.core.security import TickerValidator
from src.storage.csv_repository import SNAPSHOT_ID_TS_FORMAT, CSVRepository, validate_frame
from src.storage.quality import evaluate_snapshot
from src.storage.schema import (
    COLUMNS,
    OPTION_CHAIN_POLARS_SCHEMA,
    SNAPSHOT_ID_RE,
    SnapshotMeta,
)

NO_USABLE_DAILY_SNAPSHOT: Final = "NO_USABLE_DAILY_SNAPSHOT"


@dataclass(frozen=True)
class DailyBarSelection:
    """One selected canonical Daily Bar snapshot for an ET trading date."""

    snapshot_id: str
    ts_utc: datetime
    market_date: str
    meta: SnapshotMeta | None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrackedQuoteSlice:
    """Deduplicated tracked leg quotes for one ET `market_date`."""

    market_date: str
    df: pl.DataFrame
    snapshot_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DailyChainBar:
    """Loaded option chain DataFrame for one canonical Daily Bar plus matched tracked quotes."""

    snapshot_id: str
    ts_utc: datetime
    market_date: str
    df: pl.DataFrame
    meta: SnapshotMeta | None
    selection_flags: tuple[str, ...] = ()
    tracked_df: pl.DataFrame | None = None
    tracked_snapshot_ids: tuple[str, ...] = ()


def _parse_snapshot_filename_utc(stem: str, symbol: str) -> datetime | None:
    if SNAPSHOT_ID_RE.fullmatch(stem) is None or not stem.startswith(f"{symbol}_"):
        return None
    raw_ts = stem[len(symbol) + 1 :]
    try:
        return datetime.strptime(raw_ts, SNAPSHOT_ID_TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_ts_utc(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _read_index_meta(
    repo: CSVRepository, symbol: str
) -> tuple[dict[str, SnapshotMeta] | None, str | None]:
    """Read `snapshot_index.json` entries for `symbol` without ever rebuilding or writing."""
    path = repo.index_path
    if not path.is_file():
        return None, "Snapshot index unavailable; snapshot quality metadata unknown."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("tickers"), dict):
            raise ValueError("invalid index structure")
        entries = data["tickers"].get(symbol, {}).get("snapshots", [])
        if not isinstance(entries, list):
            raise ValueError("invalid ticker snapshots list")
        metas = [SnapshotMeta.model_validate(item) for item in entries]
    except (OSError, ValueError, TypeError, AttributeError, ValidationError) as exc:
        return None, f"Snapshot index unreadable ({exc}); snapshot quality metadata unknown."
    return {m.snapshot_id: m for m in metas}, None


def _default_tracked_dir(repo: CSVRepository) -> Path:
    path = Path(PaperTrackerSettings().tracked_dir)
    return path if path.is_absolute() else repo.root / path


def _read_tracked_csv(path: Path) -> pl.DataFrame:
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


@dataclass(frozen=True)
class _CandidateBar:
    snapshot_id: str
    ts_utc: datetime
    meta: SnapshotMeta | None
    usable: bool


def select_daily_bar_snapshots(
    repo: CSVRepository,
    ticker: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> tuple[list[DailyBarSelection], list[str]]:
    """Select 1 canonical Daily Bar per ET trading date inside `[start_date, end_date]`.

    Pre-filters files by parsing `YYYYMMDD` from filenames FIRST so CSVs outside the window
    are never opened (`O(window)`). Within each ET date, prefers the latest usable snapshot
    (`evaluate_snapshot(meta).usable`), falling back to the latest snapshot of that date with
    flag `NO_USABLE_DAILY_SNAPSHOT`.
    """
    symbol = TickerValidator.validate(ticker)
    if start_date is not None and end_date is not None and start_date > end_date:
        raise BacktestValidationError(
            f"start_date {start_date.isoformat()} must be <= end_date {end_date.isoformat()}"
        )

    notes: list[str] = []
    ticker_dir = repo.snapshots_dir / symbol
    if not ticker_dir.is_dir():
        return [], notes

    # Pre-screen filenames before reading index or touching any CSVs.
    matched_files: list[tuple[str, datetime]] = []
    for path in sorted(ticker_dir.glob("*.csv")):
        if path.name.startswith("."):
            continue
        file_ts_utc = _parse_snapshot_filename_utc(path.stem, symbol)
        if file_ts_utc is None:
            notes.append(f"Skipped invalid snapshot filename {path.name}")
            continue
        # UTC date is equal to ET date or 1 day ahead (UTC-4 / UTC-5).
        utc_day = file_ts_utc.date()
        if start_date is not None and utc_day < start_date:
            continue
        if end_date is not None and utc_day > end_date + timedelta(days=1):
            continue
        matched_files.append((path.stem, file_ts_utc))

    if not matched_files:
        return [], notes

    index_meta, index_note = _read_index_meta(repo, symbol)
    if index_note is not None:
        notes.append(index_note)

    by_et_date: dict[date, list[_CandidateBar]] = defaultdict(list)
    for snapshot_id, file_ts_utc in matched_files:
        meta = index_meta.get(snapshot_id) if index_meta is not None else None
        ts_utc = file_ts_utc
        if meta is not None:
            try:
                ts_utc = _parse_ts_utc(meta.snapshot_ts_utc)
            except ValueError:
                ts_utc = file_ts_utc
        day = et_date(ts_utc)
        if start_date is not None and day < start_date:
            continue
        if end_date is not None and day > end_date:
            continue
        usable = meta is not None and evaluate_snapshot(meta).usable
        by_et_date[day].append(
            _CandidateBar(
                snapshot_id=snapshot_id,
                ts_utc=ts_utc,
                meta=meta,
                usable=usable,
            )
        )

    selections: list[DailyBarSelection] = []
    for day in sorted(by_et_date):
        candidates = by_et_date[day]
        usable_candidates = [c for c in candidates if c.usable]
        if usable_candidates:
            chosen = max(usable_candidates, key=lambda c: (c.ts_utc, c.snapshot_id))
            flags: tuple[str, ...] = ()
        else:
            chosen = max(candidates, key=lambda c: (c.ts_utc, c.snapshot_id))
            flags = (NO_USABLE_DAILY_SNAPSHOT,)
        selections.append(
            DailyBarSelection(
                snapshot_id=chosen.snapshot_id,
                ts_utc=chosen.ts_utc,
                market_date=day.isoformat(),
                meta=chosen.meta,
                flags=flags,
            )
        )

    selections.sort(key=lambda s: (s.ts_utc, s.snapshot_id))
    return selections, notes


def load_tracked_quotes_by_date(
    ticker: str,
    *,
    target_dates: Collection[date | str] | None = None,
    tracked_dir: Path,
    notes: list[str] | None = None,
) -> dict[str, TrackedQuoteSlice]:
    """Load tracked leg quotes per ET `market_date` without creating dirs or touching other dates.

    If multiple tracked quote CSVs exist for the same ET date, selects the latest row per
    `(expiry, strike, right)` contract chronologically.
    """
    symbol = TickerValidator.validate(ticker)
    issue_log = notes if notes is not None else []
    ticker_dir = Path(tracked_dir) / symbol
    if not ticker_dir.is_dir():
        return {}

    allowed_dates: set[str] | None = None
    min_day: date | None = None
    max_day: date | None = None
    if target_dates is not None:
        parsed_days = [d if isinstance(d, date) else date.fromisoformat(d) for d in target_dates]
        if not parsed_days:
            return {}
        allowed_dates = {d.isoformat() for d in parsed_days}
        min_day = min(parsed_days)
        max_day = max(parsed_days)

    files_by_date: dict[str, list[tuple[datetime, str, Path]]] = defaultdict(list)
    for path in sorted(ticker_dir.glob("*.csv")):
        if path.name.startswith("."):
            continue
        file_ts_utc = _parse_snapshot_filename_utc(path.stem, symbol)
        if file_ts_utc is None:
            issue_log.append(f"Skipped invalid tracked filename {path.name}")
            continue
        utc_day = file_ts_utc.date()
        if min_day is not None and utc_day < min_day:
            continue
        if max_day is not None and utc_day > max_day + timedelta(days=1):
            continue
        et_day_str = et_date(file_ts_utc).isoformat()
        if allowed_dates is not None and et_day_str not in allowed_dates:
            continue
        files_by_date[et_day_str].append((file_ts_utc, path.stem, path))

    slices: dict[str, TrackedQuoteSlice] = {}
    for day_str, entries in sorted(files_by_date.items()):
        entries.sort(key=lambda item: (item[0], item[1]))
        loaded_frames: list[pl.DataFrame] = []
        for _ts_utc, _sid, path in entries:
            try:
                loaded_frames.append(_read_tracked_csv(path))
            except StorageSchemaError as exc:
                issue_log.append(f"Skipped unreadable tracked quote {path.name}: {exc}")
                continue
        if not loaded_frames:
            continue
        combined = (
            loaded_frames[0]
            if len(loaded_frames) == 1
            else pl.concat(loaded_frames, how="vertical")
        )
        deduped = (
            combined.sort(["snapshot_ts_utc", "snapshot_id"])
            .with_columns(pl.col("strike").round(4).alias("_strike_key"))
            .unique(subset=["expiry", "_strike_key", "right"], keep="last", maintain_order=True)
            .drop("_strike_key")
        )
        ordered_ids: list[str] = []
        seen_ids: set[str] = set()
        for sid in deduped["snapshot_id"].to_list():
            sid_str = str(sid)
            if sid_str not in seen_ids:
                seen_ids.add(sid_str)
                ordered_ids.append(sid_str)
        slices[day_str] = TrackedQuoteSlice(
            market_date=day_str,
            df=deduped,
            snapshot_ids=tuple(ordered_ids),
        )
    return slices


def load_daily_chain_bars(
    repo: CSVRepository,
    ticker: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    tracked_dir: Path | None = None,
) -> tuple[list[DailyChainBar], list[str]]:
    """Load ONLY selected canonical Daily Bar CSVs and attach matching tracked leg quotes."""
    symbol = TickerValidator.validate(ticker)
    selections, notes = select_daily_bar_snapshots(
        repo,
        symbol,
        start_date=start_date,
        end_date=end_date,
    )
    if not selections:
        return [], notes

    base_tracked = Path(tracked_dir) if tracked_dir is not None else _default_tracked_dir(repo)
    tracked_by_date = load_tracked_quotes_by_date(
        symbol,
        target_dates=[sel.market_date for sel in selections],
        tracked_dir=base_tracked,
        notes=notes,
    )

    bars: list[DailyChainBar] = []
    for sel in selections:
        try:
            df = repo.load_snapshot(symbol, sel.snapshot_id)
        except (StorageSchemaError, SnapshotNotFoundError) as exc:
            notes.append(f"Skipped unreadable snapshot {sel.snapshot_id}: {exc}")
            continue
        slice_ = tracked_by_date.get(sel.market_date)
        bars.append(
            DailyChainBar(
                snapshot_id=sel.snapshot_id,
                ts_utc=sel.ts_utc,
                market_date=sel.market_date,
                df=df,
                meta=sel.meta,
                selection_flags=sel.flags,
                tracked_df=slice_.df if slice_ is not None else None,
                tracked_snapshot_ids=slice_.snapshot_ids if slice_ is not None else (),
            )
        )
    bars.sort(key=lambda b: (b.ts_utc, b.snapshot_id))
    return bars, notes
