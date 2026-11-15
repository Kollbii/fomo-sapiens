"""IBKRBrokerClient: read-only lock, connection errors, error-code handling, market data types."""

from __future__ import annotations

import asyncio
import gc
import warnings
from types import SimpleNamespace
from typing import Any

import pytest
from ib_async import Option

from src.broker.client import (
    DISCONNECT_CODES,
    INFO_CODES,
    RESTORE_CODES,
    IBKRBrokerClient,
)
from src.broker.fetcher import (
    PagedOptionChainFetcher,
)
from src.core.config import IBKRSettings, Settings
from src.core.exceptions import (
    GatewayConnectionError,
    GatewayTimeoutError,
    SecurityViolationError,
)
from src.core.security import ReadOnlyIBGuard
from tests.support.aio import VirtualEventLoop, noop_sleep, run_coro, run_virtual_loop
from tests.support.ib_fakes import FakeChainIB


def test_client_locks_ib_and_connects_type3_default() -> None:
    fake_ib = FakeChainIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    assert ReadOnlyIBGuard.verify_locked(client.ib)
    with pytest.raises(SecurityViolationError):
        client.ib.placeOrder("bad")

    run_coro(client.ensure_connected())
    assert client.is_connected
    assert client.current_type == 3
    assert client.get_active_market_data_type() == 3
    assert fake_ib.market_data_types == [3]

    # Switching to fallback cascades 3 -> 4
    assert client.can_fallback()
    assert client.switch_to_fallback_data_type() == 4
    assert client.current_type == 4
    assert client.get_active_market_data_type() == 4
    assert not client.can_fallback()

    # If client.marketDataType is exposed as a valid int, get_active_market_data_type reads it
    fake_ib.client.marketDataType = 2
    assert client.get_active_market_data_type() == 2
    delattr(fake_ib.client, "marketDataType")
    assert client.get_active_market_data_type() == 4

    client.disconnect()
    assert not client.is_connected


def test_client_connection_errors_raise_gateway_error() -> None:
    fake_ib = FakeChainIB(connect_exc=ConnectionRefusedError("refused"))
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    with pytest.raises(GatewayConnectionError):
        run_coro(client.ensure_connected())
    assert not client.is_connected

    fake_ib2 = FakeChainIB(connect_ok=False)
    client2 = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib2)
    with pytest.raises(GatewayConnectionError):
        run_coro(client2.ensure_connected())


def test_client_error_event_absorbs_10089_354_and_flags_pacing() -> None:
    live_settings = Settings(
        ibkr=IBKRSettings(
            market_data_mode="LIVE",
            default_market_data_type=1,
            fallback_market_data_type=2,
        )
    )
    fake_ib = FakeChainIB()
    client = IBKRBrokerClient(live_settings, ib_factory=lambda: fake_ib)
    assert client.current_type == 1

    # Info codes are ignored
    for code in (2104, 2106, 2107, 2108, 2158):
        fake_ib.errorEvent.emit(-1, code, "farm ok", None)
    assert client.subscription_warning_count == 0

    # Subscription warnings (10089 / 354) do not raise and cascade LIVE -> Type 3 Delayed
    fake_ib.errorEvent.emit(10, 10089, "Requested market data requires subscription", None)
    fake_ib.errorEvent.emit(11, 354, "Not subscribed", None)
    assert client.subscription_warning_count == 2
    assert client.live_subscription_missing is True
    assert client.current_type == 3

    # Pacing warnings (100 / 162) set the pacing flag
    assert not client.consume_pacing_warning()
    fake_ib.errorEvent.emit(-1, 100, "Max rate of messages exceeded", None)
    assert client.consume_pacing_warning() is True
    assert client.consume_pacing_warning() is False


def test_client_configured_types_ticker_observation_and_live_midpage_downgrade() -> None:
    # 1. Explicit primary=2, fallback=2 starts directly on Frozen without false Type 1 reset
    frozen_cfg = Settings(
        ibkr=IBKRSettings(
            market_data_mode="LIVE",
            default_market_data_type=2,
            fallback_market_data_type=2,
        )
    )
    fake_frozen_ib = FakeChainIB()
    frozen_client = IBKRBrokerClient(frozen_cfg, ib_factory=lambda: fake_frozen_ib)
    assert frozen_client.primary_type == 2
    assert frozen_client.fallback_type == 2
    assert frozen_client.can_fallback() is False

    # Empty Ticker(marketDataType=1) default must NOT overwrite Type 3 delayed session
    delayed_client = IBKRBrokerClient(Settings(), ib_factory=lambda: FakeChainIB())
    empty_delayed_tk = SimpleNamespace(
        marketDataType=1, bid=float("nan"), ask=float("nan"), last=float("nan"), close=float("nan")
    )
    assert delayed_client.observe_tickers_market_data_type([empty_delayed_tk]) == 3

    # Real Type 4 callback on delayed ticker updates active market data type dynamically
    tk4 = SimpleNamespace(marketDataType=4, bid=-1.0, ask=-1.0, last=-1.0, close=4.2)
    assert delayed_client.observe_tickers_market_data_type([tk4]) == 4
    assert delayed_client.get_active_market_data_type() == 4

    # 2. Live fetcher catches Error 10089 mid-page-1, logs notice, and re-collects under Type 3
    live_cfg = Settings(
        ibkr=IBKRSettings(
            market_data_mode="LIVE",
            default_market_data_type=1,
            fallback_market_data_type=2,
        )
    )
    fake_live_ib = FakeChainIB()
    live_client = IBKRBrokerClient(live_cfg, ib_factory=lambda: fake_live_ib)
    run_coro(live_client.ensure_connected())
    assert live_client.get_active_market_data_type() == 1

    def _downgrade_factory(contract: Any, _ticks: str) -> Any:
        if live_client.current_type == 1:
            fake_live_ib.errorEvent.emit(
                contract.conId or 1,
                10089,
                "Requested market data requires additional subscription for API",
                contract,
            )
            return SimpleNamespace(contract=contract, bid=-1.0, ask=-1.0, last=-1.0, close=-1.0)
        return SimpleNamespace(
            contract=contract,
            marketDataType=3,
            delayedBid=4.1,
            delayedAsk=4.3,
            delayedLast=4.2,
            close=4.0,
        )

    fake_live_ib.ticker_factory = _downgrade_factory
    fetcher = PagedOptionChainFetcher(live_client, live_cfg, sleep=noop_sleep)
    contracts = [Option("NVDA", "20261211", 170.0, "C", "SMART", conId=501)]
    tickers, pages = run_coro(fetcher.fetch_pages(contracts))
    assert pages == 1
    assert len(tickers) == 1
    assert live_client.live_subscription_missing is True
    assert live_client.get_active_market_data_type() == 3
    assert any("MarketDataType(3)" in n for n in fetcher._notices)
    # After 10089 downgrade to Type 3, delayed-frozen fallback (3 -> 4) remains enabled
    assert live_client.can_fallback() is True
    assert live_client.switch_to_fallback_data_type() == 4


# ------------------------------------------------------------- bounded stages
def _connected_client(settings: Settings | None = None) -> tuple[IBKRBrokerClient, FakeChainIB]:
    fake_ib = FakeChainIB()
    client = IBKRBrokerClient(settings or Settings(), ib_factory=lambda: fake_ib)
    run_coro(client.ensure_connected())
    return client, fake_ib


class _HangingRequest:
    """IBKR request that is never answered; records whether it was cancelled."""

    def __init__(self) -> None:
        self.cancelled = 0

    async def __call__(self, *_args: Any) -> list[Any]:
        try:
            await asyncio.get_running_loop().create_future()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return []  # pragma: no cover - never answered


def test_request_timeout_setting_default_and_bounds() -> None:
    assert IBKRSettings().request_timeout_sec == 20.0
    assert IBKRSettings(request_timeout_sec=120).request_timeout_sec == 120.0
    for bad in (0, -1.0, 120.5):
        with pytest.raises(ValueError):
            IBKRSettings(request_timeout_sec=bad)
    assert issubclass(GatewayTimeoutError, GatewayConnectionError)


def test_run_stage_times_out_with_gateway_timeout_error_and_keeps_socket() -> None:
    client, _fake_ib = _connected_client()
    hang = _HangingRequest()

    async def _run() -> float:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(GatewayTimeoutError) as exc_info:
            await client.run_stage("underlying qualification (NVDA)", hang())
        assert str(exc_info.value) == (
            "IBKR Gateway did not answer during underlying qualification (NVDA) within 20 s"
        )
        elapsed = loop.time() - t0
        await asyncio.sleep(0)  # let the abandoned request task observe its cancellation
        return elapsed

    assert run_virtual_loop(_run()) == pytest.approx(20.0)
    assert hang.cancelled == 1
    # A plain time-out keeps the (healthy) local API socket open; no abort future leaks.
    assert client.is_connected
    assert client.was_connected
    assert client._stage_aborts == set()


def test_run_stage_honours_configured_and_explicit_timeouts() -> None:
    client, _fake_ib = _connected_client(Settings(ibkr=IBKRSettings(request_timeout_sec=7.5)))

    async def _run() -> tuple[float, float]:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(GatewayTimeoutError, match=r"within 7\.5 s"):
            await client.run_stage("secdef lookup (NVDA)", _HangingRequest()())
        t1 = loop.time()
        with pytest.raises(GatewayTimeoutError, match="within 2 s"):
            await client.run_stage("reqPositions", _HangingRequest()(), timeout_sec=2.0)
        return t1 - t0, loop.time() - t1

    first, second = run_virtual_loop(_run())
    assert first == pytest.approx(7.5)
    assert second == pytest.approx(2.0)


@pytest.mark.parametrize("code", sorted(DISCONNECT_CODES))
def test_disconnect_code_fails_pending_stage_immediately(code: int) -> None:
    client, fake_ib = _connected_client()
    hang = _HangingRequest()

    async def _run() -> float:
        loop = asyncio.get_running_loop()
        loop.call_later(1.0, fake_ib.errorEvent.emit, -1, code, "Connectivity lost", None)
        t0 = loop.time()
        with pytest.raises(GatewayConnectionError) as exc_info:
            await client.run_stage("secdef lookup (NVDA)", hang())
        assert not isinstance(exc_info.value, GatewayTimeoutError)
        assert str(exc_info.value) == (
            "IBKR Gateway lost its upstream connection during secdef lookup (NVDA) "
            f"(IBKR {code}: Connectivity lost)"
        )
        elapsed = loop.time() - t0
        await asyncio.sleep(0)
        return elapsed

    assert run_virtual_loop(_run()) == pytest.approx(1.0)  # not the 20 s time-out
    assert hang.cancelled == 1
    assert client._stage_aborts == set()


class _RecordingVirtualLoop(VirtualEventLoop):
    """Virtual loop that records `call_exception_handler` contexts instead of dropping them."""

    def __init__(self) -> None:
        super().__init__()
        self.exception_contexts: list[dict[str, Any]] = []

    def call_exception_handler(self, context: dict[str, Any]) -> None:
        self.exception_contexts.append(context)


def test_link_loss_and_failed_request_in_same_step_retrieves_request_outcome() -> None:
    client, fake_ib = _connected_client()
    loop = _RecordingVirtualLoop()
    requests: list[asyncio.Future[list[Any]]] = [loop.create_future()]

    def _fail_and_drop() -> None:
        # Both waiters complete before `await_guarded` resumes: one `asyncio.wait` step.
        requests[0].set_exception(RuntimeError("request failed"))
        fake_ib.errorEvent.emit(-1, 1100, "Connectivity lost", None)

    async def _run() -> None:
        asyncio.get_running_loop().call_later(1.0, _fail_and_drop)
        with pytest.raises(GatewayConnectionError, match=r"upstream connection.*IBKR 1100"):
            await client.run_stage("secdef lookup (NVDA)", requests[0])

    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()
    assert requests[0].done()
    assert not requests[0]._log_traceback
    requests.clear()  # drop the last reference: an unretrieved outcome would be logged now
    gc.collect()
    assert not any(
        "never retrieved" in str(ctx.get("message", "")) for ctx in loop.exception_contexts
    )
    assert client._stage_aborts == set()


def test_info_restore_and_warning_codes_never_fail_a_pending_stage() -> None:
    client, fake_ib = _connected_client()
    harmless = sorted(INFO_CODES | RESTORE_CODES | {2103, 2105, 2157, 100, 162, 10089, 354, 200})
    assert not set(harmless) & DISCONNECT_CODES

    async def _slow_answer() -> list[str]:
        await asyncio.sleep(2.0)
        return ["answered"]

    def _emit_all() -> None:
        for code in harmless:
            fake_ib.errorEvent.emit(-1, code, "notice", None)

    async def _run() -> list[str]:
        loop = asyncio.get_running_loop()
        loop.call_later(1.0, _emit_all)
        with client.link_watch():
            res: list[str] = await client.run_stage("secdef lookup (NVDA)", _slow_answer())
        return res

    assert run_virtual_loop(_run()) == ["answered"]
    assert client.link_loss_seq == 0
    assert client.is_connected


def test_error_listener_never_raises_without_loop_and_scopes_upstream_loss() -> None:
    client, fake_ib = _connected_client()
    # No running loop, no pending stage: the listener only records the notice.
    fake_ib.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost", None)
    fake_ib.errorEvent.emit(-1, 1102, "Connectivity restored - data maintained", None)
    assert client.link_loss_seq == 1
    # A stale notice from before an operation never fails the next operation.
    contract = Option("NVDA", "20261218", 140.0, "C", "SMART")
    with client.link_watch() as since:
        assert since == 1
        res = run_coro(
            client.run_stage("contract qualification (1)", fake_ib.qualifyContractsAsync(contract))
        )
        assert res == [contract]
        # Upstream lost inside the scope: every later stage fails before sending its request.
        fake_ib.errorEvent.emit(-1, 2110, "Connectivity between TWS and server is broken", None)
        pending = fake_ib.qualifyContractsAsync(contract)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # the unsent request coroutine is closed cleanly
            with pytest.raises(GatewayConnectionError, match=r"upstream connection.*IBKR 2110"):
                run_coro(client.run_stage("contract qualification (1)", pending))
        with client.link_watch() as nested_since:  # nested scopes keep the outer start
            assert nested_since == 1
    assert client._link_scope_since is None
    assert client.is_connected  # upstream loss alone keeps the local socket


def test_run_stage_cancellation_cancels_the_pending_request() -> None:
    client, _fake_ib = _connected_client()
    hang = _HangingRequest()

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        stage = asyncio.ensure_future(client.run_stage("secdef lookup (NVDA)", hang()))
        loop.call_later(1.0, stage.cancel)
        with pytest.raises(asyncio.CancelledError):
            await stage
        await asyncio.sleep(0)

    run_virtual_loop(_run())
    assert hang.cancelled == 1
    assert client._stage_aborts == set()


def test_run_stage_socket_drop_still_maps_to_transport_error() -> None:
    client, fake_ib = _connected_client()

    async def _drop() -> list[Any]:
        fake_ib._connected = False
        raise ConnectionResetError("socket closed")

    async def _run() -> None:
        with pytest.raises(GatewayConnectionError, match="connection interrupted during reqPos"):
            await client.run_stage("reqPositions", _drop())

    run_virtual_loop(_run())
    assert not client.was_connected
