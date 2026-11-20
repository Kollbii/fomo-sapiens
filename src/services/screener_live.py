"""Live Best Option screener: screener rows re-priced about every second from IB Gateway quotes.

Display-only: live quotes are never written to snapshots and nothing under `data/` is touched.

- Rows: the `ok` rows of `ScreenerService.run` (cached CSVs, a cache hit) for the stream's run
  inputs, in the client's `row_ids` order (current sort / filter, <= 120).
- Allocation (`_allocate`, blocking -> `runner`): walk the rows; a row's window is every leg plus
  its nearest lower / upper strike of the same expiry and right (`repick_window_strikes` on the
  BSM-enriched snapshot; Diagonal legs keep their own expiries), centred on the row's current
  legs. Window contracts are deduplicated across rows (a shared strike is ONE line) plus one spot
  line per ticker; a row is added only while BOTH budgets still hold - option lines <=
  `LiveLegQuoteService.option_lines_budget` (the Corridor option cap, 88) and options + spots
  <= `LiveLegQuoteService.lines_budget` (90) - otherwise it is `not_live_budget` (first fit:
  a later row needing fewer new lines may still go live).
- Each frame: per live row, its ticker's snapshot rows (the expiries of its live rows) overlaid
  with the live quotes (`overlay_live_quotes`) are re-picked with `repick_strategy_legs` inside
  the SUBSCRIBED window - restricted to lines with a live quote, so a stale snapshot neighbour
  never wins - with `candidate_filters_for(kind, params)`; only a strictly higher RoR switches
  the legs. A row whose current legs lack a live quote is `no_quotes` (last values kept).
  Unchanged inputs skip the re-pick; re-picks run through the injected `runner`
  (default `asyncio.to_thread`).
- Re-allocation (`reallocate`): the allocation is recomputed for the new row order (windows
  re-centred on the current legs) and handed to the leg stream, which applies the line diff on
  its next tick. Leg switches computed meanwhile are discarded, so the current legs always lie
  inside the windows that get subscribed.
- One live feed at a time: when the stream subscribes it stops the Wall Corridor stream and
  every older screener stream (`superseded`); a Corridor stream stops it (`stop_now(
  "corridor_stream")`); every `refresh_lock` holder stops it (`AnalyzerService._stop_live_feeds`).
  A stream stopped server-side ends with one `stopped` frame naming the reason. A stopped or
  closed stream (including one closed before its first read) is forgotten at once, so
  `reallocate` on it raises `LiveStreamNotFoundError`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import polars as pl

from src.analytics.strategies import (
    EvaluatedLeg,
    PositionGreeks,
    PresetStrategyKind,
    RepickKeepReason,
    StrategyCandidateFilters,
    StrategyLegInput,
    StrategyPricing,
    StrategyRepickResult,
    StrategyRiskMetrics,
    repick_strategy_legs,
    repick_window_strikes,
)
from src.broker.client import market_data_type_label
from src.broker.corridor import CorridorContractSpec, CorridorQuoteRow, strategy_leg_spec_from_row
from src.broker.live_legs import (
    LIVE_LEGS_INTERVAL_SEC,
    SCREENER_LIVE_BUSY_MESSAGE,
    LegLineKey,
    LiveLegQuoteService,
    LiveLegsFrame,
    LiveLegsSpec,
    LiveSpotLine,
    leg_line_key,
)
from src.core.config import Settings
from src.core.exceptions import (
    InvalidRequestError,
    InvalidTickerError,
    LiveStreamNotFoundError,
    RefreshBusyError,
    ScreenerLiveBusyError,
    SnapshotNotFoundError,
    StorageSchemaError,
)
from src.services._concurrency import BlockingRunner
from src.services.corridor import QuoteKey, chain_quote_key, overlay_live_quotes
from src.services.schemas import (
    ScreenerLiveFrame,
    ScreenerLiveReallocateRequest,
    ScreenerLiveReallocateResponse,
    ScreenerLiveRequest,
    ScreenerLiveRow,
    ScreenerLiveRowAllocation,
    ScreenerLiveStatus,
    ScreenerLiveStopReason,
    ScreenerLiveStopResponse,
    ScreenerParams,
    ScreenerRow,
    ScreenerRunRequest,
)
from src.services.screener import (
    ScreenerService,
    candidate_filters_for,
    format_legs_label,
    leg_inputs,
    min_leg_open_interest,
)

logger = logging.getLogger(__name__)

STREAM_NOT_FOUND_MESSAGE: Final = (
    "Live screener stream not found or already stopped; turn Live off and on to re-open it."
)
SCREENER_LIVE_STOP_MESSAGES: Final[dict[ScreenerLiveStopReason, str]] = {
    "manual_stop": "Live screener stopped.",
    "superseded": "Live screener stopped: a newer live screener stream started.",
    "corridor_stream": (
        "Live screener stopped: the live Wall Corridor stream started (one live feed at a time)."
    ),
    "corridor_snapshot": (
        "Live screener stopped: a Wall Corridor snapshot needs the IBKR market-data lines."
    ),
    "refresh_chain": "Live screener stopped: a chain refresh needs the IBKR market-data lines.",
    "batch_refresh": "Live screener stopped: a batch load needs the IBKR market-data lines.",
    "ledger": "Live screener stopped: a Gateway ledger request needs the IBKR market-data lines.",
    "ledger_sync": "Live screener stopped: a Gateway ledger sync needs the IBKR market-data lines.",
    "ledger_mtm": "Live screener stopped: a live MTM refresh needs the IBKR market-data lines.",
    "shutdown": "Live screener stopped: the server is shutting down.",
}

_SPOT_UNKNOWN: Final = 0.0


def screener_stop_notice(lines: int, context: str) -> str:
    """Notice of a `refresh_lock` holder (or the Corridor) that stopped the live screener."""
    return (
        f"Stopped the live Best Option screener stream ({lines} line(s)) {context} to stay "
        "within IBKR's 100-line ceiling."
    )


def _utc_iso(now: datetime) -> str:
    aware = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ internal state
@dataclass(frozen=True, slots=True)
class _LiveValues:
    """Candidate values shown for a row: the screener row's until a live re-pick ran."""

    legs: tuple[EvaluatedLeg, ...]
    pricing: StrategyPricing | None
    risk: StrategyRiskMetrics | None
    net_greeks: PositionGreeks | None
    underlying_price: float | None

    @classmethod
    def from_row(cls, row: ScreenerRow) -> _LiveValues:
        return cls(tuple(row.legs), row.pricing, row.risk, row.net_greeks, row.underlying_price)

    @classmethod
    def from_repick(cls, result: StrategyRepickResult) -> _LiveValues:
        return cls(
            tuple(result.evaluated_legs),
            result.pricing,
            result.risk,
            result.net_greeks,
            result.underlying_price,
        )


@dataclass(frozen=True, slots=True)
class _TickerData:
    """One ticker's BSM-enriched snapshot (loaded once per stream through `runner`)."""

    snapshot_id: str
    frame: pl.DataFrame
    rows_by_key: Mapping[QuoteKey, Mapping[str, Any]]
    underlying_conid: int
    reference_price: float


@dataclass(frozen=True, slots=True)
class _Carry:
    """A live row's state captured when a re-allocation starts (it keeps its legs)."""

    base_legs: tuple[StrategyLegInput, ...]
    legs: tuple[StrategyLegInput, ...]
    values: _LiveValues
    passes_filters: bool | None


@dataclass(frozen=True, slots=True)
class _Slot:
    """One requested `ok` row inside an allocation (request order)."""

    row: ScreenerRow
    kind: PresetStrategyKind
    status: ScreenerLiveStatus
    legs: tuple[StrategyLegInput, ...] = ()
    windows: tuple[tuple[float, ...], ...] = ()
    values: _LiveValues | None = None
    passes_filters: bool | None = True


@dataclass(frozen=True)
class _Allocation:
    version: int
    spec: LiveLegsSpec
    slots: tuple[_Slot, ...]
    scoped: Mapping[str, pl.DataFrame]
    notices: tuple[str, ...] = ()


@dataclass
class _RowState:
    slot: _Slot
    base_legs: tuple[StrategyLegInput, ...]
    legs: tuple[StrategyLegInput, ...]
    values: _LiveValues
    passes_filters: bool | None = True
    status: ScreenerLiveStatus = "no_quotes"
    keep_reason: RepickKeepReason | None = None
    signature: tuple[Any, ...] | None = None


@dataclass
class _LiveStream:
    stream_id: str
    params: ScreenerParams
    run_request: ScreenerRunRequest
    rows_by_id: dict[str, ScreenerRow]
    tickers: dict[str, _TickerData]
    desired: _Allocation
    market_data_type: int
    allocations: dict[int, _Allocation] = field(default_factory=dict)
    applied: _Allocation | None = None
    states: dict[str, _RowState] = field(default_factory=dict)
    stop_reason: ScreenerLiveStopReason | None = None
    started: bool = False
    closed: bool = False
    next_version: int = 1
    handed_version: int = 0
    reallocating: int = 0
    alloc_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def active(self) -> bool:
        return not self.closed and self.stop_reason is None

    def current_spec(self) -> LiveLegsSpec:
        """The leg stream's `spec_source`: the latest allocation (remembered as handed out)."""
        self.handed_version = self.desired.version
        return self.desired.spec

    @property
    def allocation_pending(self) -> bool:
        """A re-allocation is being computed or not applied by the leg stream yet."""
        applied = self.applied.version if self.applied is not None else -1
        return self.reallocating > 0 or self.desired.version != applied


@dataclass(frozen=True, slots=True)
class _RepickJob:
    row_id: str
    ticker: str
    kind: PresetStrategyKind
    legs: tuple[StrategyLegInput, ...]
    windows: tuple[tuple[float, ...], ...]
    filters: StrategyCandidateFilters
    signature: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _TickerTick:
    scoped: pl.DataFrame
    live_spot: float
    quote_map: Mapping[QuoteKey, CorridorQuoteRow]


def _quote_signature(q: CorridorQuoteRow) -> tuple[Any, ...]:
    return (
        q.bid,
        q.ask,
        q.mid,
        q.last,
        q.close,
        q.volume,
        q.open_interest,
        q.iv,
        q.delta,
        q.gamma,
        q.theta,
        q.vega,
        q.greeks_source,
    )


def _run_repicks(
    jobs: Sequence[_RepickJob],
    ticks: Mapping[str, _TickerTick],
    *,
    risk_free_rate: float,
) -> dict[str, StrategyRepickResult | None]:
    """Blocking (runs in `runner`): one overlay per ticker, one re-pick per row. Pure CPU."""
    overlaid: dict[str, pl.DataFrame] = {}
    results: dict[str, StrategyRepickResult | None] = {}
    for job in jobs:
        try:
            frame = overlaid.get(job.ticker)
            if frame is None:
                tick = ticks[job.ticker]
                frame = overlay_live_quotes(tick.scoped, tick.live_spot, tick.quote_map)
                overlaid[job.ticker] = frame
            results[job.row_id] = repick_strategy_legs(
                job.kind,
                job.legs,
                frame,
                window_strikes=job.windows,
                filters=job.filters,
                qty_multiplier=1,
                risk_free_rate=risk_free_rate,
            )
        except Exception:
            logger.exception("live screener: re-pick failed for %s", job.row_id)
            results[job.row_id] = None
    return results


class ScreenerLiveStream:
    """Un-started live screener stream: an async iterator of `ScreenerLiveFrame`s plus its id.

    Subscribes lazily on the first `anext`; `aclose()` (SSE teardown) cancels every line and
    forgets the stream even when it was never read (`reallocate` then raises
    `LiveStreamNotFoundError`).
    """

    def __init__(
        self,
        stream_id: str,
        frames: AsyncGenerator[ScreenerLiveFrame, None],
        *,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.stream_id = stream_id
        self._frames = frames
        self._on_close = on_close

    def __aiter__(self) -> ScreenerLiveStream:
        return self

    async def __anext__(self) -> ScreenerLiveFrame:
        return await self._frames.__anext__()

    async def aclose(self) -> None:
        try:
            await self._frames.aclose()
        finally:
            # A generator closed before its first `anext` never runs its `finally`.
            if self._on_close is not None:
                self._on_close()


class ScreenerLiveService:
    """Live Best Option screener streams (one at a time); see the module docstring for the rules.

    `is_busy` / `busy_error` / `stop_corridor` are injected by `AnalyzerService`
    (`refresh_lock.locked`, its `RefreshBusyError` factory and its Corridor stop).
    """

    def __init__(
        self,
        screener: ScreenerService,
        quotes: LiveLegQuoteService,
        *,
        settings: Settings,
        clock: Callable[[], datetime],
        is_busy: Callable[[], bool],
        busy_error: Callable[[], RefreshBusyError],
        stop_corridor: Callable[[], Awaitable[int]],
        runner: BlockingRunner | None = None,
        interval_sec: float = LIVE_LEGS_INTERVAL_SEC,
    ) -> None:
        self.screener = screener
        self.quotes = quotes
        self.settings = settings
        self.clock = clock
        self.runner: BlockingRunner = runner or asyncio.to_thread
        self.interval_sec = interval_sec
        self._is_busy = is_busy
        self._busy_error = busy_error
        self._stop_corridor = stop_corridor
        self._streams: dict[str, _LiveStream] = {}
        self.last_stop: dict[str, Any] | None = None

    @property
    def active_stream_count(self) -> int:
        """Leg streams currently holding (or subscribing) IBKR lines."""
        return self.quotes.active_stream_count

    @property
    def lines_budget(self) -> int:
        """Options + spots cap (`LiveLegQuoteService.lines_budget`, 90)."""
        return self.quotes.lines_budget

    @property
    def option_lines_budget(self) -> int:
        """Option-line cap (`LiveLegQuoteService.option_lines_budget`, 88)."""
        return self.quotes.option_lines_budget

    # ------------------------------------------------------------------ public API
    async def open_live_stream(
        self,
        request: ScreenerLiveRequest,
        *,
        is_disconnected: Callable[[], bool | Awaitable[bool]] | None = None,
        max_frames: int | None = None,
    ) -> ScreenerLiveStream:
        """Validate, resolve the rows, allocate the line budget and connect; return the stream.

        Raises before any frame (so the HTTP layer can answer with a status code):
        `RefreshBusyError` while `refresh_lock` is held, `InvalidRequestError` for inputs the
        screener rejects, `GatewayConnectionError` / `GatewayTimeoutError` when the Gateway is
        unreachable. The returned stream subscribes lazily (see `ScreenerLiveStream`).
        """
        if max_frames is not None and max_frames < 1:
            raise InvalidRequestError("max_frames must be >= 1")
        if self._is_busy():
            raise self._busy_error()
        run_request = request.run_request()
        params = self.screener.resolve_params(run_request)
        row_ids = list(dict.fromkeys(request.row_ids))
        response = await self.screener.run(run_request)
        rows_by_id = {row.row_id: row for row in response.rows}
        tickers: dict[str, _TickerData] = {}
        budget, option_budget = self.quotes.lines_budget, self.quotes.option_lines_budget
        allocation = await self.runner(
            lambda: self._allocate(
                rows_by_id,
                tickers,
                row_ids,
                {},
                version=0,
                budget=budget,
                option_budget=option_budget,
            )
        )
        await self.quotes.client.ensure_connected()
        if self._is_busy():
            raise self._busy_error()
        stream = _LiveStream(
            stream_id=f"screener-{uuid.uuid4().hex[:12]}",
            params=params,
            run_request=run_request,
            rows_by_id=rows_by_id,
            tickers=tickers,
            desired=allocation,
            market_data_type=self.quotes.client.get_active_market_data_type(),
            allocations={0: allocation},
        )
        # Streams that never subscribed are superseded now; subscribed ones when this one does.
        for other in list(self._streams.values()):
            if not other.started:
                other.stop_reason = other.stop_reason or "superseded"
                del self._streams[other.stream_id]
        self._streams[stream.stream_id] = stream
        logger.info(
            "live screener %s opened: %d row(s), %d planned line(s)",
            stream.stream_id,
            len(allocation.slots),
            allocation.spec.line_count,
        )
        return ScreenerLiveStream(
            stream.stream_id,
            self._frames(stream, is_disconnected=is_disconnected, max_frames=max_frames),
            on_close=lambda: self._forget(stream),
        )

    async def reallocate(
        self, request: ScreenerLiveReallocateRequest
    ) -> ScreenerLiveReallocateResponse:
        """Re-allocate a running stream's lines to a new row order (applied on its next tick).

        Raises `LiveStreamNotFoundError` for an unknown or stopped stream and `RefreshBusyError`
        while `refresh_lock` is held (the holder stops the stream anyway).
        """
        stream = self._streams.get(request.stream_id)
        if stream is None or not stream.active:
            raise LiveStreamNotFoundError(STREAM_NOT_FOUND_MESSAGE)
        if self._is_busy():
            raise self._busy_error()
        row_ids = list(dict.fromkeys(request.row_ids))
        stream.reallocating += 1
        try:
            async with stream.alloc_lock:
                response = await self.screener.run(stream.run_request)
                rows_by_id = {row.row_id: row for row in response.rows}
                carry = {
                    row_id: _Carry(st.base_legs, st.legs, st.values, st.passes_filters)
                    for row_id, st in stream.states.items()
                }
                version = stream.next_version
                stream.next_version += 1
                budget, option_budget = self.quotes.lines_budget, self.quotes.option_lines_budget
                allocation = await self.runner(
                    lambda: self._allocate(
                        rows_by_id,
                        stream.tickers,
                        row_ids,
                        carry,
                        version=version,
                        budget=budget,
                        option_budget=option_budget,
                    )
                )
                if not stream.active:
                    raise LiveStreamNotFoundError(STREAM_NOT_FOUND_MESSAGE)
                stream.rows_by_id = rows_by_id
                stream.allocations[version] = allocation
                if version > stream.desired.version:
                    stream.desired = allocation
                _prune_allocations(stream)
        finally:
            stream.reallocating -= 1
        return ScreenerLiveReallocateResponse(
            stream_id=stream.stream_id,
            version=allocation.version,
            lines_planned=allocation.spec.line_count,
            lines_budget=budget,
            option_lines_budget=option_budget,
            rows=[
                ScreenerLiveRowAllocation(
                    row_id=slot.row.row_id, ticker=slot.row.ticker, live_status=slot.status
                )
                for slot in allocation.slots
            ],
            notices=list(allocation.notices),
        )

    async def stop_live_streams(
        self, *, reason: ScreenerLiveStopReason = "manual_stop"
    ) -> ScreenerLiveStopResponse:
        """UI teardown (hidden card, `pagehide`, Live off) / shutdown: free every line now."""
        stopped = self.stop_now(reason=reason)
        return ScreenerLiveStopResponse(
            stopped_subscriptions=stopped, active_streams=self.active_stream_count
        )

    def stop_now(self, *, reason: ScreenerLiveStopReason) -> int:
        """Synchronously stop every live screener stream (including ones not subscribed yet).

        Cancels 100% of their lines and forgets every stream (`reallocate` on any of them then
        raises `LiveStreamNotFoundError`); returns how many lines were held. Used by every
        `refresh_lock` holder and by the Corridor stream's registration hook.
        """
        marked = 0
        for stream in self._streams.values():
            if stream.stop_reason is None:
                stream.stop_reason = reason
                marked += 1
        self._streams.clear()
        lines = self.quotes.stop_all_now()
        if lines > 0 or marked > 0:
            self.last_stop = {
                "reason": reason,
                "stopped_subscriptions": lines,
                "stopped_at_utc": _utc_iso(self.clock()),
            }
        return lines

    # ------------------------------------------------------------------ streaming
    async def _frames(
        self,
        stream: _LiveStream,
        *,
        is_disconnected: Callable[[], bool | Awaitable[bool]] | None,
        max_frames: int | None,
    ) -> AsyncGenerator[ScreenerLiveFrame, None]:
        legs_stream = self.quotes.stream_live_legs(
            stream.current_spec,
            session_id=stream.stream_id,
            interval_sec=self.interval_sec,
            is_disconnected=is_disconnected,
            is_busy=self._is_busy,
            on_register=lambda: self._on_register(stream),
        )
        sequence = 0
        try:
            if self._is_busy():
                raise ScreenerLiveBusyError(SCREENER_LIVE_BUSY_MESSAGE)
            if stream.stop_reason is None:
                async for legs_frame in legs_stream:
                    frame = await self._live_frame(stream, legs_frame, sequence + 1)
                    if stream.stop_reason is not None:
                        break  # stopped while re-picking: no live frame after the stop
                    sequence += 1
                    yield frame
                    if max_frames is not None and sequence >= max_frames:
                        return
            if stream.stop_reason is not None:
                yield self._stopped_frame(stream, sequence + 1)
        finally:
            stream.closed = True
            try:
                await legs_stream.aclose()
            finally:
                self._forget(stream)

    def _forget(self, stream: _LiveStream) -> None:
        """Mark `stream` closed and drop it from `_streams` (idempotent)."""
        stream.closed = True
        if self._streams.get(stream.stream_id) is stream:
            del self._streams[stream.stream_id]

    async def _on_register(self, stream: _LiveStream) -> list[str]:
        """Leg session registered: claim the single live feed (or give up when superseded)."""
        if stream.stop_reason is not None:
            self.quotes.stop_session(stream.stream_id)
            return []
        if self._is_busy():
            return []  # the leg stream's busy re-check raises next; leave the other feeds alone
        stream.started = True
        for other in self._streams.values():
            if other is not stream and other.stop_reason is None:
                other.stop_reason = "superseded"  # its lines are released before ours open
        stopped = await self._stop_corridor()
        if stopped <= 0:
            return []
        return [
            f"Stopped the live Wall Corridor stream ({stopped} line(s)): one live feed at a time."
        ]

    async def _live_frame(
        self, stream: _LiveStream, legs_frame: LiveLegsFrame, sequence: int
    ) -> ScreenerLiveFrame:
        stream.market_data_type = legs_frame.market_data_type
        notices = list(legs_frame.notices)
        if stream.applied is None or stream.applied.version != legs_frame.version:
            notices.extend(self._apply_allocation(stream, legs_frame.version))
        jobs, ticks = self._repick_jobs(stream, legs_frame)
        if jobs:
            rfr = self.settings.analytics.risk_free_rate
            results = await self.runner(lambda: _run_repicks(jobs, ticks, risk_free_rate=rfr))
            self._apply_repicks(stream, jobs, results)
        return ScreenerLiveFrame(
            stream_id=stream.stream_id,
            sequence=sequence,
            version=legs_frame.version,
            updated_ts_utc=legs_frame.updated_ts_utc,
            market_data_type=legs_frame.market_data_type,
            market_data_type_label=market_data_type_label(legs_frame.market_data_type),
            lines_used=legs_frame.lines_used,
            lines_budget=legs_frame.lines_budget,
            option_lines_budget=legs_frame.option_lines_budget,
            rows=self._frame_rows(stream),
            notices=notices,
        )

    def _stopped_frame(self, stream: _LiveStream, sequence: int) -> ScreenerLiveFrame:
        reason: ScreenerLiveStopReason = stream.stop_reason or "manual_stop"
        version = stream.applied.version if stream.applied is not None else stream.desired.version
        return ScreenerLiveFrame(
            stream_id=stream.stream_id,
            sequence=sequence,
            version=version,
            updated_ts_utc=_utc_iso(self.clock()),
            market_data_type=stream.market_data_type,
            market_data_type_label=market_data_type_label(stream.market_data_type),
            lines_used=0,
            lines_budget=self.quotes.lines_budget,
            option_lines_budget=self.quotes.option_lines_budget,
            rows=[],
            notices=[SCREENER_LIVE_STOP_MESSAGES[reason]],
            stopped=True,
            stop_reason=reason,
        )

    def _apply_allocation(self, stream: _LiveStream, version: int) -> list[str]:
        """Switch the row states to the allocation the leg stream just applied."""
        allocation = stream.allocations.get(version)
        if allocation is None:  # pragma: no cover - the leg stream only applies our specs
            logger.warning("live screener %s: unknown allocation v%d", stream.stream_id, version)
            return []
        states: dict[str, _RowState] = {}
        for slot in allocation.slots:
            if slot.status != "live":
                continue
            row_id = slot.row.row_id
            base_legs = tuple(leg_inputs(slot.row.legs))
            prev = stream.states.get(row_id)
            if prev is not None and prev.base_legs == base_legs and prev.legs == slot.legs:
                prev.slot = slot
                prev.signature = None  # the window changed: re-pick on this tick
                states[row_id] = prev
            else:
                states[row_id] = _RowState(
                    slot=slot,
                    base_legs=base_legs,
                    legs=slot.legs,
                    values=slot.values
                    if slot.values is not None
                    else _LiveValues.from_row(slot.row),
                    passes_filters=slot.passes_filters,
                )
        stream.states = states
        stream.applied = allocation
        _prune_allocations(stream)
        return list(allocation.notices)

    def _repick_jobs(
        self, stream: _LiveStream, legs_frame: LiveLegsFrame
    ) -> tuple[list[_RepickJob], dict[str, _TickerTick]]:
        """Rows whose live inputs changed (others keep their last result) + per-ticker inputs."""
        applied = stream.applied
        if applied is None:  # pragma: no cover - set by `_apply_allocation` before
            return [], {}
        live_keys = legs_frame.live_keys
        quote_maps: dict[str, dict[QuoteKey, CorridorQuoteRow]] = {}
        for key in live_keys:
            symbol, expiry, strike, right = key
            quote_maps.setdefault(symbol, {})[chain_quote_key(expiry, strike, right)] = (
                legs_frame.quotes[key]
            )
        jobs: list[_RepickJob] = []
        ticks: dict[str, _TickerTick] = {}
        for row_id, state in stream.states.items():
            ticker = state.slot.row.ticker
            leg_keys = [leg_line_key(ticker, lg.expiry, lg.strike, lg.right) for lg in state.legs]
            scoped = applied.scoped.get(ticker)
            if scoped is None or not all(key in live_keys for key in leg_keys):
                state.status = "no_quotes"
                state.signature = None
                continue
            windows = _live_windows(ticker, state.legs, state.slot.windows, live_keys)
            spot = float(legs_frame.spots.get(ticker, _SPOT_UNKNOWN))
            signature = (
                spot,
                tuple(lg.strike for lg in state.legs),
                windows,
                tuple(
                    _quote_signature(
                        legs_frame.quotes[leg_line_key(ticker, lg.expiry, k, lg.right)]
                    )
                    for lg, strikes in zip(state.legs, windows, strict=True)
                    for k in strikes
                ),
            )
            if signature == state.signature:
                continue  # same live inputs and legs: the last re-pick result still holds
            if ticker not in ticks:
                ticks[ticker] = _TickerTick(scoped, spot, quote_maps.get(ticker, {}))
            jobs.append(
                _RepickJob(
                    row_id=row_id,
                    ticker=ticker,
                    kind=state.slot.kind,
                    legs=state.legs,
                    windows=windows,
                    filters=candidate_filters_for(state.slot.kind, stream.params),
                    signature=signature,
                )
            )
        return jobs, ticks

    def _apply_repicks(
        self,
        stream: _LiveStream,
        jobs: Sequence[_RepickJob],
        results: Mapping[str, StrategyRepickResult | None],
    ) -> None:
        pending = stream.allocation_pending
        for job in jobs:
            state = stream.states.get(job.row_id)
            if state is None or state.legs != job.legs:  # pragma: no cover - defensive
                continue
            result = results.get(job.row_id)
            if result is None:
                state.status = "no_quotes"
                state.signature = None
                continue
            if result.adjusted and pending:
                # A re-allocation centred its windows on the current legs: switch after it.
                state.signature = None
                continue
            if result.adjusted:
                state.legs = tuple(result.legs)
            state.values = _LiveValues.from_repick(result)
            state.keep_reason = result.keep_reason
            state.passes_filters = True if result.adjusted else result.current_passes_filters
            state.status = "live"
            state.signature = job.signature

    def _frame_rows(self, stream: _LiveStream) -> list[ScreenerLiveRow]:
        applied = stream.applied
        if applied is None:  # pragma: no cover - set by `_apply_allocation` before
            return []
        rows: list[ScreenerLiveRow] = []
        for slot in applied.slots:
            row = slot.row
            if slot.status == "not_live_budget":
                rows.append(
                    ScreenerLiveRow(
                        row_id=row.row_id,
                        ticker=row.ticker,
                        strategy_kind=slot.kind,
                        live_status="not_live_budget",
                    )
                )
                continue
            state = stream.states.get(row.row_id)
            if state is None:  # allocated nothing (snapshot unavailable): snapshot values
                values = slot.values if slot.values is not None else _LiveValues.from_row(row)
                rows.append(_live_row(slot, "no_quotes", values, adjusted=False))
                continue
            adjusted = tuple(lg.strike for lg in state.legs) != tuple(
                lg.strike for lg in state.base_legs
            )
            rows.append(
                _live_row(
                    slot,
                    state.status,
                    state.values,
                    adjusted=adjusted,
                    keep_reason=state.keep_reason,
                    passes_filters=state.passes_filters,
                )
            )
        return rows

    # ------------------------------------------------------------------ allocation (blocking)
    def _allocate(
        self,
        rows_by_id: Mapping[str, ScreenerRow],
        tickers: dict[str, _TickerData],
        row_ids: Sequence[str],
        carry: Mapping[str, _Carry],
        *,
        version: int,
        budget: int,
        option_budget: int,
    ) -> _Allocation:
        """Blocking (runs in `runner`): windows, deduplicated lines and the budget walk.

        A row fits only while its new option lines keep the options <= `option_budget` AND the
        options + spots (its ticker's spot counted once) <= `budget`.
        """
        options: dict[LegLineKey, CorridorContractSpec] = {}
        spots: dict[str, LiveSpotLine] = {}
        expiries: dict[str, set[str]] = {}
        slots: list[_Slot] = []
        unavailable: list[str] = []
        for row_id in row_ids:
            row = rows_by_id.get(row_id)
            if (
                row is None
                or row.status != "ok"
                or row.strategy_kind is None
                or not row.legs
                or not row.snapshot_id
            ):
                continue
            kind = row.strategy_kind
            base_legs = tuple(leg_inputs(row.legs))
            kept = carry.get(row_id)
            if kept is not None and kept.base_legs == base_legs:
                legs, values, passes = kept.legs, kept.values, kept.passes_filters
            else:
                legs, values, passes = base_legs, _LiveValues.from_row(row), True
            data = self._ticker_data(tickers, row.ticker, row.snapshot_id)
            if data is None:
                unavailable.append(row.ticker)
                slots.append(_Slot(row, kind, "no_quotes", legs, (), values, passes))
                continue
            lines = _window_lines(row.ticker, legs, data)
            if lines is None:  # pragma: no cover - legs always come from this snapshot
                logger.warning("live screener: no +-1 window for %s", row_id)
                slots.append(_Slot(row, kind, "no_quotes", legs, (), values, passes))
                continue
            windows, row_lines = lines
            new_lines = {key: spec for key, spec in row_lines.items() if key not in options}
            option_lines = len(options) + len(new_lines)
            spot_lines = len(spots) + (0 if row.ticker in spots else 1)
            if option_lines > option_budget or option_lines + spot_lines > budget:
                slots.append(_Slot(row, kind, "not_live_budget", legs, windows, values, passes))
                continue
            options.update(new_lines)
            if row.ticker not in spots:
                spots[row.ticker] = LiveSpotLine(
                    row.ticker, data.underlying_conid, data.reference_price
                )
            expiries.setdefault(row.ticker, set()).update(lg.expiry for lg in legs)
            slots.append(_Slot(row, kind, "live", legs, windows, values, passes))
        scoped = {
            ticker: tickers[ticker].frame.filter(pl.col("expiry").is_in(sorted(exps)))
            for ticker, exps in expiries.items()
        }
        notices = tuple(
            f"{ticker}: the archived snapshot is unavailable for live quotes."
            for ticker in dict.fromkeys(unavailable)
        )
        spec = LiveLegsSpec(version, tuple(options.values()), tuple(spots.values()))
        return _Allocation(version, spec, tuple(slots), scoped, notices)

    def _ticker_data(
        self, tickers: dict[str, _TickerData], ticker: str, snapshot_id: str
    ) -> _TickerData | None:
        cached = tickers.get(ticker)
        if cached is not None and cached.snapshot_id == snapshot_id:
            return cached
        try:
            frame = self.screener.source.load_enriched_snapshot(ticker, snapshot_id)
        except (
            SnapshotNotFoundError,
            StorageSchemaError,
            InvalidRequestError,
            InvalidTickerError,
        ) as exc:
            logger.warning(
                "live screener: snapshot %s unreadable (%s)", snapshot_id, type(exc).__name__
            )
            return None
        if frame.height == 0:
            return None
        rows_by_key: dict[QuoteKey, Mapping[str, Any]] = {}
        for raw in frame.iter_rows(named=True):
            right = str(raw["right"]).upper()
            if right in ("C", "P"):
                rows_by_key[chain_quote_key(str(raw["expiry"]), float(raw["strike"]), right)] = raw
        first = frame.row(0, named=True)
        data = _TickerData(
            snapshot_id=snapshot_id,
            frame=frame,
            rows_by_key=rows_by_key,
            underlying_conid=int(first["underlying_conid"]),
            reference_price=float(first["underlying_price"]),
        )
        tickers[ticker] = data
        return data


def _prune_allocations(stream: _LiveStream) -> None:
    """Keep only the allocations that can still be applied or are applied (bounded memory)."""
    keep = {stream.desired.version, stream.handed_version}
    if stream.applied is not None:
        keep.add(stream.applied.version)
    for version in [v for v in stream.allocations if v not in keep]:
        del stream.allocations[version]


def _window_lines(
    ticker: str, legs: Sequence[StrategyLegInput], data: _TickerData
) -> tuple[tuple[tuple[float, ...], ...], dict[LegLineKey, CorridorContractSpec]] | None:
    """A row's +-1 window per leg and its exact line specs (`None` when it cannot be built)."""
    try:
        windows = tuple(tuple(w) for w in repick_window_strikes(legs, data.frame))
    except ValueError:
        return None
    lines: dict[LegLineKey, CorridorContractSpec] = {}
    for leg, strikes in zip(legs, windows, strict=True):
        for strike in strikes:
            key = leg_line_key(ticker, leg.expiry, strike, leg.right)
            if key in lines:
                continue
            raw = data.rows_by_key.get(chain_quote_key(leg.expiry, strike, leg.right))
            if raw is None:
                return None
            lines[key] = strategy_leg_spec_from_row(raw, symbol=ticker)
    return windows, lines


def _live_windows(
    ticker: str,
    legs: Sequence[StrategyLegInput],
    windows: Sequence[Sequence[float]],
    live_keys: frozenset[LegLineKey],
) -> tuple[tuple[float, ...], ...]:
    """The subscribed window per leg, restricted to strikes with a live quote."""
    return tuple(
        tuple(k for k in strikes if leg_line_key(ticker, leg.expiry, k, leg.right) in live_keys)
        for leg, strikes in zip(legs, windows, strict=True)
    )


def _live_row(
    slot: _Slot,
    status: ScreenerLiveStatus,
    values: _LiveValues,
    *,
    adjusted: bool,
    keep_reason: RepickKeepReason | None = None,
    passes_filters: bool | None = None,
) -> ScreenerLiveRow:
    return ScreenerLiveRow(
        row_id=slot.row.row_id,
        ticker=slot.row.ticker,
        strategy_kind=slot.kind,
        live_status=status,
        adjusted=adjusted,
        keep_reason=keep_reason,
        passes_filters=passes_filters,
        underlying_price=values.underlying_price,
        legs=list(values.legs),
        legs_label=format_legs_label(values.legs) if values.legs else None,
        pricing=values.pricing,
        risk=values.risk,
        net_greeks=values.net_greeks,
        min_leg_open_interest=min_leg_open_interest(values.legs),
    )
