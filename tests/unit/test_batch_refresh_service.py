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
from src.broker.fetcher import FetchResult
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
    InvalidRequestError,
    InvalidTickerError,
    OptionChainFetchError,
    PaperTradeCapError,
    RefreshBusyError,
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


def _rows(ticker: str, snapshot_id: str) -> list[OptionContractRow]:
    rows: list[OptionContractRow] = []
    for conid, strike, right in ((1, 135.0, "P"), (2, 140.0, "P"), (3, 145.0, "C")):
        rows.append(
            OptionContractRow(
                snapshot_id=snapshot_id,
                snapshot_ts_utc="2026-11-11T19:30:00Z",
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
    """Per-ticker fake chain fetcher that records concurrency and lock ownership."""

    def __init__(self, *, pause_sec: float, failures: dict[str, BaseException]) -> None:
        self.clock = lambda: _NOW
        self.sleep = noop_sleep
        self.pause_sec = pause_sec
        self.failures = failures
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.cleanups: list[str] = []
        self.active = 0
        self.max_active = 0
        self.probe: Callable[[str], None] | None = None
        self._seq = itertools.count()

    async def fetch_option_chain(self, ticker: str, **kwargs: Any) -> FetchResult:
        self.calls.append((ticker, kwargs))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.probe is not None:
                self.probe(ticker)
            if self.pause_sec > 0.0:
                await asyncio.sleep(self.pause_sec)
            if ticker in self.failures:
                raise self.failures[ticker]
            snap_id = f"{ticker}_20261111_19{next(self._seq):02d}00"
            return FetchResult(
                snapshot_id=snap_id,
                snapshot_ts_utc="2026-11-11T19:30:00Z",
                market_date="2026-11-11",
                market_data_type=3,
                plan=_plan(ticker),
                rows=_rows(ticker, snap_id),
                pages_fetched=1,
                elapsed_ms=1.0,
                notices=(f"fetched {ticker}",),
            )
        finally:
            self.active -= 1
            self.cleanups.append(ticker)  # stands in for the fetcher's `cancelMktData` finally


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


async def _wait_finished(batch: BatchRefreshService, step: float = 0.25) -> BatchRefreshStatus:
    while True:
        status = batch.current()
        assert status is not None
        if status.state in ("completed", "cancelled") and not batch.is_running:
            return status
        await asyncio.sleep(step)


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
    svc, batch, fetcher, _, quoted = _make(
        tmp_path,
        pause_sec=0.0,
        failures={
            "AVGO": RuntimeError(_SECRET),
            "CRDO": GatewayConnectionError("Lost during chain_fetch"),
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
    assert (status.done_count, status.failed_count) == (1, 3)

    assert by_ticker["AVGO"].state == "failed"
    assert by_ticker["AVGO"].error_code == GENERIC_ERROR_CODE
    assert by_ticker["AVGO"].error == GENERIC_ERROR_MESSAGE
    assert by_ticker["CRDO"].error_code == "GATEWAY_OFFLINE"
    assert by_ticker["CRDO"].error is not None
    assert "127.0.0.1:4001 is unreachable" in by_ticker["CRDO"].error
    assert by_ticker["PLTR"].error_code == "OPTION_CHAIN_FETCH_ERROR"
    assert by_ticker["PLTR"].error == "No option chain parameters for PLTR"
    assert by_ticker["NVDA"].state == "done"
    assert all(i.duration_sec is not None for i in status.items)
    assert "SUPERSECRET" not in status.model_dump_json()
    assert "Traceback" not in status.model_dump_json()
    assert quoted == ["NVDA"]  # failed fetches never reach save / quote
    assert not svc.refresh_lock.locked()
    assert svc.refresh_owner is None


def test_cancel_takes_effect_after_current_ticker(tmp_path: Path) -> None:
    svc, batch, fetcher, _, quoted = _make(tmp_path)

    async def _scenario() -> tuple[BatchRefreshStatus, BatchRefreshStatus, BatchRefreshStatus]:
        started = await batch.start(_req(["NVDA", "AVGO", "CRDO"]))
        await asyncio.sleep(0.1)  # NVDA fetch in flight
        cancelling = batch.cancel(started.batch_id)
        final = await _wait_finished(batch)
        again = batch.cancel(started.batch_id)  # finished -> no-op
        return cancelling, final, again

    cancelling, final, again = run_virtual_loop(_scenario())
    assert cancelling.state == "cancelling"
    assert cancelling.current_ticker == "NVDA"
    assert [t for t, _ in fetcher.calls] == ["NVDA"]
    assert fetcher.cleanups == ["NVDA"]
    assert quoted == ["NVDA"]
    assert [i.state for i in final.items] == ["done", "cancelled", "cancelled"]
    assert final.items[0].snapshot_id is not None
    assert final.state == "cancelled"
    assert (final.done_count, final.cancelled_count) == (1, 2)
    assert again.state == "cancelled"
    assert not svc.refresh_lock.locked()


def test_cancel_during_last_ticker_completes(tmp_path: Path) -> None:
    svc, batch, _, _, _ = _make(tmp_path)

    async def _scenario() -> BatchRefreshStatus:
        started = await batch.start(_req(["NVDA"]))
        await asyncio.sleep(0.1)
        batch.cancel(started.batch_id)
        return await _wait_finished(batch)

    final = run_virtual_loop(_scenario())
    assert final.items[0].state == "done"
    assert final.state == "completed"
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


def test_aclose_wait_is_bounded_and_lock_released_exactly_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    svc, _, fetcher, _, _ = _make(tmp_path)
    batch = BatchRefreshService(svc, shutdown_timeout_sec=15.0)
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
        await asyncio.wait({stuck})  # let the stuck cleanup finish (virtual clock)
        return waited, status, released, still_running

    with caplog.at_level("WARNING", logger="src.services.batch_refresh"):
        waited, status, released, still_running = run_virtual_loop(_scenario())
    assert waited == pytest.approx(15.0)
    assert released and still_running
    assert status.state == "cancelled"
    assert [i.state for i in status.items] == ["cancelled", "cancelled"]
    assert status.finished_at_utc is not None
    assert "did not finish within 15.0s" in caplog.text
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
