"""Minimal `ib_async` building blocks shared by broker/web tests."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any


class FakeEvent:
    """Minimal `eventkit.Event` stand-in supporting `+=` subscription and `emit`."""

    def __init__(self) -> None:
        self._handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> FakeEvent:
        self._handlers.append(handler)
        return self

    def emit(self, *args: Any) -> None:
        for handler in list(self._handlers):
            handler(*args)


class FakeChainIB:
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
