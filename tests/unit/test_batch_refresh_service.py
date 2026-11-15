"""Sequential multi-ticker batch refresh: lock ownership, ordering, failure, cancel, shutdown."""

from __future__ import annotations

import asyncio
import itertools
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from src.broker.contracts import ChainPlan, ExpirationMatch, StrikeSelection, UnderlyingInfo
from src.broker.fetcher import ChainFetchProgress, FetchResult
from src.core.config import (
    IBKRSettings,
    LedgerSettings,
    OptionsChainSettings,
    PaperTrackerSettings,
    Settings,
    WatchlistSettings,
)
from src.core.exceptions import (
    AnalyzerError,
    BatchNotFoundError,
    GatewayConnectionError,
    GatewayTimeoutError,
    InvalidRequestError,
    InvalidTickerError,
    OptionChainFetchError,
    PaperTradeCapError,
    RefreshBusyError,
    StorageSchemaError,
    StorageUnavailableError,
)
from src.services.analyzer import AnalyzerService
from src.services.batch_refresh import BATCH_ID_PATTERN, BatchRefreshService, batch_owner
from src.services.error_messages import (
    GENERIC_ERROR_CODE,
    GENERIC_ERROR_MESSAGE,
    MAX_SAFE_MESSAGE_CHARS,
    describe_error,
)
from src.services.schemas import (
    BatchRefreshRequest,
    BatchRefreshStatus,
    RefreshRequest,
    WatchlistTickerRequest,
)
from src.storage.csv_repository import CSVRepository
from src.storage.schema import OptionContractRow
from tests.support.aio import noop_sleep, run_coro, run_virtual_loop

_NOW = datetime(2026, 11, 11, 19, 30, 0, tzinfo=UTC)
_FETCH_SEC = 1.0
_SECRET = "token=SUPERSECRET at C:\\Users\\someone\\stack.py line 42"


def _rows(
    ticker: str, snapshot_id: str, ts_utc: str = "2026-11-11T19:30:00Z"
) -> list[OptionContractRow]:
    rows: list[OptionContractRow] = []
    for conid, strike, right in ((1, 135.0, "P"), (2, 140.0, "P"), (3, 145.0, "C")):
        rows.append(
            OptionContractRow(
                snapshot_id=snapshot_id,
                snapshot_ts_utc=ts_utc,
                market_date="2026-11-11",
                market_data_type=3,
                underlying=ticker,
                underlying_conid=4815,
                underlying_price=145.0,
                option_conid=conid,
                expiry="2026-12-18",
                dte=37,
                strike=strike,
                moneyness_pct=(strike - 145.0) / 145.0,
                right=right,  # type: ignore[arg-type]
                bid=2.0,
                ask=2.2,
                mid=2.1,
                last=2.1,
                close=2.0,
                mark_price=2.1,
                volume=10,
                open_interest=100,
                iv=0.4,
                delta=0.3 if right == "C" else -0.3,
                gamma=0.02,
                theta=-0.05,
                vega=0.1,
                greeks_source="IBKR_MODEL",
            )
        )
    return rows


def _plan(ticker: str) -> ChainPlan:
    return ChainPlan(
        underlying=UnderlyingInfo(
            symbol=ticker,
            conid=4815,
            contract=SimpleNamespace(conId=4815),
            spot=145.0,
            spot_source="MID",
        ),
        market_date=date(2026, 11, 11),
        exchange="SMART",
        trading_class=ticker,
        multiplier="100",
        expirations=(ExpirationMatch("20261218", date(2026, 12, 18), 37),),
        strikes=StrikeSelection(
            strikes=(135.0, 140.0, 145.0),
            atm_strike=145.0,
            requested_step=5.0,
            effective_step=5.0,
        ),
        range_pct=0.20,
        available_strike_steps=(5.0,),
        available_range_pcts=(0.20,),
        exchange_validated_strike_count=3,
    )


class _Broker:
    def __init__(self) -> None:
        self.is_connected = True
        self.current_type = 3
        self.live_subscription_missing = False

    def get_active_market_data_type(self) -> int:
        return self.current_type

    async def ensure_connected(self) -> None:
        return None


class _Corridor:
    def __init__(self, active_streams: int) -> None:
        self.active_stream_count = active_streams
        self.stop_calls = 0

    async def stop_all_streams(self) -> int:
        self.stop_calls += 1
        stopped = self.active_stream_count
        self.active_stream_count = 0
        return stopped


class _Fetcher:
    """Per-ticker fake chain fetcher that records concurrency, lock ownership and progress.

    Each ticker runs `pages[ticker]` (default 1) pages of `pause_sec`; a finished page is
    recorded for the caller's `ChainFetchProgress` (via `_finished`), the in-flight one is not.
    `hang_after[ticker] = k` hangs forever after `k` finished pages (Gateway upstream lost).
    `cleanup_sec` makes the `finally` cleanup (stand-in for `cancelMktData`) take time.
    """

    def __init__(self, *, pause_sec: float, failures: dict[str, BaseException]) -> None:
        self.clock = lambda: _NOW
        self.sleep = noop_sleep
        self.pause_sec = pause_sec
        self.failures = failures
        self.pages: dict[str, int] = {}
        self.hang_after: dict[str, int] = {}
        self.cleanup_sec = 0.0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.cleanups: list[str] = []
        self.cancelled: list[str] = []
        self.partial_calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.probe: Callable[[str], None] | None = None
        self._seq = itertools.count()
        self._finished: dict[int, tuple[str, int]] = {}

    async def fetch_option_chain(self, ticker: str, **kwargs: Any) -> FetchResult:
        self.calls.append((ticker, kwargs))
        progress: ChainFetchProgress | None = kwargs.get("progress")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.probe is not None:
                self.probe(ticker)
            if progress is not None:
                progress.reset()
                progress.plan = _plan(ticker)
                self._finished[id(progress)] = (ticker, 0)
            hang_at = self.hang_after.get(ticker)
            n_pages = self.pages.get(ticker, 1)
            for page in range(n_pages + 1):
                if hang_at is not None and page >= hang_at:
                    await asyncio.sleep(3600.0)
                    raise AssertionError("hung fetch must be cancelled")
                if page == n_pages:
                    break
                if self.pause_sec > 0.0:
                    await asyncio.sleep(self.pause_sec)
                if progress is not None:
                    self._finished[id(progress)] = (ticker, page + 1)
            if ticker in self.failures:
                raise self.failures[ticker]
            snap_id = f"{ticker}_20261111_19{next(self._seq):02d}00"
            if progress is not None:
                progress.completed = True
            return FetchResult(
                snapshot_id=snap_id,
                snapshot_ts_utc="2026-11-11T19:30:00Z",
                market_date="2026-11-11",
                market_data_type=3,
                plan=_plan(ticker),
                rows=_rows(ticker, snap_id),
                pages_fetched=n_pages,
                elapsed_ms=1.0,
                notices=(f"fetched {ticker}",),
            )
        except asyncio.CancelledError:
            self.cancelled.append(ticker)
            raise
        finally:
            if self.cleanup_sec > 0.0:
                await asyncio.sleep(self.cleanup_sec)
            self.active -= 1
            self.cleanups.append(ticker)  # stands in for the fetcher's `cancelMktData` finally

    def build_partial_result(self, progress: ChainFetchProgress) -> FetchResult | None:
        """Fake of `PagedOptionChainFetcher.build_partial_result`: one row per finished page."""
        ticker, pages = self._finished.get(id(progress), ("", 0))
        self.partial_calls.append(ticker)
        if progress.plan is None or pages == 0:
            return None
        snap_id = f"{ticker}_20261111_19{next(self._seq):02d}00"
        return FetchResult(
            snapshot_id=snap_id,
            snapshot_ts_utc="2026-11-11T19:30:00Z",
            market_date="2026-11-11",
            market_data_type=3,
            plan=progress.plan,
            rows=_rows(ticker, snap_id)[: min(3, pages)],
            pages_fetched=pages,
            elapsed_ms=0.0,
            notices=(f"partial {ticker}: {pages} page(s)",),
            partial=True,
        )


def _make(
    tmp_path: Path,
    *,
    pause_sec: float = _FETCH_SEC,
    failures: dict[str, BaseException] | None = None,
    active_streams: int = 0,
    watchlist: WatchlistSettings | None = None,
    options_chain: OptionsChainSettings | None = None,
    gateway_sync_enabled: bool = False,
) -> tuple[AnalyzerService, BatchRefreshService, _Fetcher, _Corridor, list[str]]:
    settings = Settings(
        tracker=PaperTrackerSettings(max_active_trades=20, tracked_dir="data/tracked"),
        ledger=LedgerSettings(gateway_sync_enabled=gateway_sync_enabled, ledger_dir="data/ledger"),
        watchlist=watchlist or WatchlistSettings(),
        options_chain=options_chain or OptionsChainSettings(),
        ibkr=IBKRSettings(host="127.0.0.1", port=4001),
    )
    fetcher = _Fetcher(pause_sec=pause_sec, failures=failures or {})
    corridor = _Corridor(active_streams)
    svc = AnalyzerService(
        settings=settings,
        repo=CSVRepository(settings.storage, root=tmp_path),
        broker_client=_Broker(),  # type: ignore[arg-type]
        fetcher=fetcher,  # type: ignore[arg-type]
        corridor=corridor,  # type: ignore[arg-type]
    )
    svc.ensure_layout()
    quoted: list[str] = []

    async def _quote(ticker: str, *, underlying_conid: int = 0) -> tuple[None, list[str]]:
        assert svc.refresh_lock.locked()
        quoted.append(ticker)
        return None, [f"quoted {ticker}"]

    svc.paper_trades.quote_active_trades_for_refresh = _quote  # type: ignore[method-assign]
    ticks = itertools.count()
    batch = BatchRefreshService(
        svc,
        timer=lambda: float(next(ticks)),
        id_factory=lambda: "ab" * 16,
    )
    return svc, batch, fetcher, corridor, quoted


_FINAL_STATES = ("completed", "cancelled", "aborted")


async def _wait_finished(
    batch: BatchRefreshService, step: float = 0.25, limit_sec: float = 7200.0
) -> BatchRefreshStatus:
    deadline = asyncio.get_running_loop().time() + limit_sec
    while True:
        status = batch.current()
        assert status is not None
        if status.state in _FINAL_STATES and not batch.is_running:
            return status
        assert asyncio.get_running_loop().time() < deadline, f"batch stuck in {status.state}"
        await asyncio.sleep(step)


def _count_releases(svc: AnalyzerService) -> list[int]:
    """Wrap `release_refresh` so a test can assert the batch released the lock exactly once."""
    calls = [0]
    real = svc.release_refresh

    def _release() -> None:
        calls[0] += 1
        real()

    svc.release_refresh = _release  # type: ignore[method-assign]
    return calls


def _req(tickers: list[str], **kw: Any) -> BatchRefreshRequest:
    return BatchRefreshRequest(tickers=tickers, **kw)


# ------------------------------------------------------------------ ordering & lock
def test_batch_runs_strictly_sequential_and_holds_lock_throughout(tmp_path: Path) -> None:
    svc, batch, fetcher, _, quoted = _make(tmp_path)
    owners: list[tuple[str, str | None, bool]] = []
    fetcher.probe = lambda t: owners.append((t, svc.refresh_owner, svc.refresh_lock.locked()))

    async def _scenario() -> tuple[BatchRefreshStatus, BatchRefreshStatus, list[bool]]:
        initial = await batch.start(
            _req(["nvda", "AVGO", " crdo "], range_pct=0.3, dte_min=10, dte_max=90)
        )
        locked_samples: list[bool] = []
        while batch.is_running:
            locked_samples.append(svc.refresh_lock.locked())
            await asyncio.sleep(0.1)
        return initial, await _wait_finished(batch), locked_samples

    initial, final, samples = run_virtual_loop(_scenario())

    assert initial.state == "running"
    assert initial.total == 3
    assert [i.state for i in initial.items] == ["queued", "queued", "queued"]
    assert initial.batch_id == "ab" * 16
    assert all(samples) and len(samples) >= 3  # lock held across every ticker gap
    assert fetcher.max_active == 1
    assert [t for t, _ in fetcher.calls] == ["NVDA", "AVGO", "CRDO"]
    assert owners == [
        ("NVDA", batch_owner(1, 3, "NVDA"), True),
        ("AVGO", batch_owner(2, 3, "AVGO"), True),
        ("CRDO", batch_owner(3, 3, "CRDO"), True),
    ]
    assert owners[2][1] == "batch:3/3:CRDO"
    for _, kwargs in fetcher.calls:
        assert kwargs["strike_step"] is None
        assert kwargs["range_pct"] == 0.3
        assert kwargs["dte_min"] == 10
        assert kwargs["dte_max"] == 90
        assert kwargs["include_short_dte"] is True  # config default when omitted
    assert quoted == ["NVDA", "AVGO", "CRDO"]

    assert final.state == "completed"
    assert final.finished_at_utc == "2026-11-11T19:30:00Z"
    assert final.started_at_utc == "2026-11-11T19:30:00Z"
    assert final.current_ticker is None
    assert (final.done_count, final.failed_count, final.cancelled_count) == (3, 0, 0)
    for item in final.items:
        assert item.state == "done"
        assert item.contract_count == 3
        assert item.duration_sec is not None and item.duration_sec > 0
        assert item.notices == [f"fetched {item.ticker}", f"quoted {item.ticker}"]
        snaps = svc.repo.list_snapshots(item.ticker)
        assert [s.snapshot_id for s in snaps] == [item.snapshot_id]
    assert not svc.refresh_lock.locked()
    assert svc.refresh_owner is None


def test_include_short_dte_passthrough_and_dte_resolution(tmp_path: Path) -> None:
    svc, batch, fetcher, _, _ = _make(
        tmp_path, pause_sec=0.0, options_chain=OptionsChainSettings(dte_min=30, dte_max=90)
    )

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA"], include_short_dte=False))
        return await _wait_finished(batch)

    assert run_virtual_loop(_scenario()).state == "completed"
    assert fetcher.calls[0][1]["include_short_dte"] is False
    assert fetcher.calls[0][1]["dte_min"] is None  # raw request values, like refresh_chain
    with pytest.raises(InvalidRequestError, match="dte_min must be <= dte_max"):
        run_virtual_loop(batch.start(_req(["NVDA"], dte_min=100)))
    assert not svc.refresh_lock.locked()


def test_busy_for_single_refresh_second_batch_corridor_and_ledger(tmp_path: Path) -> None:
    svc, batch, fetcher, _, _ = _make(tmp_path, gateway_sync_enabled=True)

    async def _scenario() -> dict[str, str]:
        await batch.start(_req(["NVDA", "AVGO"]))
        await asyncio.sleep(0.1)
        owners: dict[str, str] = {}
        try:
            await svc.refresh_chain(RefreshRequest(ticker="CRDO"))
        except RefreshBusyError as exc:
            owners["refresh"] = exc.owner
        try:
            await batch.start(_req(["PLTR"]))
        except RefreshBusyError as exc:
            owners["batch"] = exc.owner
        try:
            await svc.open_corridor_stream("NVDA")
        except RefreshBusyError as exc:
            owners["corridor"] = exc.owner
        try:
            await svc.sync_ledger(mode="auto")
        except RefreshBusyError as exc:
            owners["ledger"] = exc.owner
        await asyncio.sleep(_FETCH_SEC)  # now on ticker 2
        try:
            await svc.refresh_chain(RefreshRequest(ticker="CRDO"))
        except RefreshBusyError as exc:
            owners["refresh_2"] = exc.owner
        await _wait_finished(batch)
        return owners

    owners = run_virtual_loop(_scenario())
    assert owners == {
        "refresh": "batch:1/2:NVDA",
        "batch": "batch:1/2:NVDA",
        "corridor": "batch:1/2:NVDA",
        "ledger": "batch:1/2:NVDA",
        "refresh_2": "batch:2/2:AVGO",
    }
    assert [t for t, _ in fetcher.calls] == ["NVDA", "AVGO"]
    assert not svc.refresh_lock.locked()

    # After the batch, a single refresh works again.
    fetcher.pause_sec = 0.0
    payload = run_virtual_loop(svc.refresh_chain(RefreshRequest(ticker="CRDO")))
    assert payload.meta.underlying == "CRDO"


def test_start_refuses_when_lock_held_by_other_operation(tmp_path: Path) -> None:
    svc, batch, fetcher, _, _ = _make(tmp_path)

    async def _scenario() -> str:
        assert await svc.acquire_refresh("refresh:NVDA")
        try:
            await batch.start(_req(["AVGO"]))
        except RefreshBusyError as exc:
            return exc.owner
        finally:
            assert svc.refresh_owner == "refresh:NVDA"  # other holder untouched
            svc.release_refresh()
        return ""

    assert run_virtual_loop(_scenario()) == "refresh:NVDA"
    assert batch.current() is None
    assert fetcher.calls == []


def test_corridor_stopped_once_at_batch_start(tmp_path: Path) -> None:
    svc, batch, _, corridor, _ = _make(tmp_path, pause_sec=0.0, active_streams=2)

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA", "AVGO", "CRDO"]))
        return await _wait_finished(batch)

    status = run_virtual_loop(_scenario())
    assert corridor.stop_calls == 1
    assert len(status.notices) == 1 and "Stopped 2 active Wall Corridor" in status.notices[0]
    assert svc.last_corridor_stop is not None
    assert svc.last_corridor_stop["reason"] == "batch_refresh"


def test_failure_continues_with_sanitized_messages(tmp_path: Path) -> None:
    # Ordinary (non-Gateway) errors: ticker `failed`, nothing saved, the batch continues.
    svc, batch, fetcher, _, quoted = _make(
        tmp_path,
        pause_sec=0.0,
        failures={
            "AVGO": RuntimeError(_SECRET),
            "PLTR": OptionChainFetchError("No option chain parameters for PLTR"),
        },
    )

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["AVGO", "CRDO", "PLTR", "NVDA"]))
        return await _wait_finished(batch)

    status = run_virtual_loop(_scenario())
    by_ticker = {i.ticker: i for i in status.items}
    assert [t for t, _ in fetcher.calls] == ["AVGO", "CRDO", "PLTR", "NVDA"]
    assert status.state == "completed"
    assert (status.done_count, status.failed_count, status.not_started_count) == (2, 2, 0)

    assert by_ticker["AVGO"].state == "failed"
    assert by_ticker["AVGO"].error_code == GENERIC_ERROR_CODE
    assert by_ticker["AVGO"].error == GENERIC_ERROR_MESSAGE
    assert by_ticker["PLTR"].error_code == "OPTION_CHAIN_FETCH_ERROR"
    assert by_ticker["PLTR"].error == "No option chain parameters for PLTR"
    assert by_ticker["CRDO"].state == "done"
    assert by_ticker["NVDA"].state == "done"
    assert not any(i.partial for i in status.items)
    assert fetcher.partial_calls == []  # ordinary errors never try a partial save
    assert svc.repo.list_snapshots("AVGO") == [] and svc.repo.list_snapshots("PLTR") == []
    assert all(i.duration_sec is not None for i in status.items)
    assert "SUPERSECRET" not in status.model_dump_json()
    assert "Traceback" not in status.model_dump_json()
    assert quoted == ["CRDO", "NVDA"]  # failed fetches never reach save / quote
    assert not svc.refresh_lock.locked()
    assert svc.refresh_owner is None


def test_batch_passes_fresh_progress_per_ticker(tmp_path: Path) -> None:
    _, batch, fetcher, _, _ = _make(tmp_path, pause_sec=0.0)

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA", "AVGO"]))
        return await _wait_finished(batch)

    assert run_virtual_loop(_scenario()).state == "completed"
    progresses = [kw["progress"] for _, kw in fetcher.calls]
    assert all(isinstance(p, ChainFetchProgress) for p in progresses)
    assert progresses[0] is not progresses[1]


# ------------------------------------------------------------------ cancel = stop immediately
def test_cancel_mid_ticker_stops_immediately_and_saves_finished_pages(tmp_path: Path) -> None:
    svc, batch, fetcher, _, quoted = _make(tmp_path)
    fetcher.pages["NVDA"] = 3  # pages finish at t=1, 2, 3
    complete_id = "NVDA_20261110_150000"
    svc.repo.save_snapshot("NVDA", _rows("NVDA", complete_id, "2026-11-10T15:00:00Z"))
    releases = _count_releases(svc)

    async def _scenario() -> tuple[BatchRefreshStatus, BatchRefreshStatus, float, float]:
        loop = asyncio.get_running_loop()
        started = await batch.start(_req(["NVDA", "AVGO", "CRDO"]))
        await asyncio.sleep(1.5)  # page 1 finished, page 2 in flight
        t_cancel = loop.time()
        cancelling = batch.cancel(started.batch_id)
        final = await _wait_finished(batch, step=0.01)
        again = batch.cancel(started.batch_id)  # finished -> no-op
        assert again.state == "cancelled"
        return cancelling, final, t_cancel, loop.time()

    cancelling, final, t_cancel, t_done = run_virtual_loop(_scenario())
    assert cancelling.state == "cancelling"
    assert cancelling.current_ticker == "NVDA"
    assert t_done - t_cancel < 0.1  # did not wait for the remaining pages (t=3)
    assert fetcher.cancelled == ["NVDA"]  # child task cancelled, not awaited to completion
    assert fetcher.cleanups == ["NVDA"]  # fetcher `finally` cleanup ran before release
    assert [t for t, _ in fetcher.calls] == ["NVDA"]
    assert quoted == []  # no paper-trade quoting for an interrupted ticker
    nvda, avgo, crdo = final.items
    assert nvda.state == "cancelled" and nvda.partial is True
    assert nvda.contract_count == 1  # one finished page; the in-flight page was dropped
    assert nvda.snapshot_id is not None and nvda.snapshot_id != complete_id
    assert nvda.notices[0] == "partial NVDA: 1 page(s)"
    assert any("latest cache for NVDA still holds" in n for n in nvda.notices)
    assert (avgo.state, crdo.state) == ("cancelled", "cancelled")
    assert not avgo.partial and avgo.snapshot_id is None
    assert final.state == "cancelled"
    assert (final.done_count, final.cancelled_count, final.not_started_count) == (0, 3, 0)

    snaps = svc.repo.list_snapshots("NVDA")
    assert [s.snapshot_id for s in snaps] == [nvda.snapshot_id, complete_id]
    assert snaps[0].partial is True and snaps[0].partial_reason == "cancelled"
    assert svc.repo.latest_cache_snapshot_id("NVDA") == complete_id  # latest untouched
    assert releases == [1]
    assert not svc.refresh_lock.locked() and svc.refresh_owner is None


def test_cancel_before_any_page_finished_saves_nothing(tmp_path: Path) -> None:
    svc, batch, fetcher, _, quoted = _make(tmp_path)
    releases = _count_releases(svc)

    async def _scenario() -> BatchRefreshStatus:
        started = await batch.start(_req(["NVDA"]))
        await asyncio.sleep(0.1)  # only page in flight
        batch.cancel(started.batch_id)
        return await _wait_finished(batch, step=0.01)

    final = run_virtual_loop(_scenario())
    (item,) = final.items
    assert item.state == "cancelled"
    assert (item.partial, item.snapshot_id, item.contract_count) == (False, None, None)
    assert final.state == "cancelled"
    assert fetcher.cancelled == ["NVDA"] and fetcher.cleanups == ["NVDA"]
    assert fetcher.partial_calls == ["NVDA"]
    assert svc.repo.list_snapshots("NVDA") == []
    assert quoted == []
    assert releases == [1]
    assert not svc.refresh_lock.locked()


def test_cancel_between_tickers_stops_before_next(tmp_path: Path) -> None:
    svc, batch, fetcher, corridor, _ = _make(tmp_path, active_streams=1)

    async def _slow_stop() -> int:
        await asyncio.sleep(1.0)  # cancel arrives while no ticker is running yet
        corridor.active_stream_count = 0
        return 1

    corridor.stop_all_streams = _slow_stop  # type: ignore[method-assign]

    async def _scenario() -> BatchRefreshStatus:
        started = await batch.start(_req(["NVDA", "AVGO"]))
        await asyncio.sleep(0.5)
        assert batch.cancel(started.batch_id).state == "cancelling"
        return await _wait_finished(batch)

    final = run_virtual_loop(_scenario())
    assert fetcher.calls == []
    assert [i.state for i in final.items] == ["cancelled", "cancelled"]
    assert final.state == "cancelled"
    assert not svc.refresh_lock.locked()


def test_cancel_cleanup_wait_is_bounded(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    svc, _, fetcher, _, _ = _make(tmp_path)
    batch = BatchRefreshService(svc, cancel_cleanup_sec=10.0)
    fetcher.pages["NVDA"] = 3
    fetcher.cleanup_sec = 60.0  # cleanup outliving the cancel bound
    releases = _count_releases(svc)

    async def _scenario() -> tuple[float, BatchRefreshStatus]:
        loop = asyncio.get_running_loop()
        started = await batch.start(_req(["NVDA", "AVGO"]))
        await asyncio.sleep(1.5)
        t0 = loop.time()
        batch.cancel(started.batch_id)
        final = await _wait_finished(batch, step=0.01)
        waited = loop.time() - t0
        assert len(batch._orphans) == 1  # still referenced while its cleanup runs
        assert await svc.acquire_refresh("refresh:CRDO")  # lock free for the next operation
        (orphan,) = batch._orphans
        await asyncio.wait({orphan})  # the stuck cleanup finishes later (virtual clock)
        return waited, final

    with caplog.at_level("WARNING", logger="src.services.batch_refresh"):
        waited, final = run_virtual_loop(_scenario())
    assert waited == pytest.approx(10.0, abs=0.05)
    assert "cleanup did not finish within 10.0s" in caplog.text
    assert [i.state for i in final.items] == ["cancelled", "cancelled"]
    assert final.items[0].partial is True  # page 1 finished before the cancel
    assert fetcher.cleanups == ["NVDA"]
    assert batch._orphans == set()
    assert svc.refresh_lock.locked() and svc.refresh_owner == "refresh:CRDO"
    assert releases == [1]  # the late orphan never released the other holder's lock
    svc.release_refresh()
    with pytest.raises(ValueError, match="cancel_cleanup_sec"):
        BatchRefreshService(svc, cancel_cleanup_sec=0.0)
    with pytest.raises(ValueError, match="ticker_timeout_sec"):
        BatchRefreshService(svc, ticker_timeout_sec=-1.0)


# ------------------------------------------------------------------ time-outs & Gateway loss
def test_per_ticker_timeout_saves_partial_and_aborts(tmp_path: Path) -> None:
    svc, batch, fetcher, _, quoted = _make(
        tmp_path, watchlist=WatchlistSettings(batch_ticker_timeout_sec=30.0)
    )
    fetcher.pages["AVGO"] = 3
    fetcher.hang_after["AVGO"] = 2  # Gateway lost its upstream after page 2: never answers
    releases = _count_releases(svc)

    async def _scenario() -> tuple[BatchRefreshStatus, float]:
        loop = asyncio.get_running_loop()
        await batch.start(_req(["NVDA", "AVGO", "CRDO", "PLTR"]))
        final = await _wait_finished(batch)
        return final, loop.time()

    final, ended = run_virtual_loop(_scenario())
    assert batch.ticker_timeout_sec == 30.0
    nvda, avgo, crdo, pltr = final.items
    assert nvda.state == "done" and not nvda.partial
    assert avgo.state == "failed"
    assert avgo.error_code == "GATEWAY_TIMEOUT"
    assert avgo.error == "Timed out after 30 s"
    assert avgo.partial is True and avgo.contract_count == 2
    assert (crdo.state, pltr.state) == ("not_started", "not_started")
    assert final.state == "aborted"
    assert (final.done_count, final.failed_count, final.not_started_count) == (1, 1, 2)
    assert final.notices == [
        "Stopped: AVGO timed out after 30 s (IBKR Gateway not answering). "
        "Retry when the Gateway is back."
    ]
    assert ended < 1.0 + 30.0 + 1.0  # NVDA (1 s) + cap; never hangs
    assert fetcher.cancelled == ["AVGO"]
    assert fetcher.cleanups == ["NVDA", "AVGO"]
    assert [t for t, _ in fetcher.calls] == ["NVDA", "AVGO"]
    assert quoted == ["NVDA"]
    (snap,) = svc.repo.list_snapshots("AVGO")
    assert snap.snapshot_id == avgo.snapshot_id
    assert snap.partial is True and snap.partial_reason == "timeout"
    # No latest existed for AVGO yet, so the partial snapshot became the first latest cache.
    assert svc.repo.latest_cache_snapshot_id("AVGO") == avgo.snapshot_id
    assert releases == [1]
    assert not svc.refresh_lock.locked() and svc.refresh_owner is None


@pytest.mark.parametrize(
    ("exc", "reason", "code", "notice"),
    [
        (
            GatewayConnectionError("Lost during chain_fetch"),
            "connection_lost",
            "GATEWAY_OFFLINE",
            "Stopped: IBKR Gateway connection lost during AVGO. Retry when the Gateway is back.",
        ),
        (
            GatewayTimeoutError("IBKR Gateway did not answer during qualify within 20 s"),
            "timeout",
            "GATEWAY_TIMEOUT",
            "Stopped: IBKR Gateway stopped answering during AVGO. Retry when the Gateway is back.",
        ),
    ],
)
def test_gateway_error_saves_partial_and_aborts(
    tmp_path: Path, exc: GatewayConnectionError, reason: str, code: str, notice: str
) -> None:
    svc, batch, fetcher, _, quoted = _make(tmp_path, failures={"AVGO": exc})
    fetcher.pages["AVGO"] = 2
    releases = _count_releases(svc)

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA", "AVGO", "CRDO"]))
        return await _wait_finished(batch)

    final = run_virtual_loop(_scenario())
    nvda, avgo, crdo = final.items
    assert nvda.state == "done"
    assert avgo.state == "failed" and avgo.error_code == code
    assert avgo.error is not None and avgo.error.startswith(str(exc))
    assert "127.0.0.1:4001" in avgo.error
    assert avgo.partial is True and avgo.contract_count == 2
    assert crdo.state == "not_started"
    assert final.state == "aborted"
    assert final.notices == [notice]
    assert [t for t, _ in fetcher.calls] == ["NVDA", "AVGO"]
    assert fetcher.cleanups == ["NVDA", "AVGO"] and fetcher.cancelled == []
    assert quoted == ["NVDA"]
    (snap,) = svc.repo.list_snapshots("AVGO")
    assert (snap.partial, snap.partial_reason) == (True, reason)
    assert releases == [1]
    assert not svc.refresh_lock.locked()


def test_gateway_error_without_finished_page_saves_nothing(tmp_path: Path) -> None:
    svc, batch, fetcher, _, _ = _make(
        tmp_path, failures={"NVDA": GatewayConnectionError("Lost during qualify")}
    )
    fetcher.pages["NVDA"] = 0  # failed before any page finished

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA", "AVGO"]))
        return await _wait_finished(batch)

    final = run_virtual_loop(_scenario())
    assert [i.state for i in final.items] == ["failed", "not_started"]
    assert final.items[0].partial is False and final.items[0].snapshot_id is None
    assert final.state == "aborted"
    assert svc.repo.list_snapshots("NVDA") == []
    assert not svc.refresh_lock.locked()


def test_partial_save_failure_is_reported_and_batch_still_stops(tmp_path: Path) -> None:
    svc, batch, fetcher, _, _ = _make(
        tmp_path, failures={"NVDA": GatewayConnectionError("Lost during chain_fetch")}
    )

    def _broken(*_a: Any, **_kw: Any) -> None:
        raise StorageSchemaError(_SECRET)

    svc.save_partial_ticker_locked = _broken  # type: ignore[method-assign]

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA", "AVGO"]))
        return await _wait_finished(batch)

    final = run_virtual_loop(_scenario())
    nvda = final.items[0]
    assert nvda.state == "failed" and nvda.partial is False
    assert nvda.notices == ["Finished pages could not be saved (STORAGE_SCHEMA_ERROR)."]
    assert final.state == "aborted"
    assert not svc.refresh_lock.locked()


def test_unexpected_child_cancellation_counts_as_connection_loss(tmp_path: Path) -> None:
    svc, batch, _, _, _ = _make(tmp_path, failures={"NVDA": asyncio.CancelledError()})

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA", "AVGO"]))
        return await _wait_finished(batch)

    final = run_virtual_loop(_scenario())
    nvda = final.items[0]
    assert nvda.state == "failed" and nvda.error_code == "GATEWAY_OFFLINE"
    assert nvda.partial is True  # its one page finished before the stray cancellation
    assert final.state == "aborted"
    assert final.items[1].state == "not_started"
    assert not svc.refresh_lock.locked()


@pytest.mark.parametrize("how", ["cancel", "timeout"])
def test_interrupt_during_paper_quoting_keeps_complete_snapshot(tmp_path: Path, how: str) -> None:
    svc, batch, fetcher, _, _ = _make(
        tmp_path, pause_sec=0.0, watchlist=WatchlistSettings(batch_ticker_timeout_sec=30.0)
    )

    async def _slow_quote(ticker: str, *, underlying_conid: int = 0) -> tuple[None, list[str]]:
        await asyncio.sleep(3600.0)  # exact-leg quoting hangs after the chain was saved
        return None, []

    svc.paper_trades.quote_active_trades_for_refresh = _slow_quote  # type: ignore[method-assign]

    async def _scenario() -> BatchRefreshStatus:
        started = await batch.start(_req(["NVDA", "AVGO"]))
        if how == "cancel":
            await asyncio.sleep(1.0)
            batch.cancel(started.batch_id)
        return await _wait_finished(batch)

    final = run_virtual_loop(_scenario())
    nvda, avgo = final.items
    assert nvda.state == "done" and nvda.partial is False
    assert nvda.notices[-1] == (
        "Paper-trade quoting for NVDA was interrupted; the complete chain snapshot was saved."
    )
    (snap,) = svc.repo.list_snapshots("NVDA")  # no duplicate partial snapshot
    assert snap.snapshot_id == nvda.snapshot_id and snap.partial is False
    assert fetcher.partial_calls == []
    if how == "cancel":
        assert (avgo.state, final.state) == ("cancelled", "cancelled")
    else:
        assert (avgo.state, final.state) == ("not_started", "aborted")
        assert final.notices[0].startswith("Stopped: NVDA timed out after 30 s")
    assert not svc.refresh_lock.locked()


def test_outer_task_cancel_is_not_swallowed(tmp_path: Path) -> None:
    svc, batch, fetcher, _, _ = _make(tmp_path)
    fetcher.pages["NVDA"] = 3
    releases = _count_releases(svc)

    async def _scenario() -> tuple[BatchRefreshStatus, bool]:
        await batch.start(_req(["NVDA", "AVGO"]))
        await asyncio.sleep(1.5)
        task = batch._task
        assert task is not None
        task.cancel()  # e.g. event-loop teardown cancelling every task
        await asyncio.wait({task})
        status = batch.current()
        assert status is not None
        return status, task.cancelled()

    status, cancelled = run_virtual_loop(_scenario())
    assert cancelled  # the orchestrator's own CancelledError propagated
    assert fetcher.cancelled == ["NVDA"] and fetcher.cleanups == ["NVDA"]
    assert [i.state for i in status.items] == ["cancelled", "cancelled"]
    assert status.state == "cancelled"
    assert releases == [1]
    assert not svc.refresh_lock.locked()


def test_aclose_cancels_mid_ticker_and_releases_lock(tmp_path: Path) -> None:
    svc, batch, fetcher, _, _ = _make(tmp_path)

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA", "AVGO"]))
        await asyncio.sleep(0.1)
        await batch.aclose()
        status = batch.current()
        assert status is not None
        return status

    status = run_virtual_loop(_scenario())
    assert fetcher.cleanups == ["NVDA"]  # fetcher's own finally ran
    assert [i.state for i in status.items] == ["cancelled", "cancelled"]
    assert status.state == "cancelled"
    assert status.finished_at_utc is not None
    assert not batch.is_running
    assert not svc.refresh_lock.locked()
    assert svc.refresh_owner is None
    run_virtual_loop(batch.aclose())  # idempotent when nothing runs


def test_aclose_before_task_starts_still_releases_lock(tmp_path: Path) -> None:
    svc, batch, fetcher, corridor, _ = _make(tmp_path, active_streams=1)

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA"]))
        await batch.aclose()  # task cancelled before its first step
        status = batch.current()
        assert status is not None
        return status

    status = run_virtual_loop(_scenario())
    assert fetcher.calls == []
    assert corridor.stop_calls == 0
    assert status.state == "cancelled"
    assert not svc.refresh_lock.locked()


def test_aclose_saves_finished_pages_like_cancel(tmp_path: Path) -> None:
    svc, batch, fetcher, _, _ = _make(tmp_path)
    fetcher.pages["NVDA"] = 3

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA", "AVGO"]))
        await asyncio.sleep(2.5)  # two pages finished
        await batch.aclose()
        status = batch.current()
        assert status is not None
        return status

    status = run_virtual_loop(_scenario())
    assert fetcher.cancelled == ["NVDA"]
    assert status.items[0].partial is True and status.items[0].contract_count == 2
    assert status.state == "cancelled"
    assert svc.repo.list_snapshots("NVDA")[0].partial_reason == "cancelled"
    assert not svc.refresh_lock.locked()


def test_aclose_wait_is_bounded_and_lock_released_exactly_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Orchestrator itself stuck past the shutdown bound (cleanup bound > shutdown bound).
    svc, _, fetcher, _, _ = _make(tmp_path)
    batch = BatchRefreshService(svc, shutdown_timeout_sec=15.0, cancel_cleanup_sec=30.0)
    cleanups: list[str] = []

    async def _stuck_fetch(ticker: str, **_kw: Any) -> FetchResult:
        try:
            await asyncio.sleep(3600.0)
            raise AssertionError("fetch must be cancelled by aclose()")
        finally:
            await asyncio.sleep(60.0)  # cleanup outliving the shutdown bound
            cleanups.append(ticker)

    fetcher.fetch_option_chain = _stuck_fetch  # type: ignore[method-assign]

    async def _scenario() -> tuple[float, BatchRefreshStatus, bool, bool]:
        loop = asyncio.get_running_loop()
        await batch.start(_req(["NVDA", "AVGO"]))
        await asyncio.sleep(0.1)
        t0 = loop.time()
        await batch.aclose()
        waited = loop.time() - t0
        status = batch.current()
        assert status is not None
        released, still_running = not svc.refresh_lock.locked(), batch.is_running
        # Another operation takes the lock; the stuck task finishing later must not release it.
        assert await svc.acquire_refresh("refresh:CRDO")
        stuck = batch._task
        assert stuck is not None
        await asyncio.wait({stuck})  # orchestrator: bounded cleanup wait, then re-raise
        assert stuck.cancelled()  # its own CancelledError was not swallowed
        (orphan,) = batch._orphans  # child cleanup (60 s) outlived the 30 s cleanup bound
        await asyncio.wait({orphan})  # let the stuck cleanup finish (virtual clock)
        return waited, status, released, still_running

    with caplog.at_level("WARNING", logger="src.services.batch_refresh"):
        waited, status, released, still_running = run_virtual_loop(_scenario())
    assert waited == pytest.approx(15.0)
    assert released and still_running
    assert status.state == "cancelled"
    assert [i.state for i in status.items] == ["cancelled", "cancelled"]
    assert status.finished_at_utc is not None
    assert "did not finish within 15.0s" in caplog.text
    assert "cleanup did not finish within 30.0s" in caplog.text
    assert cleanups == ["NVDA"]  # the stuck cleanup still completed afterwards
    assert svc.refresh_lock.locked() and svc.refresh_owner == "refresh:CRDO"
    assert [t for t, _ in fetcher.calls] == []  # replaced fetch; original never called
    svc.release_refresh()
    with pytest.raises(ValueError, match="shutdown_timeout_sec"):
        BatchRefreshService(svc, shutdown_timeout_sec=0.0)


def test_corridor_stop_failure_fails_all_items_and_releases(tmp_path: Path) -> None:
    svc, batch, fetcher, corridor, _ = _make(tmp_path, active_streams=1)

    async def _boom() -> int:
        raise RuntimeError(_SECRET)

    corridor.stop_all_streams = _boom  # type: ignore[method-assign]

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA", "AVGO"]))
        return await _wait_finished(batch)

    status = run_virtual_loop(_scenario())
    assert fetcher.calls == []
    assert [i.state for i in status.items] == ["failed", "failed"]
    assert status.state == "completed"
    assert "SUPERSECRET" not in status.model_dump_json()
    assert not svc.refresh_lock.locked()


# ------------------------------------------------------------------ validation & lookup
def test_validation_rejects_before_lock(tmp_path: Path) -> None:
    svc, batch, fetcher, _, _ = _make(
        tmp_path,
        pause_sec=0.0,
        watchlist=WatchlistSettings(hardcoded_tickers=("NVDA", "AVGO"), max_size=3),
    )
    with pytest.raises(InvalidTickerError):
        run_virtual_loop(batch.start(_req(["NVDA", "../etc"])))
    with pytest.raises(InvalidRequestError, match="at most 3"):
        run_virtual_loop(batch.start(_req(["NVDA", "AVGO", "CRDO", "PLTR"])))
    assert not svc.refresh_lock.locked()
    assert batch.current() is None
    assert fetcher.calls == []

    async def _dedupe() -> BatchRefreshStatus:
        # tickers need not be in the watchlist (dynamic Enter flow when the list is full)
        started = await batch.start(_req(["nvda", "NVDA", " avgo ", "CRDO", "crdo"]))
        assert [i.ticker for i in started.items] == ["NVDA", "AVGO", "CRDO"]
        return await _wait_finished(batch)

    assert run_virtual_loop(_dedupe()).done_count == 3


def test_custom_ticker_guard_applies_to_batch(tmp_path: Path) -> None:
    svc, batch, fetcher, _, _ = _make(
        tmp_path,
        watchlist=WatchlistSettings(hardcoded_tickers=("NVDA",), allow_custom_ticker=False),
    )
    with pytest.raises(InvalidTickerError, match="Custom tickers are disabled"):
        run_virtual_loop(batch.start(_req(["NVDA", "AVGO"])))
    assert not svc.refresh_lock.locked()
    assert fetcher.calls == []


def test_unknown_batch_id_raises_not_found(tmp_path: Path) -> None:
    _, batch, _, _, _ = _make(tmp_path, pause_sec=0.0)
    assert batch.current() is None
    with pytest.raises(BatchNotFoundError):
        batch.get("0" * 32)
    with pytest.raises(BatchNotFoundError):
        batch.cancel("0" * 32)

    async def _scenario() -> str:
        started = await batch.start(_req(["NVDA"]))
        await _wait_finished(batch)
        return started.batch_id

    batch_id = run_virtual_loop(_scenario())
    assert batch.get(batch_id).state == "completed"
    with pytest.raises(BatchNotFoundError):
        batch.get("f" * 32)


def test_default_batch_id_format(tmp_path: Path) -> None:
    svc, _, _, _, _ = _make(tmp_path, pause_sec=0.0)
    real = BatchRefreshService(svc)

    async def _scenario() -> str:
        started = await real.start(_req(["NVDA"]))
        await _wait_finished(real)
        return started.batch_id

    assert re.fullmatch(BATCH_ID_PATTERN, run_virtual_loop(_scenario()))


def test_new_batch_replaces_previous_status(tmp_path: Path) -> None:
    _, batch, _, _, _ = _make(tmp_path, pause_sec=0.0)
    ids = iter(["1" * 32, "2" * 32])
    batch._id_factory = lambda: next(ids)

    async def _scenario() -> None:
        await batch.start(_req(["NVDA"]))
        await _wait_finished(batch)
        await batch.start(_req(["AVGO"]))
        await _wait_finished(batch)

    run_virtual_loop(_scenario())
    current = batch.current()
    assert current is not None and current.batch_id == "2" * 32
    with pytest.raises(BatchNotFoundError):
        batch.get("1" * 32)


# ------------------------------------------------------------------ schemas
def test_batch_request_schema_bounds() -> None:
    with pytest.raises(ValidationError):
        BatchRefreshRequest(tickers=[])
    with pytest.raises(ValidationError):
        BatchRefreshRequest.model_validate({"tickers": ["NVDA"], "display_dte_min": 1})
    with pytest.raises(ValidationError):
        BatchRefreshRequest.model_validate({"tickers": ["NVDA"], "strike_step": 5.0})
    with pytest.raises(ValidationError):
        BatchRefreshRequest(tickers=["NVDA"], dte_min=90, dte_max=30)
    with pytest.raises(ValidationError):
        BatchRefreshRequest(tickers=["NVDA"], range_pct=0.0)
    with pytest.raises(ValidationError):
        BatchRefreshRequest(tickers=["X" * 64])
    with pytest.raises(ValidationError):
        BatchRefreshRequest(tickers=["NVDA"] * 51)
    with pytest.raises(ValidationError):
        WatchlistTickerRequest.model_validate({"ticker": "NVDA", "extra": 1})
    req = BatchRefreshRequest(tickers=["NVDA"])
    assert (req.range_pct, req.dte_min, req.dte_max, req.include_short_dte) == (
        0.20,
        None,
        None,
        None,
    )


# ------------------------------------------------------------------ error sanitizer
def test_describe_error_mapping() -> None:
    ibkr = IBKRSettings(host="10.0.0.5", port=4002)
    assert describe_error(InvalidTickerError("Invalid ticker symbol: 'x'")).code == (
        "INVALID_TICKER"
    )
    assert describe_error(PaperTradeCapError("cap")).code == "PAPER_TRADE_CAP"
    assert describe_error(InvalidRequestError("bad")).code == "INVALID_REQUEST"
    assert describe_error(StorageUnavailableError("locked")).code == "STORAGE_UNAVAILABLE"
    gw = describe_error(GatewayConnectionError("down"), ibkr=ibkr)
    assert gw.code == "GATEWAY_OFFLINE"
    assert gw.message.startswith("down — IBKR Gateway on 10.0.0.5:4002 is unreachable.")
    assert describe_error(AnalyzerError(_SECRET)).message == GENERIC_ERROR_MESSAGE
    assert describe_error(KeyError(_SECRET)).message == GENERIC_ERROR_MESSAGE
    flat = describe_error(OptionChainFetchError("line1\nline2\t\x00x" + "y" * 1000))
    assert "\n" not in flat.message and "\x00" not in flat.message
    assert flat.message.startswith("line1 line2 x")
    assert len(flat.message) == MAX_SAFE_MESSAGE_CHARS
    assert describe_error(OptionChainFetchError("  ")).message == GENERIC_ERROR_MESSAGE
    slow = describe_error(
        GatewayTimeoutError("IBKR Gateway did not answer during qualify within 20 s"), ibkr=ibkr
    )
    assert slow.code == "GATEWAY_TIMEOUT"
    assert slow.message.startswith(
        "IBKR Gateway did not answer during qualify within 20 s — IBKR Gateway on "
        "10.0.0.5:4002 is not answering"
    )


def test_refresh_ticker_locked_requires_lock(tmp_path: Path) -> None:
    svc, _, _, _, _ = _make(tmp_path, pause_sec=0.0)
    with pytest.raises(RuntimeError, match="requires refresh_lock"):
        run_coro(
            svc.refresh_ticker_locked(
                "NVDA",
                range_pct=0.2,
                strike_step=None,
                dte_min=None,
                dte_max=None,
                include_short_dte=False,
            )
        )
    svc.set_refresh_owner("batch:1/1:NVDA")  # no-op while the lock is free
    assert svc.refresh_owner is None


def test_status_watchlist_comes_from_store(tmp_path: Path) -> None:
    svc, _, _, _, _ = _make(tmp_path, pause_sec=0.0)

    async def _inline(func: Callable[[], Any]) -> Any:
        return func()

    svc.watchlist.runner = _inline
    assert svc.get_status().watchlist == list(svc.settings.watchlist.hardcoded_tickers)
    added = run_coro(svc.watchlist.add_ticker("aapl"))
    assert added.changed and added.ticker == "AAPL"
    assert svc.get_status().watchlist[-1] == "AAPL"
    removed = run_coro(svc.watchlist.remove_ticker("NVDA"))
    assert "NVDA" not in svc.get_status().watchlist
    assert removed.tickers == svc.get_status().watchlist
    assert (tmp_path / "data" / "watchlist.json").is_file()


# ------------------------------------------------------------------ partial snapshots in services
def _seed_complete_and_partial(svc: AnalyzerService) -> tuple[str, str]:
    complete_id = "NVDA_20261110_150000"
    partial_id = "NVDA_20261111_150000"
    svc.repo.save_snapshot("NVDA", _rows("NVDA", complete_id, "2026-11-10T15:00:00Z"))
    svc.repo.save_snapshot(
        "NVDA",
        _rows("NVDA", partial_id, "2026-11-11T15:00:00Z")[:2],
        partial_reason="cancelled",
    )
    return complete_id, partial_id


def test_save_partial_ticker_locked_guards_and_none(tmp_path: Path) -> None:
    svc, _, fetcher, _, _ = _make(tmp_path, pause_sec=0.0)
    progress = ChainFetchProgress()
    with pytest.raises(RuntimeError, match=r"save_partial_ticker_locked\(\) requires refresh_lock"):
        svc.save_partial_ticker_locked("NVDA", progress, "cancelled")

    async def _scenario() -> None:
        assert await svc.acquire_refresh("test")
        try:
            assert svc.save_partial_ticker_locked("NVDA", progress, "cancelled") is None
            await svc.refresh_ticker_locked(
                "AVGO",
                range_pct=0.2,
                strike_step=None,
                dte_min=None,
                dte_max=None,
                include_short_dte=False,
                progress=progress,
            )
            assert fetcher.calls[-1][1]["progress"] is progress
            with pytest.raises(RuntimeError, match="another ticker"):
                svc.save_partial_ticker_locked("NVDA", progress, "timeout")
            outcome = svc.save_partial_ticker_locked("AVGO", progress, "timeout")
            assert outcome is not None and outcome.partial
            assert outcome.partial_reason == "timeout"
            assert outcome.latest_updated is False  # the complete AVGO refresh owns latest
            assert outcome.meta.partial is True
            assert outcome.enriched.height == 1
        finally:
            svc.release_refresh()

    run_virtual_loop(_scenario())
    assert svc.repo.list_snapshots("NVDA") == []


def test_chain_response_flags_partial_snapshot(tmp_path: Path) -> None:
    svc, _, _, _, _ = _make(tmp_path, pause_sec=0.0)
    complete_id, partial_id = _seed_complete_and_partial(svc)

    part = svc.load_chain_analytics("NVDA", snapshot_id=partial_id)
    assert part.partial is True and part.partial_reason == "cancelled"
    assert part.meta.snapshot_id == partial_id
    latest = svc.load_chain_analytics("NVDA")  # latest cache = the complete snapshot
    assert latest.meta.snapshot_id == complete_id
    assert latest.partial is False and latest.partial_reason is None
    dumped = latest.model_dump()
    assert dumped["partial"] is False and dumped["partial_reason"] is None


def test_snapshots_list_latest_id_skips_partial(tmp_path: Path) -> None:
    svc, _, _, _, _ = _make(tmp_path, pause_sec=0.0)
    complete_id, partial_id = _seed_complete_and_partial(svc)

    listed = svc.list_snapshots("NVDA")
    assert [s.snapshot_id for s in listed.snapshots] == [partial_id, complete_id]
    assert listed.snapshots[0].partial is True
    assert listed.snapshots[0].partial_reason == "cancelled"
    assert listed.latest_snapshot_id == complete_id

    svc.repo.latest_path("NVDA").unlink()  # missing cache: newest complete archive
    assert svc.list_snapshots("NVDA").latest_snapshot_id == complete_id
    assert svc.list_snapshots("AVGO").latest_snapshot_id is None


def test_snapshots_list_latest_id_only_partial(tmp_path: Path) -> None:
    svc, _, _, _, _ = _make(tmp_path, pause_sec=0.0)
    partial_id = "NVDA_20261111_150000"
    svc.repo.save_snapshot("NVDA", _rows("NVDA", partial_id), partial_reason="timeout")
    assert svc.list_snapshots("NVDA").latest_snapshot_id == partial_id  # first-ever latest
    svc.repo.latest_path("NVDA").unlink()
    assert svc.list_snapshots("NVDA").latest_snapshot_id == partial_id


def test_watchlist_freshness_ignores_partial_snapshot(tmp_path: Path) -> None:
    svc, _, _, _, _ = _make(tmp_path, pause_sec=0.0)

    async def _inline(func: Callable[[], Any]) -> Any:
        return func()

    svc.watchlist.runner = _inline
    complete_id, _partial_id = _seed_complete_and_partial(svc)  # partial is today (ET)
    svc.repo.save_snapshot(
        "AVGO", _rows("AVGO", "AVGO_20261111_150000")[:1], partial_reason="connection_lost"
    )
    resp = run_coro(svc.watchlist.get_watchlist())
    entries = {e.ticker: e for e in resp.entries}
    assert entries["NVDA"].latest_snapshot_id == complete_id
    assert entries["NVDA"].is_today_et is False  # today's partial does not count as fresh
    assert entries["AVGO"].latest_snapshot_id is None  # only a partial exists
    assert entries["AVGO"].is_today_et is False


def test_batch_item_done_when_post_save_quoting_raises(tmp_path: Path) -> None:
    """If paper-trade quoting fails after on_saved fired, item is marked done with a notice."""
    svc, batch, _fetcher, _, _ = _make(tmp_path, pause_sec=0.0)

    async def _failing_quote(ticker: str, *, underlying_conid: int = 0) -> tuple[None, list[str]]:
        raise StorageSchemaError("paper_trades.json unreadable")

    svc.paper_trades.quote_active_trades_for_refresh = _failing_quote  # type: ignore[method-assign]

    async def _scenario() -> BatchRefreshStatus:
        await batch.start(_req(["NVDA"]))
        return await _wait_finished(batch)

    final = run_virtual_loop(_scenario())
    assert final.state == "completed"
    assert len(final.items) == 1
    item = final.items[0]
    assert item.ticker == "NVDA"
    assert item.state == "done"
    assert item.snapshot_id is not None
    assert item.partial is False
    assert any("Paper-trade quoting for NVDA failed" in n for n in item.notices)
