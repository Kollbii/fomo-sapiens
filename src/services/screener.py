"""Best Option screener: Strategy Lab Autobuild over every watchlist ticker (cached CSVs only).

- Data: per watchlist ticker (list order) its newest COMPLETE snapshot of any date, from ONE
  snapshot-index read (`latest_snapshots(include_partial=False)`). Partial snapshots are skipped;
  a ticker without a complete one is a `no_data` row. The archived CSV is used as is (no Wall
  Corridor live overlay), so rows depend only on (snapshot id, inputs). Zero IBKR calls; never
  takes `refresh_lock` (pure reads of immutable archives).
- Candidates: `ScreenerCandidateSource.build_strategy_candidates` (= Strategy Lab Autobuild,
  `AnalyzerService.build_strategy_candidates`) with target expiry Auto, qty 1, days forward 0,
  IV shift 0, and the DTE window / min credit / min RoR passed into the build (only the credit
  verticals apply them while building).
- Filters: afterwards every candidate below min RoR or min POP is removed; min credit removes
  only the credit presets (`CREDIT_STRATEGY_KINDS`). Each floor is checked with
  `candidate_passes_filters` (`candidate_filters_for` is the per-preset rule, shared with the
  live re-pick).
- Modes: `selected` / `best` keep one row per ticker (`ok` | `filtered` | `not_buildable` |
  `no_data`); `all` returns only `ok` rows. `best` picks the highest RoR (ties: higher POP, then
  preset order). Rows come back RoR desc, rows without an RoR (and every status row) last.
- Blocking work (store / index / CSV reads, BSM enrichment, candidate builds) runs through the
  injected `runner` (default `asyncio.to_thread`), one call per ticker. Per-preset results are
  cached in memory (LRU) by (ticker, snapshot id, DTE window, min credit, min RoR): re-running
  with other modes / min POP or re-showing the card recomputes nothing, and a new complete
  snapshot id invalidates its ticker naturally (stale entries are pruned on every run).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter, OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final, NamedTuple, Protocol

import polars as pl
from pydantic import ValidationError

from src.analytics.strategies import (
    PRESET_STRATEGIES,
    EvaluatedLeg,
    PresetStrategyKind,
    StrategyCandidateFilters,
    StrategyEvaluationResponse,
    StrategyLegInput,
    StrategyPricing,
    StrategyRiskMetrics,
    candidate_passes_filters,
)
from src.core.calendar import et_date
from src.core.config import Settings
from src.core.exceptions import InvalidRequestError, SnapshotNotFoundError, StorageSchemaError
from src.services._concurrency import BlockingRunner
from src.services.error_messages import GENERIC_ERROR_MESSAGE, describe_error
from src.services.schemas import (
    ScreenerFilterName,
    ScreenerParams,
    ScreenerRow,
    ScreenerRowStatus,
    ScreenerRunRequest,
    ScreenerRunResponse,
    ScreenerStatusCounts,
)
from src.services.watchlist import WatchlistService, is_snapshot_today_et
from src.storage.csv_repository import CSVRepository
from src.storage.schema import SnapshotMeta

if TYPE_CHECKING:  # runtime import would be circular (`AnalyzerService` owns the screener)
    from src.services.analyzer import StrategyCandidateOutcome

logger = logging.getLogger(__name__)

# Presets a min-credit floor applies to; the debit presets (Bull Call, Bear Put, Diagonal) are
# never removed by it.
CREDIT_STRATEGY_KINDS: Final[frozenset[PresetStrategyKind]] = frozenset(
    {"BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "IRON_CONDOR"}
)
# Strategy Lab defaults the screener builds with (not exposed as screener controls).
SCREENER_QTY_MULTIPLIER: Final = 1
SCREENER_DAYS_FORWARD: Final = 0
SCREENER_IV_SHIFT: Final = 0.0
# LRU bound on cached (ticker, snapshot id, build inputs) entries (20 tickers x 12 input sets).
SCREENER_CACHE_MAX_ENTRIES: Final = 240

NO_SNAPSHOT_REASON: Final = "No complete snapshot archived yet."
PARTIAL_ONLY_REASON: Final = (
    "Only partial snapshots are archived (interrupted refresh); load the ticker again."
)
INDEX_UNAVAILABLE_REASON: Final = "Snapshot index could not be read."
INDEX_UNAVAILABLE_NOTICE: Final = (
    "Snapshot index could not be read; screener rows are unavailable until it can."
)

_FILTER_ORDER: Final[tuple[ScreenerFilterName, ...]] = (
    "min_net_credit_dollars",
    "min_ror_pct",
    "min_pop_pct",
)
# Credit-vertical builder rejections caused by the entry floors (`StrategyFilterError` codes).
_BUILDER_FLOOR_SKIPS: Final[dict[str, tuple[ScreenerFilterName, ...]]] = {
    "BELOW_MIN_CREDIT": ("min_net_credit_dollars",),
    "BELOW_MIN_ROR": ("min_ror_pct",),
    "BELOW_MIN_CREDIT_AND_ROR": ("min_net_credit_dollars", "min_ror_pct"),
}
_STATUS_RANK: Final[dict[ScreenerRowStatus, int]] = {
    "ok": 0,
    "filtered": 1,
    "not_buildable": 2,
    "no_data": 3,
}
_PRESET_INDEX: Final[dict[PresetStrategyKind, int]] = {
    kind: idx for idx, kind in enumerate(PRESET_STRATEGIES)
}
_MINUS: Final = "\u2212"  # MINUS SIGN for SELL legs in legs labels


class ScreenerCandidateSource(Protocol):
    """Strategy Lab entry points the screener reuses (implemented by `AnalyzerService`).

    Both are blocking and thread-safe: the screener calls them inside `runner`.
    """

    def load_enriched_snapshot(self, raw_ticker: str, snapshot_id: str) -> pl.DataFrame: ...

    def build_strategy_candidates(
        self,
        symbol: str,
        enriched: pl.DataFrame,
        kinds: Sequence[PresetStrategyKind],
        *,
        target_expiry: str | None = ...,
        dte_min: int | None = ...,
        dte_max: int | None = ...,
        days_forward: int = ...,
        iv_shift: float = ...,
        qty_multiplier: int = ...,
        min_net_credit_dollars: float | None = ...,
        min_ror_pct: float | None = ...,
    ) -> list[StrategyCandidateOutcome]: ...


# ------------------------------------------------------------------ filters & row formatting
def candidate_filters_for(
    kind: PresetStrategyKind, params: ScreenerParams
) -> StrategyCandidateFilters:
    """Floors one preset must meet: min RoR / min POP always, min credit only for credit presets.

    The single rule for row filtering and the live re-pick (`repick_strategy_legs(filters=...)`).
    """
    return StrategyCandidateFilters(
        min_net_credit_dollars=(
            params.min_net_credit_dollars if kind in CREDIT_STRATEGY_KINDS else None
        ),
        min_ror_pct=params.min_ror_pct,
        min_pop_pct=params.min_pop_pct,
    )


def failed_filters_for(
    kind: PresetStrategyKind,
    pricing: StrategyPricing,
    risk: StrategyRiskMetrics,
    params: ScreenerParams,
) -> list[ScreenerFilterName]:
    """Active floors an evaluated candidate fails, in request-field order (empty = it passes).

    Every floor of `candidate_filters_for` is checked on its own with `candidate_passes_filters`,
    so the result names each failing floor; it is empty exactly when the combined check passes.
    """
    floors = candidate_filters_for(kind, params)
    single: tuple[tuple[ScreenerFilterName, float | None, StrategyCandidateFilters], ...] = (
        (
            "min_net_credit_dollars",
            floors.min_net_credit_dollars,
            StrategyCandidateFilters(min_net_credit_dollars=floors.min_net_credit_dollars),
        ),
        (
            "min_ror_pct",
            floors.min_ror_pct,
            StrategyCandidateFilters(min_ror_pct=floors.min_ror_pct),
        ),
        (
            "min_pop_pct",
            floors.min_pop_pct,
            StrategyCandidateFilters(min_pop_pct=floors.min_pop_pct),
        ),
    )
    return [
        name
        for name, value, floor in single
        if value is not None
        and not candidate_passes_filters(pricing, risk, floor, qty_multiplier=params.qty_multiplier)
    ]


def _fmt_number(value: float) -> str:
    """`150`, `147.5`, `12.25`: fixed 4 decimals with trailing zeros stripped (no exponent)."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def describe_failed_filters(names: Sequence[ScreenerFilterName], params: ScreenerParams) -> str:
    """Floor labels for a `filtered` row, e.g. `min credit $1.50, min RoR 20%, min POP 60%`."""
    labels: list[str] = []
    for name in names:
        if name == "min_net_credit_dollars" and params.min_net_credit_dollars is not None:
            labels.append(f"min credit ${params.min_net_credit_dollars:,.2f}")
        elif name == "min_ror_pct" and params.min_ror_pct is not None:
            labels.append(f"min RoR {_fmt_number(params.min_ror_pct)}%")
        elif name == "min_pop_pct" and params.min_pop_pct is not None:
            labels.append(f"min POP {_fmt_number(params.min_pop_pct)}%")
    return ", ".join(labels)


def format_legs_label(legs: Sequence[EvaluatedLeg]) -> str:
    """Compact leg notation in builder leg order, joined with ` / `.

    Token = sign (`+` BUY, `−` U+2212 SELL) + quantity with `x` when above 1 + right (`C` / `P`)
    + strike without trailing zeros: `−P150 / +P145` (Bull Put), `+C145 / −C155` (Bull Call),
    `+P135 / −P140 / −C150 / +C155` (Iron Condor), `+2xC150` (qty 2). When the legs span more
    than one expiry (Diagonal) every token ends with its ISO expiry:
    `+C130 2027-01-08 / −C155 2026-12-18`.
    """
    multi_expiry = len({leg.expiry for leg in legs}) > 1
    tokens: list[str] = []
    for leg in legs:
        sign = "+" if leg.side == "BUY" else _MINUS
        qty = f"{leg.qty}x" if leg.qty > 1 else ""
        token = f"{sign}{qty}{leg.right}{_fmt_number(leg.strike)}"
        tokens.append(f"{token} {leg.expiry}" if multi_expiry else token)
    return " / ".join(tokens)


def leg_inputs(legs: Sequence[EvaluatedLeg]) -> list[StrategyLegInput]:
    """A row's legs as `StrategyLegInput`s (live re-pick input, Strategy Lab hand-off).

    Screener rows are built with `qty_multiplier=1`, so each leg's `qty` is its spec quantity.
    """
    return [
        StrategyLegInput(
            side=leg.side,
            right=leg.right,
            expiry=leg.expiry,
            strike=leg.strike,
            qty=leg.qty,
            selection_reason=leg.selection_reason,
        )
        for leg in legs
    ]


def min_leg_open_interest(legs: Sequence[EvaluatedLeg]) -> int | None:
    """Lowest leg open interest, `None` when there are no legs or ANY leg's OI is unknown.

    A missing value never counts as liquid: an unknown leg would otherwise hide a thin one.
    """
    values: list[int] = []
    for leg in legs:
        if leg.open_interest is None:
            return None
        values.append(leg.open_interest)
    return min(values) if values else None


def _safe_reason(text: str) -> str:
    """Single-line, length-capped client text (the `describe_error` sanitizer)."""
    return describe_error(InvalidRequestError(text)).message


def _normalize_floor(name: str, value: float | None, upper: float | None = None) -> float | None:
    """`None` / `0` -> `None` (off); finite `[0, upper]` otherwise, else `InvalidRequestError`."""
    if value is None:
        return None
    if not math.isfinite(value) or value < 0.0 or (upper is not None and value > upper):
        bound = f"[0, {upper:g}]" if upper is not None else ">= 0"
        raise InvalidRequestError(f"{name} must be finite and {bound} (got {value!r}).")
    return None if value == 0.0 else float(value)


def _utc_iso(now: datetime) -> str:
    aware = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ internal result types
class _BuildKey(NamedTuple):
    """Inputs that change the built candidates (mode / min POP only re-filter cached ones)."""

    dte_min: int
    dte_max: int
    min_net_credit_dollars: float | None
    min_ror_pct: float | None


_CacheKey = tuple[str, str, _BuildKey]  # (ticker, snapshot_id, build inputs)


@dataclass(frozen=True, slots=True)
class _KindResult:
    """Traceback-free result of one preset build, safe to cache.

    `evaluation` drops the payoff curves / price grid (never part of a screener row).
    """

    evaluation: StrategyEvaluationResponse | None = None
    reason: str | None = None
    skip_reason: str | None = None

    @classmethod
    def from_outcome(cls, outcome: StrategyCandidateOutcome) -> _KindResult:
        if outcome.evaluation is not None:
            slim = outcome.evaluation.model_copy(update={"price_grid": [], "curves": []})
            return cls(evaluation=slim)
        if isinstance(outcome.error, ValidationError):  # engine bug: details stay server-side
            logger.warning("screener: %s build failed validation: %s", outcome.kind, outcome.error)
        return cls(reason=outcome.reason or GENERIC_ERROR_MESSAGE, skip_reason=outcome.skip_reason)


@dataclass(frozen=True, slots=True)
class _TickerFailure:
    """Ticker-level failure (never cached): unreadable archive or an unexpected error."""

    status: ScreenerRowStatus
    reason: str


@dataclass(frozen=True, slots=True)
class _TickerResults:
    by_kind: dict[PresetStrategyKind, _KindResult] = field(default_factory=dict)
    failure: _TickerFailure | None = None


@dataclass(frozen=True, slots=True)
class _Listing:
    """Watchlist tickers plus their newest complete snapshot (one index read)."""

    tickers: tuple[str, ...]
    complete: dict[str, SnapshotMeta]
    partial_only: frozenset[str]
    notices: tuple[str, ...]
    index_ok: bool


@dataclass(frozen=True, slots=True)
class _Verdict:
    """One preset after the filters: `ok`, `filtered` or `not_buildable`."""

    kind: PresetStrategyKind
    status: ScreenerRowStatus
    evaluation: StrategyEvaluationResponse | None = None
    failed: tuple[ScreenerFilterName, ...] = ()
    reason: str | None = None
    skip_reason: str | None = None


def _judge(
    kind: PresetStrategyKind, result: _KindResult | None, params: ScreenerParams
) -> _Verdict:
    if result is None:
        return _Verdict(kind, "not_buildable", reason=GENERIC_ERROR_MESSAGE)
    evaluation = result.evaluation
    if evaluation is not None:
        failed = tuple(failed_filters_for(kind, evaluation.pricing, evaluation.risk, params))
        return _Verdict(kind, "filtered" if failed else "ok", evaluation=evaluation, failed=failed)
    floors = _BUILDER_FLOOR_SKIPS.get(result.skip_reason or "")
    if floors is not None:
        return _Verdict(kind, "filtered", failed=floors, skip_reason=result.skip_reason)
    return _Verdict(kind, "not_buildable", reason=result.reason, skip_reason=result.skip_reason)


def _best_rank(verdict: _Verdict) -> tuple[bool, float, float, int]:
    """`max()` key: highest RoR (an RoR beats none), then higher POP, then preset order."""
    risk = verdict.evaluation.risk if verdict.evaluation is not None else None
    ror = risk.return_on_risk_pct if risk is not None else None
    pop = risk.prob_of_profit_pct if risk is not None else None
    return (
        ror is not None,
        ror if ror is not None else -math.inf,
        pop if pop is not None else -math.inf,
        -_PRESET_INDEX[verdict.kind],
    )


def _row_ror(row: ScreenerRow) -> float | None:
    if row.status != "ok" or row.risk is None:
        return None
    return row.risk.return_on_risk_pct


class ScreenerService:
    """Best Option screener over the watchlist; see the module docstring for the rules.

    `run` raises `InvalidRequestError` for invalid inputs only; per-ticker problems become
    status rows. `source` is the Strategy Lab Autobuild provider (`AnalyzerService`).
    """

    def __init__(
        self,
        source: ScreenerCandidateSource,
        watchlist: WatchlistService,
        repo: CSVRepository,
        *,
        settings: Settings,
        clock: Callable[[], datetime],
        runner: BlockingRunner | None = None,
        cache_max_entries: int = SCREENER_CACHE_MAX_ENTRIES,
    ) -> None:
        if cache_max_entries < 1:
            raise ValueError("cache_max_entries must be >= 1")
        self.source = source
        self.watchlist = watchlist
        self.repo = repo
        self.settings = settings
        self.clock = clock
        self.runner: BlockingRunner = runner or asyncio.to_thread
        self._cache_max_entries = cache_max_entries
        self._cache: OrderedDict[_CacheKey, dict[PresetStrategyKind, _KindResult]] = OrderedDict()
        # One run at a time: a concurrent identical run waits and then hits the cache instead
        # of building the same candidates twice.
        self._run_lock = asyncio.Lock()

    @property
    def cache_size(self) -> int:
        """Cached (ticker, snapshot id, build inputs) entries."""
        return len(self._cache)

    def clear_cache(self) -> None:
        self._cache.clear()

    def resolve_params(self, request: ScreenerRunRequest) -> ScreenerParams:
        """Effective inputs: config DTE defaults, floors `0` -> off, `strategy_kind` only for
        `selected`. Raises `InvalidRequestError` when they are inconsistent."""
        oc = self.settings.options_chain
        dte_min = oc.dte_min if request.dte_min is None else int(request.dte_min)
        dte_max = oc.dte_max if request.dte_max is None else int(request.dte_max)
        if dte_min > dte_max:
            raise InvalidRequestError("dte_min must be <= dte_max")
        if request.mode == "selected" and request.strategy_kind is None:
            raise InvalidRequestError("strategy_kind is required when mode is 'selected'")
        return ScreenerParams(
            mode=request.mode,
            strategy_kind=request.strategy_kind if request.mode == "selected" else None,
            dte_min=dte_min,
            dte_max=dte_max,
            min_net_credit_dollars=_normalize_floor(
                "min_net_credit_dollars", request.min_net_credit_dollars
            ),
            min_ror_pct=_normalize_floor("min_ror_pct", request.min_ror_pct, upper=1000.0),
            min_pop_pct=_normalize_floor("min_pop_pct", request.min_pop_pct, upper=100.0),
            qty_multiplier=SCREENER_QTY_MULTIPLIER,
            days_forward=SCREENER_DAYS_FORWARD,
            iv_shift=SCREENER_IV_SHIFT,
        )

    async def run(self, request: ScreenerRunRequest) -> ScreenerRunResponse:
        """Screen every watchlist ticker on its newest complete snapshot (cached CSVs only)."""
        params = self.resolve_params(request)
        kinds: tuple[PresetStrategyKind, ...] = (
            (params.strategy_kind,)
            if params.mode == "selected" and params.strategy_kind is not None
            else PRESET_STRATEGIES
        )
        build = _BuildKey(
            params.dte_min, params.dte_max, params.min_net_credit_dollars, params.min_ror_pct
        )
        computed = 0
        async with self._run_lock:
            listing = await self.runner(self._read_listing)
            if listing.index_ok:
                self._prune(listing.complete)
            results: dict[str, _TickerResults] = {}
            for ticker in listing.tickers:
                meta = listing.complete.get(ticker)
                if meta is None:
                    continue
                results[ticker], did_compute = await self._ticker_results(
                    ticker, meta.snapshot_id, build, kinds
                )
                computed += int(did_compute)
        response = self._respond(params, kinds, listing, results, self.clock())
        logger.info(
            "screener run mode=%s tickers=%d computed=%d cached=%d rows=%d",
            params.mode,
            len(listing.tickers),
            computed,
            len(results) - computed,
            len(response.rows),
        )
        return response

    # ------------------------------------------------------------------ data & cache
    def _read_listing(self) -> _Listing:
        """Blocking: fresh watchlist read + ONE snapshot-index read (runs inside `runner`)."""
        state = self.watchlist.read_state()
        tickers = tuple(state.tickers)
        notices: tuple[str, ...] = (state.notice,) if state.notice else ()
        try:
            complete = self.repo.latest_snapshots(tickers, include_partial=False)
            missing = [t for t in tickers if t not in complete]
            partial_only = (
                frozenset(self.repo.latest_snapshots(missing, include_partial=True))
                if missing
                else frozenset()
            )
        except (StorageSchemaError, OSError, ValueError) as exc:
            logger.warning("screener: snapshot index unavailable (%s)", type(exc).__name__)
            return _Listing(
                tickers, {}, frozenset(), (*notices, INDEX_UNAVAILABLE_NOTICE), index_ok=False
            )
        return _Listing(tickers, complete, partial_only, notices, index_ok=True)

    def _prune(self, latest: dict[str, SnapshotMeta]) -> None:
        """Drop entries of tickers whose newest complete snapshot changed or left the list."""
        current = {(ticker, meta.snapshot_id) for ticker, meta in latest.items()}
        for key in [k for k in self._cache if (k[0], k[1]) not in current]:
            del self._cache[key]

    def _store(self, key: _CacheKey, value: dict[PresetStrategyKind, _KindResult]) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_max_entries:
            self._cache.popitem(last=False)

    async def _ticker_results(
        self,
        ticker: str,
        snapshot_id: str,
        build: _BuildKey,
        kinds: tuple[PresetStrategyKind, ...],
    ) -> tuple[_TickerResults, bool]:
        """Cached per-preset results, building only the missing presets (`True` = built)."""
        key: _CacheKey = (ticker, snapshot_id, build)
        known = self._cache.get(key)
        if known is not None:
            self._cache.move_to_end(key)
        cached = dict(known) if known is not None else {}
        missing = tuple(kind for kind in kinds if kind not in cached)
        if not missing:
            return _TickerResults(by_kind=cached), False
        try:
            fresh = await self.runner(lambda: self._compute(ticker, snapshot_id, build, missing))
        except Exception:
            logger.exception("screener: unexpected error while screening %s", ticker)
            failure = _TickerFailure("not_buildable", GENERIC_ERROR_MESSAGE)
            return _TickerResults(failure=failure), True
        if fresh.failure is not None:
            return fresh, True
        merged = {**cached, **fresh.by_kind}
        self._store(key, merged)
        return _TickerResults(by_kind=merged), True

    def _compute(
        self,
        ticker: str,
        snapshot_id: str,
        build: _BuildKey,
        kinds: tuple[PresetStrategyKind, ...],
    ) -> _TickerResults:
        """Blocking: load + enrich the archive, then Autobuild `kinds` (runs inside `runner`)."""
        try:
            enriched = self.source.load_enriched_snapshot(ticker, snapshot_id)
        except (SnapshotNotFoundError, StorageSchemaError) as exc:
            logger.warning("screener: snapshot %s unreadable (%s)", snapshot_id, type(exc).__name__)
            return _TickerResults(failure=_TickerFailure("no_data", describe_error(exc).message))
        try:
            outcomes = self.source.build_strategy_candidates(
                ticker,
                enriched,
                kinds,
                target_expiry=None,
                dte_min=build.dte_min,
                dte_max=build.dte_max,
                days_forward=SCREENER_DAYS_FORWARD,
                iv_shift=SCREENER_IV_SHIFT,
                qty_multiplier=SCREENER_QTY_MULTIPLIER,
                min_net_credit_dollars=build.min_net_credit_dollars,
                min_ror_pct=build.min_ror_pct,
            )
        except InvalidRequestError as exc:
            # E.g. no contracts in the DTE window: deterministic for this snapshot + inputs.
            reason = describe_error(exc).message
            return _TickerResults(by_kind={kind: _KindResult(reason=reason) for kind in kinds})
        return _TickerResults(by_kind={o.kind: _KindResult.from_outcome(o) for o in outcomes})

    # ------------------------------------------------------------------ rows
    def _respond(
        self,
        params: ScreenerParams,
        kinds: tuple[PresetStrategyKind, ...],
        listing: _Listing,
        results: dict[str, _TickerResults],
        now: datetime,
    ) -> ScreenerRunResponse:
        today = et_date(now)
        status_kind = params.strategy_kind if params.mode == "selected" else None
        rows: list[ScreenerRow] = []
        counts: Counter[ScreenerRowStatus] = Counter()
        for ticker in listing.tickers:
            meta = listing.complete.get(ticker)
            if meta is None:
                if not listing.index_ok:
                    reason = INDEX_UNAVAILABLE_REASON
                elif ticker in listing.partial_only:
                    reason = PARTIAL_ONLY_REASON
                else:
                    reason = NO_SNAPSHOT_REASON
                counts["no_data"] += 1
                if params.mode != "all":
                    rows.append(_status_row(ticker, "no_data", reason, status_kind, None, today))
                continue
            res = results[ticker]
            if res.failure is not None:
                failure = res.failure
                row_meta = None if failure.status == "no_data" else meta
                if params.mode == "all":
                    counts[failure.status] += 1 if failure.status == "no_data" else len(kinds)
                else:
                    counts[failure.status] += 1
                    rows.append(
                        _status_row(
                            ticker, failure.status, failure.reason, status_kind, row_meta, today
                        )
                    )
                continue
            verdicts = [_judge(kind, res.by_kind.get(kind), params) for kind in kinds]
            if params.mode == "all":
                for verdict in verdicts:
                    counts[verdict.status] += 1
                    if verdict.status == "ok":
                        rows.append(_candidate_row(ticker, verdict, meta, today))
                continue
            if params.mode == "selected":
                row = _verdict_row(ticker, verdicts[0], meta, today, params)
            else:
                row = _best_row(ticker, verdicts, meta, today, params)
            counts[row.status] += 1
            rows.append(row)

        ticker_pos = {ticker: idx for idx, ticker in enumerate(listing.tickers)}
        rows.sort(key=lambda r: _order_key(r, ticker_pos))
        return ScreenerRunResponse(
            mode=params.mode,
            params=params,
            generated_at_utc=_utc_iso(now),
            today_et=today.isoformat(),
            tickers=list(listing.tickers),
            rows=rows,
            counts=ScreenerStatusCounts(
                ok=counts["ok"],
                filtered=counts["filtered"],
                not_buildable=counts["not_buildable"],
                no_data=counts["no_data"],
            ),
            notices=list(listing.notices),
        )


def _order_key(row: ScreenerRow, ticker_pos: dict[str, int]) -> tuple[bool, float, int, int, int]:
    """Default order: RoR desc (no RoR last), then status, watchlist order, preset order."""
    ror = _row_ror(row)
    kind_pos = (
        _PRESET_INDEX[row.strategy_kind]
        if row.strategy_kind is not None
        else len(PRESET_STRATEGIES)
    )
    return (
        ror is None,
        -ror if ror is not None else 0.0,
        _STATUS_RANK[row.status],
        ticker_pos.get(row.ticker, len(ticker_pos)),
        kind_pos,
    )


def _meta_fields(meta: SnapshotMeta | None, today: date) -> dict[str, object]:
    if meta is None:
        return {}
    return {
        "snapshot_id": meta.snapshot_id,
        "snapshot_ts_utc": meta.snapshot_ts_utc,
        "is_today_et": is_snapshot_today_et(meta.snapshot_ts_utc, today),
        "market_data_type": meta.market_data_type,
    }


def _status_row(
    ticker: str,
    status: ScreenerRowStatus,
    reason: str | None,
    kind: PresetStrategyKind | None,
    meta: SnapshotMeta | None,
    today: date,
    *,
    failed: Sequence[ScreenerFilterName] = (),
    skip_reason: str | None = None,
) -> ScreenerRow:
    return ScreenerRow.model_validate(
        {
            "row_id": f"{ticker}:{kind}" if kind is not None else ticker,
            "ticker": ticker,
            "status": status,
            "status_reason": reason,
            "failed_filters": list(failed),
            "skip_reason": skip_reason,
            "strategy_kind": kind,
            "underlying_price": meta.underlying_price if meta is not None else None,
            **_meta_fields(meta, today),
        }
    )


def _candidate_row(ticker: str, verdict: _Verdict, meta: SnapshotMeta, today: date) -> ScreenerRow:
    evaluation = verdict.evaluation
    if evaluation is None:  # pragma: no cover - `ok` verdicts always carry an evaluation
        raise RuntimeError("screener candidate row needs an evaluation")
    return ScreenerRow.model_validate(
        {
            "row_id": f"{ticker}:{verdict.kind}",
            "ticker": ticker,
            "status": "ok",
            "strategy_kind": verdict.kind,
            **_meta_fields(meta, today),
            "underlying_price": evaluation.underlying_price,
            "short_expiry": evaluation.short_expiry,
            "short_dte": evaluation.short_dte,
            "long_expiry": evaluation.long_expiry,
            "long_dte": evaluation.long_dte,
            "legs": list(evaluation.legs),
            "legs_label": format_legs_label(evaluation.legs),
            "pricing": evaluation.pricing,
            "risk": evaluation.risk,
            "net_greeks": evaluation.net_greeks,
            "min_leg_open_interest": min_leg_open_interest(evaluation.legs),
            "rationale": list(evaluation.rationale),
            "primary_call_wall": evaluation.primary_call_wall,
            "primary_put_wall": evaluation.primary_put_wall,
        }
    )


def _verdict_row(
    ticker: str, verdict: _Verdict, meta: SnapshotMeta, today: date, params: ScreenerParams
) -> ScreenerRow:
    """`selected` mode: the preset's candidate row, or its status row."""
    if verdict.status == "ok":
        return _candidate_row(ticker, verdict, meta, today)
    reason = (
        describe_failed_filters(verdict.failed, params)
        if verdict.status == "filtered"
        else verdict.reason
    )
    return _status_row(
        ticker,
        verdict.status,
        reason,
        verdict.kind,
        meta,
        today,
        failed=verdict.failed,
        skip_reason=verdict.skip_reason,
    )


def _best_row(
    ticker: str,
    verdicts: Sequence[_Verdict],
    meta: SnapshotMeta,
    today: date,
    params: ScreenerParams,
) -> ScreenerRow:
    """`best` mode: the top passing preset, else one `filtered` / `not_buildable` status row.

    `filtered` (some preset was built or rejected by a floor) names the union of failing floors;
    otherwise the not-buildable reasons are merged (one reason, or `KIND: reason; ...`).
    """
    passing = [v for v in verdicts if v.status == "ok"]
    if passing:
        return _candidate_row(ticker, max(passing, key=_best_rank), meta, today)
    filtered = [v for v in verdicts if v.status == "filtered"]
    if filtered:
        names = tuple(name for name in _FILTER_ORDER if any(name in v.failed for v in filtered))
        reason = describe_failed_filters(names, params)
        return _status_row(ticker, "filtered", reason, None, meta, today, failed=names)
    reasons = [(v.kind, v.reason or GENERIC_ERROR_MESSAGE) for v in verdicts]
    distinct = list(dict.fromkeys(reason for _, reason in reasons))
    merged = (
        distinct[0]
        if len(distinct) == 1
        else "; ".join(f"{kind}: {reason}" for kind, reason in reasons)
    )
    return _status_row(ticker, "not_buildable", _safe_reason(merged), None, meta, today)
