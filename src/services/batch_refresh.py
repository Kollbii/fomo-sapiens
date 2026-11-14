"""Sequential multi-ticker batch refresh (one background task, one batch at a time).

Invariants:
- `refresh_lock` is acquired in `start()` BEFORE it returns (owner `batch:<i>/<n>:<SYM>`, relabelled
  per ticker) and released exactly once when the task ends, so a single refresh, Corridor
  snapshot/stream, Gateway ledger sync / MTM poll, or a second batch get `RefreshBusyError`
  for the whole batch and no other IBKR operation can interleave between tickers.
- Tickers run strictly one after another through `AnalyzerService.refresh_ticker_locked`
  (same fetch -> enrich -> save -> paper-trade quote path as `refresh_chain`); `strike_step`
  is forced to Auto (`None`). Active Wall Corridor streams are stopped once at batch start.
- A failing ticker is marked `failed` with a sanitized message (`describe_error`) and the batch
  continues. Details are logged server-side only.
- `cancel()` is cooperative: it takes effect after the current ticker finished (its
  `cancelMktData` cleanup always completes); remaining tickers become `cancelled`.
- `aclose()` (application shutdown) is the ONE case where the task is cancelled mid-ticker;
  the fetcher's own `finally` still cancels every market-data line, and the lock is released.
  The wait is bounded (`BATCH_SHUTDOWN_TIMEOUT_SEC`): a task stuck in cleanup is logged and the
  lock is released anyway, exactly once (`_finish` guard), so shutdown can never hang.
- State lives in memory and only the latest batch is kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from src.core.exceptions import AnalyzerError, BatchNotFoundError, InvalidRequestError
from src.services.analyzer import AnalyzerService
from src.services.error_messages import describe_error
from src.services.schemas import (
    BatchRefreshRequest,
    BatchRefreshStatus,
    BatchState,
    BatchTickerResult,
    BatchTickerState,
)

logger = logging.getLogger(__name__)

BATCH_ID_PATTERN: Final = r"^[0-9a-f]{32}$"
_ACTIVE_ITEM_STATES: Final[frozenset[BatchTickerState]] = frozenset({"queued", "running"})
BATCH_SHUTDOWN_TIMEOUT_SEC: Final = 15.0
"""Upper bound `aclose()` waits for the cancelled batch task before releasing the lock."""


def batch_owner(position: int, total: int, symbol: str) -> str:
    """`refresh_lock` owner label shown in `RefreshBusyError` (e.g. `batch:3/16:MRVL`)."""
    return f"batch:{position}/{total}:{symbol}"


@dataclass(frozen=True, slots=True)
class _BatchParams:
    range_pct: float
    dte_min: int | None
    dte_max: int | None
    include_short_dte: bool


@dataclass(slots=True)
class _BatchItem:
    ticker: str
    state: BatchTickerState = "queued"
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
    lock_released: bool = False
    notices: list[str] = field(default_factory=list)

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
    ) -> None:
        if not shutdown_timeout_sec > 0.0:
            raise ValueError("shutdown_timeout_sec must be > 0")
        self.analyzer = analyzer
        self._timer = timer
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._shutdown_timeout_sec = float(shutdown_timeout_sec)
        self._batch: _Batch | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def max_tickers(self) -> int:
        return self.analyzer.settings.watchlist.max_size

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
            )
            task = asyncio.get_running_loop().create_task(
                self._run(batch), name=f"batch-refresh-{batch.batch_id}"
            )
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
        """Request a cooperative cancel (after the current ticker); finished batch -> no-op."""
        batch = self._require(batch_id)
        if batch.state == "running":
            batch.cancel_requested = True
            batch.state = "cancelling"
            logger.info("batch %s cancel requested", batch.batch_id)
        return batch.to_status()

    async def aclose(self) -> None:
        """Shutdown: cancel the running task (mid-ticker), wait (bounded), release the lock.

        Waits at most `shutdown_timeout_sec`; on timeout a warning is logged and the batch is
        finalized anyway. `_finish` is idempotent, so the lock is released exactly once even if
        the stuck task completes later.
        """
        task = self._task
        batch = self._batch
        if task is None or task.done():
            return
        if batch is not None and batch.state == "running":
            batch.cancel_requested = True
            batch.state = "cancelling"
        task.cancel()
        _, pending = await asyncio.wait({task}, timeout=self._shutdown_timeout_sec)
        if pending:
            logger.warning(
                "batch %s: task did not finish within %.1fs of shutdown cancel; "
                "releasing refresh lock anyway",
                batch.batch_id if batch is not None else "?",
                self._shutdown_timeout_sec,
            )
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

    async def _run(self, batch: _Batch) -> None:
        try:
            try:
                stopped = await self.analyzer._stop_corridor(reason="batch_refresh")
            except Exception as exc:
                logger.exception("batch %s: stopping Corridor streams failed", batch.batch_id)
                self._fail_remaining(batch, exc)
                return
            if stopped > 0:
                batch.notices.append(
                    f"Stopped {stopped} active Wall Corridor quote stream(s) before batch "
                    "load to stay within IBKR's 100-line ceiling."
                )
            total = len(batch.items)
            for position, item in enumerate(batch.items, start=1):
                if batch.cancel_requested:
                    break
                await self._run_item(batch, item, position, total)
        finally:
            self._finish(batch)

    async def _run_item(self, batch: _Batch, item: _BatchItem, position: int, total: int) -> None:
        self.analyzer.set_refresh_owner(batch_owner(position, total, item.ticker))
        batch.current_ticker = item.ticker
        item.state = "running"
        params = batch.params
        started = self._timer()
        try:
            outcome = await self.analyzer.refresh_ticker_locked(
                item.ticker,
                range_pct=params.range_pct,
                strike_step=None,  # batches always use Auto strike step
                dte_min=params.dte_min,
                dte_max=params.dte_max,
                include_short_dte=params.include_short_dte,
            )
        except Exception as exc:
            if isinstance(exc, AnalyzerError):
                logger.warning(
                    "batch %s: %s failed (%s): %s",
                    batch.batch_id,
                    item.ticker,
                    type(exc).__name__,
                    exc,
                )
            else:
                logger.exception("batch %s: %s failed unexpectedly", batch.batch_id, item.ticker)
            safe = describe_error(exc, ibkr=self.analyzer.settings.ibkr)
            item.state = "failed"
            item.error_code = safe.code
            item.error = safe.message
        except BaseException:
            item.state = "cancelled"  # shutdown (`aclose`) cancelled the task mid-ticker
            raise
        else:
            item.state = "done"
            item.snapshot_id = outcome.meta.snapshot_id
            item.contract_count = outcome.meta.contract_count
            item.notices = list(outcome.notices)
        finally:
            item.duration_sec = round(max(0.0, self._timer() - started), 3)

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
                    item.state = "cancelled"
            batch.current_ticker = None
            batch.state = "cancelled" if batch.count("cancelled") > 0 else "completed"
            try:
                batch.finished_at_utc = self.analyzer._utc_now_iso()
            except Exception:  # pragma: no cover - defensive (broken injected clock)
                logger.exception("batch %s: clock failed while finishing", batch.batch_id)
        finally:
            batch.lock_released = True
            self.analyzer.release_refresh()
            logger.info(
                "batch %s %s: %d done, %d failed, %d cancelled",
                batch.batch_id,
                batch.state,
                batch.count("done"),
                batch.count("failed"),
                batch.count("cancelled"),
            )

    def _on_task_done(self, batch: _Batch, task: asyncio.Task[None]) -> None:
        self._finish(batch)
        if not task.cancelled():
            with contextlib.suppress(asyncio.CancelledError):
                exc = task.exception()
                if exc is not None:  # pragma: no cover - `_run` catches every Exception
                    logger.error("batch %s task crashed: %s", batch.batch_id, type(exc).__name__)
