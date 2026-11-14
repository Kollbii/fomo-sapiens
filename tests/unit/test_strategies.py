from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable
from typing import Any, get_args

import numpy as np
import polars as pl
import pytest
from pydantic import ValidationError

from src.analytics.bsm_fallback import DAYS_PER_YEAR, DEFAULT_RISK_FREE_RATE, bsm_greeks, bsm_price
from src.analytics.strategies import (
    PRESET_STRATEGIES,
    PresetStrategyKind,
    StrategyCandidateFilters,
    StrategyFilterError,
    StrategyKind,
    StrategyLegInput,
    StrategyPricing,
    StrategyRepickResult,
    StrategyRiskMetrics,
    candidate_passes_filters,
    compute_expected_return_on_risk_pct,
    compute_nearest_breakeven_distance_pct,
    evaluate_strategy,
    repick_strategy_legs,
    repick_window_strikes,
    suggest_strategy_legs_from_walls,
    try_suggest_strategy_legs_from_walls,
)
from src.analytics.walls import compute_walls
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, OptionContractRow, OptionRight


def _build_synthetic_chain(
    *,
    spot: float = 100.0,
    strikes: list[float] | None = None,
    expiries: list[tuple[str, int]] | None = None,
    put_wall: float = 90.0,
    call_wall: float = 110.0,
    iv: float = 0.35,
    omit_bid_ask: bool = False,
) -> pl.DataFrame:
    strike_list = strikes or [80.0, 85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0, 120.0]
    exp_list = expiries or [("2026-12-19", 38), ("2027-01-16", 66)]
    rows: list[dict[str, Any]] = []
    conid = 500_000

    for expiry, dte in exp_list:
        t_years = max(float(dte), 0.5) / DAYS_PER_YEAR
        for strike in strike_list:
            for right in ("C", "P"):
                conid += 1
                is_call = right == "C"
                s_arr = np.array([spot], dtype=np.float64)
                k_arr = np.array([strike], dtype=np.float64)
                t_arr = np.array([t_years], dtype=np.float64)
                iv_arr = np.array([iv], dtype=np.float64)
                c_arr = np.array([is_call], dtype=np.bool_)

                theo = float(
                    bsm_price(s_arr, k_arr, t_arr, DEFAULT_RISK_FREE_RATE, iv_arr, c_arr)[0]
                )
                d_arr, g_arr, th_arr, v_arr = bsm_greeks(
                    s_arr, k_arr, t_arr, DEFAULT_RISK_FREE_RATE, iv_arr, c_arr
                )
                spread_half = max(0.05, round(theo * 0.03, 2))
                bid = None if omit_bid_ask else round(max(0.05, theo - spread_half), 2)
                ask = None if omit_bid_ask else round(max(0.10, theo + spread_half), 2)
                mid = (
                    round(theo, 2) if omit_bid_ask else round(((bid or 0.0) + (ask or 0.0)) / 2, 2)
                )

                oi = 400
                vol = 120
                if right == "P" and math.isclose(strike, put_wall):
                    oi = 12_000
                    vol = 3_400
                elif right == "C" and math.isclose(strike, call_wall):
                    oi = 14_000
                    vol = 4_100
                elif abs(strike - spot) <= 5.0:
                    oi = 2_800
                    vol = 950

                rows.append(
                    OptionContractRow(
                        snapshot_id="NVDA_20261111_153000",
                        snapshot_ts_utc="2026-11-11T15:30:00Z",
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
                        bid=bid,
                        ask=ask,
                        mid=mid,
                        last=mid,
                        close=mid,
                        mark_price=mid,
                        volume=vol,
                        open_interest=oi,
                        iv=iv,
                        delta=float(d_arr[0]),
                        gamma=float(g_arr[0]),
                        theta=float(th_arr[0]),
                        vega=float(v_arr[0]),
                        greeks_source="BSM_FALLBACK",
                    ).model_dump()
                )
    return pl.DataFrame(rows, schema=OPTION_CHAIN_POLARS_SCHEMA)


def test_bull_put_spread_anchors_to_put_wall_and_bounds_risk() -> None:
    df = _build_synthetic_chain(put_wall=90.0, call_wall=110.0)
    ev = evaluate_strategy(df, strategy_kind="BULL_PUT_SPREAD", target_expiry="2026-12-19")

    assert ev.strategy_kind == "BULL_PUT_SPREAD"
    assert ev.short_expiry == "2026-12-19"
    assert ev.primary_put_wall == pytest.approx(90.0)
    assert len(ev.legs) == 2

    short_leg = next(leg for leg in ev.legs if leg.side == "SELL")
    long_leg = next(leg for leg in ev.legs if leg.side == "BUY")
    assert short_leg.right == "P"
    assert long_leg.right == "P"
    assert short_leg.strike == pytest.approx(90.0)
    assert long_leg.strike < short_leg.strike

    assert ev.pricing.is_credit is True
    assert ev.pricing.mid_cash_total > 0.0
    assert ev.pricing.natural_cash_total < ev.pricing.mid_cash_total
    assert ev.pricing.slippage_dollars > 0.0
    assert len(ev.pricing.limit_ladder) >= 2

    spread_width = (short_leg.strike - long_leg.strike) * 100.0
    assert ev.risk.max_loss_unbounded is False
    assert ev.risk.max_profit == pytest.approx(ev.pricing.mid_cash_total, abs=15.0)
    assert ev.risk.max_loss == pytest.approx(spread_width - ev.pricing.mid_cash_total, abs=15.0)
    assert ev.risk.return_on_risk_pct is not None and ev.risk.return_on_risk_pct > 0.0
    assert len(ev.risk.breakevens) == 1
    assert long_leg.strike < ev.risk.breakevens[0] < short_leg.strike
    assert ev.risk.prob_of_profit_pct is not None and 50.0 < ev.risk.prob_of_profit_pct < 99.0
    assert ev.risk.expected_move_1sigma_low is not None
    assert ev.risk.expected_move_1sigma_high is not None
    assert ev.risk.expected_move_1sigma_low < 100.0 < ev.risk.expected_move_1sigma_high
    assert ev.net_greeks.delta_shares > 0.0
    assert ev.net_greeks.theta_daily_dollars > 0.0
    assert len(ev.curves) == 3  # Today, Midpoint, At Expiration


def test_bull_call_spread_anchors_short_cap_to_call_wall() -> None:
    df = _build_synthetic_chain(put_wall=90.0, call_wall=110.0)
    ev = evaluate_strategy(df, strategy_kind="BULL_CALL_SPREAD", target_expiry="2026-12-19")

    assert ev.strategy_kind == "BULL_CALL_SPREAD"
    assert len(ev.legs) == 2
    long_leg = next(leg for leg in ev.legs if leg.side == "BUY")
    short_leg = next(leg for leg in ev.legs if leg.side == "SELL")
    assert long_leg.right == "C" and short_leg.right == "C"
    assert long_leg.strike < short_leg.strike
    assert short_leg.strike == pytest.approx(110.0)

    assert ev.pricing.is_credit is False
    assert ev.pricing.mid_cash_total < 0.0
    assert ev.risk.max_loss_unbounded is False
    assert ev.risk.max_loss == pytest.approx(abs(ev.pricing.mid_cash_total), abs=15.0)
    assert ev.net_greeks.delta_shares > 0.0
    assert len(ev.risk.breakevens) == 1
    assert long_leg.strike < ev.risk.breakevens[0] < short_leg.strike


def test_diagonal_spread_uses_multi_dte_calendar_structure() -> None:
    df = _build_synthetic_chain(put_wall=90.0, call_wall=110.0)
    ev = evaluate_strategy(df, strategy_kind="DIAGONAL_SPREAD", target_expiry="2026-12-19")

    assert ev.strategy_kind == "DIAGONAL_SPREAD"
    assert ev.short_expiry == "2026-12-19"
    assert ev.long_expiry == "2027-01-16"
    assert ev.long_dte > ev.short_dte

    long_leg = next(leg for leg in ev.legs if leg.side == "BUY")
    short_leg = next(leg for leg in ev.legs if leg.side == "SELL")
    assert long_leg.expiry == "2027-01-16"
    assert short_leg.expiry == "2026-12-19"
    assert long_leg.strike < 100.0
    assert short_leg.strike > long_leg.strike
    assert "At Short Expiry (38d)" in [c.label for c in ev.curves]


def test_iron_condor_builds_four_ordered_wings_and_two_breakevens() -> None:
    df = _build_synthetic_chain(put_wall=90.0, call_wall=110.0)
    ev = evaluate_strategy(df, strategy_kind="IRON_CONDOR", target_expiry="2026-12-19")

    assert ev.strategy_kind == "IRON_CONDOR"
    assert len(ev.legs) == 4
    puts = sorted((leg for leg in ev.legs if leg.right == "P"), key=lambda x: x.strike)
    calls = sorted((leg for leg in ev.legs if leg.right == "C"), key=lambda x: x.strike)
    assert len(puts) == 2 and len(calls) == 2
    assert puts[0].side == "BUY" and puts[1].side == "SELL"
    assert calls[0].side == "SELL" and calls[1].side == "BUY"
    assert puts[1].strike == pytest.approx(90.0)
    assert calls[0].strike == pytest.approx(110.0)
    assert puts[0].strike < puts[1].strike < calls[0].strike < calls[1].strike
    assert ev.pricing.is_credit is True
    assert len(ev.risk.breakevens) == 2
    assert puts[0].strike < ev.risk.breakevens[0] < puts[1].strike
    assert calls[0].strike < ev.risk.breakevens[1] < calls[1].strike


def test_custom_legs_simulation_slider_and_qty_multiplier() -> None:
    df = _build_synthetic_chain()
    legs = [
        StrategyLegInput(side="SELL", right="P", expiry="2026-12-19", strike=95.0, qty=1),
        StrategyLegInput(side="BUY", right="P", expiry="2026-12-19", strike=90.0, qty=1),
    ]
    ev_1x = evaluate_strategy(df, strategy_kind="CUSTOM", legs=legs, qty_multiplier=1)
    ev_3x = evaluate_strategy(
        df,
        strategy_kind="CUSTOM",
        legs=legs,
        qty_multiplier=3,
        days_forward=15,
        iv_shift=-0.05,
    )

    assert ev_3x.pricing.mid_cash_total == pytest.approx(
        ev_1x.pricing.mid_cash_total * 3.0, abs=0.1
    )
    assert ev_3x.net_greeks.theta_daily_dollars == pytest.approx(
        ev_1x.net_greeks.theta_daily_dollars * 3.0, abs=0.1
    )
    assert ev_3x.days_forward == 15
    assert ev_3x.iv_shift == pytest.approx(-0.05)
    sim_curves = [c for c in ev_3x.curves if c.label.startswith("Simulated")]
    assert len(sim_curves) == 1
    # Lower IV + forward time decay on a credit put spread increases simulated P&L near ATM
    mid_idx = min(range(len(ev_3x.price_grid)), key=lambda i: abs(ev_3x.price_grid[i] - 100.0))
    today_pnl = ev_3x.curves[0].pnl_values[mid_idx]
    sim_pnl = sim_curves[0].pnl_values[mid_idx]
    assert sim_pnl > today_pnl


def test_naked_short_call_marks_unbounded_max_loss() -> None:
    df = _build_synthetic_chain()
    ev = evaluate_strategy(
        df,
        strategy_kind="CUSTOM",
        legs=[StrategyLegInput(side="SELL", right="C", expiry="2026-12-19", strike=105.0, qty=1)],
    )
    assert ev.risk.max_loss_unbounded is True
    assert ev.risk.return_on_risk_pct is None


def test_missing_bid_ask_quotes_fall_back_to_mid_and_bsm_iv() -> None:
    df = _build_synthetic_chain(omit_bid_ask=True)
    # Strip BSM greeks to ensure fallback BSM valuation still works smoothly
    df = df.with_columns(
        pl.lit(None).cast(pl.Float64).alias("iv"),
        pl.lit(None).cast(pl.Float64).alias("delta"),
        pl.lit(None).cast(pl.Float64).alias("gamma"),
        pl.lit(None).cast(pl.Float64).alias("theta"),
        pl.lit(None).cast(pl.Float64).alias("vega"),
        pl.lit("NONE").alias("greeks_source"),
    )
    ev = evaluate_strategy(df, strategy_kind="BULL_PUT_SPREAD", target_expiry="2026-12-19")
    assert all(leg.natural_fill == pytest.approx(leg.mid, abs=1e-4) for leg in ev.legs)
    assert ev.pricing.slippage_dollars == pytest.approx(0.0)
    assert all(leg.iv is not None and leg.delta is not None for leg in ev.legs)


def test_strategy_validation_errors() -> None:
    df = _build_synthetic_chain()
    with pytest.raises(ValueError, match="empty"):
        evaluate_strategy(df.head(0), strategy_kind="BULL_PUT_SPREAD")

    with pytest.raises(ValueError, match="explicit legs"):
        evaluate_strategy(df, strategy_kind="CUSTOM", legs=None)

    with pytest.raises(ValueError, match="not found"):
        evaluate_strategy(
            df,
            strategy_kind="CUSTOM",
            legs=[
                StrategyLegInput(side="BUY", right="C", expiry="2026-12-19", strike=999.0, qty=1)
            ],
        )

    legs, notes, cw, pw = suggest_strategy_legs_from_walls(df, "BULL_PUT_SPREAD")
    assert len(legs) == 2 and notes and cw == pytest.approx(110.0) and pw == pytest.approx(90.0)


def _custom_put_chain(
    put_quotes: list[tuple[float, float, float, float, int]],
    *,
    spot: float = 100.0,
    expiry: str = "2026-12-19",
    dte: int = 38,
) -> pl.DataFrame:
    """Build synthetic chain with explicit `(strike, bid, ask, last, oi)` put quotes."""
    return _custom_option_chain(put_quotes, right="P", spot=spot, expiry=expiry, dte=dte)


def _custom_call_chain(
    call_quotes: list[tuple[float, float, float, float, int]],
    *,
    spot: float = 100.0,
    expiry: str = "2026-12-19",
    dte: int = 38,
) -> pl.DataFrame:
    """Build synthetic chain with explicit `(strike, bid, ask, last, oi)` call quotes."""
    return _custom_option_chain(call_quotes, right="C", spot=spot, expiry=expiry, dte=dte)


def _custom_option_chain(
    quotes: list[tuple[float, float, float, float, int]],
    *,
    right: OptionRight,
    spot: float,
    expiry: str,
    dte: int,
) -> pl.DataFrame:
    """One-right chain; OTM |Delta| 0.25, ITM/ATM |Delta| 0.60 (signed per right)."""
    rows: list[dict[str, Any]] = []
    conid = 700_000
    for strike, bid, ask, last, oi in quotes:
        conid += 1
        mid = round((bid + ask) / 2.0, 2)
        if right == "P":
            delta = -0.25 if strike < spot else -0.60
        else:
            delta = 0.25 if strike > spot else 0.60
        rows.append(
            OptionContractRow(
                snapshot_id="NVDA_20261111_153000",
                snapshot_ts_utc="2026-11-11T15:30:00Z",
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
                bid=bid,
                ask=ask,
                mid=mid,
                last=last,
                close=last,
                mark_price=mid,
                volume=100,
                open_interest=oi,
                iv=0.35,
                delta=delta,
                gamma=0.02,
                theta=-0.04,
                vega=0.10,
                greeks_source="IBKR_MODEL",
            ).model_dump()
        )
    return pl.DataFrame(rows, schema=OPTION_CHAIN_POLARS_SCHEMA)


def test_bull_put_spread_entry_filters_evaluate_all_wings_beyond_top4() -> None:
    # Put wall at 90P ($1.40 mid). First 4 lower wings (87.5, 85, 82.5, 80) yield <$100 credit;
    # 5th lower wing 75P ($0.25 mid) clears $115 >= $100 without truncating to ordered[:4].
    df = _custom_put_chain(
        [
            (70.0, 0.08, 0.12, 0.10, 200),
            (75.0, 0.23, 0.27, 0.25, 200),
            (80.0, 0.43, 0.47, 0.45, 200),
            (82.5, 0.53, 0.57, 0.55, 200),
            (85.0, 0.73, 0.77, 0.75, 200),
            (87.5, 0.93, 0.97, 0.95, 200),
            (90.0, 1.38, 1.42, 1.40, 12_000),
            (95.0, 2.48, 2.52, 2.50, 400),
        ],
        spot=100.0,
    )
    # Without caps (or 0.0), preserves narrow wall-anchored wing (90P / 87.5P -> $45)
    legs_uncapped, _, _, _ = suggest_strategy_legs_from_walls(
        df, "BULL_PUT_SPREAD", min_net_credit_dollars=0.0, min_ror_pct=0.0
    )
    assert [leg.strike for leg in legs_uncapped] == [90.0, 87.5]

    # With $100 floor, inspects beyond ordered[:4] and finds 90P / 75P ($115)
    ev = evaluate_strategy(df, strategy_kind="BULL_PUT_SPREAD", min_net_credit_dollars=100.0)
    short_leg = next(leg for leg in ev.legs if leg.side == "SELL")
    long_leg = next(leg for leg in ev.legs if leg.side == "BUY")
    assert short_leg.strike == pytest.approx(90.0)
    assert long_leg.strike == pytest.approx(75.0)
    assert ev.pricing.mid_cash_total == pytest.approx(115.0)


def test_bull_put_spread_fallback_otm_ror_filter_and_structured_rejections() -> None:
    # Put wall 85P ($0.60) cannot reach $100 credit; 95P ($2.10) / 90P ($1.00) yields $110 (28.2%);
    # 95P / 85P yields $150 & 17.6% RoR (<20% RoR); ITM 110P ($11.00) must never be picked.
    df = _custom_put_chain(
        [
            (85.0, 0.58, 0.62, 0.60, 15_000),
            (90.0, 0.98, 1.02, 1.00, 600),
            (95.0, 2.08, 2.12, 2.10, 800),
            (110.0, 10.90, 11.10, 11.00, 300),
        ],
        spot=100.0,
    )
    ev = evaluate_strategy(
        df,
        strategy_kind="BULL_PUT_SPREAD",
        min_net_credit_dollars=100.0,
        min_ror_pct=20.0,
    )
    short_leg = next(leg for leg in ev.legs if leg.side == "SELL")
    long_leg = next(leg for leg in ev.legs if leg.side == "BUY")
    assert short_leg.strike == pytest.approx(95.0)
    assert long_leg.strike == pytest.approx(90.0)
    assert ev.pricing.mid_cash_total == pytest.approx(110.0)
    assert (ev.risk.return_on_risk_pct or 0.0) >= 20.0

    # Impossible threshold -> try_suggest returns legs=None with structured skip_reason
    outcome = try_suggest_strategy_legs_from_walls(
        df, "BULL_PUT_SPREAD", min_net_credit_dollars=300.0, min_ror_pct=20.0
    )
    assert outcome.legs is None
    assert outcome.skip_reason == "BELOW_MIN_CREDIT"

    with pytest.raises(StrategyFilterError) as exc_info:
        suggest_strategy_legs_from_walls(df, "BULL_PUT_SPREAD", min_net_credit_dollars=300.0)
    assert exc_info.value.skip_reason == "BELOW_MIN_CREDIT"

    # Entry basis alignment: mid passes $100, LAST_CLOSE_ONLY ($1.50 - $0.90 = $60) rejects
    df_basis = _custom_put_chain(
        [
            (90.0, 0.95, 1.05, 0.90, 500),
            (95.0, 2.15, 2.25, 1.50, 8_000),
        ],
        spot=100.0,
    )
    ok_mid = try_suggest_strategy_legs_from_walls(
        df_basis,
        "BULL_PUT_SPREAD",
        min_net_credit_dollars=100.0,
        entry_price_basis="AUTO_MID_LAST_CLOSE",
    )
    assert ok_mid.legs is not None
    bad_last = try_suggest_strategy_legs_from_walls(
        df_basis,
        "BULL_PUT_SPREAD",
        min_net_credit_dollars=100.0,
        entry_price_basis="LAST_CLOSE_ONLY",
    )
    assert bad_last.legs is None
    assert bad_last.skip_reason == "BELOW_MIN_CREDIT"

    # Guard max_loss <= 0 (credit >= width * 100) -> rejected cleanly with NON_POSITIVE_MAX_LOSS
    df_arb = _custom_put_chain(
        [
            (90.0, 0.45, 0.55, 0.50, 500),
            (95.0, 5.95, 6.05, 6.00, 8_000),
        ],
        spot=100.0,
    )
    arb_out = try_suggest_strategy_legs_from_walls(
        df_arb, "BULL_PUT_SPREAD", min_net_credit_dollars=100.0
    )
    assert arb_out.legs is None
    assert arb_out.skip_reason == "NON_POSITIVE_MAX_LOSS"


def test_zero_and_short_dte_strategy_curves_pop_iv_shift_and_zero_delta_guard() -> None:
    df_0d = _build_synthetic_chain(
        spot=100.0,
        strikes=[90.0, 95.0, 98.0, 100.0, 102.0, 105.0, 110.0],
        expiries=[("2026-11-11", 0), ("2026-11-18", 7)],
        put_wall=98.0,
        call_wall=102.0,
        iv=0.45,
    )
    # 0-DTE Today (T+0) uses intraday 0.5d BSM (distinct from At Expiration intrinsic)
    # and reacts to iv_shift.
    ev_0d = evaluate_strategy(
        df_0d,
        strategy_kind="BULL_PUT_SPREAD",
        target_expiry="2026-11-11",
        iv_shift=0.15,
    )
    assert ev_0d.short_dte == 0
    assert ev_0d.days_forward == 0
    assert ev_0d.risk.prob_of_profit_pct is not None and 0.0 < ev_0d.risk.prob_of_profit_pct < 100.0
    assert (
        ev_0d.risk.expected_move_1sigma_pct is not None
        and ev_0d.risk.expected_move_1sigma_pct > 0.0
    )
    assert ev_0d.risk.max_loss > 0.0
    assert len(ev_0d.risk.breakevens) == 1
    assert [c.label for c in ev_0d.curves] == [
        "Today (T+0)",
        "At Expiration (0d)",
        "Simulated (T+0d IV+15%)",
    ]
    spot_idx = min(range(len(ev_0d.price_grid)), key=lambda i: abs(ev_0d.price_grid[i] - 100.0))
    today_at_spot = ev_0d.curves[0].pnl_values[spot_idx]
    exp_at_spot = ev_0d.curves[1].pnl_values[spot_idx]
    sim_at_spot = ev_0d.curves[2].pnl_values[spot_idx]
    assert exp_at_spot == pytest.approx(ev_0d.pricing.mid_cash_total, abs=0.05)
    assert today_at_spot < exp_at_spot
    assert sim_at_spot != pytest.approx(today_at_spot, abs=0.05)

    # Short-DTE (0d/7d) Diagonal and Iron Condor build cleanly
    ev_diag = evaluate_strategy(df_0d, strategy_kind="DIAGONAL_SPREAD", target_expiry="2026-11-11")
    assert ev_diag.short_dte == 0 and ev_diag.long_dte == 7
    ev_ic = evaluate_strategy(df_0d, strategy_kind="IRON_CONDOR", target_expiry="2026-11-18")
    assert ev_ic.short_dte == 7 and len(ev_ic.legs) == 4

    # Zero delta (0.0) on far OTM 0-DTE put must not masquerade as target 0.28 delta
    df_zero_delta = _custom_put_chain(
        [
            (70.0, 0.04, 0.06, 0.05, 500),
            (95.0, 0.38, 0.42, 0.40, 500),
            (98.0, 0.98, 1.02, 1.00, 500),
        ],
        spot=100.0,
        expiry="2026-11-11",
        dte=0,
    ).with_columns(
        pl.when(pl.col("strike") == 70.0)
        .then(0.0)
        .when(pl.col("strike") == 98.0)
        .then(-0.28)
        .otherwise(-0.10)
        .alias("delta")
    )
    legs_zd, _, _, _ = suggest_strategy_legs_from_walls(df_zero_delta, "BULL_PUT_SPREAD")
    short_zd = next(leg for leg in legs_zd if leg.side == "SELL")
    assert short_zd.strike == pytest.approx(98.0)


# ------------------------------------------------------------------ Preset registry
def test_preset_strategies_order_literals_and_every_preset_builds() -> None:
    assert PRESET_STRATEGIES == (
        "BULL_PUT_SPREAD",
        "BULL_CALL_SPREAD",
        "DIAGONAL_SPREAD",
        "IRON_CONDOR",
        "BEAR_CALL_SPREAD",
        "BEAR_PUT_SPREAD",
    )
    assert get_args(PresetStrategyKind) == PRESET_STRATEGIES
    assert set(get_args(StrategyKind)) == {*PRESET_STRATEGIES, "CUSTOM"}

    df = _build_synthetic_chain(put_wall=90.0, call_wall=110.0)
    walls = compute_walls(df)
    for kind in PRESET_STRATEGIES:
        ev = evaluate_strategy(
            df, strategy_kind=kind, target_expiry="2026-12-19", walls_report=walls
        )
        assert ev.strategy_kind == kind
        assert ev.risk.max_loss_unbounded is False
        assert ev.risk.return_on_risk_pct is not None


# ------------------------------------------------------------------ Bear Call (mirror of Bull Put)
def test_bear_call_spread_anchors_to_call_wall_and_bounds_risk() -> None:
    df = _build_synthetic_chain(put_wall=90.0, call_wall=110.0)
    ev = evaluate_strategy(df, strategy_kind="BEAR_CALL_SPREAD", target_expiry="2026-12-19")

    assert ev.strategy_kind == "BEAR_CALL_SPREAD"
    assert ev.short_expiry == "2026-12-19"
    assert ev.primary_call_wall == pytest.approx(110.0)
    assert [(leg.side, leg.right) for leg in ev.legs] == [("SELL", "C"), ("BUY", "C")]
    short_leg, long_leg = ev.legs
    assert short_leg.strike == pytest.approx(110.0)
    assert long_leg.strike == pytest.approx(115.0)
    assert short_leg.selection_reason == "Short Call near Call Wall $110"
    assert long_leg.selection_reason == "Protective Call wing"
    assert ev.rationale[0].startswith("Short Call $110 anchored to Call Wall $110 (Delta=0.")
    assert ev.rationale[1] == "Long Call wing $115 caps risk across $5 spread width."

    assert ev.pricing.is_credit is True
    assert ev.pricing.mid_cash_total > 0.0
    assert ev.pricing.natural_cash_total < ev.pricing.mid_cash_total
    assert ev.pricing.slippage_dollars > 0.0
    assert len(ev.pricing.limit_ladder) >= 2

    # Expiry payoff: +credit below the short call, credit - width above the wing.
    credit = ev.pricing.mid_cash_total
    width = (long_leg.strike - short_leg.strike) * 100.0
    assert ev.risk.max_loss_unbounded is False
    assert ev.risk.max_profit == pytest.approx(credit, abs=0.01)
    assert ev.risk.max_loss == pytest.approx(width - credit, abs=0.01)
    assert ev.risk.return_on_risk_pct == pytest.approx(round(credit / (width - credit) * 100.0, 1))
    assert ev.risk.breakevens == [pytest.approx(short_leg.strike + credit / 100.0, abs=0.01)]
    assert ev.risk.prob_of_profit_pct is not None and 50.0 < ev.risk.prob_of_profit_pct < 99.0
    assert ev.net_greeks.delta_shares < 0.0
    assert ev.net_greeks.theta_daily_dollars > 0.0
    assert len(ev.curves) == 3  # Today, Midpoint, At Expiration


def test_bear_call_spread_entry_filters_evaluate_all_wings_beyond_top4() -> None:
    # Mirror of the Bull Put case: Call Wall 110C ($1.40). The first 4 higher wings (112.5, 115,
    # 117.5, 120) yield < $100 credit; the 5th (125C, $0.25) clears $115 beyond ordered[:4].
    df = _custom_call_chain(
        [
            (105.0, 2.48, 2.52, 2.50, 400),
            (110.0, 1.38, 1.42, 1.40, 12_000),
            (112.5, 0.93, 0.97, 0.95, 200),
            (115.0, 0.73, 0.77, 0.75, 200),
            (117.5, 0.53, 0.57, 0.55, 200),
            (120.0, 0.43, 0.47, 0.45, 200),
            (125.0, 0.23, 0.27, 0.25, 200),
            (130.0, 0.08, 0.12, 0.10, 200),
        ],
        spot=100.0,
    )
    legs_uncapped, _, call_wall, put_wall = suggest_strategy_legs_from_walls(
        df, "BEAR_CALL_SPREAD", min_net_credit_dollars=0.0, min_ror_pct=0.0
    )
    assert call_wall == pytest.approx(110.0) and put_wall is None
    assert [(leg.side, leg.strike) for leg in legs_uncapped] == [("SELL", 110.0), ("BUY", 112.5)]

    ev = evaluate_strategy(df, strategy_kind="BEAR_CALL_SPREAD", min_net_credit_dollars=100.0)
    assert [(leg.side, leg.strike) for leg in ev.legs] == [("SELL", 110.0), ("BUY", 125.0)]
    assert ev.pricing.mid_cash_total == pytest.approx(115.0)
    assert ev.rationale[2] == "Spread credit $115.00 vs $1385.00 max risk (RoR 8.3%)."


def test_bear_call_spread_fallback_otm_ror_filter_and_structured_rejections() -> None:
    # Mirror of the Bull Put case: Call Wall 115C ($0.60) cannot reach $100; 105C ($2.10) / 110C
    # ($1.00) yields $110 at 28.2% RoR; 105C / 115C yields $150 at 17.6%; ITM 90C never sells.
    df = _custom_call_chain(
        [
            (90.0, 10.90, 11.10, 11.00, 300),
            (105.0, 2.08, 2.12, 2.10, 800),
            (110.0, 0.98, 1.02, 1.00, 600),
            (115.0, 0.58, 0.62, 0.60, 15_000),
        ],
        spot=100.0,
    )
    ev = evaluate_strategy(
        df, strategy_kind="BEAR_CALL_SPREAD", min_net_credit_dollars=100.0, min_ror_pct=20.0
    )
    assert [(leg.side, leg.strike) for leg in ev.legs] == [("SELL", 105.0), ("BUY", 110.0)]
    # Hand-computed expiry payoff: +$110 below 105, $110 - $500 above 110, zero at 105 + 1.10.
    assert ev.pricing.mid_cash_total == pytest.approx(110.0)
    assert ev.risk.max_profit == pytest.approx(110.0)
    assert ev.risk.max_loss == pytest.approx(390.0)
    assert ev.risk.return_on_risk_pct == pytest.approx(28.2)
    assert ev.risk.breakevens == [pytest.approx(106.10)]

    # Impossible thresholds -> legs=None with the same structured skip reasons as Bull Put.
    for min_credit, min_ror, reason in (
        (300.0, 20.0, "BELOW_MIN_CREDIT"),
        (None, 50.0, "BELOW_MIN_ROR"),
        (300.0, 50.0, "BELOW_MIN_CREDIT_AND_ROR"),
    ):
        outcome = try_suggest_strategy_legs_from_walls(
            df, "BEAR_CALL_SPREAD", min_net_credit_dollars=min_credit, min_ror_pct=min_ror
        )
        assert outcome.legs is None
        assert outcome.skip_reason == reason
    assert outcome.notes == [
        "No OTM Bear Call Spread met entry quality thresholds (min_credit=$300, min_ror=50%)."
    ]

    with pytest.raises(StrategyFilterError) as exc_info:
        suggest_strategy_legs_from_walls(df, "BEAR_CALL_SPREAD", min_net_credit_dollars=300.0)
    assert exc_info.value.skip_reason == "BELOW_MIN_CREDIT"

    # Entry basis alignment: mid passes $100, LAST_CLOSE_ONLY ($1.50 - $0.90 = $60) rejects.
    df_basis = _custom_call_chain(
        [
            (105.0, 2.15, 2.25, 1.50, 8_000),
            (110.0, 0.95, 1.05, 0.90, 500),
        ],
        spot=100.0,
    )
    ok_mid = try_suggest_strategy_legs_from_walls(
        df_basis,
        "BEAR_CALL_SPREAD",
        min_net_credit_dollars=100.0,
        entry_price_basis="AUTO_MID_LAST_CLOSE",
    )
    assert ok_mid.legs is not None
    bad_last = try_suggest_strategy_legs_from_walls(
        df_basis,
        "BEAR_CALL_SPREAD",
        min_net_credit_dollars=100.0,
        entry_price_basis="LAST_CLOSE_ONLY",
    )
    assert bad_last.legs is None
    assert bad_last.skip_reason == "BELOW_MIN_CREDIT"

    # Credit >= width * 100 -> max loss <= 0 -> rejected cleanly with NON_POSITIVE_MAX_LOSS.
    df_arb = _custom_call_chain(
        [
            (105.0, 5.95, 6.05, 6.00, 8_000),
            (110.0, 0.45, 0.55, 0.50, 500),
        ],
        spot=100.0,
    )
    arb_out = try_suggest_strategy_legs_from_walls(
        df_arb, "BEAR_CALL_SPREAD", min_net_credit_dollars=100.0
    )
    assert arb_out.legs is None
    assert arb_out.skip_reason == "NON_POSITIVE_MAX_LOSS"
    assert arb_out.notes == ["No viable Bear Call Spread pair (NON_POSITIVE_MAX_LOSS)."]


def test_bear_call_spread_insufficient_wings_and_missing_candidates() -> None:
    one_otm = _custom_call_chain([(110.0, 0.98, 1.02, 1.00, 600)], spot=100.0)
    outcome = try_suggest_strategy_legs_from_walls(
        one_otm, "BEAR_CALL_SPREAD", min_net_credit_dollars=10.0
    )
    assert outcome.legs is None and outcome.skip_reason == "INSUFFICIENT_WINGS"
    assert outcome.notes == ["Insufficient higher-strike Call wings for Bear Call Spread."]
    with pytest.raises(ValueError, match=r"Insufficient wing strikes above \$110"):
        try_suggest_strategy_legs_from_walls(one_otm, "BEAR_CALL_SPREAD")

    itm_only = _custom_call_chain(
        [(90.0, 10.90, 11.10, 11.00, 500), (95.0, 6.90, 7.10, 7.00, 500)], spot=100.0
    )
    no_otm = try_suggest_strategy_legs_from_walls(itm_only, "BEAR_CALL_SPREAD", min_ror_pct=5.0)
    assert no_otm.legs is None and no_otm.skip_reason == "NO_OTM_CALL_CANDIDATES"
    assert no_otm.notes == ["No OTM/near-ATM short call candidates with usable entry prices."]

    inverted = _custom_call_chain(
        [(105.0, 0.98, 1.02, 1.00, 900), (110.0, 1.98, 2.02, 2.00, 500)], spot=100.0
    )
    no_credit = try_suggest_strategy_legs_from_walls(
        inverted, "BEAR_CALL_SPREAD", min_net_credit_dollars=10.0
    )
    assert no_credit.legs is None and no_credit.skip_reason == "NON_POSITIVE_CREDIT"
    assert no_credit.notes == ["No viable Bear Call Spread pair (NON_POSITIVE_CREDIT)."]

    puts_only = _custom_put_chain([(90.0, 0.98, 1.02, 1.00, 500), (95.0, 1.98, 2.02, 2.00, 500)])
    with pytest.raises(ValueError, match="No eligible option contracts"):
        evaluate_strategy(puts_only, strategy_kind="BEAR_CALL_SPREAD")


# ------------------------------------------------------------------ Bear Put (mirror of Bull Call)
def test_bear_put_spread_anchors_short_floor_to_put_wall() -> None:
    df = _build_synthetic_chain(put_wall=90.0, call_wall=110.0)
    ev = evaluate_strategy(df, strategy_kind="BEAR_PUT_SPREAD", target_expiry="2026-12-19")

    assert ev.strategy_kind == "BEAR_PUT_SPREAD"
    assert ev.primary_put_wall == pytest.approx(90.0)
    assert [(leg.side, leg.right) for leg in ev.legs] == [("BUY", "P"), ("SELL", "P")]
    long_leg, short_leg = ev.legs
    assert long_leg.strike == pytest.approx(100.0)
    assert short_leg.strike == pytest.approx(90.0)
    assert long_leg.selection_reason == "ATM/near-ATM Long Put"
    assert short_leg.selection_reason == "Short Put floor at Put Wall $90"
    assert ev.rationale == [
        "Long Put $100 selected near ATM for strong directional Delta leverage.",
        "Short Put $90 caps downside right at Put Wall $90 to lower net debit.",
    ]

    # Expiry payoff: -debit above the long put, width - debit below the short put.
    debit = -ev.pricing.mid_cash_total
    width = (long_leg.strike - short_leg.strike) * 100.0
    assert ev.pricing.is_credit is False and debit > 0.0
    assert ev.pricing.natural_cash_total < ev.pricing.mid_cash_total
    assert ev.risk.max_loss_unbounded is False
    assert ev.risk.max_loss == pytest.approx(debit, abs=0.01)
    assert ev.risk.max_profit == pytest.approx(width - debit, abs=0.01)
    assert ev.risk.return_on_risk_pct == pytest.approx(round((width - debit) / debit * 100.0, 1))
    assert ev.risk.breakevens == [pytest.approx(long_leg.strike - debit / 100.0, abs=0.01)]
    assert ev.net_greeks.delta_shares < 0.0
    assert ev.net_greeks.theta_daily_dollars < 0.0


def test_bear_put_spread_needs_lower_strike_and_ignores_entry_floors() -> None:
    df = _build_synthetic_chain(put_wall=90.0, call_wall=110.0)
    plain = suggest_strategy_legs_from_walls(df, "BEAR_PUT_SPREAD", target_expiry="2026-12-19")[0]
    floored = suggest_strategy_legs_from_walls(
        df,
        "BEAR_PUT_SPREAD",
        target_expiry="2026-12-19",
        min_net_credit_dollars=500.0,
        min_ror_pct=900.0,
    )[0]
    assert floored == plain  # debit verticals ignore credit / RoR floors, exactly like Bull Call

    single = _custom_put_chain([(100.0, 3.98, 4.02, 4.00, 500)], spot=100.0)
    with pytest.raises(ValueError, match="Insufficient lower Put strikes below ATM for Bear Put"):
        evaluate_strategy(single, strategy_kind="BEAR_PUT_SPREAD")
    calls_only = _custom_call_chain([(100.0, 3.98, 4.02, 4.00, 500), (105.0, 1.98, 2.02, 2.0, 500)])
    with pytest.raises(ValueError, match="No eligible option contracts"):
        evaluate_strategy(calls_only, strategy_kind="BEAR_PUT_SPREAD")


def _reflect_chain(df: pl.DataFrame, spot: float = 100.0) -> pl.DataFrame:
    """Mirror a chain around spot: strike K -> 2S - K, calls <-> puts, Delta sign flipped."""
    return df.with_columns(
        (2.0 * spot - pl.col("strike")).alias("strike"),
        (-pl.col("moneyness_pct")).alias("moneyness_pct"),
        pl.when(pl.col("right") == "P").then(pl.lit("C")).otherwise(pl.lit("P")).alias("right"),
        (-pl.col("delta")).alias("delta"),
    )


@pytest.mark.parametrize(
    ("bull_kind", "bear_kind", "bull_right"),
    [
        ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "P"),
        ("BULL_CALL_SPREAD", "BEAR_PUT_SPREAD", "C"),
    ],
)
@pytest.mark.parametrize(
    ("min_credit", "min_ror"), [(None, None), (40.0, None), (None, 15.0), (500.0, None)]
)
def test_bear_presets_mirror_bull_presets_on_reflected_chain(
    bull_kind: PresetStrategyKind,
    bear_kind: PresetStrategyKind,
    bull_right: str,
    min_credit: float | None,
    min_ror: float | None,
) -> None:
    one_right = _build_synthetic_chain(put_wall=90.0, call_wall=110.0).filter(
        pl.col("right") == bull_right
    )
    bull = try_suggest_strategy_legs_from_walls(
        one_right,
        bull_kind,
        target_expiry="2026-12-19",
        min_net_credit_dollars=min_credit,
        min_ror_pct=min_ror,
    )
    bear = try_suggest_strategy_legs_from_walls(
        _reflect_chain(one_right),
        bear_kind,
        target_expiry="2026-12-19",
        min_net_credit_dollars=min_credit,
        min_ror_pct=min_ror,
    )
    assert bear.skip_reason == bull.skip_reason
    if bull.legs is None:
        assert bear.legs is None
    else:
        assert bear.legs is not None
        mirrored = [(leg.side, 200.0 - leg.strike) for leg in bear.legs]
        assert mirrored == [(leg.side, leg.strike) for leg in bull.legs]


# ------------------------------------------------------------------ EV/risk & break-even distance
def test_expected_return_on_risk_math_and_none_cases() -> None:
    # (0.60 x $100 - 0.40 x $400) / $400 = -25%; (0.80 x $300 - 0.20 x $200) / $200 = +100%.
    assert compute_expected_return_on_risk_pct(60.0, 100.0, 400.0, max_loss_unbounded=False) == (
        -25.0
    )
    assert compute_expected_return_on_risk_pct(80.0, 300.0, 200.0, max_loss_unbounded=False) == (
        100.0
    )
    assert compute_expected_return_on_risk_pct(0.0, 300.0, 200.0, max_loss_unbounded=False) == (
        -100.0
    )
    assert compute_expected_return_on_risk_pct(100.0, 0.0, 200.0, max_loss_unbounded=False) == 0.0
    unusable: list[tuple[float | None, float, float, bool]] = [
        (None, 100.0, 400.0, False),
        (math.nan, 100.0, 400.0, False),
        (-0.1, 100.0, 400.0, False),
        (100.1, 100.0, 400.0, False),
        (60.0, math.nan, 400.0, False),
        (60.0, -1.0, 400.0, False),
        (60.0, 100.0, 0.0, False),
        (60.0, 100.0, 1e-7, False),
        (60.0, 100.0, -400.0, False),
        (60.0, 100.0, math.inf, False),
        (60.0, 100.0, 400.0, True),
    ]
    for pop, max_profit, max_loss, unbounded in unusable:
        assert (
            compute_expected_return_on_risk_pct(
                pop, max_profit, max_loss, max_loss_unbounded=unbounded
            )
            is None
        )


def test_breakeven_distance_single_two_and_none() -> None:
    assert compute_nearest_breakeven_distance_pct([106.1], 100.0) == 6.1
    assert compute_nearest_breakeven_distance_pct([92.0, 103.0], 100.0) == 3.0
    assert compute_nearest_breakeven_distance_pct([88.65, 111.35], 100.0) == 11.35
    assert compute_nearest_breakeven_distance_pct([95.0], 50.0) == 90.0
    assert compute_nearest_breakeven_distance_pct([], 100.0) is None
    assert compute_nearest_breakeven_distance_pct([math.nan], 100.0) is None
    assert compute_nearest_breakeven_distance_pct([95.0], 0.0) is None
    assert compute_nearest_breakeven_distance_pct([95.0], math.nan) is None

    # Credit wider than the spread: expiry P&L >= +$50 everywhere -> no break-even, no risk.
    arb = _custom_put_chain([(90.0, 0.45, 0.55, 0.50, 500), (95.0, 5.95, 6.05, 6.00, 8_000)])
    ev = evaluate_strategy(
        arb,
        strategy_kind="CUSTOM",
        legs=[
            StrategyLegInput(side="SELL", right="P", expiry="2026-12-19", strike=95.0),
            StrategyLegInput(side="BUY", right="P", expiry="2026-12-19", strike=90.0),
        ],
    )
    assert ev.risk.breakevens == []
    assert ev.risk.nearest_breakeven_distance_pct is None
    assert ev.risk.max_loss == 0.0 and ev.risk.max_profit == pytest.approx(550.0)
    assert ev.risk.return_on_risk_pct is None
    assert ev.risk.expected_return_on_risk_pct is None


def test_engine_reports_ev_risk_and_breakeven_distance_from_its_own_metrics() -> None:
    df = _build_synthetic_chain(put_wall=90.0, call_wall=110.0)
    walls = compute_walls(df)
    for kind in PRESET_STRATEGIES:
        risk = evaluate_strategy(
            df, strategy_kind=kind, target_expiry="2026-12-19", walls_report=walls
        ).risk
        assert risk.prob_of_profit_pct is not None and risk.max_loss > 0.0
        pop = risk.prob_of_profit_pct / 100.0
        expected_ev = (pop * risk.max_profit - (1.0 - pop) * risk.max_loss) / risk.max_loss
        assert risk.expected_return_on_risk_pct == pytest.approx(round(expected_ev * 100.0, 1))
        nearest = min(abs(be - 100.0) for be in risk.breakevens)
        assert risk.nearest_breakeven_distance_pct == pytest.approx(round(nearest, 2))

    # Two break-evens (Iron Condor): the side closer to spot wins.
    ic = evaluate_strategy(
        df, strategy_kind="IRON_CONDOR", target_expiry="2026-12-19", walls_report=walls
    )
    low_be, high_be = ic.risk.breakevens
    assert ic.risk.nearest_breakeven_distance_pct == pytest.approx(
        round(min(100.0 - low_be, high_be - 100.0), 2)
    )

    # Naked short call: unbounded loss -> RoR and EV/risk undefined; break-even still measured.
    naked = evaluate_strategy(
        df,
        strategy_kind="CUSTOM",
        legs=[StrategyLegInput(side="SELL", right="C", expiry="2026-12-19", strike=105.0)],
    )
    assert naked.risk.max_loss_unbounded is True
    assert naked.risk.return_on_risk_pct is None
    assert naked.risk.expected_return_on_risk_pct is None
    assert naked.risk.nearest_breakeven_distance_pct is not None


# ------------------------------------------------------------------ Candidate filters
def test_candidate_filters_semantics_and_validation() -> None:
    credit = StrategyPricing(
        is_credit=True,
        net_mid_per_share=1.0,
        net_natural_per_share=0.9,
        mid_cash_total=300.0,
        natural_cash_total=270.0,
        slippage_dollars=30.0,
        slippage_pct=10.0,
        limit_ladder=[0.9, 0.95, 1.0],
    )
    debit = credit.model_copy(
        update={"is_credit": False, "net_mid_per_share": -1.0, "mid_cash_total": -300.0}
    )
    risk = StrategyRiskMetrics(
        max_profit=300.0,
        max_loss=1500.0,
        max_loss_unbounded=False,
        return_on_risk_pct=20.0,
        breakevens=[97.0],
        prob_of_profit_pct=70.0,
        expected_move_1sigma_pct=None,
        expected_move_1sigma_low=None,
        expected_move_1sigma_high=None,
    )
    blank = risk.model_copy(update={"return_on_risk_pct": None, "prob_of_profit_pct": None})
    flt = StrategyCandidateFilters

    assert candidate_passes_filters(credit, risk, None)
    assert candidate_passes_filters(
        debit, blank, flt(min_net_credit_dollars=0.0, min_ror_pct=0.0, min_pop_pct=0.0)
    )  # 0.0 disables every floor
    # Credit floor is per unit: $300 total over qty 3 = $100 per spread.
    assert candidate_passes_filters(
        credit, risk, flt(min_net_credit_dollars=100.0), qty_multiplier=3
    )
    assert not candidate_passes_filters(
        credit, risk, flt(min_net_credit_dollars=100.01), qty_multiplier=3
    )
    assert not candidate_passes_filters(debit, risk, flt(min_net_credit_dollars=1.0))
    # Floors are inclusive on the reported metrics and apply to debit structures too.
    assert candidate_passes_filters(debit, risk, flt(min_ror_pct=20.0, min_pop_pct=70.0))
    assert not candidate_passes_filters(credit, risk, flt(min_ror_pct=20.1))
    assert not candidate_passes_filters(credit, risk, flt(min_pop_pct=70.1))
    assert not candidate_passes_filters(credit, blank, flt(min_ror_pct=1.0))
    assert not candidate_passes_filters(credit, blank, flt(min_pop_pct=1.0))
    assert candidate_passes_filters(credit, blank, flt(min_net_credit_dollars=50.0))
    with pytest.raises(ValueError, match="qty_multiplier"):
        candidate_passes_filters(credit, risk, None, qty_multiplier=0)

    for bad in (
        {"min_net_credit_dollars": -1.0},
        {"min_ror_pct": math.nan},
        {"min_pop_pct": 100.5},
        {"min_pop": 10.0},
    ):
        with pytest.raises(ValidationError):
            StrategyCandidateFilters.model_validate(bad)


# ------------------------------------------------------------------ Live re-pick
_EXP = "2026-12-19"


def _put_quote_chain(mids: dict[float, float], *, spot: float = 100.0) -> pl.DataFrame:
    """Put-only chain quoted $0.02 either side of each mid (OI 500, IV 0.35 everywhere)."""
    return _custom_put_chain(
        [(k, round(m - 0.02, 2), round(m + 0.02, 2), m, 500) for k, m in sorted(mids.items())],
        spot=spot,
    )


def _bull_put(short_k: float, long_k: float) -> list[StrategyLegInput]:
    return [
        StrategyLegInput(
            side="SELL",
            right="P",
            expiry=_EXP,
            strike=short_k,
            selection_reason="Short Put near Put Wall $95",
        ),
        StrategyLegInput(
            side="BUY",
            right="P",
            expiry=_EXP,
            strike=long_k,
            selection_reason="Protective Put wing",
        ),
    ]


def _strikes(legs: list[StrategyLegInput]) -> list[float]:
    return [leg.strike for leg in legs]


# Window short {90, 95, 100} x long {85, 90, 95}; RoR = credit / (width - credit):
# current 95/90 = $100 / $400 = 25.0%; 90/85 11.1%; 95/85 17.6%; 100/85 $350 / $1150 = 30.4%;
# 100/90 $300 / $700 = 42.9%; 100/95 $200 / $300 = 66.7%.
_REPICK_MIDS = {85.0: 0.50, 90.0: 1.00, 95.0: 2.00, 100.0: 4.00}


def test_repick_switches_to_higher_ror_neighbour_with_engine_metrics() -> None:
    df = _put_quote_chain(_REPICK_MIDS)
    current = _bull_put(95.0, 90.0)
    res = repick_strategy_legs("BULL_PUT_SPREAD", current, df)

    assert res.adjusted is True and res.keep_reason is None
    assert res.candidates_evaluated == 5  # (90,90), (90,95), (95,95) break the vertical geometry
    assert [(leg.side, leg.strike) for leg in res.legs] == [("SELL", 100.0), ("BUY", 95.0)]
    assert [leg.selection_reason for leg in res.legs] == [leg.selection_reason for leg in current]
    assert res.current_passes_filters is True
    assert res.pricing.mid_cash_total == pytest.approx(200.0)
    assert res.risk.max_profit == pytest.approx(200.0)
    assert res.risk.max_loss == pytest.approx(300.0)
    assert res.risk.return_on_risk_pct == 66.7
    assert res.risk.breakevens == [pytest.approx(98.0)]

    # Same numbers as the full Strategy Lab engine on the same frame and legs.
    ev = evaluate_strategy(df, strategy_kind="BULL_PUT_SPREAD", legs=res.legs)
    assert res.pricing == ev.pricing
    assert res.risk == ev.risk
    assert res.net_greeks == ev.net_greeks
    assert res.evaluated_legs == ev.legs
    assert res.underlying == "NVDA" and res.underlying_price == pytest.approx(100.0)


def test_repick_filters_reject_better_ror_neighbours() -> None:
    df = _put_quote_chain(_REPICK_MIDS)
    current = _bull_put(95.0, 90.0)

    def _repick(filters: StrategyCandidateFilters, qty_multiplier: int = 1) -> StrategyRepickResult:
        return repick_strategy_legs(
            "BULL_PUT_SPREAD", current, df, filters=filters, qty_multiplier=qty_multiplier
        )

    floor_250 = _repick(StrategyCandidateFilters(min_net_credit_dollars=250.0))
    assert floor_250.adjusted and _strikes(floor_250.legs) == [100.0, 90.0]
    assert floor_250.risk.return_on_risk_pct == 42.9
    assert floor_250.current_passes_filters is False  # current credit $100 < $250

    floor_320 = _repick(StrategyCandidateFilters(min_net_credit_dollars=320.0))
    assert floor_320.adjusted and _strikes(floor_320.legs) == [100.0, 85.0]
    assert floor_320.risk.return_on_risk_pct == 30.4

    per_unit = _repick(StrategyCandidateFilters(min_net_credit_dollars=250.0), qty_multiplier=3)
    assert _strikes(per_unit.legs) == [100.0, 90.0]
    assert per_unit.pricing.mid_cash_total == pytest.approx(900.0)
    assert [leg.qty for leg in per_unit.evaluated_legs] == [3, 3]

    blocked = _repick(StrategyCandidateFilters(min_net_credit_dollars=400.0))
    assert blocked.adjusted is False and blocked.keep_reason == "BETTER_CANDIDATE_FILTERED"
    assert blocked.legs == current and blocked.risk.return_on_risk_pct == 25.0

    ror_floor = _repick(StrategyCandidateFilters(min_ror_pct=50.0))
    assert _strikes(ror_floor.legs) == [100.0, 95.0] and ror_floor.current_passes_filters is False

    # Every better neighbour sells the ATM 100P, so its POP sits below the current 95P short.
    walls = compute_walls(df)
    cur_pop = evaluate_strategy(
        df, strategy_kind="BULL_PUT_SPREAD", legs=current, walls_report=walls
    ).risk.prob_of_profit_pct
    assert cur_pop is not None
    for long_k in (85.0, 90.0, 95.0):
        better_pop = evaluate_strategy(
            df, strategy_kind="BULL_PUT_SPREAD", legs=_bull_put(100.0, long_k), walls_report=walls
        ).risk.prob_of_profit_pct
        assert better_pop is not None and better_pop < cur_pop
    pop_block = _repick(StrategyCandidateFilters(min_pop_pct=cur_pop))
    assert pop_block.keep_reason == "BETTER_CANDIDATE_FILTERED"
    assert pop_block.current_passes_filters is True


def test_repick_tie_keeps_current_legs() -> None:
    # 100/95 ($100 / $400) and 100/90 ($200 / $800) both tie the current 95/90 at 25.0%.
    df = _put_quote_chain({85.0: 0.50, 90.0: 1.00, 95.0: 2.00, 100.0: 3.00})
    current = _bull_put(95.0, 90.0)
    walls = compute_walls(df)
    tied = [
        evaluate_strategy(
            df, strategy_kind="BULL_PUT_SPREAD", legs=_bull_put(100.0, k), walls_report=walls
        ).risk.return_on_risk_pct
        for k in (95.0, 90.0)
    ]
    assert tied == [25.0, 25.0]

    res = repick_strategy_legs("BULL_PUT_SPREAD", current, df)
    assert res.adjusted is False and res.keep_reason == "NO_BETTER_CANDIDATE"
    assert res.legs == current
    assert res.risk.return_on_risk_pct == 25.0


def test_repick_skips_unquoted_neighbours_and_reports_keep_reasons() -> None:
    current = _bull_put(95.0, 90.0)
    # An unquoted 100P (no bid/ask/mid/last/close) must not fake a better credit.
    unquoted = _put_quote_chain(_REPICK_MIDS).with_columns(
        [
            pl.when(pl.col("strike") == 100.0).then(None).otherwise(pl.col(col)).alias(col)
            for col in ("bid", "ask", "mid", "last", "close", "mark_price")
        ]
    )
    res = repick_strategy_legs("BULL_PUT_SPREAD", current, unquoted)
    assert res.candidates_evaluated == 2  # only 90/85 and 95/85 remain priced and valid
    assert res.keep_reason == "NO_BETTER_CANDIDATE" and res.legs == current

    lone = repick_strategy_legs(
        "BULL_PUT_SPREAD", current, _put_quote_chain(_REPICK_MIDS), window_strikes=[[95.0], [90.0]]
    )
    assert lone.candidates_evaluated == 0 and lone.keep_reason == "NO_ALTERNATIVE_IN_WINDOW"

    # Credit above the width => max loss $0 => RoR undefined: never switch away on that basis.
    arb = _put_quote_chain({85.0: 0.25, 90.0: 0.50, 95.0: 6.00, 100.0: 6.50})
    stuck = repick_strategy_legs("BULL_PUT_SPREAD", current, arb)
    assert stuck.risk.return_on_risk_pct is None and stuck.candidates_evaluated == 5
    assert stuck.adjusted is False and stuck.keep_reason == "CURRENT_ROR_UNAVAILABLE"


def test_repick_explicit_window_stays_fixed_after_adjustment() -> None:
    first = _put_quote_chain(_REPICK_MIDS)
    current = _bull_put(95.0, 90.0)
    window = repick_window_strikes(current, first)
    assert window == [[90.0, 95.0, 100.0], [85.0, 90.0, 95.0]]
    step1 = repick_strategy_legs("BULL_PUT_SPREAD", current, first, window_strikes=window)
    assert _strikes(step1.legs) == [100.0, 95.0]

    # Next tick 90/85 is best ($250 / $250 = 100%): two strikes below the adjusted legs, yet inside
    # the session window. A window re-centred on the adjusted legs only sees 11.1% ties.
    second = _put_quote_chain({85.0: 0.50, 90.0: 3.00, 95.0: 3.50, 100.0: 4.00})
    fixed = repick_strategy_legs("BULL_PUT_SPREAD", step1.legs, second, window_strikes=window)
    assert fixed.adjusted and _strikes(fixed.legs) == [90.0, 85.0]
    assert fixed.risk.return_on_risk_pct == 100.0
    recentred = repick_strategy_legs("BULL_PUT_SPREAD", step1.legs, second)
    assert recentred.adjusted is False and recentred.keep_reason == "NO_BETTER_CANDIDATE"

    # Window strikes missing from the frame are ignored; a narrower window hides the best pair.
    partial = repick_strategy_legs(
        "BULL_PUT_SPREAD", current, first, window_strikes=[[95.0, 97.5, 100.0], [90.0]]
    )
    assert partial.candidates_evaluated == 1 and _strikes(partial.legs) == [100.0, 90.0]
    narrow = repick_strategy_legs(
        "BULL_PUT_SPREAD", current, first, window_strikes=[[90.0, 95.0], [85.0, 90.0, 95.0]]
    )
    assert narrow.candidates_evaluated == 2 and narrow.keep_reason == "NO_BETTER_CANDIDATE"

    # Edge strikes get a single neighbour; a leg missing from the frame raises.
    assert repick_window_strikes(_bull_put(100.0, 85.0), first) == [[95.0, 100.0], [85.0, 90.0]]
    with pytest.raises(ValueError, match="not found"):
        repick_window_strikes(_bull_put(97.5, 90.0), first)


def test_repick_live_spot_override_matches_engine_at_that_spot() -> None:
    df = _put_quote_chain(_REPICK_MIDS)
    current = _bull_put(95.0, 90.0)
    base = repick_strategy_legs("BULL_PUT_SPREAD", current, df)
    live = repick_strategy_legs("BULL_PUT_SPREAD", current, df, spot=97.0)

    assert live.underlying_price == pytest.approx(97.0)
    assert live.risk.prob_of_profit_pct != base.risk.prob_of_profit_pct
    shifted = df.with_columns(pl.lit(97.0).alias("underlying_price"))
    ev = evaluate_strategy(shifted, strategy_kind="BULL_PUT_SPREAD", legs=live.legs)
    assert live.pricing == ev.pricing
    assert live.risk == ev.risk
    assert live.net_greeks == ev.net_greeks


def _preset_geometry_ok(kind: PresetStrategyKind, legs: list[StrategyLegInput]) -> bool:
    k = {(leg.side, leg.right): leg.strike for leg in legs}
    rules: dict[PresetStrategyKind, Callable[[], bool]] = {
        "BULL_PUT_SPREAD": lambda: k[("SELL", "P")] > k[("BUY", "P")],
        "BEAR_CALL_SPREAD": lambda: k[("BUY", "C")] > k[("SELL", "C")],
        "BULL_CALL_SPREAD": lambda: k[("SELL", "C")] > k[("BUY", "C")],
        "BEAR_PUT_SPREAD": lambda: k[("BUY", "P")] > k[("SELL", "P")],
        "DIAGONAL_SPREAD": lambda: k[("SELL", "C")] > k[("BUY", "C")],
        "IRON_CONDOR": lambda: (
            k[("BUY", "P")] < k[("SELL", "P")] < k[("SELL", "C")] < k[("BUY", "C")]
        ),
    }
    return rules[kind]()


@pytest.mark.parametrize(
    ("kind", "expected_alternatives"),
    [
        ("BULL_PUT_SPREAD", 5),
        ("BEAR_CALL_SPREAD", 5),
        ("BULL_CALL_SPREAD", 7),
        ("BEAR_PUT_SPREAD", 7),
        ("DIAGONAL_SPREAD", 8),
        ("IRON_CONDOR", 35),
    ],
)
def test_repick_matches_bruteforce_engine_oracle_over_window(
    kind: PresetStrategyKind, expected_alternatives: int
) -> None:
    df = _build_synthetic_chain(put_wall=90.0, call_wall=110.0)
    walls = compute_walls(df)
    current = suggest_strategy_legs_from_walls(
        df, kind, target_expiry="2026-12-19", walls_report=walls
    )[0]
    window = repick_window_strikes(current, df)
    assert all(len(strikes) == 3 for strikes in window)
    res = repick_strategy_legs(kind, current, df, window_strikes=window)

    cur_ror = evaluate_strategy(
        df, strategy_kind=kind, legs=current, walls_report=walls
    ).risk.return_on_risk_pct
    assert cur_ror is not None
    alternative_rors: list[float] = []
    for strikes in itertools.product(*window):
        cand = [
            leg.model_copy(update={"strike": k}) for leg, k in zip(current, strikes, strict=True)
        ]
        if list(strikes) == _strikes(current) or not _preset_geometry_ok(kind, cand):
            continue
        ror = evaluate_strategy(
            df, strategy_kind=kind, legs=cand, walls_report=walls
        ).risk.return_on_risk_pct
        assert ror is not None
        alternative_rors.append(ror)

    assert res.candidates_evaluated == len(alternative_rors) == expected_alternatives
    best_alt = max(alternative_rors)
    assert res.adjusted is (best_alt > cur_ror)
    assert res.risk.return_on_risk_pct == max(cur_ror, best_alt)
    assert _preset_geometry_ok(kind, res.legs)
    # Side, right and expiry of every leg never change (Diagonal keeps its two expiries).
    assert [(leg.side, leg.right, leg.expiry) for leg in res.legs] == [
        (leg.side, leg.right, leg.expiry) for leg in current
    ]
    if kind == "DIAGONAL_SPREAD":
        assert [leg.expiry for leg in res.legs] == ["2027-01-16", "2026-12-19"]
    ev = evaluate_strategy(df, strategy_kind=kind, legs=res.legs, walls_report=walls)
    assert res.risk == ev.risk and res.pricing == ev.pricing


def test_repick_iron_condor_full_window_runs_well_under_50ms() -> None:
    # Dense $2.5 grid: all 3^4 = 81 combinations around 80/90/110/120 keep the condor ordering.
    df = _build_synthetic_chain(
        strikes=[70.0 + 2.5 * i for i in range(25)], put_wall=90.0, call_wall=110.0
    )
    legs = [
        StrategyLegInput(side="BUY", right="P", expiry=_EXP, strike=80.0),
        StrategyLegInput(side="SELL", right="P", expiry=_EXP, strike=90.0),
        StrategyLegInput(side="SELL", right="C", expiry=_EXP, strike=110.0),
        StrategyLegInput(side="BUY", right="C", expiry=_EXP, strike=120.0),
    ]
    window = repick_window_strikes(legs, df)
    assert [len(strikes) for strikes in window] == [3, 3, 3, 3]
    warm = repick_strategy_legs("IRON_CONDOR", legs, df, window_strikes=window)
    assert warm.candidates_evaluated == 80

    timings: list[float] = []
    for _ in range(3):
        started = time.perf_counter()
        repick_strategy_legs("IRON_CONDOR", legs, df, window_strikes=window)
        timings.append(time.perf_counter() - started)
    assert min(timings) < 0.050


def test_repick_validation_errors() -> None:
    df = _put_quote_chain(_REPICK_MIDS)
    current = _bull_put(95.0, 90.0)
    with pytest.raises(ValueError, match="preset strategies only"):
        repick_strategy_legs("CUSTOM", current, df)  # type: ignore[arg-type]
    with pytest.raises(
        ValueError, match=r"BEAR_CALL_SPREAD re-pick expects legs \[SELL C, BUY C\]"
    ):
        repick_strategy_legs("BEAR_CALL_SPREAD", current, df)
    with pytest.raises(ValueError, match="same expiry"):
        repick_strategy_legs(
            "BULL_PUT_SPREAD",
            [current[0], current[1].model_copy(update={"expiry": "2027-01-16"})],
            df,
        )
    with pytest.raises(ValueError, match="window_strikes has 1 entries for 2 legs"):
        repick_strategy_legs("BULL_PUT_SPREAD", current, df, window_strikes=[[95.0]])
    with pytest.raises(ValueError, match="qty_multiplier"):
        repick_strategy_legs("BULL_PUT_SPREAD", current, df, qty_multiplier=0)
    with pytest.raises(ValueError, match="risk_free_rate"):
        repick_strategy_legs("BULL_PUT_SPREAD", current, df, risk_free_rate=math.nan)
    for bad_spot in (0.0, -5.0, math.nan, math.inf):
        with pytest.raises(ValueError, match="spot"):
            repick_strategy_legs("BULL_PUT_SPREAD", current, df, spot=bad_spot)
    with pytest.raises(ValueError, match="not found"):
        repick_strategy_legs("BULL_PUT_SPREAD", _bull_put(97.5, 90.0), df)
    with pytest.raises(ValueError, match="no rows for leg expiries"):
        repick_strategy_legs(
            "BULL_PUT_SPREAD",
            [leg.model_copy(update={"expiry": "2030-01-18"}) for leg in current],
            df,
        )
