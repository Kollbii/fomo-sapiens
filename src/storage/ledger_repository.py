"""Atomic CSV & JSON repository for the Hybrid Options Trade Ledger (`data/ledger/`)."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.core.config import PROJECT_ROOT
from src.core.exceptions import StorageSchemaError
from src.storage.ledger_schema import (
    FLEX_SOURCES,
    LEDGER_EXECUTION_COLUMNS,
    LEDGER_EXECUTION_NON_NULLABLE,
    LEDGER_EXECUTION_POLARS_SCHEMA,
    LEDGER_POSITION_COLUMNS,
    LEDGER_POSITION_NON_NULLABLE,
    LEDGER_POSITION_POLARS_SCHEMA,
    LedgerExecutionRow,
    LedgerPositionRow,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_write_csv(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    df.write_csv(tmp)
    os.replace(tmp, path)


def _exec_id_suffix_match(id_a: str, id_b: str) -> bool:
    a = id_a.strip()
    b = id_b.strip()
    if not a or not b or a.startswith("sha256:") or b.startswith("sha256:"):
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 8 and (longer.endswith(f".{shorter}") or longer.endswith(shorter))


def _matches_secondary(gw: LedgerExecutionRow, fx: LedgerExecutionRow) -> bool:
    """Match a provisional `GATEWAY_LIVE` fill to an authoritative `FLEX_*` execution."""
    if (
        gw.underlying != fx.underlying
        or gw.asset_class != fx.asset_class
        or gw.expiry != fx.expiry
        or gw.right != fx.right
        or gw.side != fx.side
    ):
        return False
    if (gw.strike is None) != (fx.strike is None):
        return False
    if gw.strike is not None and fx.strike is not None and abs(gw.strike - fx.strike) > 1e-6:
        return False
    if abs(gw.quantity - fx.quantity) > 1e-6 or abs(gw.price - fx.price) > 1e-4:
        return False
    same_minute = gw.executed_at[:16] == fx.executed_at[:16]
    return same_minute or _exec_id_suffix_match(gw.exec_id, fx.exec_id)


_LIFECYCLE_EVENTS: frozenset[str] = frozenset({"EXPIRED", "ASSIGNED", "EXERCISED"})


def _lifecycle_key(
    row: LedgerExecutionRow,
) -> tuple[str, str, str | None, float | None, str | None, str, float, str, str]:
    strike_norm = round(row.strike, 6) if row.strike is not None else None
    return (
        row.underlying,
        row.asset_class,
        row.expiry,
        strike_norm,
        row.right,
        row.side,
        round(row.quantity, 4),
        row.executed_at[:10],
        row.event_code,
    )


def _merge_execution_metadata(
    primary: LedgerExecutionRow, fallback: LedgerExecutionRow
) -> LedgerExecutionRow:
    """Keep authoritative `primary` fields while backfilling null metadata from `fallback`."""
    updates: dict[str, Any] = {}
    for field_name in (
        "ib_order_id",
        "conid",
        "underlying_conid",
        "realized_pnl_ib",
        "open_close",
    ):
        if getattr(primary, field_name) is None and getattr(fallback, field_name) is not None:
            updates[field_name] = getattr(fallback, field_name)
    return primary.model_copy(update=updates) if updates else primary


class LedgerSyncState(BaseModel):
    """Metadata persisted in `data/ledger/sync_state.json` (never stores credentials/accounts)."""

    model_config = ConfigDict(extra="forbid")

    last_sync_at: str | None = None
    last_sync_mode: str | None = None
    source_counts: dict[str, int] = Field(default_factory=dict)
    total_executions: int = 0
    open_positions_count: int = 0
    recommended_incremental_mode: Literal["full", "monthly"] = "full"


class LedgerRepository:
    """Thread-safe, atomic CSV + JSON storage for `executions.csv` and `open_positions.csv`."""

    def __init__(self, ledger_dir: str | Path = "data/ledger", root: Path | None = None) -> None:
        base_root = Path(root) if root is not None else PROJECT_ROOT
        raw_dir = Path(ledger_dir)
        self.ledger_dir = raw_dir if raw_dir.is_absolute() else base_root / raw_dir
        self.imports_dir = self.ledger_dir / "imports"
        self.executions_path = self.ledger_dir / "executions.csv"
        self.positions_path = self.ledger_dir / "open_positions.csv"
        self.sync_state_path = self.ledger_dir / "sync_state.json"
        self._lock = threading.RLock()

    def ensure_layout(self) -> None:
        """Create `ledger_dir` and `ledger_dir/imports` idempotently."""
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self.imports_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ read
    def _read_typed_csv(
        self,
        path: Path,
        *,
        columns: tuple[str, ...],
        non_nullable: tuple[str, ...],
        schema: pl.Schema,
    ) -> pl.DataFrame:
        if not path.is_file():
            return pl.DataFrame(schema=schema)
        try:
            with path.open("r", encoding="utf-8") as fh:
                first_line = fh.readline().strip()
            if not first_line:
                raise StorageSchemaError(f"{path.name}: empty CSV missing header")
            header = tuple(first_line.split(","))
            if header != columns:
                raise StorageSchemaError(
                    f"{path.name}: header does not match expected {len(columns)}-column schema"
                )
            df = pl.read_csv(path, schema=schema)
        except StorageSchemaError:
            raise
        except (pl.exceptions.PolarsError, UnicodeDecodeError, OSError) as exc:
            raise StorageSchemaError(f"{path.name}: unreadable or corrupt CSV: {exc}") from exc
        if df.height > 0:
            for col in non_nullable:
                if df[col].null_count() > 0:
                    raise StorageSchemaError(f"{path.name}: null values in required column '{col}'")
        return df

    def load_executions(self) -> pl.DataFrame:
        """Load `executions.csv`, returning an empty schema-typed DataFrame if absent."""
        with self._lock:
            return self._read_typed_csv(
                self.executions_path,
                columns=LEDGER_EXECUTION_COLUMNS,
                non_nullable=LEDGER_EXECUTION_NON_NULLABLE,
                schema=LEDGER_EXECUTION_POLARS_SCHEMA,
            )

    def load_positions(self) -> pl.DataFrame:
        """Load `open_positions.csv`, returning an empty schema-typed DataFrame if absent."""
        with self._lock:
            return self._read_typed_csv(
                self.positions_path,
                columns=LEDGER_POSITION_COLUMNS,
                non_nullable=LEDGER_POSITION_NON_NULLABLE,
                schema=LEDGER_POSITION_POLARS_SCHEMA,
            )

    def has_existing_history(self) -> bool:
        """Return True if `executions.csv` already stores at least one execution row."""
        return self.load_executions().height > 0

    # ------------------------------------------------------------------ write
    def upsert_executions(
        self, rows: Sequence[LedgerExecutionRow | Mapping[str, Any]]
    ) -> tuple[int, int]:
        """Validate, deduplicate, upgrade Gateway->Flex matches, sort, and write atomically."""
        if not rows:
            return (0, 0)
        try:
            incoming = [
                r if isinstance(r, LedgerExecutionRow) else LedgerExecutionRow.model_validate(r)
                for r in rows
            ]
        except ValidationError as exc:
            raise StorageSchemaError(f"Execution row validation failed: {exc}") from exc

        with self._lock:
            self.ensure_layout()
            existing_df = self.load_executions()
            by_id: dict[str, LedgerExecutionRow] = {}
            for rec in existing_df.to_dicts():
                model = LedgerExecutionRow.model_validate(rec)
                by_id[model.exec_id] = model

            inserted_ids: set[str] = set()
            updated_ids: set[str] = set()

            for inc in incoming:
                if (
                    inc.source in FLEX_SOURCES
                    and inc.event_code in _LIFECYCLE_EVENTS
                    and not inc.exec_id.startswith("sha256:")
                ):
                    inc_key = _lifecycle_key(inc)
                    syn_match = next(
                        (
                            row
                            for row in by_id.values()
                            if row.exec_id.startswith("sha256:")
                            and row.event_code in _LIFECYCLE_EVENTS
                            and _lifecycle_key(row) == inc_key
                        ),
                        None,
                    )
                    if syn_match is not None:
                        del by_id[syn_match.exec_id]
                        cur = by_id.get(inc.exec_id)
                        merged = _merge_execution_metadata(inc, cur) if cur is not None else inc
                        merged = _merge_execution_metadata(merged, syn_match)
                        by_id[inc.exec_id] = merged
                        if syn_match.exec_id in inserted_ids:
                            inserted_ids.remove(syn_match.exec_id)
                            if cur is None:
                                inserted_ids.add(inc.exec_id)
                            elif inc.exec_id not in inserted_ids:
                                updated_ids.add(inc.exec_id)
                        elif cur is None or inc.exec_id not in inserted_ids:
                            updated_ids.add(inc.exec_id)
                        continue

                if inc.exec_id.startswith("sha256:") and inc.event_code in _LIFECYCLE_EVENTS:
                    inc_key = _lifecycle_key(inc)
                    real_match = next(
                        (
                            row
                            for row in by_id.values()
                            if not row.exec_id.startswith("sha256:")
                            and row.event_code in _LIFECYCLE_EVENTS
                            and _lifecycle_key(row) == inc_key
                        ),
                        None,
                    )
                    if real_match is not None:
                        if inc.exec_id in by_id:
                            del by_id[inc.exec_id]
                            inserted_ids.discard(inc.exec_id)
                            if real_match.exec_id not in inserted_ids:
                                updated_ids.add(real_match.exec_id)
                        updated = _merge_execution_metadata(real_match, inc)
                        if updated != real_match:
                            by_id[real_match.exec_id] = updated
                            if real_match.exec_id not in inserted_ids:
                                updated_ids.add(real_match.exec_id)
                        continue

                if inc.exec_id in by_id:
                    cur = by_id[inc.exec_id]
                    if cur.source in FLEX_SOURCES and inc.source == "GATEWAY_LIVE":
                        continue
                    merged = _merge_execution_metadata(inc, cur)
                    if merged != cur:
                        by_id[inc.exec_id] = merged
                        if inc.exec_id not in inserted_ids:
                            updated_ids.add(inc.exec_id)
                    continue

                if inc.source in FLEX_SOURCES:
                    gw_match = next(
                        (
                            row
                            for row in by_id.values()
                            if row.source == "GATEWAY_LIVE" and _matches_secondary(row, inc)
                        ),
                        None,
                    )
                    if gw_match is not None:
                        del by_id[gw_match.exec_id]
                        merged = _merge_execution_metadata(inc, gw_match)
                        by_id[inc.exec_id] = merged
                        if gw_match.exec_id in inserted_ids:
                            inserted_ids.remove(gw_match.exec_id)
                            inserted_ids.add(inc.exec_id)
                        else:
                            updated_ids.add(inc.exec_id)
                        continue
                else:
                    fx_match = next(
                        (
                            row
                            for row in by_id.values()
                            if row.source in FLEX_SOURCES and _matches_secondary(inc, row)
                        ),
                        None,
                    )
                    if fx_match is not None:
                        continue

                by_id[inc.exec_id] = inc
                inserted_ids.add(inc.exec_id)

            ordered = sorted(by_id.values(), key=lambda r: (r.executed_at, r.exec_id))
            out_df = pl.DataFrame(
                [r.model_dump() for r in ordered],
                schema=LEDGER_EXECUTION_POLARS_SCHEMA,
            )
            _atomic_write_csv(out_df, self.executions_path)
            return (len(inserted_ids), len(updated_ids))

    def replace_positions(self, rows: Sequence[LedgerPositionRow | Mapping[str, Any]]) -> int:
        """Atomically replace `open_positions.csv` with validated current open positions."""
        try:
            models = [
                r if isinstance(r, LedgerPositionRow) else LedgerPositionRow.model_validate(r)
                for r in rows
            ]
        except ValidationError as exc:
            raise StorageSchemaError(f"Position row validation failed: {exc}") from exc

        ordered = sorted(
            models,
            key=lambda r: (
                r.underlying,
                r.asset_class,
                r.expiry or "",
                r.strike or 0.0,
                r.right or "",
                r.symbol,
            ),
        )
        with self._lock:
            self.ensure_layout()
            df = (
                pl.DataFrame(
                    [m.model_dump() for m in ordered],
                    schema=LEDGER_POSITION_POLARS_SCHEMA,
                )
                if ordered
                else pl.DataFrame(schema=LEDGER_POSITION_POLARS_SCHEMA)
            )
            _atomic_write_csv(df, self.positions_path)
        return len(ordered)

    # ------------------------------------------------------------------ sync state
    def _compute_source_counts(self, exec_df: pl.DataFrame) -> dict[str, int]:
        if exec_df.height == 0:
            return {}
        grouped = exec_df.group_by("source").len().sort("source")
        return {str(row["source"]): int(row["len"]) for row in grouped.to_dicts()}

    def load_sync_state(self) -> dict[str, Any]:
        """Load `sync_state.json` or return computed defaults when missing/corrupt."""
        with self._lock:
            exec_df = self.load_executions()
            pos_df = self.load_positions()
            source_counts = self._compute_source_counts(exec_df)
            recommended: Literal["full", "monthly"] = "monthly" if exec_df.height > 0 else "full"
            if self.sync_state_path.is_file():
                try:
                    raw = json.loads(self.sync_state_path.read_text(encoding="utf-8"))
                    parsed = LedgerSyncState.model_validate(raw)
                    data = parsed.model_dump()
                    data["source_counts"] = source_counts or parsed.source_counts
                    data["total_executions"] = exec_df.height
                    data["open_positions_count"] = pos_df.height
                    data["recommended_incremental_mode"] = recommended
                    return data
                except (ValueError, ValidationError, OSError) as exc:
                    logger.warning("ledger sync_state.json corrupt (%s); using computed state", exc)
            return LedgerSyncState(
                source_counts=source_counts,
                total_executions=exec_df.height,
                open_positions_count=pos_df.height,
                recommended_incremental_mode=recommended,
            ).model_dump()

    def save_sync_state(
        self,
        *,
        last_sync_at: str | None = None,
        last_sync_mode: str | None = None,
    ) -> dict[str, Any]:
        """Persist `sync_state.json` atomically using current execution/position counts."""
        with self._lock:
            self.ensure_layout()
            exec_df = self.load_executions()
            pos_df = self.load_positions()
            source_counts = self._compute_source_counts(exec_df)
            recommended: Literal["full", "monthly"] = "monthly" if exec_df.height > 0 else "full"
            state = LedgerSyncState(
                last_sync_at=last_sync_at or _utc_now_iso(),
                last_sync_mode=last_sync_mode,
                source_counts=source_counts,
                total_executions=exec_df.height,
                open_positions_count=pos_df.height,
                recommended_incremental_mode=recommended,
            )
            payload = state.model_dump()
            _atomic_write_bytes(
                self.sync_state_path,
                json.dumps(payload, indent=2).encode("utf-8"),
            )
            return payload
