"""Unit tests for IBKR 100-line concurrency lock, pre-refresh corridor stop, and MTM sync."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.analytics.strategies import StrategyLegInput
from src.broker.contracts import ChainPlan, ExpirationMatch, StrikeSelection, UnderlyingInfo
from src.broker.fetcher import ExactContractSpec, ExactQuoteResult, FetchResult
from src.core.config import LedgerSettings, PaperTrackerSettings, Settings
from src.core.exceptions import (
    GatewayConnectionError,
    OptionChainFetchError,
    RefreshBusyError,
)
from src.services.analyzer import AnalyzerService
from src.services.ledger import LedgerService
from src.services.schemas import CorridorQuotesRequest, RefreshRequest
from src.storage.csv_repository import CSVRepository
from src.storage.ledger_repository import LedgerRepository
from src.storage.ledger_schema import LedgerPositionRow
from src.storage.schema import OptionContractRow, SpotSource
from tests.support.aio import noop_sleep, run_coro, run_virtual_loop

_SNAP_ID = "NVDA_20261111_193000"
_SNAP_TS = "2026-11-11T19:30:00Z"
_MKT_DATE = "2026-11-11"
_NOW = datetime(2026, 11, 11, 19, 30, 0, tzinfo=UTC)


def _row(
    *,
    snapshot_id: str = _SNAP_ID,
    conid: int,
    expiry: str,
    strike: float,
    right: str,
    bid: float | None,
    ask: float | None,
    last: float | None = None,
    close: float | None = None,
) -> OptionContractRow:
    mid = (bid + ask) / 2.0 if (bid is not None and ask is not None) else None
    return OptionContractRow(
        snapshot_id=snapshot_id,
        snapshot_ts_utc=_SNAP_TS,
        market_date=_MKT_DATE,
        market_data_type=3,
        underlying="NVDA",
        underlying_conid=4815747,
        underlying_price=145.0,
        option_conid=conid,
        expiry=expiry,
        dte=37,
        strike=strike,
        moneyness_pct=(strike - 145.0) / 145.0,
        right=right,  # type: ignore[arg-type]
        bid=bid,
        ask=ask,
        mid=mid,
        last=last,
        close=close,
        mark_price=mid or close,
        volume=120,
        open_interest=500,
        iv=0.45,
        delta=-0.25 if right == "P" else 0.25,
        gamma=0.02,
        theta=-0.05,
        vega=0.12,
        greeks_source="IBKR_MODEL",
    )


def _sample_rows(
    *, snapshot_id: str = _SNAP_ID, include_close_only: bool = False
) -> list[OptionContractRow]:
    rows = [
        _row(
            snapshot_id=snapshot_id,
            conid=9001,
            expiry="2026-12-18",
            strike=135.0,
            right="P",
            bid=1.9,
            ask=2.1,
        ),
        _row(
            snapshot_id=snapshot_id,
            conid=9002,
            expiry="2026-12-18",
            strike=140.0,
            right="P",
            bid=3.1,
            ask=3.3,
        ),
        _row(
            snapshot_id=snapshot_id,
            conid=9003,
            expiry="2026-12-18",
            strike=145.0,
            right="C",
            bid=5.0,
            ask=5.4,
        ),
        _row(
            snapshot_id=snapshot_id,
            conid=9004,
            expiry="2026-12-18",
            strike=150.0,
            right="C",
            bid=3.0,
            ask=3.2,
        ),
    ]
    if include_close_only:
        rows.append(
            _row(
                snapshot_id=snapshot_id,
                conid=9005,
                expiry="2026-12-18",
                strike=155.0,
                right="C",
                bid=None,
                ask=None,
                last=None,
                close=1.4,
            )
        )
    return rows


def _make_plan(spot_source: SpotSource = "MID") -> ChainPlan:
    return ChainPlan(
        underlying=UnderlyingInfo(
            symbol="NVDA",
            conid=4815747,
            contract=SimpleNamespace(conId=4815747),
            spot=145.0,
            spot_source=spot_source,
        ),
        market_date=date(2026, 11, 11),
        exchange="SMART",
        trading_class="NVDA",
        multiplier="100",
        expirations=(ExpirationMatch("20261218", date(2026, 12, 18), 37),),
        strikes=StrikeSelection(
            strikes=(135.0, 140.0, 145.0, 150.0),
            atm_strike=145.0,
            requested_step=5.0,
            effective_step=5.0,
        ),
        range_pct=0.20,
        available_strike_steps=(5.0,),
        available_range_pcts=(0.20,),
        exchange_validated_strike_count=4,
    )


class _FakeBroker:
    def __init__(self, settings: Settings, *, connected: bool = True) -> None:
        self.settings = settings
        self.is_connected = connected
        self.current_type = 3
        self.live_subscription_missing = False
        self.calls: list[str] = []
        self.ib = SimpleNamespace(
            reqMktData=self._req_mkt_data,
            cancelMktData=self._cancel_mkt_data,
            reqExecutionsAsync=self._req_executions,
            reqPositionsAsync=self._req_positions,
        )

    def _req_mkt_data(self, contract: Any, *_args: Any) -> Any:
        return SimpleNamespace(contract=contract, bid=2.0, ask=2.2, last=2.1, close=2.0)

    def _cancel_mkt_data(self, _contract: Any) -> None:
        return None

    async def _req_executions(self, _filter: Any) -> list[Any]:
        self.calls.append("reqExecutionsAsync")
        return []

    async def _req_positions(self) -> list[Any]:
        self.calls.append("reqPositionsAsync")
        return []

    def get_active_market_data_type(self) -> int:
        return self.current_type

    async def ensure_connected(self) -> None:
        if not self.is_connected:
            raise GatewayConnectionError("Not connected")
        self.calls.append("ensure_connected")

    def reset_market_data_type(self) -> None:
        return None

    def ensure_still_connected(self, *, stage: str = "") -> None:
        if not self.is_connected:
            raise GatewayConnectionError(f"Lost during {stage}")

    def handle_stage_error(self, exc: Exception, *, stage: str = "") -> None:
        return None

    def can_fallback(self) -> bool:
        return False

    def observe_tickers_market_data_type(self, _tickers: Any) -> None:
        return None

    async def run_stage(self, name: str, coro: Any) -> Any:
        return await coro


class _FakeFetcher:
    def __init__(
        self,
        call_log: list[str],
        *,
        pause_sec: float = 0.0,
        fail_chain: Exception | None = None,
        fail_exact: Exception | None = None,
        spot_source: SpotSource = "MID",
        include_close_only: bool = False,
    ) -> None:
        self.clock = lambda: _NOW
        self.sleep = noop_sleep
        self.call_log = call_log
        self.pause_sec = pause_sec
        self.fail_chain = fail_chain
        self.fail_exact = fail_exact
        self.spot_source = spot_source
        self.include_close_only = include_close_only
        self.exact_calls: list[tuple[str, list[ExactContractSpec], int]] = []

    async def fetch_option_chain(self, ticker: str, **_kwargs: Any) -> FetchResult:
        self.call_log.append("fetch_option_chain")
        if self.pause_sec > 0.0:
            await asyncio.sleep(self.pause_sec)
        if self.fail_chain is not None:
            raise self.fail_chain
        return FetchResult(
            snapshot_id=_SNAP_ID,
            snapshot_ts_utc=_SNAP_TS,
            market_date=_MKT_DATE,
            market_data_type=3,
            plan=_make_plan(self.spot_source),
            rows=_sample_rows(include_close_only=self.include_close_only),
            pages_fetched=1,
            elapsed_ms=12.0,
        )

    async def fetch_exact_contracts(
        self,
        ticker: str,
        specs: list[ExactContractSpec],
        *,
        underlying_conid: int = 0,
    ) -> ExactQuoteResult:
        self.call_log.append("fetch_exact_contracts")
        self.exact_calls.append((ticker, list(specs), underlying_conid))
        if self.fail_exact is not None:
            raise self.fail_exact
        quoted_rows = tuple(
            _row(
                snapshot_id="NVDA_20261111_193100",
                conid=spec.option_conid,
                expiry=spec.expiry,
                strike=spec.strike,
                right=spec.right,
                bid=2.40,
                ask=2.60,
            )
            for spec in specs
        )
        return ExactQuoteResult(
            snapshot_id="NVDA_20261111_193100",
            snapshot_ts_utc="2026-11-11T19:31:00Z",
            market_date=_MKT_DATE,
            market_data_type=3,
            underlying_price=145.0,
            spot_source=self.spot_source,
            rows=quoted_rows,
            missing=(),
        )


class _FakeCorridor:
    def __init__(self, call_log: list[str], *, active_streams: int = 0) -> None:
        self.call_log = call_log
        self.active_stream_count = active_streams

    async def stop_all_streams(self) -> int:
        self.call_log.append("stop_corridor")
        stopped = self.active_stream_count
        self.active_stream_count = 0
        return stopped

    async def fetch_corridor_quotes(
        self, selection: Any, *, snapshot_id: str, on_frame: Any = None
    ) -> Any:
        self.call_log.append("fetch_corridor_quotes")
        return SimpleNamespace(snapshot_id=snapshot_id, ticker=selection.ticker, rows=[])

    async def stream_corridor_quotes(self, *_args: Any, **_kwargs: Any) -> Any:
        yield SimpleNamespace()


def _make_service(
    tmp_path: Path,
    *,
    active_streams: int = 0,
    pause_sec: float = 0.0,
    fail_chain: Exception | None = None,
    fail_exact: Exception | None = None,
    spot_source: SpotSource = "MID",
    include_close_only: bool = False,
    gateway_sync_enabled: bool = False,
    ledger_service: LedgerService | None = None,
) -> tuple[AnalyzerService, list[str], _FakeFetcher, _FakeCorridor, _FakeBroker]:
    call_log: list[str] = []
    settings = Settings(
        tracker=PaperTrackerSettings(max_active_trades=20, tracked_dir="data/tracked"),
        ledger=LedgerSettings(gateway_sync_enabled=gateway_sync_enabled, ledger_dir="data/ledger"),
    )
    repo = CSVRepository(settings.storage, root=tmp_path)
    broker = _FakeBroker(settings)
    fetcher = _FakeFetcher(
        call_log,
        pause_sec=pause_sec,
        fail_chain=fail_chain,
        fail_exact=fail_exact,
        spot_source=spot_source,
        include_close_only=include_close_only,
    )
    corridor = _FakeCorridor(call_log, active_streams=active_streams)
    svc = AnalyzerService(
        settings=settings,
        repo=repo,
        broker_client=broker,  # type: ignore[arg-type]
        fetcher=fetcher,  # type: ignore[arg-type]
        corridor=corridor,  # type: ignore[arg-type]
        ledger_service=ledger_service,
    )
    svc.ensure_layout()
    return svc, call_log, fetcher, corridor, broker


def test_acquire_and_release_refresh_virtual_timeout(tmp_path: Path) -> None:
    svc, *_ = _make_service(tmp_path)

    async def _timeout_scenario() -> tuple[bool, bool, bool]:
        first = await svc.acquire_refresh("refresh:NVDA")
        immediate_busy = await svc.acquire_refresh("corridor:NVDA", timeout_sec=0.0)
        timed_out = await svc.acquire_refresh("ledger:mtm", timeout_sec=0.05)
        svc.release_refresh()
        return first, immediate_busy, timed_out

    assert run_virtual_loop(_timeout_scenario()) == (True, False, False)
    assert svc.refresh_owner is None
    assert not svc.refresh_lock.locked()

    svc2, *_ = _make_service(tmp_path / "handover")

    async def _handover_scenario() -> tuple[bool, str | None]:
        assert await svc2.acquire_refresh("holder")

        async def _release_soon() -> None:
            await asyncio.sleep(0.02)
            svc2.release_refresh()

        releaser = asyncio.create_task(_release_soon())
        acquired = await svc2.acquire_refresh("waiter", timeout_sec=0.10)
        await releaser
        owner = svc2.refresh_owner
        svc2.release_refresh()
        return acquired, owner

    assert run_virtual_loop(_handover_scenario()) == (True, "waiter")


def test_refresh_chain_contention_raises_busy_and_releases_on_error(tmp_path: Path) -> None:
    svc, *_ = _make_service(tmp_path, pause_sec=0.25)

    async def _concurrent() -> tuple[str | None, int | None, bool]:
        first = asyncio.create_task(svc.refresh_chain(RefreshRequest(ticker="NVDA")))
        await asyncio.sleep(0.01)
        busy_owner: str | None = None
        retry_after: int | None = None
        try:
            await svc.refresh_chain(RefreshRequest(ticker="AVGO"))
        except RefreshBusyError as exc:
            busy_owner = exc.owner
            retry_after = exc.retry_after_sec
        await first
        return busy_owner, retry_after, svc.refresh_lock.locked()

    owner, retry_sec, still_locked = run_virtual_loop(_concurrent())
    assert owner == "refresh:NVDA"
    assert retry_sec == 10
    assert not still_locked
    assert svc.refresh_owner is None

    failing_svc, *_ = _make_service(
        tmp_path / "fail_case",
        fail_chain=OptionChainFetchError("upstream error"),
    )
    with pytest.raises(OptionChainFetchError):
        run_coro(failing_svc.refresh_chain(RefreshRequest(ticker="NVDA")))
    assert not failing_svc.refresh_lock.locked()
    assert failing_svc.refresh_owner is None


def test_refresh_stops_corridor_first_and_quotes_paper_trades(tmp_path: Path) -> None:
    svc, call_log, fetcher, corridor, _ = _make_service(
        tmp_path,
        active_streams=18,
        spot_source="MID",
        include_close_only=True,
    )
    seed_id = "NVDA_20261110_193000"
    # Seed an earlier snapshot and register an ACTIVE paper trade on it.
    svc.repo.save_snapshot(
        "NVDA",
        [r.model_dump() for r in _sample_rows(snapshot_id=seed_id)],
        range_pct=0.20,
        strike_step=5.0,
        requested_strike_step=5.0,
        spot_source="MID",
    )
    trade = svc.paper_trades.register_trade(
        ticker="NVDA",
        snapshot_id=seed_id,
        strategy_kind="BULL_PUT_SPREAD",
        legs=[
            StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=140.0),
            StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=135.0),
        ],
    )

    payload = run_coro(svc.refresh_chain(RefreshRequest(ticker="NVDA")))

    assert call_log == ["stop_corridor", "fetch_option_chain", "fetch_exact_contracts"]
    assert corridor.active_stream_count == 0
    assert svc.last_corridor_stop is not None
    assert svc.last_corridor_stop["reason"] == "refresh_chain"
    assert svc.last_corridor_stop["ticker"] == "NVDA"
    assert svc.last_corridor_stop["stopped_subscriptions"] == 18
    assert any("Stopped 18 active Wall Corridor" in n for n in payload.meta.notices)

    # Quality + spot_source populated on LIVE_REFRESH & subsequent CACHE load.
    assert payload.meta.spot_source == "MID"
    assert payload.meta.contract_count == 5
    assert payload.meta.quoted_contract_count == 4
    assert payload.meta.prior_close_only_count == 1
    assert payload.meta.quote_coverage == 0.8

    cached = svc.load_chain_analytics("NVDA", _SNAP_ID)
    assert cached.meta.spot_source == "MID"
    assert cached.meta.quoted_contract_count == 4
    assert cached.meta.prior_close_only_count == 1
    assert cached.meta.quote_coverage == 0.8

    # Paper trade legs were quoted and recorded cleanly under tmp_path.
    assert len(fetcher.exact_calls) == 1
    _, specs, und_conid = fetcher.exact_calls[0]
    assert und_conid == 4815747
    assert {s.option_conid for s in specs} == {9001, 9002}
    tracked_csvs = list((svc.tracked_dir / "NVDA").glob("*.csv"))
    assert len(tracked_csvs) == 1
    trades_after, _ = svc.paper_trades.list_trades()
    assert trades_after[0].id == trade.id
    assert trades_after[0].last_quote_utc is not None


def test_paper_trade_quote_error_adds_notice_without_losing_chain(tmp_path: Path) -> None:
    svc, *_ = _make_service(
        tmp_path,
        fail_exact=GatewayConnectionError("temporary hiccup"),
    )
    seed_id = "NVDA_20261110_193000"
    svc.repo.save_snapshot(
        "NVDA",
        [r.model_dump() for r in _sample_rows(snapshot_id=seed_id)],
        spot_source="MID",
    )
    svc.paper_trades.register_trade(
        ticker="NVDA",
        snapshot_id=seed_id,
        strategy_kind="BULL_PUT_SPREAD",
        legs=[
            StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=140.0),
            StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=135.0),
        ],
    )

    payload = run_coro(svc.refresh_chain(RefreshRequest(ticker="NVDA")))
    assert payload.meta.snapshot_id == _SNAP_ID
    assert any("Paper trade leg quote refresh warning" in n for n in payload.meta.notices)
    quote_records = svc.paper_trades.store.read_quote_records()
    assert len(quote_records) == 1
    assert quote_records[0].status == "ERROR"


def test_corridor_and_ledger_coordinate_with_refresh_lock(tmp_path: Path) -> None:
    svc, call_log, _, corridor, _ = _make_service(
        tmp_path,
        active_streams=12,
        gateway_sync_enabled=True,
    )
    svc.repo.save_snapshot("NVDA", [r.model_dump() for r in _sample_rows()], spot_source="MID")
    svc.ledger.repo.replace_positions(
        [
            LedgerPositionRow(
                conid=9001,
                symbol="NVDA 20261218 135 P",
                underlying="NVDA",
                asset_class="OPT",
                expiry="2026-12-18",
                strike=135.0,
                right="P",
                multiplier=100,
                position=-1.0,
                avg_cost=2.0,
                source="GATEWAY_LIVE",
                updated_at=_SNAP_TS,
            )
        ]
    )

    # Active lock blocks fetch_corridor_quotes and open_corridor_stream.
    assert run_coro(svc.acquire_refresh("refresh:NVDA"))
    with pytest.raises(RefreshBusyError) as exc_info:
        run_coro(svc.fetch_corridor_quotes(CorridorQuotesRequest(ticker="NVDA")))
    assert exc_info.value.owner == "refresh:NVDA"

    with pytest.raises(RefreshBusyError):
        run_coro(svc.open_corridor_stream("NVDA"))

    with pytest.raises(RefreshBusyError):
        run_coro(svc.refresh_open_ledger_mtm())

    # Flex-only sync does not require Gateway/lock.
    # Release lock and verify MTM stops active corridor streams cleanly.
    svc.release_refresh()
    mtm = run_coro(svc.refresh_open_ledger_mtm())
    assert mtm.refreshed_count == 1
    assert corridor.active_stream_count == 0
    assert "stop_corridor" in call_log
    assert svc.last_corridor_stop is not None
    assert svc.last_corridor_stop["reason"] == "ledger"
    assert any("Stopped 12 active Wall Corridor" in n for n in mtm.notices)
    assert not svc.refresh_lock.locked()

    # Manual stop records last_corridor_stop when streams stopped.
    corridor.active_stream_count = 7
    stop_res = run_coro(svc.stop_corridor_streams())
    assert stop_res.stopped_subscriptions == 7
    assert svc.last_corridor_stop is not None
    assert svc.last_corridor_stop["reason"] == "manual_stop"

    # Gateway ledger sync stops active corridor streams too.
    corridor.active_stream_count = 5
    sync_res = run_coro(svc.sync_ledger(mode="auto", include_gateway=True))
    assert sync_res.gateway_synced is True
    assert corridor.active_stream_count == 0
    assert any("Stopped 5 active Wall Corridor" in n for n in sync_res.notices)


def test_injected_custom_ledger_without_lock_is_still_coordinated(tmp_path: Path) -> None:
    custom_settings = Settings(
        tracker=PaperTrackerSettings(tracked_dir="data/tracked"),
        ledger=LedgerSettings(gateway_sync_enabled=True, ledger_dir="data/ledger"),
    )
    custom_repo = LedgerRepository(custom_settings.ledger.ledger_dir, root=tmp_path)
    custom_repo.replace_positions(
        [
            LedgerPositionRow(
                conid=9002,
                symbol="NVDA 20261218 140 P",
                underlying="NVDA",
                asset_class="OPT",
                expiry="2026-12-18",
                strike=140.0,
                right="P",
                multiplier=100,
                position=-1.0,
                avg_cost=3.2,
                source="GATEWAY_LIVE",
                updated_at=_SNAP_TS,
            )
        ]
    )
    custom_ledger = LedgerService(
        custom_settings,
        repo=custom_repo,
        broker_client=_FakeBroker(custom_settings),  # type: ignore[arg-type]
        sleep=noop_sleep,
        now_fn=lambda: _NOW,
    )
    svc, call_log, _, corridor, _ = _make_service(
        tmp_path,
        active_streams=9,
        gateway_sync_enabled=True,
        ledger_service=custom_ledger,
    )

    assert run_coro(svc.acquire_refresh("refresh:NVDA"))
    with pytest.raises(RefreshBusyError):
        run_coro(svc.refresh_open_ledger_mtm())
    with pytest.raises(RefreshBusyError):
        run_coro(svc.sync_ledger(mode="auto", include_gateway=True))
    svc.release_refresh()

    mtm = run_coro(svc.refresh_open_ledger_mtm())
    assert mtm.refreshed_count == 1
    assert corridor.active_stream_count == 0
    assert "stop_corridor" in call_log
    assert svc.last_corridor_stop is not None
    assert svc.last_corridor_stop["reason"] == "ledger_mtm"
    assert not svc.refresh_lock.locked()
