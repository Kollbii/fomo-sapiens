"""Unit tests for historical backtest service (`list_backtest_entries` and `run_backtest`)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.analytics.backtest import BacktestConfig
from src.analytics.strategies import StrategyLegInput
from src.core.config import PaperTrackerSettings, Settings
from src.core.exceptions import (
    BacktestValidationError,
    InvalidTickerError,
    SnapshotNotFoundError,
)
from src.services.analyzer import AnalyzerService
from src.services.backtest import list_backtest_entries, run_backtest
from src.services.schemas import BacktestRunRequest
from src.storage.csv_repository import CSVRepository
from src.storage.schema import OptionContractRow, SpotSource

_EXPIRY = "2026-12-18"


def _chain_for_day(
    *,
    snapshot_id: str,
    ts_utc: str,
    market_date: str,
    spot: float,
    short_put_mid: float,
    long_put_mid: float,
    market_data_type: int = 3,
    include_quotes: bool = True,
) -> list[dict[str, Any]]:
    contracts = [
        (
            9001,
            130.0,
            "P",
            long_put_mid * 0.65,
            long_put_mid * 0.65 - 0.1,
            long_put_mid * 0.65 + 0.1,
        ),
        (9002, 135.0, "P", long_put_mid, long_put_mid - 0.1, long_put_mid + 0.1),
        (9003, 140.0, "P", short_put_mid, short_put_mid - 0.1, short_put_mid + 0.1),
        (9004, 145.0, "C", 5.2, 5.1, 5.3),
        (9005, 150.0, "C", 3.1, 3.0, 3.2),
    ]
    dte = (date.fromisoformat(_EXPIRY) - date.fromisoformat(market_date)).days
    rows: list[dict[str, Any]] = []
    for conid, strike, right, mid_val, bid_raw, ask_raw in contracts:
        bid = round(max(0.05, bid_raw), 2) if include_quotes else None
        ask = round(max(0.10, ask_raw), 2) if include_quotes else None
        mid = round(mid_val, 2) if include_quotes else None
        rows.append(
            OptionContractRow(
                snapshot_id=snapshot_id,
                snapshot_ts_utc=ts_utc,
                market_date=market_date,
                market_data_type=market_data_type,
                underlying="NVDA",
                underlying_conid=4815747,
                underlying_price=spot,
                option_conid=conid,
                expiry=_EXPIRY,
                dte=dte,
                strike=strike,
                moneyness_pct=(strike - spot) / spot,
                right=right,  # type: ignore[arg-type]
                bid=bid,
                ask=ask,
                mid=mid,
                last=mid,
                close=round(mid_val, 2) if include_quotes else None,
                mark_price=mid,
                volume=100,
                open_interest=500,
                iv=0.42,
                delta=-0.22 if right == "P" else 0.25,
                gamma=0.02,
                theta=-0.04,
                vega=0.11,
                greeks_source="IBKR_MODEL",
            ).model_dump()
        )
    return rows


def _make_svc(tmp_path: Path) -> AnalyzerService:
    settings = Settings(
        tracker=PaperTrackerSettings(max_active_trades=20, tracked_dir="data/tracked")
    )
    repo = CSVRepository(settings.storage, root=tmp_path)
    svc = AnalyzerService(settings=settings, repo=repo)
    svc.ensure_layout()
    return svc


def _seed_multiday_history(svc: AnalyzerService) -> dict[str, str]:
    # 5 trading days in November 2026 (Thu Nov 5 .. Thu Nov 12; bull put decays profitably).
    schedule: list[tuple[str, str, str, float, float, float, int, SpotSource, bool]] = [
        (
            "NVDA_20261105_190000",
            "2026-11-05T19:00:00Z",
            "2026-11-05",
            145.0,
            3.20,
            2.00,
            3,
            "MID",
            True,
        ),
        (
            "NVDA_20261106_190000",
            "2026-11-06T19:00:00Z",
            "2026-11-06",
            146.0,
            2.80,
            1.80,
            3,
            "MID",
            True,
        ),
        # Lower quality / frozen intermediate day + better usable day same history.
        (
            "NVDA_20261109_150000",
            "2026-11-09T15:00:00Z",
            "2026-11-09",
            147.0,
            2.40,
            1.60,
            4,
            "CLOSE",
            False,
        ),
        (
            "NVDA_20261110_190000",
            "2026-11-10T19:00:00Z",
            "2026-11-10",
            148.0,
            1.90,
            1.25,
            3,
            "LAST",
            True,
        ),
        (
            "NVDA_20261112_190000",
            "2026-11-12T19:00:00Z",
            "2026-11-12",
            150.0,
            1.20,
            0.80,
            3,
            "MID",
            True,
        ),
    ]
    ids: dict[str, str] = {}
    for snap_id, ts_utc, mkt_date, spot, sp_mid, lp_mid, md_type, spot_src, has_quotes in schedule:
        svc.repo.save_snapshot(
            "NVDA",
            _chain_for_day(
                snapshot_id=snap_id,
                ts_utc=ts_utc,
                market_date=mkt_date,
                spot=spot,
                short_put_mid=sp_mid,
                long_put_mid=lp_mid,
                market_data_type=md_type,
                include_quotes=has_quotes,
            ),
            spot_source=spot_src,
        )
        ids[mkt_date] = snap_id
    return ids


def test_list_backtest_entries_formats_et_poland_and_quality_verdicts(tmp_path: Path) -> None:
    svc = _make_svc(tmp_path)
    _seed_multiday_history(svc)

    resp = list_backtest_entries(svc, "nvda")
    assert resp.ticker == "NVDA"
    assert [e.snapshot_id for e in resp.entries] == [
        "NVDA_20261112_190000",
        "NVDA_20261110_190000",
        "NVDA_20261109_150000",
        "NVDA_20261106_190000",
        "NVDA_20261105_190000",
    ]
    usable_entry = resp.entries[0]
    assert usable_entry.usable is True
    assert usable_entry.quality_reasons == []
    assert usable_entry.ts_et.endswith(" ET")
    assert usable_entry.ts_poland.endswith((" CET", " CEST"))
    assert usable_entry.spot_source == "MID"
    assert usable_entry.quote_coverage == 1.0

    bad_entry = resp.entries[2]
    assert bad_entry.snapshot_id == "NVDA_20261109_150000"
    assert bad_entry.usable is False
    assert len(bad_entry.quality_reasons) == 3

    with pytest.raises(InvalidTickerError):
        list_backtest_entries(svc, "BAD!")


def test_run_backtest_systematic_nested_and_flat_config(tmp_path: Path) -> None:
    svc = _make_svc(tmp_path)
    _seed_multiday_history(svc)

    # Flat BacktestConfig fields accepted cleanly alongside nested config.
    flat_req = BacktestRunRequest.model_validate(
        {
            "mode": "SYSTEMATIC",
            "ticker": "NVDA",
            "start_date": "2026-11-05",
            "end_date": "2026-11-12",
            "strategy_kind": "BULL_PUT_SPREAD",
            "entry_every_n_days": 1,
            "target_dte_min": 20,
            "target_dte_max": 60,
            "profit_target_pct": 40.0,
        }
    )
    res = run_backtest(svc, flat_req)
    assert res.mode == "SYSTEMATIC"
    assert res.source is None
    assert res.history_snapshot_count == 5
    assert res.daily_bar_count == 5
    assert res.simulation.mode == "SYSTEMATIC"
    assert len(res.simulation.equity_curve) == 5
    assert res.simulation.summary.trade_count >= 1

    # Filtered window limits bars in O(window).
    win_req = BacktestRunRequest(
        mode="SYSTEMATIC",
        ticker="NVDA",
        start_date=date(2026, 11, 10),
        end_date=date(2026, 11, 12),
        config=BacktestConfig(
            strategy_kind="BULL_PUT_SPREAD", target_dte_min=20, target_dte_max=60
        ),
    )
    win_res = run_backtest(svc, win_req)
    assert win_res.history_snapshot_count == 2
    assert win_res.daily_bar_count == 2


def test_run_backtest_single_trade_auto_build_legs_paper_trade_and_secondary_snap(
    tmp_path: Path,
) -> None:
    svc = _make_svc(tmp_path)
    ids = _seed_multiday_history(svc)
    # Add an earlier same-day snapshot on 2026-11-05 so 190000 is canonical and 150000 is secondary.
    secondary_id = "NVDA_20261105_150000"
    svc.repo.save_snapshot(
        "NVDA",
        _chain_for_day(
            snapshot_id=secondary_id,
            ts_utc="2026-11-05T15:00:00Z",
            market_date="2026-11-05",
            spot=144.5,
            short_put_mid=3.35,
            long_put_mid=2.10,
            include_quotes=True,
        ),
        spot_source="MID",
    )

    legs = [
        StrategyLegInput(side="SELL", right="P", expiry=_EXPIRY, strike=140.0),
        StrategyLegInput(side="BUY", right="P", expiry=_EXPIRY, strike=135.0),
    ]
    # Register a PaperTrade anchored on the canonical 2026-11-05 snapshot; fix clock first so
    # 2026-12-18 isn't expired relative to wall clock if wall clock is past 2026-12-18.
    svc.paper_trades.clock = lambda: datetime(2026, 11, 5, 19, 5, 0, tzinfo=UTC)
    paper_trade = svc.paper_trades.register_trade(
        ticker="NVDA",
        snapshot_id=ids["2026-11-05"],
        strategy_kind="BULL_PUT_SPREAD",
        legs=legs,
        qty=2,
    )

    # 1) LEGS replay matches existing active paper trade automatically.
    legs_res = run_backtest(
        svc,
        BacktestRunRequest(
            mode="SINGLE_TRADE",
            ticker="NVDA",
            source="LEGS",
            entry_snapshot_id=ids["2026-11-05"],
            end_snapshot_id=ids["2026-11-10"],
            legs=legs,
            config=BacktestConfig(
                strategy_kind="BULL_PUT_SPREAD", target_dte_min=20, target_dte_max=60
            ),
        ),
    )
    assert legs_res.mode == "SINGLE_TRADE"
    assert legs_res.source == "LEGS"
    assert legs_res.matching_paper_trade is not None
    assert legs_res.matching_paper_trade.id == paper_trade.id
    assert len(legs_res.simulation.trades) == 1
    assert legs_res.simulation.trades[0].entry_snapshot_id == ids["2026-11-05"]

    # 2) PAPER_TRADE replay resolves ticker, entry_snapshot_id, qty, and legs from paper_trade_id.
    # Also seed tracked quotes on 2026-11-09 to ensure tracked_df merges cleanly.
    svc.paper_trades.store.save_quotes(
        "NVDA",
        _chain_for_day(
            snapshot_id="NVDA_20261109_180000",
            ts_utc="2026-11-09T18:00:00Z",
            market_date="2026-11-09",
            spot=147.0,
            short_put_mid=2.30,
            long_put_mid=1.50,
            include_quotes=True,
        ),
    )
    pt_res = run_backtest(
        svc,
        BacktestRunRequest(
            mode="SINGLE_TRADE",
            source="PAPER_TRADE",
            paper_trade_id=paper_trade.id,
            config=BacktestConfig(profit_target_pct=None, stop_loss_pct=None),
        ),
    )
    assert pt_res.source == "PAPER_TRADE"
    assert pt_res.paper_trade_id == paper_trade.id
    assert pt_res.matching_paper_trade is not None
    assert pt_res.matching_paper_trade.id == paper_trade.id
    assert pt_res.simulation.ticker == "NVDA"
    assert pt_res.simulation.trades[0].qty == 2
    assert any(
        "NVDA_20261109_180000" in step.tracked_snapshot_ids
        for step in pt_res.simulation.trades[0].steps
    )

    # 3) AUTO_BUILD starting on the secondary same-day snapshot succeeds.
    auto_res = run_backtest(
        svc,
        BacktestRunRequest(
            mode="SINGLE_TRADE",
            ticker="NVDA",
            source="AUTO_BUILD",
            entry_snapshot_id=secondary_id,
            config=BacktestConfig(
                strategy_kind="BULL_PUT_SPREAD",
                target_dte_min=20,
                target_dte_max=60,
            ),
        ),
    )
    assert auto_res.source == "AUTO_BUILD"
    assert auto_res.simulation.trades[0].entry_snapshot_id == secondary_id


def test_run_backtest_validation_errors(tmp_path: Path) -> None:
    svc = _make_svc(tmp_path)
    ids = _seed_multiday_history(svc)
    legs = [
        StrategyLegInput(side="SELL", right="P", expiry=_EXPIRY, strike=140.0),
        StrategyLegInput(side="BUY", right="P", expiry=_EXPIRY, strike=135.0),
    ]

    with pytest.raises(ValidationError):
        BacktestRunRequest(
            mode="SYSTEMATIC",
            ticker="NVDA",
            start_date=date(2026, 11, 12),
            end_date=date(2026, 11, 5),
        )

    bad_dates = BacktestRunRequest.model_construct(
        mode="SYSTEMATIC",
        ticker="NVDA",
        start_date=date(2026, 11, 12),
        end_date=date(2026, 11, 5),
        config=BacktestConfig(),
    )
    with pytest.raises(BacktestValidationError):
        run_backtest(svc, bad_dates)

    with pytest.raises(SnapshotNotFoundError):
        run_backtest(
            svc,
            BacktestRunRequest(
                mode="SINGLE_TRADE",
                ticker="NVDA",
                source="AUTO_BUILD",
                entry_snapshot_id="NVDA_20261101_190000",
            ),
        )

    with pytest.raises(SnapshotNotFoundError):
        run_backtest(
            svc,
            BacktestRunRequest(
                mode="SINGLE_TRADE",
                source="PAPER_TRADE",
                paper_trade_id="NVDA-20261105-deadbeef",
            ),
        )

    with pytest.raises(BacktestValidationError):
        run_backtest(
            svc,
            BacktestRunRequest(
                mode="SINGLE_TRADE",
                ticker="NVDA",
                source="LEGS",
                entry_snapshot_id=ids["2026-11-05"],
            ),
        )

    with pytest.raises(BacktestValidationError):
        run_backtest(
            svc,
            BacktestRunRequest(
                mode="SINGLE_TRADE",
                ticker="NVDA",
                source="AUTO_BUILD",
                entry_snapshot_id=ids["2026-11-05"],
                legs=legs,
            ),
        )

    with pytest.raises(InvalidTickerError):
        run_backtest(svc, BacktestRunRequest(mode="SYSTEMATIC", ticker="BAD!"))
