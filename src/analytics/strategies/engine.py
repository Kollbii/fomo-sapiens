"""Strategy evaluation: fills, risk bounds, POP, EV/risk, expected move, net Greeks, curves.

`evaluate_strategy` is the full Strategy Lab path (walls, preset leg selection, Plotly curves).
The metrics-only building blocks below it (`build_leg_states`, `strategy_pricing`,
`strategy_risk_metrics`, `evaluated_legs`, `net_position_greeks`) are shared with the live
re-pick (`repick.py`) so both report identical numbers for the same legs and quotes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

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
    EntryPriceBasis,
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

# Max-loss magnitudes at or below this are treated as "no risk" (RoR and EV/risk undefined).
MIN_RISK_DOLLARS: float = 1e-6


# ------------------------------------------------------------------ Leg State (IV + Greeks)
@dataclass(frozen=True)
class LegState:
    """One matched leg: its spec, chain row, effective BSM IV and per-share Greeks.

    Greeks are the row's native values when all four are present, else BSM at `base_iv`.
    """

    spec: StrategyLegInput
    row: PreparedRow
    base_iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


def solve_row_ivs(
    rows: Sequence[PreparedRow], spot: float, risk_free_rate: float
) -> list[float | None]:
    """Solve BSM IV from each row's Mid (`None` where the price cannot be inverted)."""
    if not rows:
        return []
    mids = np.array([row.mid for row in rows], dtype=np.float64)
    spots = np.full(len(rows), spot, dtype=np.float64)
    strikes = np.array([row.strike for row in rows], dtype=np.float64)
    t_years = np.array(
        [max(float(row.dte), MIN_DTE_YEARS_NUMERATOR) / DAYS_PER_YEAR for row in rows],
        dtype=np.float64,
    )
    is_calls = np.array([row.right == "C" for row in rows], dtype=np.bool_)
    solved_iv, solved_mask = solve_implied_volatility(
        mark_price=mids,
        spot=spots,
        strike=strikes,
        t_years=t_years,
        risk_free_rate=risk_free_rate,
        is_call=is_calls,
    )
    return [float(solved_iv[idx]) if bool(solved_mask[idx]) else None for idx in range(len(rows))]


def effective_leg_iv(row: PreparedRow, solved_iv: float | None, fallback_iv: float) -> float:
    """Mid-implied IV, else the row's quoted IV inside `[0.01, 5.0]`, else `fallback_iv`."""
    if solved_iv is not None:
        return solved_iv
    if row.iv is not None and 0.01 <= row.iv <= 5.0:
        return float(row.iv)
    return fallback_iv


def resolve_row_greeks(
    row: PreparedRow, spot: float, risk_free_rate: float, base_iv: float
) -> tuple[float, float, float, float]:
    """Per-share `(delta, gamma, theta, vega)`: native row Greeks or a BSM fallback at `base_iv`."""
    d_val, g_val, th_val, v_val = (row.delta, row.gamma, row.theta, row.vega)
    if d_val is not None and g_val is not None and th_val is not None and v_val is not None:
        return d_val, g_val, th_val, v_val
    t_leg = np.array([max(float(row.dte), MIN_DTE_YEARS_NUMERATOR) / DAYS_PER_YEAR])
    d_arr, g_arr, th_arr, v_arr = bsm_greeks(
        np.array([spot]),
        np.array([row.strike]),
        t_leg,
        risk_free_rate,
        np.array([float(base_iv)]),
        np.array([row.right == "C"], dtype=np.bool_),
    )
    return float(d_arr[0]), float(g_arr[0]), float(th_arr[0]), float(v_arr[0])


def build_leg_states(
    matched: Sequence[tuple[StrategyLegInput, PreparedRow]],
    spot: float,
    risk_free_rate: float,
    fallback_iv: float,
) -> list[LegState]:
    """Resolve effective IVs (one vectorized solve) and Greeks for every matched leg."""
    solved = solve_row_ivs([row for _, row in matched], spot, risk_free_rate)
    states: list[LegState] = []
    for (spec, row), solved_iv in zip(matched, solved, strict=True):
        base_iv = effective_leg_iv(row, solved_iv, fallback_iv)
        d_val, g_val, th_val, v_val = resolve_row_greeks(row, spot, risk_free_rate, base_iv)
        states.append(LegState(spec, row, base_iv, d_val, g_val, th_val, v_val))
    return states


# ------------------------------------------------------------------ Pricing & Greeks
def natural_fill_price(spec: StrategyLegInput, row: PreparedRow) -> float:
    """Natural fill per share: Ask for BUY, Bid for SELL, Mid when that side is missing."""
    if spec.side == "BUY" and row.ask is not None and row.ask > 0.0:
        return float(row.ask)
    if spec.side == "SELL" and row.bid is not None and row.bid > 0.0:
        return float(row.bid)
    return row.mid


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


def strategy_pricing(
    states: Sequence[LegState], qty_multiplier: int
) -> tuple[StrategyPricing, float]:
    """Mid / Natural cash flows (credit > 0) and the unrounded Mid cash total for P&L curves."""
    mid_cash_total = 0.0
    natural_cash_total = 0.0
    for st in states:
        total_qty = st.spec.qty * qty_multiplier
        cash_sign = -1.0 if st.spec.side == "BUY" else 1.0
        natural_fill = natural_fill_price(st.spec, st.row)
        mid_cash_total += cash_sign * float(total_qty) * 100.0 * st.row.mid
        natural_cash_total += cash_sign * float(total_qty) * 100.0 * natural_fill

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
    return pricing, mid_cash_total


def net_position_greeks(states: Sequence[LegState], qty_multiplier: int) -> PositionGreeks:
    """Net position Greeks in share / dollar terms (per-share Greek x signed qty x 100)."""
    net_delta = 0.0
    net_gamma = 0.0
    net_theta = 0.0
    net_vega = 0.0
    for st in states:
        total_qty = st.spec.qty * qty_multiplier
        pos_sign = 1.0 if st.spec.side == "BUY" else -1.0
        net_delta += pos_sign * float(total_qty) * 100.0 * st.delta
        net_gamma += pos_sign * float(total_qty) * 100.0 * st.gamma
        net_theta += pos_sign * float(total_qty) * 100.0 * st.theta
        net_vega += pos_sign * float(total_qty) * 100.0 * st.vega
    return PositionGreeks(
        delta_shares=round(net_delta, 2),
        gamma_shares=round(net_gamma, 2),
        theta_daily_dollars=round(net_theta, 2),
        vega_per_1pct_dollars=round(net_vega, 2),
    )


def evaluated_legs(states: Sequence[LegState], qty_multiplier: int) -> list[EvaluatedLeg]:
    """Leg breakdown (quotes, natural fill, IV, Greeks, OI) for the response payloads."""
    out: list[EvaluatedLeg] = []
    for st in states:
        spec, row = st.spec, st.row
        out.append(
            EvaluatedLeg(
                side=spec.side,
                right=spec.right,
                expiry=row.expiry,
                dte=row.dte,
                strike=row.strike,
                qty=spec.qty * qty_multiplier,
                bid=row.bid,
                ask=row.ask,
                mid=round(row.mid, 4),
                natural_fill=round(natural_fill_price(spec, row), 4),
                spread_pct=round(row.spread_pct, 2) if row.spread_pct is not None else None,
                iv=round(float(row.iv if row.iv is not None else st.base_iv), 4),
                delta=round(st.delta, 4),
                gamma=round(st.gamma, 5),
                theta=round(st.theta, 4),
                vega=round(st.vega, 4),
                open_interest=row.open_interest,
                volume=row.volume,
                option_conid=row.option_conid,
                selection_reason=spec.selection_reason,
            )
        )
    return out


# ------------------------------------------------------------------ Pricing, Curves & POP Engine
def evaluate_pnl_grid(
    price_grid: FloatArray,
    states: Sequence[LegState],
    *,
    entry_cash_total: float,
    days_elapsed: float,
    iv_shift: float,
    qty_multiplier: int,
    risk_free_rate: float,
    at_expiration: bool = False,
) -> FloatArray:
    """Compute vector P&L (in total $) across `price_grid` at `days_elapsed` & `iv_shift`."""
    total_value = np.zeros_like(price_grid, dtype=np.float64)
    for st in states:
        spec, row = st.spec, st.row
        total_qty = spec.qty * qty_multiplier
        pos_sign = 1.0 if spec.side == "BUY" else -1.0
        rem_days = max(0.0, float(row.dte) - days_elapsed)

        # 0-DTE Today (T+0) / Simulated curves preserve intraday BSM time value (0.5d floor);
        # expiration curves (or legs at/past expiry) settle strictly to intrinsic payoff.
        if rem_days <= 1e-9 and (at_expiration or row.dte > 0 or days_elapsed > 1e-9):
            leg_val = (
                np.maximum(0.0, price_grid - row.strike)
                if row.right == "C"
                else np.maximum(0.0, row.strike - price_grid)
            )
        else:
            strike_arr = np.full_like(price_grid, row.strike, dtype=np.float64)
            is_call_arr = np.full(price_grid.shape, row.right == "C", dtype=np.bool_)
            t_rem = np.full_like(price_grid, max(rem_days, MIN_DTE_YEARS_NUMERATOR) / DAYS_PER_YEAR)
            eff_iv = np.full_like(price_grid, max(0.01, min(5.0, float(st.base_iv) + iv_shift)))
            leg_val = bsm_price(price_grid, strike_arr, t_rem, risk_free_rate, eff_iv, is_call_arr)

        total_value += pos_sign * float(total_qty) * 100.0 * leg_val
    return total_value + entry_cash_total


def _extract_breakevens(grid: FloatArray, pnl: FloatArray) -> list[float]:
    """Zero touches / sign changes of `pnl` on `grid`, linearly interpolated, de-duplicated."""
    if len(grid) < 2:
        return []
    y_lo = pnl[:-1]
    y_hi = pnl[1:]
    touches = (np.abs(y_lo) <= 1e-6) & (grid[:-1] > 0.0)
    crosses = ((y_lo < 0.0) & (y_hi > 0.0)) | ((y_lo > 0.0) & (y_hi < 0.0))
    breakevens: list[float] = []
    for raw_idx in np.flatnonzero(touches | crosses):
        i = int(raw_idx)
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


@dataclass(frozen=True)
class PopGrid:
    """Log-normal terminal prices at the short expiry and their normalized weights (read-only)."""

    prices: FloatArray
    weights: FloatArray


def lognormal_pop_grid(
    spot: float, short_dte: int, ref_iv: float, risk_free_rate: float
) -> PopGrid | None:
    """801-point +-5 sigma log-normal grid for POP, or `None` when spot/IV make it degenerate."""
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
    s_grid.setflags(write=False)
    weights.setflags(write=False)
    return PopGrid(prices=s_grid, weights=weights)


def _pop_pct_on_grid(
    pop_grid: PopGrid | None,
    states: Sequence[LegState],
    *,
    short_dte: int,
    entry_cash_total: float,
    qty_multiplier: int,
    risk_free_rate: float,
) -> float | None:
    if pop_grid is None:
        return None
    horizon_pnl = evaluate_pnl_grid(
        pop_grid.prices,
        states,
        entry_cash_total=entry_cash_total,
        days_elapsed=float(short_dte),
        iv_shift=0.0,
        qty_multiplier=qty_multiplier,
        risk_free_rate=risk_free_rate,
        at_expiration=True,
    )
    pop = float(np.sum(pop_grid.weights[horizon_pnl > 0.0])) * 100.0
    return round(max(0.0, min(100.0, pop)), 1)


def compute_expected_return_on_risk_pct(
    prob_of_profit_pct: float | None,
    max_profit: float,
    max_loss: float,
    *,
    max_loss_unbounded: bool,
) -> float | None:
    """EV/risk %: `(POP x max_profit - (1 - POP) x max_loss) / max_loss x 100`, POP as a fraction.

    Sign convention (`StrategyRiskMetrics`): `max_profit` and `max_loss` are non-negative dollar
    magnitudes, so a full loss enters the formula as `+max_loss`; `-100` means "always lose the
    max", `0` is break-even EV. Rounded to 0.1 like `return_on_risk_pct`. Returns `None` when the
    position is `max_loss_unbounded`, POP is missing / non-finite / outside `[0, 100]`,
    `max_profit` is non-finite or negative, or `max_loss` is not a finite number above
    `MIN_RISK_DOLLARS` (the same guard that leaves `return_on_risk_pct` undefined).
    """
    if max_loss_unbounded or prob_of_profit_pct is None:
        return None
    if not (math.isfinite(prob_of_profit_pct) and 0.0 <= prob_of_profit_pct <= 100.0):
        return None
    if not (math.isfinite(max_profit) and max_profit >= 0.0):
        return None
    if not (math.isfinite(max_loss) and max_loss > MIN_RISK_DOLLARS):
        return None
    pop = prob_of_profit_pct / 100.0
    ev_pct = (pop * max_profit - (1.0 - pop) * max_loss) / max_loss * 100.0
    return round(ev_pct, 1)


def compute_nearest_breakeven_distance_pct(
    breakevens: Sequence[float], spot: float
) -> float | None:
    """Distance from spot to the closest break-even, `min |BE - spot| / spot x 100` (0.01 pct).

    Returns `None` when there is no finite break-even or `spot` is not a finite positive price.
    """
    if not (math.isfinite(spot) and spot > 0.0):
        return None
    distances = [abs(be - spot) for be in breakevens if math.isfinite(be)]
    if not distances:
        return None
    return round(min(distances) / spot * 100.0, 2)


def strategy_risk_metrics(
    states: Sequence[LegState],
    *,
    spot: float,
    short_dte: int,
    ref_iv: float,
    mid_cash_total: float,
    qty_multiplier: int,
    risk_free_rate: float,
    pop_grid: PopGrid | None,
) -> StrategyRiskMetrics:
    """Expiry risk bounds, RoR, EV/risk, breakevens, POP and 1-sigma move (no display curves).

    `pop_grid` must come from `lognormal_pop_grid(spot, short_dte, ref_iv, risk_free_rate)`; it is
    a parameter so callers evaluating many leg combinations can build it once.
    """
    # Diagnostic grid covering S=0 up to high multiples for exact risk bounds & breakevens
    leg_strikes = [st.row.strike for st in states]
    diag_lo = 0.01
    diag_hi = max(spot, max(leg_strikes)) * 3.5
    diag_uniform = np.linspace(diag_lo, diag_hi, 320, dtype=np.float64)
    diag_grid = np.unique(
        np.round(np.concatenate([diag_uniform, np.array([spot, *leg_strikes])]), 4)
    )
    diag_expiry_pnl = evaluate_pnl_grid(
        diag_grid,
        states,
        entry_cash_total=mid_cash_total,
        days_elapsed=float(short_dte),
        iv_shift=0.0,
        qty_multiplier=qty_multiplier,
        risk_free_rate=risk_free_rate,
        at_expiration=True,
    )

    net_upside_call_qty = sum(
        (1 if st.spec.side == "BUY" else -1) * st.spec.qty for st in states if st.spec.right == "C"
    )
    max_loss_unbounded = net_upside_call_qty < 0
    raw_min = float(np.min(diag_expiry_pnl))
    raw_max = float(np.max(diag_expiry_pnl))
    max_loss = round(max(0.0, -raw_min), 2)
    max_profit = round(max(0.0, raw_max), 2)
    ror_pct = (
        round((max_profit / max_loss) * 100.0, 1)
        if (not max_loss_unbounded and max_loss > MIN_RISK_DOLLARS)
        else None
    )

    breakevens = _extract_breakevens(diag_grid, diag_expiry_pnl)
    pop_pct = _pop_pct_on_grid(
        pop_grid,
        states,
        short_dte=short_dte,
        entry_cash_total=mid_cash_total,
        qty_multiplier=qty_multiplier,
        risk_free_rate=risk_free_rate,
    )
    em_pct = ref_iv * math.sqrt(max(float(short_dte), MIN_DTE_YEARS_NUMERATOR) / DAYS_PER_YEAR)
    em_dollars = spot * em_pct

    return StrategyRiskMetrics(
        max_profit=max_profit,
        max_loss=max_loss,
        max_loss_unbounded=max_loss_unbounded,
        return_on_risk_pct=ror_pct,
        breakevens=breakevens,
        prob_of_profit_pct=pop_pct,
        expected_move_1sigma_pct=round(em_pct * 100.0, 2),
        expected_move_1sigma_low=round(max(0.01, spot - em_dollars), 2),
        expected_move_1sigma_high=round(spot + em_dollars, 2),
        expected_return_on_risk_pct=compute_expected_return_on_risk_pct(
            pop_pct, max_profit, max_loss, max_loss_unbounded=max_loss_unbounded
        ),
        nearest_breakeven_distance_pct=compute_nearest_breakeven_distance_pct(breakevens, spot),
    )


def _build_display_grid(spot: float, leg_strikes: list[float]) -> FloatArray:
    low_k = min(leg_strikes)
    high_k = max(leg_strikes)
    lo = max(0.01, min(spot * 0.72, low_k * 0.85))
    hi = max(spot * 1.28, high_k * 1.15)
    uniform = np.linspace(lo, hi, 121, dtype=np.float64)
    knots = np.array([spot, *leg_strikes], dtype=np.float64)
    merged = np.unique(np.round(np.concatenate([uniform, knots]), 4))
    return merged


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
    min_net_credit_dollars: float | None = None,
    min_ror_pct: float | None = None,
    entry_price_basis: EntryPriceBasis = "MID",
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
            min_net_credit_dollars=min_net_credit_dollars,
            min_ror_pct=min_ror_pct,
            entry_price_basis=entry_price_basis,
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

    # Near-ATM IV of the short expiry: IV-solver fallback, POP vol and 1-sigma move.
    ref_iv = chain_reference_iv(rows, spot, short_expiry)
    states = build_leg_states(matched, spot, risk_free_rate, ref_iv)
    pricing, mid_cash_total = strategy_pricing(states, qty_multiplier)
    risk = strategy_risk_metrics(
        states,
        spot=spot,
        short_dte=short_dte,
        ref_iv=ref_iv,
        mid_cash_total=mid_cash_total,
        qty_multiplier=qty_multiplier,
        risk_free_rate=risk_free_rate,
        pop_grid=lognormal_pop_grid(spot, short_dte, ref_iv, risk_free_rate),
    )

    # Interactive Plotly curves: Today (T+0), 50% Decay, Short Expiry (+ custom Sim if active)
    display_grid = _build_display_grid(spot, [row.strike for _, row in matched])
    half_days = max(0, short_dte // 2)
    curve_specs: list[tuple[str, int, float, bool]] = [
        ("Today (T+0)", 0, 0.0, False),
    ]
    if 0 < half_days < short_dte:
        curve_specs.append((f"Midpoint (T+{half_days}d)", half_days, 0.0, False))
    expiry_label = (
        f"At Short Expiry ({short_dte}d)"
        if short_dte != long_dte
        else f"At Expiration ({short_dte}d)"
    )
    curve_specs.append((expiry_label, short_dte, 0.0, True))

    if eff_days_forward != 0 or abs(iv_shift) > 1e-6:
        iv_tag = f" IV{iv_shift:+.0%}" if abs(iv_shift) > 1e-6 else ""
        sim_label = f"Simulated (T+{eff_days_forward}d{iv_tag})"
        curve_specs.append((sim_label, eff_days_forward, iv_shift, False))

    curves = [
        StrategyPayoffSeries(
            label=label,
            days_elapsed=d_el,
            iv_shift=sh,
            pnl_values=[
                round(float(v), 2)
                for v in evaluate_pnl_grid(
                    display_grid,
                    states,
                    entry_cash_total=mid_cash_total,
                    days_elapsed=float(d_el),
                    iv_shift=sh,
                    qty_multiplier=qty_multiplier,
                    risk_free_rate=risk_free_rate,
                    at_expiration=is_exp,
                )
            ],
        )
        for label, d_el, sh, is_exp in curve_specs
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
        legs=evaluated_legs(states, qty_multiplier),
        pricing=pricing,
        risk=risk,
        net_greeks=net_position_greeks(states, qty_multiplier),
        price_grid=[round(float(p), 2) for p in display_grid],
        curves=curves,
        rationale=rationale,
    )
