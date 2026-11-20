"""Mode 1 CSV archive: atomic immutable snapshots, `latest` cache, self-healing index."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import ValidationError

from src.core.config import PROJECT_ROOT, StorageSettings
from src.core.exceptions import SnapshotNotFoundError, StorageSchemaError
from src.core.security import TickerValidator
from src.storage._atomic import (
    StatSig,
    atomic_copy_file,
    atomic_write_bytes,
    atomic_write_csv,
    path_stat_sig,
)
from src.storage.quality import measure_quote_quality
from src.storage.schema import (
    COLUMNS,
    NON_NULLABLE_COLUMNS,
    OPTION_CHAIN_POLARS_SCHEMA,
    SNAPSHOT_ID_RE,
    OptionContractRow,
    PartialReason,
    SnapshotMeta,
    SpotSource,
    infer_chain_step_and_range,
)

logger = logging.getLogger(__name__)

SNAPSHOT_ID_TS_FORMAT = "%Y%m%d_%H%M%S"


def make_snapshot_id(ticker: str, ts: datetime) -> str:
    """`{TICKER}_{YYYYMMDD_HHMMSS}` in UTC."""
    symbol = TickerValidator.validate(ticker)
    return f"{symbol}_{ts.astimezone(UTC).strftime(SNAPSHOT_ID_TS_FORMAT)}"


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clone_snapshot_meta(meta: SnapshotMeta) -> SnapshotMeta:
    """Return a fast mutation-isolated copy of `SnapshotMeta` without recursive deepcopy."""
    updates: dict[str, Any] = {"expirations": list(meta.expirations)}
    if meta.available_strike_steps is not None:
        updates["available_strike_steps"] = list(meta.available_strike_steps)
    if meta.available_range_pcts is not None:
        updates["available_range_pcts"] = list(meta.available_range_pcts)
    return meta.model_copy(update=updates)


@dataclass(frozen=True)
class SnapshotSaveResult:
    """Outcome of `CSVRepository.save_snapshot_result`.

    `latest_updated` is False only for a partial snapshot saved while `data/latest/{T}_latest.csv`
    already existed (the previous complete cache is kept).
    """

    meta: SnapshotMeta
    latest_updated: bool


def validate_frame(
    df: pl.DataFrame,
    source: str,
    schema: pl.Schema | None = None,
) -> pl.DataFrame:
    """Reject frames violating the canonical option chain column contract."""
    if df.height == 0:
        raise StorageSchemaError(f"{source}: no data rows")
    expected_cols = tuple(schema.names()) if schema is not None else COLUMNS
    if tuple(df.columns) != expected_cols:
        raise StorageSchemaError(
            f"{source}: columns do not match the {len(expected_cols)}-column schema"
        )
    if schema is not None and df.schema != schema:
        raise StorageSchemaError(f"{source}: column dtypes do not match expected schema")
    for name in NON_NULLABLE_COLUMNS:
        if name in df.columns and df[name].null_count() > 0:
            raise StorageSchemaError(f"{source}: null values in required column '{name}'")
    return df


class CSVRepository:
    def __init__(self, storage: StorageSettings | None = None, root: Path | None = None) -> None:
        self.storage = storage or StorageSettings()
        self.root = Path(root) if root is not None else PROJECT_ROOT
        self.snapshots_dir = self._resolve(self.storage.snapshots_dir)
        self.latest_dir = self._resolve(self.storage.latest_dir)
        self.index_path = self._resolve(self.storage.metadata_index_file)
        self._lock = threading.RLock()
        self._index_cache_sig: StatSig | None = None
        self._index_cache_data: dict[str, Any] | None = None
        self._index_cache_metas: dict[str, tuple[SnapshotMeta, ...]] | None = None

    def ensure_layout(self) -> None:
        """Create the Mode 1 storage directories (idempotent; never touches existing files)."""
        for directory in (self.snapshots_dir, self.latest_dir, self.index_path.parent):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def snapshot_path(self, ticker: str, snapshot_id: str) -> Path:
        symbol = TickerValidator.validate(ticker)
        if SNAPSHOT_ID_RE.fullmatch(snapshot_id) is None or not snapshot_id.startswith(
            f"{symbol}_"
        ):
            raise StorageSchemaError(f"Invalid snapshot_id {snapshot_id!r} for {symbol}")
        return self.snapshots_dir / symbol / f"{snapshot_id}.csv"

    def latest_path(self, ticker: str) -> Path:
        return self.latest_dir / f"{TickerValidator.validate(ticker)}_latest.csv"

    # ------------------------------------------------------------------ write
    def save_snapshot(
        self,
        ticker: str,
        rows: Sequence[OptionContractRow | Mapping[str, Any]],
        *,
        range_pct: float | None = None,
        strike_step: float | None = None,
        requested_strike_step: float | None = None,
        spot_source: SpotSource | None = None,
        partial_reason: PartialReason | None = None,
    ) -> SnapshotMeta:
        """Validate rows and archive them as a new immutable snapshot (never overwrites).

        See `save_snapshot_result` for the `partial_reason` semantics.
        """
        return self.save_snapshot_result(
            ticker,
            rows,
            range_pct=range_pct,
            strike_step=strike_step,
            requested_strike_step=requested_strike_step,
            spot_source=spot_source,
            partial_reason=partial_reason,
        ).meta

    def save_snapshot_result(
        self,
        ticker: str,
        rows: Sequence[OptionContractRow | Mapping[str, Any]],
        *,
        range_pct: float | None = None,
        strike_step: float | None = None,
        requested_strike_step: float | None = None,
        spot_source: SpotSource | None = None,
        partial_reason: PartialReason | None = None,
    ) -> SnapshotSaveResult:
        """Archive a new immutable snapshot and report whether the `latest` cache was updated.

        A complete snapshot (`partial_reason=None`) always refreshes `data/latest/{T}_latest.csv`.
        A partial one (interrupted refresh) is archived and indexed with `partial=True` +
        `partial_reason`, but only becomes the latest cache when none exists for the ticker yet.
        """
        symbol = TickerValidator.validate(ticker)
        if not rows:
            raise StorageSchemaError("Refusing to save an empty snapshot")
        try:
            models = [
                r if isinstance(r, OptionContractRow) else OptionContractRow.model_validate(r)
                for r in rows
            ]
        except ValidationError as exc:
            raise StorageSchemaError(f"Row validation failed: {exc}") from exc

        ids = {m.snapshot_id for m in models}
        if len(ids) != 1:
            raise StorageSchemaError(f"Rows span multiple snapshot_ids: {sorted(ids)}")
        if {m.underlying for m in models} != {symbol}:
            raise StorageSchemaError(f"Rows contain underlyings other than {symbol}")
        snapshot_id = ids.pop()
        target = self.snapshot_path(symbol, snapshot_id)

        df = validate_frame(
            pl.DataFrame([m.model_dump() for m in models], schema=OPTION_CHAIN_POLARS_SCHEMA),
            snapshot_id,
        )
        base = self._meta_from_frame(
            df,
            target,
            range_pct,
            strike_step,
            requested_strike_step=requested_strike_step,
        )
        try:
            meta = SnapshotMeta.model_validate(
                {
                    **base.model_dump(),
                    "spot_source": spot_source,
                    "partial": partial_reason is not None,
                    "partial_reason": partial_reason,
                }
            )
        except ValidationError as exc:
            raise StorageSchemaError(f"Snapshot metadata validation failed: {exc}") from exc

        with self._lock:
            if target.exists():
                raise StorageSchemaError(f"Snapshot already exists (immutable): {snapshot_id}")
            latest = self.latest_path(symbol)
            update_latest = partial_reason is None or not latest.is_file()
            self.latest_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_csv(df, target)
            if update_latest:
                atomic_copy_file(target, latest, ensure_parent=False)
            self._index_add(symbol, meta, latest_snapshot_id=snapshot_id if update_latest else None)
        logger.info(
            "snapshot saved id=%s contracts=%d partial=%s latest_updated=%s",
            snapshot_id,
            meta.contract_count,
            partial_reason or "no",
            update_latest,
        )
        return SnapshotSaveResult(meta=meta, latest_updated=update_latest)

    # ------------------------------------------------------------------ read
    def _read_csv(self, path: Path) -> pl.DataFrame:
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
        return validate_frame(df, path.name)

    def load_snapshot(self, ticker: str, snapshot_id: str) -> pl.DataFrame:
        path = self.snapshot_path(ticker, snapshot_id)
        if not path.is_file():
            raise SnapshotNotFoundError(f"Snapshot not found: {snapshot_id}")
        return self._read_csv(path)

    def load_latest(self, ticker: str) -> pl.DataFrame:
        """Latest cache CSV; falls back to the newest valid archive if the cache is bad/missing."""
        symbol = TickerValidator.validate(ticker)
        path = self.latest_path(symbol)
        if path.is_file():
            try:
                return self._read_csv(path)
            except StorageSchemaError:
                logger.warning("latest cache for %s is corrupt; falling back to archive", symbol)
        # Newest complete snapshot first; partial ones only when nothing complete is readable.
        for meta in sorted(self.list_snapshots(symbol), key=lambda m: m.is_partial):
            try:
                return self.load_snapshot(symbol, meta.snapshot_id)
            except (StorageSchemaError, SnapshotNotFoundError):
                continue
        raise SnapshotNotFoundError(f"No snapshots available for {symbol}")

    def list_snapshots(self, ticker: str) -> list[SnapshotMeta]:
        """Snapshot metadata for `ticker`, newest first (index is rebuilt if needed)."""
        symbol = TickerValidator.validate(ticker)
        index = self.load_index()
        with self._lock:
            cached_metas = (
                self._index_cache_metas
                if index is self._index_cache_data and self._index_cache_metas is not None
                else None
            )
        if cached_metas is not None:
            return [_clone_snapshot_meta(m) for m in cached_metas.get(symbol, ())]
        entry = index.get("tickers", {}).get(symbol)
        if not entry:
            return []
        return [SnapshotMeta.model_validate(s) for s in entry["snapshots"]]

    def get_snapshot_meta(self, ticker: str, snapshot_id: str) -> SnapshotMeta | None:
        """Return metadata for a single archived `snapshot_id`, or None if absent."""
        symbol = TickerValidator.validate(ticker)
        index = self.load_index()
        with self._lock:
            cached_metas = (
                self._index_cache_metas
                if index is self._index_cache_data and self._index_cache_metas is not None
                else None
            )
        if cached_metas is not None:
            for meta in cached_metas.get(symbol, ()):
                if meta.snapshot_id == snapshot_id:
                    return _clone_snapshot_meta(meta)
            return None
        entry = index.get("tickers", {}).get(symbol)
        snaps = entry.get("snapshots") if isinstance(entry, dict) else None
        for snap in snaps or ():
            if snap.get("snapshot_id") == snapshot_id:
                return SnapshotMeta.model_validate(snap)
        return None

    def latest_snapshots(
        self,
        tickers: Iterable[str],
        *,
        include_partial: bool = True,
    ) -> dict[str, SnapshotMeta]:
        """Newest `SnapshotMeta` per ticker from ONE index read (tickers without data omitted).

        `include_partial=False` skips snapshots flagged `partial` and returns the newest complete
        one (tickers with only partial snapshots are omitted); use it for freshness checks.
        """
        index = self.load_index()
        with self._lock:
            cached_metas = (
                self._index_cache_metas
                if index is self._index_cache_data and self._index_cache_metas is not None
                else None
            )
        out: dict[str, SnapshotMeta] = {}
        if cached_metas is not None:
            for raw in tickers:
                symbol = TickerValidator.validate(raw)
                for meta in cached_metas.get(symbol, ()):  # index is newest-first
                    if include_partial or not meta.is_partial:
                        out[symbol] = _clone_snapshot_meta(meta)
                        break
            return out
        index_tickers = index.get("tickers", {})
        for raw in tickers:
            symbol = TickerValidator.validate(raw)
            entry = index_tickers.get(symbol)
            snaps = entry.get("snapshots") if isinstance(entry, dict) else None
            for snap in snaps or ():  # index is newest-first
                meta = SnapshotMeta.model_validate(snap)
                if include_partial or not meta.is_partial:
                    out[symbol] = meta
                    break
        return out

    def latest_cache_snapshot_id(self, ticker: str) -> str | None:
        """`snapshot_id` held by `data/latest/{T}_latest.csv` (None if missing or unreadable)."""
        symbol = TickerValidator.validate(ticker)
        path = self.latest_path(symbol)
        if not path.is_file():
            return None
        try:
            head = pl.read_csv(
                path,
                n_rows=1,
                columns=["snapshot_id"],
                schema_overrides={"snapshot_id": pl.Utf8},
            )
        except (pl.exceptions.PolarsError, UnicodeDecodeError, OSError, ValueError):
            return None
        if head.height == 0:
            return None
        value = head["snapshot_id"][0]
        if (
            not isinstance(value, str)
            or SNAPSHOT_ID_RE.fullmatch(value) is None
            or not value.startswith(f"{symbol}_")
        ):
            return None
        return value

    # ------------------------------------------------------------------ index
    def _meta_from_frame(
        self,
        df: pl.DataFrame,
        path: Path,
        range_pct: float | None,
        strike_step: float | None,
        *,
        requested_strike_step: float | None = None,
        infer_missing: bool = False,
    ) -> SnapshotMeta:
        first = df.row(0, named=True)
        resolved_step = strike_step
        resolved_range = range_pct
        if infer_missing and (resolved_step is None or resolved_range is None):
            inf_step, inf_range = infer_chain_step_and_range(df)
            if resolved_step is None:
                resolved_step = inf_step
            if resolved_range is None:
                resolved_range = inf_range
        quality = measure_quote_quality(df)
        return SnapshotMeta(
            snapshot_id=first["snapshot_id"],
            file_path=self._rel(path),
            snapshot_ts_utc=first["snapshot_ts_utc"],
            market_date=first["market_date"],
            market_data_type=first["market_data_type"],
            underlying_price=first["underlying_price"],
            contract_count=df.height,
            expirations=sorted(df["expiry"].unique().to_list()),
            range_pct=resolved_range,
            strike_step=resolved_step,
            requested_strike_step=requested_strike_step,
            quoted_contract_count=quality.quoted_contract_count,
            quote_coverage=quality.quote_coverage,
            prior_close_only_count=quality.prior_close_only_count,
        )

    def _index_stat_sig(self) -> StatSig | None:
        return path_stat_sig(self.index_path)

    def _invalidate_index_cache(self) -> None:
        self._index_cache_sig = None
        self._index_cache_data = None
        self._index_cache_metas = None

    def _write_index(
        self,
        tickers: dict[str, dict[str, Any]],
        *,
        cached_metas: dict[str, tuple[SnapshotMeta, ...]] | None = None,
    ) -> dict[str, Any]:
        index: dict[str, Any] = {"updated_at_utc": _utc_now_iso(), "tickers": tickers}
        atomic_write_bytes(self.index_path, json.dumps(index, indent=2).encode("utf-8"))
        with self._lock:
            if cached_metas is not None and (sig := self._index_stat_sig()) is not None:
                self._index_cache_sig = sig
                self._index_cache_data = index
                self._index_cache_metas = cached_metas
            else:
                self._invalidate_index_cache()
        return index

    def _ticker_entry(
        self,
        symbol: str,
        metas: list[SnapshotMeta],
        *,
        latest_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        """Index entry; `latest_snapshot_id` names the snapshot held by the latest cache file.

        When not given it is read from the cache file itself (partial snapshots may leave an
        older complete one there), falling back to the newest snapshot.
        """
        ordered = sorted(metas, key=lambda m: m.snapshot_id, reverse=True)
        known = {m.snapshot_id for m in ordered}
        cached = latest_snapshot_id or self.latest_cache_snapshot_id(symbol)
        return {
            "latest_snapshot_id": cached if cached in known else ordered[0].snapshot_id,
            "latest_file": self._rel(self.latest_path(symbol)),
            "snapshots": [m.model_dump() for m in ordered],
        }

    def _index_add(
        self,
        symbol: str,
        meta: SnapshotMeta,
        *,
        latest_snapshot_id: str | None = None,
    ) -> None:
        try:
            raw = self._read_index_file()
        except StorageSchemaError:
            rebuilt = self.rebuild_index()  # already includes the freshly written snapshot
            rebuilt_tickers = dict(rebuilt["tickers"])
            entry = rebuilt_tickers.get(symbol)
            if entry is not None:
                # Restore the caller-supplied fields a CSV rescan cannot recover (range/step,
                # spot_source, partial flag): store exactly what the normal path would.
                rebuilt_tickers[symbol] = {
                    **entry,
                    "snapshots": [
                        meta.model_dump() if snap["snapshot_id"] == meta.snapshot_id else snap
                        for snap in entry["snapshots"]
                    ],
                }
                self._write_index(rebuilt_tickers)
            return
        tickers = dict(raw["tickers"])
        base_cached_metas: dict[str, tuple[SnapshotMeta, ...]] | None = None
        if raw is self._index_cache_data and self._index_cache_metas is not None:
            existing = list(self._index_cache_metas.get(symbol, ()))
            base_cached_metas = dict(self._index_cache_metas)
        else:
            existing = [
                SnapshotMeta.model_validate(s) for s in tickers.get(symbol, {}).get("snapshots", [])
            ]
        ordered = sorted(
            [*(m for m in existing if m.snapshot_id != meta.snapshot_id), meta],
            key=lambda m: m.snapshot_id,
            reverse=True,
        )
        tickers[symbol] = self._ticker_entry(symbol, ordered, latest_snapshot_id=latest_snapshot_id)
        if base_cached_metas is not None:
            base_cached_metas[symbol] = tuple(ordered)
        self._write_index(tickers, cached_metas=base_cached_metas)

    def _read_index_file(self) -> dict[str, Any]:
        with self._lock:
            sig = self._index_stat_sig()
            if sig is None:
                self._invalidate_index_cache()
                if not self.snapshots_dir.is_dir():
                    return {"tickers": {}}  # nothing archived yet
                raise StorageSchemaError("snapshot index missing")
            if sig == self._index_cache_sig and self._index_cache_data is not None:
                return self._index_cache_data
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("tickers"), dict):
                    raise ValueError("bad structure")
                metas_by_ticker: dict[str, tuple[SnapshotMeta, ...]] = {}
                for sym, entry in data["tickers"].items():
                    metas_by_ticker[sym] = tuple(
                        SnapshotMeta.model_validate(s) for s in entry["snapshots"]
                    )
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self._invalidate_index_cache()
                raise StorageSchemaError(f"snapshot index corrupt: {exc}") from exc
            self._index_cache_sig = sig
            self._index_cache_data = data
            self._index_cache_metas = metas_by_ticker
            return data

    def load_index(self) -> dict[str, Any]:
        """Return the index, rebuilding it from disk when missing or corrupt."""
        with self._lock:
            try:
                return self._read_index_file()
            except StorageSchemaError:
                return self.rebuild_index()

    def rebuild_index(self) -> dict[str, Any]:
        """Scan `snapshots/*/*.csv`, skip corrupt files, and atomically rewrite the index."""
        with self._lock:
            tickers: dict[str, dict[str, Any]] = {}
            cached_metas: dict[str, tuple[SnapshotMeta, ...]] = {}
            if self.snapshots_dir.is_dir():
                for ticker_dir in sorted(p for p in self.snapshots_dir.iterdir() if p.is_dir()):
                    try:
                        symbol = TickerValidator.validate(ticker_dir.name)
                    except Exception:
                        logger.warning("skipping non-ticker directory %s", ticker_dir.name)
                        continue
                    if symbol != ticker_dir.name:
                        continue
                    metas: list[SnapshotMeta] = []
                    for csv_path in sorted(ticker_dir.glob("*.csv")):
                        if csv_path.name.startswith("."):
                            continue
                        try:
                            if csv_path.stem != self.snapshot_path(symbol, csv_path.stem).stem:
                                continue
                            df = self._read_csv(csv_path)
                        except StorageSchemaError as exc:
                            logger.warning("rebuild_index skipping %s: %s", csv_path.name, exc)
                            continue
                        if df["snapshot_id"][0] != csv_path.stem:
                            logger.warning("rebuild_index skipping %s: id mismatch", csv_path.name)
                            continue
                        metas.append(
                            self._meta_from_frame(df, csv_path, None, None, infer_missing=True)
                        )
                    if metas:
                        ordered = sorted(metas, key=lambda m: m.snapshot_id, reverse=True)
                        tickers[symbol] = self._ticker_entry(symbol, ordered)
                        cached_metas[symbol] = tuple(ordered)
            return self._write_index(tickers, cached_metas=cached_metas)
