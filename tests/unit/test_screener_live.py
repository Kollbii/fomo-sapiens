"""Live Best Option screener (`ScreenerLiveService` via `AnalyzerService`): allocation, re-pick,
re-allocation, one-live-feed exclusion, `refresh_lock` holders, busy re-check and cleanup.

Socket-free: a real `IBKRBrokerClient` over a quoting `FakeChainIB` (tests/support), real Wall
Corridor service on the same fake, archived BSM-priced snapshots in `tmp_path`. Screener rows are
crafted with Strategy Lab's own evaluation (`evaluate_strategy` with explicit legs) and served by
a stubbed `ScreenerService.run`, so every window, line and RoR below is known exactly. Live mids
are linear in the strike by default, which makes every combination of a window tie on RoR.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

import numpy as np
import pytest
from pydantic import ValidationError

import src.services.ledger as ledger_module
from src.analytics.bsm_fallback import bsm_price
from src.analytics.strategies import PresetStrategyKind, StrategyLegInput
from src.broker.client import IBKRBrokerClient
from src.broker.contracts import ChainPlan, ExpirationMatch, StrikeSelection, UnderlyingInfo
from src.broker.fetcher import PAGE_PACING_PAUSE_SEC, FetchResult
from src.core.config import (
    CorridorSettings,
    LedgerSettings,
    PaperTrackerSettings,
    Settings,
    WatchlistSettings,
)
from src.core.exceptions import (
    GatewayConnectionError,
    LiveStreamNotFoundError,
    RefreshBusyError,
    ScreenerLiveBusyError,
)
from src.services.analyzer import AnalyzerService
from src.services.batch_refresh import BatchRefreshService
from src.services.schemas import (
    BatchRefreshRequest,
    BatchRefreshStatus,
    CorridorQuotesRequest,
    RefreshRequest,
    ScreenerLiveFrame,
    ScreenerLiveReallocateRequest,
    ScreenerLiveRequest,
    ScreenerRow,
    ScreenerRunRequest,
    ScreenerRunResponse,
    ScreenerStatusCounts,
    StrategyEvaluateRequest,
)
from src.services.screener import format_legs_label, min_leg_open_interest
from src.services.screener_live import SCREENER_LIVE_STOP_MESSAGES, ScreenerLiveStream
from src.storage.csv_repository import CSVRepository, make_snapshot_id
from src.storage.ledger_schema import LedgerPositionRow
from src.storage.schema import OptionContractRow
from tests.support.aio import noop_sleep, run_coro, run_virtual_loop
from tests.support.ib_fakes import FakeChainIB

_T = TypeVar("_T")

_TS = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)  # archived snapshots
_NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
_E1 = (_TS.date() + timedelta(days=37)).isoformat()
_E2 = (_TS.date() + timedelta(days=65)).isoformat()
# ticker -> (spot, strike step); archived with spot +- `half_width` steps (5 = 11 strikes) on two
# expiries. AAA / BBB form the default environment; DDD..FFF only the line-budget tests.
_SPOTS: dict[str, tuple[float, float]] = {
    "AAA": (100.0, 5.0),
    "BBB": (50.0, 2.5),
    "DDD": (500.0, 5.0),
    "EEE": (300.0, 5.0),
    "FFF": (200.0, 5.0),
}
_UNDERLYING_CONID = {"AAA": 4065, "BBB": 4066, "DDD": 4067, "EEE": 4068, "FFF": 4069}
_DEFAULT_TICKERS = ("AAA", "BBB")
#: Live mids run linear from `base` strike steps beyond the spot: 4 for AAA / BBB (floored at
#: 0.25 on their wings), 13 for DDD..FFF (linear on a +-12-step grid: no row ever re-picks).
_MID_BASE_STEPS = {"AAA": 4, "BBB": 4}
_WIDE_MID_BASE_STEPS = 13

Leg = tuple[str, str, float, str]  # side, right, strike, expiry
RowDef = tuple[str, PresetStrategyKind, tuple[Leg, ...]]  # ticker, kind, legs
_ROWS: dict[str, RowDef] = {
    "AAA:BULL_PUT_SPREAD": (
        "AAA",
        "BULL_PUT_SPREAD",
        (("SELL", "P", 100.0, _E1), ("BUY", "P", 95.0, _E1)),
    ),
    "AAA:BEAR_PUT_SPREAD": (
        "AAA",
        "BEAR_PUT_SPREAD",
        (("BUY", "P", 105.0, _E1), ("SELL", "P", 100.0, _E1)),
    ),
    "AAA:IRON_CONDOR": (
        "AAA",
        "IRON_CONDOR",
        (
            ("BUY", "P", 90.0, _E1),
            ("SELL", "P", 95.0, _E1),
            ("SELL", "C", 105.0, _E1),
            ("BUY", "C", 110.0, _E1),
        ),
    ),
    "BBB:BULL_PUT_SPREAD": (
        "BBB",
        "BULL_PUT_SPREAD",
        (("SELL", "P", 50.0, _E1), ("BUY", "P", 47.5, _E1)),
    ),
    "AAA:DIAGONAL_SPREAD": (
        "AAA",
        "DIAGONAL_SPREAD",
        (("BUY", "C", 100.0, _E2), ("SELL", "C", 110.0, _E1)),
    ),
    "BBB:BEAR_CALL_SPREAD": (
        "BBB",
        "BEAR_CALL_SPREAD",
        (("SELL", "C", 52.5, _E1), ("BUY", "C", 55.0, _E1)),
    ),
    "BBB:BEAR_PUT_SPREAD": (
        "BBB",
        "BEAR_PUT_SPREAD",
        (("BUY", "P", 52.5, _E1), ("SELL", "P", 50.0, _E1)),
    ),
}
_BULL_PUT = "AAA:BULL_PUT_SPREAD"

LineKey = tuple[Any, ...]


async def _inline(func: Callable[[], _T]) -> _T:
    return func()


def _chain(ticker: str, ts: datetime, *, half_width: int = 5) -> list[dict[str, Any]]:
    """BSM-priced archive (IV 45%, bid / ask +-3%), Greeks left to the BSM enrichment."""
    spot, step = _SPOTS[ticker]
    snap_id = make_snapshot_id(ticker, ts)
    rows: list[dict[str, Any]] = []
    conid = 100_000 * (1 + list(_SPOTS).index(ticker))
    for dte in (37, 65):
        expiry = (_TS.date() + timedelta(days=dte)).isoformat()
        for i in range(-half_width, half_width + 1):
            strike = spot + step * i
            for right in ("C", "P"):
                conid += 1
                px = float(
                    bsm_price(
                        np.array([spot]),
                        np.array([strike]),
                        np.array([dte / 365.0]),
                        0.0425,
                        np.array([0.45]),
                        np.array([right == "C"]),
                    )[0]
                )
                px = max(px, 0.05)
                rows.append(
                    {
                        "snapshot_id": snap_id,
                        "snapshot_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "market_date": ts.date().isoformat(),
                        "market_data_type": 3,
                        "underlying": ticker,
                        "underlying_conid": _UNDERLYING_CONID[ticker],
                        "underlying_price": spot,
                        "option_conid": conid,
                        "expiry": expiry,
                        "dte": dte,
                        "strike": strike,
                        "moneyness_pct": (strike - spot) / spot * 100.0,
                        "right": right,
                        "bid": round(px * 0.97, 2),
                        "ask": round(px * 1.03, 2),
                        "mid": None,
                        "last": round(px, 2),
                        "close": round(px, 2),
                        "mark_price": None,
                        "volume": 250,
                        "open_interest": 1500,
                        "iv": None,
                        "delta": None,
                        "gamma": None,
                        "theta": None,
                        "vega": None,
                        "greeks_source": "NONE",
                    }
                )
    return rows


def _linear_mid(ticker: str, strike: float, right: str) -> float:
    """Live mid linear in the strike (all window combinations tie on RoR), floored at 0.25."""
    spot, step = _SPOTS[ticker]
    base = _MID_BASE_STEPS.get(ticker, _WIDE_MID_BASE_STEPS)
    steps = (
        (strike - (spot - base * step)) / step
        if right == "P"
        else ((spot + base * step) - strike) / step
    )
    return max(0.25, steps)


def _vertical(ticker: str, right: str, lo: int, expiry: str = _E1) -> tuple[str, RowDef]:
    """Credit vertical whose +-1 window is exactly the 4 strikes `lo .. lo + 3` steps off spot.

    Puts: Bull Put SELL `lo + 2` / BUY `lo + 1`; calls: Bear Call SELL `lo + 1` / BUY `lo + 2`.
    """
    spot, step = _SPOTS[ticker]
    k1, k2 = spot + step * (lo + 1), spot + step * (lo + 2)
    row_id = f"{ticker}:{right}{lo:+d}:{'E1' if expiry == _E1 else 'E2'}"
    if right == "P":
        puts = (("SELL", "P", k2, expiry), ("BUY", "P", k1, expiry))
        return row_id, (ticker, "BULL_PUT_SPREAD", puts)
    calls = (("SELL", "C", k1, expiry), ("BUY", "C", k2, expiry))
    return row_id, (ticker, "BEAR_CALL_SPREAD", calls)


def _ckey(contract: Any) -> LineKey:
    if getattr(contract, "secType", "") == "STK":
        return ("STK", contract.symbol)
    raw = str(contract.lastTradeDateOrContractMonth)
    return (
        contract.symbol,
        f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}",
        float(contract.strike),
        contract.right,
    )


class _QuoteIB(FakeChainIB):
    """Quoting fake: spot per ticker, option mid per contract (bid = ask = last = close = mid).

    `set_mid(..., None)` leaves a contract without any IBKR price. Logs sub / cancel in order and
    tracks the open lines (a second open of an open line is recorded as a double subscription).
    """

    def __init__(self) -> None:
        super().__init__()
        self.mids: dict[LineKey, float | None] = {}
        self.tickers: dict[LineKey, Any] = {}
        self.events: list[tuple[str, LineKey]] = []
        self.open_lines: set[LineKey] = set()
        self.max_open = 0
        self.double_subscriptions: list[LineKey] = []
        self.ticker_factory = self._ticker

    def set_mid(
        self, ticker: str, expiry: str, strike: float, right: str, mid: float | None
    ) -> None:
        key = (ticker, expiry, float(strike), right)
        self.mids[key] = mid
        if key in self.tickers:
            self._quote(self.tickers[key], key)

    def _quote(self, tk: Any, key: LineKey) -> None:
        if key[0] == "STK":
            spot = _SPOTS[key[1]][0]
            tk.last, tk.bid, tk.ask, tk.close = spot, spot - 0.05, spot + 0.05, spot
            return
        mid = self.mids.get(key, _linear_mid(key[0], key[2], key[3]))
        px = math.nan if mid is None else mid
        tk.bid = tk.ask = tk.last = tk.close = px

    def _ticker(self, contract: Any, _ticks: str) -> Any:
        tk = SimpleNamespace(
            contract=contract,
            volume=120,
            callOpenInterest=700,
            putOpenInterest=600,
            modelGreeks=None,
        )
        key = _ckey(contract)
        self._quote(tk, key)
        self.tickers[key] = tk
        return tk

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
        self.open_lines.add(key)
        self.max_open = max(self.max_open, len(self.open_lines))
        self.events.append(("sub", key))
        return ticker

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        super().cancelMktData(contract)
        self.open_lines.discard(_ckey(contract))
        self.events.append(("cancel", _ckey(contract)))


class _Fetcher:
    """Chain fetcher stand-in: clock / sleep for the services and a canned chain per refresh."""

    def __init__(self, sleep: Callable[[float], Any]) -> None:
        self.clock = lambda: _NOW
        self.sleep = sleep
        self.calls: list[str] = []

    async def fetch_option_chain(self, ticker: str, **_kwargs: Any) -> FetchResult:
        self.calls.append(ticker)
        spot, step = _SPOTS[ticker]
        rows = [OptionContractRow.model_validate(r) for r in _chain(ticker, _NOW)]
        plan = ChainPlan(
            underlying=UnderlyingInfo(
                symbol=ticker,
                conid=_UNDERLYING_CONID[ticker],
                contract=SimpleNamespace(conId=_UNDERLYING_CONID[ticker]),
                spot=spot,
                spot_source="MID",
            ),
            market_date=_NOW.date(),
            exchange="SMART",
            trading_class=ticker,
            multiplier="100",
            expirations=(
                ExpirationMatch(_E1.replace("-", ""), date.fromisoformat(_E1), 37),
                ExpirationMatch(_E2.replace("-", ""), date.fromisoformat(_E2), 65),
            ),
            strikes=StrikeSelection(
                strikes=tuple(spot + step * i for i in range(-5, 6)),
                atm_strike=spot,
                requested_step=step,
                effective_step=step,
            ),
            range_pct=0.20,
            available_strike_steps=(step,),
            available_range_pcts=(0.20,),
            exchange_validated_strike_count=11,
        )
        return FetchResult(
            snapshot_id=make_snapshot_id(ticker, _NOW),
            snapshot_ts_utc=_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
            market_date=_NOW.date().isoformat(),
            market_data_type=3,
            plan=plan,
            rows=rows,
            pages_fetched=1,
            elapsed_ms=1.0,
        )


class _Env:
    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        hard_max: int = 90,
        sleep: Callable[[float], Any] = noop_sleep,
        gateway_sync: bool = False,
        rows: Sequence[str] = (),
        extra_rows: Sequence[ScreenerRow] = (),
        tickers: Sequence[str] = _DEFAULT_TICKERS,
        half_width: int = 5,
        defs: Mapping[str, RowDef] | None = None,
    ) -> None:
        self.tickers = tuple(tickers)
        self.defs: dict[str, RowDef] = {**_ROWS, **(defs or {})}
        self.settings = Settings(
            watchlist=WatchlistSettings(hardcoded_tickers=self.tickers, max_size=20),
            tracker=PaperTrackerSettings(tracked_dir="data/tracked"),
            ledger=LedgerSettings(ledger_dir="data/ledger", gateway_sync_enabled=gateway_sync),
            corridor=CorridorSettings(
                default_max_contracts=min(88, hard_max), hard_max_contracts=hard_max
            ),
        )
        self.repo = CSVRepository(self.settings.storage, root=tmp_path)
        self.snaps = {
            t: self.repo.save_snapshot_result(
                t, _chain(t, _TS, half_width=half_width), spot_source="MID"
            ).meta.snapshot_id
            for t in self.tickers
        }
        self.ib = _QuoteIB()
        self.client = IBKRBrokerClient(self.settings, ib_factory=lambda: self.ib)
        self.fetcher = _Fetcher(sleep)
        self.svc = AnalyzerService(
            settings=self.settings,
            repo=self.repo,
            broker_client=self.client,
            fetcher=self.fetcher,  # type: ignore[arg-type]
        )
        self.svc.ensure_layout()
        self.svc.watchlist.runner = _inline
        self.svc.screener.runner = _inline
        self.svc.screener_live.runner = _inline
        self.live = self.svc.screener_live
        self.rows = {rid: self.row(rid) for rid in (rows or _ROWS)}
        self.run_calls = 0
        extra = list(extra_rows)

        async def _run(request: ScreenerRunRequest) -> ScreenerRunResponse:
            self.run_calls += 1
            params = self.svc.screener.resolve_params(request)
            listed = [*self.rows.values(), *extra]
            return ScreenerRunResponse(
                mode=params.mode,
                params=params,
                generated_at_utc="2026-11-11T20:00:00Z",
                today_et="2026-11-11",
                tickers=list(self.tickers),
                rows=listed,
                counts=ScreenerStatusCounts(ok=len(self.rows), no_data=len(extra)),
            )

        monkeypatch.setattr(self.svc.screener, "run", _run)

    def row(self, row_id: str) -> ScreenerRow:
        """Screener row = Strategy Lab's own evaluation of the crafted legs on the archive."""
        ticker, kind, legs = self.defs[row_id]
        ev = self.svc.evaluate_strategy(
            StrategyEvaluateRequest(
                ticker=ticker,
                snapshot_id=self.snaps[ticker],
                strategy_kind=kind,
                legs=[
                    StrategyLegInput(side=side, right=right, strike=strike, expiry=expiry)  # type: ignore[arg-type]
                    for side, right, strike, expiry in legs
                ],
            )
        )
        return ScreenerRow(
            row_id=row_id,
            ticker=ticker,
            status="ok",
            strategy_kind=kind,
            snapshot_id=self.snaps[ticker],
            snapshot_ts_utc=_TS.strftime("%Y-%m-%dT%H:%M:%SZ"),
            underlying_price=ev.underlying_price,
            short_expiry=ev.short_expiry,
            short_dte=ev.short_dte,
            long_expiry=ev.long_expiry,
            long_dte=ev.long_dte,
            legs=list(ev.legs),
            legs_label=format_legs_label(ev.legs),
            pricing=ev.pricing,
            risk=ev.risk,
            net_greeks=ev.net_greeks,
            min_leg_open_interest=min_leg_open_interest(ev.legs),
        )

    def open(self, row_ids: Sequence[str], **params: Any) -> ScreenerLiveStream:
        request = ScreenerLiveRequest(row_ids=list(row_ids), **params)
        return run_coro(self.live.open_live_stream(request))

    def data_files(self, root: Path) -> list[tuple[str, int, int]]:
        return sorted(
            (str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob("*")
            if p.is_file()
        )


def _put(ticker: str, strike: float, expiry: str = _E1) -> LineKey:
    return (ticker, expiry, strike, "P")


def _call(ticker: str, strike: float, expiry: str = _E1) -> LineKey:
    return (ticker, expiry, strike, "C")


def _strikes(frame: ScreenerLiveFrame, row_id: str) -> tuple[float, ...]:
    row = next(r for r in frame.rows if r.row_id == row_id)
    return tuple(leg.strike for leg in row.legs)


def _assert_released(env: _Env) -> None:
    assert not env.ib.open_lines
    assert len(env.ib.cancelled) == len(env.ib.subscribed)
    assert env.ib.double_subscriptions == []
    assert env.live.active_stream_count == 0


# ------------------------------------------------------------------ allocation
def test_allocation_walks_rows_in_order_dedupes_shared_strikes_and_respects_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status_row = ScreenerRow(row_id="CCC", ticker="CCC", status="no_data")
    env = _Env(tmp_path, monkeypatch, hard_max=20, extra_rows=[status_row])
    order = [
        "AAA:BULL_PUT_SPREAD",  # 4 lines + AAA spot -> 5
        "AAA:BEAR_PUT_SPREAD",  # shares P95/P100/P105, adds P110 -> 6
        "AAA:IRON_CONDOR",  # shares P90/P95/P100, adds P85 + 4 calls -> 11
        "ZZZ:IRON_CONDOR",  # unknown id: skipped
        "BBB:BULL_PUT_SPREAD",  # 4 lines + BBB spot -> 16
        "CCC",  # status row: never streams
        "AAA:DIAGONAL_SPREAD",  # 3 new E2 calls (E1 short-leg window shared) -> 19
        "BBB:BEAR_CALL_SPREAD",  # 4 new calls -> 23 > 20: not live (budget)
        "BBB:BEAR_PUT_SPREAD",  # shares 3 puts, adds P55 -> 20: still fits (first fit)
        "AAA:BULL_PUT_SPREAD",  # duplicate: ignored
    ]
    stream = env.open(order)
    frame = run_coro(anext(stream))

    assert [r.row_id for r in frame.rows] == [
        "AAA:BULL_PUT_SPREAD",
        "AAA:BEAR_PUT_SPREAD",
        "AAA:IRON_CONDOR",
        "BBB:BULL_PUT_SPREAD",
        "AAA:DIAGONAL_SPREAD",
        "BBB:BEAR_CALL_SPREAD",
        "BBB:BEAR_PUT_SPREAD",
    ]
    status = {r.row_id: r.live_status for r in frame.rows}
    assert status.pop("BBB:BEAR_CALL_SPREAD") == "not_live_budget"
    assert set(status.values()) == {"live"}
    assert frame.lines_used == frame.lines_budget == 20
    assert frame.option_lines_budget == 20  # option cap = min(default_max 20, total 20)
    expected_options = {
        *(_put("AAA", k) for k in (85.0, 90.0, 95.0, 100.0, 105.0, 110.0)),
        *(_call("AAA", k) for k in (100.0, 105.0, 110.0, 115.0)),
        *(_call("AAA", k, _E2) for k in (95.0, 100.0, 105.0)),
        *(_put("BBB", k) for k in (45.0, 47.5, 50.0, 52.5, 55.0)),
    }
    assert env.ib.open_lines == expected_options | {("STK", "AAA"), ("STK", "BBB")}
    assert len(env.ib.subscribed) == 20  # each shared strike subscribed exactly once
    budget_row = next(r for r in frame.rows if r.row_id == "BBB:BEAR_CALL_SPREAD")
    assert budget_row.legs == [] and budget_row.pricing is None
    live_row = next(r for r in frame.rows if r.row_id == "AAA:IRON_CONDOR")
    assert len(live_row.legs) == 4 and live_row.risk is not None and live_row.legs_label

    run_coro(stream.aclose())
    _assert_released(env)


def test_single_ticker_allocation_stops_at_the_88_option_line_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DDD offers 93 candidate option lines: 88 options + 1 spot = 89 lines go live and every
    row needing one more option line is `not_live_budget`, although 90 lines incl. the spot
    would still fit the total - on open and on re-allocation alike."""
    tiles = [  # 22 disjoint 4-strike windows on a 25-strike grid = 88 option lines
        _vertical("DDD", right, lo, expiry)
        for right, expiry, starts in (
            ("P", _E1, (-12, -8, -4, 0, 4, 8)),
            ("C", _E1, (-12, -8, -4, 0, 4, 8)),
            ("P", _E2, (-12, -8, -4, 0, 4, 8)),
            ("C", _E2, (-12, -8, -4, 0)),
        )
        for lo in starts
    ]
    one_more = _vertical("DDD", "P", 9)  # window P+9..P+12: only P+12 (P560) is new
    four_more = _vertical("DDD", "C", 4, _E2)  # four new E2 calls
    no_new = _vertical("DDD", "P", 1)  # window P+1..P+4: every line already allocated
    defs = dict([*tiles, one_more, four_more, no_new])
    order = list(defs)
    env = _Env(tmp_path, monkeypatch, tickers=["DDD"], half_width=12, rows=order, defs=defs)
    stream = env.open(order)
    frame = run_coro(anext(stream))

    status = {r.row_id: r.live_status for r in frame.rows}
    assert list(status) == order
    assert [rid for rid, st in status.items() if st != "live"] == [one_more[0], four_more[0]]
    options = {key for key in env.ib.open_lines if key[0] != "STK"}
    assert len(options) == frame.option_lines_budget == 88
    assert env.ib.open_lines - options == {("STK", "DDD")}
    assert (frame.lines_used, frame.lines_budget, env.ib.max_open) == (89, 90, 89)
    assert _put("DDD", 560.0) not in options

    # Re-allocation walks the new order under both caps: P560 now goes first, so the last C/E2
    # tile (C500..C515) would be the 89th option line (85 + 4) and leaves the live set.
    resp = run_coro(
        env.live.reallocate(
            ScreenerLiveReallocateRequest(stream_id=stream.stream_id, row_ids=[one_more[0], *order])
        )
    )
    assert (resp.lines_planned, resp.lines_budget, resp.option_lines_budget) == (86, 90, 88)
    planned = {r.row_id: r.live_status for r in resp.rows}
    last_tile = _vertical("DDD", "C", 0, _E2)[0]
    assert [rid for rid, st in planned.items() if st != "live"] == [last_tile, four_more[0]]
    mark = len(env.ib.events)
    second = run_coro(anext(stream))
    assert (second.version, second.lines_used) == (1, 86)
    assert env.ib.events[mark:] == [  # cancel-before-subscribe: never above 88 + 1 lines
        *(("cancel", _call("DDD", k, _E2)) for k in (500.0, 505.0, 510.0, 515.0)),
        ("sub", _put("DDD", 560.0)),
    ]
    assert env.ib.max_open == 89
    run_coro(stream.aclose())
    _assert_released(env)


def test_multi_ticker_allocation_caps_the_total_at_90_lines_with_fewer_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three tickers: their spots count against the 90-line total, which binds before the option
    cap - 87 options + 3 spots = 90 lines; a row that fits the 88-option cap but would make
    91 lines is `not_live_budget`."""
    tile_starts = [
        (expiry, right, lo) for expiry in (_E1, _E2) for right in "PC" for lo in (-5, -1)
    ]

    def _tiles(ticker: str, count: int) -> list[tuple[str, RowDef]]:
        """The first `count` of 8 disjoint 4-strike windows on an 11-strike grid."""
        return [_vertical(ticker, right, lo, expiry) for expiry, right, lo in tile_starts[:count]]

    over_total_a = _vertical("FFF", "P", -1, _E2)  # 4 new: 88 options fit, 91 lines do not
    fills_total = _vertical("DDD", "P", 2)  # P+2 shared, 3 new: 87 options + 3 spots = 90
    over_total_b = _vertical("EEE", "P", 0)  # 1 new: 88 options fit, 91 lines do not
    defs = dict(
        [
            *_tiles("DDD", 8),  # 32 options + DDD spot -> 33 lines
            *_tiles("EEE", 8),  # 64 options + 2 spots -> 66 lines
            *_tiles("FFF", 5),  # 84 options + 3 spots -> 87 lines
            over_total_a,
            fills_total,
            over_total_b,
        ]
    )
    order = list(defs)
    env = _Env(tmp_path, monkeypatch, tickers=["DDD", "EEE", "FFF"], rows=order, defs=defs)
    stream = env.open(order)
    frame = run_coro(anext(stream))

    status = {r.row_id: r.live_status for r in frame.rows}
    assert list(status) == order
    assert [rid for rid, st in status.items() if st != "live"] == [
        over_total_a[0],
        over_total_b[0],
    ]
    options = {key for key in env.ib.open_lines if key[0] != "STK"}
    assert env.ib.open_lines - options == {("STK", "DDD"), ("STK", "EEE"), ("STK", "FFF")}
    assert (len(options), frame.option_lines_budget) == (87, 88)
    assert frame.lines_used == env.ib.max_open == frame.lines_budget == 90
    run_coro(stream.aclose())
    _assert_released(env)


def test_open_validates_inputs_and_refuses_while_the_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    with pytest.raises(ValidationError):
        ScreenerLiveRequest(row_ids=["X"] * 121)
    with pytest.raises(ValidationError):
        ScreenerLiveRequest(row_ids=[], unexpected=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ScreenerLiveReallocateRequest(stream_id="", row_ids=[])
    assert ScreenerLiveRequest(mode="all", row_ids=["A"]).run_request() == ScreenerRunRequest(
        mode="all"
    )

    assert run_coro(env.svc.acquire_refresh("batch:1/2:AAA"))
    with pytest.raises(RefreshBusyError) as exc_info:
        env.open([_BULL_PUT])
    assert exc_info.value.owner == "batch:1/2:AAA"
    env.svc.release_refresh()
    assert env.ib.subscribed == []


# ------------------------------------------------------------------ live re-pick
def test_repick_switches_to_a_strictly_higher_ror_neighbour_and_keeps_legs_on_ties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    stream = env.open([_BULL_PUT])

    first = run_coro(anext(stream))  # linear mids: every window combination ties at 25% RoR
    row = first.rows[0]
    assert (row.live_status, row.adjusted, row.keep_reason) == (
        "live",
        False,
        "NO_BETTER_CANDIDATE",
    )
    assert _strikes(first, _BULL_PUT) == (100.0, 95.0)
    assert row.risk is not None and row.risk.return_on_risk_pct == 25.0
    assert row.legs[0].bid == 4.0  # live quote, not the snapshot's
    assert first.market_data_type_label == "Delayed"

    env.ib.set_mid("AAA", _E1, 105.0, "P", 5.5)  # SELL P105 / BUY P100 now earns 1.50 on 5 wide
    second = run_coro(anext(stream))
    row = second.rows[0]
    assert (row.live_status, row.adjusted, row.keep_reason) == ("live", True, None)
    assert _strikes(second, _BULL_PUT) == (105.0, 100.0)
    assert row.legs_label == "−P105 / +P100"
    assert row.risk is not None and row.risk.return_on_risk_pct == 42.9
    assert row.passes_filters is True

    third = run_coro(anext(stream))  # unchanged quotes: the adjusted legs are kept
    assert _strikes(third, _BULL_PUT) == (105.0, 100.0)
    assert third.rows[0].adjusted is True and third.rows[0].keep_reason == "NO_BETTER_CANDIDATE"

    env.ib.set_mid("AAA", _E1, 105.0, "P", 5.0)  # back to linear: ties keep the current legs
    fourth = run_coro(anext(stream))
    assert _strikes(fourth, _BULL_PUT) == (105.0, 100.0)
    assert fourth.rows[0].adjusted is True
    # No line changed: the window stays the subscribed one (re-centres only on re-allocation).
    assert len(env.ib.subscribed) == 5 and env.ib.cancelled == []
    run_coro(stream.aclose())
    _assert_released(env)


def test_repick_applies_the_screener_floors_and_ignores_unquoted_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    stream = env.open([_BULL_PUT], min_ror_pct=30.0)
    run_coro(anext(stream))

    # A better neighbour that still misses min RoR 30% (29.9%) never replaces the legs.
    env.ib.set_mid("AAA", _E1, 105.0, "P", 5.15)
    row = run_coro(anext(stream)).rows[0]
    assert (row.adjusted, row.keep_reason) == (False, "BETTER_CANDIDATE_FILTERED")
    assert row.passes_filters is False  # the current legs (25%) miss the floor live

    # An unquoted neighbour keeps its (much richer) stale snapshot price but is no candidate.
    env.ib.set_mid("AAA", _E1, 105.0, "P", None)
    row = run_coro(anext(stream)).rows[0]
    assert (row.live_status, row.adjusted) == ("live", False)
    assert row.keep_reason in ("NO_BETTER_CANDIDATE", "BETTER_CANDIDATE_FILTERED")

    # A current leg without a live quote: `no_quotes`, last values kept, no re-pick.
    before = row.risk
    env.ib.set_mid("AAA", _E1, 95.0, "P", None)
    row = run_coro(anext(stream)).rows[0]
    assert (row.live_status, row.adjusted) == ("no_quotes", False)
    assert row.risk == before
    run_coro(stream.aclose())
    _assert_released(env)


# ------------------------------------------------------------------ re-allocation
def test_reallocate_applies_only_the_line_diff_on_the_next_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch)
    stream = env.open(["AAA:BULL_PUT_SPREAD", "BBB:BULL_PUT_SPREAD"])
    first = run_coro(anext(stream))
    assert first.lines_used == 10 and first.version == 0

    mark = len(env.ib.events)
    resp = run_coro(
        env.live.reallocate(
            ScreenerLiveReallocateRequest(
                stream_id=stream.stream_id,
                row_ids=["AAA:BEAR_PUT_SPREAD", "BBB:BULL_PUT_SPREAD", "AAA:BULL_PUT_SPREAD"],
            )
        )
    )
    assert (resp.stream_id, resp.version, resp.lines_planned, resp.lines_budget) == (
        stream.stream_id,
        1,
        11,
        90,
    )
    assert resp.option_lines_budget == 88
    assert [(r.row_id, r.live_status) for r in resp.rows] == [
        ("AAA:BEAR_PUT_SPREAD", "live"),
        ("BBB:BULL_PUT_SPREAD", "live"),
        ("AAA:BULL_PUT_SPREAD", "live"),
    ]
    assert env.ib.events[mark:] == []  # applied by the stream, not by the request

    second = run_coro(anext(stream))
    assert second.version == 1 and second.lines_used == 11
    assert env.ib.events[mark:] == [("sub", _put("AAA", 110.0))]  # only the added line

    mark = len(env.ib.events)
    run_coro(
        env.live.reallocate(
            ScreenerLiveReallocateRequest(
                stream_id=stream.stream_id, row_ids=["AAA:BEAR_PUT_SPREAD"]
            )
        )
    )
    third = run_coro(anext(stream))
    assert [r.row_id for r in third.rows] == ["AAA:BEAR_PUT_SPREAD"]
    removed = {
        _put("AAA", 90.0),
        *(_put("BBB", k) for k in (45.0, 47.5, 50.0, 52.5)),
        ("STK", "BBB"),
    }
    diff = env.ib.events[mark:]
    assert {key for kind, key in diff} == removed and {kind for kind, _ in diff} == {"cancel"}
    assert third.lines_used == 5 and env.ib.max_open == 11

    with pytest.raises(LiveStreamNotFoundError):
        run_coro(env.live.reallocate(ScreenerLiveReallocateRequest(stream_id="nope", row_ids=[])))
    assert run_coro(env.svc.acquire_refresh("refresh:AAA"))
    with pytest.raises(RefreshBusyError):
        run_coro(
            env.live.reallocate(
                ScreenerLiveReallocateRequest(stream_id=stream.stream_id, row_ids=[])
            )
        )
    env.svc.release_refresh()
    run_coro(stream.aclose())
    _assert_released(env)
    with pytest.raises(LiveStreamNotFoundError):
        run_coro(
            env.live.reallocate(
                ScreenerLiveReallocateRequest(stream_id=stream.stream_id, row_ids=[])
            )
        )


def test_reallocation_recentres_windows_on_adjusted_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    stream = env.open([_BULL_PUT])
    run_coro(anext(stream))
    env.ib.set_mid("AAA", _E1, 105.0, "P", 5.5)
    assert _strikes(run_coro(anext(stream)), _BULL_PUT) == (105.0, 100.0)

    mark = len(env.ib.events)
    run_coro(
        env.live.reallocate(
            ScreenerLiveReallocateRequest(stream_id=stream.stream_id, row_ids=[_BULL_PUT])
        )
    )
    frame = run_coro(anext(stream))
    # Window re-centred on SELL P105 / BUY P100: P90 leaves, P110 joins (no other change).
    assert env.ib.events[mark:] == [("cancel", _put("AAA", 90.0)), ("sub", _put("AAA", 110.0))]
    assert _strikes(frame, _BULL_PUT) == (105.0, 100.0) and frame.rows[0].adjusted is True
    run_coro(stream.aclose())
    _assert_released(env)


# ------------------------------------------------------------------ one live feed at a time
def test_screener_stream_stops_a_running_corridor_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    corridor = run_coro(
        env.svc.open_corridor_stream(
            "AAA", snapshot_id=env.snaps["AAA"], window_strikes=2, max_contracts=12
        )
    )
    corridor_frame = run_coro(anext(corridor))
    corridor_lines = corridor_frame.subscribed_count
    assert corridor_lines > 0 and env.svc.corridor.active_stream_count == 1

    stream = env.open([_BULL_PUT])
    frame = run_coro(anext(stream))
    assert frame.notices[0] == (
        f"Stopped the live Wall Corridor stream ({corridor_lines} line(s)): one live feed "
        "at a time."
    )
    assert env.svc.corridor.active_stream_count == 0
    assert env.svc.last_corridor_stop is not None
    assert env.svc.last_corridor_stop["reason"] == "screener_stream"
    assert env.ib.double_subscriptions == []  # Corridor lines were gone before ours opened
    assert env.ib.max_open == max(corridor_lines, frame.lines_used)
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(corridor))
    run_coro(stream.aclose())
    _assert_released(env)


def test_corridor_stream_stops_the_screener_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    stream = env.open([_BULL_PUT])
    held = run_coro(anext(stream)).lines_used

    corridor = run_coro(
        env.svc.open_corridor_stream(
            "AAA", snapshot_id=env.snaps["AAA"], window_strikes=2, max_contracts=12
        )
    )
    corridor_frame = run_coro(anext(corridor))
    assert corridor_frame.notices[0] == (
        f"Stopped the live Best Option screener stream ({held} line(s)): one live feed at a time."
    )
    assert env.live.active_stream_count == 0
    assert env.ib.double_subscriptions == []

    stopped = run_coro(anext(stream))
    assert (stopped.stopped, stopped.stop_reason, stopped.rows) == (True, "corridor_stream", [])
    assert stopped.notices == [SCREENER_LIVE_STOP_MESSAGES["corridor_stream"]]
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(stream))
    run_coro(corridor.aclose())
    _assert_released(env)


def test_a_new_screener_stream_supersedes_the_previous_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    old = env.open([_BULL_PUT])
    run_coro(anext(old))
    pending = env.open([_BULL_PUT])  # opened but never iterated: superseded by the next open
    new = env.open([_BULL_PUT])
    mark = len(env.ib.events)
    run_coro(anext(new))
    assert [kind for kind, _ in env.ib.events[mark:]] == ["cancel"] * 5 + ["sub"] * 5
    assert env.ib.max_open == 5 and env.live.active_stream_count == 1
    for gone in (old, pending):
        last = run_coro(anext(gone))
        assert (last.stopped, last.stop_reason) == (True, "superseded")
        with pytest.raises(StopAsyncIteration):
            run_coro(anext(gone))
    run_coro(new.aclose())
    _assert_released(env)


# ------------------------------------------------------------------ refresh_lock holders
def test_single_refresh_stops_the_screener_stream_with_a_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    stream = env.open([_BULL_PUT])
    held = run_coro(anext(stream)).lines_used
    payload = run_coro(env.svc.refresh_chain(RefreshRequest(ticker="AAA")))
    assert env.fetcher.calls == ["AAA"]
    assert (
        f"Stopped the live Best Option screener stream ({held} line(s)) before chain refresh to "
        "stay within IBKR's 100-line ceiling." in payload.meta.notices
    )
    assert not env.ib.open_lines  # freed before the chain pull
    stopped = run_coro(anext(stream))
    assert (stopped.stopped, stopped.stop_reason) == (True, "refresh_chain")
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(stream))
    _assert_released(env)
    assert env.live.last_stop is not None and env.live.last_stop["reason"] == "refresh_chain"


def test_batch_load_stops_the_screener_stream_once_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    batch = BatchRefreshService(env.svc)

    async def _scenario() -> tuple[int, BatchRefreshStatus, ScreenerLiveFrame]:
        stream = await env.live.open_live_stream(ScreenerLiveRequest(row_ids=[_BULL_PUT]))
        held = (await anext(stream)).lines_used
        await batch.start(BatchRefreshRequest(tickers=["AAA", "BBB"]))
        for _ in range(400):  # bounded virtual-time wait for the background batch
            if not batch.is_running:
                break
            await asyncio.sleep(0.25)
        status = batch.current()
        assert status is not None
        stopped = await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return held, status, stopped

    held, status, stopped = run_virtual_loop(_scenario())
    assert status.state == "completed" and env.fetcher.calls == ["AAA", "BBB"]
    assert status.notices == [
        f"Stopped the live Best Option screener stream ({held} line(s)) before batch load to "
        "stay within IBKR's 100-line ceiling."
    ]
    assert (stopped.stopped, stopped.stop_reason) == (True, "batch_refresh")
    _assert_released(env)


def test_gateway_ledger_sync_and_mtm_stop_the_screener_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT], gateway_sync=True)

    async def _gateway(*_a: Any, **_k: Any) -> tuple[list[Any], list[Any]]:
        assert not env.ib.open_lines  # the live screener is gone before Gateway requests
        return [], []

    async def _marks(*_a: Any, **_k: Any) -> dict[int, float]:
        assert not env.ib.open_lines
        return {}

    monkeypatch.setattr(ledger_module, "fetch_gateway_ledger", _gateway)
    monkeypatch.setattr(ledger_module, "refresh_open_option_marks", _marks)

    stream = env.open([_BULL_PUT])
    held = run_coro(anext(stream)).lines_used
    sync = run_coro(env.svc.sync_ledger(mode="auto"))
    assert sync.gateway_synced is True
    assert (
        f"Stopped the live Best Option screener stream ({held} line(s)) before Gateway ledger sync."
        in sync.notices
    )
    assert run_coro(anext(stream)).stop_reason == "ledger"

    env.svc.ledger.repo.replace_positions(
        [
            LedgerPositionRow(
                conid=100_034,
                symbol="AAA 20261218 100 P",
                underlying="AAA",
                asset_class="OPT",
                expiry=_E1,
                strike=100.0,
                right="P",
                multiplier=100,
                position=-1.0,
                avg_cost=3.0,
                source="GATEWAY_LIVE",
                updated_at="2026-11-11T19:00:00Z",
            )
        ]
    )
    stream = env.open([_BULL_PUT])
    held = run_coro(anext(stream)).lines_used
    mtm = run_coro(env.svc.refresh_open_ledger_mtm())
    assert any(
        n.startswith(f"Stopped the live Best Option screener stream ({held} line(s)) before live")
        for n in mtm.notices
    )
    assert run_coro(anext(stream)).stop_reason == "ledger"
    _assert_released(env)


def test_corridor_snapshot_stops_the_screener_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    stream = env.open([_BULL_PUT])
    held = run_coro(anext(stream)).lines_used
    frame = run_coro(
        env.svc.fetch_corridor_quotes(
            CorridorQuotesRequest(
                ticker="AAA", snapshot_id=env.snaps["AAA"], window_strikes=2, max_contracts=12
            )
        )
    )
    assert frame.mode == "ONE_SHOT"
    assert frame.notices[-1] == (
        f"Stopped the live Best Option screener stream ({held} line(s)) before the Wall "
        "Corridor snapshot to stay within IBKR's 100-line ceiling."
    )
    assert env.ib.double_subscriptions == []
    assert run_coro(anext(stream)).stop_reason == "corridor_snapshot"
    _assert_released(env)


def test_lock_taken_before_the_first_read_opens_zero_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    stream = env.open([_BULL_PUT])
    assert run_coro(env.svc.acquire_refresh("batch:1/3:AAA"))
    assert run_coro(env.svc._stop_live_feeds(reason="batch_refresh")).screener_lines == 0
    with pytest.raises(ScreenerLiveBusyError, match="market-data line budget"):
        run_coro(anext(stream))
    assert env.ib.subscribed == [] and env.ib.cancelled == []
    assert env.live.active_stream_count == 0
    assert env.svc.refresh_owner == "batch:1/3:AAA"  # holder untouched
    env.svc.release_refresh()
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(stream))


# ------------------------------------------------------------------ cleanup & display-only
def test_manual_stop_and_gateway_drop_cancel_every_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT, "BBB:BULL_PUT_SPREAD"])
    stream = env.open([_BULL_PUT, "BBB:BULL_PUT_SPREAD"])
    held = run_coro(anext(stream)).lines_used
    stop = run_coro(env.live.stop_live_streams())
    assert (stop.stopped_subscriptions, stop.active_streams) == (held, 0)
    last = run_coro(anext(stream))
    assert (last.stopped, last.stop_reason, last.lines_used) == (True, "manual_stop", 0)
    assert (last.lines_budget, last.option_lines_budget) == (90, 88)
    _assert_released(env)

    stream = env.open([_BULL_PUT])
    run_coro(anext(stream))
    env.ib.disconnect()
    with pytest.raises(GatewayConnectionError):
        run_coro(anext(stream))
    _assert_released(env)
    assert run_coro(env.live.stop_live_streams()).stopped_subscriptions == 0


def test_a_stream_closed_or_stopped_before_its_first_read_is_forgotten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])

    def _reallocate(stream: ScreenerLiveStream) -> None:
        run_coro(
            env.live.reallocate(
                ScreenerLiveReallocateRequest(stream_id=stream.stream_id, row_ids=[_BULL_PUT])
            )
        )

    closed = env.open([_BULL_PUT])
    run_coro(closed.aclose())  # SSE teardown before the first read: the generator never ran
    with pytest.raises(LiveStreamNotFoundError):
        _reallocate(closed)
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(closed))

    unread = env.open([_BULL_PUT])
    assert run_coro(env.live.stop_live_streams()).stopped_subscriptions == 0
    with pytest.raises(LiveStreamNotFoundError):
        _reallocate(unread)
    last = run_coro(anext(unread))
    assert (last.stopped, last.stop_reason) == (True, "manual_stop")

    running = env.open([_BULL_PUT])
    held = run_coro(anext(running)).lines_used
    assert env.live.stop_now(reason="refresh_chain") == held
    with pytest.raises(LiveStreamNotFoundError):
        _reallocate(running)
    assert run_coro(anext(running)).stop_reason == "refresh_chain"
    assert env.live._streams == {}
    assert len(env.ib.subscribed) == held  # only the stream that was read opened lines
    _assert_released(env)


def _ddd_tiles(*starts: tuple[str, int]) -> list[tuple[str, RowDef]]:
    """Disjoint 4-strike DDD windows (E1) on the +-12-step grid: 4 option lines each."""
    return [_vertical("DDD", right, lo) for right, lo in starts]


def _lock_holder_stop(env: _Env, held: list[tuple[int, int]]) -> Callable[[], Any]:
    """A `refresh_lock` holder arriving mid-stream: take the lock, stop every live feed."""

    async def _stop() -> None:
        assert await env.svc.acquire_refresh("refresh:DDD")
        stopped = await env.svc._stop_live_feeds(reason="refresh_chain")
        held.append((stopped.screener_lines, len(env.ib.subscribed)))

    return _stop


def test_lock_holder_stop_during_an_initial_burst_pause_ends_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    defs = dict(_ddd_tiles(*(("P", lo) for lo in (-12, -8, -4, 0, 4, 8))))  # 24 options + spot
    order = list(defs)
    env = _Env(tmp_path, monkeypatch, tickers=["DDD"], half_width=12, rows=order, defs=defs)
    held: list[tuple[int, int]] = []
    stop = _lock_holder_stop(env, held)

    async def _stop_on_burst_pause(sec: float) -> None:
        if math.isclose(sec, PAGE_PACING_PAUSE_SEC) and not held:
            await stop()

    env.live.quotes.sleep = _stop_on_burst_pause
    stream = env.open(order)
    last = run_coro(anext(stream))
    assert (last.stopped, last.stop_reason, last.lines_used) == (True, "refresh_chain", 0)
    assert held == [(20, 20)]  # the first burst (spot + 19 options) was open, then cancelled
    assert len(env.ib.subscribed) == 20  # no line opened after the stop
    with pytest.raises(LiveStreamNotFoundError):
        run_coro(
            env.live.reallocate(
                ScreenerLiveReallocateRequest(stream_id=stream.stream_id, row_ids=order)
            )
        )
    env.svc.release_refresh()
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(stream))
    _assert_released(env)


@pytest.mark.parametrize("stage", ["burst_pause", "qualification"])
def test_lock_holder_stop_during_a_reallocation_ends_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    start = _ddd_tiles(("P", -12))
    swap = _ddd_tiles(*(("P", lo) for lo in (-8, -4, 0, 4, 8)), ("C", -12))  # 24 new options
    defs = dict([*start, *swap])
    env = _Env(tmp_path, monkeypatch, tickers=["DDD"], half_width=12, rows=list(defs), defs=defs)
    stream = env.open([start[0][0]])
    assert run_coro(anext(stream)).lines_used == 5

    held: list[tuple[int, int]] = []
    stop = _lock_holder_stop(env, held)
    quotes = env.live.quotes
    if stage == "burst_pause":

        async def _stop_on_burst_pause(sec: float) -> None:
            if math.isclose(sec, PAGE_PACING_PAUSE_SEC) and not held:
                await stop()

        quotes.sleep = _stop_on_burst_pause
    else:
        resolve = quotes._contracts_for

        async def _stop_while_resolving(*args: Any) -> Any:
            resolved = await resolve(*args)
            await stop()  # the lock holder arrives while the added contracts are resolved
            return resolved

        monkeypatch.setattr(quotes, "_contracts_for", _stop_while_resolving)

    run_coro(
        env.live.reallocate(
            ScreenerLiveReallocateRequest(stream_id=stream.stream_id, row_ids=[r for r, _ in swap])
        )
    )
    last = run_coro(anext(stream))
    assert (last.stopped, last.stop_reason, last.lines_used) == (True, "refresh_chain", 0)
    # 4 cancels first; at the burst pause 16 of the 24 new lines were open (+ the spot).
    expected = (17, 5 + 16) if stage == "burst_pause" else (1, 5)
    assert held == [expected]
    assert len(env.ib.subscribed) == expected[1]  # no line opened after the stop
    env.svc.release_refresh()
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(stream))
    _assert_released(env)


def test_live_stream_never_writes_to_the_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT, "AAA:BEAR_PUT_SPREAD"])
    before = env.data_files(tmp_path)
    stream = env.open([_BULL_PUT])
    run_coro(anext(stream))
    env.ib.set_mid("AAA", _E1, 105.0, "P", 5.5)
    assert run_coro(anext(stream)).rows[0].adjusted is True
    run_coro(
        env.live.reallocate(
            ScreenerLiveReallocateRequest(
                stream_id=stream.stream_id, row_ids=["AAA:BEAR_PUT_SPREAD", _BULL_PUT]
            )
        )
    )
    run_coro(anext(stream))
    run_coro(env.live.stop_live_streams())
    run_coro(anext(stream))
    assert env.data_files(tmp_path) == before
    assert env.svc.corridor_cache._entries == {}  # live quotes never reach Strategy Lab caches


def test_frames_arrive_about_every_second_on_the_virtual_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT], sleep=asyncio.sleep)

    async def _scenario() -> list[float]:
        loop = asyncio.get_running_loop()
        stream = await env.live.open_live_stream(
            ScreenerLiveRequest(row_ids=[_BULL_PUT]), max_frames=4
        )
        stamps: list[float] = []
        async for frame in stream:
            assert not frame.stopped
            stamps.append(loop.time())
        return stamps

    stamps = run_virtual_loop(_scenario())
    assert [round(b - a, 6) for a, b in zip(stamps, stamps[1:], strict=False)] == [1.0, 1.0, 1.0]
    _assert_released(env)


def test_rapid_reallocations_coalesce_into_one_diff_to_the_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch)
    stream = env.open(["AAA:BULL_PUT_SPREAD"])
    run_coro(anext(stream))
    versions = [
        run_coro(
            env.live.reallocate(
                ScreenerLiveReallocateRequest(stream_id=stream.stream_id, row_ids=row_ids)
            )
        ).version
        for row_ids in (["AAA:BEAR_PUT_SPREAD"], ["BBB:BULL_PUT_SPREAD"], ["AAA:IRON_CONDOR"])
    ]
    assert versions == [1, 2, 3]
    mark = len(env.ib.events)
    frame = run_coro(anext(stream))
    assert frame.version == 3 and [r.row_id for r in frame.rows] == ["AAA:IRON_CONDOR"]
    diff = env.ib.events[mark:]
    # Straight from v0 to v3 (v1 / v2 never applied): BBB was never subscribed.
    assert all(key[0] != "BBB" and key != ("STK", "BBB") for _, key in diff)
    # Bull Put window P90-P105 -> condor window P85-P100 + C100-C115: one cancel, five adds.
    assert diff[0] == ("cancel", _put("AAA", 105.0))
    assert {key for _, key in diff[1:]} == {
        _put("AAA", 85.0),
        *(_call("AAA", k) for k in (100.0, 105.0, 110.0, 115.0)),
    }
    assert [kind for kind, _ in diff[1:]] == ["sub"] * 5
    assert frame.lines_used == 9
    run_coro(stream.aclose())
    _assert_released(env)


def test_a_stop_arriving_during_a_repick_yields_the_stopped_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _Env(tmp_path, monkeypatch, rows=[_BULL_PUT])
    stream = env.open([_BULL_PUT])
    run_coro(anext(stream))
    env.ib.set_mid("AAA", _E1, 105.0, "P", 5.5)  # changed inputs: the next tick re-picks

    async def _stop_while_computing(func: Callable[[], _T]) -> _T:
        env.live.stop_now(reason="refresh_chain")  # e.g. a refresh took the lock meanwhile
        return func()

    env.live.runner = _stop_while_computing
    frame = run_coro(anext(stream))
    assert (frame.stopped, frame.stop_reason, frame.rows) == (True, "refresh_chain", [])
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(stream))
    _assert_released(env)
