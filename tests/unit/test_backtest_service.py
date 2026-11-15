"""Unit tests for historical backtest service (`list_backtest_entries` and `run_backtest`)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.analytics.backtest import FLAG_BROKER_FILL_ENTRY, BacktestConfig
from src.analytics.ledger import (
    LedgerAnalyticsReport,
    LedgerCampaignRow,
    LedgerKPIs,
    LedgerOpenPositionView,
)
from src.analytics.strategies import StrategyLegInput
from src.core.config import PaperTrackerSettings, Settings
from src.core.exceptions import (
    BacktestValidationError,
    InvalidRequestError,
    InvalidTickerError,
    SnapshotNotFoundError,
)
from src.services.analyzer import AnalyzerService
from src.services.backtest import list_backtest_entries, run_backtest
from src.services.schemas import (
    BacktestRunRequest,
    StrategyEvaluateRequest,
    StrategySuggestRequest,
)
from src.storage.csv_repository import CSVRepository
from src.storage.ledger_schema import LedgerPositionRow
from src.storage.paper_trade_store import PaperTrade, PaperTradeLeg
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


def _seed_entry_filter_history(svc: AnalyzerService) -> dict[str, str]:
    """Seed Wed Nov 4..Fri Nov 6 2026 (same ISO week 45) with varying spread credits."""
    # Day 1 (Wed Nov 4): 140/135 credit=$20 (RoR ~4.17%), 140/130 credit=$90 (RoR ~9.89%),
    #                    135/130 credit=$70 (RoR ~16.28%) -> all < $100 and < 20% RoR.
    # Day 2 (Thu Nov 5): 140/135 credit=$140 (max loss $360, RoR ~38.89%).
    # Day 3 (Fri Nov 6): 140/135 credit=$80.
    schedule = [
        ("NVDA_20261104_190000", "2026-11-04T19:00:00Z", "2026-11-04", 145.0, 2.20, 2.00),
        ("NVDA_20261105_190000", "2026-11-05T19:00:00Z", "2026-11-05", 145.0, 3.40, 2.00),
        ("NVDA_20261106_190000", "2026-11-06T19:00:00Z", "2026-11-06", 146.0, 2.80, 2.00),
    ]
    ids: dict[str, str] = {}
    for snap_id, ts_utc, mkt_date, spot, sp_mid, lp_mid in schedule:
        svc.repo.save_snapshot(
            "NVDA",
            _chain_for_day(
                snapshot_id=snap_id,
                ts_utc=ts_utc,
                market_date=mkt_date,
                spot=spot,
                short_put_mid=sp_mid,
                long_put_mid=lp_mid,
                market_data_type=3,
                include_quotes=True,
            ),
            spot_source="MID",
        )
        ids[mkt_date] = snap_id
    return ids


def test_run_backtest_entry_filters_next_day_retry_and_disable_caps(tmp_path: Path) -> None:
    svc = _make_svc(tmp_path)
    ids = _seed_entry_filter_history(svc)

    # Default config: min_entry_credit_dollars=100.0, min_entry_ror_pct=None.
    default_req = BacktestRunRequest(mode="SYSTEMATIC", ticker="NVDA")
    assert default_req.config.min_entry_credit_dollars == 100.0
    assert default_req.config.min_entry_ror_pct is None

    # Wed Nov 4 ($60/$70 < $100 even with qty=5) is skipped and retried next day Thu Nov 5 ($140).
    retry_req = BacktestRunRequest.model_validate(
        {
            "mode": "SYSTEMATIC",
            "ticker": "NVDA",
            "entry_cadence": "WEEKLY_DAY",
            "entry_weekday": 2,  # Wednesday
            "qty": 5,
            "target_dte_min": 20,
            "target_dte_max": 60,
            "profit_target_pct": None,
            "stop_loss_pct": None,
        }
    )
    retry_res = run_backtest(svc, retry_req)
    assert retry_res.simulation.summary.skipped_entries_count == 1
    assert retry_res.simulation.summary.skipped_by_reason == {"BELOW_MIN_CREDIT": 1}
    assert retry_res.simulation.summary.trade_count == 1
    assert retry_res.simulation.trades[0].entry_date == "2026-11-05"
    assert retry_res.simulation.trades[0].entry_snapshot_id == ids["2026-11-05"]

    # High min_entry_ror_pct filters Wed Nov 4 and enters Thu Nov 5 (~38.89% RoR).
    ror_req = BacktestRunRequest.model_validate(
        {
            "mode": "SYSTEMATIC",
            "ticker": "NVDA",
            "entry_cadence": "WEEKLY_DAY",
            "entry_weekday": 2,
            "target_dte_min": 20,
            "target_dte_max": 60,
            "min_entry_credit_dollars": 0.0,
            "min_entry_ror_pct": 25.0,
        }
    )
    ror_res = run_backtest(svc, ror_req)
    assert ror_res.simulation.summary.skipped_entries_count == 1
    assert ror_res.simulation.summary.skipped_by_reason == {"BELOW_MIN_ROR": 1}
    assert len(ror_res.simulation.trades) == 1
    assert ror_res.simulation.trades[0].entry_date == "2026-11-05"

    # Disabling caps via 0.0 or None lets Wed Nov 4 enter immediately with zero skips.
    for disabled_cfg in (
        BacktestConfig(
            entry_cadence="WEEKLY_DAY",
            entry_weekday=2,
            target_dte_min=20,
            target_dte_max=60,
            min_entry_credit_dollars=0.0,
            min_entry_ror_pct=0.0,
        ),
        BacktestConfig(
            entry_cadence="WEEKLY_DAY",
            entry_weekday=2,
            target_dte_min=20,
            target_dte_max=60,
            min_entry_credit_dollars=None,
            min_entry_ror_pct=None,
        ),
    ):
        dis_res = run_backtest(
            svc, BacktestRunRequest(mode="SYSTEMATIC", ticker="NVDA", config=disabled_cfg)
        )
        assert dis_res.simulation.summary.skipped_entries_count == 0
        assert dis_res.simulation.summary.skipped_by_reason == {}
        assert dis_res.simulation.trades[0].entry_date == "2026-11-04"

    # SINGLE_TRADE AUTO_BUILD raises BacktestValidationError (HTTP 422) when entry fails cap.
    with pytest.raises(BacktestValidationError, match="min|credit|RoR|below"):
        run_backtest(
            svc,
            BacktestRunRequest(
                mode="SINGLE_TRADE",
                ticker="NVDA",
                source="AUTO_BUILD",
                entry_snapshot_id=ids["2026-11-04"],
                config=BacktestConfig(
                    target_dte_min=20,
                    target_dte_max=60,
                    min_entry_credit_dollars=100.0,
                ),
            ),
        )


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"min_entry_credit_dollars": -1.0},
        {"min_entry_credit_dollars": float("nan")},
        {"min_entry_credit_dollars": float("inf")},
        {"min_entry_ror_pct": -0.5},
        {"min_entry_ror_pct": 1000.1},
        {"min_entry_ror_pct": float("nan")},
        {"min_entry_ror_pct": float("-inf")},
    ],
)
def test_run_backtest_rejects_invalid_entry_filter_values(
    tmp_path: Path, bad_kwargs: dict[str, Any]
) -> None:
    svc = _make_svc(tmp_path)
    _seed_entry_filter_history(svc)

    # Flat and nested Pydantic request validation reject negative, out-of-range, and non-finite.
    with pytest.raises(ValidationError):
        BacktestRunRequest.model_validate({"mode": "SYSTEMATIC", "ticker": "NVDA", **bad_kwargs})
    with pytest.raises(ValidationError):
        BacktestRunRequest.model_validate(
            {"mode": "SYSTEMATIC", "ticker": "NVDA", "config": bad_kwargs}
        )

    # Defensive service-layer check rejects model_construct bypasses with BacktestValidationError.
    bypass_cfg = BacktestConfig.model_construct(None, **bad_kwargs)
    bypass_req = BacktestRunRequest.model_construct(
        mode="SYSTEMATIC",
        ticker="NVDA",
        config=bypass_cfg,
    )
    with pytest.raises(BacktestValidationError):
        run_backtest(svc, bypass_req)


def test_strategy_lab_optional_entry_filters_keep_defaults_and_validate(tmp_path: Path) -> None:
    svc = _make_svc(tmp_path)
    ids = _seed_entry_filter_history(svc)

    # Default requests behave identically (no filter applied unless explicitly passed).
    default_suggest = svc.suggest_strategies(
        StrategySuggestRequest(ticker="NVDA", snapshot_id=ids["2026-11-04"])
    )
    assert any(ev.strategy_kind == "BULL_PUT_SPREAD" for ev in default_suggest.evaluations)

    default_eval = svc.evaluate_strategy(
        StrategyEvaluateRequest(
            ticker="NVDA",
            snapshot_id=ids["2026-11-04"],
            strategy_kind="BULL_PUT_SPREAD",
        )
    )
    assert default_eval.strategy_kind == "BULL_PUT_SPREAD"

    # Explicit min_net_credit_dollars=100 rejects $60/$70 Wed Nov 4 bull put spread with 422.
    with pytest.raises(InvalidRequestError):
        svc.suggest_strategies(
            StrategySuggestRequest(
                ticker="NVDA",
                snapshot_id=ids["2026-11-04"],
                strategy_kind="BULL_PUT_SPREAD",
                min_net_credit_dollars=100.0,
            )
        )

    with pytest.raises(InvalidRequestError):
        svc.evaluate_strategy(
            StrategyEvaluateRequest(
                ticker="NVDA",
                snapshot_id=ids["2026-11-04"],
                strategy_kind="BULL_PUT_SPREAD",
                min_ror_pct=25.0,
            )
        )

    # 0.0 disables the filter cleanly.
    zero_eval = svc.evaluate_strategy(
        StrategyEvaluateRequest(
            ticker="NVDA",
            snapshot_id=ids["2026-11-04"],
            strategy_kind="BULL_PUT_SPREAD",
            min_net_credit_dollars=0.0,
            min_ror_pct=0.0,
        )
    )
    assert zero_eval.strategy_kind == "BULL_PUT_SPREAD"

    # Invalid values rejected by Pydantic and service defensive validation.
    with pytest.raises(ValidationError):
        StrategySuggestRequest(ticker="NVDA", min_net_credit_dollars=-5.0)
    with pytest.raises(ValidationError):
        StrategyEvaluateRequest(ticker="NVDA", min_ror_pct=float("nan"))
    with pytest.raises(InvalidRequestError):
        svc.evaluate_strategy(
            StrategyEvaluateRequest.model_construct(
                ticker="NVDA",
                snapshot_id=ids["2026-11-04"],
                min_net_credit_dollars=float("inf"),
            )
        )


def _empty_ledger_kpis() -> LedgerKPIs:
    return LedgerKPIs(
        realized_pnl_ex_fee=0.0,
        stock_pnl_ex_fee=0.0,
        total_pnl_ex_fee=0.0,
        net_premium_ex_fee=0.0,
        fees=0.0,
        win_rate_pct=None,
        closed_count=0,
        open_count=0,
        roll_count=0,
        unrealized_mtm_ex_fee=None,
    )


def test_run_backtest_ledger_paper_trade_anchor_ratio_qty_and_synthetic_mtm(
    tmp_path: Path,
) -> None:
    """LEDGER PaperTrades use BrokerFillAnchor, preserve qty_ratio, and synthesize MTM if empty."""
    svc = _make_svc(tmp_path)
    _seed_multiday_history(svc)
    svc.paper_trades.clock = lambda: datetime(2026, 11, 12, 16, 0, tzinfo=UTC)

    report = LedgerAnalyticsReport(
        as_of="2026-11-12T16:00:00Z",
        window="ALL",
        ticker="ALL",
        group_by="campaigns",
        view="open_mtm",
        include_stock_gain=True,
        available_tickers=["NVDA"],
        kpis=_empty_ledger_kpis(),
        campaigns=[
            LedgerCampaignRow(
                campaign_id="camp-nvda-bps",
                underlying="NVDA",
                strategy_type="BULL_PUT_SPREAD",
                status="OPEN",
                opened_at="2026-11-05T15:30:00Z",
                last_activity_at="2026-11-05T15:30:00Z",
                roll_count=0,
                initial_summary="140/135P",
                current_summary="140/135P",
                contracts_total=1.0,
                contracts_open=1.0,
                net_premium_ex_fee=150.0,
                realized_pnl_ex_fee=0.0,
                fees=1.3,
            ),
            LedgerCampaignRow(
                campaign_id="camp-nvda-ratio",
                underlying="NVDA",
                strategy_type="CUSTOM_COMBO",
                status="OPEN",
                opened_at="2026-11-05T16:00:00Z",
                last_activity_at="2026-11-05T16:00:00Z",
                roll_count=0,
                initial_summary="-2x140P/+4x135P",
                current_summary="-2x140P/+4x135P",
                contracts_total=6.0,
                contracts_open=6.0,
                net_premium_ex_fee=-120.0,
                realized_pnl_ex_fee=0.0,
                fees=3.0,
            ),
        ],
        open_positions=[
            # Distinct broker fill ($150 net credit vs snapshot mid $120 on 2026-11-05)
            LedgerOpenPositionView(
                position_key="NVDA|OPT|20261218|140.0|P",
                underlying="NVDA",
                asset_class="OPT",
                strategy_type="BULL_PUT_SPREAD",
                symbol="NVDA  261218P00140000",
                expiry="20261218",
                strike=140.0,
                right="P",
                quantity=-1.0,
                avg_cost_price=3.60,
                mark_price=1.20,
                mark_source="FLEX_MARK",
                cost_basis_ex_fee=-360.0,
                market_value=-120.0,
                unrealized_pnl_ex_fee=240.0,
                fees=0.65,
                campaign_id="camp-nvda-bps",
                conid=9101,
                opened_at="2026-11-05T15:30:00Z",
            ),
            LedgerOpenPositionView(
                position_key="NVDA|OPT|20261218|135.0|P",
                underlying="NVDA",
                asset_class="OPT",
                strategy_type="BULL_PUT_SPREAD",
                symbol="NVDA  261218P00135000",
                expiry="20261218",
                strike=135.0,
                right="P",
                quantity=1.0,
                avg_cost_price=2.10,
                mark_price=0.80,
                mark_source="FLEX_MARK",
                cost_basis_ex_fee=210.0,
                market_value=80.0,
                unrealized_pnl_ex_fee=-130.0,
                fees=0.65,
                campaign_id="camp-nvda-bps",
                conid=9102,
                opened_at="2026-11-05T15:30:00Z",
            ),
            # Ratio spread (-2 short 140P, +4 long 135P) -> trade.qty=2, qty_ratio=(1, 2)
            LedgerOpenPositionView(
                position_key="NVDA|OPT|20261218|140.0|P|RATIO",
                underlying="NVDA",
                asset_class="OPT",
                strategy_type="CUSTOM_COMBO",
                symbol="NVDA  261218P00140000",
                expiry="20261218",
                strike=140.0,
                right="P",
                quantity=-2.0,
                avg_cost_price=3.60,
                mark_price=1.20,
                mark_source="FLEX_MARK",
                cost_basis_ex_fee=-720.0,
                market_value=-240.0,
                unrealized_pnl_ex_fee=480.0,
                fees=1.0,
                campaign_id="camp-nvda-ratio",
                conid=9101,
                opened_at="2026-11-05T16:00:00Z",
            ),
            LedgerOpenPositionView(
                position_key="NVDA|OPT|20261218|135.0|P|RATIO",
                underlying="NVDA",
                asset_class="OPT",
                strategy_type="CUSTOM_COMBO",
                symbol="NVDA  261218P00135000",
                expiry="20261218",
                strike=135.0,
                right="P",
                quantity=4.0,
                avg_cost_price=2.10,
                mark_price=0.80,
                mark_source="FLEX_MARK",
                cost_basis_ex_fee=840.0,
                market_value=320.0,
                unrealized_pnl_ex_fee=-520.0,
                fees=2.0,
                campaign_id="camp-nvda-ratio",
                conid=9102,
                opened_at="2026-11-05T16:00:00Z",
            ),
        ],
    )
    synced = svc.paper_trades.sync_from_ledger_report(report)
    assert synced.created_count == 2
    by_campaign = {t.ledger_campaign_id: t for t in synced.trades}
    bps_trade = by_campaign["camp-nvda-bps"]
    ratio_trade = by_campaign["camp-nvda-ratio"]

    # 1) Replay with historical daily bars uses BrokerFillAnchor on Day 0, then daily chain bars.
    bps_res = run_backtest(
        svc,
        BacktestRunRequest(
            mode="SINGLE_TRADE",
            source="PAPER_TRADE",
            paper_trade_id=bps_trade.id,
            config=BacktestConfig(profit_target_pct=None, stop_loss_pct=None),
        ),
    )
    assert bps_res.daily_bar_count == 5
    bps_rec = bps_res.simulation.trades[0]
    assert bps_rec.trade_id == bps_trade.id
    assert bps_rec.entry_snapshot_id == "NVDA_20261105_LEDGER"
    assert bps_rec.entry_date == "2026-11-05"
    assert bps_rec.entry_cash_mid_dollars == pytest.approx(150.0)
    assert FLAG_BROKER_FILL_ENTRY in bps_rec.flags
    assert bps_rec.steps[0].mark_source == "BROKER_FILL"
    assert bps_rec.steps[0].market_date == "2026-11-05"
    assert bps_rec.steps[-1].market_date == "2026-11-12"
    assert bps_rec.steps[-1].mark_source == "MID"

    # 2) Ratio spread replay preserves leg qty = qty_ratio * trade.qty (SELL 2, BUY 4).
    ratio_res = run_backtest(
        svc,
        BacktestRunRequest(
            mode="SINGLE_TRADE",
            source="PAPER_TRADE",
            paper_trade_id=ratio_trade.id,
            config=BacktestConfig(profit_target_pct=None, stop_loss_pct=None),
        ),
    )
    ratio_rec = ratio_res.simulation.trades[0]
    assert [(leg.side, leg.qty) for leg in ratio_rec.legs] == [("SELL", 2), ("BUY", 4)]

    # 3) Zero local chain snapshots on/after market_date triggers synthetic MTM bar fallback:
    #    covers both Ledger mark_price (PLTR) and mark_price=None -> entry price (AAPL).
    svc.ledger.repo.replace_positions(
        [
            LedgerPositionRow(
                conid=9201,
                symbol="PLTR  261218P00050000",
                underlying="PLTR",
                asset_class="OPT",
                expiry="20261218",
                strike=50.0,
                right="P",
                multiplier=100,
                position=-2.0,
                avg_cost=2.50,
                mark_price=1.80,
                market_value=-360.0,
                unrealized_pnl=140.0,
                source="FLEX_ACTIVITY_365",
                updated_at="2026-11-12T15:00:00Z",
            ),
            LedgerPositionRow(
                conid=9202,
                symbol="AAPL  261218C00220000",
                underlying="AAPL",
                asset_class="OPT",
                expiry="20261218",
                strike=220.0,
                right="C",
                multiplier=100,
                position=-1.0,
                avg_cost=3.10,
                mark_price=None,
                market_value=None,
                unrealized_pnl=None,
                source="FLEX_ACTIVITY_365",
                updated_at="2026-11-12T15:00:00Z",
            ),
        ]
    )
    svc.sync_paper_trades_from_ledger()
    all_after, _ = svc.paper_trades.list_trades(status="ACTIVE")
    pltr_trade = next(t for t in all_after if t.ticker == "PLTR")
    aapl_trade = next(t for t in all_after if t.ticker == "AAPL")

    pltr_res = run_backtest(
        svc,
        BacktestRunRequest(
            mode="SINGLE_TRADE",
            source="PAPER_TRADE",
            paper_trade_id=pltr_trade.id,
            config=BacktestConfig(profit_target_pct=None, stop_loss_pct=None),
        ),
    )
    assert pltr_res.daily_bar_count == 1
    assert any("PLTR_2026-11-12_LEDGER_MTM" in w for w in pltr_res.warnings)
    pltr_rec = pltr_res.simulation.trades[0]
    assert pltr_rec.entry_snapshot_id == "PLTR_20261112_LEDGER"
    assert pltr_rec.entry_cash_mid_dollars == pytest.approx(500.0)
    assert pltr_rec.steps[-1].snapshot_id == "PLTR_2026-11-12_LEDGER_MTM"
    assert pltr_rec.steps[-1].gross_pnl_dollars == pytest.approx(140.0)

    aapl_res = run_backtest(
        svc,
        BacktestRunRequest(
            mode="SINGLE_TRADE",
            source="PAPER_TRADE",
            paper_trade_id=aapl_trade.id,
            config=BacktestConfig(profit_target_pct=None, stop_loss_pct=None),
        ),
    )
    assert aapl_res.daily_bar_count == 1
    aapl_rec = aapl_res.simulation.trades[0]
    assert aapl_rec.steps[-1].snapshot_id == "AAPL_2026-11-12_LEDGER_MTM"
    assert aapl_rec.steps[-1].gross_pnl_dollars == pytest.approx(0.0)


def test_run_backtest_ledger_paper_trade_scales_entry_cash_when_qty_overridden(
    tmp_path: Path,
) -> None:
    """Explicit qty override scales entry_net_cash_dollars and synthetic MTM identically."""
    svc = _make_svc(tmp_path)
    _seed_multiday_history(svc)
    svc.paper_trades.clock = lambda: datetime(2026, 11, 12, 16, 0, tzinfo=UTC)

    # Multi-lot (qty=2) LEDGER short put: -2 @ $2.50 -> entry_net_cash=$500, MTM=$1.80 (+$140).
    svc.ledger.repo.replace_positions(
        [
            LedgerPositionRow(
                conid=9301,
                symbol="PLTR  261218P00050000",
                underlying="PLTR",
                asset_class="OPT",
                expiry="20261218",
                strike=50.0,
                right="P",
                multiplier=100,
                position=-2.0,
                avg_cost=2.50,
                mark_price=1.80,
                market_value=-360.0,
                unrealized_pnl=140.0,
                source="FLEX_ACTIVITY_365",
                updated_at="2026-11-12T15:00:00Z",
            ),
            LedgerPositionRow(
                conid=9302,
                symbol="NVDA  261218P00140000",
                underlying="NVDA",
                asset_class="OPT",
                expiry="20261218",
                strike=140.0,
                right="P",
                multiplier=100,
                position=-2.0,
                avg_cost=3.20,
                mark_price=1.20,
                market_value=-240.0,
                unrealized_pnl=400.0,
                source="FLEX_ACTIVITY_365",
                updated_at="2026-11-05T15:30:00Z",
            ),
        ]
    )
    svc.sync_paper_trades_from_ledger()

    # Also register a qty=2 LEDGER trade with entry_price=None and no Ledger MTM mark to verify
    # resolve_broker_leg_prices uses scaled_entry_net_cash with eff_qty for synthetic MTM bars.
    no_mark_trade = PaperTrade(
        id="CRDO-20261112-deadbeef",
        ticker="CRDO",
        strategy_kind="CUSTOM",
        status="ACTIVE",
        legs=[
            PaperTradeLeg(
                side="SELL",
                right="P",
                expiry=_EXPIRY,
                strike=50.0,
                qty_ratio=1,
                entry_price=None,
            )
        ],
        qty=2,
        created_from_snapshot_id=None,
        created_utc="2026-11-12T15:00:00Z",
        market_date="2026-11-12",
        entry_net_cash=500.0,
        source="LEDGER",
    )
    existing_trades = svc.paper_trades.store.load_trades()
    svc.paper_trades.store.save_trades([*existing_trades, no_mark_trade])

    active_trades, _ = svc.paper_trades.list_trades(status="ACTIVE")
    pltr_trade = next(t for t in active_trades if t.ticker == "PLTR")
    nvda_trade = next(t for t in active_trades if t.ticker == "NVDA")
    assert pltr_trade.qty == 2
    assert pltr_trade.entry_net_cash == pytest.approx(500.0)

    # 1) Synthetic MTM replay with explicit "qty": 1 in config -> $250 entry cash, $70 MTM P&L.
    pltr_req = BacktestRunRequest.model_validate(
        {
            "mode": "SINGLE_TRADE",
            "source": "PAPER_TRADE",
            "paper_trade_id": pltr_trade.id,
            "config": {"qty": 1, "profit_target_pct": None, "stop_loss_pct": None},
        }
    )
    pltr_res = run_backtest(svc, pltr_req)
    pltr_rec = pltr_res.simulation.trades[0]
    assert pltr_rec.qty == 1
    assert pltr_rec.entry_cash_mid_dollars == pytest.approx(250.0)
    assert pltr_rec.steps[0].mark_source == "BROKER_FILL"
    assert pltr_rec.steps[0].gross_pnl_dollars == pytest.approx(0.0)
    assert pltr_rec.steps[-1].snapshot_id == "PLTR_2026-11-12_LEDGER_MTM"
    assert pltr_rec.steps[-1].gross_pnl_dollars == pytest.approx(70.0)
    assert pltr_rec.gross_pnl_dollars == pytest.approx(70.0)

    # 2) Historical daily-bar replay with explicit "qty": 1 -> $320 entry, +$200 on 2026-11-12.
    nvda_req = BacktestRunRequest.model_validate(
        {
            "mode": "SINGLE_TRADE",
            "source": "PAPER_TRADE",
            "paper_trade_id": nvda_trade.id,
            "config": {"qty": 1, "profit_target_pct": None, "stop_loss_pct": None},
        }
    )
    nvda_res = run_backtest(svc, nvda_req)
    nvda_rec = nvda_res.simulation.trades[0]
    assert nvda_rec.qty == 1
    assert nvda_rec.entry_cash_mid_dollars == pytest.approx(320.0)
    assert nvda_rec.steps[0].gross_pnl_dollars == pytest.approx(0.0)
    assert nvda_rec.steps[-1].market_date == "2026-11-12"
    assert nvda_rec.steps[-1].gross_pnl_dollars == pytest.approx(200.0)

    # 3) Synthetic MTM fallback without mark_price or leg.entry_price scales mark to $2.50 ($0 P&L).
    crdo_req = BacktestRunRequest.model_validate(
        {
            "mode": "SINGLE_TRADE",
            "source": "PAPER_TRADE",
            "paper_trade_id": no_mark_trade.id,
            "config": {"qty": 1, "profit_target_pct": None, "stop_loss_pct": None},
        }
    )
    crdo_res = run_backtest(svc, crdo_req)
    crdo_rec = crdo_res.simulation.trades[0]
    assert crdo_rec.qty == 1
    assert crdo_rec.entry_cash_mid_dollars == pytest.approx(250.0)
    assert crdo_rec.steps[0].gross_pnl_dollars == pytest.approx(0.0)
    assert crdo_rec.steps[-1].snapshot_id == "CRDO_2026-11-12_LEDGER_MTM"
    assert crdo_rec.steps[-1].gross_pnl_dollars == pytest.approx(0.0)


def test_run_backtest_paper_trade_rejects_end_date_before_entry_date(tmp_path: Path) -> None:
    """Replaying a PAPER_TRADE with end_date < paper_trade.market_date raises validation error."""
    svc = _make_svc(tmp_path)
    svc.paper_trades.clock = lambda: datetime(2026, 11, 12, 16, 0, tzinfo=UTC)
    svc.ledger.repo.replace_positions(
        [
            LedgerPositionRow(
                conid=9301,
                symbol="PLTR  261218P00050000",
                underlying="PLTR",
                asset_class="OPT",
                expiry="20261218",
                strike=50.0,
                right="P",
                multiplier=100,
                position=-2.0,
                avg_cost=2.50,
                mark_price=1.80,
                market_value=-360.0,
                unrealized_pnl=140.0,
                source="FLEX_ACTIVITY_365",
                updated_at="2026-11-12T15:00:00Z",
            )
        ]
    )
    svc.sync_paper_trades_from_ledger()
    pltr_trade = svc.paper_trades.list_trades(status="ACTIVE")[0][0]
    assert pltr_trade.market_date == "2026-11-12"

    with pytest.raises(BacktestValidationError, match="end_date"):
        run_backtest(
            svc,
            BacktestRunRequest(
                mode="SINGLE_TRADE",
                source="PAPER_TRADE",
                paper_trade_id=pltr_trade.id,
                end_date=date(2026, 11, 11),
            ),
        )

    with pytest.raises(BacktestValidationError, match="end_date"):
        run_backtest(
            svc,
            BacktestRunRequest(
                mode="SINGLE_TRADE",
                source="PAPER_TRADE",
                paper_trade_id=pltr_trade.id,
                start_date=date(2026, 11, 10),
                end_date=date(2026, 11, 11),
            ),
        )
