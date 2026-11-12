"""Option Backtest Analyzer service: replay one bull put spread over the collected history.

Used by `src/web/backtest_api.py` (`GET /api/backtest/entries/{ticker}`, `POST /api/backtest/run`):
runs engine version 2 (`src/analytics/backtest.py`) for one trade from one of three sources:
`LEGS` (own spread), `AUTO_BUILD` (Strategy Lab Auto-Builder on the entry snapshot) or `TRACKED`
(a spread in the tracker registry). Blocking (file reads + engine): the web layer calls these
functions in a worker thread. Read-only: no IBKR calls, nothing is written; the engine receives
the configured `tracker.tracked_dir`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analytics.backtest import (
    BacktestResult,
    backtest_bull_put_from_repo,
    backtest_tracked_spread,
)
from src.analytics.strategies import StrategyLegInput
from src.core.calendar import US_EASTERN
from src.core.exceptions import InvalidRequestError
from src.storage.collector_store import RUNS_FILENAME as COLLECTOR_RUNS_FILENAME
from src.storage.quality import evaluate_snapshot

if TYPE_CHECKING:
    from src.services.analyzer import AnalyzerService

POLAND: Final = ZoneInfo("Europe/Warsaw")
SNAPSHOT_ID_PATTERN: Final = r"^[A-Z]{1,5}_\d{8}_\d{6}$"
SPREAD_ID_PATTERN: Final = r"^[A-Z]{1,5}-\d{8}-[0-9a-f]{8}$"
ASSUMPTIONS: Final[tuple[str, ...]] = (
    "Entry and every mark at mid (bid/ask midpoint, then the Strategy Lab fallback chain).",
    "No commissions or fees.",
    "No early assignment; the spread is held to expiry and settled at intrinsic value.",
    "Quotes are 15-minute delayed (Type 3) unless a step shows another market data type.",
    "Tracked quotes are taken a few seconds after the normal snapshot of the same slot.",
    "No dividends.",
)


# ------------------------------------------------------------------ models
class BacktestRunRequest(BaseModel):
    """`POST /api/backtest/run`. Required fields depend on `source`."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["LEGS", "AUTO_BUILD", "TRACKED"]
    ticker: str | None = Field(default=None, min_length=1, max_length=10)
    entry_snapshot_id: str | None = Field(default=None, pattern=SNAPSHOT_ID_PATTERN)
    expiry: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    short_strike: float | None = Field(default=None, gt=0, lt=1e6)
    long_strike: float | None = Field(default=None, gt=0, lt=1e6)
    qty: int = Field(default=1, ge=1, le=100)  # same limit as Strategy Lab legs
    spread_id: str | None = Field(default=None, pattern=SPREAD_ID_PATTERN)

    @model_validator(mode="after")
    def _fields_for_source(self) -> BacktestRunRequest:
        if self.source == "TRACKED":
            if not self.spread_id:
                raise ValueError("source TRACKED needs spread_id")
            return self
        if not self.ticker or not self.entry_snapshot_id:
            raise ValueError(f"source {self.source} needs ticker and entry_snapshot_id")
        if self.source == "LEGS":
            if self.expiry is None or self.short_strike is None or self.long_strike is None:
                raise ValueError("source LEGS needs expiry, short_strike and long_strike")
            if self.long_strike >= self.short_strike:
                raise ValueError(
                    f"The long put strike ({self.long_strike:g}) must be below the short put "
                    f"strike ({self.short_strike:g})"
                )
        return self


class BacktestWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str


class TrackedMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spread_id: str
    source: str
    status: str


class CollectorState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    ever_enabled: bool


class BacktestRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["LEGS", "AUTO_BUILD", "TRACKED"]
    spread_id: str | None
    result: BacktestResult
    ts_poland: list[str]  # aligned with result.steps
    settlement_ts_poland: str | None
    outcome: Literal["SETTLED", "OPEN", "UNRESOLVED"]
    days_held: float
    history_snapshot_count: int
    tracked: TrackedMatch | None  # an ACTIVE tracked spread with the same legs
    previously_tracked: list[TrackedMatch]  # STOPPED / EXPIRED spreads with the same legs
    collector: CollectorState
    warnings: list[BacktestWarning]
    assumptions: list[str]


class BacktestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    ts_utc: str
    ts_et: str
    ts_poland: str
    collection_mode: str | None
    contract_count: int
    expirations: list[str]
    quote_coverage: float | None
    spot_source: str | None
    market_data_type: int
    usable: bool
    quality_reasons: list[str]


class BacktestEntriesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    entries: list[BacktestEntry]  # newest first


# ------------------------------------------------------------------ helpers
def _parse_utc(text: str) -> datetime:
    value = text[:-1] + "+00:00" if text.endswith("Z") else text
    dt = datetime.fromisoformat(value)
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _fmt(ts: datetime, zone: ZoneInfo, label: str) -> str:
    return ts.astimezone(zone).strftime(f"%Y-%m-%d %H:%M {label}")


def _tracked_dir(svc: AnalyzerService) -> Path:
    path = Path(svc.settings.tracker.tracked_dir)
    return path if path.is_absolute() else svc.repo.root / path


def _snapshot_count(svc: AnalyzerService, ticker: str) -> int:
    folder = svc.repo.snapshots_dir / ticker
    if not folder.is_dir():
        return 0
    return sum(1 for p in folder.glob(f"{ticker}_*.csv") if not p.name.startswith("."))


def _legs_key(legs: list[Any]) -> tuple[tuple[str, str, float, str], ...]:
    return tuple(
        sorted((leg.side, leg.right, round(float(leg.strike), 4), leg.expiry) for leg in legs)
    )


def _matches(
    svc: AnalyzerService, ticker: str, legs: list[Any]
) -> tuple[TrackedMatch | None, list[TrackedMatch]]:
    key = _legs_key(legs)
    active: TrackedMatch | None = None
    previous: list[TrackedMatch] = []
    for spread in svc.get_tracker().spreads():
        if spread.ticker != ticker or spread.legs_key() != key:
            continue
        match = TrackedMatch(spread_id=spread.id, source=spread.source, status=spread.status)
        if spread.status == "ACTIVE":
            active = active or match
        else:
            previous.append(match)
    return active, previous


def _warnings(
    svc: AnalyzerService, result: BacktestResult, ticker: str, history: int
) -> list[BacktestWarning]:
    out: list[BacktestWarning] = []
    snapshot_steps = [s for s in result.steps if s.kind == "SNAPSHOT"]
    if history < 2 or len(snapshot_steps) < 2:
        out.append(
            BacktestWarning(
                code="FEW_SNAPSHOTS",
                message=(
                    f"Only {len(snapshot_steps)} snapshot step(s) from the entry on "
                    f"({history} saved for {ticker}); the replay says little yet."
                ),
            )
        )
    if result.summary.steps_ok == 0:
        out.append(
            BacktestWarning(
                code="NO_OK_STEPS",
                message="No step is OK: every value is flagged or missing; no summary numbers.",
            )
        )
    if result.settlement.status == "NOT_REACHED":
        out.append(
            BacktestWarning(
                code="TRADE_OPEN",
                message=(
                    f"Trade still open: the data ends before the {result.settlement.expiry} "
                    "expiry; the last P&L is a mark, not a result."
                ),
            )
        )
    elif result.settlement.status == "UNRESOLVED":
        out.append(
            BacktestWarning(
                code="SETTLEMENT_UNRESOLVED",
                message="Expiry passed but no usable closing spot exists; the outcome is unknown.",
            )
        )
    if any("CALENDAR_UNKNOWN" in s.flags for s in result.steps):
        out.append(
            BacktestWarning(
                code="CALENDAR_UNKNOWN",
                message="Some steps are beyond the NYSE holiday table; holidays there are unknown.",
            )
        )
    skipped = result.reproducibility.skipped_run_log_lines
    if skipped:
        out.append(
            BacktestWarning(
                code="SKIPPED_RUN_LOG_LINES",
                message=f"Unreadable run-log lines were skipped: {', '.join(skipped)}.",
            )
        )
    tracked_dir = _tracked_dir(svc) / ticker
    has_tracked = tracked_dir.is_dir() and any(tracked_dir.glob(f"{ticker}_*.csv"))
    collector_log = svc.repo.index_path.parent / COLLECTOR_RUNS_FILENAME
    collector_bad = not collector_log.is_file() or any(
        s.startswith(COLLECTOR_RUNS_FILENAME) for s in skipped
    )
    if has_tracked and collector_bad:
        out.append(
            BacktestWarning(
                code="COLLECTOR_LOG_MISSING",
                message=(
                    f"Tracked quotes exist but {COLLECTOR_RUNS_FILENAME} is missing or partly "
                    "unreadable: snapshots cannot be matched to their slots, so tracked slots "
                    "may appear as extra steps next to their snapshots (doubled data)."
                ),
            )
        )
    return out


def _run_engine(svc: AnalyzerService, req: BacktestRunRequest) -> tuple[BacktestResult, str]:
    """Blocking engine call (worker thread). Returns `(result, ticker)`."""
    tracked_dir = _tracked_dir(svc)
    rfr = svc.settings.analytics.risk_free_rate
    if req.source == "TRACKED":
        result = backtest_tracked_spread(
            svc.repo, req.spread_id or "", risk_free_rate=rfr, tracked_dir=tracked_dir
        )
        return result, result.ticker
    ticker = svc.validate_ticker(req.ticker or "")
    if req.source == "LEGS":
        legs = [
            StrategyLegInput(
                side="SELL",
                right="P",
                expiry=req.expiry or "",
                strike=req.short_strike or 0.0,
                qty=req.qty,
            ),
            StrategyLegInput(
                side="BUY",
                right="P",
                expiry=req.expiry or "",
                strike=req.long_strike or 0.0,
                qty=req.qty,
            ),
        ]
        result = backtest_bull_put_from_repo(
            svc.repo,
            ticker,
            req.entry_snapshot_id or "",
            mode="LEGS",
            legs=legs,
            risk_free_rate=rfr,
            tracked_dir=tracked_dir,
        )
    else:
        result = backtest_bull_put_from_repo(
            svc.repo,
            ticker,
            req.entry_snapshot_id or "",
            mode="AUTO_BUILD",
            qty=req.qty,
            risk_free_rate=rfr,
            tracked_dir=tracked_dir,
        )
    return result, ticker


# ------------------------------------------------------------------ service functions
def list_backtest_entries(svc: AnalyzerService, ticker: str) -> BacktestEntriesResponse:
    """Entry-step choices for `ticker`, newest first (blocking)."""
    symbol = svc.validate_ticker(ticker)
    metas = svc.repo.list_snapshots(symbol)  # newest first
    entries = []
    for meta in metas:
        verdict = evaluate_snapshot(meta)
        ts = _parse_utc(meta.snapshot_ts_utc)
        entries.append(
            BacktestEntry(
                snapshot_id=meta.snapshot_id,
                ts_utc=meta.snapshot_ts_utc,
                ts_et=_fmt(ts, US_EASTERN, "ET"),
                ts_poland=_fmt(ts, POLAND, "PL"),
                collection_mode=meta.collection_mode,
                contract_count=meta.contract_count,
                expirations=list(meta.expirations),
                quote_coverage=meta.quote_coverage,
                spot_source=meta.spot_source,
                market_data_type=meta.market_data_type,
                usable=verdict.usable,
                quality_reasons=list(verdict.reasons),
            )
        )
    return BacktestEntriesResponse(ticker=symbol, entries=entries)


def run_backtest(svc: AnalyzerService, payload: BacktestRunRequest) -> BacktestRunResponse:
    """Run one backtest (blocking). Invalid legs / no valid spread -> `InvalidRequestError`."""
    try:
        result, ticker = _run_engine(svc, payload)
    except InvalidRequestError:
        raise
    except ValueError as exc:  # invalid legs, contract not in the snapshot, no valid spread
        raise InvalidRequestError(str(exc)) from exc

    history = _snapshot_count(svc, ticker)
    tracked, previous = _matches(svc, ticker, result.entry.legs)
    collector = svc.get_collector()
    ever = bool(collector.settings.enabled_since_utc) or collector.store.runs_path.is_file()
    entry_ts = _parse_utc(result.entry.ts_utc)
    settle_ts: datetime | None = None
    if result.settlement.status == "SETTLED" and result.settlement.ts_et:
        settle_ts = datetime.strptime(result.settlement.ts_et, "%Y-%m-%d %H:%M ET").replace(
            tzinfo=US_EASTERN
        )
    last_ts = settle_ts or (_parse_utc(result.steps[-1].ts_utc) if result.steps else entry_ts)
    outcome: Literal["SETTLED", "OPEN", "UNRESOLVED"] = (
        "SETTLED"
        if result.settlement.status == "SETTLED"
        else ("OPEN" if result.settlement.status == "NOT_REACHED" else "UNRESOLVED")
    )
    return BacktestRunResponse(
        source=payload.source,
        spread_id=payload.spread_id,
        result=result,
        ts_poland=[_fmt(_parse_utc(s.ts_utc), POLAND, "PL") for s in result.steps],
        settlement_ts_poland=_fmt(settle_ts, POLAND, "PL") if settle_ts else None,
        outcome=outcome,
        days_held=round((last_ts - entry_ts).total_seconds() / 86400.0, 2),
        history_snapshot_count=history,
        tracked=tracked,
        previously_tracked=previous,
        collector=CollectorState(enabled=collector.settings.enabled, ever_enabled=ever),
        warnings=_warnings(svc, result, ticker, history),
        assumptions=list(ASSUMPTIONS),
    )
