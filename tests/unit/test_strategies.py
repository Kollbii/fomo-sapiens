from __future__ import annotations

import math
from typing import Any

import numpy as np
import polars as pl
import pytest

from src.analytics.bsm_fallback import DAYS_PER_YEAR, DEFAULT_RISK_FREE_RATE, bsm_greeks, bsm_price
from src.analytics.strategies import (
    StrategyFilterError,
    StrategyLegInput,
    evaluate_strategy,
    suggest_strategy_legs_from_walls,
    try_suggest_strategy_legs_from_walls,
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
    rows: list[dict[str, Any]] = []
    conid = 700_000
    for strike, bid, ask, last, oi in put_quotes:
        conid += 1
        mid = round((bid + ask) / 2.0, 2)
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
                right="P",
                bid=bid,
                ask=ask,
                mid=mid,
                last=last,
                close=last,
                mark_price=mid,
                volume=100,
                open_interest=oi,
                iv=0.35,
                delta=-0.25 if strike < spot else -0.60,
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
