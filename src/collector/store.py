"""Collector settings (atomic JSON) and run log (append-only JSONL) in `data/metadata/`.

Nothing here touches `data/snapshots/`; snapshots are written only by `CSVRepository`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.collector.calendar import ALLOWED_INTERVALS_MIN, DEFAULT_INTERVAL_MIN
from src.core.exceptions import InvalidTickerError
from src.core.security import TickerValidator

logger = logging.getLogger(__name__)

SETTINGS_FILENAME: Final = "collector_settings.json"
RUNS_FILENAME: Final = "collector_runs.jsonl"
MAX_COLLECTOR_TICKERS: Final = 6
# Same bounds as `RefreshRequest` (DTE window of the chain refresh).
PIN_DTE_MIN_LIMIT: Final = 0
PIN_DTE_MAX_LIMIT: Final = 120


def utc_now_iso(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TickerPin(BaseModel):
    """Fixed refresh parameters for one collected ticker (same for every scheduled snapshot)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    range_pct: float = Field(gt=0.0, le=1.0)
    strike_step: float = Field(gt=0.0)  # numeric only: Auto would change between snapshots
    dte_min: int = Field(ge=PIN_DTE_MIN_LIMIT, le=PIN_DTE_MAX_LIMIT)
    dte_max: int = Field(ge=PIN_DTE_MIN_LIMIT + 1, le=PIN_DTE_MAX_LIMIT)
    pinned_since_utc: str | None = None

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        try:
            return TickerValidator.validate(value)
        except InvalidTickerError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def _window(self) -> TickerPin:
        if self.dte_min > self.dte_max:
            raise ValueError("dte_min must be <= dte_max")
        return self

    def params_key(self) -> tuple[float, float, int, int]:
        return (round(self.range_pct, 6), round(self.strike_step, 6), self.dte_min, self.dte_max)


DEFAULT_PINS: Final[tuple[TickerPin, ...]] = (
    TickerPin(symbol="NVDA", range_pct=0.35, strike_step=5.0, dte_min=30, dte_max=120),
    TickerPin(symbol="AVGO", range_pct=0.35, strike_step=10.0, dte_min=30, dte_max=120),
)


class CollectorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_min: int = DEFAULT_INTERVAL_MIN
    tickers: list[TickerPin] = Field(default_factory=lambda: [p.model_copy() for p in DEFAULT_PINS])
    updated_at_utc: str | None = None
    # When collection was last switched on; used to find slots missed while the app was down.
    enabled_since_utc: str | None = None

    @field_validator("interval_min")
    @classmethod
    def _interval(cls, value: int) -> int:
        if value not in ALLOWED_INTERVALS_MIN:
            raise ValueError(f"interval_min must be one of {list(ALLOWED_INTERVALS_MIN)}")
        return value

    @field_validator("tickers")
    @classmethod
    def _tickers(cls, value: list[TickerPin]) -> list[TickerPin]:
        if len(value) > MAX_COLLECTOR_TICKERS:
            raise ValueError(f"At most {MAX_COLLECTOR_TICKERS} collector tickers")
        symbols = [p.symbol for p in value]
        if len(set(symbols)) != len(symbols):
            raise ValueError("Each ticker may appear only once")
        return value


RunStatus = Literal["OK", "LOW_QUALITY", "FAILED", "SKIPPED", "MISSED"]


class RunRecord(BaseModel):
    """One ticker attempt in one collection slot."""

    model_config = ConfigDict(extra="forbid")

    planned_slot_utc: str
    slot_kind: Literal["INTERVAL", "CLOSING"]
    actual_run_utc: str | None
    ticker: str
    attempt: int = 1
    status: RunStatus
    snapshot_id: str | None = None
    quote_coverage: float | None = None
    usable: bool | None = None
    quality_reasons: list[str] = Field(default_factory=list)
    requested_range_pct: float | None = None
    requested_strike_step: float | None = None
    effective_strike_step: float | None = None
    dte_min: int | None = None
    dte_max: int | None = None
    pin_drift: bool = False
    error: str | None = None
    note: str | None = None


class CollectorStore:
    def __init__(self, metadata_dir: Path) -> None:
        self.metadata_dir = Path(metadata_dir)
        self.settings_path = self.metadata_dir / SETTINGS_FILENAME
        self.runs_path = self.metadata_dir / RUNS_FILENAME
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ settings
    def load_settings(self) -> tuple[CollectorSettings, bool]:
        """Return `(settings, from_file)`. A missing or invalid file gives the defaults (off)."""
        if not self.settings_path.is_file():
            return CollectorSettings(), False
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return CollectorSettings.model_validate(data), True
        except (OSError, ValueError, ValidationError) as exc:
            logger.warning("collector settings unreadable (%s); using defaults (off)", exc)
            return CollectorSettings(), False

    def save_settings(self, settings: CollectorSettings) -> None:
        payload = settings.model_dump_json(indent=2).encode("utf-8")
        with self._lock:
            self.metadata_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.settings_path.with_name(f".{self.settings_path.name}.tmp")
            tmp.write_bytes(payload)
            os.replace(tmp, self.settings_path)

    # ------------------------------------------------------------------ run log
    def append_runs(self, records: list[RunRecord]) -> None:
        if not records:
            return
        lines = "".join(r.model_dump_json() + "\n" for r in records)
        with self._lock:
            self.metadata_dir.mkdir(parents=True, exist_ok=True)
            with self.runs_path.open("a", encoding="utf-8") as fh:
                fh.write(lines)
                fh.flush()
                os.fsync(fh.fileno())

    def read_runs(self, limit: int | None = None) -> list[RunRecord]:
        """Run records oldest first; unreadable lines are skipped with a warning."""
        if not self.runs_path.is_file():
            return []
        out: list[RunRecord] = []
        with self._lock:
            text = self.runs_path.read_text(encoding="utf-8")
        for n, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                out.append(RunRecord.model_validate_json(line))
            except ValidationError:
                logger.warning("collector run log line %d unreadable; skipped", n)
        return out[-limit:] if limit else out
