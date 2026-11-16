"""`LiveLegQuoteService`: multi-ticker exact-contract leg stream (budget, diff, cleanup, cadence).

Socket-free: `FakeChainIB` (tests/support) behind a real `IBKRBrokerClient`, driven by `run_coro`
or the virtual-clock loop. The fake logs every `reqMktData` / `cancelMktData` in order and tracks
the open lines, so budget, cancel-before-subscribe and 100% cleanup are asserted on IBKR calls.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from src.analytics.bsm_fallback import DEFAULT_RISK_FREE_RATE
from src.broker.client import (
    QUALIFY_CHUNK_PAUSE_SEC,
    QUALIFY_CHUNK_SIZE,
    IBKRBrokerClient,
    SleepFn,
)
from src.broker.corridor import (
    CORRIDOR_MAX_CONTRACTS,
    DEFAULT_CORRIDOR_MAX_CONTRACTS,
    CorridorContractSpec,
)
from src.broker.corridor.lines import build_quote_rows
from src.broker.fetcher import DELAYED_GENERIC_TICKS, PAGE_PACING_CHUNK_SIZE, PAGE_PACING_PAUSE_SEC
from src.broker.live_legs import (
    LIVE_LEGS_INTERVAL_SEC,
    SCREENER_LIVE_BUSY_MESSAGE,
    LiveLegQuoteService,
    LiveLegsFrame,
    LiveLegsSpec,
    LiveSpotLine,
    spec_line_key,
)
from src.core.config import CorridorSettings, IBKRSettings, Settings
from src.core.exceptions import (
    GatewayConnectionError,
    GatewayTimeoutError,
    PacingViolationError,
    ScreenerLiveBusyError,
)
from src.services.corridor import make_bsm_greeks_fallback
from tests.support.aio import noop_sleep, run_coro, run_virtual_loop
from tests.support.ib_fakes import FakeChainIB

_NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
_E1 = "2026-12-18"
LineKey = tuple[Any, ...]


def _ckey(contract: Any) -> LineKey:
    if getattr(contract, "secType", "") == "STK":
        return ("STK", contract.symbol)
    return (
        contract.symbol,
        contract.lastTradeDateOrContractMonth,
        float(contract.strike),
        contract.right,
    )


class _LegIB(FakeChainIB):
    """`FakeChainIB` + ordered sub/cancel log, open-line tracking and mutable tickers."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, LineKey]] = []
        self.open_lines: set[LineKey] = set()
        self.max_open = 0
        self.double_subscriptions: list[LineKey] = []
        self.stray_cancels: list[LineKey] = []
        self.qualify_calls = 0
        self.tickers: dict[LineKey, Any] = {}
        self.ticker_factory = self._ticker

    def _ticker(self, contract: Any, _ticks: str) -> Any:
        if getattr(contract, "secType", "") == "STK":
            return SimpleNamespace(contract=contract, last=100.0, bid=99.9, ask=100.1, close=99.5)
        return SimpleNamespace(
            contract=contract,
            bid=2.0,
            ask=2.2,
            last=2.1,
            close=2.05,
            volume=150,
            callOpenInterest=900,
            putOpenInterest=800,
            modelGreeks=None,
        )

    def reqMktData(  # noqa: N802
        self,
        contract: Any,
        genericTickList: str = "",  # noqa: N803
        snapshot: bool = False,
        regulatorySnapshot: bool = False,  # noqa: N803
    ) -> Any:
        key = _ckey(contract)
        if key in self.open_lines:
            self.double_subscriptions.append(key)
        ticker = super().reqMktData(contract, genericTickList, snapshot, regulatorySnapshot)
        self.tickers[key] = ticker
        self.open_lines.add(key)
        self.max_open = max(self.max_open, len(self.open_lines))
        self.events.append(("sub", key))
        return ticker

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        key = _ckey(contract)
        if key not in self.open_lines:
            self.stray_cancels.append(key)
        super().cancelMktData(contract)
        self.open_lines.discard(key)
        self.events.append(("cancel", key))

    async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]:  # noqa: N802
        self.qualify_calls += 1
        return await super().qualifyContractsAsync(*contracts)


def _opt(
    symbol: str, strike: float, right: str = "P", *, conid: int | None = None
) -> CorridorContractSpec:
    return CorridorContractSpec(
        option_conid=(
            conid
            if conid is not None
            else 500_000
            + 1_000 * (ord(symbol[0]) - 64)
            + int(strike * 10)
            + (5 if right == "C" else 0)
        ),
        symbol=symbol,
        expiry=_E1,
        ib_expiry=_E1.replace("-", ""),
        dte=37,
        strike=strike,
        right="C" if right == "C" else "P",
        zone="STRATEGY_LEG",
        is_put_wall=False,
        is_call_wall=False,
        is_strategy_leg=True,
        cached_bid=1.0,
        cached_ask=1.2,
        cached_mid=1.1,
        cached_last=1.1,
        cached_close=1.1,
        cached_volume=10,
        cached_open_interest=100,
        cached_iv=0.4,
        cached_delta=-0.3,
        cached_gamma=0.02,
        cached_theta=-0.03,
        cached_vega=0.1,
        cached_greeks_source="BSM_FALLBACK",
    )


def _key(spec: CorridorContractSpec) -> LineKey:
    return (spec.symbol, spec.ib_expiry, float(spec.strike), spec.right)


def _spot(symbol: str, conid: int = 4815, price: float = 100.0) -> LiveSpotLine:
    return LiveSpotLine(symbol=symbol, underlying_conid=conid, reference_price=price)


class _Source:
    """Mutable `spec_source` (the service layer swaps in a new version to re-allocate)."""

    def __init__(self, spec: LiveLegsSpec) -> None:
        self.spec = spec

    def __call__(self) -> LiveLegsSpec:
        return self.spec


def _service(
    *, settings: Settings | None = None, sleep: SleepFn = noop_sleep, greeks: Any = None
) -> tuple[LiveLegQuoteService, _LegIB]:
    ib = _LegIB()
    cfg = settings or Settings()
    client = IBKRBrokerClient(cfg, ib_factory=lambda: ib)
    svc = LiveLegQuoteService(client, cfg, clock=lambda: _NOW, sleep=sleep, greeks_fallback=greeks)
    return svc, ib


async def _first(gen: AsyncGenerator[LiveLegsFrame, None]) -> LiveLegsFrame:
    return await anext(gen)


def _assert_all_released(svc: LiveLegQuoteService, ib: _LegIB) -> None:
    assert not ib.open_lines
    assert len(ib.cancelled) == len(ib.subscribed)
    assert ib.double_subscriptions == [] and ib.stray_cancels == []
    assert svc.active_stream_count == 0 and svc.active_line_count == 0


# ------------------------------------------------------------------ lines & budget
def test_stream_opens_exact_lines_reusing_conids_and_cancels_all_on_aclose() -> None:
    svc, ib = _service()
    nvda = [_opt("NVDA", k) for k in (95.0, 100.0, 105.0)] + [_opt("NVDA", 110.0, "C")]
    avgo = [_opt("AVGO", k) for k in (170.0, 175.0)]
    spec = LiveLegsSpec(
        0, tuple(nvda + avgo), (_spot("NVDA"), _spot("AVGO", conid=4816, price=172.0))
    )
    gen = svc.stream_live_legs(_Source(spec), session_id="s1")
    frame = run_coro(anext(gen))

    assert frame.lines_used == len(ib.subscribed) == 8
    assert ib.qualify_calls == 0  # snapshot conIds reused, no chain / qualification request
    assert [key for _, key in ib.events[:2]] == [("STK", "NVDA"), ("STK", "AVGO")]
    for contract, ticks, snapshot, regulatory in ib.subscribed:
        assert (snapshot, regulatory) == (False, False)
        assert ticks == ("" if contract.secType == "STK" else DELAYED_GENERIC_TICKS)
    assert {c.conId for c, *_ in ib.subscribed if c.secType == "OPT"} == {
        o.option_conid for o in nvda + avgo
    }
    assert set(frame.quotes) == {spec_line_key(o) for o in nvda + avgo} == frame.live_keys
    assert frame.spots == {"NVDA": 100.0, "AVGO": 100.0}
    assert (frame.version, frame.sequence, frame.lines_budget) == (0, 1, CORRIDOR_MAX_CONTRACTS)
    assert frame.option_lines_budget == DEFAULT_CORRIDOR_MAX_CONTRACTS
    assert frame.market_data_type == 3 and frame.updated_ts_utc == "2026-11-11T20:00:00Z"
    assert svc.active_stream_count == 1 and svc.active_line_count == 8

    run_coro(gen.aclose())
    _assert_all_released(svc, ib)


def test_line_budget_is_enforced_before_any_line_is_opened() -> None:
    svc, ib = _service()
    opts = tuple(_opt("NVDA", 20.0 + i) for i in range(89))
    two_spots = (_spot("NVDA"), _spot("AVGO", conid=4816))
    over = LiveLegsSpec(0, opts, two_spots)  # 89 + 2 = 91 lines
    with pytest.raises(PacingViolationError, match="exceed the budget 90"):
        run_coro(anext(svc.stream_live_legs(_Source(over), session_id="s1")))
    assert ib.subscribed == [] and svc.active_stream_count == 0

    exact = LiveLegsSpec(0, opts[:88], two_spots)  # 88 + 2 = 90 lines: allowed
    gen = svc.stream_live_legs(_Source(exact), session_id="s2")
    assert run_coro(anext(gen)).lines_used == CORRIDOR_MAX_CONTRACTS
    run_coro(gen.aclose())
    _assert_all_released(svc, ib)

    # Lower configured Corridor caps bound the live screener too (total AND option lines).
    small = Settings(corridor=CorridorSettings(default_max_contracts=40, hard_max_contracts=50))
    svc_small, ib_small = _service(settings=small)
    assert (svc_small.lines_budget, svc_small.option_lines_budget) == (50, 40)
    capped = LiveLegsSpec(0, opts[:50], (_spot("NVDA"),))
    with pytest.raises(PacingViolationError):
        run_coro(anext(svc_small.stream_live_legs(_Source(capped), session_id="s3")))
    over_options = LiveLegsSpec(0, opts[:41], (_spot("NVDA"),))  # 42 <= 50 lines, 41 > 40
    with pytest.raises(PacingViolationError, match="option lines 41 exceed the option budget 40"):
        run_coro(anext(svc_small.stream_live_legs(_Source(over_options), session_id="s4")))
    assert ib_small.subscribed == []

    # A config of default_max_contracts=90 is still clamped to 88 option lines (spec <= 88).
    wide = Settings(corridor=CorridorSettings(default_max_contracts=90, hard_max_contracts=90))
    svc_wide, ib_wide = _service(settings=wide)
    assert (svc_wide.lines_budget, svc_wide.option_lines_budget) == (90, 88)
    too_many = LiveLegsSpec(0, opts[:89], (_spot("NVDA"),))  # 90 lines, but 89 options > 88
    with pytest.raises(PacingViolationError, match="option lines 89 exceed the option budget 88"):
        run_coro(anext(svc_wide.stream_live_legs(_Source(too_many), session_id="s5")))
    assert ib_wide.subscribed == []


def test_option_cap_and_line_total_are_both_enforced_before_any_line() -> None:
    """<= 88 option lines (Corridor option cap) AND <= 90 lines incl. spots: either breach is a
    `PacingViolationError` before any `reqMktData`; both boundaries are accepted."""
    svc, ib = _service()
    budget, option_budget = svc.lines_budget, svc.option_lines_budget
    assert (budget, option_budget) == (CORRIDOR_MAX_CONTRACTS, DEFAULT_CORRIDOR_MAX_CONTRACTS)
    opts = {s: tuple(_opt(s, 10.0 + i) for i in range(89)) for s in ("NVDA", "AVGO", "CRDO")}
    spots = (_spot("NVDA"), _spot("AVGO", conid=4816), _spot("CRDO", conid=4817))

    over_options = LiveLegsSpec(0, opts["NVDA"], spots[:1])  # 89 + 1 = 90 lines
    over_total = LiveLegsSpec(  # 88 + 3 = 91 lines
        0, opts["NVDA"][:30] + opts["AVGO"][:30] + opts["CRDO"][:28], spots
    )
    with pytest.raises(PacingViolationError, match="option lines 89 exceed the option budget 88"):
        over_options.validate(budget, option_budget=option_budget)
    with pytest.raises(PacingViolationError, match=r"lines 91 \(options \+ spots\) exceed"):
        over_total.validate(budget, option_budget=option_budget)
    for idx, spec in enumerate((over_options, over_total)):
        with pytest.raises(PacingViolationError):
            run_coro(anext(svc.stream_live_legs(_Source(spec), session_id=f"over{idx}")))
    assert ib.subscribed == [] and svc.active_stream_count == 0

    # Boundaries: 88 + 1 = 89, 88 + 2 = 90 and 87 + 3 = 90 lines.
    LiveLegsSpec(0, opts["NVDA"][:44] + opts["AVGO"][:44], spots[:2]).validate(
        budget, option_budget=option_budget
    )
    LiveLegsSpec(0, opts["NVDA"][:29] + opts["AVGO"][:29] + opts["CRDO"][:29], spots).validate(
        budget, option_budget=option_budget
    )
    at_cap = LiveLegsSpec(0, opts["NVDA"][:88], spots[:1])
    gen = svc.stream_live_legs(_Source(at_cap), session_id="s1")
    frame = run_coro(anext(gen))
    assert (frame.lines_used, frame.lines_budget, frame.option_lines_budget) == (89, 90, 88)
    run_coro(gen.aclose())
    _assert_all_released(svc, ib)


def test_inconsistent_specs_are_rejected_before_any_line() -> None:
    svc, ib = _service()
    no_spot = LiveLegsSpec(0, (_opt("NVDA", 100.0),), ())
    with pytest.raises(ValueError, match="no spot line"):
        run_coro(anext(svc.stream_live_legs(_Source(no_spot), session_id="s1")))
    dup = LiveLegsSpec(0, (_opt("NVDA", 100.0), _opt("NVDA", 100.0)), (_spot("NVDA"),))
    with pytest.raises(ValueError, match="Duplicate option line"):
        run_coro(anext(svc.stream_live_legs(_Source(dup), session_id="s2")))
    assert ib.subscribed == [] and svc.active_stream_count == 0


# ------------------------------------------------------------------ re-allocation diff
def test_reallocation_cancels_only_removed_then_subscribes_only_added() -> None:
    svc, ib = _service()
    a, b, c, d = (_opt("NVDA", k) for k in (95.0, 100.0, 105.0, 110.0))
    e = _opt("AVGO", 175.0)
    source = _Source(LiveLegsSpec(0, (a, b, c), (_spot("NVDA"),)))
    gen = svc.stream_live_legs(source, session_id="s1")
    assert run_coro(anext(gen)).lines_used == 4

    mark = len(ib.events)
    source.spec = LiveLegsSpec(1, (b, c, d, e), (_spot("NVDA"), _spot("AVGO", conid=4816)))
    second = run_coro(anext(gen))
    # Removed line cancelled first, then only the added lines (spots before options).
    assert ib.events[mark:] == [
        ("cancel", _key(a)),
        ("sub", ("STK", "AVGO")),
        ("sub", _key(d)),
        ("sub", _key(e)),
    ]
    assert (second.version, second.lines_used) == (1, 6)
    assert set(second.quotes) == {spec_line_key(x) for x in (b, c, d, e)}

    mark = len(ib.events)
    assert run_coro(anext(gen)).version == 1  # same version: no line change at all
    assert ib.events[mark:] == []
    run_coro(gen.aclose())
    _assert_all_released(svc, ib)
    assert len(ib.subscribed) == 7


def test_full_swap_reallocation_never_exceeds_budget_and_paces_bursts() -> None:
    sleeps: list[float] = []

    async def _sleep(sec: float) -> None:
        sleeps.append(sec)

    svc, ib = _service(sleep=_sleep)
    # 88 options (the option cap) over two tickers + 2 spots = the full 90-line budget.
    old = tuple(_opt(s, 10.0 + i) for s in ("NVDA", "AVGO") for i in range(44))
    new = tuple(_opt(s, 200.0 + i) for s in ("NVDA", "AVGO") for i in range(44))
    two_spots = (_spot("NVDA"), _spot("AVGO", conid=4816))
    source = _Source(LiveLegsSpec(0, old, two_spots))
    gen = svc.stream_live_legs(source, session_id="s1")
    assert run_coro(anext(gen)).lines_used == CORRIDOR_MAX_CONTRACTS
    # 90 reqMktData in bursts of 20: a pause before calls 21, 41, 61 and 81 (none before the 1st).
    assert sleeps == [PAGE_PACING_PAUSE_SEC] * 4

    sleeps.clear()
    source.spec = LiveLegsSpec(1, new, two_spots)
    frame = run_coro(anext(gen))
    # The 1 s frame interval, then 88 cancels + 88 reqMktData on one burst pacer (8 pauses).
    assert sleeps == [LIVE_LEGS_INTERVAL_SEC] + [PAGE_PACING_PAUSE_SEC] * 8
    assert frame.lines_used == CORRIDOR_MAX_CONTRACTS
    assert ib.max_open == CORRIDOR_MAX_CONTRACTS  # cancel-before-subscribe: never above 90
    swap = [kind for kind, _ in ib.events[CORRIDOR_MAX_CONTRACTS:]]
    assert swap == ["cancel"] * 88 + ["sub"] * 88
    run_coro(gen.aclose())
    _assert_all_released(svc, ib)


def test_reallocation_above_the_option_cap_fails_and_releases_every_line() -> None:
    """A re-allocation breaching the option cap (89 options + 1 spot = 90 lines) is refused
    before its diff touches a line; the failing stream cancels 100% of its lines."""
    svc, ib = _service()
    base = tuple(_opt("NVDA", 10.0 + i) for i in range(88))
    source = _Source(LiveLegsSpec(0, base, (_spot("NVDA"),)))
    gen = svc.stream_live_legs(source, session_id="s1")
    assert run_coro(anext(gen)).lines_used == 89

    mark = len(ib.events)
    source.spec = LiveLegsSpec(1, (*base, _opt("NVDA", 200.0)), (_spot("NVDA"),))
    with pytest.raises(PacingViolationError, match="option lines 89 exceed the option budget 88"):
        run_coro(anext(gen))
    assert [kind for kind, _ in ib.events[mark:]] == ["cancel"] * 89  # no v1 line subscribed
    _assert_all_released(svc, ib)


# ------------------------------------------------------------------ busy & exclusion
def test_busy_is_rechecked_at_start_and_after_registration_with_zero_lines() -> None:
    svc, ib = _service()
    spec = LiveLegsSpec(0, (_opt("NVDA", 100.0),), (_spot("NVDA"),))
    with pytest.raises(ScreenerLiveBusyError) as exc_info:
        run_coro(anext(svc.stream_live_legs(_Source(spec), session_id="s1", is_busy=lambda: True)))
    assert str(exc_info.value) == SCREENER_LIVE_BUSY_MESSAGE

    answers = iter([False, True])  # lock taken while the stream prepared / registered
    registered: list[int] = []

    async def _hook() -> list[str]:
        registered.append(svc.active_stream_count)
        return []

    gen = svc.stream_live_legs(
        _Source(spec), session_id="s2", is_busy=lambda: next(answers), on_register=_hook
    )
    with pytest.raises(ScreenerLiveBusyError):
        run_coro(anext(gen))
    assert registered == [1]  # the session was visible to a lock holder before the re-check
    assert ib.subscribed == [] and ib.cancelled == []
    assert svc.active_stream_count == 0


def test_one_leg_stream_at_a_time_and_register_notices_lead_the_first_frame() -> None:
    svc, ib = _service()
    spec = LiveLegsSpec(0, (_opt("NVDA", 100.0), _opt("NVDA", 105.0)), (_spot("NVDA"),))
    old = svc.stream_live_legs(_Source(spec), session_id="old")
    run_coro(anext(old))

    async def _hook() -> list[str]:
        return ["Stopped the other live feed."]

    new = svc.stream_live_legs(_Source(spec), session_id="new", on_register=_hook)
    mark = len(ib.events)
    frame = run_coro(anext(new))
    assert frame.notices == ("Stopped the other live feed.",)
    assert [kind for kind, _ in ib.events[mark:]] == ["cancel"] * 3 + ["sub"] * 3
    assert ib.max_open == 3 and svc.active_stream_count == 1
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(old))
    run_coro(new.aclose())
    _assert_all_released(svc, ib)


def test_register_hook_can_stop_its_own_session_with_zero_lines() -> None:
    svc, ib = _service()
    spec = LiveLegsSpec(0, (_opt("NVDA", 100.0),), (_spot("NVDA"),))

    async def _superseded() -> list[str]:
        svc.stop_session("s1")
        return []

    gen = svc.stream_live_legs(_Source(spec), session_id="s1", on_register=_superseded)
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(gen))
    assert ib.subscribed == [] and svc.active_stream_count == 0


# ------------------------------------------------------------------ cleanup on every path
@pytest.mark.parametrize(
    "path", ["stop_all", "socket_drop", "upstream_loss", "client_disconnect", "max_frames"]
)
def test_every_exit_path_cancels_all_lines(path: str) -> None:
    svc, ib = _service()
    spec = LiveLegsSpec(0, tuple(_opt("NVDA", k) for k in (95.0, 100.0, 105.0)), (_spot("NVDA"),))
    gone = {"flag": False}
    gen = svc.stream_live_legs(
        _Source(spec),
        session_id="s1",
        is_disconnected=lambda: gone["flag"],
        max_frames=2 if path == "max_frames" else None,
    )
    run_coro(anext(gen))
    assert len(ib.open_lines) == 4
    expected: type[BaseException] = StopAsyncIteration
    if path == "stop_all":
        assert svc.stop_all_now() == 4
    elif path == "socket_drop":
        ib.disconnect()
        expected = GatewayConnectionError
    elif path == "upstream_loss":
        ib.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost.", None)
        expected = GatewayConnectionError
    elif path == "client_disconnect":
        gone["flag"] = True
    else:
        run_coro(anext(gen))  # the 2nd (last) frame
    with pytest.raises(expected):
        run_coro(anext(gen))
    _assert_all_released(svc, ib)


def test_cancellation_during_the_frame_interval_cancels_all_lines() -> None:
    svc, ib = _service(sleep=asyncio.sleep)
    spec = LiveLegsSpec(0, (_opt("NVDA", 95.0), _opt("NVDA", 100.0)), (_spot("NVDA"),))

    async def _scenario() -> None:
        gen = svc.stream_live_legs(_Source(spec), session_id="s1")
        await anext(gen)
        task = asyncio.get_running_loop().create_task(_first(gen))
        await asyncio.sleep(0.5)  # the stream sits in its 1 s interval sleep
        assert len(ib.open_lines) == 3
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run_virtual_loop(_scenario())
    _assert_all_released(svc, ib)


def test_gateway_drop_during_a_burst_pause_cancels_the_partial_burst() -> None:
    svc, ib = _service()

    async def _drop_on_pause(sec: float) -> None:
        if math.isclose(sec, PAGE_PACING_PAUSE_SEC):
            ib.disconnect()

    svc.sleep = _drop_on_pause
    spec = LiveLegsSpec(0, tuple(_opt("NVDA", 50.0 + i) for i in range(30)), (_spot("NVDA"),))
    with pytest.raises(GatewayConnectionError):
        run_coro(anext(svc.stream_live_legs(_Source(spec), session_id="s1")))
    assert len(ib.subscribed) == PAGE_PACING_CHUNK_SIZE
    _assert_all_released(svc, ib)


def test_cancellation_during_an_initial_burst_pause_cancels_every_opened_line() -> None:
    svc, ib = _service(sleep=asyncio.sleep)
    # 1 spot + 30 options: a burst pause sits before the 21st reqMktData.
    spec = LiveLegsSpec(0, tuple(_opt("NVDA", 50.0 + i) for i in range(30)), (_spot("NVDA"),))

    async def _scenario() -> None:
        gen = svc.stream_live_legs(_Source(spec), session_id="s1")
        task = asyncio.get_running_loop().create_task(_first(gen))
        await asyncio.sleep(PAGE_PACING_PAUSE_SEC / 2)  # inside the 0.55 s burst pause
        assert len(ib.open_lines) == len(ib.subscribed) == PAGE_PACING_CHUNK_SIZE
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(StopAsyncIteration):
            await anext(gen)

    run_virtual_loop(_scenario())
    assert len(ib.subscribed) == PAGE_PACING_CHUNK_SIZE  # nothing opened after the cancel
    _assert_all_released(svc, ib)


def test_cancellation_during_a_reallocation_burst_pause_cancels_old_and_new_lines() -> None:
    svc, ib = _service(sleep=asyncio.sleep)
    source = _Source(
        LiveLegsSpec(0, tuple(_opt("NVDA", 10.0 + i) for i in range(5)), (_spot("NVDA"),))
    )
    added = tuple(_opt("NVDA", 100.0 + i) for i in range(25))

    async def _scenario() -> None:
        gen = svc.stream_live_legs(source, session_id="s1")
        assert (await anext(gen)).lines_used == 6
        # v1 swaps the 5 options for 25 new ones: 5 cancels + 15 reqMktData fill the first
        # burst, then the pacer pauses before the 16th new line.
        source.spec = LiveLegsSpec(1, added, (_spot("NVDA"),))
        task = asyncio.get_running_loop().create_task(_first(gen))
        await asyncio.sleep(LIVE_LEGS_INTERVAL_SEC + PAGE_PACING_PAUSE_SEC / 2)
        assert len(ib.open_lines) == 1 + 15 and len(ib.subscribed) == 6 + 15
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run_virtual_loop(_scenario())
    assert len(ib.subscribed) == 6 + 15  # nothing opened after the cancel
    _assert_all_released(svc, ib)


def test_stop_during_reallocation_qualification_opens_no_line_afterwards() -> None:
    svc, ib = _service()
    base = (_opt("NVDA", 95.0), _opt("NVDA", 100.0))
    source = _Source(LiveLegsSpec(0, base, (_spot("NVDA"),)))
    gen = svc.stream_live_legs(source, session_id="s1")
    assert run_coro(anext(gen)).lines_used == 3
    stopped: list[int] = []

    def _stop_while_qualifying(*contracts: Any) -> list[Any]:
        stopped.append(svc.stop_all_now())  # e.g. a `refresh_lock` holder's `stop_now`
        for idx, c in enumerate(contracts, start=1):
            c.conId = 20_000 + idx
        return list(contracts)

    ib.qualify_handler = _stop_while_qualifying
    source.spec = LiveLegsSpec(1, (*base, _opt("NVDA", 105.0, conid=0)), (_spot("NVDA"),))
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(gen))
    assert stopped == [3]
    assert len(ib.subscribed) == 3  # the qualified line was never subscribed
    _assert_all_released(svc, ib)


def test_gateway_timeout_while_qualifying_a_missing_conid() -> None:
    cfg = Settings(ibkr=IBKRSettings(request_timeout_sec=1.0))
    svc, ib = _service(settings=cfg)

    async def _hang(*_contracts: Any) -> list[Any]:
        await asyncio.sleep(3600.0)
        return []

    ib.qualifyContractsAsync = _hang  # type: ignore[method-assign]
    # Missing conId at stream start: qualification times out before any line exists.
    unqualified = LiveLegsSpec(0, (_opt("NVDA", 100.0, conid=0),), (_spot("NVDA"),))
    with pytest.raises(GatewayTimeoutError):
        run_virtual_loop(_first(svc.stream_live_legs(_Source(unqualified), session_id="s1")))
    assert ib.subscribed == [] and svc.active_stream_count == 0

    # Missing conId in a re-allocation: the stream fails and every open line is cancelled.
    source = _Source(LiveLegsSpec(0, (_opt("NVDA", 95.0),), (_spot("NVDA"),)))

    async def _scenario() -> None:
        gen = svc.stream_live_legs(source, session_id="s2")
        await anext(gen)
        source.spec = LiveLegsSpec(
            1, (_opt("NVDA", 95.0), _opt("NVDA", 100.0, conid=0)), (_spot("NVDA"),)
        )
        with pytest.raises(GatewayTimeoutError):
            await anext(gen)

    run_virtual_loop(_scenario())
    _assert_all_released(svc, ib)


# ------------------------------------------------------------------ cadence & quotes
def test_frames_follow_a_one_second_cadence_on_the_virtual_clock() -> None:
    svc, ib = _service(sleep=asyncio.sleep)
    spec = LiveLegsSpec(0, (_opt("NVDA", 100.0),), (_spot("NVDA"),))

    async def _scenario() -> list[float]:
        loop = asyncio.get_running_loop()
        stamps: list[float] = []
        async for _frame in svc.stream_live_legs(_Source(spec), session_id="s1", max_frames=4):
            stamps.append(loop.time())
        return stamps

    stamps = run_virtual_loop(_scenario())
    assert [round(b - a, 6) for a, b in zip(stamps, stamps[1:], strict=False)] == [1.0, 1.0, 1.0]
    _assert_all_released(svc, ib)
    with pytest.raises(ValueError, match="interval_sec"):
        run_coro(anext(svc.stream_live_legs(_Source(spec), session_id="s2", interval_sec=0.1)))


class _TypeAwareTicker:
    """Option ticker quoting only once the client cascaded to Type 4 (`priced=False`: never)."""

    def __init__(self, ib: _LegIB, contract: Any, *, priced: bool) -> None:
        self.contract = contract
        self._ib = ib
        self._priced = priced
        self.modelGreeks = None  # noqa: N815

    def _px(self, value: float) -> float:
        current = self._ib.market_data_types[-1] if self._ib.market_data_types else 3
        return value if self._priced and current == 4 else math.nan

    @property
    def bid(self) -> float:
        return self._px(4.4)

    @property
    def ask(self) -> float:
        return self._px(4.8)

    @property
    def last(self) -> float:
        return self._px(4.6)

    @property
    def close(self) -> float:
        return self._px(4.5)


def test_delayed_cascade_notice_bsm_greeks_and_unpriced_lines_keep_cached_quotes() -> None:
    svc, ib = _service(greeks=make_bsm_greeks_fallback(DEFAULT_RISK_FREE_RATE))
    priced, dark = _opt("NVDA", 100.0), _opt("NVDA", 95.0)

    def _factory(contract: Any, _ticks: str) -> Any:
        if getattr(contract, "secType", "") == "STK":
            return SimpleNamespace(contract=contract, last=100.0)
        return _TypeAwareTicker(ib, contract, priced=float(contract.strike) == 100.0)

    ib.ticker_factory = _factory
    gen = svc.stream_live_legs(
        _Source(LiveLegsSpec(0, (priced, dark), (_spot("NVDA"),))), session_id="s1"
    )
    frame = run_coro(anext(gen))
    assert frame.market_data_type == 4
    assert frame.notices == (
        "Live screener Type 3 (Delayed) quotes were >80% empty; cascaded to Type 4 "
        "(Delayed-Frozen).",
    )
    assert frame.live_keys == frozenset({spec_line_key(priced)})
    live_q, dark_q = frame.quotes[spec_line_key(priced)], frame.quotes[spec_line_key(dark)]
    assert (live_q.bid, live_q.ask, live_q.greeks_source) == (4.4, 4.8, "BSM_FALLBACK")
    assert live_q.iv is not None and live_q.delta is not None
    assert (dark_q.bid, dark_q.ask) == (1.0, 1.2)  # cached snapshot quote, flagged not live
    run_coro(gen.aclose())
    _assert_all_released(svc, ib)


def test_build_quote_rows_uses_each_lines_own_spot_for_the_greeks_fallback() -> None:
    """Multi-ticker frames: the BSM fallback runs per line with that ticker's spot; a line
    without a known spot keeps its cached Greeks (the Corridor passes one spot for all)."""
    nvda, avgo, unknown = _opt("NVDA", 100.0), _opt("AVGO", 100.0), _opt("CRDO", 100.0)
    tickers = [
        SimpleNamespace(bid=4.0, ask=4.4, last=4.2, close=4.1, modelGreeks=None) for _ in range(3)
    ]
    rows, priced = build_quote_rows(
        [nvda, avgo, unknown],
        tickers,
        spots=[100.0, 180.0, 0.0],
        updated_ts_utc="2026-11-11T20:00:00Z",
        greeks_fallback=make_bsm_greeks_fallback(DEFAULT_RISK_FREE_RATE),
    )
    assert priced == [True, True, True]
    assert [r.greeks_source for r in rows] == ["BSM_FALLBACK", "BSM_FALLBACK", "BSM_FALLBACK"]
    assert rows[0].delta is not None and rows[1].delta is not None
    # Same put, same price, different spots -> different implied vol / delta.
    assert rows[0].delta != rows[1].delta and rows[0].iv != rows[1].iv
    assert (rows[2].iv, rows[2].delta) == (unknown.cached_iv, unknown.cached_delta)
    with pytest.raises(ValueError, match="spots has 1 entries"):
        build_quote_rows(
            [nvda, avgo],
            tickers[:2],
            spots=[100.0],
            updated_ts_utc="2026-11-11T20:00:00Z",
            greeks_fallback=None,
        )


def test_spec_budgets_outside_their_ranges_are_pacing_violations() -> None:
    spec = LiveLegsSpec(0, (_opt("NVDA", 100.0),), (_spot("NVDA"),))
    spec.validate(CORRIDOR_MAX_CONTRACTS, option_budget=DEFAULT_CORRIDOR_MAX_CONTRACTS)
    with pytest.raises(PacingViolationError, match="between 0 and 90"):
        spec.validate(CORRIDOR_MAX_CONTRACTS + 1, option_budget=DEFAULT_CORRIDOR_MAX_CONTRACTS)
    # The option budget can never exceed the total (options + spots) budget, nor be negative.
    with pytest.raises(PacingViolationError, match="option line budget 51 must be between 0"):
        spec.validate(50, option_budget=51)
    with pytest.raises(PacingViolationError, match="option line budget -1 must be between 0"):
        spec.validate(50, option_budget=-1)


def test_live_legs_qualifies_missing_conids_in_chunks_of_ten() -> None:
    batch_sizes: list[int] = []
    sleeps: list[float] = []

    async def _rec_sleep(sec: float) -> None:
        sleeps.append(sec)

    svc, ib = _service(sleep=_rec_sleep)
    orig_qualify = ib.qualifyContractsAsync

    async def _track_qualify(*contracts: Any) -> list[Any]:
        batch_sizes.append(len(contracts))
        return await orig_qualify(*contracts)

    ib.qualifyContractsAsync = _track_qualify  # type: ignore[method-assign]
    options = tuple(_opt("NVDA", 90.0 + idx, conid=0) for idx in range(14))
    spec = LiveLegsSpec(0, options, (_spot("NVDA"),))
    frame = run_coro(_first(svc.stream_live_legs(_Source(spec), session_id="s-chunk")))
    assert batch_sizes == [QUALIFY_CHUNK_SIZE, 14 - QUALIFY_CHUNK_SIZE]
    assert QUALIFY_CHUNK_PAUSE_SEC in sleeps
    assert len(frame.quotes) == 14
    _assert_all_released(svc, ib)


def test_live_legs_replaces_none_slot_in_qualify_response() -> None:
    svc, ib = _service()

    async def _qualify_with_none(*contracts: Any) -> list[Any]:
        contracts[0].conId = 91_001
        return [contracts[0], None]

    ib.qualifyContractsAsync = _qualify_with_none  # type: ignore[method-assign]
    spec = LiveLegsSpec(
        0,
        (_opt("NVDA", 95.0, conid=0), _opt("NVDA", 100.0, conid=0)),
        (_spot("NVDA"),),
    )
    frame = run_coro(_first(svc.stream_live_legs(_Source(spec), session_id="s-none")))
    sub_contracts = [c for c, *_ in ib.subscribed]
    assert all(c is not None for c in sub_contracts)
    opt_subs = [c for c in sub_contracts if getattr(c, "secType", "") == "OPT"]
    assert [(float(c.strike), int(c.conId)) for c in opt_subs] == [
        (95.0, 91_001),
        (100.0, 0),
    ]
    assert len(frame.quotes) == 2
    _assert_all_released(svc, ib)
