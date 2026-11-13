"""PagedOptionChainFetcher: <= 80/page pagination, Type 3 -> 4 fallback, pacing and cleanup."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from ib_async import Option, OptionComputation

from src.broker.client import IBKRBrokerClient
from src.broker.fetcher import (
    DELAYED_GENERIC_TICKS,
    LIVE_OPRA_GENERIC_TICKS,
    PACING_COOLDOWN_SEC,
    PAGE_PACING_CHUNK_SIZE,
    PAGE_PACING_PAUSE_SEC,
    ExactContractSpec,
    PagedOptionChainFetcher,
    empty_quote_ratio,
    empty_real_quote_ratio,
    has_real_quote,
    paginate_contracts,
)
from src.core.config import IBKRSettings, Settings
from src.core.exceptions import (
    GatewayConnectionError,
    InvalidTickerError,
    OptionChainFetchError,
    PacingViolationError,
)
from tests.support.aio import noop_sleep, run_coro
from tests.support.ib_fakes import FakeChainIB


def test_paginate_contracts_splits_by_80_and_enforces_400_cap() -> None:
    items = list(range(205))
    pages = paginate_contracts(items, max_per_page=80, max_pages=5)
    assert [len(p) for p in pages] == [80, 80, 45]

    with pytest.raises(PacingViolationError):
        paginate_contracts(list(range(401)), max_per_page=80, max_pages=5)


def test_fetcher_cancels_mkt_data_on_exception() -> None:
    fake_ib = FakeChainIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, sleep=noop_sleep)

    contracts = [
        Option("NVDA", "20261211", float(130 + i), "C", "SMART", conId=i + 1) for i in range(5)
    ]

    async def _boom_sleep(sec: float) -> None:
        if sec == PAGE_PACING_PAUSE_SEC:
            return
        raise RuntimeError("simulated poll failure")

    fetcher.sleep = _boom_sleep
    # Tickers have no usable quotes so poll_until invokes _boom_sleep on the first poll
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=-1.0, ask=-1.0, last=-1.0, close=-1.0
    )

    with pytest.raises(RuntimeError, match="simulated poll failure"):
        run_coro(fetcher._fetch_page_delayed(contracts))

    # Every subscribed contract MUST be cancelled despite the exception
    assert len(fake_ib.subscribed) == 5
    assert fake_ib.cancelled == contracts


def test_fetcher_type3_to_type4_auto_fallback_when_over_80pct_empty() -> None:
    fake_ib = FakeChainIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, sleep=noop_sleep)

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
    tickers = run_coro(fetcher._fetch_page_delayed(contracts))

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
    fake_ib = FakeChainIB()
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
    tickers, pages_fetched = run_coro(fetcher.fetch_pages(contracts))

    assert pages_fetched == 2
    assert len(tickers) == 85
    assert all(t[1] == LIVE_OPRA_GENERIC_TICKS for t in fake_ib.subscribed)
    assert fetcher.active_cooldown_sec == PACING_COOLDOWN_SEC
    assert PACING_COOLDOWN_SEC in sleeps


def test_end_to_end_fetch_option_chain_multi_page() -> None:
    fake_ib = FakeChainIB()
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
    fetcher = PagedOptionChainFetcher(client, clock=lambda: now, sleep=noop_sleep)

    result = run_coro(fetcher.fetch_option_chain("NVDA", range_pct=0.20, strike_step=5.0))
    assert result.snapshot_id == "NVDA_20261111_153000"
    assert [m.dte for m in result.plan.expirations] == [30, 37, 43, 50, 58, 93, 120]
    assert result.pages_fetched == 2  # 154 contracts -> 80 + 74
    assert len(result.rows) == 154
    assert len(fake_ib.subscribed) == len(fake_ib.cancelled)
    assert all(r.greeks_source == "NONE" for r in result.rows)
    assert all(r.mark_price == pytest.approx(5.2) for r in result.rows)


def test_qualify_option_contracts_emits_notice_on_partial_reject() -> None:
    fake_ib = FakeChainIB()
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
        sleep=noop_sleep,
    )
    res = run_coro(fetcher.fetch_option_chain("NVDA", range_pct=0.20, strike_step=5.0))
    assert len(res.rows) == 5  # 6 candidates minus 1 rejected
    assert any("filtered out 1 unqualified" in n.lower() for n in res.notices)


def test_fetcher_burst_pacing_yields_and_mid_batch_disconnect_raises_gateway_error() -> None:
    fake_ib = FakeChainIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    run_coro(client.ensure_connected())
    sleeps: list[float] = []

    async def _rec_sleep(sec: float) -> None:
        sleeps.append(sec)

    fetcher = PagedOptionChainFetcher(client, sleep=_rec_sleep)
    contracts = [
        Option("NVDA", "20261211", float(100 + i), "C", "SMART", conId=i + 1)
        for i in range(PAGE_PACING_CHUNK_SIZE * 2 + 5)
    ]
    tickers, pages = run_coro(fetcher.fetch_pages(contracts))
    assert pages == 1
    assert len(tickers) == len(contracts)
    # 3 chunks during subscribe (idx 0, 20, 40) + 3 chunks during cancel = 6 pacing pauses
    assert sleeps.count(PAGE_PACING_PAUSE_SEC) == 6
    assert len(fake_ib.cancelled) == len(contracts)

    # Now simulate a mid-batch socket drop inside reqMktData / poll loop -> GatewayConnectionError
    fake_ib2 = FakeChainIB()
    client2 = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib2)
    run_coro(client2.ensure_connected())
    fake_ib2.errorEvent.emit(-1, 1100, "Connectivity between IB and Trader Workstation lost", None)

    def _drop_factory(contract: Any, _ticks: str) -> Any:
        if contract.conId == 3:
            fake_ib2._connected = False
            raise ConnectionResetError("connection lost mid-page")
        return SimpleNamespace(contract=contract, bid=5.0, ask=5.2, last=5.1, close=5.0)

    fake_ib2.ticker_factory = _drop_factory
    fetcher2 = PagedOptionChainFetcher(client2, sleep=noop_sleep)
    with pytest.raises(GatewayConnectionError, match="market-data collection.*IBKR 1100"):
        run_coro(fetcher2._fetch_page_delayed(contracts[:5]))
    assert not client2.is_connected
    # Already-subscribed contracts prior to socket failure are still cancelled cleanly
    assert fake_ib2.cancelled == contracts[:2]


def test_cancel_paced_cancels_all_contracts_on_cancelled_error() -> None:
    fake_ib = FakeChainIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    run_coro(client.ensure_connected())

    calls = 0

    async def _cancel_sleep(_sec: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError()

    fetcher = PagedOptionChainFetcher(client, sleep=_cancel_sleep)
    contracts = [
        Option("NVDA", "20261211", float(100 + i), "C", "SMART", conId=i + 1)
        for i in range(PAGE_PACING_CHUNK_SIZE + 5)
    ]
    with pytest.raises(asyncio.CancelledError):
        run_coro(fetcher._cancel_paced(contracts))
    assert len(fake_ib.cancelled) == len(contracts)


def test_fetch_exact_contracts_quotes_dedupes_and_skips_expired() -> None:
    fake_ib = FakeChainIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    live_exp = "2026-12-18"
    live_ib_exp = "20261218"
    expired_exp = "2026-11-06"

    def _qualify(*contracts: Any) -> list[Any]:
        out: list[Any] = []
        for idx, c in enumerate(contracts, start=1):
            if c.secType == "STK":
                c.conId = 4815
            elif int(getattr(c, "conId", 0) or 0) <= 0:
                c.conId = 9000 + idx
            out.append(c)
        return out

    fake_ib.qualify_handler = _qualify
    fake_ib.ticker_factory = lambda c, _ticks: (
        SimpleNamespace(contract=c, bid=139.9, ask=140.1, last=140.0, close=139.5)
        if c.secType == "STK"
        else SimpleNamespace(
            contract=c,
            bid=2.40,
            ask=2.60,
            last=2.50,
            close=2.30,
            volume=50,
            callOpenInterest=200,
            putOpenInterest=200,
            modelGreeks=OptionComputation(
                tickAttrib=0, impliedVol=0.45, delta=0.32, gamma=0.02, theta=-0.05, vega=0.14
            ),
        )
    )

    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, clock=lambda: now, sleep=noop_sleep)

    specs = [
        ExactContractSpec(option_conid=0, expiry=live_exp, strike=130.0, right="P"),
        # Duplicate leg with known conId should win deduplication so only 1 option is queried
        ExactContractSpec(option_conid=7701, expiry=live_exp, strike=130.0, right="P"),
        ExactContractSpec(option_conid=7702, expiry=live_exp, strike=125.0, right="P"),
        ExactContractSpec(option_conid=7703, expiry=expired_exp, strike=120.0, right="P"),
    ]
    res = run_coro(fetcher.fetch_exact_contracts("NVDA", specs, underlying_conid=4815))
    assert res.underlying_price == pytest.approx(140.0)
    assert res.spot_source == "LAST"
    assert len(res.rows) == 2
    assert {r.option_conid for r in res.rows} == {7701, 7702}
    assert {r.expiry for r in res.rows} == {live_exp}
    assert len(res.missing) == 1
    assert res.missing[0].expiry == expired_exp
    assert all(
        c.lastTradeDateOrContractMonth == live_ib_exp
        for c in fake_ib.cancelled
        if c.secType == "OPT"
    )

    with pytest.raises(InvalidTickerError):
        run_coro(fetcher.fetch_exact_contracts("../BAD", specs))
    with pytest.raises(OptionChainFetchError, match="No contracts"):
        run_coro(fetcher.fetch_exact_contracts("NVDA", []))
    with pytest.raises(OptionChainFetchError, match="expired"):
        run_coro(
            fetcher.fetch_exact_contracts(
                "NVDA",
                [ExactContractSpec(option_conid=7703, expiry=expired_exp, strike=120.0, right="P")],
                4815,
            )
        )
    too_many = [
        ExactContractSpec(option_conid=1000 + i, expiry=live_exp, strike=100.0 + i, right="C")
        for i in range(401)
    ]
    with pytest.raises(PacingViolationError):
        run_coro(fetcher.fetch_exact_contracts("NVDA", too_many, underlying_conid=4815))

    # Real-quote helper sanity (recognizes delayedBid/delayedAsk too)
    assert has_real_quote(SimpleNamespace(delayedBid=1.2, delayedAsk=None, close=1.0))
    assert not has_real_quote(SimpleNamespace(bid=None, ask=None, last=1.0, close=1.0))
    assert (
        empty_real_quote_ratio([SimpleNamespace(close=1.0), SimpleNamespace(bid=1.0, ask=1.2)])
        == 0.5
    )
