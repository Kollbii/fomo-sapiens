"""Server-persisted user watchlist: `{storage.data_dir}/watchlist.json`.

- Missing file -> the configured seed list (`watchlist.hardcoded_tickers`) is served; the file is
  written lazily on the first add/remove (reads never touch the disk).
- Every entry passes `TickerValidator` (including the `allow_custom_ticker=false` guard) on load
  and on every write; entries are deduped, insertion order is kept, new tickers append at the end,
  and the list is capped at `watchlist.max_size` (`WatchlistCapError` beyond it).
- Writes are atomic (`.tmp` + `fsync` + `os.replace()`). Reads never raise:
  - corrupt/invalid content (bad JSON/UTF-8, schema violation, deep nesting, file larger than
    `MAX_WATCHLIST_FILE_BYTES`) -> seed list + `CORRUPT_WATCHLIST_NOTICE`; the file is left
    untouched until the next successful write, which first copies it to `watchlist.corrupt.json`;
  - an I/O error opening/reading the file (e.g. a transient Windows lock) -> seed list +
    `UNREADABLE_WATCHLIST_NOTICE`; add/remove then refuse to write (`StorageUnavailableError`) so
    the user's list is never silently replaced.
- An empty list is a valid persisted state and is never re-seeded.
- The store never touches snapshot / latest CSVs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from src.core.config import StorageSettings, WatchlistSettings, resolve_project_path
from src.core.exceptions import InvalidTickerError, StorageUnavailableError, WatchlistCapError
from src.core.security import TickerValidator

logger = logging.getLogger(__name__)

WATCHLIST_FILENAME: Final = "watchlist.json"
WATCHLIST_CORRUPT_FILENAME: Final = "watchlist.corrupt.json"
WATCHLIST_FILE_VERSION: Final = 1
MAX_WATCHLIST_FILE_BYTES: Final = 64 * 1024
_UTC_TS_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"

CORRUPT_WATCHLIST_NOTICE: Final = (
    f"Saved watchlist ({WATCHLIST_FILENAME}) could not be read; showing the default list. "
    f"The file will be backed up to {WATCHLIST_CORRUPT_FILENAME} on the next change."
)
UNREADABLE_WATCHLIST_NOTICE: Final = (
    f"Saved watchlist ({WATCHLIST_FILENAME}) could not be opened; showing the default list. "
    "Changes are disabled until the file can be read again."
)

WatchlistFileStatus = Literal["ok", "corrupt", "unreadable"]


def resolve_watchlist_path(storage: StorageSettings, root: Path | None = None) -> Path:
    """`{storage.data_dir}/watchlist.json`; relative `data_dir` resolves against `root`."""
    if root is None:
        return resolve_project_path(storage.data_dir) / WATCHLIST_FILENAME
    data_dir = Path(storage.data_dir)
    base = data_dir if data_dir.is_absolute() else Path(root) / data_dir
    return base.resolve() / WATCHLIST_FILENAME


def _validate_utc_ts(value: str) -> str:
    if not value.endswith("Z"):
        raise ValueError(f"updated_at must be ISO-8601 UTC ending in 'Z': {value!r}")
    datetime.fromisoformat(value[:-1] + "+00:00")
    return value


class WatchlistFile(BaseModel):
    """On-disk JSON shape: `{"version": 1, "tickers": [...], "updated_at": "...Z"}`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = WATCHLIST_FILE_VERSION
    tickers: tuple[str, ...]
    updated_at: str

    @field_validator("version", mode="before")
    @classmethod
    def _strict_version(cls, value: Any) -> Any:
        # `Literal[1]` alone accepts `True` / `1.0` (equal and same hash); require an exact int.
        if type(value) is not int:
            raise ValueError(f"version must be the integer {WATCHLIST_FILE_VERSION}")
        return value

    @field_validator("tickers")
    @classmethod
    def _tickers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        out: list[str] = []
        for raw in value:
            try:
                out.append(TickerValidator.validate(raw))
            except InvalidTickerError as exc:
                raise ValueError(str(exc)) from exc
        return tuple(out)

    @field_validator("updated_at")
    @classmethod
    def _updated_at(cls, value: str) -> str:
        return _validate_utc_ts(value)


@dataclass(frozen=True, slots=True)
class WatchlistState:
    """Watchlist as served to callers. `notice` is set when the saved file was unusable."""

    tickers: tuple[str, ...]
    max_size: int
    notice: str | None = None
    status: WatchlistFileStatus = "ok"

    @property
    def count(self) -> int:
        return len(self.tickers)

    @property
    def is_full(self) -> bool:
        return len(self.tickers) >= self.max_size

    @property
    def writable(self) -> bool:
        return self.status != "unreadable"


@dataclass(frozen=True, slots=True)
class WatchlistUpdate:
    """Result of `add` / `remove`: normalized `ticker`, resulting `state`, and `changed` flag."""

    ticker: str
    state: WatchlistState
    changed: bool


_NOTICES: Final[dict[WatchlistFileStatus, str | None]] = {
    "ok": None,
    "corrupt": CORRUPT_WATCHLIST_NOTICE,
    "unreadable": UNREADABLE_WATCHLIST_NOTICE,
}


@dataclass(frozen=True, slots=True)
class _Snapshot:
    tickers: tuple[str, ...]
    status: WatchlistFileStatus


def _default_clock() -> datetime:
    return datetime.now(UTC)


class WatchlistStore:
    """Thread-safe JSON persistence for the user's editable watchlist."""

    def __init__(
        self,
        path: Path,
        settings: WatchlistSettings,
        *,
        validator: TickerValidator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.corrupt_path = self.path.with_name(WATCHLIST_CORRUPT_FILENAME)
        self.max_size = settings.max_size
        self.validator = validator or TickerValidator(
            settings.hardcoded_tickers, allow_custom=settings.allow_custom_ticker
        )
        self.clock = clock or _default_clock
        self.seed: tuple[str, ...] = self._normalize(settings.hardcoded_tickers)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ public API
    def ensure_layout(self) -> None:
        """Create the parent directory idempotently (never creates or touches the file)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> WatchlistState:
        """Current list; missing file -> seed, unusable file -> seed + notice (never raises)."""
        with self._lock:
            snap = self._read()
            return self._state(snap.tickers, snap.status)

    def add(self, raw_ticker: str) -> WatchlistUpdate:
        """Validate and append `raw_ticker`; a duplicate is a no-op, a full list raises."""
        symbol = self.validator.validate_selection(raw_ticker)
        with self._lock:
            snap = self._read_for_write()
            if symbol in snap.tickers:
                return WatchlistUpdate(symbol, self._state(snap.tickers, snap.status), False)
            if len(snap.tickers) >= self.max_size:
                raise WatchlistCapError(
                    f"Watchlist is full ({len(snap.tickers)}/{self.max_size}); "
                    f"remove a ticker before adding {symbol}."
                )
            tickers = self._write((*snap.tickers, symbol), backup_corrupt=snap.status == "corrupt")
            return WatchlistUpdate(symbol, self._state(tickers, "ok"), True)

    def remove(self, raw_ticker: str) -> WatchlistUpdate:
        """Remove `raw_ticker` if present (absent -> no-op). Never touches snapshot CSVs."""
        symbol = TickerValidator.validate(raw_ticker)
        with self._lock:
            snap = self._read_for_write()
            if symbol not in snap.tickers:
                return WatchlistUpdate(symbol, self._state(snap.tickers, snap.status), False)
            remaining = tuple(t for t in snap.tickers if t != symbol)
            tickers = self._write(remaining, backup_corrupt=snap.status == "corrupt")
            return WatchlistUpdate(symbol, self._state(tickers, "ok"), True)

    # ------------------------------------------------------------------ internals
    def _state(self, tickers: tuple[str, ...], status: WatchlistFileStatus) -> WatchlistState:
        return WatchlistState(
            tickers=tickers, max_size=self.max_size, notice=_NOTICES[status], status=status
        )

    def _normalize(self, raw: Iterable[str]) -> tuple[str, ...]:
        """Validate (incl. custom-ticker guard), dedupe keeping first occurrence, enforce cap."""
        seen: dict[str, None] = {}
        for item in raw:
            seen.setdefault(self.validator.validate_selection(item), None)
        tickers = tuple(seen)
        if len(tickers) > self.max_size:
            raise WatchlistCapError(
                f"Watchlist has {len(tickers)} tickers; max_size is {self.max_size}."
            )
        return tickers

    def _read_for_write(self) -> _Snapshot:
        snap = self._read()
        if snap.status == "unreadable":
            raise StorageUnavailableError(
                f"Saved watchlist ({WATCHLIST_FILENAME}) cannot be opened right now; "
                "the change was not saved. Retry once the file is no longer locked."
            )
        return snap

    def _read(self) -> _Snapshot:
        try:
            with self.path.open("rb") as fh:
                raw = fh.read(MAX_WATCHLIST_FILE_BYTES + 1)
        except FileNotFoundError:
            return _Snapshot(self.seed, "ok")
        except OSError as exc:
            logger.warning(
                "%s could not be opened (%s); serving seed watchlist read-only",
                WATCHLIST_FILENAME,
                type(exc).__name__,
            )
            return _Snapshot(self.seed, "unreadable")
        if len(raw) > MAX_WATCHLIST_FILE_BYTES:
            logger.warning(
                "%s exceeds %d bytes; serving seed watchlist",
                WATCHLIST_FILENAME,
                MAX_WATCHLIST_FILE_BYTES,
            )
            return _Snapshot(self.seed, "corrupt")
        try:
            model = WatchlistFile.model_validate(json.loads(raw.decode("utf-8")))
            return _Snapshot(self._normalize(model.tickers), "ok")
        except (
            UnicodeDecodeError,
            ValueError,
            ValidationError,
            RecursionError,
            InvalidTickerError,
            WatchlistCapError,
        ) as exc:
            logger.warning(
                "%s unusable (%s); serving seed watchlist", WATCHLIST_FILENAME, type(exc).__name__
            )
            return _Snapshot(self.seed, "corrupt")

    def _now_iso(self) -> str:
        dt = self.clock()
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        return dt.strftime(_UTC_TS_FORMAT)

    def _write(self, tickers: Iterable[str], *, backup_corrupt: bool) -> tuple[str, ...]:
        normalized = self._normalize(tickers)
        model = WatchlistFile(tickers=normalized, updated_at=self._now_iso())
        data = (json.dumps(model.model_dump(mode="json"), indent=2) + "\n").encode("utf-8")
        self.ensure_layout()
        if backup_corrupt and self.path.is_file():
            self._atomic_copy(self.path, self.corrupt_path)
        self._atomic_write_bytes(self.path, data)
        return normalized

    @staticmethod
    def _discard_tmp(tmp: Path) -> None:
        # Never let cleanup failure mask the original exception (or a successful replace).
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)

    @classmethod
    def _atomic_write_bytes(cls, path: Path, data: bytes) -> None:
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            with tmp.open("wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            cls._discard_tmp(tmp)

    @classmethod
    def _atomic_copy(cls, src: Path, dest: Path) -> None:
        tmp = dest.with_name(f".{dest.name}.tmp")
        try:
            shutil.copyfile(src, tmp)
            with tmp.open("r+b") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
        finally:
            cls._discard_tmp(tmp)
