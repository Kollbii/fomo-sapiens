"""Strategy evaluation: fills, risk bounds, POP, expected move, net Greeks, payoff curves."""

from __future__ import annotations

import math

import numpy as np
import polars as pl

from src.analytics.bsm_fallback import (
    DAYS_PER_YEAR,
    DEFAULT_RISK_FREE_RATE,
    MIN_DTE_YEARS_NUMERATOR,
    bsm_greeks,
    bsm_price,
    solve_implied_volatility,
)
from src.analytics.strategies.builders import find_wall_for_expiry, suggest_strategy_legs_from_walls
from src.analytics.strategies.chain import (
    PreparedRow,
    chain_reference_iv,
    find_row,
    prepare_rows,
    row_lookup_map,
)
from src.analytics.strategies.models import (
    EvaluatedLeg,
    FloatArray,
    PositionGreeks,
    StrategyEvaluationResponse,
    StrategyKind,
    StrategyLegInput,
    StrategyPayoffSeries,
    StrategyPricing,
    StrategyRiskMetrics,
)
from src.analytics.walls import ChainWallsReport, compute_walls


# ------------------------------------------------------------------ Pricing, Curves & POP Engine
def _solve_effective_ivs(
    matched: list[tuple[StrategyLegInput, PreparedRow]],
    spot: float,
    risk_free_rate: float,
    fallback_iv: float,
) -> FloatArray:
    mids = np.array([row.mid for _, row in matched], dtype=np.float64)
    spots = np.full(len(matched), spot, dtype=np.float64)
    strikes = np.array([row.strike for _, row in matched], dtype=np.float64)
    t_years = np.array(
        [max(float(row.dte), MIN_DTE_YEARS_NUMERATOR) / DAYS_PER_YEAR for _, row in matched],
        dtype=np.float64,
    )
    is_calls = np.array([row.right == "C" for _, row in matched], dtype=np.bool_)
    solved_iv, solved_mask = solve_implied_volatility(
        mark_price=mids,
        spot=spots,
        strike=strikes,
        t_years=t_years,
        risk_free_rate=risk_free_rate,
        is_call=is_calls,
    )
    out = np.empty(len(matched), dtype=np.float64)
    for idx, (_, row) in enumerate(matched):
        if bool(solved_mask[idx]):
            out[idx] = float(solved_iv[idx])
        elif row.iv is not None and 0.01 <= row.iv <= 5.0:
            out[idx] = float(row.iv)
        else:
            out[idx] = fallback_iv
    return out


def _evaluate_pnl_grid(
    price_grid: FloatArray,
    matched: list[tuple[StrategyLegInput, PreparedRow]],
    base_ivs: FloatArray,
    *,
    entry_cash_total: float,
    days_elapsed: float,
    iv_shift: float,
    qty_multiplier: int,
    risk_free_rate: float,
) -> FloatArray:
    """Compute vector P&L (in total $) across `price_grid` at `days_elapsed` & `iv_shift`."""
    total_value = np.zeros_like(price_grid, dtype=np.float64)
    for idx, (spec, row) in enumerate(matched):
        total_qty = spec.qty * qty_multiplier
        pos_sign = 1.0 if spec.side == "BUY" else -1.0
        rem_days = max(0.0, float(row.dte) - days_elapsed)
        strike_arr = np.full_like(price_grid, row.strike, dtype=np.float64)
        is_call_arr = np.full(price_grid.shape, row.right == "C", dtype=np.bool_)

        if rem_days <= 1e-9:
            leg_val = np.where(
                is_call_arr,
                np.maximum(0.0, price_grid - strike_arr),
                np.maximum(0.0, strike_arr - price_grid),
            )
        else:
            t_rem = np.full_like(price_grid, max(rem_days, MIN_DTE_YEARS_NUMERATOR) / DAYS_PER_YEAR)
            eff_iv = np.full_like(price_grid, max(0.01, min(5.0, float(base_ivs[idx]) + iv_shift)))
            leg_val = bsm_price(price_grid, strike_arr, t_rem, risk_free_rate, eff_iv, is_call_arr)

        total_value += pos_sign * float(total_qty) * 100.0 * leg_val
    return total_value + entry_cash_total


def _extract_breakevens(grid: FloatArray, pnl: FloatArray) -> list[float]:
    breakevens: list[float] = []
    for i in range(len(grid) - 1):
        y1 = float(pnl[i])
        y2 = float(pnl[i + 1])
        x1 = float(grid[i])
        x2 = float(grid[i + 1])
        if abs(y1) <= 1e-6 and x1 > 0.0:
            if not breakevens or abs(x1 - breakevens[-1]) > 0.02:
                breakevens.append(round(x1, 2))
            continue
        if (y1 < 0.0 < y2) or (y1 > 0.0 > y2):
            frac = abs(y1) / (abs(y1) + abs(y2))
            cross = x1 + frac * (x2 - x1)
            if not breakevens or abs(cross - breakevens[-1]) > 0.02:
                breakevens.append(round(cross, 2))
    return breakevens


def _estimate_pop_pct(
    spot: float,
    short_dte: int,
    ref_iv: float,
    risk_free_rate: float,
    matched: list[tuple[StrategyLegInput, PreparedRow]],
    base_ivs: FloatArray,
    entry_cash_total: float,
    qty_multiplier: int,
) -> float | None:
    if spot <= 0.0 or ref_iv <= 0.0:
        return None
    t_years = max(float(short_dte), MIN_DTE_YEARS_NUMERATOR) / DAYS_PER_YEAR
    vol_sqrt_t = ref_iv * math.sqrt(t_years)
    if vol_sqrt_t <= 1e-9:
        return None
    z_grid = np.linspace(-5.0, 5.0, 801, dtype=np.float64)
    mu = (risk_free_rate - 0.5 * ref_iv * ref_iv) * t_years
    s_grid = spot * np.exp(mu + vol_sqrt_t * z_grid)
    weights = np.exp(-0.5 * np.square(z_grid))
    weights /= float(np.sum(weights))

    horizon_pnl = _evaluate_pnl_grid(
        s_grid,
        matched,
        base_ivs,
        entry_cash_total=entry_cash_total,
        days_elapsed=float(short_dte),
        iv_shift=0.0,
        qty_multiplier=qty_multiplier,
        risk_free_rate=risk_free_rate,
    )
    pop = float(np.sum(weights[horizon_pnl > 0.0])) * 100.0
    return round(max(0.0, min(100.0, pop)), 1)


def _build_display_grid(spot: float, leg_strikes: list[float]) -> FloatArray:
    low_k = min(leg_strikes)
    high_k = max(leg_strikes)
    lo = max(0.01, min(spot * 0.72, low_k * 0.85))
    hi = max(spot * 1.28, high_k * 1.15)
    uniform = np.linspace(lo, hi, 121, dtype=np.float64)
    knots = np.array([spot, *leg_strikes], dtype=np.float64)
    merged = np.unique(np.round(np.concatenate([uniform, knots]), 4))
    return merged


def _build_limit_ladder(natural_per_share: float, mid_per_share: float) -> list[float]:
    candidates = [
        round(natural_per_share, 2),
        round(0.5 * natural_per_share + 0.5 * mid_per_share, 2),
        round(mid_per_share, 2),
    ]
    ladder: list[float] = []
    for price in candidates:
        if all(abs(price - existing) >= 0.005 for existing in ladder):
            ladder.append(price)
    return ladder


def evaluate_strategy(
    df: pl.DataFrame,
    *,
    strategy_kind: StrategyKind = "BULL_PUT_SPREAD",
    target_expiry: str | None = None,
    legs: list[StrategyLegInput] | None = None,
    days_forward: int = 0,
    iv_shift: float = 0.0,
    qty_multiplier: int = 1,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    walls_report: ChainWallsReport | None = None,
) -> StrategyEvaluationResponse:
    """Evaluate pricing, risk metrics, net Greeks, and BSM T+n curves for any multi-leg strategy."""
    underlying, spot, rows = prepare_rows(df)
    walls = walls_report or compute_walls(df)
    lookup = row_lookup_map(rows)

    rationale: list[str] = []
    eff_kind: StrategyKind = strategy_kind
    if legs is None:
        if strategy_kind == "CUSTOM":
            raise ValueError("Custom strategy evaluation requires explicit legs")
        chosen_legs, auto_notes, call_wall, put_wall = suggest_strategy_legs_from_walls(
            df,
            strategy_kind,
            target_expiry=target_expiry,
            walls_report=walls,
        )
        rationale.extend(auto_notes)
    else:
        if not legs:
            raise ValueError("At least one option leg is required")
        chosen_legs = legs
        ref_exp = target_expiry or legs[0].expiry
        _, call_wall, put_wall = find_wall_for_expiry(walls, ref_exp)
        rationale.append(f"Evaluating {len(legs)} user-selected option leg(s).")

    matched: list[tuple[StrategyLegInput, PreparedRow]] = [
        (spec, find_row(lookup, spec.expiry, spec.strike, spec.right)) for spec in chosen_legs
    ]
    short_dte = min(row.dte for _, row in matched)
    long_dte = max(row.dte for _, row in matched)
    short_expiry = min(matched, key=lambda pair: pair[1].dte)[1].expiry
    long_expiry = max(matched, key=lambda pair: pair[1].dte)[1].expiry
    eff_days_forward = max(0, min(days_forward, short_dte))

    fallback_iv = chain_reference_iv(rows, spot, short_expiry)
    base_ivs = _solve_effective_ivs(matched, spot, risk_free_rate, fallback_iv)

    # Compute net cash flows & Greeks
    eval_legs: list[EvaluatedLeg] = []
    mid_cash_total = 0.0
    natural_cash_total = 0.0
    net_delta = 0.0
    net_gamma = 0.0
    net_theta = 0.0
    net_vega = 0.0

    for idx, (spec, row) in enumerate(matched):
        total_qty = spec.qty * qty_multiplier
        cash_sign = -1.0 if spec.side == "BUY" else 1.0
        pos_sign = 1.0 if spec.side == "BUY" else -1.0
        natural_fill = (
            float(row.ask)
            if spec.side == "BUY" and row.ask is not None and row.ask > 0.0
            else (
                float(row.bid)
                if spec.side == "SELL" and row.bid is not None and row.bid > 0.0
                else row.mid
            )
        )
        mid_cash_total += cash_sign * float(total_qty) * 100.0 * row.mid
        natural_cash_total += cash_sign * float(total_qty) * 100.0 * natural_fill

        d_val, g_val, th_val, v_val = (row.delta, row.gamma, row.theta, row.vega)
        if d_val is None or g_val is None or th_val is None or v_val is None:
            t_leg = np.array([max(float(row.dte), MIN_DTE_YEARS_NUMERATOR) / DAYS_PER_YEAR])
            d_arr, g_arr, th_arr, v_arr = bsm_greeks(
                np.array([spot]),
                np.array([row.strike]),
                t_leg,
                risk_free_rate,
                np.array([float(base_ivs[idx])]),
                np.array([row.right == "C"], dtype=np.bool_),
            )
            d_val, g_val, th_val, v_val = (
                float(d_arr[0]),
                float(g_arr[0]),
                float(th_arr[0]),
                float(v_arr[0]),
            )

        net_delta += pos_sign * float(total_qty) * 100.0 * d_val
        net_gamma += pos_sign * float(total_qty) * 100.0 * g_val
        net_theta += pos_sign * float(total_qty) * 100.0 * th_val
        net_vega += pos_sign * float(total_qty) * 100.0 * v_val

        eval_legs.append(
            EvaluatedLeg(
                side=spec.side,
                right=spec.right,
                expiry=row.expiry,
                dte=row.dte,
                strike=row.strike,
                qty=total_qty,
                bid=row.bid,
                ask=row.ask,
                mid=round(row.mid, 4),
                natural_fill=round(natural_fill, 4),
                spread_pct=round(row.spread_pct, 2) if row.spread_pct is not None else None,
                iv=round(float(row.iv if row.iv is not None else base_ivs[idx]), 4),
                delta=round(d_val, 4),
                gamma=round(g_val, 5),
                theta=round(th_val, 4),
                vega=round(v_val, 4),
                open_interest=row.open_interest,
                volume=row.volume,
                option_conid=row.option_conid,
                selection_reason=spec.selection_reason,
            )
        )

    per_spread_denom = float(100 * qty_multiplier)
    net_mid_ps = round(mid_cash_total / per_spread_denom, 4)
    net_nat_ps = round(natural_cash_total / per_spread_denom, 4)
    slippage = abs(mid_cash_total - natural_cash_total)
    slippage_pct = (
        round((slippage / abs(mid_cash_total)) * 100.0, 2) if abs(mid_cash_total) > 1e-6 else None
    )

    pricing = StrategyPricing(
        is_credit=mid_cash_total >= 0.0,
        net_mid_per_share=net_mid_ps,
        net_natural_per_share=net_nat_ps,
        mid_cash_total=round(mid_cash_total, 2),
        natural_cash_total=round(natural_cash_total, 2),
        slippage_dollars=round(slippage, 2),
        slippage_pct=slippage_pct,
        limit_ladder=_build_limit_ladder(net_nat_ps, net_mid_ps),
    )

    # Diagnostic grid covering S=0 up to high multiples for exact risk bounds & breakevens
    leg_strikes = [row.strike for _, row in matched]
    diag_lo = 0.01
    diag_hi = max(spot, max(leg_strikes)) * 3.5
    diag_uniform = np.linspace(diag_lo, diag_hi, 320, dtype=np.float64)
    diag_grid = np.unique(
        np.round(np.concatenate([diag_uniform, np.array([spot, *leg_strikes])]), 4)
    )
    diag_expiry_pnl = _evaluate_pnl_grid(
        diag_grid,
        matched,
        base_ivs,
        entry_cash_total=mid_cash_total,
        days_elapsed=float(short_dte),
        iv_shift=0.0,
        qty_multiplier=qty_multiplier,
        risk_free_rate=risk_free_rate,
    )

    net_upside_call_qty = sum(
        (1 if spec.side == "BUY" else -1) * spec.qty for spec, _ in matched if spec.right == "C"
    )
    max_loss_unbounded = net_upside_call_qty < 0
    raw_min = float(np.min(diag_expiry_pnl))
    raw_max = float(np.max(diag_expiry_pnl))
    max_loss = round(max(0.0, -raw_min), 2)
    max_profit = round(max(0.0, raw_max), 2)
    ror_pct = (
        round((max_profit / max_loss) * 100.0, 1)
        if (not max_loss_unbounded and max_loss > 1e-6)
        else None
    )

    breakevens = _extract_breakevens(diag_grid, diag_expiry_pnl)
    atm_iv = chain_reference_iv(rows, spot, short_expiry)
    pop_pct = _estimate_pop_pct(
        spot, short_dte, atm_iv, risk_free_rate, matched, base_ivs, mid_cash_total, qty_multiplier
    )
    em_pct = atm_iv * math.sqrt(max(float(short_dte), MIN_DTE_YEARS_NUMERATOR) / DAYS_PER_YEAR)
    em_dollars = spot * em_pct

    risk = StrategyRiskMetrics(
        max_profit=max_profit,
        max_loss=max_loss,
        max_loss_unbounded=max_loss_unbounded,
        return_on_risk_pct=ror_pct,
        breakevens=breakevens,
        prob_of_profit_pct=pop_pct,
        expected_move_1sigma_pct=round(em_pct * 100.0, 2),
        expected_move_1sigma_low=round(max(0.01, spot - em_dollars), 2),
        expected_move_1sigma_high=round(spot + em_dollars, 2),
    )

    # Interactive Plotly curves: Today (T+0), 50% Decay, Short Expiry (+ custom Sim if active)
    display_grid = _build_display_grid(spot, leg_strikes)
    half_days = max(0, short_dte // 2)
    curve_specs: list[tuple[str, int, float]] = [
        ("Today (T+0)", 0, 0.0),
    ]
    if 0 < half_days < short_dte:
        curve_specs.append((f"Midpoint (T+{half_days}d)", half_days, 0.0))
    expiry_label = (
        f"At Short Expiry ({short_dte}d)"
        if short_dte != long_dte
        else f"At Expiration ({short_dte}d)"
    )
    curve_specs.append((expiry_label, short_dte, 0.0))

    if eff_days_forward != 0 or abs(iv_shift) > 1e-6:
        iv_tag = f" IV{iv_shift:+.0%}" if abs(iv_shift) > 1e-6 else ""
        sim_label = f"Simulated (T+{eff_days_forward}d{iv_tag})"
        curve_specs.append((sim_label, eff_days_forward, iv_shift))

    curves = [
        StrategyPayoffSeries(
            label=label,
            days_elapsed=d_el,
            iv_shift=sh,
            pnl_values=[
                round(float(v), 2)
                for v in _evaluate_pnl_grid(
                    display_grid,
                    matched,
                    base_ivs,
                    entry_cash_total=mid_cash_total,
                    days_elapsed=float(d_el),
                    iv_shift=sh,
                    qty_multiplier=qty_multiplier,
                    risk_free_rate=risk_free_rate,
                )
            ],
        )
        for label, d_el, sh in curve_specs
    ]

    return StrategyEvaluationResponse(
        underlying=underlying,
        underlying_price=round(spot, 4),
        strategy_kind=eff_kind,
        short_expiry=short_expiry,
        short_dte=short_dte,
        long_expiry=long_expiry,
        long_dte=long_dte,
        days_forward=eff_days_forward,
        iv_shift=round(iv_shift, 4),
        qty_multiplier=qty_multiplier,
        risk_free_rate=risk_free_rate,
        primary_call_wall=call_wall,
        primary_put_wall=put_wall,
        legs=eval_legs,
        pricing=pricing,
        risk=risk,
        net_greeks=PositionGreeks(
            delta_shares=round(net_delta, 2),
            gamma_shares=round(net_gamma, 2),
            theta_daily_dollars=round(net_theta, 2),
            vega_per_1pct_dollars=round(net_vega, 2),
        ),
        price_grid=[round(float(p), 2) for p in display_grid],
        curves=curves,
        rationale=rationale,
    )
