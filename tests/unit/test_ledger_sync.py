"""Unit tests for opt-in read-only Gateway ledger sync and bounded mark refresh."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from ib_async import CommissionReport, Contract, Execution, ExecutionFilter, Fill, Position

from src.broker.client import IBKRBrokerClient
from src.broker.ledger_sync import fetch_gateway_ledger, refresh_open_option_marks
from src.core.config import IBKRSettings, LedgerSettings, Settings
from src.core.exceptions import ConfigurationError, PacingViolationError
from src.storage.ledger_schema import LedgerPositionRow
from tests.support.aio import noop_sleep, run_coro
from tests.support.ib_fakes import FakeEvent


class _FakeLedgerIB:
    """Deterministic `ib_async.IB` mock for executions, positions, and mark-price ticks."""

    def __init__(self) -> None:
        self._connected = False
        self.errorEvent = FakeEvent()
        self.wrapper = SimpleNamespace(clientId=0)
        self.market_data_types: list[int] = []
        self.exec_filters: list[Any] = []
        self.positions_calls = 0
        self.fills_opt: list[Any] = []
        self.fills_stk: list[Any] = []
        self.fills_other: list[Any] = []
        self.positions_result: list[Any] = []
        self.subscribed: list[tuple[Any, str, bool, bool]] = []
        self.cancelled: list[Any] = []
        self.ticker_factory: Any = None

        async def _connect_async(host: str, port: int, client_id: int, timeout_sec: float) -> None:
            self._connected = True

        self.client = SimpleNamespace(connectAsync=_connect_async)

    def isConnected(self) -> bool:  # noqa: N802
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def reqMarketDataType(self, data_type: int) -> None:  # noqa: N802
        self.market_data_types.append(data_type)

    async def reqExecutionsAsync(self, execFilter: Any = None) -> list[Any]:  # noqa: N802, N803
        self.exec_filters.append(execFilter)
        sec_type = getattr(execFilter, "secType", "") if execFilter is not None else ""
        if sec_type == "OPT":
            return [*self.fills_opt, *self.fills_other]
        if sec_type == "STK":
            return list(self.fills_stk)
        return [*self.fills_opt, *self.fills_stk, *self.fills_other]

    async def reqPositionsAsync(self) -> list[Any]:  # noqa: N802
        self.positions_calls += 1
        return list(self.positions_result)

    def reqMktData(  # noqa: N802
        self,
        contract: Any,
        genericTickList: str = "",  # noqa: N803
        snapshot: bool = False,
        regulatorySnapshot: bool = False,  # noqa: N803
    ) -> Any:
        self.subscribed.append((contract, genericTickList, snapshot, regulatorySnapshot))
        if self.ticker_factory is not None:
            return self.ticker_factory(contract, self.market_data_types)
        return SimpleNamespace(
            contract=contract,
            bid=4.8,
            ask=5.0,
            last=4.9,
            close=4.85,
            marketDataType=self.market_data_types[-1] if self.market_data_types else 3,
        )

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        self.cancelled.append(contract)


def _settings(*, enabled: bool = True, mode: str = "DELAYED") -> Settings:
    if mode == "LIVE":
        ibkr = IBKRSettings(
            market_data_mode="LIVE",
            default_market_data_type=1,
            fallback_market_data_type=2,
        )
    else:
        ibkr = IBKRSettings(
            market_data_mode="DELAYED",
            default_market_data_type=3,
            fallback_market_data_type=4,
        )
    return Settings(ibkr=ibkr, ledger=LedgerSettings(gateway_sync_enabled=enabled))


def test_fetch_gateway_ledger_requires_opt_in() -> None:
    fake_ib = _FakeLedgerIB()
    broker = IBKRBrokerClient(_settings(enabled=False), ib_factory=lambda: fake_ib)
    with pytest.raises(ConfigurationError, match="ENABLE_GATEWAY_LEDGER_SYNC"):
        run_coro(fetch_gateway_ledger(broker))
    assert not broker.is_connected
    assert fake_ib.exec_filters == []
    assert fake_ib.positions_calls == 0


def test_fetch_gateway_ledger_filters_opt_stk_and_strips_account() -> None:
    fake_ib = _FakeLedgerIB()
    ts = datetime(2026, 11, 12, 15, 30, 0, tzinfo=UTC)

    opt_c = Contract(
        secType="OPT",
        conId=700001,
        symbol="NVDA",
        localSymbol="NVDA  261218C00140000",
        lastTradeDateOrContractMonth="20261218",
        strike=140.0,
        right="C",
        multiplier="100",
        currency="USD",
    )
    stk_c = Contract(secType="STK", conId=4350, symbol="AVGO", currency="USD")
    fut_c = Contract(secType="FUT", conId=9999, symbol="ES", currency="USD")

    fake_ib.fills_opt = [
        Fill(
            contract=opt_c,
            execution=Execution(
                execId="0000e0d5.672a1b3c.01.01",
                acctNumber="U1234567",
                side="SLD",
                shares=2.0,
                price=5.40,
                orderId=501,
            ),
            commissionReport=CommissionReport(
                execId="0000e0d5.672a1b3c.01.01",
                commission=1.35,
                currency="USD",
                realizedPNL=sys.float_info.max,
            ),
            time=ts,
        )
    ]
    fake_ib.fills_stk = [
        Fill(
            contract=stk_c,
            execution=Execution(
                execId="stk-exec-1",
                acctNumber="U1234567",
                side="BOT",
                shares=10.0,
                price=180.0,
                orderId=502,
            ),
            commissionReport=CommissionReport(
                execId="stk-exec-1",
                commission=-1.00,
                currency="USD",
                realizedPNL=0.0,
            ),
            time=ts,
        )
    ]
    fake_ib.fills_other = [
        Fill(
            contract=fut_c,
            execution=Execution(
                execId="fut-exec-1",
                acctNumber="U1234567",
                side="BOT",
                shares=1.0,
                price=5900.0,
                orderId=503,
            ),
            commissionReport=CommissionReport(),
            time=ts,
        )
    ]
    fake_ib.positions_result = [
        Position(account="U1234567", contract=opt_c, position=-2.0, avgCost=540.0),
        Position(account="U1234567", contract=stk_c, position=10.0, avgCost=180.0),
        Position(account="U1234567", contract=fut_c, position=1.0, avgCost=5900.0),
        Position(account="U1234567", contract=opt_c, position=0.0, avgCost=0.0),
    ]

    broker = IBKRBrokerClient(_settings(enabled=True), ib_factory=lambda: fake_ib)
    execs, positions = run_coro(fetch_gateway_ledger(broker, include_stock=True, clock=lambda: ts))

    assert len(fake_ib.exec_filters) == 2
    assert isinstance(fake_ib.exec_filters[0], ExecutionFilter)
    assert fake_ib.exec_filters[0].secType == "OPT"
    assert fake_ib.exec_filters[1].secType == "STK"
    assert fake_ib.positions_calls == 1

    assert len(execs) == 2
    assert {e.asset_class for e in execs} == {"OPT", "STK"}
    opt_row = next(e for e in execs if e.asset_class == "OPT")
    assert opt_row.source == "GATEWAY_LIVE"
    assert opt_row.proceeds == pytest.approx(1080.0)
    assert opt_row.commission == pytest.approx(1.35)
    assert opt_row.realized_pnl_ib is None  # UNSET_DOUBLE sanitized
    stk_row = next(e for e in execs if e.asset_class == "STK")
    assert stk_row.multiplier == 1
    assert stk_row.proceeds == pytest.approx(-1800.0)

    assert len(positions) == 2
    assert {p.asset_class for p in positions} == {"OPT", "STK"}
    assert "U1234567" not in repr(execs)
    assert "U1234567" not in repr(positions)

    # include_stock=False skips STK execution query & STK positions
    fake_ib.exec_filters.clear()
    execs_opt_only, pos_opt_only = run_coro(
        fetch_gateway_ledger(broker, include_stock=False, clock=lambda: ts)
    )
    assert [f.secType for f in fake_ib.exec_filters] == ["OPT"]
    assert [e.asset_class for e in execs_opt_only] == ["OPT"]
    assert [p.asset_class for p in pos_opt_only] == ["OPT"]


def _make_pos(conid: int | None, idx: int = 1) -> LedgerPositionRow:
    return LedgerPositionRow(
        conid=conid,
        symbol=f"NVDA  261218C{140 + idx:05d}000",
        underlying="NVDA",
        asset_class="OPT",
        expiry="20261218",
        strike=float(140 + idx),
        right="C",
        position=-1.0,
        source="GATEWAY_LIVE",
        updated_at="2026-11-12T15:30:00Z",
    )


def test_refresh_open_option_marks_bounds_yields_fallback_and_cancels() -> None:
    fake_ib = _FakeLedgerIB()
    broker = IBKRBrokerClient(_settings(enabled=True, mode="DELAYED"), ib_factory=lambda: fake_ib)

    # Hard cap > 60 raises PacingViolationError
    with pytest.raises(PacingViolationError):
        run_coro(refresh_open_option_marks(broker, [_make_pos(700001)], max_contracts=61))

    # Empty / missing conId returns {} without connecting
    assert run_coro(refresh_open_option_marks(broker, [_make_pos(None)])) == {}
    assert not broker.is_connected

    # > 20 positions yields every 20 subscriptions, caps at 60, and cancels 100%
    positions = [_make_pos(700000 + i, i) for i in range(65)]
    sleeps: list[float] = []

    async def rec_sleep(sec: float) -> None:
        sleeps.append(sec)

    marks = run_coro(refresh_open_option_marks(broker, positions, sleep=rec_sleep))
    assert len(marks) == 60
    assert len(fake_ib.subscribed) == 60
    assert len(fake_ib.cancelled) == 60
    assert all(args[1:] == ("", False, False) for args in fake_ib.subscribed)
    assert sleeps.count(0.01) >= 2  # yielded every 20 subscriptions & cancellations

    # Type 3 -> 4 fallback when Type 3 quotes are empty
    fake_ib2 = _FakeLedgerIB()

    def fallback_factory(contract: Any, md_types: list[int]) -> Any:
        if md_types[-1] == 3:
            return SimpleNamespace(contract=contract, bid=-1.0, ask=-1.0, last=-1.0, close=-1.0)
        return SimpleNamespace(contract=contract, delayedBid=3.1, delayedAsk=3.3, marketDataType=4)

    fake_ib2.ticker_factory = fallback_factory
    broker2 = IBKRBrokerClient(_settings(enabled=True, mode="DELAYED"), ib_factory=lambda: fake_ib2)
    marks2 = run_coro(refresh_open_option_marks(broker2, [_make_pos(700001)], sleep=noop_sleep))
    assert marks2 == {700001: 3.2}
    assert fake_ib2.market_data_types[0] == 3
    assert fake_ib2.market_data_types[-1] == 4
    assert len(fake_ib2.subscribed) == len(fake_ib2.cancelled) == 2

    # LIVE 10089 missing subscription cascades to Type 3 Delayed and still cancels 100%
    fake_ib_live = _FakeLedgerIB()
    broker_live = IBKRBrokerClient(
        _settings(enabled=True, mode="LIVE"), ib_factory=lambda: fake_ib_live
    )

    def live_10089_factory(contract: Any, md_types: list[int]) -> Any:
        if md_types[-1] == 1:
            fake_ib_live.errorEvent.emit(10, 10089, "Requires OPRA subscription", contract)
            return SimpleNamespace(contract=contract, bid=-1.0, ask=-1.0)
        return SimpleNamespace(contract=contract, delayedBid=4.0, delayedAsk=4.4, marketDataType=3)

    fake_ib_live.ticker_factory = live_10089_factory
    marks_live = run_coro(
        refresh_open_option_marks(broker_live, [_make_pos(700001)], sleep=noop_sleep)
    )
    assert marks_live == {700001: 4.2}
    assert broker_live.current_type == 3
    assert len(fake_ib_live.subscribed) == len(fake_ib_live.cancelled) == 2

    # Exception mid-collection STILL cancels all already-subscribed contracts in finally
    fake_ib3 = _FakeLedgerIB()
    sub_count = 0

    def exploding_factory(contract: Any, md_types: list[int]) -> Any:
        nonlocal sub_count
        sub_count += 1
        if sub_count == 3:
            raise RuntimeError("boom during reqMktData")
        return SimpleNamespace(contract=contract, bid=2.0, ask=2.2)

    fake_ib3.ticker_factory = exploding_factory
    broker3 = IBKRBrokerClient(_settings(enabled=True, mode="DELAYED"), ib_factory=lambda: fake_ib3)
    with pytest.raises(RuntimeError, match="boom"):
        run_coro(
            refresh_open_option_marks(
                broker3, [_make_pos(700001, 1), _make_pos(700002, 2), _make_pos(700003, 3)]
            )
        )
    assert len(fake_ib3.cancelled) == 2

    # Exception during poll_until (after all 3 subscribed) STILL cancels 100% of subscriptions
    fake_ib4 = _FakeLedgerIB()
    fake_ib4.ticker_factory = lambda contract, _md: SimpleNamespace(
        contract=contract, bid=-1.0, ask=-1.0
    )
    broker4 = IBKRBrokerClient(_settings(enabled=True, mode="DELAYED"), ib_factory=lambda: fake_ib4)

    async def exploding_sleep(_sec: float) -> None:
        raise RuntimeError("poll interrupted")

    with pytest.raises(RuntimeError, match="poll interrupted"):
        run_coro(
            refresh_open_option_marks(
                broker4,
                [_make_pos(700001, 1), _make_pos(700002, 2), _make_pos(700003, 3)],
                sleep=exploding_sleep,
            )
        )
    assert len(fake_ib4.subscribed) == 3
    assert len(fake_ib4.cancelled) == 3


def test_refresh_open_option_marks_cancels_all_on_cancelled_error_and_double_fallback() -> None:
    # 1. With 25 (>20) contracts, CancelledError during poll AND during finally sleep STILL
    #    cancels 100% of all 25 contracts and propagates CancelledError cleanly.
    fake_ib_cancel = _FakeLedgerIB()
    fake_ib_cancel.ticker_factory = lambda contract, _md: SimpleNamespace(
        contract=contract, bid=-1.0, ask=-1.0
    )
    broker_cancel = IBKRBrokerClient(
        _settings(enabled=True, mode="DELAYED"), ib_factory=lambda: fake_ib_cancel
    )
    positions_25 = [_make_pos(710000 + i, i + 1) for i in range(25)]
    sleep_calls = 0

    async def cancel_sleep(_sec: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        # Allow the subscription pacing yield at contract #20, then cancel during poll_until
        # and again during finally cleanup yield at contract #20.
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run_coro(refresh_open_option_marks(broker_cancel, positions_25, sleep=cancel_sleep))
    assert sleep_calls == 3
    assert len(fake_ib_cancel.subscribed) == 25
    assert len(fake_ib_cancel.cancelled) == 25

    # 2. LIVE 1 -> 3 downgrade with >80% empty delayed ticks cascades 3 -> 4 on the same refresh
    fake_ib_chain = _FakeLedgerIB()
    broker_chain = IBKRBrokerClient(
        _settings(enabled=True, mode="LIVE"), ib_factory=lambda: fake_ib_chain
    )

    def chain_factory(contract: Any, md_types: list[int]) -> Any:
        current = md_types[-1]
        if current == 1:
            fake_ib_chain.errorEvent.emit(10, 10089, "Requires OPRA subscription", contract)
            return SimpleNamespace(contract=contract, bid=-1.0, ask=-1.0)
        if current == 3:
            return SimpleNamespace(contract=contract, bid=-1.0, ask=-1.0, marketDataType=3)
        return SimpleNamespace(contract=contract, delayedBid=3.8, delayedAsk=4.2, marketDataType=4)

    fake_ib_chain.ticker_factory = chain_factory
    marks_chain = run_coro(
        refresh_open_option_marks(broker_chain, [_make_pos(700001)], sleep=noop_sleep)
    )
    assert marks_chain == {700001: 4.0}
    assert broker_chain.current_type == 4
    assert len(fake_ib_chain.subscribed) == len(fake_ib_chain.cancelled) == 3
