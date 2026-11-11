from __future__ import annotations

import asyncio
import collections
import heapq
import inspect
import math
from collections.abc import Coroutine
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, TypeVar

import pytest
from ib_async import Option, OptionComputation, Stock

from src.broker.client import IBKRBrokerClient
from src.broker.contracts import (
    EXACT_PREFETCH_MAX_CONCURRENCY,
    ContractDiscoveryService,
    choose_auto_strike_step,
    detect_available_range_pcts,
    detect_available_strike_steps,
    detect_native_strike_step,
    extract_spot,
    match_expirations,
    select_chain,
    select_strikes,
)
from src.broker.fetcher import (
    DELAYED_GENERIC_TICKS,
    LIVE_OPRA_GENERIC_TICKS,
    PACING_COOLDOWN_SEC,
    PAGE_PACING_CHUNK_SIZE,
    PAGE_PACING_YIELD_SEC,
    PagedOptionChainFetcher,
    empty_quote_ratio,
    paginate_contracts,
)
from src.broker.parser import SnapshotContext, TickerParser
from src.core.config import IBKRSettings, OptionsChainSettings, Settings
from src.core.exceptions import (
    GatewayConnectionError,
    InvalidTickerError,
    OptionChainFetchError,
    PacingViolationError,
    SecurityViolationError,
    UnderlyingPriceUnavailableError,
)
from src.core.security import ReadOnlyIBGuard


class FakeEvent:
    """Minimal event hook matching `eventkit.Event` `+=` / `emit` semantics."""

    def __init__(self) -> None:
        self._handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> FakeEvent:
        self._handlers.append(handler)
        return self

    def emit(self, *args: Any) -> None:
        for h in list(self._handlers):
            h(*args)


class FakeIB:
    """Controllable mock for `ib_async.IB`."""

    def __init__(self, *, connect_ok: bool = True, connect_exc: Exception | None = None) -> None:
        self._connected = False
        self._connect_ok = connect_ok
        self._connect_exc = connect_exc
        self.errorEvent = FakeEvent()
        self.wrapper = SimpleNamespace(clientId=0)
        self.market_data_types: list[int] = []
        self.subscribed: list[tuple[Any, str, bool, bool]] = []
        self.cancelled: list[Any] = []
        self.qualify_handler: Any = None
        self.contract_details_handler: Any = None
        self.chains_result: list[Any] = []
        self.ticker_factory: Any = None

        async def _connect_async(host: str, port: int, client_id: int, timeout_sec: float) -> None:
            if self._connect_exc is not None:
                raise self._connect_exc
            self._connected = self._connect_ok

        self.client = SimpleNamespace(connectAsync=_connect_async)

    def isConnected(self) -> bool:  # noqa: N802
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def reqMarketDataType(self, data_type: int) -> None:  # noqa: N802
        self.market_data_types.append(data_type)

    async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]:  # noqa: N802
        if self.qualify_handler is not None:
            res: list[Any] = self.qualify_handler(*contracts)
            return res
        out: list[Any] = []
        for idx, c in enumerate(contracts, start=1):
            c.conId = getattr(c, "conId", 0) or (10_000 + idx)
            out.append(c)
        return out

    async def reqSecDefOptParamsAsync(  # noqa: N802
        self, symbol: str, fut_fop_exchange: str, sec_type: str, con_id: int
    ) -> list[Any]:
        return self.chains_result

    async def reqContractDetailsAsync(self, contract: Any) -> list[Any]:  # noqa: N802
        if self.contract_details_handler is None:
            return []
        res = self.contract_details_handler(contract)
        if inspect.isawaitable(res):
            out: list[Any] = await res
            return out
        return list(res)

    def reqMktData(  # noqa: N802
        self,
        contract: Any,
        genericTickList: str = "",  # noqa: N803
        snapshot: bool = False,
        regulatorySnapshot: bool = False,  # noqa: N803
    ) -> Any:
        self.subscribed.append((contract, genericTickList, snapshot, regulatorySnapshot))
        if self.ticker_factory is not None:
            return self.ticker_factory(contract, genericTickList)
        return SimpleNamespace(
            contract=contract,
            bid=4.8,
            ask=5.2,
            last=5.0,
            close=4.9,
            volume=150,
            callOpenInterest=500,
            putOpenInterest=400,
            modelGreeks=None,
        )

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        self.cancelled.append(contract)


T = TypeVar("T")


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """Drive a pure/mocked coroutine to completion without opening OS loopback sockets."""
    try:
        while True:
            coro.send(None)
    except StopIteration as stop:
        val: T = stop.value
        return val


class _VirtualEventLoop:
    """Minimal socketless virtual-clock event loop safe inside restricted sandboxes."""

    def __init__(self) -> None:
        self._now = 0.0
        self._ready: collections.deque[asyncio.Handle] = collections.deque()
        self._scheduled: list[tuple[float, int, asyncio.TimerHandle]] = []
        self._seq = 0
        self._closed = False

    def get_debug(self) -> bool:
        return False

    def is_running(self) -> bool:
        running: Any = asyncio.events._get_running_loop()
        return running is self

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True

    def time(self) -> float:
        return self._now

    def create_future(self) -> asyncio.Future[Any]:
        return asyncio.Future(loop=self)  # type: ignore[arg-type]

    def create_task(
        self,
        coro: Coroutine[Any, Any, T],
        *,
        name: str | None = None,
        context: Any = None,
    ) -> asyncio.Task[T]:
        return asyncio.Task(coro, loop=self, name=name, context=context)  # type: ignore[arg-type]

    def _timer_handle_cancelled(self, handle: asyncio.TimerHandle) -> None:
        return None

    def call_exception_handler(self, context: dict[str, Any]) -> None:
        return None

    def call_soon(self, callback: Any, *args: Any, context: Any = None) -> asyncio.Handle:
        handle = asyncio.Handle(callback, args, self, context)  # type: ignore[arg-type]
        self._ready.append(handle)
        return handle

    def call_soon_threadsafe(
        self, callback: Any, *args: Any, context: Any = None
    ) -> asyncio.Handle:
        return self.call_soon(callback, *args, context=context)

    def call_later(
        self, delay: float, callback: Any, *args: Any, context: Any = None
    ) -> asyncio.TimerHandle:
        return self.call_at(self._now + max(0.0, delay), callback, *args, context=context)

    def call_at(
        self, when: float, callback: Any, *args: Any, context: Any = None
    ) -> asyncio.TimerHandle:
        self._seq += 1
        timer = asyncio.TimerHandle(when, callback, args, self, context)  # type: ignore[arg-type]
        heapq.heappush(self._scheduled, (when, self._seq, timer))
        return timer

    def run_until_complete(self, future: Any) -> Any:
        task = future if isinstance(future, asyncio.Task) else self.create_task(future)
        prev = asyncio.events._get_running_loop()
        asyncio.events._set_running_loop(self)  # type: ignore[arg-type]
        try:
            while not task.done():
                while self._ready:
                    handle = self._ready.popleft()
                    if not handle.cancelled():
                        handle._run()
                if task.done():
                    break
                while self._scheduled and self._scheduled[0][2].cancelled():
                    heapq.heappop(self._scheduled)
                if not self._scheduled:
                    if not self._ready:
                        raise RuntimeError("Virtual event loop stalled")
                    continue
                when, _, timer = heapq.heappop(self._scheduled)
                self._now = max(self._now, when)
                if not timer.cancelled():
                    timer._run()
            return task.result()
        finally:
            asyncio.events._set_running_loop(prev)


def _run_virtual_loop(coro: Coroutine[Any, Any, T]) -> T:
    """Execute an async coroutine with a live `asyncio.get_running_loop()` without sockets."""
    loop = _VirtualEventLoop()
    try:
        res: T = loop.run_until_complete(coro)
        return res
    finally:
        loop.close()


async def _noop_sleep(_seconds: float) -> None:
    return None


# ------------------------------------------------------------------ client tests
def test_client_locks_ib_and_connects_type3_default() -> None:
    fake_ib = FakeIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    assert ReadOnlyIBGuard.verify_locked(client.ib)
    with pytest.raises(SecurityViolationError):
        client.ib.placeOrder("bad")

    _run(client.ensure_connected())
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
    fake_ib = FakeIB(connect_exc=ConnectionRefusedError("refused"))
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    with pytest.raises(GatewayConnectionError):
        _run(client.ensure_connected())
    assert not client.is_connected

    fake_ib2 = FakeIB(connect_ok=False)
    client2 = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib2)
    with pytest.raises(GatewayConnectionError):
        _run(client2.ensure_connected())


def test_client_error_event_absorbs_10089_354_and_flags_pacing() -> None:
    live_settings = Settings(
        ibkr=IBKRSettings(
            market_data_mode="LIVE",
            default_market_data_type=1,
            fallback_market_data_type=2,
        )
    )
    fake_ib = FakeIB()
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


# ------------------------------------------------------------------ DTE & strike math
def test_dte_strict_weekly_matching_and_iso_week_dedup() -> None:
    # 2026-11-11 is Wednesday.
    # Candidates inside [30, 120] inclusive: 30, 35/37, 43, 50, 56/58/60, 90/93, 120.
    # Boundaries 15, 121, 150 must be excluded by dte_min <= dte <= dte_max (30 and 120 included).
    market_date = date(2026, 11, 11)
    dtes = [15, 30, 35, 37, 43, 50, 56, 58, 60, 90, 93, 120, 121, 150]
    expirations = [(market_date + timedelta(days=d)).strftime("%Y%m%d") for d in dtes]

    matches = match_expirations(
        expirations,
        market_date=market_date,
        dte_min=30,
        dte_max=120,
    )
    # Within each ISO week, Friday overrides mid-week (37 beats 35, 58 beats 56/60, 93 beats 90).
    assert [m.dte for m in matches] == [30, 37, 43, 50, 58, 93, 120]
    assert all(30 <= m.dte <= 120 for m in matches)
    assert len({m.ib_expiry for m in matches}) == 7


def test_select_strikes_moneyness_atm_anchor_and_auto_widening() -> None:
    spot = 142.40
    # Dense $1.00 strikes from $50 to $250
    all_strikes = [float(x) for x in range(50, 251)]

    # +-20% around 142.40 -> [113.92, 170.88], grid-aligned $5 multiples anchored on 140.0
    sel = select_strikes(all_strikes, spot=spot, range_pct=0.20, strike_step=5.0)
    assert sel.atm_strike == 140.0
    assert 140.0 in sel.strikes
    assert all(s % 5.0 == 0.0 for s in sel.strikes)
    assert not sel.widened
    assert all(142.40 * 0.80 <= s <= 142.40 * 1.20 for s in sel.strikes)
    diffs = [round(b - a, 6) for a, b in zip(sel.strikes, sel.strikes[1:], strict=False)]
    assert all(d >= 5.0 for d in diffs)

    # Mixed $2.50 short-DTE strikes must NOT trap step=$5.0 onto .50 off-grid strikes (e.g. 347.5)
    aapl_spot = 347.40
    mixed_strikes = [320.0 + 2.5 * i for i in range(25)]  # 320.0 .. 380.0 in $2.50 steps
    aapl_sel = select_strikes(mixed_strikes, spot=aapl_spot, range_pct=0.10, strike_step=5.0)
    assert aapl_sel.atm_strike == 345.0
    assert all(s % 5.0 == 0.0 for s in aapl_sel.strikes)

    # Auto strike step skips non-native $2.5 on integer-only grids and picks $5.0;
    # when .50 increments are present, it picks $2.5.
    assert choose_auto_strike_step(all_strikes, spot=spot, range_pct=0.20) == 5.0
    assert choose_auto_strike_step(mixed_strikes, spot=aapl_spot, range_pct=0.10) == 2.5

    # Force auto-widening when max_strikes cap is exceeded (e.g. 4 expirations -> max 40 strikes)
    wide = select_strikes(
        all_strikes,
        spot=spot,
        range_pct=0.50,
        strike_step=1.0,
        max_strikes=40,
    )
    assert len(wide.strikes) <= 40
    assert wide.atm_strike == 140.0
    assert 140.0 in wide.strikes
    assert wide.widened
    assert wide.effective_step >= 5.0


def test_select_chain_prefers_smart_multiplier_100_largest_grid() -> None:
    chains = [
        SimpleNamespace(
            exchange="CBOE",
            tradingClass="NVDA",
            multiplier="100",
            expirations=["20261218"] * 10,
            strikes=[100.0] * 50,
        ),
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA2",
            multiplier="100",
            expirations=["20261218"],
            strikes=[100.0, 105.0],
        ),
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=["20261211", "20261218", "20261224"],
            strikes=[130.0, 135.0, 140.0, 145.0, 150.0],
        ),
    ]
    chosen = select_chain(chains, "NVDA")
    assert chosen.exchange == "SMART"
    assert chosen.tradingClass == "NVDA"


def test_discovery_spot_fallback_and_errors() -> None:
    fake_ib = FakeIB()
    # First call (Type 3) returns sentinels, second call (Type 4) returns close=140.5
    call_count = 0

    def _spot_ticker(contract: Any, _ticks: str) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SimpleNamespace(
                contract=contract, bid=-1.0, ask=-1.0, last=float("nan"), close=0.0
            )
        return SimpleNamespace(contract=contract, bid=-1.0, ask=-1.0, last=-1.0, close=140.5)

    fake_ib.ticker_factory = _spot_ticker
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, sleep=_noop_sleep)

    stock = _run(svc.qualify_underlying("nvda"))
    spot = _run(svc.resolve_spot(stock))
    assert spot == 140.5
    assert client.current_type == 4
    assert len(fake_ib.cancelled) == 2

    # Unqualified ticker raises InvalidTickerError
    fake_ib.qualify_handler = lambda *_c: [None]
    with pytest.raises(InvalidTickerError):
        _run(svc.qualify_underlying("ZZZZ"))

    # Spot unavailable even after fallback raises UnderlyingPriceUnavailableError
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=-1.0, ask=-1.0, last=-1.0, close=-1.0
    )
    with pytest.raises(UnderlyingPriceUnavailableError):
        _run(svc.resolve_spot(Stock("NVDA", "SMART", "USD", conId=1)))


def test_extract_spot_priority() -> None:
    assert extract_spot(SimpleNamespace(last=150.0, bid=148.0, ask=149.0, close=140.0)) == 150.0
    assert extract_spot(SimpleNamespace(last=-1.0, bid=148.0, ask=150.0, close=140.0)) == 149.0
    t_close = SimpleNamespace(last=float("nan"), bid=-1.0, ask=150.0, close=140.0)
    assert extract_spot(t_close) == 140.0
    assert extract_spot(SimpleNamespace(last=-1.0, bid=-1.0, ask=-1.0, close=0.0)) is None
    with pytest.raises(OptionChainFetchError):
        select_chain([], "NVDA")


# ------------------------------------------------------------------ parser tests
def _sample_context() -> SnapshotContext:
    return SnapshotContext(
        snapshot_id="NVDA_20261111_153000",
        snapshot_ts_utc="2026-11-11T15:30:00Z",
        market_date="2026-11-11",
        market_data_type=3,
        underlying="NVDA",
        underlying_conid=4815,
        underlying_price=140.0,
    )


def test_parser_call_and_put_oi_and_ibkr_model_greeks() -> None:
    ctx = _sample_context()
    call_opt = Option("NVDA", "20261211", 145.0, "C", "SMART", "100", "USD", conId=9001)
    put_opt = Option("NVDA", "20261211", 135.0, "P", "SMART", "100", "USD", conId=9002)

    call_ticker = SimpleNamespace(
        contract=call_opt,
        bid=4.0,
        ask=4.4,
        last=4.1,
        close=3.9,
        volume=250,
        callOpenInterest=1200,
        putOpenInterest=9999,
        modelGreeks=OptionComputation(
            tickAttrib=0,
            impliedVol=0.42,
            delta=0.48,
            optPrice=4.2,
            pvDividend=0.0,
            gamma=0.03,
            vega=0.18,
            theta=-0.09,
            undPrice=140.0,
        ),
    )
    row_c = TickerParser.parse_ticker(call_ticker, context=ctx)
    assert row_c.right == "C"
    assert row_c.open_interest == 1200
    assert row_c.mid == pytest.approx(4.2)
    assert row_c.mark_price == pytest.approx(4.2)
    assert row_c.greeks_source == "IBKR_MODEL"
    assert row_c.iv == pytest.approx(0.42)
    assert row_c.delta == pytest.approx(0.48)
    assert row_c.dte == 30

    put_ticker = SimpleNamespace(
        contract=put_opt,
        bid=-1.0,
        ask=float("nan"),
        last=-1.0,
        close=3.5,
        volume=2_147_483_647,
        callOpenInterest=8888,
        putOpenInterest=750,
        modelGreeks=None,
    )
    row_p = TickerParser.parse_ticker(put_ticker, context=ctx)
    assert row_p.right == "P"
    assert row_p.open_interest == 750
    assert row_p.bid is None and row_p.ask is None and row_p.mid is None and row_p.last is None
    assert row_p.close == 3.5
    assert row_p.mark_price == 3.5
    assert row_p.volume is None
    assert row_p.greeks_source == "NONE"
    assert (row_p.iv, row_p.delta, row_p.gamma, row_p.theta, row_p.vega) == (None,) * 5


@pytest.mark.parametrize(
    "bad_greeks",
    [
        SimpleNamespace(impliedVol=-0.1, delta=0.5, gamma=0.02, theta=-0.1, vega=0.1),
        SimpleNamespace(impliedVol=12.5, delta=0.5, gamma=0.02, theta=-0.1, vega=0.1),
        SimpleNamespace(impliedVol=0.4, delta=1.5, gamma=0.02, theta=-0.1, vega=0.1),
        SimpleNamespace(impliedVol=0.4, delta=0.5, gamma=-0.01, theta=-0.1, vega=0.1),
        SimpleNamespace(impliedVol=math.nan, delta=0.5, gamma=0.02, theta=-0.1, vega=0.1),
    ],
)
def test_parser_rejects_out_of_bounds_model_greeks(bad_greeks: Any) -> None:
    ctx = _sample_context()
    opt = Option("NVDA", "20261211", 140.0, "C", "SMART", "100", "USD", conId=9003)
    tk = SimpleNamespace(
        contract=opt,
        bid=5.0,
        ask=5.2,
        last=5.1,
        close=5.0,
        volume=10,
        callOpenInterest=-1,
        modelGreeks=bad_greeks,
    )
    row = TickerParser.parse_ticker(tk, context=ctx)
    assert row.greeks_source == "NONE"
    assert (row.iv, row.delta, row.gamma, row.theta, row.vega) == (None,) * 5
    assert row.open_interest is None


# ------------------------------------------------------------------ pagination & fetcher tests
def test_paginate_contracts_splits_by_80_and_enforces_400_cap() -> None:
    items = list(range(205))
    pages = paginate_contracts(items, max_per_page=80, max_pages=5)
    assert [len(p) for p in pages] == [80, 80, 45]

    with pytest.raises(PacingViolationError):
        paginate_contracts(list(range(401)), max_per_page=80, max_pages=5)


def test_fetcher_cancels_mkt_data_on_exception() -> None:
    fake_ib = FakeIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, sleep=_noop_sleep)

    contracts = [
        Option("NVDA", "20261211", float(130 + i), "C", "SMART", conId=i + 1) for i in range(5)
    ]

    async def _boom_sleep(_sec: float) -> None:
        raise RuntimeError("simulated poll failure")

    fetcher.sleep = _boom_sleep
    # Tickers have no usable quotes so poll_until invokes _boom_sleep on the first poll
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=-1.0, ask=-1.0, last=-1.0, close=-1.0
    )

    with pytest.raises(RuntimeError, match="simulated poll failure"):
        _run(fetcher._fetch_page_delayed(contracts))

    # Every subscribed contract MUST be cancelled despite the exception
    assert len(fake_ib.subscribed) == 5
    assert fake_ib.cancelled == contracts


def test_fetcher_type3_to_type4_auto_fallback_when_over_80pct_empty() -> None:
    fake_ib = FakeIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, sleep=_noop_sleep)

    contracts = [
        Option("NVDA", "20261211", float(130 + i), "C", "SMART", conId=i + 1) for i in range(10)
    ]

    def _factory(contract: Any, _ticks: str) -> Any:
        # Under Type 3: 9 of 10 contracts are empty (90% > 80%)
        if client.current_type == 3:
            if contract.conId == 1:
                return SimpleNamespace(contract=contract, bid=5.0, ask=5.2, last=5.1, close=5.0)
            return SimpleNamespace(contract=contract, bid=-1.0, ask=-1.0, last=-1.0, close=-1.0)
        # Under Type 4 (Delayed-Frozen): closing prices populate
        return SimpleNamespace(contract=contract, bid=-1.0, ask=-1.0, last=-1.0, close=4.75)

    fake_ib.ticker_factory = _factory
    tickers = _run(fetcher._fetch_page_delayed(contracts))

    assert client.current_type == 4
    assert fake_ib.market_data_types == [4]
    assert empty_quote_ratio(tickers) == 0.0
    # 10 subscribed & cancelled on pass 1 (Type 3) + 10 subscribed & cancelled on pass 2 (Type 4)
    assert len(fake_ib.subscribed) == 20
    assert len(fake_ib.cancelled) == 20
    assert all(t[1] == DELAYED_GENERIC_TICKS for t in fake_ib.subscribed)


def test_fetcher_live_opra_hook_and_pacing_cooldown() -> None:
    live_settings = Settings(
        ibkr=IBKRSettings(
            market_data_mode="LIVE",
            default_market_data_type=1,
            fallback_market_data_type=2,
        )
    )
    fake_ib = FakeIB()
    client = IBKRBrokerClient(live_settings, ib_factory=lambda: fake_ib)
    sleeps: list[float] = []

    async def _record_sleep(sec: float) -> None:
        sleeps.append(sec)

    fetcher = PagedOptionChainFetcher(client, live_settings, sleep=_record_sleep)

    def _live_factory(contract: Any, _ticks: str) -> Any:
        # Trigger pacing warning 100 during page 1
        if contract.conId == 1:
            fake_ib.errorEvent.emit(-1, 100, "Max rate of messages exceeded", None)
        return SimpleNamespace(
            contract=contract,
            bid=6.0,
            ask=6.2,
            last=6.1,
            close=6.0,
            volume=50,
            callOpenInterest=300,
            putOpenInterest=300,
            modelGreeks=OptionComputation(
                tickAttrib=0, impliedVol=0.35, delta=0.5, gamma=0.04, vega=0.2, theta=-0.1
            ),
        )

    fake_ib.ticker_factory = _live_factory
    contracts = [
        Option("NVDA", "20261211", float(100 + i), "C", "SMART", conId=i + 1) for i in range(85)
    ]
    tickers, pages_fetched = _run(fetcher.fetch_pages(contracts))

    assert pages_fetched == 2
    assert len(tickers) == 85
    assert all(t[1] == LIVE_OPRA_GENERIC_TICKS for t in fake_ib.subscribed)
    assert fetcher.active_cooldown_sec == PACING_COOLDOWN_SEC
    assert PACING_COOLDOWN_SEC in sleeps


def test_end_to_end_fetch_option_chain_multi_page() -> None:
    fake_ib = FakeIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    market_date = date(2026, 11, 11)
    # 30 and 120 DTE are retained, 125 drops, 60 shares ISO week with Fri 58 -> 7 weekly expiries
    expirations = [
        (market_date + timedelta(days=d)).strftime("%Y%m%d")
        for d in (30, 35, 37, 43, 50, 58, 60, 93, 120, 125)
    ]
    strikes = [float(s) for s in range(110, 175, 5)]  # 11 in +-20% -> 7 * 11 * 2 = 154 contracts
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=expirations,
            strikes=strikes,
        )
    ]

    def _ticker_factory(contract: Any, _ticks: str) -> Any:
        if getattr(contract, "secType", "") == "STK":
            return SimpleNamespace(contract=contract, bid=139.9, ask=140.1, last=140.0, close=139.5)
        return SimpleNamespace(
            contract=contract,
            bid=5.0,
            ask=5.4,
            last=5.2,
            close=5.1,
            volume=80,
            callOpenInterest=-1,
            putOpenInterest=-1,
            modelGreeks=None,
        )

    fake_ib.ticker_factory = _ticker_factory
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, clock=lambda: now, sleep=_noop_sleep)

    result = _run(fetcher.fetch_option_chain("NVDA", range_pct=0.20, strike_step=5.0))
    assert result.snapshot_id == "NVDA_20261111_153000"
    assert [m.dte for m in result.plan.expirations] == [30, 37, 43, 50, 58, 93, 120]
    assert result.pages_fetched == 2  # 154 contracts -> 80 + 74
    assert len(result.rows) == 154
    assert len(fake_ib.subscribed) == len(fake_ib.cancelled)
    assert all(r.greeks_source == "NONE" for r in result.rows)
    assert all(r.mark_price == pytest.approx(5.2) for r in result.rows)


def test_per_expiration_intersection_prevents_phantom_strikes() -> None:
    """Monthly/weekly SMART chains with distinct strike grids must not cross-pollinate."""
    fake_ib = FakeIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    market_date = date(2026, 11, 11)
    exp_weekly = (market_date + timedelta(days=37)).strftime("%Y%m%d")  # Friday weekly ($2.5)
    exp_monthly = (market_date + timedelta(days=43)).strftime("%Y%m%d")  # Next week ($5 only)
    weekly_strikes = [120.0 + 2.5 * i for i in range(17)]  # 120.0..160.0 in $2.5 steps
    monthly_strikes = [float(s) for s in range(120, 165, 5)]  # $5 steps only (no 137.5, 142.5)
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=[exp_weekly],
            strikes=weekly_strikes,
        ),
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=[exp_monthly],
            strikes=monthly_strikes,
        ),
    ]
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=139.9, ask=140.1, last=140.0, close=139.8
    )
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, clock=lambda: now, sleep=_noop_sleep)

    plan = _run(svc.build_plan("NVDA", range_pct=0.20, strike_step=2.5))
    spec_by_exp = {spec.match.ib_expiry: spec for spec in plan.expiration_specs}
    assert set(spec_by_exp) == {exp_weekly, exp_monthly}
    assert 137.5 in spec_by_exp[exp_weekly].strikes
    assert 137.5 not in spec_by_exp[exp_monthly].strikes
    assert 142.5 not in spec_by_exp[exp_monthly].strikes
    assert spec_by_exp[exp_monthly].effective_step == pytest.approx(5.0)

    fetcher = PagedOptionChainFetcher(client, sleep=_noop_sleep)
    built = fetcher.build_candidate_contracts(plan)
    monthly_built_strikes = {
        float(c.strike) for c in built if c.lastTradeDateOrContractMonth == exp_monthly
    }
    assert 137.5 not in monthly_built_strikes
    assert 142.5 not in monthly_built_strikes


def test_non_standard_10_dollar_step_adapts_with_notice() -> None:
    fake_ib = FakeIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    market_date = date(2026, 11, 11)
    exps = [(market_date + timedelta(days=d)).strftime("%Y%m%d") for d in (37, 43)]
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=exps,
            strikes=[float(s) for s in range(100, 190, 10)],
        )
    ]
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=139.9, ask=140.1, last=140.0, close=139.8
    )
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, clock=lambda: now, sleep=_noop_sleep)
    plan = _run(svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert plan.exchange_native_verified is True
    assert plan.exchange_validated_strike_count > 0
    assert plan.strikes.effective_step == pytest.approx(10.0)
    assert 10.0 in plan.available_strike_steps
    assert any("adapted" in n.lower() or "$10" in n for n in plan.notices)
    assert any("exchange" in n.lower() or "validated" in n.lower() for n in plan.notices)


def test_detect_available_strike_steps_and_range_pcts() -> None:
    ten_dollar_chain = [110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0]
    assert detect_native_strike_step(ten_dollar_chain) == pytest.approx(10.0)
    avail_steps = detect_available_strike_steps(
        ten_dollar_chain,
        spot=140.0,
        range_pct=0.20,
        candidate_steps=[1.0, 2.5, 5.0, 10.0],
    )
    assert avail_steps == [10.0]

    narrow_chain = [130.0, 140.0, 150.0]
    avail_ranges = detect_available_range_pcts(
        narrow_chain,
        spot=140.0,
        candidate_ranges=[0.05, 0.10, 0.20, 0.30],
    )
    assert avail_ranges == [0.10, 0.20, 0.30]


def test_qualify_option_contracts_emits_notice_on_partial_reject() -> None:
    fake_ib = FakeIB()
    market_date = date(2026, 11, 11)
    exps = [(market_date + timedelta(days=37)).strftime("%Y%m%d")]
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=exps,
            strikes=[135.0, 140.0, 145.0],
        )
    ]

    def _partial_qualify(*contracts: Any) -> list[Any]:
        # Simulate underlying qualification succeeding cleanly, but rejecting 1 option contract.
        if len(contracts) == 1 and getattr(contracts[0], "secType", "") == "STK":
            contracts[0].conId = 4815
            return [contracts[0]]
        kept: list[Any] = []
        for idx, c in enumerate(contracts):
            if idx == 0:
                c.conId = 0  # dropped by IBKR Error 200
                continue
            c.conId = 9000 + idx
            kept.append(c)
        return kept

    fake_ib.qualify_handler = _partial_qualify
    fake_ib.ticker_factory = lambda c, _t: (
        SimpleNamespace(contract=c, bid=139.9, ask=140.1, last=140.0, close=139.8)
        if getattr(c, "secType", "") == "STK"
        else SimpleNamespace(
            contract=c,
            bid=5.0,
            ask=5.4,
            last=5.2,
            close=5.1,
            volume=20,
            callOpenInterest=100,
            putOpenInterest=100,
            modelGreeks=None,
        )
    )
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(
        client,
        clock=lambda: datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC),
        sleep=_noop_sleep,
    )
    res = _run(fetcher.fetch_option_chain("NVDA", range_pct=0.20, strike_step=5.0))
    assert len(res.rows) == 5  # 6 candidates minus 1 rejected
    assert any("filtered out 1 unqualified" in n.lower() for n in res.notices)


def _make_cd_list(
    symbol: str,
    ib_expiry: str,
    strikes: list[float],
    *,
    conid_start: int,
    trading_class: str = "NVDA",
    asymmetric_call_only_strike: float | None = None,
) -> list[Any]:
    details: list[Any] = []
    cid = conid_start
    for strike in strikes:
        rights = ("C",) if strike == asymmetric_call_only_strike else ("C", "P")
        for right in rights:
            cid += 1
            opt = Option(
                symbol=symbol,
                lastTradeDateOrContractMonth=ib_expiry,
                strike=strike,
                right=right,
                exchange="SMART",
                multiplier="100",
                currency="USD",
                tradingClass=trading_class,
                conId=cid,
                localSymbol=f"{symbol} {ib_expiry}{right}{int(strike * 1000):08d}",
            )
            details.append(SimpleNamespace(contract=opt))
    return details


def test_prefetch_exact_per_expiration_strikes_eliminates_phantom_strikes_and_qualify_calls() -> (
    None
):
    """Single-tradingClass pooled SMART chain is narrowed per expiry via reqContractDetailsAsync."""
    fake_ib = FakeIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    market_date = date(2026, 11, 11)
    exp_weekly = (market_date + timedelta(days=30)).strftime("%Y%m%d")  # Friday weekly ($2.5)
    exp_monthly = "20261218"  # Monthly OPEX (37 DTE): $5 strikes only + 1 asymmetric outer call

    weekly_strikes = [165.0 + 2.5 * i for i in range(13)]  # 165.0..195.0 including .5
    monthly_strikes = [165.0, 170.0, 175.0, 180.0, 185.0, 190.0, 195.0, 200.0]
    pooled_strikes = sorted(set(weekly_strikes) | set(monthly_strikes))

    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=[exp_weekly, exp_monthly],
            strikes=pooled_strikes,
        )
    ]

    details_queries: list[str] = []

    def _cd_handler(contract: Any) -> list[Any]:
        exp = str(contract.lastTradeDateOrContractMonth)
        details_queries.append(exp)
        assert contract.strike == 0.0
        assert contract.right == ""
        if exp == exp_weekly:
            return _make_cd_list("NVDA", exp, weekly_strikes, conid_start=50_000)
        return _make_cd_list(
            "NVDA",
            exp,
            monthly_strikes,
            conid_start=60_000,
            asymmetric_call_only_strike=200.0,
        )

    fake_ib.contract_details_handler = _cd_handler

    qualify_batches: list[int] = []

    def _track_qualify(*contracts: Any) -> list[Any]:
        qualify_batches.append(len(contracts))
        for idx, c in enumerate(contracts, start=1):
            c.conId = getattr(c, "conId", 0) or (4800 + idx)
        return list(contracts)

    fake_ib.qualify_handler = _track_qualify
    fake_ib.ticker_factory = lambda c, _t: (
        SimpleNamespace(contract=c, bid=179.9, ask=180.1, last=180.0, close=179.8)
        if getattr(c, "secType", "") == "STK"
        else SimpleNamespace(
            contract=c,
            bid=6.0,
            ask=6.4,
            last=6.2,
            close=6.1,
            volume=40,
            callOpenInterest=200,
            putOpenInterest=200,
            modelGreeks=None,
        )
    )

    sleeps: list[float] = []

    async def _rec_sleep(sec: float) -> None:
        sleeps.append(sec)

    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, clock=lambda: now, sleep=_rec_sleep)

    res = _run(fetcher.fetch_option_chain("NVDA", range_pct=0.20, strike_step=None))

    assert details_queries == [exp_weekly, exp_monthly]
    # Cooldown slept strictly between the 2 sequential lookups (not on 1st or after 2nd)
    assert 0.10 in sleeps
    # Only the underlying Stock was sent to qualifyContractsAsync (zero Option batches!)
    assert qualify_batches == [1]

    spec_by_exp = {spec.match.ib_expiry: spec for spec in res.plan.expiration_specs}
    assert spec_by_exp[exp_weekly].exact_strikes_verified is True
    assert spec_by_exp[exp_monthly].exact_strikes_verified is True
    assert spec_by_exp[exp_weekly].effective_step == pytest.approx(2.5)
    assert spec_by_exp[exp_monthly].effective_step == pytest.approx(5.0)
    assert 172.5 in spec_by_exp[exp_weekly].strikes
    for half in (172.5, 177.5, 182.5, 187.5):
        assert half not in spec_by_exp[exp_monthly].strikes

    monthly_rows = [r for r in res.rows if r.expiry == "2026-12-18"]
    # Strike 200.0 only has Call in exact ContractDetails, so Put was never requested
    rights_at_200 = {r.right for r in monthly_rows if r.strike == 200.0}
    assert rights_at_200 == {"C"}
    assert any("reqcontractdetails" in n.lower() for n in res.notices)


def test_exact_strike_prefetch_ttl_cache_and_expiration() -> None:
    fake_ib = FakeIB()
    clock_state = [datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)]
    exp = (clock_state[0].date() + timedelta(days=37)).strftime("%Y%m%d")
    strikes = [135.0, 140.0, 145.0]
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=[exp],
            strikes=strikes,
        )
    ]
    calls: list[str] = []

    def _cd_handler(c: Any) -> list[Any]:
        calls.append(str(c.lastTradeDateOrContractMonth))
        return _make_cd_list("NVDA", exp, strikes, conid_start=70_000)

    fake_ib.contract_details_handler = _cd_handler
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=139.9, ask=140.1, last=140.0, close=139.8
    )

    settings = Settings(
        options_chain=OptionsChainSettings(
            contract_details_cache_ttl_sec=300.0,
            contract_details_cooldown_sec=0.05,
        )
    )
    client = IBKRBrokerClient(settings, ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(
        client, settings, clock=lambda: clock_state[0], sleep=_noop_sleep
    )

    _run(svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert len(calls) == 1

    # Inside TTL: changing range_pct reuses exact contracts cache without hitting IBKR again
    clock_state[0] = clock_state[0] + timedelta(seconds=120)
    _run(svc.build_plan("NVDA", range_pct=0.10, strike_step=5.0))
    assert len(calls) == 1

    # Past TTL (301s > 300s): forces a fresh reqContractDetailsAsync query
    clock_state[0] = clock_state[0] + timedelta(seconds=301)
    _run(svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert len(calls) == 2


def test_exact_strike_prefetch_falls_back_on_timeout_and_disabled_flag() -> None:
    fake_ib = FakeIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    exp = (now.date() + timedelta(days=37)).strftime("%Y%m%d")
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=[exp],
            strikes=[135.0, 140.0, 145.0],
        )
    ]
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=139.9, ask=140.1, last=140.0, close=139.8
    )

    # Case 1: TimeoutError from reqContractDetailsAsync falls back cleanly to reqSecDefOptParams
    async def _timeout_handler(_c: Any) -> list[Any]:
        raise TimeoutError("reqContractDetails timed out")

    fake_ib.contract_details_handler = _timeout_handler
    timeout_settings = Settings(
        options_chain=OptionsChainSettings(
            prefetch_exact_strikes=True,
            contract_details_timeout_sec=0.02,
        )
    )
    client = IBKRBrokerClient(timeout_settings, ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, timeout_settings, clock=lambda: now, sleep=_noop_sleep)
    plan = _run(svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert plan.expiration_specs[0].exact_strikes_verified is False
    assert plan.strikes.strikes == (135.0, 140.0, 145.0)
    assert any("reqsecdefoptparams" in n.lower() for n in plan.notices)

    # Case 2: disabled flag never invokes reqContractDetailsAsync
    called = [False]

    def _disabled_handler(_c: Any) -> list[Any]:
        called[0] = True
        return []

    fake_ib.contract_details_handler = _disabled_handler
    disabled_settings = Settings(options_chain=OptionsChainSettings(prefetch_exact_strikes=False))
    client2 = IBKRBrokerClient(disabled_settings, ib_factory=lambda: fake_ib)
    svc2 = ContractDiscoveryService(
        client2, disabled_settings, clock=lambda: now, sleep=_noop_sleep
    )
    plan2 = _run(svc2.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert called[0] is False
    assert plan2.expiration_specs[0].exact_strikes_verified is False


def test_prefetch_exact_contracts_runs_bounded_concurrency_in_event_loop() -> None:
    fake_ib = FakeIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    market_date = date(2026, 11, 11)
    dtes = [30, 37, 44, 51, 58, 65]
    exps = [(market_date + timedelta(days=d)).strftime("%Y%m%d") for d in dtes]
    strikes = [135.0, 140.0, 145.0]
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=exps,
            strikes=strikes,
        )
    ]

    active = 0
    max_active = 0

    async def _cd_handler(contract: Any) -> list[Any]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.015)
            return _make_cd_list(
                "NVDA", str(contract.lastTradeDateOrContractMonth), strikes, conid_start=80_000
            )
        finally:
            active -= 1

    fake_ib.contract_details_handler = _cd_handler
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=139.9, ask=140.1, last=140.0, close=139.8
    )
    settings = Settings(
        options_chain=OptionsChainSettings(
            prefetch_exact_strikes=True,
            contract_details_cooldown_sec=0.0,
        )
    )
    client = IBKRBrokerClient(settings, ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, settings, clock=lambda: now)

    plan = _run_virtual_loop(svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert 2 <= max_active <= EXACT_PREFETCH_MAX_CONCURRENCY
    assert [spec.match.ib_expiry for spec in plan.expiration_specs] == exps
    assert all(spec.exact_strikes_verified for spec in plan.expiration_specs)


def test_fetcher_burst_pacing_yields_and_mid_batch_disconnect_raises_gateway_error() -> None:
    fake_ib = FakeIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    _run(client.ensure_connected())
    sleeps: list[float] = []

    async def _rec_sleep(sec: float) -> None:
        sleeps.append(sec)

    fetcher = PagedOptionChainFetcher(client, sleep=_rec_sleep)
    contracts = [
        Option("NVDA", "20261211", float(100 + i), "C", "SMART", conId=i + 1)
        for i in range(PAGE_PACING_CHUNK_SIZE * 2 + 5)
    ]
    tickers, pages = _run(fetcher.fetch_pages(contracts))
    assert pages == 1
    assert len(tickers) == len(contracts)
    # 2 chunks during subscribe + 2 chunks during cancel = 4 pacing yields
    assert sleeps.count(PAGE_PACING_YIELD_SEC) == 4
    assert len(fake_ib.cancelled) == len(contracts)

    # Now simulate a mid-batch socket drop inside reqMktData / poll loop -> GatewayConnectionError
    fake_ib2 = FakeIB()
    client2 = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib2)
    _run(client2.ensure_connected())
    fake_ib2.errorEvent.emit(-1, 1100, "Connectivity between IB and Trader Workstation lost", None)

    def _drop_factory(contract: Any, _ticks: str) -> Any:
        if contract.conId == 3:
            fake_ib2._connected = False
            raise ConnectionResetError("connection lost mid-page")
        return SimpleNamespace(contract=contract, bid=5.0, ask=5.2, last=5.1, close=5.0)

    fake_ib2.ticker_factory = _drop_factory
    fetcher2 = PagedOptionChainFetcher(client2, sleep=_noop_sleep)
    with pytest.raises(GatewayConnectionError, match="market-data collection.*IBKR 1100"):
        _run(fetcher2._fetch_page_delayed(contracts[:5]))
    assert not client2.is_connected
    # Already-subscribed contracts prior to socket failure are still cancelled cleanly
    assert fake_ib2.cancelled == contracts[:2]
