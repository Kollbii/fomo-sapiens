from __future__ import annotations

import math
from collections.abc import Coroutine
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, TypeVar

import pytest
from ib_async import Option, OptionComputation, Stock

from src.broker.client import IBKRBrokerClient
from src.broker.contracts import (
    ContractDiscoveryService,
    choose_auto_strike_step,
    extract_spot,
    match_expirations,
    select_chain,
    select_strikes,
)
from src.broker.fetcher import (
    DELAYED_GENERIC_TICKS,
    LIVE_OPRA_GENERIC_TICKS,
    PACING_COOLDOWN_SEC,
    PagedOptionChainFetcher,
    empty_quote_ratio,
    paginate_contracts,
)
from src.broker.parser import SnapshotContext, TickerParser
from src.core.config import IBKRSettings, Settings
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
    assert fake_ib.market_data_types == [3]

    # Switching to fallback cascades 3 -> 4
    assert client.can_fallback()
    assert client.switch_to_fallback_data_type() == 4
    assert client.current_type == 4
    assert not client.can_fallback()

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
    # Candidates inside (30, 60): 35 (Wed Dec 16), 37 (Fri Dec 18), 43 (Thu Dec 24),
    # 50 (Thu Dec 31), 56 (Wed Jan 6), 58 (Fri Jan 8).
    # Boundaries 15, 30, 60, 90 must be excluded by strict dte_min < dte < dte_max.
    market_date = date(2026, 11, 11)
    dtes = [15, 30, 35, 37, 43, 50, 56, 58, 60, 90]
    expirations = [(market_date + timedelta(days=d)).strftime("%Y%m%d") for d in dtes]

    matches = match_expirations(
        expirations,
        market_date=market_date,
        dte_min=30,
        dte_max=60,
    )
    # Within each ISO week, Friday overrides mid-week (37 beats 35, 58 beats 56).
    assert [m.dte for m in matches] == [37, 43, 50, 58]
    assert all(30 < m.dte < 60 for m in matches)
    assert len({m.ib_expiry for m in matches}) == 4


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
    # Boundaries 30 and 60 plus mid-week 35 should drop, leaving 4 weekly expiries (37, 43, 50, 58)
    expirations = [
        (market_date + timedelta(days=d)).strftime("%Y%m%d") for d in (30, 35, 37, 43, 50, 58, 60)
    ]
    strikes = [float(s) for s in range(110, 175, 5)]  # 11 in +-20% -> 4 * 11 * 2 = 88 contracts
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
    assert [m.dte for m in result.plan.expirations] == [37, 43, 50, 58]
    assert result.pages_fetched == 2  # 88 contracts -> 80 + 8
    assert len(result.rows) == 88
    assert len(fake_ib.subscribed) == len(fake_ib.cancelled)
    assert all(r.greeks_source == "NONE" for r in result.rows)
    assert all(r.mark_price == pytest.approx(5.2) for r in result.rows)
