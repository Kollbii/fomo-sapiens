"""Snapshot quality: page quote coverage under the `ib_async` 45 msg/s throttle."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest
from ib_async import Option, Stock

from src.broker.client import IBKRBrokerClient
from src.broker.contracts import ContractDiscoveryService, select_spot
from src.broker.fetcher import PagedOptionChainFetcher, empty_real_quote_ratio
from src.broker.parser import has_real_quote
from src.core.config import Settings
from src.storage.quality import (
    MIN_QUOTE_COVERAGE,
    evaluate_snapshot,
    is_usable_snapshot,
    measure_quote_quality,
)
from src.storage.schema import SnapshotMeta
from tests.support.aio import run_coro as _run
from tests.support.ib_fakes import FakeChainIB as FakeIB

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


def test_paced_pages_never_exceed_the_ib_async_window() -> None:
    clock = VirtualClock()
    fake_ib = ThrottledFakeIB(clock)
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, sleep=clock.sleep)

    _run(fetcher.fetch_pages(_contracts(400)))

    assert fake_ib.max_in_window <= 40
    # 5 pages x (4 subscribe + 4 cancel bursts) x 0.55 s + 4 cooldowns + polling stays bounded
    assert clock.now < 45.0


def test_page_quote_counts_reported_per_page() -> None:
    clock = VirtualClock()
    fake_ib = ThrottledFakeIB(clock)
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, sleep=clock.sleep)
    tickers, _ = _run(fetcher.fetch_pages(_contracts(170)))
    assert [sum(_quoted(t) for t in tickers[i : i + 80]) for i in range(0, 170, 80)] == [
        80,
        80,
        10,
    ]


def test_cancels_survive_an_interrupted_pacing_pause() -> None:
    fake_ib = FakeIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    calls = {"n": 0}

    async def _sleep(sec: float) -> None:
        calls["n"] += 1
        if calls["n"] > 3:  # 3 subscribe pauses pass, the first cancel pause is cancelled
            raise asyncio.CancelledError

    fetcher = PagedOptionChainFetcher(client, sleep=_sleep)
    contracts = _contracts(45)
    with pytest.raises(asyncio.CancelledError):
        _run(fetcher._collect_page_once(contracts, generic_tick_list="", ready_predicate=bool))
    assert fake_ib.cancelled == contracts


# ------------------------------------------------------------------ quote definition
@pytest.mark.parametrize(
    ("bid", "ask", "last", "close", "expected"),
    [
        (4.8, 5.2, 5.0, 4.9, True),
        (0.0, 0.05, math.nan, 0.04, True),  # far OTM: zero bid, valid ask
        (-1.0, 0.05, -1.0, -1.0, True),  # IBKR -1 "no bid" sentinel
        (0.10, -1.0, math.nan, math.nan, True),  # bid only
        (-1.0, -1.0, 5.0, 4.9, False),  # last trade but no current quote
        (math.nan, math.nan, math.nan, 4.9, False),  # previous close only
        (0.0, 0.0, 0.0, 0.0, False),
        (1e9, 2147483647.0, math.nan, math.nan, False),  # IBKR sentinels
    ],
)
def test_has_real_quote(bid: float, ask: float, last: float, close: float, expected: bool) -> None:
    tk = SimpleNamespace(bid=bid, ask=ask, last=last, close=close)
    assert has_real_quote(tk) is expected


def test_empty_real_quote_ratio_ignores_close_only() -> None:
    quoted = SimpleNamespace(bid=1.0, ask=1.1, last=1.05, close=1.0)
    close_only = SimpleNamespace(bid=-1.0, ask=-1.0, last=-1.0, close=1.0)
    assert empty_real_quote_ratio([quoted, close_only, close_only, close_only]) == 0.75
    assert empty_real_quote_ratio([]) == 0.0


# ------------------------------------------------------------------ spot source
@pytest.mark.parametrize(
    ("last", "bid", "ask", "close", "expected"),
    [
        (150.0, 149.8, 150.2, 140.0, (150.0, "LAST")),
        (150.0, 152.9, 153.1, 140.0, (150.0, "LAST")),  # 1.96% from mid: within 2%
        (150.0, 153.0, 154.0, 140.0, (153.5, "MID")),  # 2.28% from mid: use mid
        (159.97, 174.9, 175.1, 169.3, (175.0, "MID")),  # the 230508 case
        (150.0, -1.0, 151.0, 140.0, (150.0, "LAST")),  # no two-sided quote: no cross-check
        (-1.0, 148.0, 150.0, 140.0, (149.0, "MID")),
        (math.nan, -1.0, -1.0, 140.0, (140.0, "CLOSE")),
        (-1.0, -1.0, -1.0, 0.0, None),
    ],
)
def test_select_spot_source(
    last: float, bid: float, ask: float, close: float, expected: tuple[float, str] | None
) -> None:
    picked = select_spot(SimpleNamespace(last=last, bid=bid, ask=ask, close=close))
    if expected is None:
        assert picked is None
    else:
        assert picked is not None
        assert picked[0] == pytest.approx(expected[0])
        assert picked[1] == expected[1]


def test_resolve_spot_with_source_waits_for_last_and_quote() -> None:
    clock = VirtualClock()
    fake_ib = FakeIB()
    ticker = SimpleNamespace(last=150.0, bid=math.nan, ask=math.nan, close=140.0)

    def _factory(contract: Any, _ticks: str) -> Any:
        ticker.contract = contract
        return ticker

    async def _sleep(sec: float) -> None:
        clock.now += sec
        if clock.now >= 0.5:  # bid/ask arrive after `last`
            ticker.bid, ticker.ask = 153.0, 154.0

    fake_ib.ticker_factory = _factory
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, sleep=_sleep)
    spot, source = _run(svc.resolve_spot_with_source(Stock("NVDA", "SMART", "USD", conId=1)))
    assert (spot, source) == (153.5, "MID")


def test_resolve_spot_reports_close_when_nothing_else_arrives() -> None:
    fake_ib = FakeIB()
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, last=math.nan, bid=-1.0, ask=-1.0, close=140.0
    )
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, sleep=VirtualClock().sleep)
    assert _run(svc.resolve_spot_with_source(Stock("NVDA", "SMART", "USD", conId=1))) == (
        140.0,
        "CLOSE",
    )


# ------------------------------------------------------------------ usable-snapshot rule
def _meta(**overrides: Any) -> SnapshotMeta:
    base: dict[str, Any] = {
        "snapshot_id": "NVDA_20261112_150000",
        "file_path": "data/snapshots/NVDA/NVDA_20261112_150000.csv",
        "snapshot_ts_utc": "2026-11-12T15:00:00Z",
        "market_date": "2026-11-12",
        "market_data_type": 3,
        "underlying_price": 177.0,
        "contract_count": 100,
        "expirations": ["2026-12-18"],
        "spot_source": "LAST",
        "quoted_contract_count": 90,
        "quote_coverage": 0.90,
        "prior_close_only_count": 0,
        "collection_mode": "MANUAL",
    }
    base.update(overrides)
    return SnapshotMeta.model_validate(base)


def test_usable_at_threshold_and_not_below() -> None:
    assert MIN_QUOTE_COVERAGE == 0.90
    assert is_usable_snapshot(_meta(quote_coverage=0.90))
    assert is_usable_snapshot(_meta(quote_coverage=1.0))
    verdict = evaluate_snapshot(_meta(quote_coverage=0.8999))
    assert not verdict.usable
    assert verdict.reasons == ("quote_coverage 0.90 < 0.90",)


@pytest.mark.parametrize(("source", "usable"), [("LAST", True), ("MID", True), ("CLOSE", False)])
def test_usable_by_spot_source(source: str, usable: bool) -> None:
    assert is_usable_snapshot(_meta(spot_source=source)) is usable


@pytest.mark.parametrize(("md_type", "usable"), [(1, True), (2, False), (3, True), (4, False)])
def test_usable_by_market_data_type(md_type: int, usable: bool) -> None:
    assert is_usable_snapshot(_meta(market_data_type=md_type)) is usable


def test_legacy_meta_without_quality_fields_is_not_usable() -> None:
    legacy = _meta(
        spot_source=None,
        quoted_contract_count=None,
        quote_coverage=None,
        prior_close_only_count=None,
        collection_mode=None,
    )
    verdict = evaluate_snapshot(legacy)
    assert not verdict.usable
    assert verdict.reasons == ("quote_coverage unknown", "spot_source unknown")


def test_every_failed_condition_is_reported() -> None:
    verdict = evaluate_snapshot(_meta(quote_coverage=0.5, spot_source="CLOSE", market_data_type=4))
    assert len(verdict.reasons) == 3


def test_measure_quote_quality_counts() -> None:
    df = pl.DataFrame(
        {
            "bid": [4.8, None, None, None, None],
            "ask": [5.2, 0.05, None, None, None],
            "last": [5.0, None, 3.0, None, None],
            "close": [4.9, 0.04, 2.9, 2.0, None],
        }
    )
    q = measure_quote_quality(df)
    assert (q.contract_count, q.quoted_contract_count, q.prior_close_only_count) == (5, 2, 1)
    assert q.quote_coverage == 0.4
    assert measure_quote_quality(df.head(0)).quote_coverage == 0.0
