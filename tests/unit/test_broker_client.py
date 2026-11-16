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
    QUALIFY_CHUNK_PAUSE_SEC,
    QUALIFY_CHUNK_SIZE,
    RESTORE_CODES,
    IBKRBrokerClient,
)
from src.broker.fetcher import (
    PagedOptionChainFetcher,
)
from src.broker.flex import MAX_SAFE_MESSAGE_CHARS
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
    assert IBKRSettings().request_timeout_sec == 25.0
    assert IBKRSettings(request_timeout_sec=120).request_timeout_sec == 120.0
    for bad in (0, -1.0, 120.5):
        with pytest.raises(ValueError):
            IBKRSettings(request_timeout_sec=bad)
    assert issubclass(GatewayTimeoutError, GatewayConnectionError)


def _conn_state(client: IBKRBrokerClient) -> str:
    return client.connection_state


def test_run_stage_times_out_recycles_socket_and_tracks_connection_state() -> None:
    fake_ib = FakeChainIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    assert _conn_state(client) == "offline"
    assert client.connection_notice is None

    run_coro(client.ensure_connected())
    assert _conn_state(client) == "connected"
    assert client.connection_notice is None

    hang = _HangingRequest()

    async def _run() -> float:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(GatewayTimeoutError) as exc_info:
            await client.run_stage("underlying qualification (NVDA)", hang())
        assert str(exc_info.value) == (
            "IBKR Gateway did not answer during underlying qualification (NVDA) within 25 s"
        )
        elapsed = loop.time() - t0
        await asyncio.sleep(0)  # let the abandoned request task observe its cancellation
        return elapsed

    assert run_virtual_loop(_run()) == pytest.approx(25.0)
    assert hang.cancelled == 1
    assert not client.is_connected
    assert not client.was_connected
    assert _conn_state(client) == "timeout_reset"
    assert client.connection_notice == (
        "Socket recycled after Gateway time-out during underlying qualification (NVDA); "
        "auto-reconnects on next pull"
    )
    assert client._stage_aborts == set()

    # Reconnecting clears timeout diagnostics; explicit disconnect returns to offline.
    run_coro(client.ensure_connected())
    assert _conn_state(client) == "connected"
    assert client.connection_notice is None

    client.reset_after_timeout(stage="manual check")
    assert _conn_state(client) == "timeout_reset"
    client.disconnect()
    assert _conn_state(client) == "offline"
    assert client.connection_notice is None


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

    assert run_virtual_loop(_run()) == pytest.approx(1.0)  # not the 25 s time-out
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
        fake_ib.errorEvent.emit(-1, 502, "Couldn't connect to TWS", None)
        with pytest.raises(GatewayConnectionError, match="connection interrupted during reqPos"):
            await client.run_stage("reqPositions", _drop())

    run_virtual_loop(_run())
    assert not client.was_connected
    assert _conn_state(client) == "connection_lost"
    assert client.connection_notice == "IBKR 502: Couldn't connect to TWS"
    client.disconnect()
    assert _conn_state(client) == "offline"
    assert client.connection_notice is None


def test_qualify_contracts_chunks_with_pacing_and_aborts_on_loss() -> None:
    client, fake_ib = _connected_client()
    assert run_coro(client.qualify_contracts([])) == []
    with pytest.raises(ValueError, match="chunk_size must be >= 1"):
        run_coro(
            client.qualify_contracts(
                [Option("NVDA", "20261218", 140.0, "C", "SMART")], chunk_size=0
            )
        )

    batches: list[int] = []
    sleeps: list[float] = []

    def _chunk_qualify(*contracts: Any) -> list[Any]:
        batches.append(len(contracts))
        if len(batches) == 3:
            # Partial mismatch on chunk 3: only 3 of 5 contracts qualify.
            kept: list[Any] = []
            for idx, c in enumerate(contracts):
                if idx >= 3:
                    c.conId = 0
                    continue
                c.conId = 7000 + len(batches) * 100 + idx
                kept.append(c)
            return kept
        for idx, c in enumerate(contracts):
            c.conId = 7000 + len(batches) * 100 + idx
        return list(contracts)

    async def _rec_sleep(sec: float) -> None:
        sleeps.append(sec)

    fake_ib.qualify_handler = _chunk_qualify
    contracts = [
        Option("NVDA", "20261218", 100.0 + idx, "C", "SMART")
        for idx in range(QUALIFY_CHUNK_SIZE * 2 + 5)
    ]
    res = run_coro(client.qualify_contracts(contracts, sleep=_rec_sleep))
    assert batches == [QUALIFY_CHUNK_SIZE, QUALIFY_CHUNK_SIZE, 5]
    assert sleeps == [QUALIFY_CHUNK_PAUSE_SEC, QUALIFY_CHUNK_PAUSE_SEC]
    assert len(res) == QUALIFY_CHUNK_SIZE * 2 + 5
    assert all(c.conId > 0 for c in res[: QUALIFY_CHUNK_SIZE * 2 + 3])
    assert all(c.conId == 0 for c in res[QUALIFY_CHUNK_SIZE * 2 + 3 :])

    # Upstream loss arriving during the inter-chunk pause aborts before the next chunk.
    client2, fake_ib2 = _connected_client()
    sent: list[int] = []

    def _ok(*batch: Any) -> list[Any]:
        sent.append(len(batch))
        for idx, c in enumerate(batch):
            c.conId = 8000 + idx
        return list(batch)

    fake_ib2.qualify_handler = _ok

    async def _lose_link(_sec: float) -> None:
        fake_ib2.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS lost", None)

    batch15 = [Option("NVDA", "20261218", 100.0 + idx, "C", "SMART") for idx in range(15)]
    with pytest.raises(GatewayConnectionError, match=r"upstream connection.*IBKR 1100"):
        run_coro(client2.qualify_contracts(batch15, sleep=_lose_link))
    assert QUALIFY_CHUNK_SIZE == 8
    assert QUALIFY_CHUNK_PAUSE_SEC == 0.45
    assert sent == [QUALIFY_CHUNK_SIZE]


def test_error_200_tracking_reset_and_consume_without_failing_stage() -> None:
    client, fake_ib = _connected_client()
    assert client.contract_not_found_count == 0
    assert client.consume_contract_not_found() == 0

    fake_ib.errorEvent.emit(10, 200, "No security definition has been found for the request", None)
    fake_ib.errorEvent.emit(11, 200, "No security definition has been found for the request", None)
    assert client.contract_not_found_count == 2
    assert client.consume_pacing_warning() is False
    assert client.link_loss_seq == 0
    assert client.is_connected

    assert client.consume_contract_not_found() == 2
    assert client.contract_not_found_count == 0
    assert client.consume_contract_not_found() == 0

    fake_ib.errorEvent.emit(12, 200, "No security definition has been found for the request", None)
    assert client.contract_not_found_count == 1
    client.reset_contract_not_found()
    assert client.contract_not_found_count == 0

    fake_ib.errorEvent.emit(13, 200, "No security definition has been found for the request", None)
    assert client.contract_not_found_count == 1
    client.reset_market_data_type()
    assert client.contract_not_found_count == 0


def test_qualify_contracts_replaces_none_and_non_contract_slots_in_same_length_chunk() -> None:
    client, fake_ib = _connected_client()
    c0 = Option("NVDA", "20261218", 130.0, "C", "SMART")
    c1 = Option("NVDA", "20261218", 135.0, "C", "SMART")
    c2 = Option("NVDA", "20261218", 140.0, "C", "SMART")
    c3 = Option("NVDA", "20261218", 145.0, "C", "SMART")

    def _same_len_with_nulls(*contracts: Any) -> list[Any]:
        contracts[0].conId = 9001
        contracts[3].conId = 9003
        # Simulate ib_async returning None for an unknown contract and a list for an ambiguous one.
        return [contracts[0], None, [contracts[2]], contracts[3]]

    fake_ib.qualify_handler = _same_len_with_nulls
    res = run_coro(client.qualify_contracts([c0, c1, c2, c3], sleep=noop_sleep))
    assert len(res) == 4
    assert res[0] is c0 and res[0].conId == 9001
    assert res[1] is c1 and res[1].conId == 0
    assert res[2] is c2 and res[2].conId == 0
    assert res[3] is c3 and res[3].conId == 9003


def test_connection_notice_sanitizes_secrets_controls_and_caps_length() -> None:
    client, fake_ib = _connected_client()
    raw_msg = (
        "Disconnected\n\t\x00for acctId=U1234567 and account DU9876543 "
        "https://ndcdyn.interactivebrokers.com/flex?t=tok-secret-1 token=my-secret-token\r\n"
        "Traceback (most recent call last):\n  File '/app/secret.py', line 9\n" + "x" * 500
    )
    fake_ib.errorEvent.emit(-1, 1100, raw_msg, None)

    async def _drop() -> None:
        fake_ib._connected = False
        raise ConnectionResetError("socket closed")

    with pytest.raises(GatewayConnectionError):
        run_virtual_loop(client.run_stage("reqPositions", _drop()))

    assert _conn_state(client) == "connection_lost"
    notice = client.connection_notice
    assert notice is not None
    assert notice.startswith("IBKR 1100:")
    assert len(notice) <= MAX_SAFE_MESSAGE_CHARS
    for forbidden in (
        "U1234567",
        "DU9876543",
        "tok-secret-1",
        "my-secret-token",
        "/app/secret.py",
        "\n",
        "\r",
        "\t",
        "\x00",
    ):
        assert forbidden not in notice
