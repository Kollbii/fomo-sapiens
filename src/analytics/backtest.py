"""Bull put spread backtest over saved snapshots (pure, read-only, deterministic).

Each saved snapshot of a ticker is one time step. The engine opens the spread on the entry
snapshot (mode A: user legs, mode B: Strategy Lab Wall Auto-Builder `BULL_PUT_SPREAD`), then
marks the same contracts (expiry, strike, right) on every later snapshot and settles at
intrinsic value after the short expiry when a valid closing spot exists.

Rules:
- Read-only: no IBKR calls, nothing is written (the snapshot index is read, never rebuilt).
- No corridor overlay; prices come from the saved CSVs only.
- Flag, never skip: every step is returned with a status (`OK` / `UNRELIABLE` / `INCOMPLETE`)
  and its flags; summaries use `OK` steps only. Missing prices are never estimated.
- Exit rule: hold to expiry, or stop at an optional end snapshot.

Tracked quotes (engine version 2, `src/analytics/tracked_history.py`): when the position
tracker recorded the legs of a spread in a scheduled slot, a leg missing from that slot's normal
snapshot (any `MISSING_*`) is marked from the tracked row instead; slots with a tracked file but
no normal snapshot become `TRACKED_ONLY` steps (spot / spot source / data type from the tracker
run log); tracker gaps become `GAP` steps (always `INCOMPLETE`, ignored by the summary).
Session times, the expiry close and the settlement time come from the NYSE calendar in
`src/core/calendar.py` (early closes 13:00 ET -> settlement spot at/after 13:15 ET).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import BaseModel, ConfigDict, ValidationError

from src.analytics import strategies as _strategies
from src.analytics.bsm_fallback import DEFAULT_RISK_FREE_RATE, BSMFallbackEngine
from src.analytics.strategies import (
    EvaluatedLeg,
    StrategyLegInput,
    evaluate_strategy,
)
from src.analytics.tracked_history import (
    TRACKER_GAP_STATUSES,
    TrackedSlot,
    TrackedSpreadInfo,
    contract_key,
    load_tracked_history,
    read_tracked_spreads,
)
from src.analytics.walls import compute_walls
from src.core.calendar import (
    CALENDAR_LAST_DAY,
    CALENDAR_SOURCE_URL,
    DATA_DELAY,
    calendar_covers,
    session_for,
)
from src.core.exceptions import SnapshotNotFoundError
from src.core.security import TickerValidator
from src.storage.csv_repository import CSVRepository
from src.storage.quality import MIN_QUOTE_COVERAGE, USABLE_MARKET_DATA_TYPES, measure_quote_quality
from src.storage.schema import SNAPSHOT_ID_RE, SnapshotMeta, infer_chain_step_and_range

# ------------------------------------------------------------------ constants (one place)
ENGINE_VERSION: Final = "2"
US_EASTERN: Final = ZoneInfo("America/New_York")  # same zoneinfo source as src/broker/contracts.py
# Fallback session times, used only when the calendar has no session for a date.
SESSION_OPEN_ET: Final = (9, 30)
SESSION_CLOSE_ET: Final = (16, 0)
# Settlement spot: last snapshot on the expiry date at/after the close + 15 min (16:15 ET, or
# 13:15 ET on early closes; allows for the 15-min delay of Type 3 data) whose spot source is
# not the previous close; else the tracker run-log spot of such a slot.
SETTLEMENT_DELAY: Final = DATA_DELAY
SETTLEMENT_MIN_TIME_ET: Final = (16, 15)  # regular days; see `_settlement_time`
WALLS_TOP_N: Final = 3  # same default as `analytics.top_walls_count` / `compute_walls`
RELIABLE_PRICE_SOURCES: Final = frozenset({"BID_ASK"})
# A bull put spread is worth between 0 and its strike width per share; outside that (beyond
# this tolerance) the quotes are inconsistent (stale, crossed or pre-market).
SPREAD_BOUNDS_TOLERANCE: Final = 0.005
QUALITY_THRESHOLDS: Final[dict[str, Any]] = {
    "min_quote_coverage": MIN_QUOTE_COVERAGE,
    "reliable_market_data_types": sorted(USABLE_MARKET_DATA_TYPES),
    "reliable_price_sources": sorted(RELIABLE_PRICE_SOURCES),
    "session_et": (
        "NYSE calendar (src/core/calendar.py): 09:30-16:00 ET, 13:00 on early closes, "
        f"no session on holidays; table until {CALENDAR_LAST_DAY.isoformat()}"
    ),
    "settlement_min_time_et": "close + 15 min (16:15, or 13:15 on early closes)",
    "calendar_source_url": CALENDAR_SOURCE_URL,
    "spread_bounds_tolerance_per_share": SPREAD_BOUNDS_TOLERANCE,
}
# Strategy Lab values the entry depends on, as of the hashed `strategies.py` (the hash is the
# authority; these are recorded for readability).
STRATEGY_LAB_CONSTANTS: Final[dict[str, Any]] = {
    "target_dte": 38,
    "bull_put_short_delta_target": 0.28,
    "bull_put_anchor_fallback_pct_of_spot": 0.95,
    "bull_put_wall_max_pct_of_spot": 1.02,
    "wall_bonus": 22.0,
    "wing_width_pct_of_spot": 0.025,
    "fallback_iv": 0.35,
    "entry_price": "mid",
}

StepStatus = Literal["OK", "UNRELIABLE", "INCOMPLETE"]
PriceSource = Literal["BID_ASK", "MID", "MARK", "LAST", "CLOSE"]
LegStatus = Literal[
    "PRICED",
    "NO_PRICE",
    "MISSING_OUTSIDE_DTE_WINDOW",
    "MISSING_OUTSIDE_STRIKE_RANGE",
    "MISSING_NOT_LISTED",
    "MISSING_NO_DATA",  # tracker slot without any quote for the leg (gap / not in the file)
]
QuoteSource = Literal["SNAPSHOT", "TRACKED"]
StepKind = Literal["SNAPSHOT", "TRACKED_ONLY", "GAP"]
SpotOrigin = Literal["SNAPSHOT", "TRACKER_RUN_LOG"]
SettlementStatus = Literal["SETTLED", "UNRESOLVED", "NOT_REACHED"]
EntryMode = Literal["LEGS", "AUTO_BUILD"]

# Flags that make a step UNRELIABLE (a missing / unpriced leg makes it INCOMPLETE instead).
UNRELIABLE_FLAGS: Final = frozenset(
    {
        "LEG_FALLBACK_PRICE",
        "LOW_QUOTE_COVERAGE",
        "SPOT_SOURCE_CLOSE",
        "FROZEN_DATA",
        "OFF_SESSION",
        "SPREAD_VALUE_OUT_OF_BOUNDS",
    }
)
# Informational flags (shown, do not change the status).
INFO_FLAGS: Final = frozenset(
    {
        "SPOT_SOURCE_UNKNOWN",
        "FILTER_CHANGED",
        "MIXED_QUOTE_SOURCES",  # normal-snapshot and tracked rows in one step
        "TRACKED_ONLY_STEP",  # no normal snapshot in this slot; tracked rows only
        "CALENDAR_UNKNOWN",  # date beyond the calendar table (holidays unknown)
    }
)
# Why a leg has no tracked fallback (the step is INCOMPLETE; nothing is estimated).
TRACKER_FLAGS: Final = frozenset(
    {
        "TRACKER_MISSED",  # app not running at the planned slot
        "TRACKER_FAILED",  # tracker step failed (Gateway down, timeout, ...)
        "TRACKER_SKIPPED",  # refresh lock busy
        "TRACKER_NO_QUOTE",  # tracked file has the leg but no price
        "TRACKER_PARTIAL",  # tracker recorded only part of the legs
        "TRACKER_STOPPED",  # the tracked spread was stopped before this step
        "TRACKER_FILE_MISSING",  # file named in the run log is missing / unreadable
    }
)


# ------------------------------------------------------------------ inputs
@dataclass(frozen=True)
class BacktestStep:
    """One saved snapshot: id, UTC timestamp, chain rows, optional index metadata."""

    snapshot_id: str
    ts_utc: datetime
    df: pl.DataFrame
    meta: SnapshotMeta | None = None
    kind: StepKind = "SNAPSHOT"
    slot_utc: datetime | None = None
    # Tracker data of this slot (paired only with the slot's first normal snapshot).
    tracked: TrackedSlot | None = None
    # Tracked spreads of the ticker (registry), shared by all steps of one load.
    tracked_spreads: tuple[TrackedSpreadInfo, ...] = field(default=(), repr=False)
    # Run-log lines that could not be read in this load (reported in the result).
    skipped_run_log_lines: tuple[str, ...] = ()


# ------------------------------------------------------------------ outputs
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LegMark(_Model):
    side: Literal["BUY", "SELL"]
    right: Literal["C", "P"]
    expiry: str
    strike: float
    qty: int
    status: LegStatus
    price: float | None
    price_source: PriceSource | None
    bid: float | None
    ask: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    quote_source: QuoteSource | None = None


class StepGreeks(_Model):
    delta_shares: float
    gamma_shares: float
    theta_daily_dollars: float
    vega_per_1pct_dollars: float


class StepResult(_Model):
    snapshot_id: str
    ts_utc: str
    ts_et: str
    market_date: str
    market_data_type: int | None  # None only on GAP steps
    spot: float | None  # None only on GAP steps
    spot_source: str | None
    quote_coverage: float | None  # None only on GAP steps
    short_dte: int
    legs: list[LegMark]
    spread_value_per_share: float | None
    position_value_dollars: float | None
    pnl_dollars: float | None
    pnl_pct_of_max_risk: float | None
    net_greeks: StepGreeks | None
    flags: list[str]
    status: StepStatus
    kind: StepKind = "SNAPSHOT"
    slot_utc: str | None = None
    tracked_snapshot_id: str | None = None
    tracker_run_entries: list[str] = []


class Settlement(_Model):
    status: SettlementStatus
    expiry: str
    snapshot_id: str | None = None
    ts_et: str | None = None
    spot: float | None = None
    intrinsic_spread_per_share: float | None = None
    pnl_dollars: float | None = None
    pnl_pct_of_max_risk: float | None = None
    note: str
    spot_source: str | None = None
    market_data_type: int | None = None
    spot_from: SpotOrigin | None = None
    tracker_run_entry: str | None = None


class EntrySummary(_Model):
    snapshot_id: str
    ts_utc: str
    ts_et: str
    spot: float
    legs: list[EvaluatedLeg]
    entry_cash_mid_dollars: float
    entry_cash_natural_dollars: float
    natural_slippage_dollars: float
    net_mid_per_share: float
    max_profit_dollars: float
    max_loss_dollars: float
    breakevens: list[float]
    rationale: list[str]


class BacktestSummary(_Model):
    steps_total: int
    steps_ok: int
    steps_unreliable: int
    steps_incomplete: int
    last_ok_snapshot_id: str | None
    last_ok_pnl_dollars: float | None
    min_ok_pnl_dollars: float | None
    max_ok_pnl_dollars: float | None
    steps_gap: int = 0  # GAP steps are listed but not counted in the numbers above


class Reproducibility(_Model):
    engine_version: str
    strategies_sha256: str
    strategy_lab_constants: dict[str, Any]
    quality_thresholds: dict[str, Any]
    risk_free_rate: float
    walls_top_n: int
    entry_snapshot_id: str
    step_snapshot_ids: list[str]
    end_snapshot_id: str | None
    tracked_data_used: bool = False
    tracked_files: list[str] = []
    tracker_run_entries: list[str] = []
    skipped_run_log_lines: list[str] = []


class BacktestResult(_Model):
    ticker: str
    strategy: Literal["BULL_PUT_SPREAD"]
    mode: EntryMode
    qty: int
    entry: EntrySummary
    steps: list[StepResult]
    settlement: Settlement
    summary: BacktestSummary
    notes: list[str]
    reproducibility: Reproducibility


# ------------------------------------------------------------------ helpers
def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def resolve_price_with_source(row: dict[str, Any]) -> tuple[float, PriceSource | None]:
    """Same value as the Strategy Lab leg mid (`strategies._resolve_row_mid`), plus its origin.

    Order: bid/ask midpoint (valid bid > 0, ask >= bid) -> `mid` -> `mark_price` -> `last` ->
    `close`. Stored `mark_price` is itself derived from mid / last / close, so its origin is
    reported as `LAST` or `CLOSE` when it equals that field. `(0.0, None)` = no price.
    """
    bid = _opt_float(row.get("bid"))
    ask = _opt_float(row.get("ask"))
    if bid is not None and ask is not None and bid > 0.0 and ask >= bid:
        return (bid + ask) / 2.0, "BID_ASK"
    last = _opt_float(row.get("last"))
    close = _opt_float(row.get("close"))
    for key in ("mid", "mark_price", "last", "close"):
        val = _opt_float(row.get(key))
        if val is None or val <= 0.0:
            continue
        if key == "mid":
            return val, "MID"
        if key == "mark_price":
            if last is not None and last > 0.0 and abs(val - last) < 1e-9:
                return val, "LAST"
            if close is not None and close > 0.0 and abs(val - close) < 1e-9:
                return val, "CLOSE"
            return val, "MARK"
        return val, "LAST" if key == "last" else "CLOSE"
    return 0.0, None


def strategies_source_sha256() -> str:
    """SHA-256 of the Strategy Lab source: every `src/analytics/strategies/*.py`, sorted by
    name, each prefixed with its file name; line endings normalized (stable across checkouts)."""
    digest = hashlib.sha256()
    for path in sorted(Path(_strategies.__file__).parent.glob("*.py")):
        digest.update(path.name.encode("utf-8") + b"\n")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def _parse_ts_utc(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = datetime.fromisoformat(text)
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _et(ts: datetime) -> datetime:
    return ts.astimezone(US_EASTERN)


def _fmt_et(ts: datetime) -> str:
    return _et(ts).strftime("%Y-%m-%d %H:%M ET")


def _in_session(ts: datetime) -> bool:
    """Inside the regular session of its ET date (NYSE calendar: holidays, early closes)."""
    session = session_for(_et(ts).date())
    return session is not None and session.open_utc <= ts < session.close_utc


def _close_utc(day: date) -> datetime:
    """Session close of `day` (16:00 ET, 13:00 on early closes; 16:00 if no session)."""
    session = session_for(day)
    if session is not None:
        return session.close_utc
    hh, mm = SESSION_CLOSE_ET
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=US_EASTERN).astimezone(UTC)


def _settlement_time(expiry: date) -> datetime:
    return _close_utc(expiry) + SETTLEMENT_DELAY


def _enrich(df: pl.DataFrame, risk_free_rate: float) -> pl.DataFrame:
    """Fill missing Greeks the same way the API does before the Strategy Lab runs."""
    needs_bsm = df.filter((pl.col("greeks_source") == "NONE") | pl.col("iv").is_null()).height > 0
    if not needs_bsm:
        return df
    return BSMFallbackEngine.enrich_missing_greeks(df, risk_free_rate=risk_free_rate)


def validate_bull_put_legs(legs: list[StrategyLegInput]) -> None:
    """Mode A structure check; every message names the problem."""
    if len(legs) != 2:
        raise ValueError(f"A bull put spread needs exactly 2 legs, got {len(legs)}")
    rights = {leg.right for leg in legs}
    if rights != {"P"}:
        raise ValueError("Both legs of a bull put spread must be puts (right='P')")
    if legs[0].expiry != legs[1].expiry:
        raise ValueError(
            f"Both legs must share one expiry, got {legs[0].expiry} and {legs[1].expiry}"
        )
    sides = sorted(leg.side for leg in legs)
    if sides != ["BUY", "SELL"]:
        raise ValueError("A bull put spread needs one SELL leg and one BUY leg")
    if any(leg.qty <= 0 for leg in legs):
        raise ValueError("Leg quantities must be positive")
    if legs[0].qty != legs[1].qty:
        raise ValueError(f"Leg quantities must match, got {legs[0].qty} and {legs[1].qty}")
    short = next(leg for leg in legs if leg.side == "SELL")
    long = next(leg for leg in legs if leg.side == "BUY")
    if abs(short.strike - long.strike) < 1e-9:
        raise ValueError(f"The two legs need different strikes, both are {short.strike:g}")
    if short.strike < long.strike:
        raise ValueError(
            f"The SELL put strike ({short.strike:g}) must be above the BUY put strike "
            f"({long.strike:g}) for a bull put spread"
        )


# ------------------------------------------------------------------ loading (read-only)
def _read_index_meta(repo: CSVRepository, symbol: str) -> dict[str, SnapshotMeta] | None:
    """Index entries for `symbol` read straight from disk. Never rebuilds or writes the index."""
    path = repo.index_path
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("tickers", {}).get(symbol, {}).get("snapshots", [])
        metas = [SnapshotMeta.model_validate(e) for e in entries]
    except (OSError, ValueError, AttributeError, ValidationError):
        return None
    return {m.snapshot_id: m for m in metas}


def load_backtest_steps(
    repo: CSVRepository,
    ticker: str,
    entry_snapshot_id: str,
    end_snapshot_id: str | None = None,
    *,
    tracked_dir: Path | None = None,
) -> tuple[list[BacktestStep], list[str]]:
    """Entry snapshot plus every later snapshot (up to `end_snapshot_id`), in time order.

    Lists `data/snapshots/{TICKER}/*.csv` and loads each file with
    `CSVRepository.load_snapshot` (read-only). Tracker data (`tracked_dir`, default
    `data/tracked` under the repository root, plus the run logs and registry in the metadata
    folder) is attached read-only: see `src/analytics/tracked_history.py`. Returns
    `(steps, notes)`.
    """
    symbol = TickerValidator.validate(ticker)
    notes: list[str] = []
    ticker_dir = repo.snapshots_dir / symbol
    ids = (
        sorted(
            p.stem
            for p in ticker_dir.glob("*.csv")
            if not p.name.startswith(".")
            and SNAPSHOT_ID_RE.fullmatch(p.stem)
            and p.stem.startswith(f"{symbol}_")
        )
        if ticker_dir.is_dir()
        else []
    )
    if not ids:
        raise SnapshotNotFoundError(f"No snapshot history for {symbol}")
    if entry_snapshot_id not in ids:
        raise SnapshotNotFoundError(f"Entry snapshot not found: {entry_snapshot_id}")
    if end_snapshot_id is not None and end_snapshot_id not in ids:
        raise SnapshotNotFoundError(f"End snapshot not found: {end_snapshot_id}")

    index_meta = _read_index_meta(repo, symbol)
    if index_meta is None:
        notes.append("Snapshot index unavailable; spot_source / collection_mode unknown.")

    steps: list[BacktestStep] = []
    for sid in ids:
        try:
            df = repo.load_snapshot(symbol, sid)
        except Exception as exc:  # corrupt file: reported, never silently dropped
            notes.append(f"Skipped unreadable snapshot {sid}: {exc}")
            continue
        if df.height == 0:
            notes.append(f"Skipped empty snapshot {sid}")
            continue
        ts = _parse_ts_utc(str(df["snapshot_ts_utc"][0]))
        steps.append(BacktestStep(sid, ts, df, (index_meta or {}).get(sid)))
    steps.sort(key=lambda s: (s.ts_utc, s.snapshot_id))

    entry_pos = next((i for i, s in enumerate(steps) if s.snapshot_id == entry_snapshot_id), None)
    if entry_pos is None:
        raise SnapshotNotFoundError(f"Entry snapshot unreadable: {entry_snapshot_id}")
    end_pos = len(steps) - 1
    if end_snapshot_id is not None:
        found = next((i for i, s in enumerate(steps) if s.snapshot_id == end_snapshot_id), None)
        if found is None:
            raise SnapshotNotFoundError(f"End snapshot unreadable: {end_snapshot_id}")
        if found < entry_pos:
            raise ValueError(f"End snapshot {end_snapshot_id} is before the entry snapshot")
        end_pos = found
    window = steps[entry_pos : end_pos + 1]
    end_ts = window[-1].ts_utc if end_snapshot_id is not None else None
    steps_out = _attach_tracked(repo, symbol, window, notes, tracked_dir=tracked_dir, end_ts=end_ts)
    return steps_out, notes


def _attach_tracked(
    repo: CSVRepository,
    symbol: str,
    window: list[BacktestStep],
    notes: list[str],
    *,
    tracked_dir: Path | None,
    end_ts: datetime | None,
) -> list[BacktestStep]:
    """Pair normal steps with tracker slots; add TRACKED_ONLY and GAP steps (read-only)."""
    history = load_tracked_history(repo, symbol, tracked_dir=tracked_dir)
    notes.extend(history.issues)
    skipped = tuple(history.skipped_lines)
    if not (history.slots or history.snapshot_slots or history.spreads or skipped):
        return window
    start = window[0].ts_utc
    spreads = history.spreads
    out: list[BacktestStep] = []
    paired_slots: set[datetime] = set()
    for step in window:
        slot_info = history.snapshot_slots.get(step.snapshot_id)
        if slot_info is None:
            out.append(_with(step, tracked_spreads=spreads, skipped_run_log_lines=skipped))
            continue
        slot, first = slot_info
        tracked = history.slots.get(slot) if first else None
        if tracked is not None:
            paired_slots.add(slot)
        out.append(
            _with(
                step,
                slot_utc=slot,
                tracked=tracked,
                tracked_spreads=spreads,
                skipped_run_log_lines=skipped,
            )
        )
    for slot, tracked in sorted(history.slots.items()):
        if slot in paired_slots:
            continue
        ts = tracked.file_ts_utc or slot
        if ts <= start:
            continue  # tracker data before the entry is not part of this run
        if end_ts is not None and ts > end_ts:
            continue  # after the requested end snapshot
        if tracked.df is not None and tracked.tracked_snapshot_id is not None:
            out.append(
                BacktestStep(
                    tracked.tracked_snapshot_id,
                    ts,
                    tracked.df,
                    None,
                    kind="TRACKED_ONLY",
                    slot_utc=slot,
                    tracked=tracked,
                    tracked_spreads=spreads,
                    skipped_run_log_lines=skipped,
                )
            )
        else:
            out.append(
                BacktestStep(
                    f"GAP_{symbol}_{slot:%Y%m%d_%H%M%S}",
                    slot,
                    tracked.df if tracked.df is not None else pl.DataFrame(),
                    None,
                    kind="GAP",
                    slot_utc=slot,
                    tracked=tracked,
                    tracked_spreads=spreads,
                    skipped_run_log_lines=skipped,
                )
            )
    out.sort(key=lambda s: (s.ts_utc, s.snapshot_id))
    return out


def _with(step: BacktestStep, **changes: Any) -> BacktestStep:
    return BacktestStep(
        step.snapshot_id,
        step.ts_utc,
        step.df,
        step.meta,
        kind=changes.get("kind", step.kind),
        slot_utc=changes.get("slot_utc", step.slot_utc),
        tracked=changes.get("tracked", step.tracked),
        tracked_spreads=changes.get("tracked_spreads", step.tracked_spreads),
        skipped_run_log_lines=changes.get("skipped_run_log_lines", step.skipped_run_log_lines),
    )


# ------------------------------------------------------------------ marking
def _missing_reason(df: pl.DataFrame, leg: EvaluatedLeg, leg_dte: int) -> LegStatus:
    """Why a contract is absent: outside the snapshot's DTE window, outside its strike range
    for that expiry, or simply not listed (e.g. a different strike step)."""
    dte_min = int(df["dte"].min())  # type: ignore[arg-type]
    dte_max = int(df["dte"].max())  # type: ignore[arg-type]
    if leg_dte < dte_min or leg_dte > dte_max:
        return "MISSING_OUTSIDE_DTE_WINDOW"
    same_exp = df.filter((pl.col("expiry") == leg.expiry) & (pl.col("right") == leg.right))
    if same_exp.height:
        lo = float(same_exp["strike"].min())  # type: ignore[arg-type]
        hi = float(same_exp["strike"].max())  # type: ignore[arg-type]
        if leg.strike < lo - 1e-9 or leg.strike > hi + 1e-9:
            return "MISSING_OUTSIDE_STRIKE_RANGE"
    return "MISSING_NOT_LISTED"


def _mark_leg(
    df: pl.DataFrame,
    leg: EvaluatedLeg,
    market_date: date,
    *,
    quote_source: QuoteSource = "SNAPSHOT",
    absent_status: LegStatus | None = None,
) -> LegMark:
    rows = (
        df.filter(
            (pl.col("expiry") == leg.expiry)
            & (pl.col("right") == leg.right)
            & ((pl.col("strike") - leg.strike).abs() < 1e-4)
        )
        if df.height
        else df
    )
    status: LegStatus
    price: float | None = None
    source: PriceSource | None = None
    row: dict[str, Any] = {}
    if rows.height == 0:
        if absent_status is not None:
            status = absent_status
        else:
            leg_dte = (date.fromisoformat(leg.expiry) - market_date).days
            status = _missing_reason(df, leg, leg_dte)
    else:
        row = rows.row(0, named=True)
        raw_price, source = resolve_price_with_source(row)
        if source is None:
            status = "NO_PRICE"
        else:
            status = "PRICED"
            price = round(raw_price, 4)
    return LegMark(
        side=leg.side,
        right=leg.right,
        expiry=leg.expiry,
        strike=leg.strike,
        qty=leg.qty,
        status=status,
        price=price,
        price_source=source,
        bid=_opt_float(row.get("bid")),
        ask=_opt_float(row.get("ask")),
        delta=_opt_float(row.get("delta")),
        gamma=_opt_float(row.get("gamma")),
        theta=_opt_float(row.get("theta")),
        vega=_opt_float(row.get("vega")),
        quote_source=quote_source if rows.height else None,
    )


def _has_contract(df: pl.DataFrame | None, leg: EvaluatedLeg) -> bool:
    if df is None or df.height == 0:
        return False
    return (
        df.filter(
            (pl.col("expiry") == leg.expiry)
            & (pl.col("right") == leg.right)
            & ((pl.col("strike") - leg.strike).abs() < 1e-4)
        ).height
        > 0
    )


def _round(value: float | None, digits: int) -> float | None:
    """Round and normalize -0.0 to 0.0."""
    if value is None:
        return None
    out = round(value, digits)
    return 0.0 if out == 0 else out


def _spread_width(legs: list[EvaluatedLeg]) -> float:
    short = next(leg for leg in legs if leg.side == "SELL")
    long = next(leg for leg in legs if leg.side == "BUY")
    return short.strike - long.strike


def _sign(side: str) -> float:
    return 1.0 if side == "BUY" else -1.0


def _relevant_spread_ids(
    spreads: tuple[TrackedSpreadInfo, ...], legs: list[EvaluatedLeg]
) -> frozenset[str]:
    """Tracked spreads holding every backtested leg contract (their gaps affect this run)."""
    wanted = frozenset(contract_key(leg.expiry, leg.strike, leg.right) for leg in legs)
    return frozenset(s.spread_id for s in spreads if wanted <= s.contracts)


def _relevant_entries(step: BacktestStep, relevant: frozenset[str]) -> list[str]:
    if step.tracked is None:
        return []
    return [e.ref for e in step.tracked.entries if e.spread_id in relevant]


def _tracker_flags(step: BacktestStep, relevant: frozenset[str]) -> list[str]:
    """Why the tracker has no usable quote for an affected step (documented flags)."""
    flags: list[str] = []
    tracked = step.tracked
    entries = [e for e in tracked.entries if e.spread_id in relevant] if tracked else []
    for entry in entries:
        if entry.status in TRACKER_GAP_STATUSES or entry.status in ("NO_QUOTE", "PARTIAL"):
            flags.append(f"TRACKER_{entry.status}")
    if tracked is not None and tracked.file_missing:
        flags.append("TRACKER_FILE_MISSING")
    if not entries:
        for spread in step.tracked_spreads:
            if (
                spread.spread_id in relevant
                and spread.status == "STOPPED"
                and spread.status_changed_utc is not None
                and spread.status_changed_utc <= step.ts_utc
            ):
                flags.append("TRACKER_STOPPED")
                break
    return flags


def _greeks(marks: list[LegMark]) -> StepGreeks | None:
    if not all(None not in (m.delta, m.gamma, m.theta, m.vega) for m in marks):
        return None
    return StepGreeks(
        delta_shares=round(sum(_sign(m.side) * m.qty * 100 * (m.delta or 0) for m in marks), 2),
        gamma_shares=round(sum(_sign(m.side) * m.qty * 100 * (m.gamma or 0) for m in marks), 2),
        theta_daily_dollars=round(
            sum(_sign(m.side) * m.qty * 100 * (m.theta or 0) for m in marks), 2
        ),
        vega_per_1pct_dollars=round(
            sum(_sign(m.side) * m.qty * 100 * (m.vega or 0) for m in marks), 2
        ),
    )


def _gap_step(step: BacktestStep, legs: list[EvaluatedLeg], relevant: frozenset[str]) -> StepResult:
    """A tracker slot with no quotes at all: INCOMPLETE, nothing estimated."""
    market_date = _et(step.ts_utc).date()
    marks = [
        LegMark(
            side=leg.side,
            right=leg.right,
            expiry=leg.expiry,
            strike=leg.strike,
            qty=leg.qty,
            status="MISSING_NO_DATA",
            price=None,
            price_source=None,
            bid=None,
            ask=None,
            delta=None,
            gamma=None,
            theta=None,
            vega=None,
        )
        for leg in legs
    ]
    flags = [f"LEG_MISSING_NO_DATA:{m.strike:g}{m.right}" for m in marks]
    flags += _tracker_flags(step, relevant)
    if not calendar_covers(market_date):
        flags.append("CALENDAR_UNKNOWN")
    return StepResult(
        snapshot_id=step.snapshot_id,
        ts_utc=step.ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ts_et=_fmt_et(step.ts_utc),
        market_date=market_date.isoformat(),
        market_data_type=None,
        spot=None,
        spot_source=None,
        quote_coverage=None,
        short_dte=(date.fromisoformat(legs[0].expiry) - market_date).days,
        legs=marks,
        spread_value_per_share=None,
        position_value_dollars=None,
        pnl_dollars=None,
        pnl_pct_of_max_risk=None,
        net_greeks=None,
        flags=sorted(set(flags)),
        status="INCOMPLETE",
        kind="GAP",
        slot_utc=_iso_or_none(step.slot_utc),
        tracker_run_entries=_relevant_entries(step, relevant),
    )


def _iso_or_none(ts: datetime | None) -> str | None:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts is not None else None


def _mark_step(
    step: BacktestStep,
    legs: list[EvaluatedLeg],
    *,
    entry_cash: float,
    max_loss: float,
    units: int,
    entry_filter: tuple[float | None, float | None],
    risk_free_rate: float,
    relevant: frozenset[str] = frozenset(),
) -> StepResult:
    if step.kind == "GAP":
        return _gap_step(step, legs, relevant)
    df = _enrich(step.df, risk_free_rate)
    first = df.row(0, named=True)
    market_date = date.fromisoformat(str(first["market_date"]))
    tracked = step.tracked
    used_entries: list[str] = []
    flags: list[str] = []

    if step.kind == "TRACKED_ONLY":
        entry = tracked.best_entry(relevant) if tracked is not None else None
        md_type = (
            int(entry.market_data_type)
            if entry is not None and entry.market_data_type is not None
            else int(first["market_data_type"])
        )
        spot = (
            float(entry.spot)
            if entry is not None and entry.spot is not None
            else float(first["underlying_price"])
        )
        spot_source = entry.spot_source if entry is not None else None
        if entry is not None:
            used_entries.append(entry.ref)
        # Coverage of a tracked file: same quote definition, over the tracked legs only.
        coverage = measure_quote_quality(df).quote_coverage
        marks = [
            _mark_leg(df, leg, market_date, quote_source="TRACKED", absent_status="MISSING_NO_DATA")
            for leg in legs
        ]
        flags.append("TRACKED_ONLY_STEP")
    else:
        md_type = int(first["market_data_type"])
        spot = float(first["underlying_price"])
        spot_source = step.meta.spot_source if step.meta is not None else None
        coverage = measure_quote_quality(df).quote_coverage
        marks = [_mark_leg(df, leg, market_date) for leg in legs]
        tracked_df = (
            _enrich(tracked.df, risk_free_rate)
            if tracked is not None and tracked.df is not None and tracked.df.height
            else None
        )
        for i, (leg, mark) in enumerate(zip(legs, marks, strict=True)):
            if mark.status.startswith("MISSING") and _has_contract(tracked_df, leg):
                assert tracked_df is not None
                marks[i] = _mark_leg(tracked_df, leg, market_date, quote_source="TRACKED")

    for m in marks:
        if m.status.startswith("MISSING"):
            flags.append(f"LEG_{m.status}:{m.strike:g}{m.right}")
        elif m.status == "NO_PRICE":
            flags.append(f"LEG_NO_PRICE:{m.strike:g}{m.right}")
        elif m.price_source not in RELIABLE_PRICE_SOURCES:
            flags.append("LEG_FALLBACK_PRICE")
    sources = {m.quote_source for m in marks if m.quote_source is not None}
    if step.kind == "SNAPSHOT" and "TRACKED" in sources:
        if "SNAPSHOT" in sources:
            flags.append("MIXED_QUOTE_SOURCES")
        used_entries.extend(_relevant_entries(step, relevant))
    if coverage < MIN_QUOTE_COVERAGE:
        flags.append("LOW_QUOTE_COVERAGE")
    if spot_source == "CLOSE":
        flags.append("SPOT_SOURCE_CLOSE")
    elif spot_source is None:
        flags.append("SPOT_SOURCE_UNKNOWN")
    if md_type not in USABLE_MARKET_DATA_TYPES:
        flags.append("FROZEN_DATA")
    if not _in_session(step.ts_utc):
        flags.append("OFF_SESSION")
    if not calendar_covers(market_date):
        flags.append("CALENDAR_UNKNOWN")
    if step.kind == "SNAPSHOT" and infer_chain_step_and_range(step.df) != entry_filter:
        flags.append("FILTER_CHANGED")
    complete = all(m.status == "PRICED" for m in marks)
    if not complete:
        tracker_flags = _tracker_flags(step, relevant)
        if tracker_flags:
            flags.extend(tracker_flags)
            used_entries.extend(_relevant_entries(step, relevant))

    position_value = pnl = pnl_pct = spread_ps = None
    greeks: StepGreeks | None = None
    if complete:
        position_value = sum(_sign(m.side) * m.qty * 100.0 * float(m.price or 0.0) for m in marks)
        pnl = entry_cash + position_value
        pnl_pct = (pnl / max_loss * 100.0) if max_loss > 1e-9 else None
        spread_ps = -position_value / (100.0 * units)
        width = _spread_width(legs)
        if spread_ps < -SPREAD_BOUNDS_TOLERANCE or spread_ps > width + SPREAD_BOUNDS_TOLERANCE:
            flags.append("SPREAD_VALUE_OUT_OF_BOUNDS")
        greeks = _greeks(marks)

    flags = sorted(set(flags))
    status: StepStatus
    if not complete:
        status = "INCOMPLETE"
    elif any(f in UNRELIABLE_FLAGS for f in flags):
        status = "UNRELIABLE"
    else:
        status = "OK"

    uses_tracked = step.kind == "TRACKED_ONLY" or "TRACKED" in sources
    short_expiry = date.fromisoformat(legs[0].expiry)
    return StepResult(
        snapshot_id=step.snapshot_id,
        ts_utc=step.ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ts_et=_fmt_et(step.ts_utc),
        market_date=market_date.isoformat(),
        market_data_type=md_type,
        spot=spot,
        spot_source=spot_source,
        quote_coverage=coverage,
        short_dte=(short_expiry - market_date).days,
        legs=marks,
        spread_value_per_share=_round(spread_ps, 4),
        position_value_dollars=_round(position_value, 2),
        pnl_dollars=_round(pnl, 2),
        pnl_pct_of_max_risk=_round(pnl_pct, 2),
        net_greeks=greeks,
        flags=flags,
        status=status,
        kind=step.kind,
        slot_utc=_iso_or_none(step.slot_utc),
        tracked_snapshot_id=(
            tracked.tracked_snapshot_id if uses_tracked and tracked is not None else None
        ),
        tracker_run_entries=sorted(set(used_entries)),
    )


def _is_after_expiry_close(ts: datetime, expiry: date) -> bool:
    """At/after the session close of the expiry date (13:00 ET on early closes)."""
    return ts >= _close_utc(expiry)


def _settle(
    steps: list[BacktestStep],
    legs: list[EvaluatedLeg],
    *,
    entry_cash: float,
    max_loss: float,
    units: int,
    data_ends_before_expiry: bool,
    relevant: frozenset[str] = frozenset(),
) -> Settlement:
    expiry = date.fromisoformat(legs[0].expiry)
    settle_at = _settlement_time(expiry)
    settle_text = _et(settle_at).strftime("%H:%M")
    if data_ends_before_expiry:
        return Settlement(
            status="NOT_REACHED",
            expiry=expiry.isoformat(),
            note="The data (or the end step) ends before the short expiry closed.",
        )

    def _on_time(ts: datetime) -> bool:
        return _et(ts).date() == expiry and ts >= settle_at

    spot: float | None = None
    note = "Settled at intrinsic value using the closing-time spot of the expiry date."
    fields: dict[str, Any] = {}
    candidates = [
        s
        for s in steps
        if s.kind == "SNAPSHOT"
        and _on_time(s.ts_utc)
        and (s.meta is None or s.meta.spot_source != "CLOSE")
    ]
    if candidates:
        snap = candidates[-1]
        spot = float(snap.df["underlying_price"][0])
        fields = {
            "snapshot_id": snap.snapshot_id,
            "ts_et": _fmt_et(snap.ts_utc),
            "spot_source": snap.meta.spot_source if snap.meta is not None else None,
            "market_data_type": int(snap.df["market_data_type"][0]),
            "spot_from": "SNAPSHOT",
        }
    else:
        log_spots = []
        for s in steps:
            if s.tracked is None:
                continue
            ts = s.tracked.file_ts_utc or s.tracked.slot_utc
            entry = s.tracked.best_entry(relevant)
            if (
                entry is not None
                and entry.spot is not None
                and entry.spot_source not in (None, "CLOSE")
                and _on_time(ts)
            ):
                log_spots.append((ts, s.tracked, entry))
        if log_spots:
            ts, tracked, entry = max(log_spots, key=lambda x: x[0])
            spot = float(entry.spot or 0.0)
            note = (
                "Settled at intrinsic value using the closing-time spot of the expiry date "
                "from the tracker run log (no usable normal snapshot at that time)."
            )
            fields = {
                "snapshot_id": tracked.tracked_snapshot_id,
                "ts_et": _fmt_et(ts),
                "spot_source": entry.spot_source,
                "market_data_type": entry.market_data_type,
                "spot_from": "TRACKER_RUN_LOG",
                "tracker_run_entry": entry.ref,
            }
    if spot is None:
        extra = (
            " No tracker run-log spot either." if any(s.tracked is not None for s in steps) else ""
        )
        return Settlement(
            status="UNRESOLVED",
            expiry=expiry.isoformat(),
            note=(
                f"No snapshot on the expiry date at/after {settle_text} ET with a non-CLOSE "
                "spot; the settlement price is unknown and was not estimated." + extra
            ),
        )
    value = 0.0
    for leg in legs:
        intrinsic = max(0.0, spot - leg.strike) if leg.right == "C" else max(0.0, leg.strike - spot)
        value += _sign(leg.side) * leg.qty * 100.0 * intrinsic
    pnl = entry_cash + value
    return Settlement(
        status="SETTLED",
        expiry=expiry.isoformat(),
        spot=spot,
        intrinsic_spread_per_share=_round(-value / (100.0 * units), 4),
        pnl_dollars=_round(pnl, 2),
        pnl_pct_of_max_risk=_round(pnl / max_loss * 100.0, 2) if max_loss > 1e-9 else None,
        note=note,
        **fields,
    )


# ------------------------------------------------------------------ engine
def run_bull_put_backtest(
    steps: list[BacktestStep],
    *,
    ticker: str,
    mode: EntryMode,
    legs: list[StrategyLegInput] | None = None,
    qty: int = 1,
    target_expiry: str | None = None,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    end_snapshot_id: str | None = None,
    notes: list[str] | None = None,
) -> BacktestResult:
    """Open a bull put spread on `steps[0]`, mark it on every later step, settle after expiry."""
    symbol = TickerValidator.validate(ticker)
    if qty <= 0:
        raise ValueError(f"Quantity must be positive, got {qty}")
    if not steps:
        raise SnapshotNotFoundError(f"No snapshot history for {symbol}")
    run_notes = list(notes or [])
    entry_step = steps[0]
    entry_df = _enrich(entry_step.df, risk_free_rate)
    walls = compute_walls(entry_df, top_n=WALLS_TOP_N)

    if mode == "LEGS":
        if not legs:
            raise ValueError("Mode LEGS needs the two spread legs")
        validate_bull_put_legs(legs)
        ev = evaluate_strategy(
            entry_df,
            strategy_kind="BULL_PUT_SPREAD",
            legs=legs,
            qty_multiplier=qty,
            risk_free_rate=risk_free_rate,
            walls_report=walls,
        )
    elif mode == "AUTO_BUILD":
        if legs:
            raise ValueError("Mode AUTO_BUILD chooses the legs itself; do not pass legs")
        try:
            ev = evaluate_strategy(
                entry_df,
                strategy_kind="BULL_PUT_SPREAD",
                target_expiry=target_expiry,
                qty_multiplier=qty,
                risk_free_rate=risk_free_rate,
                walls_report=walls,
            )
        except ValueError as exc:
            raise ValueError(f"Auto-build found no valid bull put spread: {exc}") from exc
    else:  # pragma: no cover - guarded by the Literal type
        raise ValueError(f"Unknown entry mode {mode!r}")

    if any(leg.mid <= 0.0 for leg in ev.legs):
        raise ValueError("An entry leg has no price in the entry snapshot")
    entry_cash = ev.pricing.mid_cash_total
    max_loss = ev.risk.max_loss
    entry_filter = infer_chain_step_and_range(entry_step.df)
    expiry = date.fromisoformat(ev.legs[0].expiry)
    units = ev.legs[0].qty  # contracts per leg (leg qty x qty multiplier)

    relevant = _relevant_spread_ids(entry_step.tracked_spreads, ev.legs)
    steps = [s for s in steps if _keep_step(s, ev.legs, relevant)]
    open_steps = [s for s in steps if not _is_after_expiry_close(s.ts_utc, expiry)]
    after = [s for s in steps if _is_after_expiry_close(s.ts_utc, expiry)]
    step_results = [
        _mark_step(
            s,
            ev.legs,
            entry_cash=entry_cash,
            max_loss=max_loss,
            units=units,
            entry_filter=entry_filter,
            risk_free_rate=risk_free_rate,
            relevant=relevant,
        )
        for s in open_steps
    ]
    settlement = _settle(
        after,
        ev.legs,
        entry_cash=entry_cash,
        max_loss=max_loss,
        units=units,
        data_ends_before_expiry=not after,
        relevant=relevant,
    )
    if len(steps) < 2:
        run_notes.append("Only the entry snapshot is available; there are no later steps.")

    counted = [r for r in step_results if r.kind != "GAP"]  # GAP steps never enter numbers
    ok = [r for r in counted if r.status == "OK"]
    ok_pnls = [r.pnl_dollars for r in ok if r.pnl_dollars is not None]
    summary = BacktestSummary(
        steps_total=len(counted),
        steps_ok=len(ok),
        steps_unreliable=sum(1 for r in counted if r.status == "UNRELIABLE"),
        steps_incomplete=sum(1 for r in counted if r.status == "INCOMPLETE"),
        last_ok_snapshot_id=ok[-1].snapshot_id if ok else None,
        last_ok_pnl_dollars=ok[-1].pnl_dollars if ok else None,
        min_ok_pnl_dollars=min(ok_pnls) if ok_pnls else None,
        max_ok_pnl_dollars=max(ok_pnls) if ok_pnls else None,
        steps_gap=len(step_results) - len(counted),
    )
    tracked_files = sorted(
        {r.tracked_snapshot_id for r in step_results if r.tracked_snapshot_id}
        | (
            {settlement.snapshot_id}
            if settlement.spot_from == "TRACKER_RUN_LOG" and settlement.snapshot_id
            else set()
        )
    )
    run_entries = sorted(
        {ref for r in step_results for ref in r.tracker_run_entries}
        | ({settlement.tracker_run_entry} if settlement.tracker_run_entry else set())
    )
    tracked_used = bool(tracked_files or run_entries) or any(
        r.kind != "SNAPSHOT" for r in step_results
    )
    entry = EntrySummary(
        snapshot_id=entry_step.snapshot_id,
        ts_utc=entry_step.ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ts_et=_fmt_et(entry_step.ts_utc),
        spot=ev.underlying_price,
        legs=ev.legs,
        entry_cash_mid_dollars=entry_cash,
        entry_cash_natural_dollars=ev.pricing.natural_cash_total,
        natural_slippage_dollars=ev.pricing.slippage_dollars,
        net_mid_per_share=ev.pricing.net_mid_per_share,
        max_profit_dollars=ev.risk.max_profit,
        max_loss_dollars=max_loss,
        breakevens=ev.risk.breakevens,
        rationale=ev.rationale,
    )
    return BacktestResult(
        ticker=symbol,
        strategy="BULL_PUT_SPREAD",
        mode=mode,
        qty=qty,
        entry=entry,
        steps=step_results,
        settlement=settlement,
        summary=summary,
        notes=run_notes,
        reproducibility=Reproducibility(
            engine_version=ENGINE_VERSION,
            strategies_sha256=strategies_source_sha256(),
            strategy_lab_constants=dict(STRATEGY_LAB_CONSTANTS),
            quality_thresholds=dict(QUALITY_THRESHOLDS),
            risk_free_rate=risk_free_rate,
            walls_top_n=WALLS_TOP_N,
            entry_snapshot_id=entry_step.snapshot_id,
            step_snapshot_ids=[s.snapshot_id for s in steps],
            end_snapshot_id=end_snapshot_id,
            tracked_data_used=tracked_used,
            tracked_files=tracked_files,
            tracker_run_entries=run_entries,
            skipped_run_log_lines=list(entry_step.skipped_run_log_lines),
        ),
    )


def _keep_step(step: BacktestStep, legs: list[EvaluatedLeg], relevant: frozenset[str]) -> bool:
    """Tracker-only and gap steps belong to this run only if they concern its legs."""
    if step.kind == "SNAPSHOT":
        return True
    has_entry = step.tracked is not None and any(
        e.spread_id in relevant for e in step.tracked.entries
    )
    if step.kind == "GAP":
        return has_entry
    return has_entry or any(_has_contract(step.df, leg) for leg in legs)


def backtest_bull_put_from_repo(
    repo: CSVRepository,
    ticker: str,
    entry_snapshot_id: str,
    *,
    mode: EntryMode,
    legs: list[StrategyLegInput] | None = None,
    qty: int = 1,
    target_expiry: str | None = None,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    end_snapshot_id: str | None = None,
    tracked_dir: Path | None = None,
) -> BacktestResult:
    """Load the steps read-only from `repo` (plus tracker data) and run the backtest."""
    steps, notes = load_backtest_steps(
        repo, ticker, entry_snapshot_id, end_snapshot_id, tracked_dir=tracked_dir
    )
    return run_bull_put_backtest(
        steps,
        ticker=ticker,
        mode=mode,
        legs=legs,
        qty=qty,
        target_expiry=target_expiry,
        risk_free_rate=risk_free_rate,
        end_snapshot_id=end_snapshot_id,
        notes=notes,
    )


def backtest_tracked_spread(
    repo: CSVRepository,
    spread_id: str,
    *,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    end_snapshot_id: str | None = None,
    tracked_dir: Path | None = None,
) -> BacktestResult:
    """Backtest a tracked spread by registry id (read-only).

    Legs, quantity and the entry snapshot come from `tracked_spreads.json`; the run is plain
    mode `LEGS` with those legs (an AUTO pick's legs are the Auto-Builder's, and mode `LEGS`
    with them equals mode `AUTO_BUILD`), so no extra entry mode is needed.
    """
    spreads, problem = read_tracked_spreads(repo.index_path.parent)
    if problem:
        raise ValueError(problem)
    spread = next((s for s in spreads if s.id == spread_id), None)
    if spread is None:
        raise SnapshotNotFoundError(f"Unknown tracked spread {spread_id}")
    legs = [
        StrategyLegInput(
            side=leg.side, right=leg.right, expiry=leg.expiry, strike=leg.strike, qty=spread.qty
        )
        for leg in spread.legs
    ]
    return backtest_bull_put_from_repo(
        repo,
        spread.ticker,
        spread.created_from_snapshot_id,
        mode="LEGS",
        legs=legs,
        risk_free_rate=risk_free_rate,
        end_snapshot_id=end_snapshot_id,
        tracked_dir=tracked_dir,
    )
