"""Unit tests for pure backtest engine, cadence rules, presets, TP/SL bases, and guards."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest

from src.analytics.backtest import (
    FLAG_NO_USABLE_DAILY_SNAPSHOT,
    BacktestConfig,
    run_single_trade_replay,
    run_systematic_backtest,
)
from src.analytics.bsm_fallback import DAYS_PER_YEAR, DEFAULT_RISK_FREE_RATE, bsm_greeks, bsm_price
from src.analytics.strategies.models import PresetStrategyKind, StrategyLegInput
from src.core.exceptions import BacktestValidationError, InvalidTickerError
from src.storage.backtest_store import NO_USABLE_DAILY_SNAPSHOT, DailyChainBar
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, OptionRight, SnapshotMeta, SpotSource


def _make_chain_df(
    *,
    snapshot_id: str,
    ts_utc: datetime,
    market_date: str,
    spot: float = 100.0,
    strikes: tuple[float, ...] = (85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0),
    expiries: tuple[tuple[str, int], ...] = (("2026-12-18", 38), ("2027-01-15", 66)),
    iv: float = 0.35,
    overrides: dict[tuple[str, float, OptionRight], dict[str, Any]] | None = None,
) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    conid = 20_000
    ts_str = ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    day_d = date.fromisoformat(market_date)
    for expiry, explicit_dte in expiries:
        dte = (
            explicit_dte if explicit_dte >= 0 else max(0, (date.fromisoformat(expiry) - day_d).days)
        )
        t_years = max(float(dte), 0.5) / DAYS_PER_YEAR
        for strike in strikes:
            for right in ("C", "P"):
                right_lit: OptionRight = "C" if right == "C" else "P"
                conid += 1
                s_arr = np.array([spot], dtype=np.float64)
                k_arr = np.array([strike], dtype=np.float64)
                t_arr = np.array([t_years], dtype=np.float64)
                iv_arr = np.array([iv], dtype=np.float64)
                c_arr = np.array([right_lit == "C"], dtype=np.bool_)
                theo = float(
                    bsm_price(s_arr, k_arr, t_arr, DEFAULT_RISK_FREE_RATE, iv_arr, c_arr)[0]
                )
                d_arr, g_arr, th_arr, v_arr = bsm_greeks(
                    s_arr, k_arr, t_arr, DEFAULT_RISK_FREE_RATE, iv_arr, c_arr
                )
                bid = round(max(0.05, theo - 0.05), 2)
                ask = round(max(0.10, theo + 0.05), 2)
                mid = round((bid + ask) / 2.0, 2)
                row: dict[str, Any] = {
                    "snapshot_id": snapshot_id,
                    "snapshot_ts_utc": ts_str,
                    "market_date": market_date,
                    "market_data_type": 3,
                    "underlying": "NVDA",
                    "underlying_conid": 4815,
                    "underlying_price": spot,
                    "option_conid": conid,
                    "expiry": expiry,
                    "dte": dte,
                    "strike": strike,
                    "moneyness_pct": round((strike - spot) / spot * 100.0, 4),
                    "right": right_lit,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "last": mid,
                    "close": round(max(0.05, mid - 0.05), 2),
                    "mark_price": mid,
                    "volume": 150,
                    "open_interest": 800,
                    "iv": iv,
                    "delta": float(d_arr[0]),
                    "gamma": float(g_arr[0]),
                    "theta": float(th_arr[0]),
                    "vega": float(v_arr[0]),
                    "greeks_source": "IBKR_MODEL",
                }
                if overrides and (expiry, strike, right_lit) in overrides:
                    row.update(overrides[(expiry, strike, right_lit)])
                rows.append(row)
    return pl.DataFrame(rows, schema=OPTION_CHAIN_POLARS_SCHEMA)


def _bar(
    snapshot_id: str,
    ts_utc: datetime,
    market_date: str,
    df: pl.DataFrame,
    *,
    spot: float = 100.0,
    spot_source: SpotSource = "LAST",
    selection_flags: tuple[str, ...] = (),
    tracked_df: pl.DataFrame | None = None,
    tracked_snapshot_ids: tuple[str, ...] = (),
) -> DailyChainBar:
    expirations = sorted({str(x) for x in df["expiry"].to_list()}) if df.height > 0 else []
    meta = SnapshotMeta(
        snapshot_id=snapshot_id,
        file_path=f"{snapshot_id}.csv",
        snapshot_ts_utc=ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        market_date=market_date,
        market_data_type=3,
        underlying_price=spot,
        contract_count=df.height,
        expirations=expirations,
        spot_source=spot_source,
        quoted_contract_count=df.height,
        quote_coverage=1.0 if df.height > 0 else 0.0,
        prior_close_only_count=0,
    )
    return DailyChainBar(
        snapshot_id=snapshot_id,
        ts_utc=ts_utc,
        market_date=market_date,
        df=df,
        meta=meta,
        selection_flags=selection_flags,
        tracked_df=tracked_df,
        tracked_snapshot_ids=tracked_snapshot_ids,
    )


@pytest.mark.parametrize(
    "preset",
    ["BULL_PUT_SPREAD", "BULL_CALL_SPREAD", "DIAGONAL_SPREAD", "IRON_CONDOR"],
)
def test_all_four_presets_simulate_end_to_end(preset: PresetStrategyKind) -> None:
    d0 = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)  # Friday
    d1 = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)  # Next Friday
    exp_front = "2026-12-18"  # ~42 DTE from Nov 6
    exp_far = "2027-02-19"  # ~105 DTE (> target_dte_max=60 for DIAGONAL_SPREAD test)

    b0 = _bar(
        "s0",
        d0,
        "2026-11-06",
        _make_chain_df(
            snapshot_id="s0",
            ts_utc=d0,
            market_date="2026-11-06",
            spot=100.0,
            expiries=((exp_front, -1), (exp_far, -1)),
        ),
        spot=100.0,
    )
    b1 = _bar(
        "s1",
        d1,
        "2026-11-13",
        _make_chain_df(
            snapshot_id="s1",
            ts_utc=d1,
            market_date="2026-11-13",
            spot=102.0,
            expiries=((exp_front, -1), (exp_far, -1)),
        ),
        spot=102.0,
    )
    cfg = BacktestConfig(
        strategy_kind=preset,
        target_dte_min=30,
        target_dte_max=60,
        entry_cadence="WHEN_FLAT",
        profit_target_pct=None,
        stop_loss_pct=None,
    )
    res = run_systematic_backtest([b0, b1], cfg, ticker="nvda")
    assert res.ticker == "NVDA"
    assert res.summary.trade_count == 1
    assert res.summary.open_trades == 1
    assert len(res.equity_curve) == 2
    trade = res.trades[0]
    assert trade.strategy_kind == preset
    assert trade.exit_reason == "END_OF_DATA"
    if preset == "DIAGONAL_SPREAD":
        exp_set = {leg.expiry for leg in trade.legs}
        assert exp_set == {exp_front, exp_far}


def test_unsorted_bars_chronological_exits_before_entries_and_same_bar_reentry() -> None:
    # Bar 0 (Mon 2026-11-02): enter bull put spread at $1.50 credit (95P $2.50, 90P $1.00)
    # Bar 1 (Tue 2026-11-03): prices fall so spread costs $0.50 -> +$100 (>50% profit target!)
    # With WHEN_FLAT + max_open_trades=1, exit MUST occur BEFORE entry attempt on Bar 1,
    # allowing Trade 2 to open cleanly on Bar 1 itself.
    legs_custom = [
        StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=95.0, qty=1),
        StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=90.0, qty=1),
    ]
    t0 = datetime(2026, 11, 2, 20, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
    ov0: dict[tuple[str, float, OptionRight], dict[str, Any]] = {
        ("2026-12-18", 95.0, "P"): {"bid": 2.4, "ask": 2.6, "mid": 2.5, "last": 2.5},
        ("2026-12-18", 90.0, "P"): {"bid": 0.9, "ask": 1.1, "mid": 1.0, "last": 1.0},
    }
    ov1: dict[tuple[str, float, OptionRight], dict[str, Any]] = {
        ("2026-12-18", 95.0, "P"): {"bid": 1.4, "ask": 1.6, "mid": 1.5, "last": 1.5},
        ("2026-12-18", 90.0, "P"): {"bid": 0.9, "ask": 1.1, "mid": 1.0, "last": 1.0},
    }
    b0 = _bar(
        "s0",
        t0,
        "2026-11-02",
        _make_chain_df(
            snapshot_id="s0",
            ts_utc=t0,
            market_date="2026-11-02",
            expiries=(("2026-12-18", 46),),
            overrides=ov0,
        ),
    )
    b1 = _bar(
        "s1",
        t1,
        "2026-11-03",
        _make_chain_df(
            snapshot_id="s1",
            ts_utc=t1,
            market_date="2026-11-03",
            expiries=(("2026-12-18", 45),),
            overrides=ov1,
        ),
    )
    cfg = BacktestConfig(
        strategy_kind="CUSTOM",
        entry_cadence="WHEN_FLAT",
        max_open_trades=1,
        qty=2,
        commission_per_contract=0.65,
        tp_sl_basis="CREDIT_DEBIT_PCT",
        profit_target_pct=50.0,
        stop_loss_pct=100.0,
    )
    # Pass bars in REVERSE order to prove strict chronological sorting
    res = run_systematic_backtest([b1, b0], cfg, ticker="NVDA", legs=legs_custom)
    assert [pt.market_date for pt in res.equity_curve] == ["2026-11-02", "2026-11-03"]
    assert res.summary.trade_count == 2
    assert res.summary.closed_trades == 1
    assert res.summary.open_trades == 1

    tr1, tr2 = res.trades
    assert tr1.entry_date == "2026-11-02"
    assert tr1.exit_date == "2026-11-03"
    assert tr1.exit_reason == "PROFIT_TARGET"
    # qty=2 on 2 legs -> 4 contracts total; open + close commission = 8 * 0.65 = 5.20
    assert tr1.commissions_dollars == pytest.approx(5.20)
    # Gross PnL = +$300 entry - $100 close = +$200; net = 200 - 5.20 = 194.80
    assert tr1.gross_pnl_dollars == pytest.approx(200.0)
    assert tr1.net_pnl_dollars == pytest.approx(194.80)
    assert tr2.entry_date == "2026-11-03"
    assert tr2.commissions_dollars == pytest.approx(2.60)
    # All winning closed trade -> profit_factor=None (no zero division), avg_loss=None
    assert res.summary.profit_factor is None
    assert res.summary.avg_loss_dollars is None
    assert res.summary.win_rate_pct == pytest.approx(100.0)


def test_cadence_weekly_day_holiday_adjustment_and_every_n_days() -> None:
    # MLK week 2026: Mon 2026-01-19 is NYSE holiday -> entry_weekday=0 shifts to Tue 2026-01-20
    dates_jan = ["2026-01-20", "2026-01-21", "2026-01-26", "2026-01-27"]
    bars_jan = []
    for idx, d_str in enumerate(dates_jan):
        ts = datetime.fromisoformat(f"{d_str}T20:00:00+00:00")
        bars_jan.append(
            _bar(
                f"jan_{idx}",
                ts,
                d_str,
                _make_chain_df(
                    snapshot_id=f"jan_{idx}",
                    ts_utc=ts,
                    market_date=d_str,
                    expiries=(("2026-03-20", -1),),
                ),
            )
        )
    cfg_mon = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        target_dte_min=30,
        target_dte_max=70,
        entry_cadence="WEEKLY_DAY",
        entry_weekday=0,
        max_open_trades=5,
        profit_target_pct=None,
        stop_loss_pct=None,
    )
    res_mon = run_systematic_backtest(bars_jan, cfg_mon, ticker="NVDA")
    assert [t.entry_date for t in res_mon.trades] == ["2026-01-20", "2026-01-26"]

    # Thanksgiving week 2026: Thu 2026-11-26 holiday -> entry_weekday=3 shifts to Fri 2026-11-27
    bars_nov = []
    for idx, d_str in enumerate(["2026-11-25", "2026-11-27"]):
        ts = datetime.fromisoformat(f"{d_str}T17:00:00+00:00")
        bars_nov.append(
            _bar(
                f"nov_{idx}",
                ts,
                d_str,
                _make_chain_df(
                    snapshot_id=f"nov_{idx}",
                    ts_utc=ts,
                    market_date=d_str,
                    expiries=(("2027-01-15", -1),),
                ),
            )
        )
    cfg_thu = cfg_mon.model_copy(update={"entry_weekday": 3})
    res_thu = run_systematic_backtest(bars_nov, cfg_thu, ticker="NVDA")
    assert [t.entry_date for t in res_thu.trades] == ["2026-11-27"]

    # EVERY_N_DAYS = 2 -> enters on bar 0 (Jan 20) and bar 2 (Jan 26)
    cfg_every2 = cfg_mon.model_copy(
        update={"entry_cadence": "EVERY_N_DAYS", "entry_every_n_days": 2}
    )
    res_every2 = run_systematic_backtest(bars_jan, cfg_every2, ticker="NVDA")
    assert [t.entry_date for t in res_every2.trades] == ["2026-01-20", "2026-01-26"]


def test_tp_sl_basis_credit_debit_pct_vs_max_risk_pct_and_exit_dte() -> None:
    legs = [
        StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=95.0, qty=1),
        StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=90.0, qty=1),
    ]
    # Entry credit = $1.00 ($100 basis), $5 wide -> max_loss = $400.
    # Day 1 loss = -$70 -> 70% of credit (triggers 50% SL under CREDIT_DEBIT_PCT),
    # only 17.5% of max risk (does NOT trigger 50% SL under MAX_RISK_PCT!).
    t0 = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 12, 20, 0, tzinfo=UTC)
    b0 = _bar(
        "b0",
        t0,
        "2026-11-10",
        _make_chain_df(
            snapshot_id="b0",
            ts_utc=t0,
            market_date="2026-11-10",
            expiries=(("2026-12-18", 38),),
            overrides={
                ("2026-12-18", 95.0, "P"): {"bid": 1.9, "ask": 2.1, "mid": 2.0, "last": 2.0},
                ("2026-12-18", 90.0, "P"): {"bid": 0.9, "ask": 1.1, "mid": 1.0, "last": 1.0},
            },
        ),
    )
    b1 = _bar(
        "b1",
        t1,
        "2026-11-11",
        _make_chain_df(
            snapshot_id="b1",
            ts_utc=t1,
            market_date="2026-11-11",
            expiries=(("2026-12-18", 37),),
            overrides={
                ("2026-12-18", 95.0, "P"): {"bid": 2.6, "ask": 2.8, "mid": 2.7, "last": 2.7},
                ("2026-12-18", 90.0, "P"): {"bid": 0.9, "ask": 1.1, "mid": 1.0, "last": 1.0},
            },
        ),
    )
    b2 = _bar(
        "b2",
        t2,
        "2026-11-12",
        _make_chain_df(
            snapshot_id="b2",
            ts_utc=t2,
            market_date="2026-11-12",
            expiries=(("2026-12-18", 36),),
            overrides={
                ("2026-12-18", 95.0, "P"): {"bid": 3.4, "ask": 3.6, "mid": 3.5, "last": 3.5},
                ("2026-12-18", 90.0, "P"): {"bid": 0.9, "ask": 1.1, "mid": 1.0, "last": 1.0},
            },
        ),
    )

    cfg_credit = BacktestConfig(
        strategy_kind="CUSTOM",
        tp_sl_basis="CREDIT_DEBIT_PCT",
        profit_target_pct=50.0,
        stop_loss_pct=50.0,
    )
    res_credit = run_single_trade_replay(
        [b0, b1, b2], cfg_credit, ticker="NVDA", entry_snapshot_id="b0", legs=legs
    )
    assert res_credit.trades[0].exit_date == "2026-11-11"
    assert res_credit.trades[0].exit_reason == "STOP_LOSS"

    cfg_risk = cfg_credit.model_copy(update={"tp_sl_basis": "MAX_RISK_PCT"})
    res_risk = run_single_trade_replay(
        [b0, b1, b2], cfg_risk, ticker="NVDA", entry_snapshot_id="b0", legs=legs
    )
    assert res_risk.trades[0].exit_date == "2026-11-12"  # -150 vs max_loss 400 -> 37.5%, holds!
    assert res_risk.trades[0].exit_reason == "END_OF_DATA"

    # With exit_dte=37, EXIT_DTE closes on b1 cleanly
    cfg_dte = cfg_risk.model_copy(update={"exit_dte": 37})
    res_dte = run_single_trade_replay(
        [b0, b1, b2], cfg_dte, ticker="NVDA", entry_snapshot_id="b0", legs=legs
    )
    assert res_dte.trades[0].exit_date == "2026-11-11"
    assert res_dte.trades[0].exit_reason == "EXIT_DTE"


def test_diagonal_settlement_charges_commission_only_on_unexpired_leg() -> None:
    legs = [
        StrategyLegInput(side="SELL", right="C", expiry="2026-11-20", strike=105.0, qty=1),
        StrategyLegInput(side="BUY", right="C", expiry="2026-12-18", strike=105.0, qty=1),
    ]
    t0 = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
    t_settle = datetime(2026, 11, 20, 21, 15, tzinfo=UTC)  # Regular expiry cutoff 16:15 EST
    b0 = _bar(
        "s0",
        t0,
        "2026-11-10",
        _make_chain_df(
            snapshot_id="s0",
            ts_utc=t0,
            market_date="2026-11-10",
            expiries=(("2026-11-20", 10), ("2026-12-18", 38)),
        ),
    )
    b1 = _bar(
        "s1",
        t_settle,
        "2026-11-20",
        _make_chain_df(
            snapshot_id="s1",
            ts_utc=t_settle,
            market_date="2026-11-20",
            expiries=(("2026-12-18", 28),),
        ),
    )
    cfg = BacktestConfig(
        strategy_kind="CUSTOM",
        qty=2,
        commission_per_contract=1.0,
        profit_target_pct=None,
        stop_loss_pct=None,
    )
    res = run_single_trade_replay([b0, b1], cfg, ticker="NVDA", entry_snapshot_id="s0", legs=legs)
    tr = res.trades[0]
    assert tr.exit_reason == "EXPIRY_SETTLED"
    # Open = 4 contracts ($4.00); short settles ($0) + closes back 2 contracts ($2.00) -> $6.00
    assert tr.commissions_dollars == pytest.approx(6.0)


def test_naked_short_call_max_risk_pct_never_divides_by_zero_and_skips_tpsl() -> None:
    naked = [StrategyLegInput(side="SELL", right="C", expiry="2026-12-18", strike=105.0, qty=1)]
    t0 = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
    b0 = _bar(
        "n0",
        t0,
        "2026-11-10",
        _make_chain_df(
            snapshot_id="n0", ts_utc=t0, market_date="2026-11-10", expiries=(("2026-12-18", 38),)
        ),
    )
    b1 = _bar(
        "n1",
        t1,
        "2026-11-11",
        _make_chain_df(
            snapshot_id="n1",
            ts_utc=t1,
            market_date="2026-11-11",
            spot=130.0,
            expiries=(("2026-12-18", 37),),
        ),
        spot=130.0,
    )
    cfg = BacktestConfig(
        strategy_kind="CUSTOM",
        tp_sl_basis="MAX_RISK_PCT",
        profit_target_pct=50.0,
        stop_loss_pct=50.0,
    )
    res = run_single_trade_replay([b0, b1], cfg, ticker="NVDA", entry_snapshot_id="n0", legs=naked)
    tr = res.trades[0]
    assert tr.max_loss_unbounded
    assert tr.max_loss_dollars is None
    assert tr.return_on_risk_pct is None
    assert tr.exit_reason == "END_OF_DATA"
    assert "MAX_RISK_UNBOUNDED_SKIP_TPSL" in tr.flags
    assert any("unbounded" in n.lower() for n in res.notes)


def test_edge_guards_empty_single_strike_no_usable_snapshot_unresolved_and_validation() -> None:
    cfg = BacktestConfig(strategy_kind="BULL_PUT_SPREAD", entry_cadence="WHEN_FLAT")
    # Empty bars
    empty_res = run_systematic_backtest([], cfg, ticker="NVDA")
    assert empty_res.summary.trade_count == 0
    assert empty_res.summary.final_equity == pytest.approx(cfg.initial_capital)
    assert empty_res.summary.win_rate_pct is None
    assert empty_res.summary.profit_factor is None
    assert empty_res.summary.expectancy_dollars is None

    # Bar 0: NO_USABLE_DAILY_SNAPSHOT empty frame -> skips cleanly without crash
    t0 = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
    t1 = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 20, 21, 15, tzinfo=UTC)
    empty_df = pl.DataFrame(schema=OPTION_CHAIN_POLARS_SCHEMA)
    b_unusable = _bar(
        "bad0",
        t0,
        "2026-11-10",
        empty_df,
        spot=100.0,
        spot_source="CLOSE",
        selection_flags=(NO_USABLE_DAILY_SNAPSHOT,),
    )
    # Bar 1: single strike only -> strategy builder raises ValueError -> caught & skipped
    b_single = _bar(
        "single1",
        t1,
        "2026-11-11",
        _make_chain_df(
            snapshot_id="single1",
            ts_utc=t1,
            market_date="2026-11-11",
            strikes=(100.0,),
            expiries=(("2026-12-18", 37),),
        ),
    )
    res_skip = run_systematic_backtest([b_unusable, b_single], cfg, ticker="NVDA")
    assert res_skip.summary.trade_count == 0
    assert res_skip.summary.ok_bars == 0
    assert res_skip.summary.flagged_bars == 2
    assert FLAG_NO_USABLE_DAILY_SNAPSHOT in res_skip.equity_curve[0].flags

    # Unresolved expiry (`spot_source="CLOSE"`) keeps UNRESOLVED & deducts open commission
    legs = [StrategyLegInput(side="SELL", right="P", expiry="2026-11-20", strike=95.0, qty=1)]
    b_open = _bar(
        "o0",
        t1,
        "2026-11-11",
        _make_chain_df(
            snapshot_id="o0", ts_utc=t1, market_date="2026-11-11", expiries=(("2026-11-20", 9),)
        ),
    )
    b_exp_close = _bar(
        "e1",
        t2,
        "2026-11-20",
        _make_chain_df(
            snapshot_id="e1", ts_utc=t2, market_date="2026-11-20", expiries=(("2026-12-18", 28),)
        ),
        spot=94.0,
        spot_source="CLOSE",
    )
    cfg_custom = BacktestConfig(strategy_kind="CUSTOM", commission_per_contract=1.0)
    res_unres = run_single_trade_replay(
        [b_open, b_exp_close], cfg_custom, ticker="NVDA", entry_snapshot_id="o0", legs=legs
    )
    tr_unres = res_unres.trades[0]
    assert tr_unres.status == "UNRESOLVED"
    assert tr_unres.net_pnl_dollars is None
    assert tr_unres.commissions_dollars == pytest.approx(1.0)
    assert res_unres.summary.final_equity == pytest.approx(cfg_custom.initial_capital - 1.0)

    # Validation errors: CUSTOM missing legs, bad snapshots, end < entry, bad ticker
    with pytest.raises(BacktestValidationError):
        run_systematic_backtest([b_open], BacktestConfig(strategy_kind="CUSTOM"), ticker="NVDA")
    with pytest.raises(BacktestValidationError):
        run_single_trade_replay([b_open], cfg, ticker="NVDA", entry_snapshot_id="missing")
    with pytest.raises(BacktestValidationError):
        run_single_trade_replay(
            [b_open, b_exp_close],
            cfg_custom,
            ticker="NVDA",
            entry_snapshot_id="e1",
            end_snapshot_id="o0",
            legs=legs,
        )
    with pytest.raises(InvalidTickerError):
        run_systematic_backtest([], cfg, ticker="BAD TICKER!")

    # LAST_CLOSE_ONLY end-to-end respects end_snapshot_id slice
    b_open2 = _bar(
        "o2",
        t1 + timedelta(days=1),
        "2026-11-12",
        _make_chain_df(
            snapshot_id="o2",
            ts_utc=t1 + timedelta(days=1),
            market_date="2026-11-12",
            expiries=(("2026-11-20", 8),),
        ),
    )
    res_slice = run_single_trade_replay(
        [b_open, b_open2, b_exp_close],
        BacktestConfig(
            strategy_kind="CUSTOM", price_basis="LAST_CLOSE_ONLY", close_price_mode="NATURAL_SPREAD"
        ),
        ticker="NVDA",
        entry_snapshot_id="o0",
        end_snapshot_id="o2",
        legs=legs,
    )
    assert [pt.snapshot_id for pt in res_slice.equity_curve] == ["o0", "o2"]
    assert res_slice.trades[0].exit_reason == "END_OF_DATA"
