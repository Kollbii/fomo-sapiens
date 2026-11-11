from __future__ import annotations

import math
from typing import Any

import numpy as np
import polars as pl
import pytest

from src.analytics.bsm_fallback import DAYS_PER_YEAR, DEFAULT_RISK_FREE_RATE, bsm_greeks, bsm_price
from src.analytics.strategies import (
    StrategyLegInput,
    evaluate_strategy,
    suggest_strategy_legs_from_walls,
)
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, OptionContractRow


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
        t_years = float(dte) / DAYS_PER_YEAR
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
