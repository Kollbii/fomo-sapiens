"""Snapshot quality: page quote coverage under the `ib_async` 45 msg/s throttle."""

from __future__ import annotations

import math
from collections import deque
from typing import Any

from ib_async import Option

from src.broker.client import IBKRBrokerClient
from src.broker.fetcher import PagedOptionChainFetcher
from src.core.config import Settings
from tests.unit.test_contracts_and_pagination import FakeIB, _run

IB_ASYNC_MAX_REQUESTS = 45  # ib_async.Client.MaxRequests
IB_ASYNC_INTERVAL_SEC = 1.0  # ib_async.Client.RequestsInterval
QUOTE_LATENCY_SEC = 0.30  # time from a sent reqMktData to its first delayed quote


class VirtualClock:
    """Virtual time advanced only by the injected `sleep`."""

    def __init__(self) -> None:
        self.now = 0.0

    async def sleep(self, sec: float) -> None:
        self.now += max(0.0, sec)


class ThrottledTicker:
    """Quote fields appear `QUOTE_LATENCY_SEC` after the request left the throttle queue,
    unless the cancel left the queue first (then no data ever arrives)."""

    def __init__(self, contract: Any, clock: VirtualClock, data_at: float) -> None:
        self.contract = contract
        self._clock = clock
        self.data_at = data_at
        self.cancelled_at = math.inf
        self.volume = 10
        self.callOpenInterest = 100
        self.putOpenInterest = 100
        self.modelGreeks = None
        self.marketDataType = 3

    def _has_data(self) -> bool:
        return self._clock.now >= self.data_at and self.data_at < self.cancelled_at

    @property
    def bid(self) -> float:
        return 4.8 if self._has_data() else math.nan

    @property
    def ask(self) -> float:
        return 5.2 if self._has_data() else math.nan

    @property
    def last(self) -> float:
        return 5.0 if self._has_data() else math.nan

    @property
    def close(self) -> float:
        return 4.9 if self._has_data() else math.nan


class ThrottledFakeIB(FakeIB):
    """Models `ib_async.Client.sendMsg`: FIFO queue, at most 45 messages in any 1 s window
    (a message sent exactly 1.0 s ago still counts)."""

    def __init__(self, clock: VirtualClock) -> None:
        super().__init__()
        self._clock = clock
        self._sent: deque[float] = deque()
        self._last_send = 0.0
        self._tickers: dict[int, ThrottledTicker] = {}
        self.max_in_window = 0

    def _send_time(self) -> float:
        t = max(self._clock.now, self._last_send)
        if len(self._sent) >= IB_ASYNC_MAX_REQUESTS:
            t = max(t, self._sent[-IB_ASYNC_MAX_REQUESTS] + IB_ASYNC_INTERVAL_SEC + 1e-6)
        self._sent.append(t)
        self._last_send = t
        in_window = sum(1 for s in self._sent if t - s <= IB_ASYNC_INTERVAL_SEC)
        self.max_in_window = max(self.max_in_window, in_window)
        return t

    def reqMktData(  # noqa: N802
        self,
        contract: Any,
        genericTickList: str = "",  # noqa: N803
        snapshot: bool = False,
        regulatorySnapshot: bool = False,  # noqa: N803
    ) -> Any:
        self.subscribed.append((contract, genericTickList, snapshot, regulatorySnapshot))
        ticker = ThrottledTicker(contract, self._clock, self._send_time() + QUOTE_LATENCY_SEC)
        self._tickers[id(contract)] = ticker
        return ticker

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        self.cancelled.append(contract)
        sent = self._send_time()
        ticker = self._tickers.get(id(contract))
        if ticker is not None:
            ticker.cancelled_at = min(ticker.cancelled_at, sent)


def _contracts(n: int) -> list[Option]:
    return [
        Option("NVDA", "20261218", float(100 + i), "C" if i % 2 else "P", "SMART", conId=i + 1)
        for i in range(n)
    ]


def _quoted(ticker: Any) -> bool:
    return not math.isnan(ticker.bid) or not math.isnan(ticker.ask)


def test_every_page_gets_quotes_under_ib_async_throttle() -> None:
    """Reproduces the 'only page 0 is quoted' pattern seen in saved snapshots."""
    clock = VirtualClock()
    fake_ib = ThrottledFakeIB(clock)
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, sleep=clock.sleep)

    tickers, pages = _run(fetcher.fetch_pages(_contracts(400)))

    assert pages == 5
    per_page = [sum(_quoted(t) for t in tickers[i : i + 80]) for i in range(0, 400, 80)]
    assert per_page == [80, 80, 80, 80, 80]
    assert len(fake_ib.cancelled) == 400
