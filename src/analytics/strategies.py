"""Multi-leg options strategy pricing, Wall-anchored auto-construction, and BSM T+n simulation."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt
import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from src.analytics.bsm_fallback import (
    DAYS_PER_YEAR,
    DEFAULT_RISK_FREE_RATE,
    MIN_DTE_YEARS_NUMERATOR,
    bsm_greeks,
    bsm_price,
    solve_implied_volatility,
)
from src.analytics.walls import ChainWallsReport, DTEWallSummary, compute_walls
from src.storage.schema import OptionRight

FloatArray = npt.NDArray[np.float64]

StrategyKind = Literal[
    "BULL_PUT_SPREAD",
    "BULL_CALL_SPREAD",
    "DIAGONAL_SPREAD",
    "IRON_CONDOR",
    "CUSTOM",
]
PresetStrategyKind = Literal[
    "BULL_PUT_SPREAD",
    "BULL_CALL_SPREAD",
    "DIAGONAL_SPREAD",
    "IRON_CONDOR",
]
LegSide = Literal["BUY", "SELL"]

PRESET_STRATEGIES: tuple[PresetStrategyKind, ...] = (
    "BULL_PUT_SPREAD",
    "BULL_CALL_SPREAD",
    "DIAGONAL_SPREAD",
    "IRON_CONDOR",
)


class StrategyLegInput(BaseModel):
    """User- or builder-supplied specification for one option leg."""

    model_config = ConfigDict(extra="forbid")

    side: LegSide
    right: OptionRight
    expiry: str
    strike: float = Field(gt=0.0)
    qty: int = Field(default=1, ge=1, le=100)
    selection_reason: str | None = None


class EvaluatedLeg(BaseModel):
    """Option leg enriched with orderbook quotes, Greeks, and selection rationale."""

    model_config = ConfigDict(extra="forbid")

    side: LegSide
    right: OptionRight
    expiry: str
    dte: int
    strike: float
    qty: int
    bid: float | None
    ask: float | None
    mid: float
    natural_fill: float
    spread_pct: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    open_interest: int | None
    volume: int | None
    option_conid: int | None
    selection_reason: str | None = None


class StrategyPricing(BaseModel):
    """Execution cost/credit breakdown across Mid and Natural market fills."""

    model_config = ConfigDict(extra="forbid")

    is_credit: bool
    net_mid_per_share: float
    net_natural_per_share: float
    mid_cash_total: float
    natural_cash_total: float
    slippage_dollars: float
    slippage_pct: float | None
    limit_ladder: list[float]


class StrategyRiskMetrics(BaseModel):
    """Risk/reward bounds, breakevens, POP, and 1-sigma expected move envelope."""

    model_config = ConfigDict(extra="forbid")

    max_profit: float
    max_loss: float
    max_loss_unbounded: bool
    return_on_risk_pct: float | None
    breakevens: list[float]
    prob_of_profit_pct: float | None
    expected_move_1sigma_pct: float | None
    expected_move_1sigma_low: float | None
    expected_move_1sigma_high: float | None


class PositionGreeks(BaseModel):
    """Net position Greeks across all legs (dollar/share equivalent, qty x 100)."""

    model_config = ConfigDict(extra="forbid")

    delta_shares: float
    gamma_shares: float
    theta_daily_dollars: float
    vega_per_1pct_dollars: float


class StrategyPayoffSeries(BaseModel):
    """One P&L curve over `price_grid` at a specific elapsed-day / IV state."""

    model_config = ConfigDict(extra="forbid")

    label: str
    days_elapsed: int
    iv_shift: float
    pnl_values: list[float]


class StrategyEvaluationResponse(BaseModel):
    """Complete strategy valuation, risk envelope, and multi-horizon Plotly curves."""

    model_config = ConfigDict(extra="forbid")

    underlying: str
    underlying_price: float
    strategy_kind: StrategyKind
    short_expiry: str
    short_dte: int
    long_expiry: str
    long_dte: int
    days_forward: int
    iv_shift: float
    qty_multiplier: int
    risk_free_rate: float
    primary_call_wall: float | None
    primary_put_wall: float | None
    legs: list[EvaluatedLeg]
    pricing: StrategyPricing
    risk: StrategyRiskMetrics
    net_greeks: PositionGreeks
    price_grid: list[float]
    curves: list[StrategyPayoffSeries]
    rationale: list[str]


@dataclass(frozen=True)
class _PreparedRow:
    expiry: str
    dte: int
    strike: float
    right: OptionRight
    bid: float | None
    ask: float | None
    mid: float
    spread_pct: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    open_interest: int | None
    volume: int | None
    option_conid: int | None


# ------------------------------------------------------------------ Row & Quote Helpers
def _opt_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        out = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _opt_int(val: object) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, (float, str)):
        try:
            return int(val)
        except ValueError:
            return None
    return None


def _resolve_row_mid(raw: dict[str, object]) -> float:
    bid = _opt_float(raw.get("bid"))
    ask = _opt_float(raw.get("ask"))
    if bid is not None and ask is not None and bid > 0.0 and ask >= bid:
        return (bid + ask) / 2.0
    for key in ("mid", "mark_price", "last", "close"):
        val = _opt_float(raw.get(key))
        if val is not None and val > 0.0:
            return val
    return 0.0


def _prepare_rows(df: pl.DataFrame) -> tuple[str, float, list[_PreparedRow]]:
    if df.height == 0:
        raise ValueError("Cannot evaluate option strategy on an empty chain DataFrame")
    first = df.row(0, named=True)
    underlying = str(first["underlying"])
    spot = float(first["underlying_price"])
    if spot <= 0.0:
        raise ValueError(f"Invalid underlying_price {spot} for {underlying}")

    rows: list[_PreparedRow] = []
    for raw in df.iter_rows(named=True):
        right_str = str(raw["right"]).upper()
        if right_str not in ("C", "P"):
            continue
        right_lit: OptionRight = "C" if right_str == "C" else "P"
        bid = _opt_float(raw.get("bid"))
        ask = _opt_float(raw.get("ask"))
        mid = _resolve_row_mid(raw)
        spread_pct: float | None = None
        if bid is not None and ask is not None and bid > 0.0 and ask >= bid and mid > 0.0:
            spread_pct = ((ask - bid) / mid) * 100.0
        rows.append(
            _PreparedRow(
                expiry=str(raw["expiry"]),
                dte=int(raw["dte"]),
                strike=float(raw["strike"]),
                right=right_lit,
                bid=bid,
                ask=ask,
                mid=mid,
                spread_pct=spread_pct,
                iv=_opt_float(raw.get("iv")),
                delta=_opt_float(raw.get("delta")),
                gamma=_opt_float(raw.get("gamma")),
                theta=_opt_float(raw.get("theta")),
                vega=_opt_float(raw.get("vega")),
                open_interest=_opt_int(raw.get("open_interest")),
                volume=_opt_int(raw.get("volume")),
                option_conid=_opt_int(raw.get("option_conid")),
            )
        )
    return underlying, spot, rows


def _row_lookup_map(rows: list[_PreparedRow]) -> dict[tuple[str, float, OptionRight], _PreparedRow]:
    mapping: dict[tuple[str, float, OptionRight], _PreparedRow] = {}
    for r in rows:
        mapping[(r.expiry, round(r.strike, 6), r.right)] = r
    return mapping


def _find_row(
    lookup: dict[tuple[str, float, OptionRight], _PreparedRow],
    expiry: str,
    strike: float,
    right: OptionRight,
) -> _PreparedRow:
    exact = lookup.get((expiry, round(strike, 6), right))
    if exact is not None:
        return exact
    for (exp_k, k_val, r_val), row in lookup.items():
        if exp_k == expiry and r_val == right and abs(k_val - strike) < 1e-4:
            return row
    raise ValueError(f"Contract not found in loaded snapshot: {expiry} {strike:g}{right}")


def _chain_reference_iv(rows: list[_PreparedRow], spot: float, target_expiry: str) -> float:
    same_exp = [
        r for r in rows if r.expiry == target_expiry and r.iv is not None and 0.01 <= r.iv <= 5.0
    ]
    pool = same_exp or [r for r in rows if r.iv is not None and 0.01 <= r.iv <= 5.0]
    if not pool:
        return 0.35
    near_atm = sorted(pool, key=lambda r: (abs(r.strike - spot), r.spread_pct or 999.0))
    return float(near_atm[0].iv or 0.35)


# ------------------------------------------------------------------ Wall-Anchored Auto-Builder
def _liquidity_score(row: _PreparedRow) -> float:
    if row.mid <= 0.01:
        return -100.0
    spread_pen = min((row.spread_pct if row.spread_pct is not None else 25.0), 100.0)
    spread_bonus = max(0.0, 35.0 - spread_pen)
    activity = math.log1p(max(row.open_interest or 0, 0) + max(row.volume or 0, 0)) * 4.5
    return spread_bonus + activity


def _pick_target_expiry(
    rows: list[_PreparedRow],
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


def _find_wall_for_expiry(
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
    candidates: list[_PreparedRow],
    score_fn: Callable[[_PreparedRow], float],
) -> _PreparedRow:
    if not candidates:
        raise ValueError("No eligible option contracts found in snapshot for strategy preset")
    return max(candidates, key=score_fn)


def _pick_wing_strike(
    pool: list[_PreparedRow],
    anchor_strike: float,
    *,
    direction: Literal["BELOW", "ABOVE"],
    spot: float,
) -> _PreparedRow:
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
            -_liquidity_score(r),
        ),
    )


def _select_bull_put_spread(
    exp_puts: list[_PreparedRow],
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

    def _short_put_score(r: _PreparedRow) -> float:
        delta_err = abs(abs(r.delta or 0.28) - 0.28) * 30.0
        wall_bonus = 22.0 if (put_wall is not None and abs(r.strike - put_wall) < 1e-4) else 0.0
        dist_pen = (abs(r.strike - anchor_ref) / max(spot, 1.0)) * 80.0
        return _liquidity_score(r) + wall_bonus - delta_err - dist_pen

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
    exp_calls: list[_PreparedRow],
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

    def _long_call_score(r: _PreparedRow) -> float:
        delta_err = abs((r.delta or 0.55) - 0.55) * 35.0
        atm_pen = (abs(r.strike - spot) / max(spot, 1.0)) * 120.0
        return _liquidity_score(r) - delta_err - atm_pen

    long_call = _best_candidate(pool_long, _long_call_score)

    higher_calls = [r for r in exp_calls if r.strike > long_call.strike + 1e-6]
    if not higher_calls:
        raise ValueError("Insufficient higher Call strikes above ATM for Bull Call Spread")

    target_cap = (
        call_wall
        if (call_wall is not None and call_wall > long_call.strike + 1e-6)
        else spot * 1.08
    )

    def _short_call_score(r: _PreparedRow) -> float:
        wall_bonus = 24.0 if (call_wall is not None and abs(r.strike - call_wall) < 1e-4) else 0.0
        delta_err = abs((r.delta or 0.30) - 0.28) * 25.0
        dist_pen = (abs(r.strike - target_cap) / max(spot, 1.0)) * 90.0
        return _liquidity_score(r) + wall_bonus - delta_err - dist_pen

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
    exp_puts: list[_PreparedRow],
    exp_calls: list[_PreparedRow],
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
            _liquidity_score(r)
            + (22.0 if (put_wall is not None and abs(r.strike - put_wall) < 1e-4) else 0.0)
            - abs(abs(r.delta or 0.20) - 0.20) * 35.0
            - (abs(r.strike - put_anchor) / max(spot, 1.0)) * 75.0
        ),
    )
    long_put = _pick_wing_strike(exp_puts, short_put.strike, direction="BELOW", spot=spot)

    short_call = _best_candidate(
        call_inner,
        lambda r: (
            _liquidity_score(r)
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
    rows: list[_PreparedRow],
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
            _liquidity_score(r)
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
            _liquidity_score(r)
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
    _, spot, rows = _prepare_rows(df)
    walls = walls_report or compute_walls(df)
    short_exp, short_dte = _pick_target_expiry(rows, target_expiry)
    _, call_wall, put_wall = _find_wall_for_expiry(walls, short_exp)

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


# ------------------------------------------------------------------ Pricing, Curves & POP Engine
def _solve_effective_ivs(
    matched: list[tuple[StrategyLegInput, _PreparedRow]],
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
    matched: list[tuple[StrategyLegInput, _PreparedRow]],
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
    matched: list[tuple[StrategyLegInput, _PreparedRow]],
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
    underlying, spot, rows = _prepare_rows(df)
    walls = walls_report or compute_walls(df)
    lookup = _row_lookup_map(rows)

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
        _, call_wall, put_wall = _find_wall_for_expiry(walls, ref_exp)
        rationale.append(f"Evaluating {len(legs)} user-selected option leg(s).")

    matched: list[tuple[StrategyLegInput, _PreparedRow]] = [
        (spec, _find_row(lookup, spec.expiry, spec.strike, spec.right)) for spec in chosen_legs
    ]
    short_dte = min(row.dte for _, row in matched)
    long_dte = max(row.dte for _, row in matched)
    short_expiry = min(matched, key=lambda pair: pair[1].dte)[1].expiry
    long_expiry = max(matched, key=lambda pair: pair[1].dte)[1].expiry
    eff_days_forward = max(0, min(days_forward, short_dte))

    fallback_iv = _chain_reference_iv(rows, spot, short_expiry)
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
    atm_iv = _chain_reference_iv(rows, spot, short_expiry)
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
