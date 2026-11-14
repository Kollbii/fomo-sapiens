from __future__ import annotations

import math
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import numpy as np
import polars as pl
import pytest
from ib_async import Contract

from src.analytics.bsm_fallback import DAYS_PER_YEAR, DEFAULT_RISK_FREE_RATE, bsm_greeks, bsm_price
from src.analytics.walls import compute_walls
from src.broker.client import IBKRBrokerClient
from src.broker.corridor import (
    CORRIDOR_MAX_CONTRACTS,
    DEFAULT_CORRIDOR_MAX_CONTRACTS,
    DEFAULT_WINDOW_STRIKES,
    CorridorContractSpec,
    CorridorLegRef,
    CorridorQuoteService,
    CorridorSelection,
    corridor_quote_key,
    select_wall_corridor,
)
from src.broker.fetcher import PAGE_PACING_CHUNK_SIZE, PAGE_PACING_PAUSE_SEC
from src.core.config import IBKRSettings, Settings
from src.core.exceptions import GatewayConnectionError, PacingViolationError
from src.services.corridor import make_bsm_greeks_fallback, wall_anchors_from_report
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, OptionContractRow
from tests.support.aio import noop_sleep, run_coro
from tests.support.ib_fakes import FakeEvent


class _FakeIB:
    """Deterministic `ib_async.IB` mock tracking `qualifyContractsAsync` and `cancelMktData`."""

    def __init__(self) -> None:
        self._connected = False
        self.errorEvent = FakeEvent()
        self.wrapper = SimpleNamespace(clientId=0)
        self.market_data_types: list[int] = []
        self.qualify_calls = 0
        self.subscribed: list[tuple[Any, str, bool, bool]] = []
        self.cancelled: list[Any] = []
        self.tickers_by_conid: dict[int, Any] = {}
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

    async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]:  # noqa: N802
        self.qualify_calls += 1
        out: list[Any] = []
        for idx, c in enumerate(contracts, start=1):
            if int(getattr(c, "conId", 0) or 0) <= 0:
                c.conId = 90_000 + idx
            out.append(c)
        return out

    def reqMktData(  # noqa: N802
        self,
        contract: Any,
        genericTickList: str = "",  # noqa: N803
        snapshot: bool = False,
        regulatorySnapshot: bool = False,  # noqa: N803
    ) -> Any:
        self.subscribed.append((contract, genericTickList, snapshot, regulatorySnapshot))
        if self.ticker_factory is not None:
            tk = self.ticker_factory(contract, genericTickList, self.market_data_types)
        elif getattr(contract, "secType", "") == "STK":
            tk = SimpleNamespace(
                contract=contract,
                marketPrice=lambda: 100.0,
                last=100.0,
                close=99.8,
                bid=99.9,
                ask=100.1,
                delayedLast=100.0,
                delayedClose=99.8,
                delayedBid=99.9,
                delayedAsk=100.1,
            )
        else:
            tk = SimpleNamespace(
                contract=contract,
                bid=5.0,
                ask=5.4,
                last=5.2,
                close=5.1,
                volume=320,
                callOpenInterest=1500,
                putOpenInterest=1400,
                modelGreeks=None,
            )
        con_id = int(getattr(contract, "conId", 0) or 0)
        self.tickers_by_conid[con_id] = tk
        return tk

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        self.cancelled.append(contract)


def _build_chain_df(
    *,
    strikes: list[float] | None = None,
    expiries: list[tuple[str, int]] | None = None,
    spot: float = 100.0,
    put_wall: float = 88.0,
    call_wall: float = 112.0,
) -> pl.DataFrame:
    strike_list = strikes or [float(60 + 2 * i) for i in range(32)]
    exp_list = expiries or [("2026-12-19", 38), ("2027-01-16", 66)]
    rows: list[dict[str, Any]] = []
    conid = 700_000
    for expiry, dte in exp_list:
        t_years = float(dte) / DAYS_PER_YEAR
        for strike in strike_list:
            for right in ("C", "P"):
                conid += 1
                is_call = right == "C"
                s_arr = np.array([spot], dtype=np.float64)
                k_arr = np.array([strike], dtype=np.float64)
                t_arr = np.array([t_years], dtype=np.float64)
                iv_arr = np.array([0.35], dtype=np.float64)
                c_arr = np.array([is_call], dtype=np.bool_)
                theo = float(
                    bsm_price(s_arr, k_arr, t_arr, DEFAULT_RISK_FREE_RATE, iv_arr, c_arr)[0]
                )
                d_arr, g_arr, th_arr, v_arr = bsm_greeks(
                    s_arr, k_arr, t_arr, DEFAULT_RISK_FREE_RATE, iv_arr, c_arr
                )
                oi = 250
                vol = 60
                if right == "P" and math.isclose(strike, put_wall):
                    oi = 18_000
                    vol = 4_200
                elif right == "C" and math.isclose(strike, call_wall):
                    oi = 16_000
                    vol = 3_900
                rows.append(
                    OptionContractRow(
                        snapshot_id="NVDA_20261111_160000",
                        snapshot_ts_utc="2026-11-11T16:00:00Z",
                        market_date="2026-11-11",
                        market_data_type=3,
                        underlying="NVDA",
                        underlying_conid=4815,
                        underlying_price=spot,
                        option_conid=conid,
                        expiry=expiry,
                        dte=dte,
                        strike=strike,
                        moneyness_pct=((strike - spot) / spot) * 100.0,
                        right=right,
                        bid=round(max(0.05, theo - 0.15), 2),
                        ask=round(max(0.10, theo + 0.15), 2),
                        mid=round(max(0.08, theo), 2),
                        last=round(max(0.08, theo), 2),
                        close=round(max(0.08, theo), 2),
                        mark_price=round(max(0.08, theo), 2),
                        volume=vol,
                        open_interest=oi,
                        iv=0.35,
                        delta=float(d_arr[0]),
                        gamma=float(g_arr[0]),
                        theta=float(th_arr[0]),
                        vega=float(v_arr[0]),
                        greeks_source="BSM_FALLBACK",
                    ).model_dump()
                )
    return pl.DataFrame(rows, schema=OPTION_CHAIN_POLARS_SCHEMA)


def _select(df: pl.DataFrame, **kwargs: Any) -> CorridorSelection:
    """Service-equivalent selection: anchors derived from the analytics walls report."""
    anchors = wall_anchors_from_report(compute_walls(df, top_n=3))
    return select_wall_corridor(df, wall_anchors=anchors, **kwargs)


def test_select_wall_corridor_caps_at_90_and_prioritizes_walls_and_legs() -> None:
    df = _build_chain_df()
    priority_leg = CorridorLegRef(expiry="2027-01-16", strike=100.0, right="C")

    sel = _select(
        df,
        target_expiry="2026-12-19",
        window_strikes=5,
        max_contracts=56,
        priority_legs=[priority_leg],
    )
    assert CORRIDOR_MAX_CONTRACTS == 90
    assert DEFAULT_CORRIDOR_MAX_CONTRACTS == 88
    assert DEFAULT_WINDOW_STRIKES == 10
    assert sel.ticker == "NVDA"
    assert sel.target_expiry == "2026-12-19"
    assert sel.primary_put_wall == pytest.approx(88.0)
    assert sel.primary_call_wall == pytest.approx(112.0)
    assert 1 <= len(sel.specs) <= 56 <= CORRIDOR_MAX_CONTRACTS

    keys = {corridor_quote_key(s.expiry, s.strike, s.right) for s in sel.specs}
    assert corridor_quote_key("2027-01-16", 100.0, "C") in keys
    assert corridor_quote_key("2026-12-19", 88.0, "P") in keys
    assert corridor_quote_key("2026-12-19", 88.0, "C") in keys
    assert corridor_quote_key("2026-12-19", 112.0, "C") in keys
    assert corridor_quote_key("2026-12-19", 112.0, "P") in keys

    leg_spec = next(
        s for s in sel.specs if s.expiry == "2027-01-16" and s.strike == 100.0 and s.right == "C"
    )
    assert leg_spec.is_strategy_leg is True
    assert leg_spec.zone == "STRATEGY_LEG"

    # Secondary DTE Tier 2 includes ±min(2, window_strikes) around secondary walls with both rights.
    sec_specs = [
        s for s in sel.specs if s.expiry == "2027-01-16" and s.zone in ("PUT_WALL", "CALL_WALL")
    ]
    assert sec_specs
    sec_by_strike: dict[float, set[str]] = {}
    for s in sec_specs:
        sec_by_strike.setdefault(s.strike, set()).add(s.right)
    assert sec_by_strike[88.0] == {"C", "P"}
    assert sec_by_strike[112.0] == {"C", "P"}

    # Exceeding CORRIDOR_MAX_CONTRACTS (e.g. 91) or <= 0 raises PacingViolationError.
    with pytest.raises(PacingViolationError):
        _select(df, target_expiry="2026-12-19", window_strikes=10, max_contracts=91)
    with pytest.raises(PacingViolationError):
        _select(df, target_expiry="2026-12-19", window_strikes=10, max_contracts=0)


def test_select_wall_corridor_disjoint_walls_pairs_call_and_put_at_88_cap() -> None:
    strikes = [float(130 + i) for i in range(91)]
    df = _build_chain_df(
        strikes=strikes,
        expiries=[("2026-12-19", 38)],
        spot=175.0,
        put_wall=150.0,
        call_wall=200.0,
    )
    priority_legs = [
        CorridorLegRef(expiry="2026-12-19", strike=135.0, right="P"),
        CorridorLegRef(expiry="2026-12-19", strike=215.0, right="C"),
    ]
    sel = _select(
        df,
        target_expiry="2026-12-19",
        window_strikes=10,
        max_contracts=88,
        priority_legs=priority_legs,
    )
    assert sel.window_strikes == 10
    assert sel.max_contracts == 88
    assert len(sel.specs) == 88
    assert sel.primary_put_wall == pytest.approx(150.0)
    assert sel.primary_call_wall == pytest.approx(200.0)

    tier1_strikes = set(range(140, 161)) | set(range(190, 211))
    assert len(tier1_strikes) == 42

    by_strike: dict[float, set[str]] = {}
    for spec in sel.specs:
        by_strike.setdefault(spec.strike, set()).add(spec.right)

    tier1_specs = [s for s in sel.specs if int(round(s.strike)) in tier1_strikes]
    assert len(tier1_specs) == 84
    for strike_num in tier1_strikes:
        assert by_strike[float(strike_num)] == {"C", "P"}

    leg_keys = {(s.strike, s.right) for s in sel.specs if s.is_strategy_leg}
    assert leg_keys == {(135.0, "P"), (215.0, "C")}

    atm_fill = [s for s in sel.specs if s.zone == "ATM"]
    assert [(s.strike, s.right) for s in atm_fill] == [(175.0, "C"), (175.0, "P")]


def test_prepare_contracts_raises_pacing_error_above_90() -> None:
    fake_ib = _FakeIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    service = CorridorQuoteService(client, sleep=noop_sleep)

    def _make_spec(i: int) -> CorridorContractSpec:
        return CorridorContractSpec(
            option_conid=1000 + i,
            symbol="NVDA",
            expiry="2026-12-19",
            ib_expiry="20261219",
            dte=38,
            strike=90.0 + i,
            right="P",
            zone="PUT_WALL",
            is_put_wall=(i == 0),
            is_call_wall=False,
            is_strategy_leg=False,
            cached_bid=1.0,
            cached_ask=1.2,
            cached_mid=1.1,
            cached_last=1.1,
            cached_close=1.1,
            cached_volume=10,
            cached_open_interest=100,
            cached_iv=0.35,
            cached_delta=-0.2,
            cached_gamma=0.01,
            cached_theta=-0.02,
            cached_vega=0.1,
            cached_greeks_source="BSM_FALLBACK",
        )

    def _make_sel(count: int) -> CorridorSelection:
        return CorridorSelection(
            ticker="NVDA",
            underlying_conid=4815,
            underlying_price=100.0,
            target_expiry="2026-12-19",
            target_dte=38,
            window_strikes=10,
            max_contracts=min(count, CORRIDOR_MAX_CONTRACTS),
            primary_put_wall=90.0,
            primary_call_wall=110.0,
            wall_basis="OPEN_INTEREST",
            atm_strike=100.0,
            specs=tuple(_make_spec(i) for i in range(count)),
        )

    with pytest.raises(PacingViolationError):
        run_coro(service._prepare_contracts(_make_sel(CORRIDOR_MAX_CONTRACTS + 1)))

    # 88 option contracts + 1 spot = 89 <= 90 (10-line IBKR buffer preserved)
    opts_88, spot_88 = run_coro(service._prepare_contracts(_make_sel(88)))
    assert len(opts_88) == 88
    assert spot_88 is not None

    # 90 option contracts leaves zero room for spot -> spot_contract omitted (90 <= 90)
    opts_90, spot_90 = run_coro(service._prepare_contracts(_make_sel(90)))
    assert len(opts_90) == 90
    assert spot_90 is None


def test_fetch_corridor_quotes_reuses_conids_and_cancels_100pct() -> None:
    df = _build_chain_df(
        strikes=[85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0],
        put_wall=90.0,
        call_wall=110.0,
    )
    fake_ib = _FakeIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    service = CorridorQuoteService(
        client,
        clock=lambda: datetime(2026, 11, 11, 16, 5, 0, tzinfo=UTC),
        sleep=noop_sleep,
    )

    frame = run_coro(
        service.fetch_corridor_quotes(
            _select(df, target_expiry="2026-12-19", window_strikes=3, max_contracts=40),
            snapshot_id="NVDA_20261111_160000",
        )
    )
    assert fake_ib.qualify_calls == 0
    assert frame.mode == "ONE_SHOT"
    assert frame.market_data_type == 3
    assert 1 <= frame.subscribed_count <= 40
    assert len(fake_ib.cancelled) == frame.subscribed_count
    assert service.active_stream_count == 0
    assert all(isinstance(c, Contract) or getattr(c, "conId", 0) > 0 for c in fake_ib.cancelled)


def test_fetch_corridor_quotes_falls_back_to_type4_and_computes_bsm_greeks() -> None:
    df = _build_chain_df(strikes=[85.0, 90.0, 100.0, 110.0, 115.0], put_wall=90.0, call_wall=110.0)
    fake_ib = _FakeIB()

    def _dynamic_ticker(contract: Any, _ticks: str, mode_history: list[int]) -> Any:
        if getattr(contract, "secType", "") == "STK":
            return SimpleNamespace(contract=contract, delayedLast=100.0, marketPrice=lambda: 100.0)
        tk = SimpleNamespace(
            contract=contract,
            bid=float("nan"),
            ask=float("nan"),
            last=float("nan"),
            close=float("nan"),
            volume=0,
            callOpenInterest=0,
            putOpenInterest=0,
            modelGreeks=None,
        )

        class _ModeAwareTicker:
            def __getattr__(self, name: str) -> Any:
                current_mode = mode_history[-1] if mode_history else 3
                if current_mode == 4:
                    if name == "bid":
                        return 4.4
                    if name == "ask":
                        return 4.8
                    if name == "last":
                        return 4.6
                    if name == "volume":
                        return 250
                    if name in ("callOpenInterest", "putOpenInterest"):
                        return 1200
                return getattr(tk, name)

        return _ModeAwareTicker()

    fake_ib.ticker_factory = _dynamic_ticker
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    service = CorridorQuoteService(
        client,
        sleep=noop_sleep,
        greeks_fallback=make_bsm_greeks_fallback(DEFAULT_RISK_FREE_RATE),
    )

    frame = run_coro(
        service.fetch_corridor_quotes(
            _select(df, target_expiry="2026-12-19", window_strikes=2, max_contracts=30),
            snapshot_id="NVDA_20261111_160000",
        )
    )
    assert frame.market_data_type == 4
    assert any("Type 4" in n for n in frame.notices)
    assert all(q.greeks_source == "BSM_FALLBACK" for q in frame.quotes)
    assert all(q.iv is not None and q.delta is not None for q in frame.quotes)
    assert len(fake_ib.cancelled) == len(fake_ib.subscribed)
    assert frame.subscribed_count * 2 == len(fake_ib.cancelled)


def test_stream_corridor_quotes_lifecycle_cleanup_and_disconnect() -> None:
    df = _build_chain_df(strikes=[85.0, 90.0, 100.0, 110.0, 115.0], put_wall=90.0, call_wall=110.0)
    fake_ib = _FakeIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    service = CorridorQuoteService(client, sleep=noop_sleep)

    # 1) Two frames with a live quote tick mutation between them -> normal termination cleanup
    async def _collect_two() -> list[Any]:
        out: list[Any] = []
        async for frm in service.stream_corridor_quotes(
            _select(df, target_expiry="2026-12-19", window_strikes=2, max_contracts=30),
            snapshot_id="NVDA_20261111_160000",
            max_frames=2,
        ):
            out.append(frm)
            # Mutate bid/ask on first option ticker before second frame is polled
            opt_conid = frm.quotes[0].option_conid
            tk = fake_ib.tickers_by_conid[opt_conid]
            tk.bid = 6.5
            tk.ask = 6.9
        return out

    frames = run_coro(_collect_two())
    assert len(frames) == 2
    assert frames[0].sequence == 1
    assert frames[1].sequence == 2
    assert frames[1].mode == "STREAM"
    assert frames[1].quotes[0].bid == pytest.approx(6.5)
    assert (
        corridor_quote_key(
            frames[1].quotes[0].expiry, frames[1].quotes[0].strike, frames[1].quotes[0].right
        )
        in frames[1].changed_keys
    )
    assert len(fake_ib.cancelled) == frames[0].subscribed_count
    assert service.active_stream_count == 0

    # 2) SSE client disconnect (`await gen.aclose()`) -> 100% cancelMktData
    fake_ib.subscribed.clear()
    fake_ib.cancelled.clear()
    gen = service.stream_corridor_quotes(
        _select(df, target_expiry="2026-12-19", window_strikes=2, max_contracts=30),
        snapshot_id="NVDA_20261111_160000",
    )
    first_frame = run_coro(anext(gen))
    assert service.active_stream_count == 1
    run_coro(gen.aclose())
    assert service.active_stream_count == 0
    assert len(fake_ib.cancelled) == first_frame.subscribed_count

    # 3) Explicit stop_all_streams() -> exits loop cleanly with 100% cancelMktData
    fake_ib.subscribed.clear()
    fake_ib.cancelled.clear()
    gen2 = service.stream_corridor_quotes(
        _select(df, target_expiry="2026-12-19", window_strikes=2, max_contracts=30),
        snapshot_id="NVDA_20261111_160000",
    )
    frame2 = run_coro(anext(gen2))
    stopped = run_coro(service.stop_all_streams())
    assert stopped == frame2.subscribed_count
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(gen2))
    assert len(fake_ib.cancelled) == frame2.subscribed_count
    assert service.active_stream_count == 0

    # 4) Gateway disconnects mid-stream -> GatewayConnectionError + 100% cancelMktData
    fake_ib.subscribed.clear()
    fake_ib.cancelled.clear()
    gen3 = service.stream_corridor_quotes(
        _select(df, target_expiry="2026-12-19", window_strikes=2, max_contracts=30),
        snapshot_id="NVDA_20261111_160000",
    )
    frame3 = run_coro(anext(gen3))
    fake_ib.disconnect()
    with pytest.raises(GatewayConnectionError):
        run_coro(anext(gen3))
    assert len(fake_ib.cancelled) == frame3.subscribed_count
    assert service.active_stream_count == 0


def test_corridor_live_mode_falls_back_type1_to_type2_dynamically() -> None:
    df = _build_chain_df(strikes=[90.0, 100.0, 110.0], put_wall=90.0, call_wall=110.0)
    live_settings = Settings(
        ibkr=IBKRSettings(
            market_data_mode="LIVE",
            default_market_data_type=1,
            fallback_market_data_type=2,
        )
    )
    fake_ib = _FakeIB()

    def _live_frozen_ticker(contract: Any, _ticks: str, mode_history: list[int]) -> Any:
        if getattr(contract, "secType", "") == "STK":
            return SimpleNamespace(contract=contract, last=100.0, marketPrice=lambda: 100.0)
        tk = SimpleNamespace(
            contract=contract,
            marketDataType=1,
            bid=float("nan"),
            ask=float("nan"),
            last=float("nan"),
            close=float("nan"),
            volume=0,
            callOpenInterest=0,
            putOpenInterest=0,
            modelGreeks=None,
        )

        class _FrozenAware:
            def __getattr__(self, name: str) -> Any:
                cur = mode_history[-1] if mode_history else 1
                if cur == 2:
                    if name == "marketDataType":
                        return 2
                    if name in ("bid", "ask", "last", "close"):
                        return 5.0
                return getattr(tk, name)

        return _FrozenAware()

    fake_ib.ticker_factory = _live_frozen_ticker
    client = IBKRBrokerClient(live_settings, ib_factory=lambda: fake_ib)
    service = CorridorQuoteService(client, live_settings, sleep=noop_sleep)

    frame = run_coro(
        service.fetch_corridor_quotes(
            _select(df, target_expiry="2026-12-19", window_strikes=1, max_contracts=20),
            snapshot_id="NVDA_20261111_160000",
        )
    )
    assert frame.market_data_type == 2
    assert any("Type 1" in n and "Type 2" in n for n in frame.notices)
    assert fake_ib.market_data_types[-2:] == [1, 2]
    assert len(fake_ib.cancelled) == len(fake_ib.subscribed)


def test_corridor_without_injected_greeks_fallback_keeps_cached_snapshot_greeks() -> None:
    """The broker never computes Greeks itself; without a fallback cached values are reused."""
    df = _build_chain_df(strikes=[90.0, 100.0, 110.0], put_wall=90.0, call_wall=110.0)
    fake_ib = _FakeIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    service = CorridorQuoteService(client, sleep=noop_sleep)

    frame = run_coro(
        service.fetch_corridor_quotes(
            _select(df, target_expiry="2026-12-19", window_strikes=1, max_contracts=10),
            snapshot_id="NVDA_20261111_160000",
        )
    )
    cached = {(r["expiry"], r["strike"], r["right"]): r for r in df.to_dicts()}
    assert frame.quotes
    for q in frame.quotes:
        row = cached[(q.expiry, q.strike, q.right)]
        assert q.iv == pytest.approx(row["iv"])
        assert q.delta == pytest.approx(row["delta"])
        assert q.greeks_source == row["greeks_source"]
    assert len(fake_ib.cancelled) == len(fake_ib.subscribed)


def test_corridor_burst_pacing_20_call_chunks_and_cleanup() -> None:
    strikes = [float(130 + i) for i in range(60)]
    df = _build_chain_df(
        strikes=strikes,
        expiries=[("2026-12-19", 38)],
        spot=160.0,
        put_wall=150.0,
        call_wall=170.0,
    )
    fake_ib = _FakeIB()
    sleep_calls: list[float] = []

    async def _record_sleep(sec: float) -> None:
        sleep_calls.append(sec)

    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    service = CorridorQuoteService(client, sleep=_record_sleep)

    # 45 option contracts + 1 spot = 46 reqMktData calls -> chunks of [20, 20, 6] => 2 sleeps
    sel_45 = _select(df, target_expiry="2026-12-19", window_strikes=10, max_contracts=45)
    assert len(sel_45.specs) == 45
    frame = run_coro(service.fetch_corridor_quotes(sel_45, snapshot_id="NVDA_20261111_160000"))
    assert frame.subscribed_count == 46
    assert frame.subscription_cap == 90
    assert len(fake_ib.subscribed) == 46
    assert len(fake_ib.cancelled) == 46
    assert sleep_calls == [PAGE_PACING_PAUSE_SEC, PAGE_PACING_PAUSE_SEC]
    assert service.active_stream_count == 0

    # Exact multiple of 20 (39 options + 1 spot = 40 calls): 1 chunk sleep, no trailing sleep
    fake_ib.subscribed.clear()
    fake_ib.cancelled.clear()
    sleep_calls.clear()
    sel_39 = _select(df, target_expiry="2026-12-19", window_strikes=10, max_contracts=39)
    frame_40 = run_coro(service.fetch_corridor_quotes(sel_39, snapshot_id="NVDA_20261111_160000"))
    assert frame_40.subscribed_count == 40
    assert len(fake_ib.cancelled) == 40
    assert sleep_calls == [PAGE_PACING_PAUSE_SEC]


def test_corridor_mid_subscribe_stop_and_disconnect_cancel_partial_batch() -> None:
    strikes = [float(130 + i) for i in range(60)]
    df = _build_chain_df(
        strikes=strikes,
        expiries=[("2026-12-19", 38)],
        spot=160.0,
        put_wall=150.0,
        call_wall=170.0,
    )
    sel_45 = _select(df, target_expiry="2026-12-19", window_strikes=10, max_contracts=45)

    # 1) Stop signal arrives during first 20-call chunk pause -> breaks & cancels 20 partial subs
    fake_ib = _FakeIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)

    async def _stop_on_pause(sec: float) -> None:
        if math.isclose(sec, PAGE_PACING_PAUSE_SEC):
            for sess in service._active_sessions.values():
                sess.stop_event.set()

    service = CorridorQuoteService(client, sleep=_stop_on_pause)
    gen = service.stream_corridor_quotes(sel_45, snapshot_id="NVDA_20261111_160000")
    with pytest.raises(StopAsyncIteration):
        run_coro(anext(gen))
    assert len(fake_ib.subscribed) == PAGE_PACING_CHUNK_SIZE
    assert len(fake_ib.cancelled) == PAGE_PACING_CHUNK_SIZE
    assert service.active_stream_count == 0

    # 2) Gateway disconnect during first chunk pause -> GatewayConnectionError & cancels 20 subs
    fake_ib_disc = _FakeIB()
    client_disc = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib_disc)

    async def _disconnect_on_pause(sec: float) -> None:
        if math.isclose(sec, PAGE_PACING_PAUSE_SEC):
            fake_ib_disc.disconnect()

    service_disc = CorridorQuoteService(client_disc, sleep=_disconnect_on_pause)
    with pytest.raises(GatewayConnectionError):
        run_coro(service_disc.fetch_corridor_quotes(sel_45, snapshot_id="NVDA_20261111_160000"))
    assert len(fake_ib_disc.subscribed) == PAGE_PACING_CHUNK_SIZE
    assert len(fake_ib_disc.cancelled) == PAGE_PACING_CHUNK_SIZE
    assert service_disc.active_stream_count == 0
