"""Wall-anchored auto-construction of preset strategies (bull put/call, condor, diagonal)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import polars as pl

from src.analytics.strategies.chain import PreparedRow, liquidity_score, prepare_rows
from src.analytics.strategies.models import PresetStrategyKind, StrategyLegInput
from src.analytics.walls import ChainWallsReport, DTEWallSummary, compute_walls


def pick_target_expiry(
    rows: list[PreparedRow],
    preferred_expiry: str | None,
) -> tuple[str, int]:
    exp_dte: dict[str, int] = {}
    for r in rows:
        if r.expiry not in exp_dte:
            exp_dte[r.expiry] = r.dte
    if preferred_expiry and preferred_expiry in exp_dte:
        return preferred_expiry, exp_dte[preferred_expiry]
    best_exp, best_dte = min(
        exp_dte.items(),
        key=lambda item: (abs(item[1] - 38), item[1], item[0]),
    )
    return best_exp, best_dte


def find_wall_for_expiry(
    walls: ChainWallsReport, expiry: str
) -> tuple[DTEWallSummary | None, float | None, float | None]:
    dte_summary = next((w for w in walls.by_dte if w.expiry == expiry), None)
    call_wall = (
        dte_summary.primary_call_wall.strike
        if dte_summary and dte_summary.primary_call_wall
        else (
            walls.aggregate_primary_call_wall.strike if walls.aggregate_primary_call_wall else None
        )
    )
    put_wall = (
        dte_summary.primary_put_wall.strike
        if dte_summary and dte_summary.primary_put_wall
        else (walls.aggregate_primary_put_wall.strike if walls.aggregate_primary_put_wall else None)
    )
    return dte_summary, call_wall, put_wall


def _best_candidate(
    candidates: list[PreparedRow],
    score_fn: Callable[[PreparedRow], float],
) -> PreparedRow:
    if not candidates:
        raise ValueError("No eligible option contracts found in snapshot for strategy preset")
    return max(candidates, key=score_fn)


def _pick_wing_strike(
    pool: list[PreparedRow],
    anchor_strike: float,
    *,
    direction: Literal["BELOW", "ABOVE"],
    spot: float,
) -> PreparedRow:
    ordered = sorted(
        [
            r
            for r in pool
            if (
                r.strike < anchor_strike - 1e-6
                if direction == "BELOW"
                else r.strike > anchor_strike + 1e-6
            )
        ],
        key=lambda r: abs(r.strike - anchor_strike),
    )
    if not ordered:
        raise ValueError(f"Insufficient wing strikes {direction.lower()} ${anchor_strike:g}")
    desired_width = max(spot * 0.025, abs(ordered[0].strike - anchor_strike))
    return min(
        ordered[:4],
        key=lambda r: (
            abs(abs(r.strike - anchor_strike) - desired_width),
            -liquidity_score(r),
        ),
    )


def _select_bull_put_spread(
    exp_puts: list[PreparedRow],
    spot: float,
    put_wall: float | None,
) -> tuple[list[StrategyLegInput], list[str]]:
    otm_puts = [r for r in exp_puts if r.strike <= spot * 1.005 and r.mid > 0.01]
    pool = otm_puts or [r for r in exp_puts if r.mid > 0.01] or exp_puts
    # Keep at least 1 lower strike for the protective wing if available
    sorted_strikes = sorted({r.strike for r in exp_puts})
    if len(sorted_strikes) >= 2:
        min_k = sorted_strikes[0]
        above_floor = [r for r in pool if r.strike > min_k + 1e-6]
        if above_floor:
            pool = above_floor

    anchor_ref = put_wall if (put_wall is not None and put_wall <= spot * 1.02) else spot * 0.95

    def _short_put_score(r: PreparedRow) -> float:
        delta_err = abs(abs(r.delta or 0.28) - 0.28) * 30.0
        wall_bonus = 22.0 if (put_wall is not None and abs(r.strike - put_wall) < 1e-4) else 0.0
        dist_pen = (abs(r.strike - anchor_ref) / max(spot, 1.0)) * 80.0
        return liquidity_score(r) + wall_bonus - delta_err - dist_pen

    short_put = _best_candidate(pool, _short_put_score)
    long_put = _pick_wing_strike(exp_puts, short_put.strike, direction="BELOW", spot=spot)

    wall_note = f"Put Wall ${put_wall:g}" if put_wall is not None else "OTM support"
    rationale = [
        (
            f"Short Put ${short_put.strike:g} anchored to {wall_note} "
            f"(Delta={short_put.delta:.2f}, Mid=${short_put.mid:.2f})."
            if short_put.delta is not None
            else f"Short Put ${short_put.strike:g} near {wall_note} (Mid=${short_put.mid:.2f})."
        ),
        (
            f"Long Put wing ${long_put.strike:g} caps risk across "
            f"${short_put.strike - long_put.strike:g} spread width."
        ),
    ]
    legs = [
        StrategyLegInput(
            side="SELL",
            right="P",
            expiry=short_put.expiry,
            strike=short_put.strike,
            qty=1,
            selection_reason=f"Short Put near {wall_note}",
        ),
        StrategyLegInput(
            side="BUY",
            right="P",
            expiry=long_put.expiry,
            strike=long_put.strike,
            qty=1,
            selection_reason="Protective Put wing",
        ),
    ]
    return legs, rationale


def _select_bull_call_spread(
    exp_calls: list[PreparedRow],
    spot: float,
    call_wall: float | None,
) -> tuple[list[StrategyLegInput], list[str]]:
    sorted_strikes = sorted({r.strike for r in exp_calls})
    pool_long = exp_calls
    if len(sorted_strikes) >= 2:
        max_k = sorted_strikes[-1]
        below_cap = [r for r in exp_calls if r.strike < max_k - 1e-6 and r.mid > 0.01]
        if below_cap:
            pool_long = below_cap

    def _long_call_score(r: PreparedRow) -> float:
        delta_err = abs((r.delta or 0.55) - 0.55) * 35.0
        atm_pen = (abs(r.strike - spot) / max(spot, 1.0)) * 120.0
        return liquidity_score(r) - delta_err - atm_pen

    long_call = _best_candidate(pool_long, _long_call_score)

    higher_calls = [r for r in exp_calls if r.strike > long_call.strike + 1e-6]
    if not higher_calls:
        raise ValueError("Insufficient higher Call strikes above ATM for Bull Call Spread")

    target_cap = (
        call_wall
        if (call_wall is not None and call_wall > long_call.strike + 1e-6)
        else spot * 1.08
    )

    def _short_call_score(r: PreparedRow) -> float:
        wall_bonus = 24.0 if (call_wall is not None and abs(r.strike - call_wall) < 1e-4) else 0.0
        delta_err = abs((r.delta or 0.30) - 0.28) * 25.0
        dist_pen = (abs(r.strike - target_cap) / max(spot, 1.0)) * 90.0
        return liquidity_score(r) + wall_bonus - delta_err - dist_pen

    short_call = _best_candidate(higher_calls, _short_call_score)
    wall_note = f"Call Wall ${call_wall:g}" if call_wall is not None else "upside OTM cap"
    rationale = [
        f"Long Call ${long_call.strike:g} selected near ATM for strong directional Delta leverage.",
        f"Short Call ${short_call.strike:g} caps upside right at {wall_note} to lower net debit.",
    ]
    legs = [
        StrategyLegInput(
            side="BUY",
            right="C",
            expiry=long_call.expiry,
            strike=long_call.strike,
            qty=1,
            selection_reason="ATM/near-ATM Long Call",
        ),
        StrategyLegInput(
            side="SELL",
            right="C",
            expiry=short_call.expiry,
            strike=short_call.strike,
            qty=1,
            selection_reason=f"Short Call cap at {wall_note}",
        ),
    ]
    return legs, rationale


def _select_iron_condor(
    exp_puts: list[PreparedRow],
    exp_calls: list[PreparedRow],
    spot: float,
    put_wall: float | None,
    call_wall: float | None,
) -> tuple[list[StrategyLegInput], list[str]]:
    put_pool = [r for r in exp_puts if r.strike < spot - 1e-6 and r.mid > 0.01] or exp_puts
    call_pool = [r for r in exp_calls if r.strike > spot + 1e-6 and r.mid > 0.01] or exp_calls

    min_put_k = min(r.strike for r in exp_puts)
    max_call_k = max(r.strike for r in exp_calls)
    put_inner = [r for r in put_pool if r.strike > min_put_k + 1e-6] or put_pool
    call_inner = [r for r in call_pool if r.strike < max_call_k - 1e-6] or call_pool

    put_anchor = put_wall if (put_wall is not None and put_wall < spot) else spot * 0.93
    call_anchor = call_wall if (call_wall is not None and call_wall > spot) else spot * 1.07

    short_put = _best_candidate(
        put_inner,
        lambda r: (
            liquidity_score(r)
            + (22.0 if (put_wall is not None and abs(r.strike - put_wall) < 1e-4) else 0.0)
            - abs(abs(r.delta or 0.20) - 0.20) * 35.0
            - (abs(r.strike - put_anchor) / max(spot, 1.0)) * 75.0
        ),
    )
    long_put = _pick_wing_strike(exp_puts, short_put.strike, direction="BELOW", spot=spot)

    short_call = _best_candidate(
        call_inner,
        lambda r: (
            liquidity_score(r)
            + (22.0 if (call_wall is not None and abs(r.strike - call_wall) < 1e-4) else 0.0)
            - abs((r.delta or 0.20) - 0.20) * 35.0
            - (abs(r.strike - call_anchor) / max(spot, 1.0)) * 75.0
        ),
    )
    long_call = _pick_wing_strike(exp_calls, short_call.strike, direction="ABOVE", spot=spot)

    rationale = [
        f"Put wing anchored near Put Wall (${short_put.strike:g} / ${long_put.strike:g}).",
        (
            f"Call wing anchored near Call Wall (${short_call.strike:g} / "
            f"${long_call.strike:g}) for neutral Theta decay."
        ),
    ]
    legs = [
        StrategyLegInput(
            side="BUY",
            right="P",
            expiry=long_put.expiry,
            strike=long_put.strike,
            qty=1,
            selection_reason="Put protective wing",
        ),
        StrategyLegInput(
            side="SELL",
            right="P",
            expiry=short_put.expiry,
            strike=short_put.strike,
            qty=1,
            selection_reason="Short Put near Put Wall",
        ),
        StrategyLegInput(
            side="SELL",
            right="C",
            expiry=short_call.expiry,
            strike=short_call.strike,
            qty=1,
            selection_reason="Short Call near Call Wall",
        ),
        StrategyLegInput(
            side="BUY",
            right="C",
            expiry=long_call.expiry,
            strike=long_call.strike,
            qty=1,
            selection_reason="Call protective wing",
        ),
    ]
    return legs, rationale


def _select_diagonal_spread(
    rows: list[PreparedRow],
    short_exp: str,
    short_dte: int,
    spot: float,
    call_wall: float | None,
) -> tuple[list[StrategyLegInput], list[str]]:
    calls = [r for r in rows if r.right == "C"]
    if not calls:
        raise ValueError("No Call contracts available for Diagonal Spread")

    later_exps = sorted(
        {(r.expiry, r.dte) for r in calls if r.dte > short_dte},
        key=lambda item: item[1],
    )
    if later_exps:
        back_exp, back_dte = later_exps[-1]
        multi_note: str | None = None
    else:
        back_exp, back_dte = short_exp, short_dte
        multi_note = (
            "Snapshot contains a single expiration; Diagonal preset fell back to same-DTE "
            "ITM/OTM Call spread."
        )

    front_calls = [r for r in calls if r.expiry == short_exp and r.mid > 0.01]
    back_calls = [r for r in calls if r.expiry == back_exp and r.mid > 0.01]
    if not front_calls or not back_calls:
        front_calls = [r for r in calls if r.expiry == short_exp]
        back_calls = [r for r in calls if r.expiry == back_exp]

    # Pick back-month ITM anchor (target Delta ~0.75, strike <= spot)
    itm_back = [r for r in back_calls if r.strike <= spot * 0.99] or back_calls
    long_call = _best_candidate(
        itm_back,
        lambda r: (
            liquidity_score(r)
            - abs((r.delta or 0.75) - 0.75) * 45.0
            - (0.0 if r.strike < spot else 50.0)
        ),
    )

    # Pick front-month OTM Call near Call Wall strictly above spot & long strike
    otm_front = (
        [r for r in front_calls if r.strike > max(long_call.strike, spot * 0.99) + 1e-6]
        or [r for r in front_calls if r.strike > long_call.strike + 1e-6]
        or front_calls
    )

    call_target = (
        call_wall
        if (call_wall is not None and call_wall > long_call.strike + 1e-6)
        else spot * 1.06
    )
    short_call = _best_candidate(
        otm_front,
        lambda r: (
            liquidity_score(r)
            + (22.0 if (call_wall is not None and abs(r.strike - call_wall) < 1e-4) else 0.0)
            - abs((r.delta or 0.28) - 0.28) * 30.0
            - (abs(r.strike - call_target) / max(spot, 1.0)) * 75.0
        ),
    )

    rationale = [
        f"Long back-month Call ({back_dte}d ${long_call.strike:g}, "
        f"Delta={(long_call.delta or 0.75):.2f}) acts as stock replacement.",
        f"Short front-month Call ({short_dte}d ${short_call.strike:g}) harvests faster Theta "
        f"at {'Call Wall $' + format(call_wall, 'g') if call_wall is not None else 'OTM target'}.",
    ]
    if multi_note is not None:
        rationale.append(multi_note)

    legs = [
        StrategyLegInput(
            side="BUY",
            right="C",
            expiry=long_call.expiry,
            strike=long_call.strike,
            qty=1,
            selection_reason=f"Back-month LEAPS/ITM Call ({back_dte}d)",
        ),
        StrategyLegInput(
            side="SELL",
            right="C",
            expiry=short_call.expiry,
            strike=short_call.strike,
            qty=1,
            selection_reason=f"Front-month Short Call ({short_dte}d)",
        ),
    ]
    return legs, rationale


def suggest_strategy_legs_from_walls(
    df: pl.DataFrame,
    strategy_kind: PresetStrategyKind,
    *,
    target_expiry: str | None = None,
    walls_report: ChainWallsReport | None = None,
) -> tuple[list[StrategyLegInput], list[str], float | None, float | None]:
    """Automatically choose Wall- and liquidity-optimal option legs from a chain DataFrame."""
    _, spot, rows = prepare_rows(df)
    walls = walls_report or compute_walls(df)
    short_exp, short_dte = pick_target_expiry(rows, target_expiry)
    _, call_wall, put_wall = find_wall_for_expiry(walls, short_exp)

    exp_puts = sorted(
        [r for r in rows if r.expiry == short_exp and r.right == "P"], key=lambda r: r.strike
    )
    exp_calls = sorted(
        [r for r in rows if r.expiry == short_exp and r.right == "C"], key=lambda r: r.strike
    )

    if strategy_kind == "BULL_PUT_SPREAD":
        legs, notes = _select_bull_put_spread(exp_puts, spot, put_wall)
    elif strategy_kind == "BULL_CALL_SPREAD":
        legs, notes = _select_bull_call_spread(exp_calls, spot, call_wall)
    elif strategy_kind == "IRON_CONDOR":
        legs, notes = _select_iron_condor(exp_puts, exp_calls, spot, put_wall, call_wall)
    elif strategy_kind == "DIAGONAL_SPREAD":
        legs, notes = _select_diagonal_spread(rows, short_exp, short_dte, spot, call_wall)
    else:
        raise ValueError(f"Unsupported strategy preset: {strategy_kind}")
    return legs, notes, call_wall, put_wall
