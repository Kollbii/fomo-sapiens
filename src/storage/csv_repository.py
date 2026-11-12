"""Mode 1 CSV archive: atomic immutable snapshots, `latest` cache, self-healing index."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import ValidationError

from src.core.config import PROJECT_ROOT, StorageSettings
from src.core.exceptions import SnapshotNotFoundError, StorageSchemaError
from src.core.security import TickerValidator
from src.storage.schema import (
    COLUMNS,
    NON_NULLABLE_COLUMNS,
    OPTION_CHAIN_POLARS_SCHEMA,
    SNAPSHOT_ID_RE,
    OptionContractRow,
    SnapshotMeta,
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


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_copy(src: Path, dest: Path) -> None:
    tmp = dest.with_name(f".{dest.name}.tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def validate_frame(df: pl.DataFrame, source: str) -> pl.DataFrame:
    """Reject frames violating the canonical option chain column contract."""
    if df.height == 0:
        raise StorageSchemaError(f"{source}: no data rows")
    if tuple(df.columns) != COLUMNS:
        raise StorageSchemaError(f"{source}: columns do not match the {len(COLUMNS)}-column schema")
    for name in NON_NULLABLE_COLUMNS:
        if df[name].null_count() > 0:
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
    ) -> SnapshotMeta:
        """Validate rows and archive them as a new immutable snapshot (never overwrites)."""
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
        meta = self._meta_from_frame(
            df,
            target,
            range_pct,
            strike_step,
            requested_strike_step=requested_strike_step,
        )

        with self._lock:
            if target.exists():
                raise StorageSchemaError(f"Snapshot already exists (immutable): {snapshot_id}")
            target.parent.mkdir(parents=True, exist_ok=True)
            self.latest_dir.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.tmp")
            df.write_csv(tmp)
            os.replace(tmp, target)
            _atomic_copy(target, self.latest_path(symbol))
            self._index_add(symbol, meta)
        logger.info("snapshot saved id=%s contracts=%d", snapshot_id, meta.contract_count)
        return meta

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
        for meta in self.list_snapshots(symbol):
            try:
                return self.load_snapshot(symbol, meta.snapshot_id)
            except (StorageSchemaError, SnapshotNotFoundError):
                continue
        raise SnapshotNotFoundError(f"No snapshots available for {symbol}")

    def list_snapshots(self, ticker: str) -> list[SnapshotMeta]:
        """Snapshot metadata for `ticker`, newest first (index is rebuilt if needed)."""
        symbol = TickerValidator.validate(ticker)
        entry = self.load_index().get("tickers", {}).get(symbol)
        if not entry:
            return []
        return [SnapshotMeta.model_validate(s) for s in entry["snapshots"]]

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
        )

    def _write_index(self, tickers: dict[str, dict[str, Any]]) -> dict[str, Any]:
        index: dict[str, Any] = {"updated_at_utc": _utc_now_iso(), "tickers": tickers}
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(self.index_path, json.dumps(index, indent=2).encode("utf-8"))
        return index

    def _ticker_entry(self, symbol: str, metas: list[SnapshotMeta]) -> dict[str, Any]:
        ordered = sorted(metas, key=lambda m: m.snapshot_id, reverse=True)
        return {
            "latest_snapshot_id": ordered[0].snapshot_id,
            "latest_file": self._rel(self.latest_path(symbol)),
            "snapshots": [m.model_dump() for m in ordered],
        }

    def _index_add(self, symbol: str, meta: SnapshotMeta) -> None:
        try:
            tickers = self._read_index_file()["tickers"]
        except StorageSchemaError:
            rebuilt = self.rebuild_index()  # already includes the freshly written snapshot
            entry = rebuilt["tickers"].get(symbol)
            if entry is not None:
                for snap in entry["snapshots"]:
                    if snap["snapshot_id"] == meta.snapshot_id:
                        snap["range_pct"] = meta.range_pct
                        snap["strike_step"] = meta.strike_step
                        snap["requested_strike_step"] = meta.requested_strike_step
                self._write_index(rebuilt["tickers"])
            return
        existing = [
            SnapshotMeta.model_validate(s) for s in tickers.get(symbol, {}).get("snapshots", [])
        ]
        existing = [m for m in existing if m.snapshot_id != meta.snapshot_id]
        tickers[symbol] = self._ticker_entry(symbol, [*existing, meta])
        self._write_index(tickers)

    def _read_index_file(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            if not self.snapshots_dir.is_dir():
                return {"tickers": {}}  # nothing archived yet
            raise StorageSchemaError("snapshot index missing")
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("tickers"), dict):
                raise ValueError("bad structure")
            for entry in data["tickers"].values():
                for s in entry["snapshots"]:
                    SnapshotMeta.model_validate(s)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise StorageSchemaError(f"snapshot index corrupt: {exc}") from exc
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
                        tickers[symbol] = self._ticker_entry(symbol, metas)
            return self._write_index(tickers)
