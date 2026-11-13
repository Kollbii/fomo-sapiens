"""Unit tests for pure backtest pricing, filtered BSM, tracked lookup, and settlement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest

from src.analytics.backtest import (
    FLAG_LATE_EXPIRY_SETTLEMENT,
    FLAG_LEG_FALLBACK_PRICE,
    FLAG_NO_LAST_PRINT,
    FLAG_TRACKED_LEG_USED,
    FLAG_UNRESOLVED_EXPIRY_LEG,
    FLAG_UNRESOLVED_EXPIRY_SPOT,
    BacktestConfig,
    enrich_filtered_greeks,
    evaluate_trade_step_on_bar,
    expiry_settlement_cutoff_utc,
    filter_df_for_dte_window,
    filter_df_for_legs,
    is_leg_settled,
    resolve_row_prices,
)
from src.analytics.backtest.rules import build_entry_candidate
from src.analytics.bsm_fallback import (
    DAYS_PER_YEAR,
    DEFAULT_RISK_FREE_RATE,
    BSMFallbackEngine,
    bsm_greeks,
    bsm_price,
)
from src.analytics.strategies.models import StrategyLegInput
from src.storage.backtest_store import DailyChainBar
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, OptionRight, SnapshotMeta, SpotSource


def _synthetic_chain(
    *,
    snapshot_id: str,
    ts_utc: datetime,
    market_date: str,
    spot: float = 100.0,
    strikes: tuple[float, ...] = (90.0, 95.0, 100.0, 105.0, 110.0),
    expiries: tuple[tuple[str, int], ...] = (("2026-12-18", 38), ("2027-01-15", 66)),
    greeks_source: str = "IBKR_MODEL",
    null_oi: bool = False,
    market_data_type: int = 3,
    overrides: dict[tuple[str, float, OptionRight], dict[str, Any]] | None = None,
) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    conid = 10_000
    ts_str = ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for expiry, dte in expiries:
        t_years = max(float(dte), 0.5) / DAYS_PER_YEAR
        for strike in strikes:
            for right in ("C", "P"):
                right_lit: OptionRight = "C" if right == "C" else "P"
                conid += 1
                s_arr = np.array([spot], dtype=np.float64)
                k_arr = np.array([strike], dtype=np.float64)
                t_arr = np.array([t_years], dtype=np.float64)
                iv_arr = np.array([0.35], dtype=np.float64)
                c_arr = np.array([right_lit == "C"], dtype=np.bool_)
                theo = float(
                    bsm_price(s_arr, k_arr, t_arr, DEFAULT_RISK_FREE_RATE, iv_arr, c_arr)[0]
                )
                d_arr, g_arr, th_arr, v_arr = bsm_greeks(
                    s_arr, k_arr, t_arr, DEFAULT_RISK_FREE_RATE, iv_arr, c_arr
                )
                bid = round(max(0.05, theo - 0.10), 2)
                ask = round(max(0.15, theo + 0.10), 2)
                mid = round((bid + ask) / 2.0, 2)
                row: dict[str, Any] = {
                    "snapshot_id": snapshot_id,
                    "snapshot_ts_utc": ts_str,
                    "market_date": market_date,
                    "market_data_type": market_data_type,
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
                    "volume": 100,
                    "open_interest": None if null_oi else 500,
                    "iv": None if greeks_source == "NONE" else 0.35,
                    "delta": None if greeks_source == "NONE" else float(d_arr[0]),
                    "gamma": None if greeks_source == "NONE" else float(g_arr[0]),
                    "theta": None if greeks_source == "NONE" else float(th_arr[0]),
                    "vega": None if greeks_source == "NONE" else float(v_arr[0]),
                    "greeks_source": greeks_source,
                }
                if overrides and (expiry, strike, right_lit) in overrides:
                    row.update(overrides[(expiry, strike, right_lit)])
                rows.append(row)
    return pl.DataFrame(rows, schema=OPTION_CHAIN_POLARS_SCHEMA)


def _make_bar(
    *,
    snapshot_id: str,
    ts_utc: datetime,
    df: pl.DataFrame,
    spot: float = 100.0,
    spot_source: SpotSource | None = "LAST",
    quote_coverage: float | None = 1.0,
    market_data_type: int = 3,
    tracked_df: pl.DataFrame | None = None,
    tracked_snapshot_ids: tuple[str, ...] = (),
) -> DailyChainBar:
    market_date = df["market_date"][0] if df.height > 0 else ts_utc.date().isoformat()
    expirations = sorted({str(x) for x in df["expiry"].to_list()}) if df.height > 0 else []
    meta = SnapshotMeta(
        snapshot_id=snapshot_id,
        file_path=f"{snapshot_id}.csv",
        snapshot_ts_utc=ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        market_date=str(market_date),
        market_data_type=market_data_type,
        underlying_price=spot,
        contract_count=df.height,
        expirations=expirations,
        spot_source=spot_source,
        quoted_contract_count=df.height,
        quote_coverage=quote_coverage,
        prior_close_only_count=0,
    )
    return DailyChainBar(
        snapshot_id=snapshot_id,
        ts_utc=ts_utc,
        market_date=str(market_date),
        df=df,
        meta=meta,
        tracked_df=tracked_df,
        tracked_snapshot_ids=tracked_snapshot_ids,
    )


def test_contract_filtered_bsm_enrichment_never_runs_whole_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ts = datetime(2026, 11, 10, 19, 0, tzinfo=UTC)
    df = _synthetic_chain(
        snapshot_id="nvda_1",
        ts_utc=ts,
        market_date="2026-11-10",
        strikes=(80.0, 85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0, 120.0),
        expiries=(("2026-11-20", 10), ("2026-12-18", 38), ("2027-02-19", 101)),
        greeks_source="NONE",
        null_oi=True,
    )
    assert df.height == 54

    call_heights: list[int] = []
    orig = BSMFallbackEngine.enrich_missing_greeks

    def _spy(
        sub_df: pl.DataFrame, *, risk_free_rate: float = DEFAULT_RISK_FREE_RATE
    ) -> pl.DataFrame:
        call_heights.append(sub_df.height)
        return orig(sub_df, risk_free_rate=risk_free_rate)

    monkeypatch.setattr(BSMFallbackEngine, "enrich_missing_greeks", _spy)

    legs = [
        StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=95.0, qty=1),
        StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=90.0, qty=1),
    ]
    sub = filter_df_for_legs(df, legs)
    enriched = enrich_filtered_greeks(sub, risk_free_rate=DEFAULT_RISK_FREE_RATE)
    assert call_heights == [2]
    assert enriched.height == 2
    assert enriched["greeks_source"].to_list() == ["BSM_FALLBACK", "BSM_FALLBACK"]

    # Window filtering on entry limits BSM calls strictly to [dte_min, dte_max] (18 rows vs 54).
    call_heights.clear()
    bar = _make_bar(snapshot_id="nvda_1", ts_utc=ts, df=df)
    cfg = BacktestConfig(strategy_kind="BULL_PUT_SPREAD", target_dte_min=30, target_dte_max=60)
    entry = build_entry_candidate(bar, cfg)
    assert entry is not None
    assert all(h <= 18 for h in call_heights)


def test_bsm_handles_dte_zero_and_mark_below_intrinsic_gracefully() -> None:
    ts = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    df = _synthetic_chain(
        snapshot_id="nvda_dte0",
        ts_utc=ts,
        market_date="2026-11-20",
        spot=110.0,
        strikes=(100.0,),
        expiries=(("2026-11-20", 0),),
        greeks_source="NONE",
        overrides={
            ("2026-11-20", 100.0, "C"): {
                "bid": 8.0,
                "ask": 8.2,
                "mid": 8.1,
                "last": 8.1,
                "mark_price": 8.1,  # Strictly below $10 intrinsic
            }
        },
    )
    leg = StrategyLegInput(side="BUY", right="C", expiry="2026-11-20", strike=100.0, qty=1)
    enriched = enrich_filtered_greeks(filter_df_for_legs(df, [leg]), risk_free_rate=0.0425)
    assert enriched.height == 1
    assert enriched["iv"][0] is None


def test_price_basis_modes_and_fallback_flags() -> None:
    # AUTO_MID_LAST_CLOSE cascade: MID -> LAST -> CLOSE -> NO_PRICE
    res_mid = resolve_row_prices(
        {"bid": 2.0, "ask": 2.4, "last": 2.1, "close": 1.9},
        side="SELL",
        price_basis="AUTO_MID_LAST_CLOSE",
        close_price_mode="MID",
    )
    assert res_mid.price_used == pytest.approx(2.2)
    assert res_mid.price_source == "MID"
    assert not res_mid.used_fallback

    res_last_fb = resolve_row_prices(
        {"bid": None, "ask": 2.4, "last": 2.15, "close": 1.9},
        side="SELL",
        price_basis="AUTO_MID_LAST_CLOSE",
        close_price_mode="MID",
    )
    assert res_last_fb.price_used == pytest.approx(2.15)
    assert res_last_fb.price_source == "LAST"
    assert res_last_fb.used_fallback

    res_close_fb = resolve_row_prices(
        {"bid": None, "ask": None, "last": None, "close": 1.9},
        side="SELL",
        price_basis="AUTO_MID_LAST_CLOSE",
        close_price_mode="MID",
    )
    assert res_close_fb.price_used == pytest.approx(1.9)
    assert res_close_fb.price_source == "CLOSE"
    assert res_close_fb.used_fallback

    # LAST_CLOSE_ONLY must ignore bid/ask even when present
    res_strict_last = resolve_row_prices(
        {"bid": 2.0, "ask": 2.4, "last": 2.35, "close": 1.9},
        side="BUY",
        price_basis="LAST_CLOSE_ONLY",
        close_price_mode="MID",
    )
    assert res_strict_last.price_used == pytest.approx(2.35)
    assert res_strict_last.price_source == "LAST"
    assert not res_strict_last.used_fallback

    res_strict_close = resolve_row_prices(
        {"bid": 2.0, "ask": 2.4, "last": None, "close": 1.95},
        side="BUY",
        price_basis="LAST_CLOSE_ONLY",
        close_price_mode="MID",
    )
    assert res_strict_close.price_used == pytest.approx(1.95)
    assert res_strict_close.price_source == "CLOSE"
    assert res_strict_close.used_fallback

    res_no_print = resolve_row_prices(
        {"bid": 2.0, "ask": 2.4, "last": None, "close": None},
        side="BUY",
        price_basis="LAST_CLOSE_ONLY",
        close_price_mode="MID",
    )
    assert res_no_print.price_used is None
    assert res_no_print.price_source == "NO_PRICE"
    assert res_no_print.no_last_print


def test_close_price_mode_natural_spread_vs_mid() -> None:
    ts = datetime(2026, 11, 10, 19, 0, tzinfo=UTC)
    df = _synthetic_chain(
        snapshot_id="nvda_nat",
        ts_utc=ts,
        market_date="2026-11-10",
        strikes=(90.0, 95.0),
        expiries=(("2026-12-18", 38),),
        overrides={
            ("2026-12-18", 95.0, "P"): {"bid": 2.0, "ask": 2.4, "mid": 2.2, "last": 2.2},
            ("2026-12-18", 90.0, "P"): {"bid": 1.0, "ask": 1.4, "mid": 1.2, "last": 1.2},
        },
    )
    bar = _make_bar(snapshot_id="nvda_nat", ts_utc=ts, df=df)
    legs = [
        StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=95.0, qty=1),
        StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=90.0, qty=1),
    ]
    step_mid = evaluate_trade_step_on_bar(bar, legs, BacktestConfig(close_price_mode="MID"))
    step_nat = evaluate_trade_step_on_bar(
        bar, legs, BacktestConfig(close_price_mode="NATURAL_SPREAD")
    )
    # MID close cash: -2.2*100 + 1.2*100 = -100.0
    assert step_mid.active_close_value_dollars == pytest.approx(-100.0)
    # NATURAL_SPREAD close: buy short 95P at ask 2.4 (-240), sell long 90P at bid 1.0 (+100) = -140
    assert step_nat.active_close_value_dollars == pytest.approx(-140.0)
    assert step_nat.natural_close_value_dollars == pytest.approx(-140.0)

    # LAST_CLOSE_ONLY with missing last falls back to close -> FLAG_LEG_FALLBACK_PRICE
    df_fb = _synthetic_chain(
        snapshot_id="nvda_fb",
        ts_utc=ts,
        market_date="2026-11-10",
        strikes=(95.0,),
        expiries=(("2026-12-18", 38),),
        overrides={("2026-12-18", 95.0, "P"): {"last": None, "close": 1.95}},
    )
    step_fb = evaluate_trade_step_on_bar(
        _make_bar(snapshot_id="nvda_fb", ts_utc=ts, df=df_fb),
        [StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=95.0, qty=1)],
        BacktestConfig(price_basis="LAST_CLOSE_ONLY"),
    )
    assert FLAG_LEG_FALLBACK_PRICE in step_fb.flags
    assert step_fb.status == "UNRELIABLE"


def test_tracked_leg_fallback_when_contract_below_30_dte() -> None:
    ts = datetime(2026, 12, 1, 19, 0, tzinfo=UTC)
    # Chain only carries >= 30 DTE expiry; tracked_df holds the sub-30 DTE open position legs
    chain_df = _synthetic_chain(
        snapshot_id="nvda_dec1",
        ts_utc=ts,
        market_date="2026-12-01",
        strikes=(90.0, 95.0),
        expiries=(("2027-01-15", 45),),
    )
    tracked_df = _synthetic_chain(
        snapshot_id="trk_dec1",
        ts_utc=ts,
        market_date="2026-12-01",
        strikes=(90.0, 95.0),
        expiries=(("2026-12-18", 17),),
        overrides={
            ("2026-12-18", 95.0, "P"): {"bid": 1.4, "ask": 1.6, "mid": 1.5, "last": 1.5},
            ("2026-12-18", 90.0, "P"): {"bid": 0.4, "ask": 0.6, "mid": 0.5, "last": 0.5},
        },
    )
    bar = _make_bar(
        snapshot_id="nvda_dec1",
        ts_utc=ts,
        df=chain_df,
        tracked_df=tracked_df,
        tracked_snapshot_ids=("trk_dec1",),
    )
    legs = [
        StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=95.0, qty=1),
        StrategyLegInput(side="BUY", right="P", expiry="2026-12-18", strike=90.0, qty=1),
    ]
    step = evaluate_trade_step_on_bar(bar, legs, BacktestConfig())
    assert step.status == "OK"
    assert step.active_close_value_dollars == pytest.approx(-100.0)
    assert all(m.quote_source == "TRACKED" for m in step.legs)
    assert FLAG_TRACKED_LEG_USED in step.flags
    assert step.tracked_snapshot_ids == ["trk_dec1"]


def test_expiry_settlement_cutoffs_regular_early_close_and_unresolved() -> None:
    # Regular session: 2026-11-20 closes 16:00 EST (21:00 UTC) -> +15m = 21:15 UTC
    cutoff_reg = expiry_settlement_cutoff_utc("2026-11-20")
    assert cutoff_reg == datetime(2026, 11, 20, 21, 15, tzinfo=UTC)
    assert not is_leg_settled("2026-11-20", cutoff_reg - timedelta(minutes=1))
    assert is_leg_settled("2026-11-20", cutoff_reg)

    # NYSE early close 2026-11-27: 13:00 EST (18:00 UTC) -> +15m = 18:15 UTC
    cutoff_early = expiry_settlement_cutoff_utc("2026-11-27")
    assert cutoff_early == datetime(2026, 11, 27, 18, 15, tzinfo=UTC)
    assert not is_leg_settled("2026-11-27", datetime(2026, 11, 27, 18, 10, tzinfo=UTC))
    assert is_leg_settled("2026-11-27", datetime(2026, 11, 27, 18, 15, tzinfo=UTC))

    # Settle ITM/OTM bull put spread at spot=92.0 on 2026-11-27 18:20 UTC
    legs = [
        StrategyLegInput(side="SELL", right="P", expiry="2026-11-27", strike=95.0, qty=1),
        StrategyLegInput(side="BUY", right="P", expiry="2026-11-27", strike=90.0, qty=1),
    ]
    df_settle = _synthetic_chain(
        snapshot_id="nvda_settle",
        ts_utc=datetime(2026, 11, 27, 18, 20, tzinfo=UTC),
        market_date="2026-11-27",
        spot=92.0,
        strikes=(90.0, 95.0),
        expiries=(("2026-12-26", 29),),
    )
    bar_ok = _make_bar(
        snapshot_id="nvda_settle",
        ts_utc=datetime(2026, 11, 27, 18, 20, tzinfo=UTC),
        df=df_settle,
        spot=92.0,
        spot_source="LAST",
    )
    step_ok = evaluate_trade_step_on_bar(bar_ok, legs, BacktestConfig())
    assert step_ok.all_legs_settled
    assert step_ok.status == "OK"
    # Short 95P settles to $3 intrinsic (-$300), long 90P settles to $0 -> -$300
    assert step_ok.active_close_value_dollars == pytest.approx(-300.0)
    assert all(
        m.quote_source == "SETTLEMENT" and m.price_source == "INTRINSIC" for m in step_ok.legs
    )

    # Missing usable spot (`spot_source="CLOSE"`) blocks settlement -> UNRESOLVED / INCOMPLETE
    bar_close_spot = _make_bar(
        snapshot_id="nvda_bad_spot",
        ts_utc=datetime(2026, 11, 27, 18, 20, tzinfo=UTC),
        df=df_settle,
        spot=92.0,
        spot_source="CLOSE",
    )
    step_bad = evaluate_trade_step_on_bar(bar_close_spot, legs, BacktestConfig())
    assert step_bad.settlement_blocked
    assert step_bad.status == "INCOMPLETE"
    assert FLAG_UNRESOLVED_EXPIRY_SPOT in step_bad.flags

    # Late expiry settlement on bar after expiry emits FLAG_LATE_EXPIRY_SETTLEMENT
    bar_late = _make_bar(
        snapshot_id="nvda_late",
        ts_utc=datetime(2026, 11, 30, 16, 0, tzinfo=UTC),
        df=df_settle.with_columns(pl.lit("2026-11-30").alias("market_date")),
        spot=98.0,
        spot_source="MID",
    )
    step_late = evaluate_trade_step_on_bar(bar_late, legs, BacktestConfig())
    assert step_late.all_legs_settled
    assert FLAG_LATE_EXPIRY_SETTLEMENT in step_late.flags
    assert step_late.status == "UNRELIABLE"

    # Mixed diagonal expiry where back leg is missing -> UNRESOLVED_EXPIRY_LEG
    diag_legs = [
        StrategyLegInput(side="SELL", right="C", expiry="2026-11-27", strike=105.0, qty=1),
        StrategyLegInput(side="BUY", right="C", expiry="2027-01-15", strike=105.0, qty=1),
    ]
    step_diag_missing = evaluate_trade_step_on_bar(bar_ok, diag_legs, BacktestConfig())
    assert step_diag_missing.settlement_blocked
    assert FLAG_UNRESOLVED_EXPIRY_LEG in step_diag_missing.flags


def test_last_close_only_flags_no_last_print_when_last_and_close_absent() -> None:
    ts = datetime(2026, 11, 10, 19, 0, tzinfo=UTC)
    df = _synthetic_chain(
        snapshot_id="nvda_nl",
        ts_utc=ts,
        market_date="2026-11-10",
        strikes=(90.0, 95.0),
        expiries=(("2026-12-18", 38),),
        overrides={("2026-12-18", 95.0, "P"): {"last": None, "close": None}},
    )
    bar = _make_bar(snapshot_id="nvda_nl", ts_utc=ts, df=df)
    legs = [StrategyLegInput(side="SELL", right="P", expiry="2026-12-18", strike=95.0, qty=1)]
    step = evaluate_trade_step_on_bar(bar, legs, BacktestConfig(price_basis="LAST_CLOSE_ONLY"))
    assert step.status == "INCOMPLETE"
    assert FLAG_NO_LAST_PRINT in step.flags
    assert step.active_close_value_dollars is None
    assert filter_df_for_dte_window(df, 40, 50).height == 0
