"""User watchlist service: `WatchlistStore` persistence + per-ticker archive freshness.

All store / snapshot-index I/O is synchronous and runs through the injected `runner`
(default `asyncio.to_thread`) so the event loop never blocks. Freshness comes from ONE
snapshot-index read (`CSVRepository.latest_snapshots`); no IBKR call is ever made.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from typing import Final, Protocol, TypeVar

from src.core.calendar import et_date
from src.core.exceptions import StorageSchemaError
from src.services.schemas import WatchlistEntry, WatchlistResponse
from src.storage.csv_repository import CSVRepository
from src.storage.watchlist_store import WatchlistState, WatchlistStore

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

FRESHNESS_UNAVAILABLE_NOTICE: Final = (
    "Snapshot index could not be read; freshness markers are unavailable."
)


class BlockingRunner(Protocol):
    """Runs a blocking zero-argument callable off the event loop (e.g. `asyncio.to_thread`)."""

    def __call__(self, func: Callable[[], _T], /) -> Awaitable[_T]: ...


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


class WatchlistService:
    """List / add / remove the user's watchlist; never touches snapshot CSVs.

    Domain errors propagate unchanged: `InvalidTickerError` (bad symbol or custom-ticker guard),
    `WatchlistCapError` (list full), `StorageUnavailableError` (saved file cannot be opened).
    """

    def __init__(
        self,
        store: WatchlistStore,
        repo: CSVRepository,
        *,
        clock: Callable[[], datetime],
        runner: BlockingRunner | None = None,
    ) -> None:
        self.store = store
        self.repo = repo
        self.clock = clock
        self.runner: BlockingRunner = runner or asyncio.to_thread
        self._last_state: WatchlistState | None = None

    @property
    def max_size(self) -> int:
        return self.store.max_size

    @property
    def allow_custom(self) -> bool:
        """Custom-ticker guard, read from the store's validator so the two can never drift."""
        return self.store.validator.allow_custom

    def ensure_layout(self) -> None:
        self.store.ensure_layout()

    def cached_tickers(self) -> list[str]:
        """Tickers from the last async load/add/remove (sync; for status / bootstrap payloads).

        Before the first async call this performs one bounded (<= 64 KiB) synchronous read;
        the composition root primes the cache with `await get_watchlist()` at startup.
        """
        state = self._last_state
        if state is None:
            state = self._remember(self.store.load())
        return list(state.tickers)

    async def get_watchlist(self) -> WatchlistResponse:
        return await self.runner(lambda: self._respond(self.store.load()))

    async def add_ticker(self, raw_ticker: str) -> WatchlistResponse:
        def _add() -> WatchlistResponse:
            update = self.store.add(raw_ticker)
            return self._respond(update.state, ticker=update.ticker, changed=update.changed)

        return await self.runner(_add)

    async def remove_ticker(self, raw_ticker: str) -> WatchlistResponse:
        def _remove() -> WatchlistResponse:
            update = self.store.remove(raw_ticker)
            return self._respond(update.state, ticker=update.ticker, changed=update.changed)

        return await self.runner(_remove)

    # ------------------------------------------------------------------ internals
    def _remember(self, state: WatchlistState) -> WatchlistState:
        self._last_state = state
        return state

    def _today_et(self) -> date:
        return et_date(self.clock())

    def _entries(self, tickers: Sequence[str], today: date) -> tuple[list[WatchlistEntry], bool]:
        try:
            latest = self.repo.latest_snapshots(tickers)
        except (StorageSchemaError, OSError) as exc:
            logger.warning("watchlist freshness unavailable (%s)", type(exc).__name__)
            return [WatchlistEntry(ticker=t) for t in tickers], False
        entries: list[WatchlistEntry] = []
        for ticker in tickers:
            meta = latest.get(ticker)
            if meta is None:
                entries.append(WatchlistEntry(ticker=ticker))
                continue
            ts = _parse_utc(meta.snapshot_ts_utc)
            entries.append(
                WatchlistEntry(
                    ticker=ticker,
                    latest_snapshot_id=meta.snapshot_id,
                    latest_snapshot_utc=meta.snapshot_ts_utc,
                    is_today_et=ts is not None and et_date(ts) == today,
                )
            )
        return entries, True

    def _respond(
        self,
        state: WatchlistState,
        *,
        ticker: str | None = None,
        changed: bool = False,
    ) -> WatchlistResponse:
        self._remember(state)
        today = self._today_et()
        entries, fresh_ok = self._entries(state.tickers, today)
        notices = [
            n for n in (state.notice, None if fresh_ok else FRESHNESS_UNAVAILABLE_NOTICE) if n
        ]
        return WatchlistResponse(
            tickers=list(state.tickers),
            entries=entries,
            count=state.count,
            max_size=state.max_size,
            is_full=state.is_full,
            allow_custom=self.allow_custom,
            writable=state.writable,
            notice=" ".join(notices) or None,
            today_et=today.isoformat(),
            ticker=ticker,
            changed=changed,
        )
