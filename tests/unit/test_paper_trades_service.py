"""Unit tests for PaperTradeService: validation, expiry, cap, duplicates, quotes, isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from src.analytics.ledger import (
    LedgerAnalyticsReport,
    LedgerCampaignRow,
    LedgerKPIs,
    LedgerOpenPositionView,
)
from src.analytics.strategies import StrategyLegInput
from src.broker.fetcher import ExactContractSpec, ExactQuoteResult
from src.core.config import PaperTrackerSettings, Settings
from src.core.exceptions import (
    BacktestValidationError,
    DuplicatePaperTradeError,
    InvalidRequestError,
    InvalidTickerError,
    OptionChainFetchError,
    PaperTradeCapError,
    PaperTradeStateError,
    SnapshotNotFoundError,
)
from src.services.paper_trades import PAPER_TRADE_ID_RE, PaperTradeService, resolve_tracked_dir
from src.services.schemas import RegisterPaperTradeRequest
from src.storage.csv_repository import CSVRepository
from src.storage.schema import OptionContractRow
from tests.support.aio import run_coro

_SNAP_NVDA = "NVDA_20261111_193000"
_SNAP_AVGO = "AVGO_20261111_193000"
_SNAP_TS = "2026-11-11T19:30:00Z"
_MKT_DATE = "2026-11-11"


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class _FakeFetcher:
    def __init__(self, clock: _Clock, *, fail_with: Exception | None = None) -> None:
        self.clock = clock
        self.fail_with = fail_with
        self.calls: list[tuple[str, list[ExactContractSpec], int]] = []

    async def fetch_exact_contracts(
        self,
        ticker: str,
        specs: list[ExactContractSpec],
        *,
        underlying_conid: int = 0,
    ) -> ExactQuoteResult:
        self.calls.append((ticker, list(specs), underlying_conid))
        if self.fail_with is not None:
            raise self.fail_with
        now = self.clock()
        snap_id = f"{ticker}_{now.strftime('%Y%m%d_%H%M%S')}"
        rows = tuple(
            OptionContractRow(
                snapshot_id=snap_id,
                snapshot_ts_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                market_date=_MKT_DATE,
                market_data_type=3,
                underlying=ticker,
                underlying_conid=underlying_conid or 4815747,
                underlying_price=145.0,
                option_conid=spec.option_conid or (9000 + idx),
                expiry=spec.expiry,
                dte=37,
                strike=spec.strike,
                moneyness_pct=(spec.strike - 145.0) / 145.0,
                right=spec.right,
                bid=2.40,
                ask=2.60,
                mid=2.50,
                last=2.50,
                close=2.30,
                mark_price=2.50,
                volume=50,
                open_interest=300,
                iv=None,
                delta=None,
                gamma=None,
                theta=None,
                vega=None,
                greeks_source="NONE",
            )
            for idx, spec in enumerate(specs, start=1)
        )
        return ExactQuoteResult(
            snapshot_id=snap_id,
            snapshot_ts_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            market_date=_MKT_DATE,
            market_data_type=3,
            underlying_price=145.0,
            spot_source="MID",
            rows=rows,
            missing=(),
        )


def _chain_rows(ticker: str, snap_id: str) -> list[dict[str, Any]]:
    spot = 145.0 if ticker == "NVDA" else 180.0
    contracts = [
        (9001, "2026-11-20", 135.0, "P", 1.15, 1.25),
        (9002, "2026-11-20", 140.0, "P", 2.35, 2.45),
        (9003, "2026-12-18", 135.0, "P", 1.90, 2.10),
        (9004, "2026-12-18", 140.0, "P", 3.10, 3.30),
        (9005, "2026-12-18", 145.0, "C", 5.00, 5.40),
        (9006, "2026-12-18", 150.0, "C", 3.00, 3.20),
    ]
    return [
        OptionContractRow(
            snapshot_id=snap_id,
            snapshot_ts_utc=_SNAP_TS,
            market_date=_MKT_DATE,
            market_data_type=3,
            underlying=ticker,
            underlying_conid=4815747 if ticker == "NVDA" else 112233,
            underlying_price=spot,
            option_conid=conid,
            expiry=expiry,
            dte=9 if expiry == "2026-11-20" else 37,
            strike=strike,
            moneyness_pct=(strike - spot) / spot,
            right=right,  # type: ignore[arg-type]
            bid=bid,
            ask=ask,
            mid=(bid + ask) / 2.0,
            last=(bid + ask) / 2.0,
            close=bid,
            mark_price=(bid + ask) / 2.0,
            volume=100,
            open_interest=500,
            iv=0.45,
            delta=-0.25 if right == "P" else 0.25,
            gamma=0.02,
            theta=-0.05,
            vega=0.12,
            greeks_source="IBKR_MODEL",
        ).model_dump()
        for conid, expiry, strike, right, bid, ask in contracts
    ]


def _make_service(
    tmp_path: Path,
    *,
    max_active_trades: int = 5,
    now: datetime | None = None,
    fail_with: Exception | None = None,
) -> tuple[PaperTradeService, _Clock, _FakeFetcher, CSVRepository]:
    clock = _Clock(now or datetime(2026, 11, 11, 19, 30, 0, tzinfo=UTC))
    settings = Settings(
        tracker=PaperTrackerSettings(
            max_active_trades=max_active_trades,
            tracked_dir="data/tracked",
        )
    )
    repo = CSVRepository(settings.storage, root=tmp_path)
    repo.ensure_layout()
    fetcher = _FakeFetcher(clock, fail_with=fail_with)
    svc = PaperTradeService(
        settings,
        repo,
        fetcher,  # type: ignore[arg-type]
        clock=clock,
    )
    svc.ensure_layout()
    return svc, clock, fetcher, repo


def test_tmp_path_isolation_and_layout(tmp_path: Path) -> None:
    svc, _, _, repo = _make_service(tmp_path)
    resolved = resolve_tracked_dir(repo, "data/tracked")
    assert resolved == (tmp_path / "data" / "tracked").resolve()
    assert svc.store.tracked_dir == resolved
    assert svc.store.metadata_dir == (tmp_path / "data" / "metadata")
    assert svc.store.tracked_dir.is_dir()
    assert svc.store.metadata_dir.is_dir()


def test_register_computes_entry_cash_and_rejects_bad_inputs(tmp_path: Path) -> None:
    svc, _, _, repo = _make_service(tmp_path)
    repo.save_snapshot("NVDA", _chain_rows("NVDA", _SNAP_NVDA), spot_source="MID")

    # Bull put credit spread: SELL 140P ($3.20) - BUY 135P ($2.00) = +$1.20/sh * 100 = +$120.
    trade = svc.register_from_request(
        RegisterPaperTradeRequest(
            ticker="NVDA",
            snapshot_id=_SNAP_NVDA,
            strategy_kind="BULL_PUT_SPREAD",
            legs=[
                StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=140.0),
                StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=135.0),
            ],
            qty=2,
        )
    )
    assert trade.ticker == "NVDA"
    assert trade.status == "ACTIVE"
    assert trade.qty == 2
    assert trade.expiry == "2026-12-18"
    assert trade.underlying_conid == 4815747
    assert [leg.con_id for leg in trade.legs] == [9004, 9003]
    assert trade.entry_net_cash == pytest.approx(240.0)

    # Debit spread -> negative entry_net_cash.
    debit = svc.register_trade(
        ticker="NVDA",
        snapshot_id=_SNAP_NVDA,
        strategy_kind="BULL_CALL_SPREAD",
        legs=[
            StrategyLegInput(side="BUY", right="C", expiry="2026-12-18", strike=145.0),
            StrategyLegInput(side="SELL", right="C", expiry="2026-12-18", strike=150.0),
        ],
    )
    assert debit.entry_net_cash == pytest.approx(-210.0)

    # Validation checks.
    with pytest.raises(InvalidTickerError):
        svc.register_trade("INVALID!", _SNAP_NVDA, "CUSTOM", trade_legs := debit.legs[:1])  # type: ignore[arg-type]
    del trade_legs

    valid_leg = [StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=135.0)]
    with pytest.raises(BacktestValidationError):
        svc.register_trade("NVDA", "bad-snap", "CUSTOM", valid_leg)
    with pytest.raises(SnapshotNotFoundError):
        svc.register_trade("NVDA", "NVDA_20261110_193000", "CUSTOM", valid_leg)
    with pytest.raises(BacktestValidationError):
        svc.register_trade("NVDA", _SNAP_NVDA, "CUSTOM", [])
    with pytest.raises(BacktestValidationError):
        svc.register_trade("NVDA", _SNAP_NVDA, "CUSTOM", valid_leg, qty=0)
    with pytest.raises(BacktestValidationError):
        svc.register_trade(
            "NVDA",
            _SNAP_NVDA,
            "CUSTOM",
            [StrategyLegInput(side="BUY", right="P", expiry="2026-11-01", strike=135.0)],
        )
    with pytest.raises(BacktestValidationError):
        svc.register_trade(
            "NVDA",
            _SNAP_NVDA,
            "CUSTOM",
            [StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=999.0)],
        )


def test_duplicate_active_trade_and_max_active_cap(tmp_path: Path) -> None:
    svc, _, _, repo = _make_service(tmp_path, max_active_trades=2)
    repo.save_snapshot("NVDA", _chain_rows("NVDA", _SNAP_NVDA), spot_source="MID")

    legs = [
        StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=140.0),
        StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=135.0),
    ]
    t1 = svc.register_trade("NVDA", _SNAP_NVDA, "BULL_PUT_SPREAD", legs)

    # Identical legs in reversed order still caught as duplicate ACTIVE trade.
    with pytest.raises(DuplicatePaperTradeError):
        svc.register_trade("NVDA", _SNAP_NVDA, "BULL_PUT_SPREAD", list(reversed(legs)))

    # Second distinct trade fills max_active_trades=2.
    svc.register_trade(
        "NVDA",
        _SNAP_NVDA,
        "CUSTOM",
        [StrategyLegInput(side="BUY", right="C", expiry="2026-12-18", strike=145.0)],
    )
    with pytest.raises(PaperTradeCapError):
        svc.register_trade(
            "NVDA",
            _SNAP_NVDA,
            "CUSTOM",
            [StrategyLegInput(side="SELL", right="C", expiry="2026-12-18", strike=150.0)],
        )

    # Stopping t1 frees a slot AND allows re-registering those same legs.
    stopped = svc.stop_trade(t1.id)
    assert stopped.status == "STOPPED"
    assert stopped.status_changed_utc is not None

    t1_reopened = svc.register_trade("NVDA", _SNAP_NVDA, "BULL_PUT_SPREAD", legs)
    assert t1_reopened.status == "ACTIVE"
    assert t1_reopened.id != t1.id


def test_list_auto_expires_past_due_trades_and_stop_trade_validation(tmp_path: Path) -> None:
    svc, clock, _, repo = _make_service(tmp_path)
    repo.save_snapshot("NVDA", _chain_rows("NVDA", _SNAP_NVDA), spot_source="MID")
    repo.save_snapshot("AVGO", _chain_rows("AVGO", _SNAP_AVGO), spot_source="MID")

    short_dated = svc.register_trade(
        "NVDA",
        _SNAP_NVDA,
        "BULL_PUT_SPREAD",
        [
            StrategyLegInput(side="SELL", right="P", expiry="2026-11-20", strike=140.0),
            StrategyLegInput(side="BUY", right="P", expiry="2026-11-20", strike=135.0),
        ],
    )
    longer_dated = svc.register_trade(
        "AVGO",
        _SNAP_AVGO,
        "CUSTOM",
        [StrategyLegInput(side="BUY", right="C", expiry="2026-12-18", strike=145.0)],
    )

    # Advance ET date past 2026-11-20 -> short_dated auto-expires atomically on inspect.
    clock.current = datetime(2026, 11, 21, 16, 0, 0, tzinfo=UTC)
    resp = svc.list_trades_response()
    assert resp.active_count == 1
    assert resp.max_active_trades == 5
    by_id = {t.id: t for t in resp.trades}
    assert by_id[short_dated.id].status == "EXPIRED"
    assert by_id[short_dated.id].status_changed_utc == "2026-11-21T16:00:00Z"
    assert by_id[longer_dated.id].status == "ACTIVE"

    nvda_expired, active_all = svc.list_trades(status="EXPIRED", ticker="NVDA")
    assert [t.id for t in nvda_expired] == [short_dated.id]
    assert active_all == 1

    with pytest.raises(InvalidTickerError):
        svc.list_trades(ticker="BAD!")
    with pytest.raises(InvalidRequestError):
        svc.list_trades(status="BOGUS")  # type: ignore[arg-type]

    # stop_trade errors.
    with pytest.raises(InvalidRequestError):
        svc.stop_trade("not-a-trade-id")
    with pytest.raises(SnapshotNotFoundError):
        svc.stop_trade("NVDA-20261111-deadbeef")
    with pytest.raises(PaperTradeStateError):
        svc.stop_trade(short_dated.id)


def test_quote_active_trades_deduplicates_conids_and_enriches_greeks(tmp_path: Path) -> None:
    svc, _, fetcher, repo = _make_service(tmp_path)
    repo.save_snapshot("NVDA", _chain_rows("NVDA", _SNAP_NVDA), spot_source="MID")
    repo.save_snapshot("AVGO", _chain_rows("AVGO", _SNAP_AVGO), spot_source="MID")

    # Two NVDA trades sharing leg conid 9004 (140P).
    t1 = svc.register_trade(
        "NVDA",
        _SNAP_NVDA,
        "BULL_PUT_SPREAD",
        [
            StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=140.0),
            StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=135.0),
        ],
    )
    t2 = svc.register_trade(
        "NVDA",
        _SNAP_NVDA,
        "CUSTOM",
        [StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=140.0)],
    )
    t_other = svc.register_trade(
        "AVGO",
        _SNAP_AVGO,
        "CUSTOM",
        [StrategyLegInput(side="BUY", right="C", expiry="2026-12-18", strike=145.0)],
    )

    record, notices = run_coro(
        svc.quote_active_trades_for_refresh("NVDA", underlying_conid=4815747)
    )
    assert notices == [f"Quoted 2 active paper-trade contract(s) for NVDA ({_SNAP_NVDA})."]
    assert record is not None
    assert record.status == "QUOTED"
    assert record.tracked_snapshot_id == _SNAP_NVDA
    assert set(record.trade_ids) == {t1.id, t2.id}

    assert len(fetcher.calls) == 1
    ticker_arg, specs, und_conid = fetcher.calls[0]
    assert ticker_arg == "NVDA"
    assert und_conid == 4815747
    assert len(specs) == 2
    assert {s.option_conid for s in specs} == {9003, 9004}

    csv_files = sorted((svc.store.tracked_dir / "NVDA").glob("*.csv"))
    assert len(csv_files) == 1
    quoted_df = pl.read_csv(csv_files[0])
    assert quoted_df.height == 2
    assert all(src == "BSM_FALLBACK" for src in quoted_df["greeks_source"].to_list())

    trades_after, _ = svc.list_trades()
    by_id = {t.id: t for t in trades_after}
    assert by_id[t1.id].last_quote_utc == _SNAP_TS
    assert by_id[t2.id].last_quote_utc == _SNAP_TS
    assert by_id[t_other.id].last_quote_utc is None

    # Defensive error capture.
    fail_svc, _, _, fail_repo = _make_service(
        tmp_path / "err",
        fail_with=OptionChainFetchError("exact failed"),
    )
    fail_repo.save_snapshot("NVDA", _chain_rows("NVDA", _SNAP_NVDA), spot_source="MID")
    fail_svc.register_trade(
        "NVDA",
        _SNAP_NVDA,
        "CUSTOM",
        [StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=140.0)],
    )
    err_rec, err_notices = run_coro(fail_svc.quote_active_trades_for_refresh("NVDA"))
    assert err_rec is not None and err_rec.status == "ERROR"
    assert len(err_notices) == 1 and "exact failed" in err_notices[0]


def test_sync_from_ledger_report_campaigns_gcd_covered_call_unlinked_and_close(
    tmp_path: Path,
) -> None:
    svc, _, fetcher, repo = _make_service(tmp_path)
    repo.save_snapshot("NVDA", _chain_rows("NVDA", _SNAP_NVDA), spot_source="MID")

    manual_trade = svc.register_trade(
        "NVDA",
        _SNAP_NVDA,
        "CUSTOM",
        [StrategyLegInput(side="BUY", right="C", expiry="2026-12-18", strike=145.0)],
    )

    kpis = LedgerKPIs(
        realized_pnl_ex_fee=0.0,
        stock_pnl_ex_fee=0.0,
        total_pnl_ex_fee=0.0,
        net_premium_ex_fee=0.0,
        fees=0.0,
        win_rate_pct=None,
        closed_count=0,
        open_count=4,
        roll_count=0,
        unrealized_mtm_ex_fee=None,
    )
    bps_camp = LedgerCampaignRow(
        campaign_id="camp-bps",
        underlying="NVDA",
        strategy_type="BULL_PUT_SPREAD",
        status="OPEN",
        opened_at="2026-11-11T15:30:00Z",
        last_activity_at="2026-11-11T15:30:00Z",
        roll_count=0,
        initial_summary="140/135P",
        current_summary="140/135P",
        contracts_total=2.0,
        contracts_open=2.0,
        net_premium_ex_fee=240.0,
        realized_pnl_ex_fee=0.0,
        fees=2.0,
    )
    ratio_camp = LedgerCampaignRow(
        campaign_id="camp-ratio",
        underlying="AVGO",
        strategy_type="CUSTOM_COMBO",
        status="OPEN",
        opened_at="2026-11-10T16:00:00Z",
        last_activity_at="2026-11-10T16:00:00Z",
        roll_count=0,
        initial_summary="1x2 call ratio",
        current_summary="1x2 call ratio",
        contracts_total=2.0,
        contracts_open=2.0,
        net_premium_ex_fee=-180.0,
        realized_pnl_ex_fee=0.0,
        fees=3.0,
    )
    cc_camp = LedgerCampaignRow(
        campaign_id="camp-cc",
        underlying="NVDA",
        strategy_type="COVERED_CALL",
        status="OPEN",
        opened_at="2026-11-11T16:00:00Z",
        last_activity_at="2026-11-11T16:00:00Z",
        roll_count=0,
        initial_summary="CC 150C",
        current_summary="CC 150C",
        contracts_total=1.0,
        contracts_open=1.0,
        net_premium_ex_fee=310.0,
        realized_pnl_ex_fee=0.0,
        fees=1.0,
    )

    open_positions = [
        # 1. Bull put spread qty=2 -> qty=2, qty_ratio=1, snapshot match on 2026-11-11
        LedgerOpenPositionView(
            position_key="OPT:NVDA:20261218:140:P",
            underlying="NVDA",
            asset_class="OPT",
            strategy_type="BULL_PUT_SPREAD",
            symbol="NVDA 261218P140",
            expiry="20261218",
            strike=140.0,
            right="P",
            quantity=-2.0,
            avg_cost_price=3.20,
            mark_price=3.00,
            mark_source="FLEX_MARK",
            cost_basis_ex_fee=-640.0,
            fees=1.0,
            campaign_id="camp-bps",
            conid=9004,
            opened_at="2026-11-11T15:30:00Z",
        ),
        LedgerOpenPositionView(
            position_key="OPT:NVDA:20261218:135:P",
            underlying="NVDA",
            asset_class="OPT",
            strategy_type="BULL_PUT_SPREAD",
            symbol="NVDA 261218P135",
            expiry="20261218",
            strike=135.0,
            right="P",
            quantity=2.0,
            avg_cost_price=2.00,
            mark_price=1.90,
            mark_source="FLEX_MARK",
            cost_basis_ex_fee=400.0,
            fees=1.0,
            campaign_id="camp-bps",
            conid=9003,
            opened_at="2026-11-11T15:30:00Z",
        ),
        # 2. Ratio spread (+2 / -4) -> gcd=2, qty_ratio=[1, 2], no snapshot on 2026-11-10
        LedgerOpenPositionView(
            position_key="OPT:AVGO:20261218:180:C",
            underlying="AVGO",
            asset_class="OPT",
            strategy_type="CUSTOM_COMBO",
            symbol="AVGO 261218C180",
            expiry="20261218",
            strike=180.0,
            right="C",
            quantity=2.0,
            avg_cost_price=4.50,
            mark_price=4.60,
            mark_source="FLEX_MARK",
            cost_basis_ex_fee=900.0,
            fees=1.0,
            campaign_id="camp-ratio",
            conid=9101,
            opened_at="2026-11-10T16:00:00Z",
        ),
        LedgerOpenPositionView(
            position_key="OPT:AVGO:20261218:190:C",
            underlying="AVGO",
            asset_class="OPT",
            strategy_type="CUSTOM_COMBO",
            symbol="AVGO 261218C190",
            expiry="20261218",
            strike=190.0,
            right="C",
            quantity=-4.0,
            avg_cost_price=1.80,
            mark_price=1.70,
            mark_source="FLEX_MARK",
            cost_basis_ex_fee=-720.0,
            fees=2.0,
            campaign_id="camp-ratio",
            conid=9102,
            opened_at="2026-11-10T16:00:00Z",
        ),
        # 3. COVERED_CALL: STK row must be excluded, OPT short call kept
        LedgerOpenPositionView(
            position_key="STK:NVDA",
            underlying="NVDA",
            asset_class="STK",
            strategy_type="COVERED_CALL",
            symbol="NVDA",
            quantity=100.0,
            avg_cost_price=120.0,
            mark_price=145.0,
            mark_source="FLEX_MARK",
            cost_basis_ex_fee=12000.0,
            fees=0.0,
            campaign_id="camp-cc",
            conid=4815747,
            opened_at="2026-11-01T14:00:00Z",
        ),
        LedgerOpenPositionView(
            position_key="OPT:NVDA:20261218:150:C",
            underlying="NVDA",
            asset_class="OPT",
            strategy_type="COVERED_CALL",
            symbol="NVDA 261218C150",
            expiry="20261218",
            strike=150.0,
            right="C",
            quantity=-1.0,
            avg_cost_price=3.10,
            mark_price=2.80,
            mark_source="FLEX_MARK",
            cost_basis_ex_fee=-310.0,
            fees=1.0,
            campaign_id="camp-cc",
            conid=9006,
            opened_at="2026-11-11T16:00:00Z",
        ),
        # 4. Unlinked open OPT row without conid -> 1-leg CUSTOM trade
        LedgerOpenPositionView(
            position_key="OPT:PLTR:20261218:45:P",
            underlying="PLTR",
            asset_class="OPT",
            strategy_type="SHORT_PUT",
            symbol="PLTR 261218P45",
            expiry="20261218",
            strike=45.0,
            right="P",
            quantity=-1.0,
            avg_cost_price=2.25,
            mark_price=None,
            mark_source="NONE",
            cost_basis_ex_fee=-225.0,
            fees=0.0,
            campaign_id=None,
            conid=None,
            opened_at="2026-11-11T17:00:00Z",
        ),
        # 5. Pure STK and unsupported ticker -> skipped
        LedgerOpenPositionView(
            position_key="STK:PLTR",
            underlying="PLTR",
            asset_class="STK",
            strategy_type="STOCK_LOT",
            symbol="PLTR",
            quantity=50.0,
            avg_cost_price=40.0,
            mark_price=45.0,
            mark_source="FLEX_MARK",
            cost_basis_ex_fee=2000.0,
            fees=0.0,
        ),
        LedgerOpenPositionView(
            position_key="OPT:TOOLONG:20261218:10:C",
            underlying="TOOLONG",
            asset_class="OPT",
            strategy_type="LONG_CALL",
            symbol="TOOLONG",
            expiry="20261218",
            strike=10.0,
            right="C",
            quantity=1.0,
            avg_cost_price=1.0,
            mark_source="NONE",
            cost_basis_ex_fee=100.0,
            fees=0.0,
        ),
    ]

    report = LedgerAnalyticsReport(
        as_of=_SNAP_TS,
        window="ALL",
        ticker="ALL",
        group_by="campaigns",
        view="open_mtm",
        include_stock_gain=True,
        available_tickers=["AVGO", "NVDA", "PLTR"],
        kpis=kpis,
        campaigns=[bps_camp, ratio_camp, cc_camp],
        open_positions=open_positions,
    )
    res = svc.sync_from_ledger_report(report)
    assert res.created_count == 4
    assert res.updated_count == 0
    assert res.closed_count == 0
    assert res.active_ledger_count == 4
    assert res.active_count == 5  # 4 ledger + 1 manual

    by_cid = {t.ledger_campaign_id: t for t in res.trades if t.source == "LEDGER"}
    bps = by_cid["camp-bps"]
    assert PAPER_TRADE_ID_RE.fullmatch(bps.id) is not None
    assert bps.strategy_kind == "BULL_PUT_SPREAD"
    assert bps.ledger_strategy_label == "BULL PUT SPREAD"
    assert bps.qty == 2
    assert [leg.qty_ratio for leg in bps.legs] == [1, 1]
    assert [leg.entry_price for leg in bps.legs] == [3.2, 2.0]
    assert bps.entry_net_cash == pytest.approx(240.0)
    assert bps.created_from_snapshot_id == _SNAP_NVDA
    assert bps.underlying_conid == 0

    ratio = by_cid["camp-ratio"]
    assert ratio.strategy_kind == "CUSTOM"
    assert ratio.ledger_strategy_label == "CUSTOM COMBO"
    assert ratio.qty == 2
    assert [(leg.side, leg.qty_ratio) for leg in ratio.legs] == [("BUY", 1), ("SELL", 2)]
    assert ratio.entry_net_cash == pytest.approx(-180.0)
    assert ratio.created_from_snapshot_id is None

    cc = by_cid["camp-cc"]
    assert cc.strategy_kind == "CUSTOM"
    assert cc.ledger_strategy_label == "COVERED CALL"
    assert len(cc.legs) == 1
    assert cc.legs[0].side == "SELL" and cc.legs[0].right == "C"
    assert cc.entry_net_cash == pytest.approx(310.0)

    unlinked = by_cid["OPT:PLTR:20261218:45:P"]
    assert unlinked.strategy_kind == "CUSTOM"
    assert unlinked.ledger_strategy_label == "SHORT PUT"
    assert unlinked.legs[0].con_id == 0
    assert unlinked.legs[0].entry_price == pytest.approx(2.25)

    # Quoting active trades handles con_id=0 ledger legs cleanly
    pltr_rec, _ = run_coro(svc.quote_active_trades_for_refresh("PLTR"))
    assert pltr_rec is not None and pltr_rec.status == "QUOTED"
    assert fetcher.calls[-1][1][0].option_conid == 0

    # Re-sync after closing camp-bps -> transitions to CLOSED; MANUAL trade stays ACTIVE
    closed_report = report.model_copy(
        update={
            "campaigns": [ratio_camp, cc_camp],
            "open_positions": [op for op in open_positions if op.campaign_id != "camp-bps"],
        }
    )
    res2 = svc.sync_from_ledger_report(closed_report)
    assert res2.created_count == 0
    assert res2.updated_count == 3
    assert res2.closed_count == 1
    assert res2.active_ledger_count == 3
    assert res2.active_count == 4

    closed_list, active_now = svc.list_trades(status="CLOSED")
    assert active_now == 4
    assert [t.id for t in closed_list] == [bps.id]
    manual_after = next(t for t in res2.trades if t.id == manual_trade.id)
    assert manual_after.status == "ACTIVE"
    assert manual_after.source == "MANUAL"


def test_quote_active_trades_handles_corrupt_paper_trades_file(tmp_path: Path) -> None:
    """Corrupt paper_trades.json does not raise; returns ERROR quote record and warning notice."""
    svc, _clock, _fetcher, _repo = _make_service(tmp_path)
    reg_file = svc.store.registry_path
    reg_file.write_text("NOT_VALID_JSON{{{", encoding="utf-8")

    rec, notices = run_coro(svc.quote_active_trades_for_refresh("NVDA"))
    assert rec is not None
    assert rec.status == "ERROR"
    assert rec.ticker == "NVDA"
    assert "paper_trades.json unreadable" in (rec.error or "")
    assert len(notices) == 1
    assert "Paper trade leg quote refresh warning for NVDA" in notices[0]


def test_quote_active_trades_includes_unexpired_closed_and_ledger_specs(tmp_path: Path) -> None:
    """Closed paper trades and Trade Ledger `extra_specs` still unexpired are quoted."""
    svc, _, fetcher, repo = _make_service(tmp_path)
    repo.save_snapshot("NVDA", _chain_rows("NVDA", _SNAP_NVDA), spot_source="MID")

    # Register and stop a trade whose expiry (2026-12-18) is still in the future.
    stopped = svc.register_trade(
        "NVDA",
        _SNAP_NVDA,
        "CUSTOM",
        [StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=140.0)],
    )
    svc.stop_trade(stopped.id)

    # Extra ledger specs: one already quoted in main chain (excluded), one expired (excluded),
    # and one closed/assigned legacy $205P Dec 18 leg missing from main chain (quoted!).
    extra = [
        ExactContractSpec(option_conid=9003, expiry="2026-12-18", strike=135.0, right="P"),
        ExactContractSpec(option_conid=9901, expiry="2026-11-06", strike=190.0, right="P"),
        ExactContractSpec(option_conid=9902, expiry="2026-12-18", strike=205.0, right="P"),
    ]
    already_quoted = {("2026-12-18", 135.0, "P")}

    record, notices = run_coro(
        svc.quote_active_trades_for_refresh(
            "NVDA",
            underlying_conid=4815747,
            extra_specs=extra,
            already_quoted_keys=already_quoted,
        )
    )
    assert record is not None
    assert record.status == "QUOTED"
    assert len(fetcher.calls) == 1
    quoted_specs = fetcher.calls[0][1]
    assert {(s.expiry, s.strike, s.right) for s in quoted_specs} == {
        ("2026-12-18", 140.0, "P"),
        ("2026-12-18", 205.0, "P"),
    }
    assert any("closed/ledger leg(s)" in n for n in notices)


def test_analyzer_refresh_collects_spot_envelope_and_unexpired_ledger_specs(
    tmp_path: Path,
) -> None:
    """AnalyzerService passes historical spot envelope and unexpired closed ledger legs."""
    import asyncio
    from datetime import date

    from src.broker.contracts import ChainPlan, ExpirationMatch, StrikeSelection, UnderlyingInfo
    from src.broker.fetcher import FetchResult
    from src.core.config import LedgerSettings, StorageSettings
    from src.services.analyzer import AnalyzerService
    from src.storage.ledger_schema import LedgerExecutionRow

    now = datetime(2026, 11, 20, 15, 30, 0, tzinfo=UTC)
    storage = StorageSettings(
        data_dir=str(tmp_path / "data"),
        snapshots_dir=str(tmp_path / "data" / "snapshots"),
        latest_dir=str(tmp_path / "data" / "latest"),
        metadata_index_file=str(tmp_path / "data" / "metadata" / "snapshot_index.json"),
        logs_dir=str(tmp_path / "data" / "logs"),
    )
    settings = Settings(
        storage=storage,
        ledger=LedgerSettings(ledger_dir=str(tmp_path / "data" / "ledger")),
        tracker=PaperTrackerSettings(tracked_dir=str(tmp_path / "data" / "tracked")),
    )
    repo = CSVRepository(storage)
    repo.ensure_layout()

    # Older snapshot at $200 spot holding Dec 18 expiry (still active on Nov 20).
    old_row = OptionContractRow(
        snapshot_id="NVDA_20261106_153000",
        snapshot_ts_utc="2026-11-06T15:30:00Z",
        market_date="2026-11-06",
        market_data_type=3,
        underlying="NVDA",
        underlying_conid=4815747,
        underlying_price=200.0,
        option_conid=8801,
        expiry="2026-12-18",
        dte=42,
        strike=200.0,
        moneyness_pct=0.0,
        right="P",
        bid=5.0,
        ask=5.2,
        mid=5.1,
        last=5.1,
        close=5.0,
        mark_price=5.1,
        volume=10,
        open_interest=100,
        iv=0.45,
        delta=-0.48,
        gamma=0.02,
        theta=-0.05,
        vega=0.14,
        greeks_source="IBKR_MODEL",
    )
    repo.save_snapshot("NVDA", [old_row.model_dump()], spot_source="MID")

    clock = _Clock(now)

    class _RecordingFetcher:
        def __init__(self) -> None:
            self.clock = clock
            self.sleep = asyncio.sleep
            self.chain_calls: list[tuple[float, float] | None] = []
            self.exact_calls: list[list[ExactContractSpec]] = []

        async def fetch_option_chain(
            self,
            ticker: str,
            *,
            spot_envelope: tuple[float, float] | None = None,
            **_kwargs: Any,
        ) -> FetchResult:
            self.chain_calls.append(spot_envelope)
            mkt = date(2026, 11, 20)
            exp_dt = date(2026, 12, 18)
            plan = ChainPlan(
                underlying=UnderlyingInfo(ticker, 4815747, object(), 125.0, "MID"),
                market_date=mkt,
                exchange="SMART",
                trading_class=ticker,
                multiplier="100",
                expirations=(ExpirationMatch("20261218", exp_dt, 28),),
                strikes=StrikeSelection((125.0,), 125.0, 5.0, 5.0),
                range_pct=0.20,
            )
            row = OptionContractRow(
                snapshot_id="NVDA_20261120_153000",
                snapshot_ts_utc="2026-11-20T15:30:00Z",
                market_date="2026-11-20",
                market_data_type=3,
                underlying=ticker,
                underlying_conid=4815747,
                underlying_price=125.0,
                option_conid=8802,
                expiry="2026-12-18",
                dte=28,
                strike=125.0,
                moneyness_pct=0.0,
                right="P",
                bid=3.0,
                ask=3.2,
                mid=3.1,
                last=3.1,
                close=3.0,
                mark_price=3.1,
                volume=20,
                open_interest=200,
                iv=0.50,
                delta=-0.45,
                gamma=0.03,
                theta=-0.06,
                vega=0.12,
                greeks_source="IBKR_MODEL",
            )
            return FetchResult(
                snapshot_id="NVDA_20261120_153000",
                snapshot_ts_utc="2026-11-20T15:30:00Z",
                market_date="2026-11-20",
                market_data_type=3,
                plan=plan,
                rows=[row],
                pages_fetched=1,
                elapsed_ms=5.0,
            )

        async def fetch_exact_contracts(
            self,
            ticker: str,
            specs: list[ExactContractSpec],
            *,
            underlying_conid: int = 0,
        ) -> ExactQuoteResult:
            self.exact_calls.append(list(specs))
            fake_deleg = _FakeFetcher(clock)
            return await fake_deleg.fetch_exact_contracts(
                ticker, specs, underlying_conid=underlying_conid
            )

    rec_fetcher = _RecordingFetcher()
    svc = AnalyzerService(settings=settings, repo=repo, fetcher=rec_fetcher)  # type: ignore[arg-type]
    # Closed/assigned Dec 18 $205P in the Trade Ledger (expired past Nov 20 should be skipped).
    svc.ledger.repo.upsert_executions(
        [
            LedgerExecutionRow(
                exec_id="exec-assigned-205p",
                underlying="NVDA",
                symbol="NVDA  261218P00205000",
                asset_class="OPT",
                expiry="20261218",
                strike=205.0,
                right="P",
                side="SLD",
                quantity=1.0,
                price=6.5,
                proceeds=650.0,
                commission=1.0,
                executed_at="2026-11-06T15:35:00Z",
                source="FLEX_ACTIVITY_365",
                conid=9955,
            )
        ]
    )

    async def _run() -> None:
        assert await svc.acquire_refresh("test")
        try:
            await svc.refresh_ticker_locked(
                "NVDA",
                range_pct=0.20,
                strike_step=5.0,
                dte_min=30,
                dte_max=120,
                include_short_dte=False,
            )
        finally:
            svc.release_refresh()

    run_coro(_run())
    assert rec_fetcher.chain_calls == [(200.0, 200.0)]
    assert len(rec_fetcher.exact_calls) == 1
    assert [(s.expiry, s.strike, s.right, s.option_conid) for s in rec_fetcher.exact_calls[0]] == [
        ("2026-12-18", 205.0, "P", 9955)
    ]
