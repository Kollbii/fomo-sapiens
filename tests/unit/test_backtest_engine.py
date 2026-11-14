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


def _low_vs_high_credit_bar(
    snapshot_id: str,
    market_date: str,
    *,
    short_mid: float,
    long_mid: float,
    short_last: float | None = None,
    long_last: float | None = None,
) -> DailyChainBar:
    ts = datetime.fromisoformat(f"{market_date}T20:00:00+00:00")
    s_last = short_last if short_last is not None else short_mid
    l_last = long_last if long_last is not None else long_mid
    ov: dict[tuple[str, float, OptionRight], dict[str, Any]] = {
        ("2026-12-18", 95.0, "P"): {
            "bid": round(short_mid - 0.05, 2),
            "ask": round(short_mid + 0.05, 2),
            "mid": short_mid,
            "last": s_last,
            "close": s_last,
            "open_interest": 10_000,
        },
        ("2026-12-18", 90.0, "P"): {
            "bid": round(long_mid - 0.05, 2),
            "ask": round(long_mid + 0.05, 2),
            "mid": long_mid,
            "last": l_last,
            "close": l_last,
            "open_interest": 500,
        },
        ("2026-12-18", 85.0, "P"): {
            "bid": round(long_mid - 0.05, 2),
            "ask": round(long_mid + 0.05, 2),
            "mid": long_mid,
            "last": l_last,
            "close": l_last,
            "open_interest": 100,
        },
    }
    return _bar(
        snapshot_id,
        ts,
        market_date,
        _make_chain_df(
            snapshot_id=snapshot_id,
            ts_utc=ts,
            market_date=market_date,
            strikes=(85.0, 90.0, 95.0, 105.0, 110.0),
            expiries=(("2026-12-18", -1),),
            overrides=ov,
        ),
    )


def test_entry_credit_ror_filters_per_spread_baseline_and_skip_diagnostics() -> None:
    assert BacktestConfig().min_entry_credit_dollars == pytest.approx(100.0)
    assert BacktestConfig().min_entry_ror_pct is None

    # All 3 days offer only $50 credit ($1.00 short vs $0.50 long on 5-wide -> 11.1% RoR).
    bars = [
        _low_vs_high_credit_bar("d0", "2026-11-02", short_mid=1.00, long_mid=0.50),
        _low_vs_high_credit_bar("d1", "2026-11-03", short_mid=1.00, long_mid=0.50),
        _low_vs_high_credit_bar("d2", "2026-11-04", short_mid=1.00, long_mid=0.50),
    ]

    # Default $100 per-1-spread floor skips every day, even when qty=3 ($150 total < $100/spread).
    res_def = run_systematic_backtest(
        bars, BacktestConfig(entry_cadence="WHEN_FLAT", qty=3), ticker="NVDA"
    )
    assert res_def.summary.trade_count == 0
    assert res_def.summary.skipped_entries_count == 3
    assert res_def.summary.skipped_by_reason == {"BELOW_MIN_CREDIT": 3}
    assert len(res_def.notes) == 1
    assert "BELOW_MIN_CREDIT=3" in res_def.notes[0]

    # 0.0 or None disables the credit cap; RoR filter still enforces 20% floor -> skips.
    res_ror = run_systematic_backtest(
        bars,
        BacktestConfig(
            entry_cadence="WHEN_FLAT", min_entry_credit_dollars=0.0, min_entry_ror_pct=20.0
        ),
        ticker="NVDA",
    )
    assert res_ror.summary.trade_count == 0
    assert res_ror.summary.skipped_by_reason == {"BELOW_MIN_ROR": 3}

    # Both disabled -> opens immediately on day 0 with $50 spread.
    res_off = run_systematic_backtest(
        bars,
        BacktestConfig(
            entry_cadence="WHEN_FLAT",
            min_entry_credit_dollars=None,
            min_entry_ror_pct=0.0,
            profit_target_pct=None,
            stop_loss_pct=None,
        ),
        ticker="NVDA",
    )
    assert res_off.summary.trade_count == 1
    assert res_off.summary.skipped_entries_count == 0
    assert res_off.trades[0].entry_cash_mid_dollars == pytest.approx(50.0)

    # LAST_CLOSE_ONLY basis agreement: mid=$120 passes, last=$60 fails -> skipped cleanly.
    bar_last_low = _low_vs_high_credit_bar(
        "l0", "2026-11-02", short_mid=2.20, long_mid=1.00, short_last=1.60, long_last=1.00
    )
    res_last = run_systematic_backtest(
        [bar_last_low],
        BacktestConfig(
            strategy_kind="BULL_PUT_SPREAD",
            entry_cadence="WHEN_FLAT",
            price_basis="LAST_CLOSE_ONLY",
            min_entry_credit_dollars=100.0,
        ),
        ticker="NVDA",
    )
    assert res_last.summary.trade_count == 0
    assert res_last.summary.skipped_by_reason == {"BELOW_MIN_CREDIT": 1}

    # SINGLE_TRADE explicit legs replay with $50 credit is NOT rejected by default $100 floor.
    explicit_legs = [
        StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=95.0, qty=1),
        StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=90.0, qty=1),
    ]
    res_replay = run_single_trade_replay(
        bars,
        BacktestConfig(strategy_kind="BULL_PUT_SPREAD"),
        ticker="NVDA",
        entry_snapshot_id="d0",
        legs=explicit_legs,
    )
    assert res_replay.summary.trade_count == 1
    assert res_replay.trades[0].entry_cash_mid_dollars == pytest.approx(50.0)


def test_next_trading_day_retry_across_weekly_every_n_days_and_when_flat() -> None:
    # Week 1: Mon Nov 2 ($50 -> skip), Tue Nov 3 ($120 -> fill), Wed Nov 4 ($130 -> skip week)
    # Week 2: Mon Nov 9 ($125 -> fill on target weekday!)
    bars = [
        _low_vs_high_credit_bar("mon1", "2026-11-02", short_mid=1.00, long_mid=0.50),
        _low_vs_high_credit_bar("tue1", "2026-11-03", short_mid=2.20, long_mid=1.00),
        _low_vs_high_credit_bar("wed1", "2026-11-04", short_mid=2.30, long_mid=1.00),
        _low_vs_high_credit_bar("thu1", "2026-11-05", short_mid=2.35, long_mid=1.00),
        _low_vs_high_credit_bar("mon2", "2026-11-09", short_mid=2.25, long_mid=1.00),
    ]

    # 1) WEEKLY_DAY (Monday=0): retries Tuesday after Monday skip, does not re-enter Wednesday
    cfg_weekly = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        entry_cadence="WEEKLY_DAY",
        entry_weekday=0,
        max_open_trades=5,
        min_entry_credit_dollars=100.0,
        profit_target_pct=None,
        stop_loss_pct=None,
    )
    res_weekly = run_systematic_backtest(bars, cfg_weekly, ticker="NVDA")
    assert [t.entry_date for t in res_weekly.trades] == ["2026-11-03", "2026-11-09"]
    assert res_weekly.summary.skipped_entries_count == 1
    assert res_weekly.summary.skipped_by_reason == {"BELOW_MIN_CREDIT": 1}

    # 2) EVERY_N_DAYS (N=2): Mon skipped -> Tue fills (bar_idx=1), Wed waits, Thu fills (bar_idx=3)
    cfg_every2 = cfg_weekly.model_copy(
        update={"entry_cadence": "EVERY_N_DAYS", "entry_every_n_days": 2}
    )
    res_every2 = run_systematic_backtest(bars, cfg_every2, ticker="NVDA")
    assert [t.entry_date for t in res_every2.trades] == ["2026-11-03", "2026-11-05"]
    assert res_every2.summary.skipped_entries_count == 1

    # 3) WHEN_FLAT: Mon skipped -> Tue fills
    cfg_flat = cfg_weekly.model_copy(update={"entry_cadence": "WHEN_FLAT", "max_open_trades": 1})
    res_flat = run_systematic_backtest(bars, cfg_flat, ticker="NVDA")
    assert [t.entry_date for t in res_flat.trades] == ["2026-11-03"]
    assert res_flat.summary.skipped_entries_count == 1


def test_open_only_run_and_single_bar_trade_summary_and_steps() -> None:
    # Single-bar run with an open BULL_PUT_SPREAD: 1 step, OPEN status, TP/SL targets populated,
    # unrealized P&L populated, realized P&L = 0.0, and closed-only ratios stay None.
    b0 = _low_vs_high_credit_bar("d0", "2026-11-02", short_mid=2.20, long_mid=1.00)
    cfg = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        entry_cadence="WHEN_FLAT",
        commission_per_contract=0.65,
        tp_sl_basis="CREDIT_DEBIT_PCT",
        profit_target_pct=50.0,
        stop_loss_pct=100.0,
    )
    res = run_systematic_backtest([b0], cfg, ticker="NVDA")
    assert res.summary.trade_count == 1
    assert res.summary.closed_trades == 0
    assert res.summary.open_trades == 1
    assert res.summary.winning_trades == 0
    assert res.summary.losing_trades == 0
    assert res.summary.win_rate_pct is None
    assert res.summary.profit_factor is None
    assert res.summary.expectancy_dollars is None
    assert res.summary.avg_win_dollars is None
    assert res.summary.avg_loss_dollars is None
    assert res.summary.realized_pnl_dollars == pytest.approx(0.0)
    assert res.summary.unrealized_pnl_dollars == pytest.approx(-1.30)
    assert res.summary.net_pnl_dollars == pytest.approx(-1.30)
    assert res.summary.final_equity == pytest.approx(cfg.initial_capital - 1.30)

    tr = res.trades[0]
    assert tr.status == "OPEN"
    assert tr.exit_reason == "END_OF_DATA"
    assert tr.days_held == 0
    assert len(tr.steps) == 1
    assert tr.profit_target_dollars == pytest.approx(60.0)
    assert tr.stop_loss_dollars == pytest.approx(-120.0)
    assert tr.net_pnl_dollars == pytest.approx(-1.30)

    step0 = tr.steps[0]
    assert step0.market_date == "2026-11-02"
    assert step0.dte_min_leg == 46
    assert step0.spot == pytest.approx(100.0)
    assert step0.quote_missing is False
    assert step0.mark_source == "MID"
    assert step0.status == "OK"
    assert step0.net_pnl_dollars == pytest.approx(-1.30)

    # Multi-bar open-only run where trade remains OPEN with positive unrealized MTM
    b1 = _low_vs_high_credit_bar("d1", "2026-11-03", short_mid=1.90, long_mid=1.00)
    res_open2 = run_systematic_backtest([b0, b1], cfg, ticker="NVDA")
    assert res_open2.summary.closed_trades == 0
    assert res_open2.summary.open_trades == 1
    assert res_open2.summary.win_rate_pct is None
    assert res_open2.summary.profit_factor is None
    assert res_open2.summary.expectancy_dollars is None
    assert res_open2.summary.realized_pnl_dollars == pytest.approx(0.0)
    # Gross MTM = +$120 entry - $90 close = +$30; net = 30 - 1.30 = +28.70
    assert res_open2.summary.unrealized_pnl_dollars == pytest.approx(28.70)
    assert res_open2.summary.net_pnl_dollars == pytest.approx(28.70)
    assert res_open2.trades[0].status == "OPEN"
    assert len(res_open2.trades[0].steps) == 2


def test_selection_notes_live_on_trade_not_sim_notes() -> None:
    d0 = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)
    d1 = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)
    exp = "2026-12-18"
    b0 = _bar(
        "s0",
        d0,
        "2026-11-06",
        _make_chain_df(
            snapshot_id="s0", ts_utc=d0, market_date="2026-11-06", spot=100.0, expiries=((exp, -1),)
        ),
    )
    b1 = _bar(
        "s1",
        d1,
        "2026-11-13",
        _make_chain_df(
            snapshot_id="s1", ts_utc=d1, market_date="2026-11-13", spot=101.0, expiries=((exp, -1),)
        ),
        spot=101.0,
    )
    cfg = BacktestConfig(
        strategy_kind="IRON_CONDOR",
        target_dte_min=30,
        target_dte_max=60,
        entry_cadence="WHEN_FLAT",
        profit_target_pct=None,
        stop_loss_pct=None,
    )
    res = run_systematic_backtest([b0, b1], cfg, ticker="NVDA")
    assert len(res.trades) == 1
    tr = res.trades[0]
    assert len(tr.legs) == 4
    assert len(tr.selection_notes) >= 2
    assert any("wall" in note.lower() or "delta" in note.lower() for note in tr.selection_notes)
    # Simulation-wide notes must stay free of per-trade wall/delta entry selection prose.
    assert res.notes == []
    joined_sim_notes = " ".join(res.notes).lower()
    assert "anchored to" not in joined_sim_notes
    assert "put wall" not in joined_sim_notes
    assert "call wall" not in joined_sim_notes
    assert "delta=" not in joined_sim_notes


def test_sparse_missing_quote_days_preserve_none_mtm_and_carry_equity() -> None:
    # Day 0: Enter 95P/90P bull put at $1.20 credit, $0 commission -> MTM = $0.00
    # Day 1: Missing quotes (null bid/ask/last/close) -> quote_missing=True, net_pnl_dollars=None
    # Day 2: Quotes recover (spread=$0.90) -> +$30.00 MTM (quote_missing=False)
    # Day 3: Empty NO_USABLE_DAILY_SNAPSHOT bar -> step3 quote_missing=True, carries +$30.00
    legs = [
        StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=95.0, qty=1),
        StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=90.0, qty=1),
    ]
    b0 = _low_vs_high_credit_bar("q0", "2026-11-02", short_mid=2.20, long_mid=1.00)

    t1 = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
    b1_missing_quotes = _bar(
        "q1",
        t1,
        "2026-11-03",
        _make_chain_df(
            snapshot_id="q1",
            ts_utc=t1,
            market_date="2026-11-03",
            expiries=(("2026-12-18", -1),),
            overrides={
                ("2026-12-18", 95.0, "P"): {
                    "bid": None,
                    "ask": None,
                    "mid": None,
                    "last": None,
                    "close": None,
                    "mark_price": None,
                },
            },
        ),
    )
    b2_profit = _low_vs_high_credit_bar("q2", "2026-11-04", short_mid=1.90, long_mid=1.00)

    t3 = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)
    b3_empty = _bar(
        "q3",
        t3,
        "2026-11-05",
        pl.DataFrame(schema=OPTION_CHAIN_POLARS_SCHEMA),
        spot=100.0,
        spot_source="CLOSE",
        selection_flags=(NO_USABLE_DAILY_SNAPSHOT,),
    )

    cfg = BacktestConfig(
        strategy_kind="CUSTOM",
        commission_per_contract=0.0,
        profit_target_pct=50.0,
        stop_loss_pct=100.0,
    )
    res = run_single_trade_replay(
        [b0, b1_missing_quotes, b2_profit, b3_empty],
        cfg,
        ticker="NVDA",
        entry_snapshot_id="q0",
        legs=legs,
    )
    tr = res.trades[0]
    assert tr.status == "OPEN"
    assert len(tr.steps) == 4
    assert tr.selection_notes == ["Evaluating 2 user-selected option leg(s)."]

    s0, s1, s2, s3 = tr.steps
    assert s0.quote_missing is False
    assert s0.net_pnl_dollars == pytest.approx(0.0)
    assert s0.mark_source == "MID"

    assert s1.quote_missing is True
    assert s1.net_pnl_dollars is None
    assert s1.gross_pnl_dollars is None
    assert s1.mark_source == "MISSING"
    assert s1.status == "INCOMPLETE"

    assert s2.quote_missing is False
    assert s2.net_pnl_dollars == pytest.approx(30.0)
    assert s2.mark_source == "MID"
    assert s2.status == "OK"

    assert s3.quote_missing is True
    assert s3.net_pnl_dollars is None
    assert s3.mark_source == "MISSING"
    assert s3.status == "INCOMPLETE"

    assert [pt.unrealized_pnl_dollars for pt in res.equity_curve] == pytest.approx(
        [0.0, 0.0, 30.0, 30.0]
    )
    assert res.summary.open_trades == 1
    assert res.summary.closed_trades == 0
    assert res.summary.unrealized_pnl_dollars == pytest.approx(30.0)
    assert res.summary.net_pnl_dollars == pytest.approx(30.0)
    assert any("missing leg quotes" in n.lower() for n in res.notes)
    assert any("no_usable_daily_snapshot" in n.lower() for n in res.notes)


def test_short_and_zero_dte_systematic_backtest_entry_and_same_day_settlement() -> None:
    # 1) Pure 0-DTE entry before 16:15 EST cutoff -> same-day settlement after cutoff ($0 close)
    t_open = datetime(2026, 11, 20, 19, 30, tzinfo=UTC)  # 14:30 EST
    t_settle = datetime(2026, 11, 20, 21, 20, tzinfo=UTC)  # 16:20 EST (past 16:15 EST cutoff)
    ov_0d: dict[tuple[str, float, OptionRight], dict[str, Any]] = {
        ("2026-11-20", 98.0, "P"): {"bid": 1.15, "ask": 1.25, "mid": 1.20, "open_interest": 8_000},
        ("2026-11-20", 95.0, "P"): {"bid": 0.45, "ask": 0.55, "mid": 0.50, "open_interest": 400},
    }
    b_open = _bar(
        "d0_open",
        t_open,
        "2026-11-20",
        _make_chain_df(
            snapshot_id="d0_open",
            ts_utc=t_open,
            market_date="2026-11-20",
            spot=100.0,
            strikes=(92.0, 95.0, 98.0, 100.0, 102.0, 105.0),
            expiries=(("2026-11-20", 0),),
            overrides=ov_0d,
        ),
        spot=100.0,
    )
    b_settle = _bar(
        "d0_settle",
        t_settle,
        "2026-11-20",
        _make_chain_df(
            snapshot_id="d0_settle",
            ts_utc=t_settle,
            market_date="2026-11-20",
            spot=99.0,
            strikes=(92.0, 95.0, 98.0, 100.0, 102.0, 105.0),
            expiries=(("2026-11-20", 0),),
        ),
        spot=99.0,
    )
    cfg_0d = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        target_dte_min=0,
        target_dte_max=0,
        entry_cadence="WHEN_FLAT",
        commission_per_contract=0.65,
        min_entry_credit_dollars=50.0,
        profit_target_pct=None,
        stop_loss_pct=None,
    )
    res_0d = run_systematic_backtest([b_open, b_settle], cfg_0d, ticker="NVDA")
    assert res_0d.summary.closed_trades == 1
    assert res_0d.summary.skipped_by_reason == {"ALREADY_SETTLED": 1}
    tr_0d = res_0d.trades[0]
    assert tr_0d.exit_reason == "EXPIRY_SETTLED"
    assert tr_0d.days_held == 0
    assert tr_0d.commissions_dollars == pytest.approx(1.30)  # open only, $0 closing commission
    assert tr_0d.gross_pnl_dollars == pytest.approx(70.0)
    assert tr_0d.net_pnl_dollars == pytest.approx(68.70)

    # 2) [0, 29] DTE skips settled 0d expiry on mixed bar and picks center/back for Diagonal
    b_mixed = _bar(
        "mix0",
        t_settle,
        "2026-11-20",
        _make_chain_df(
            snapshot_id="mix0",
            ts_utc=t_settle,
            market_date="2026-11-20",
            spot=100.0,
            strikes=(85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0),
            expiries=(("2026-11-20", 0), ("2026-12-04", 14), ("2026-12-11", 21)),
        ),
        spot=100.0,
    )
    cfg_short_diag = BacktestConfig(
        strategy_kind="DIAGONAL_SPREAD",
        target_dte_min=0,
        target_dte_max=29,
        entry_cadence="WHEN_FLAT",
        profit_target_pct=None,
        stop_loss_pct=None,
    )
    res_diag = run_systematic_backtest([b_mixed], cfg_short_diag, ticker="NVDA")
    assert res_diag.summary.trade_count == 1
    diag_leg_exps = {leg.expiry for leg in res_diag.trades[0].legs}
    assert diag_leg_exps == {"2026-12-04", "2026-12-11"}
