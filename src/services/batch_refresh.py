"""Sequential multi-ticker batch refresh (one background task, one batch at a time).

Invariants:
- `refresh_lock` is acquired in `start()` BEFORE it returns (owner `batch:<i>/<n>:<SYM>`, relabelled
  per ticker) and released exactly once when the batch ends (`_finish` guard), so a single
  refresh, Corridor snapshot/stream, Gateway ledger sync / MTM poll, or a second batch get
  `RefreshBusyError` for the whole batch and no other IBKR operation can interleave.
- Tickers run strictly one after another, each as a CHILD task running
  `AnalyzerService.refresh_ticker_locked` (same fetch -> enrich -> save -> paper-trade quote path
  as `refresh_chain`) with a fresh `ChainFetchProgress`; `strike_step` is forced to Auto (`None`).
  Every live feed (Wall Corridor stream, live screener stream) is stopped once at batch start.
- `cancel()` stops at once: it cancels the running child task (no waiting for the ticker). The
  fetcher's `finally` cancels every market-data line; the orchestrator waits for that cleanup at
  most `cancel_cleanup_sec` (`BATCH_CANCEL_CLEANUP_SEC`), archives the FINISHED pages as a
  partial snapshot (reason `cancelled`, `data/latest` untouched), marks the ticker and every
  remaining ticker `cancelled`, then releases the lock. Between tickers it stops before the next.
- Per-ticker cap (`watchlist.batch_ticker_timeout_sec`) and `GatewayConnectionError` (incl.
  `GatewayTimeoutError`) from the child: partial save (reason `timeout` / `connection_lost`),
  ticker `failed`, remaining tickers `not_started`, batch `aborted` with a notice. Any other
  error marks the ticker `failed` (sanitized via `describe_error`, nothing saved) and the batch
  continues. Details are logged server-side only.
- `aclose()` (application shutdown) uses the same immediate-cancel path and waits at most
  `shutdown_timeout_sec` (`BATCH_SHUTDOWN_TIMEOUT_SEC`) for the orchestrator; past that bound
  the orchestrator itself is cancelled, a warning is logged and the lock is released anyway.
- The orchestrator's OWN cancellation (task cancel) is never swallowed: it cancels the child,
  waits (bounded) for its cleanup and re-raises. A child whose cleanup outlives the bound is
  kept referenced (`_orphans`) until it ends.
- State lives in memory and only the latest batch is kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from src.broker.fetcher import ChainFetchProgress
from src.core.exceptions import (
    AnalyzerError,
    BatchNotFoundError,
    GatewayConnectionError,
    GatewayTimeoutError,
    InvalidRequestError,
)
from src.services.analyzer import AnalyzerService, TickerRefreshOutcome
from src.services.error_messages import GATEWAY_TIMEOUT_CODE, describe_error
from src.services.schemas import (
    BatchRefreshRequest,
    BatchRefreshStatus,
    BatchState,
    BatchTickerResult,
    BatchTickerState,
)
from src.services.screener_live import screener_stop_notice
from src.storage.schema import PartialReason

logger = logging.getLogger(__name__)

BATCH_ID_PATTERN: Final = r"^[0-9a-f]{32}$"
_ACTIVE_ITEM_STATES: Final[frozenset[BatchTickerState]] = frozenset({"queued", "running"})
BATCH_SHUTDOWN_TIMEOUT_SEC: Final = 15.0
"""Upper bound `aclose()` waits for the batch orchestrator before releasing the lock anyway."""
BATCH_CANCEL_CLEANUP_SEC: Final = 10.0
"""Upper bound the orchestrator waits for a cancelled ticker's fetcher cleanup (`finally`)."""


def batch_owner(position: int, total: int, symbol: str) -> str:
    """`refresh_lock` owner label shown in `RefreshBusyError` (e.g. `batch:3/16:MRVL`)."""
    return f"batch:{position}/{total}:{symbol}"


def _cancel_once(task: asyncio.Task[Any]) -> None:
    """Cancel `task` unless it is done or already being cancelled.

    A second `cancel()` would interrupt the awaits of its in-progress `finally` cleanup
    (`cancelMktData` pacing), so a child is cancelled exactly once.
    """
    if not task.done() and task.cancelling() == 0:
        task.cancel()


def ticker_timeout_message(limit_sec: float) -> str:
    """Sanitized per-ticker cap message (`Timed out after 240 s`)."""
    return f"Timed out after {limit_sec:g} s"


@dataclass(frozen=True, slots=True)
class _BatchParams:
    range_pct: float
    dte_min: int | None
    dte_max: int | None
    include_short_dte: bool


@dataclass(slots=True)
class _TickerRun:
    """Per-ticker child state: finished pages and the complete snapshot once it was saved."""

    progress: ChainFetchProgress = field(default_factory=ChainFetchProgress)
    saved: list[TickerRefreshOutcome] = field(default_factory=list)


@dataclass(slots=True)
class _BatchItem:
    ticker: str
    state: BatchTickerState = "queued"
    partial: bool = False
    snapshot_id: str | None = None
    contract_count: int | None = None
    error_code: str | None = None
    error: str | None = None
    duration_sec: float | None = None
    notices: list[str] = field(default_factory=list)

    def to_result(self) -> BatchTickerResult:
        return BatchTickerResult(
            ticker=self.ticker,
            state=self.state,
            partial=self.partial,
            snapshot_id=self.snapshot_id,
            contract_count=self.contract_count,
            error_code=self.error_code,
            error=self.error,
            duration_sec=self.duration_sec,
            notices=list(self.notices),
        )


@dataclass(slots=True)
class _Batch:
    batch_id: str
    params: _BatchParams
    items: list[_BatchItem]
    started_at_utc: str
    state: BatchState = "running"
    current_ticker: str | None = None
    finished_at_utc: str | None = None
    cancel_requested: bool = False
    #: Stopped by IBKR Gateway trouble (connection lost / time-out) -> final state `aborted`.
    aborted: bool = False
    lock_released: bool = False
    notices: list[str] = field(default_factory=list)
    #: Resolved by `cancel()` / `aclose()` so the orchestrator stops waiting on the child at once.
    cancel_signal: asyncio.Future[None] | None = None
    #: Child task of the ticker currently running (None between tickers).
    child: asyncio.Task[TickerRefreshOutcome] | None = None

    def count(self, state: BatchTickerState) -> int:
        return sum(1 for item in self.items if item.state == state)

    def to_status(self) -> BatchRefreshStatus:
        return BatchRefreshStatus(
            batch_id=self.batch_id,
            state=self.state,
            total=len(self.items),
            done_count=self.count("done"),
            failed_count=self.count("failed"),
            cancelled_count=self.count("cancelled"),
            not_started_count=self.count("not_started"),
            current_ticker=self.current_ticker,
            started_at_utc=self.started_at_utc,
            finished_at_utc=self.finished_at_utc,
            notices=list(self.notices),
            items=[item.to_result() for item in self.items],
        )


class BatchRefreshService:
    """Runs one sequential batch refresh at a time on top of an `AnalyzerService`."""

    def __init__(
        self,
        analyzer: AnalyzerService,
        *,
        timer: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] | None = None,
        shutdown_timeout_sec: float = BATCH_SHUTDOWN_TIMEOUT_SEC,
        cancel_cleanup_sec: float = BATCH_CANCEL_CLEANUP_SEC,
        ticker_timeout_sec: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not shutdown_timeout_sec > 0.0:
            raise ValueError("shutdown_timeout_sec must be > 0")
        if not cancel_cleanup_sec > 0.0:
            raise ValueError("cancel_cleanup_sec must be > 0")
        if ticker_timeout_sec is not None and not ticker_timeout_sec > 0.0:
            raise ValueError("ticker_timeout_sec must be > 0")
        self.analyzer = analyzer
        self._timer = timer
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._shutdown_timeout_sec = float(shutdown_timeout_sec)
        self._cancel_cleanup_sec = float(cancel_cleanup_sec)
        self._ticker_timeout_override = (
            float(ticker_timeout_sec) if ticker_timeout_sec is not None else None
        )
        self._sleep = sleep
        self._batch: _Batch | None = None
        self._task: asyncio.Task[None] | None = None
        # Cancelled children whose cleanup outlived `cancel_cleanup_sec` (strong references).
        self._orphans: set[asyncio.Task[Any]] = set()

    @property
    def max_tickers(self) -> int:
        return self.analyzer.settings.watchlist.max_size

    @property
    def ticker_timeout_sec(self) -> float:
        """Per-ticker cap (`watchlist.batch_ticker_timeout_sec` unless overridden)."""
        if self._ticker_timeout_override is not None:
            return self._ticker_timeout_override
        return float(self.analyzer.settings.watchlist.batch_ticker_timeout_sec)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ public API
    async def start(self, req: BatchRefreshRequest) -> BatchRefreshStatus:
        """Validate, take `refresh_lock`, spawn the background task, return the initial status.

        Raises `InvalidTickerError` / `InvalidRequestError` (before the lock is touched) or
        `RefreshBusyError` when any IBKR operation, including another batch, holds the lock.
        """
        symbols = self._validate_tickers(req.tickers)
        _, _, include_short = self.analyzer.resolve_fetch_window(
            req.dte_min, req.dte_max, req.include_short_dte
        )
        if self.is_running:
            raise self.analyzer._busy_error()
        total = len(symbols)
        if not await self.analyzer.acquire_refresh(batch_owner(1, total, symbols[0])):
            raise self.analyzer._busy_error()
        try:
            loop = asyncio.get_running_loop()
            batch = _Batch(
                batch_id=self._id_factory(),
                params=_BatchParams(
                    range_pct=req.range_pct,
                    dte_min=req.dte_min,
                    dte_max=req.dte_max,
                    include_short_dte=include_short,
                ),
                items=[_BatchItem(ticker=s) for s in symbols],
                started_at_utc=self.analyzer._utc_now_iso(),
                cancel_signal=loop.create_future(),
            )
            task = loop.create_task(self._run(batch), name=f"batch-refresh-{batch.batch_id}")
        except BaseException:
            self.analyzer.release_refresh()
            raise
        # Done-callback covers a task cancelled before its first step (its `finally` never runs).
        task.add_done_callback(lambda t: self._on_task_done(batch, t))
        self._batch = batch
        self._task = task  # strong reference: the event loop only keeps weak ones
        logger.info("batch %s started: %d ticker(s)", batch.batch_id, total)
        return batch.to_status()

    def get(self, batch_id: str) -> BatchRefreshStatus:
        return self._require(batch_id).to_status()

    def current(self) -> BatchRefreshStatus | None:
        """Running or most recent batch, or `None` when no batch ran since startup."""
        return self._batch.to_status() if self._batch is not None else None

    def cancel(self, batch_id: str) -> BatchRefreshStatus:
        """Stop the batch at once (running ticker's child task cancelled); finished -> no-op.

        Returns immediately with state `cancelling`; the orchestrator then waits (bounded) for
        the fetcher cleanup, archives finished pages as a partial snapshot and releases the lock.
        """
        batch = self._require(batch_id)
        if batch.state == "running" and not batch.lock_released:
            self._request_stop(batch)
            logger.info("batch %s cancel requested", batch.batch_id)
        return batch.to_status()

    async def aclose(self) -> None:
        """Shutdown: same immediate-cancel path as `cancel()`, bounded wait, release the lock.

        Waits at most `shutdown_timeout_sec` for the orchestrator; past that bound a warning is
        logged, the orchestrator task itself is cancelled and the batch is finalized anyway.
        `_finish` is idempotent, so the lock is released exactly once even if the stuck task
        completes later.
        """
        task = self._task
        batch = self._batch
        if task is None or task.done():
            return
        if batch is not None and not batch.lock_released:
            self._request_stop(batch)
        _, pending = await asyncio.wait({task}, timeout=self._shutdown_timeout_sec)
        if pending:
            logger.warning(
                "batch %s: task did not finish within %.1fs of shutdown cancel; "
                "releasing refresh lock anyway",
                batch.batch_id if batch is not None else "?",
                self._shutdown_timeout_sec,
            )
            task.cancel()
        if batch is not None:
            self._finish(batch)  # idempotent; normally already done by the task itself

    # ------------------------------------------------------------------ internals
    def _require(self, batch_id: str) -> _Batch:
        batch = self._batch
        if batch is None or batch.batch_id != batch_id:
            raise BatchNotFoundError("Batch refresh not found (only the latest batch is kept).")
        return batch

    def _validate_tickers(self, raw: Sequence[str]) -> list[str]:
        seen: dict[str, None] = {}
        for item in raw:
            seen.setdefault(self.analyzer.validate_ticker(item), None)
        symbols = list(seen)
        if not symbols:
            raise InvalidRequestError("Select at least one ticker to load.")
        if len(symbols) > self.max_tickers:
            raise InvalidRequestError(
                f"Batch load accepts at most {self.max_tickers} tickers (got {len(symbols)})."
            )
        return symbols

    @staticmethod
    def _request_stop(batch: _Batch) -> None:
        """Flag the batch, wake the orchestrator and cancel the running child task (no wait)."""
        batch.cancel_requested = True
        if batch.state == "running":
            batch.state = "cancelling"
        signal = batch.cancel_signal
        if signal is not None and not signal.done():
            signal.set_result(None)
        child = batch.child
        if child is not None:
            _cancel_once(child)

    async def _run(self, batch: _Batch) -> None:
        try:
            if batch.cancel_requested:  # stopped before the first step
                return
            try:
                stopped = await self.analyzer._stop_live_feeds(reason="batch_refresh")
            except Exception as exc:
                logger.exception("batch %s: stopping live feeds failed", batch.batch_id)
                self._fail_remaining(batch, exc)
                return
            if stopped.corridor_lines > 0:
                batch.notices.append(
                    f"Stopped {stopped.corridor_lines} active Wall Corridor quote stream(s) "
                    "before batch load to stay within IBKR's 100-line ceiling."
                )
            if stopped.screener_lines > 0:
                batch.notices.append(
                    screener_stop_notice(stopped.screener_lines, "before batch load")
                )
            total = len(batch.items)
            for position, item in enumerate(batch.items, start=1):
                if batch.cancel_requested or batch.aborted:
                    break
                if position > 1:
                    await self._wait_inter_ticker_cooldown(batch)
                    if batch.cancel_requested or batch.aborted:
                        break
                await self._run_item(batch, item, position, total)
        finally:
            self._finish(batch)

    async def _wait_inter_ticker_cooldown(self, batch: _Batch) -> None:
        """Pause briefly between consecutive tickers unless cancelled."""
        cooldown = float(self.analyzer.settings.watchlist.batch_inter_ticker_cooldown_sec)
        if cooldown <= 0.0:
            return
        if batch.cancel_signal is None:
            await self._sleep(cooldown)
            return
        sleep_task = asyncio.ensure_future(self._sleep(cooldown))
        try:
            await asyncio.wait(
                {sleep_task, batch.cancel_signal},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if sleep_task.done() and not sleep_task.cancelled():
                sleep_task.result()
        finally:
            if not sleep_task.done():
                sleep_task.cancel()
                with contextlib.suppress(BaseException):
                    await sleep_task

    async def _run_item(self, batch: _Batch, item: _BatchItem, position: int, total: int) -> None:
        self.analyzer.set_refresh_owner(batch_owner(position, total, item.ticker))
        batch.current_ticker = item.ticker
        item.state = "running"
        params = batch.params
        run = _TickerRun()
        started = self._timer()
        child = asyncio.get_running_loop().create_task(
            self.analyzer.refresh_ticker_locked(
                item.ticker,
                range_pct=params.range_pct,
                strike_step=None,  # batches always use Auto strike step
                dte_min=params.dte_min,
                dte_max=params.dte_max,
                include_short_dte=params.include_short_dte,
                progress=run.progress,
                on_saved=run.saved.append,
            ),
            name=f"batch-ticker-{batch.batch_id}-{item.ticker}",
        )
        batch.child = child
        try:
            await self._supervise(batch, item, child, run)
        finally:
            batch.child = None
            item.duration_sec = round(max(0.0, self._timer() - started), 3)

    async def _supervise(
        self,
        batch: _Batch,
        item: _BatchItem,
        child: asyncio.Task[TickerRefreshOutcome],
        run: _TickerRun,
    ) -> None:
        """Wait for the child (cancel signal / per-ticker cap), then record its outcome."""
        limit = self.ticker_timeout_sec
        waiters: set[asyncio.Future[Any]] = {child}
        if batch.cancel_signal is not None:
            waiters.add(batch.cancel_signal)
        timed_out = False
        try:
            await asyncio.wait(waiters, timeout=limit, return_when=asyncio.FIRST_COMPLETED)
            if not child.done():
                # Woken by cancel()/aclose() (child already cancelled) or the per-ticker cap.
                timed_out = not batch.cancel_requested
                _cancel_once(child)
                await self._await_cleanup(batch, item, child)
        except asyncio.CancelledError:
            # The orchestrator ITSELF was cancelled (shutdown past its bound / loop teardown):
            # stop the child, give its cleanup a bounded chance, never swallow the cancel.
            _cancel_once(child)
            item.state = "cancelled"
            await self._await_cleanup(batch, item, child)
            raise

        if child.done() and not child.cancelled():
            exc = child.exception()
            if exc is None:
                self._mark_done(item, child.result())
                return
        else:
            exc = None
        if timed_out:
            self._handle_ticker_timeout(batch, item, run, limit)
            return
        if exc is None:  # child cancelled (or cleanup still running)
            if batch.cancel_requested:
                if not self._archive_interrupted(batch, item, run, "cancelled"):
                    item.state = "cancelled"
                return
            exc = GatewayConnectionError(
                f"IBKR request for {item.ticker} was cancelled unexpectedly"
            )
        if isinstance(exc, GatewayConnectionError):
            self._handle_gateway_error(batch, item, run, exc)
            return
        if not isinstance(exc, Exception):  # pragma: no cover - KeyboardInterrupt / SystemExit
            item.state = "cancelled"
            raise exc
        if run.saved:
            logger.warning(
                "batch %s: %s post-save step failed after snapshot %s (%s): %s",
                batch.batch_id,
                item.ticker,
                run.saved[0].meta.snapshot_id,
                type(exc).__name__,
                exc,
            )
            self._mark_done(item, run.saved[0])
            safe = describe_error(exc, ibkr=self.analyzer.settings.ibkr)
            item.notices.append(
                f"Paper-trade quoting for {item.ticker} failed ({safe.code}); "
                "the complete chain snapshot was saved."
            )
            return
        if isinstance(exc, AnalyzerError):
            logger.warning(
                "batch %s: %s failed (%s): %s",
                batch.batch_id,
                item.ticker,
                type(exc).__name__,
                exc,
            )
        else:
            logger.error(
                "batch %s: %s failed unexpectedly",
                batch.batch_id,
                item.ticker,
                exc_info=exc,
            )
        self._mark_failed(item, exc)

    async def _await_cleanup(
        self, batch: _Batch, item: _BatchItem, child: asyncio.Task[TickerRefreshOutcome]
    ) -> None:
        """Bounded wait for a cancelled child's `finally` cleanup (`cancel_cleanup_sec`)."""
        if child.done():
            return
        _, pending = await asyncio.wait({child}, timeout=self._cancel_cleanup_sec)
        if pending:
            logger.warning(
                "batch %s: %s cleanup did not finish within %.1fs of cancel; continuing",
                batch.batch_id,
                item.ticker,
                self._cancel_cleanup_sec,
            )
            self._orphans.add(child)
            child.add_done_callback(self._discard_orphan)

    def _discard_orphan(self, task: asyncio.Task[Any]) -> None:
        self._orphans.discard(task)
        if not task.cancelled():
            with contextlib.suppress(asyncio.CancelledError):
                if task.exception() is not None:
                    logger.warning("batch ticker task %s ended with an error", task.get_name())

    @staticmethod
    def _mark_done(item: _BatchItem, outcome: TickerRefreshOutcome) -> None:
        item.state = "done"
        item.snapshot_id = outcome.meta.snapshot_id
        item.contract_count = outcome.meta.contract_count
        item.notices = list(outcome.notices)

    def _mark_failed(self, item: _BatchItem, exc: BaseException) -> None:
        safe = describe_error(exc, ibkr=self.analyzer.settings.ibkr)
        item.state = "failed"
        item.error_code = safe.code
        item.error = safe.message

    def _recycle_broker_after_timeout(self, ticker: str) -> None:
        client = getattr(self.analyzer, "broker_client", None)
        if client is None:
            return
        stage = f"per-ticker limit ({ticker})"
        reset_fn = getattr(client, "reset_after_timeout", None)
        if callable(reset_fn):
            reset_fn(stage=stage)
            return
        disconnect_fn = getattr(client, "disconnect", None)
        if callable(disconnect_fn):
            disconnect_fn()

    def _handle_ticker_timeout(
        self, batch: _Batch, item: _BatchItem, run: _TickerRun, limit: float
    ) -> None:
        logger.warning(
            "batch %s: %s exceeded the %.1fs per-ticker limit; stopping the batch",
            batch.batch_id,
            item.ticker,
            limit,
        )
        self._recycle_broker_after_timeout(item.ticker)
        if not self._archive_interrupted(batch, item, run, "timeout"):
            item.state = "failed"
            item.error_code = GATEWAY_TIMEOUT_CODE
            item.error = ticker_timeout_message(limit)
        self._abort(
            batch,
            f"Stopped: {item.ticker} timed out after {limit:g} s (IBKR Gateway not answering). "
            "Retry when the Gateway is back.",
        )

    def _handle_gateway_error(
        self,
        batch: _Batch,
        item: _BatchItem,
        run: _TickerRun,
        exc: GatewayConnectionError,
    ) -> None:
        timeout = isinstance(exc, GatewayTimeoutError)
        logger.warning(
            "batch %s: %s Gateway %s (%s): %s; stopping the batch",
            batch.batch_id,
            item.ticker,
            "time-out" if timeout else "connection loss",
            type(exc).__name__,
            exc,
        )
        if not self._archive_interrupted(
            batch, item, run, "timeout" if timeout else "connection_lost"
        ):
            self._mark_failed(item, exc)
        what = "stopped answering" if timeout else "connection lost"
        self._abort(
            batch,
            f"Stopped: IBKR Gateway {what} during {item.ticker}. Retry when the Gateway is back.",
        )

    @staticmethod
    def _abort(batch: _Batch, notice: str) -> None:
        """Stop after the current ticker; a user cancel already in progress takes precedence."""
        if batch.cancel_requested:
            return
        batch.aborted = True
        batch.notices.append(notice)

    def _archive_interrupted(
        self,
        batch: _Batch,
        item: _BatchItem,
        run: _TickerRun,
        reason: PartialReason,
    ) -> bool:
        """Record what an interrupted ticker archived; True when its complete snapshot exists.

        Interrupted after the complete snapshot was saved (only paper-trade quoting was left):
        the ticker is `done` with a notice. Otherwise its finished pages are archived as a
        partial snapshot (never raises; a save error becomes a notice) and False is returned.
        """
        if run.saved:
            self._mark_done(item, run.saved[0])
            item.notices.append(
                f"Paper-trade quoting for {item.ticker} was interrupted; "
                "the complete chain snapshot was saved."
            )
            return True
        if batch.lock_released:  # finalized by `aclose()` past its bound: lock no longer ours
            return False
        try:
            outcome = self.analyzer.save_partial_ticker_locked(item.ticker, run.progress, reason)
        except Exception as exc:
            logger.warning(
                "batch %s: partial snapshot for %s not saved (%s): %s",
                batch.batch_id,
                item.ticker,
                type(exc).__name__,
                exc,
            )
            safe = describe_error(exc, ibkr=self.analyzer.settings.ibkr)
            item.notices.append(f"Finished pages could not be saved ({safe.code}).")
            return False
        if outcome is not None:
            item.partial = True
            item.snapshot_id = outcome.meta.snapshot_id
            item.contract_count = outcome.meta.contract_count
            item.notices = list(outcome.notices)
        return False

    def _fail_remaining(self, batch: _Batch, exc: BaseException) -> None:
        safe = describe_error(exc, ibkr=self.analyzer.settings.ibkr)
        for item in batch.items:
            if item.state in _ACTIVE_ITEM_STATES:
                item.state = "failed"
                item.error_code = safe.code
                item.error = safe.message

    def _finish(self, batch: _Batch) -> None:
        """Finalize states and release `refresh_lock` exactly once (idempotent)."""
        if batch.lock_released:
            return
        try:
            for item in batch.items:
                if item.state in _ACTIVE_ITEM_STATES:
                    leftover = batch.aborted and item.state == "queued"
                    item.state = "not_started" if leftover else "cancelled"
            batch.current_ticker = None
            if batch.aborted:
                batch.state = "aborted"
            else:
                batch.state = "cancelled" if batch.count("cancelled") > 0 else "completed"
            try:
                batch.finished_at_utc = self.analyzer._utc_now_iso()
            except Exception:  # pragma: no cover - defensive (broken injected clock)
                logger.exception("batch %s: clock failed while finishing", batch.batch_id)
        finally:
            batch.lock_released = True
            self.analyzer.release_refresh()
            logger.info(
                "batch %s %s: %d done, %d failed, %d cancelled, %d not started",
                batch.batch_id,
                batch.state,
                batch.count("done"),
                batch.count("failed"),
                batch.count("cancelled"),
                batch.count("not_started"),
            )

    def _on_task_done(self, batch: _Batch, task: asyncio.Task[None]) -> None:
        self._finish(batch)
        if not task.cancelled():
            with contextlib.suppress(asyncio.CancelledError):
                exc = task.exception()
                if exc is not None:  # pragma: no cover - `_run` catches every Exception
                    logger.error("batch %s task crashed: %s", batch.batch_id, type(exc).__name__)
