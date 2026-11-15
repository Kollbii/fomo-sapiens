"""Live re-pick of preset strategy legs inside a fixed quote window (pure, metrics-only).

Each second the live screener overlays live quotes on the snapshot rows of its subscribed
contracts and asks `repick_strategy_legs` whether a neighbouring strike combination now beats a
row's current legs on RoR. Every candidate is priced with the exact Strategy Lab engine math
(`build_leg_states` / `strategy_pricing` / `strategy_risk_metrics`) but without walls, payoff
curves or display grids, so a full Iron Condor window (3^4 = 81 combinations) stays cheap.

Rules:
- Each leg ranges over its window: the strikes given in `window_strikes[i]` (the subscribed
  window, fixed for the live session) or, by default, its current strike and the nearest lower /
  upper strike of the same expiry and right in `quotes_df`. Only strikes present in the frame
  count, and side / right / expiry / qty of every leg never change.
- Candidates must keep the preset geometry (verticals: positive width on the preset's side;
  Iron Condor: put wing < short put < short call < call wing; Diagonal: short strike above the
  long strike, each leg on its own expiry) and carry a positive Mid on every leg.
- The best candidate (highest `return_on_risk_pct`; ties prefer fewer moved legs, then higher
  POP, then the first in strike order) replaces the current legs only if its RoR is strictly
  higher and it passes `filters`. Ties, filter rejections, or an undefined current RoR keep the
  current legs.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import polars as pl

from src.analytics.bsm_fallback import DEFAULT_RISK_FREE_RATE
from src.analytics.strategies.chain import (
    PreparedRow,
    chain_reference_iv,
    find_row,
    prepare_rows,
    row_lookup_map,
)
from src.analytics.strategies.engine import (
    LegState,
    PopGrid,
    effective_leg_iv,
    evaluated_legs,
    lognormal_pop_grid,
    net_position_greeks,
    resolve_row_greeks,
    solve_row_ivs,
    strategy_pricing,
    strategy_risk_metrics,
)
from src.analytics.strategies.models import (
    PRESET_STRATEGIES,
    LegSide,
    PresetStrategyKind,
    RepickKeepReason,
    StrategyCandidateFilters,
    StrategyLegInput,
    StrategyPricing,
    StrategyRepickResult,
    StrategyRiskMetrics,
)
from src.storage.schema import OptionRight

RowKey = tuple[str, float, OptionRight]

_FILTER_EPS = 1e-9
_STRIKE_TOL = 1e-4  # same tolerance as `chain.find_row`
_ORDER_EPS = 1e-6  # same strict-ordering tolerance as the builders' wing selection

# One leg per (side, right) role; every preset except the Diagonal also shares one expiry.
_PRESET_LEG_ROLES: dict[PresetStrategyKind, tuple[tuple[LegSide, OptionRight], ...]] = {
    "BULL_PUT_SPREAD": (("SELL", "P"), ("BUY", "P")),
    "BULL_CALL_SPREAD": (("BUY", "C"), ("SELL", "C")),
    "DIAGONAL_SPREAD": (("BUY", "C"), ("SELL", "C")),
    "IRON_CONDOR": (("BUY", "P"), ("SELL", "P"), ("SELL", "C"), ("BUY", "C")),
    "BEAR_CALL_SPREAD": (("SELL", "C"), ("BUY", "C")),
    "BEAR_PUT_SPREAD": (("BUY", "P"), ("SELL", "P")),
}


# ------------------------------------------------------------------ Filters
def _active_floor(value: float | None) -> float | None:
    return value if (value is not None and value > 0.0) else None


def candidate_passes_filters(
    pricing: StrategyPricing,
    risk: StrategyRiskMetrics,
    filters: StrategyCandidateFilters | None,
    *,
    qty_multiplier: int = 1,
) -> bool:
    """`True` when an evaluated candidate meets every active floor of `filters`.

    Floors compare the reported (rounded) metrics, so a row shown with RoR 20.0 passes
    `min_ror_pct=20`. Semantics per floor are documented on `StrategyCandidateFilters`.
    """
    if qty_multiplier < 1:
        raise ValueError(f"qty_multiplier must be >= 1 (got {qty_multiplier})")
    if filters is None:
        return True
    min_credit = _active_floor(filters.min_net_credit_dollars)
    per_unit_credit = pricing.mid_cash_total / qty_multiplier
    if min_credit is not None and per_unit_credit < min_credit - _FILTER_EPS:
        return False
    min_ror = _active_floor(filters.min_ror_pct)
    if min_ror is not None and (
        risk.return_on_risk_pct is None or risk.return_on_risk_pct < min_ror - _FILTER_EPS
    ):
        return False
    min_pop = _active_floor(filters.min_pop_pct)
    return min_pop is None or (
        risk.prob_of_profit_pct is not None and risk.prob_of_profit_pct >= min_pop - _FILTER_EPS
    )


# ------------------------------------------------------------------ Windows & Structure
def _neighbour_window(grid: Sequence[float], leg: StrategyLegInput) -> list[float]:
    idx = next((i for i, k in enumerate(grid) if abs(k - leg.strike) < _STRIKE_TOL), None)
    if idx is None:
        raise ValueError(
            f"Contract not found in loaded snapshot: {leg.expiry} {leg.strike:g}{leg.right}"
        )
    return list(grid[max(0, idx - 1) : idx + 2])


def repick_window_strikes(
    legs: Sequence[StrategyLegInput], quotes_df: pl.DataFrame
) -> list[list[float]]:
    """Default re-pick window per leg: its strike plus the nearest lower / upper strike present in
    `quotes_df` for the same expiry and right (ascending, 1-3 strikes).

    Capture this once when a live session subscribes and pass it back as `window_strikes`, so the
    window stays fixed after adjustments (it re-centres only when the caller recomputes it).
    Raises `ValueError` when a leg's own contract is missing from the frame.
    """
    windows: list[list[float]] = []
    for leg in legs:
        strikes = quotes_df.filter(
            (pl.col("expiry") == leg.expiry) & (pl.col("right").str.to_uppercase() == leg.right)
        )["strike"].to_list()
        grid = sorted({float(k) for k in strikes if k is not None and math.isfinite(float(k))})
        windows.append(_neighbour_window(grid, leg))
    return windows


def _validate_preset_shape(kind: PresetStrategyKind, legs: Sequence[StrategyLegInput]) -> None:
    roles = _PRESET_LEG_ROLES[kind]
    got = sorted((leg.side, leg.right) for leg in legs)
    if got != sorted(roles):
        want_txt = ", ".join(f"{side} {right}" for side, right in roles)
        got_txt = ", ".join(f"{side} {right}" for side, right in got) or "no legs"
        raise ValueError(f"{kind} re-pick expects legs [{want_txt}]; got [{got_txt}]")
    if kind != "DIAGONAL_SPREAD" and len({leg.expiry for leg in legs}) != 1:
        raise ValueError(f"{kind} re-pick expects every leg on the same expiry")


def _strikes_valid(
    kind: PresetStrategyKind, legs: Sequence[StrategyLegInput], strikes: Sequence[float]
) -> bool:
    """Preset geometry check on candidate strikes (roles are unique per preset)."""
    k = {(leg.side, leg.right): strike for leg, strike in zip(legs, strikes, strict=True)}
    if kind == "BULL_PUT_SPREAD":
        return k[("SELL", "P")] > k[("BUY", "P")] + _ORDER_EPS
    if kind == "BEAR_CALL_SPREAD":
        return k[("BUY", "C")] > k[("SELL", "C")] + _ORDER_EPS
    if kind in ("BULL_CALL_SPREAD", "DIAGONAL_SPREAD"):
        return k[("SELL", "C")] > k[("BUY", "C")] + _ORDER_EPS
    if kind == "BEAR_PUT_SPREAD":
        return k[("BUY", "P")] > k[("SELL", "P")] + _ORDER_EPS
    # IRON_CONDOR
    return (
        k[("BUY", "P")] + _ORDER_EPS < k[("SELL", "P")]
        and k[("SELL", "P")] + _ORDER_EPS < k[("SELL", "C")]
        and k[("SELL", "C")] + _ORDER_EPS < k[("BUY", "C")]
    )


def _row_key(row: PreparedRow) -> RowKey:
    return (row.expiry, round(row.strike, 6), row.right)


def _find_row_or_none(
    lookup: dict[RowKey, PreparedRow], expiry: str, strike: float, right: OptionRight
) -> PreparedRow | None:
    try:
        return find_row(lookup, expiry, strike, right)
    except ValueError:
        return None


# ------------------------------------------------------------------ Metrics-only Evaluator
@dataclass(frozen=True)
class _Horizon:
    ref_iv: float
    pop_grid: PopGrid | None


@dataclass(frozen=True)
class _Evaluation:
    rows: tuple[PreparedRow, ...]
    states: list[LegState]
    pricing: StrategyPricing
    risk: StrategyRiskMetrics


class _WindowEvaluator:
    """Caches per-row IV solves, Greeks and POP grids so each combination only aggregates."""

    def __init__(
        self,
        legs: Sequence[StrategyLegInput],
        rows: list[PreparedRow],
        window_rows: Sequence[PreparedRow],
        *,
        spot: float,
        risk_free_rate: float,
        qty_multiplier: int,
        full_rows: Callable[[], list[PreparedRow]],
    ) -> None:
        self._legs = list(legs)
        self._rows = rows
        self._spot = spot
        self._r = risk_free_rate
        self._qty = qty_multiplier
        self._full_rows = full_rows
        unique = list({_row_key(row): row for row in window_rows}.values())
        self._solved = dict(
            zip(
                (_row_key(row) for row in unique),
                solve_row_ivs(unique, spot, risk_free_rate),
                strict=True,
            )
        )
        self._horizons: dict[tuple[str, int], _Horizon] = {}
        self._states: dict[tuple[int, RowKey, float], LegState] = {}

    def _reference_iv(self, short_expiry: str) -> float:
        # Same value as `chain_reference_iv` over the whole frame: the scoped rows hold every row of
        # the legs' expiries, so only an IV-less short expiry needs the full-frame fallback pool.
        if any(
            r.expiry == short_expiry and r.iv is not None and 0.01 <= r.iv <= 5.0
            for r in self._rows
        ):
            return chain_reference_iv(self._rows, self._spot, short_expiry)
        return chain_reference_iv(self._full_rows(), self._spot, short_expiry)

    def _horizon(self, short_expiry: str, short_dte: int) -> _Horizon:
        key = (short_expiry, short_dte)
        cached = self._horizons.get(key)
        if cached is None:
            ref_iv = self._reference_iv(short_expiry)
            cached = _Horizon(ref_iv, lognormal_pop_grid(self._spot, short_dte, ref_iv, self._r))
            self._horizons[key] = cached
        return cached

    def _state(self, leg_idx: int, row: PreparedRow, fallback_iv: float) -> LegState:
        key = (leg_idx, _row_key(row), fallback_iv)
        cached = self._states.get(key)
        if cached is None:
            base_iv = effective_leg_iv(row, self._solved[_row_key(row)], fallback_iv)
            d_val, g_val, th_val, v_val = resolve_row_greeks(row, self._spot, self._r, base_iv)
            cached = LegState(self._legs[leg_idx], row, base_iv, d_val, g_val, th_val, v_val)
            self._states[key] = cached
        return cached

    def evaluate(self, rows: Sequence[PreparedRow]) -> _Evaluation:
        short_row = min(rows, key=lambda r: r.dte)
        horizon = self._horizon(short_row.expiry, short_row.dte)
        states = [self._state(i, row, horizon.ref_iv) for i, row in enumerate(rows)]
        pricing, mid_cash_total = strategy_pricing(states, self._qty)
        risk = strategy_risk_metrics(
            states,
            spot=self._spot,
            short_dte=short_row.dte,
            ref_iv=horizon.ref_iv,
            mid_cash_total=mid_cash_total,
            qty_multiplier=self._qty,
            risk_free_rate=self._r,
            pop_grid=horizon.pop_grid,
        )
        return _Evaluation(tuple(rows), states, pricing, risk)


# ------------------------------------------------------------------ Re-pick
def repick_strategy_legs(
    strategy_kind: PresetStrategyKind,
    current_legs: Sequence[StrategyLegInput],
    quotes_df: pl.DataFrame,
    *,
    window_strikes: Sequence[Sequence[float]] | None = None,
    filters: StrategyCandidateFilters | None = None,
    qty_multiplier: int = 1,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    spot: float | None = None,
) -> StrategyRepickResult:
    """Keep `current_legs` or switch to the higher-RoR neighbour combination inside the window.

    `quotes_df` uses `OPTION_CHAIN_POLARS_SCHEMA` (the enriched rows `evaluate_strategy`
    consumes), typically the snapshot with live quotes overlaid on the window contracts; only
    rows on the legs' expiries are prepared. `spot` (live underlying price) overrides the frame's
    `underlying_price` when given. `window_strikes[i]` lists the strikes leg `i` may take
    (default: `repick_window_strikes`). Metrics for any returned legs equal
    `evaluate_strategy(quotes_df, strategy_kind=..., legs=result.legs, ...)` with the same spot,
    `qty_multiplier` and `risk_free_rate` (`days_forward` / `iv_shift` only shape the payoff
    curves, which this path never builds).

    Raises `ValueError` for a non-preset kind, legs that do not form the preset's shape, invalid
    `qty_multiplier` / `risk_free_rate` / `spot`, a `window_strikes` length mismatch, or a current
    leg missing from the frame.
    """
    if strategy_kind not in PRESET_STRATEGIES:
        raise ValueError(f"Live re-pick supports preset strategies only (got {strategy_kind!r})")
    legs = list(current_legs)
    _validate_preset_shape(strategy_kind, legs)
    if qty_multiplier < 1:
        raise ValueError(f"qty_multiplier must be >= 1 (got {qty_multiplier})")
    if not math.isfinite(risk_free_rate):
        raise ValueError("risk_free_rate must be finite")
    if spot is not None and not (math.isfinite(spot) and spot > 0.0):
        raise ValueError(f"spot must be a finite positive price (got {spot!r})")
    if window_strikes is not None and len(window_strikes) != len(legs):
        raise ValueError(f"window_strikes has {len(window_strikes)} entries for {len(legs)} legs")

    def _with_spot(frame: pl.DataFrame) -> pl.DataFrame:
        if spot is None:
            return frame
        return frame.with_columns(pl.lit(float(spot), dtype=pl.Float64).alias("underlying_price"))

    leg_expiries = sorted({leg.expiry for leg in legs})
    scoped = quotes_df.filter(pl.col("expiry").is_in(leg_expiries))
    if scoped.height == 0:
        raise ValueError(f"Quote frame has no rows for leg expiries {', '.join(leg_expiries)}")
    underlying, eff_spot, rows = prepare_rows(_with_spot(scoped))
    lookup = row_lookup_map(rows)
    current_rows = [find_row(lookup, leg.expiry, leg.strike, leg.right) for leg in legs]

    grids: dict[tuple[str, OptionRight], list[float]] = {}
    if window_strikes is None:
        for row in rows:
            grids.setdefault((row.expiry, row.right), []).append(row.strike)
    per_leg: list[list[PreparedRow]] = []
    for idx, leg in enumerate(legs):
        if window_strikes is None:
            grid = sorted(set(grids.get((leg.expiry, leg.right), [])))
            strikes = _neighbour_window(grid, leg)
        else:
            strikes = [float(k) for k in window_strikes[idx] if math.isfinite(float(k))]
        options: dict[RowKey, PreparedRow] = {_row_key(current_rows[idx]): current_rows[idx]}
        for strike in strikes:
            row_opt = _find_row_or_none(lookup, leg.expiry, strike, leg.right)
            if row_opt is not None:
                options.setdefault(_row_key(row_opt), row_opt)
        per_leg.append(sorted(options.values(), key=lambda r: r.strike))

    full_cache: list[list[PreparedRow]] = []

    def _full_rows() -> list[PreparedRow]:
        if not full_cache:
            full_cache.append(prepare_rows(_with_spot(quotes_df))[2])
        return full_cache[0]

    evaluator = _WindowEvaluator(
        legs,
        rows,
        [row for options_rows in per_leg for row in options_rows],
        spot=eff_spot,
        risk_free_rate=risk_free_rate,
        qty_multiplier=qty_multiplier,
        full_rows=_full_rows,
    )
    current = evaluator.evaluate(current_rows)
    current_ror = current.risk.return_on_risk_pct
    current_ok = candidate_passes_filters(
        current.pricing, current.risk, filters, qty_multiplier=qty_multiplier
    )

    current_keys = tuple(_row_key(row) for row in current_rows)
    evaluated = 0
    better_but_filtered = False
    best: _Evaluation | None = None
    best_rank: tuple[float, int, float] | None = None
    for combo in itertools.product(*per_leg):
        keys = tuple(_row_key(row) for row in combo)
        if keys == current_keys:
            continue
        if not _strikes_valid(strategy_kind, legs, [row.strike for row in combo]):
            continue
        if any(row.mid <= 0.0 for row in combo):
            continue  # an unquoted neighbour would fake a better credit / debit
        evaluated += 1
        if current_ror is None:
            continue  # nothing can rank against an undefined current RoR; just count the window
        cand = evaluator.evaluate(combo)
        ror = cand.risk.return_on_risk_pct
        if ror is None or not ror > current_ror:
            continue
        if not candidate_passes_filters(
            cand.pricing, cand.risk, filters, qty_multiplier=qty_multiplier
        ):
            better_but_filtered = True
            continue
        moved = sum(1 for a, b in zip(keys, current_keys, strict=True) if a != b)
        pop = cand.risk.prob_of_profit_pct
        rank = (ror, -moved, pop if pop is not None else -math.inf)
        if best_rank is None or rank > best_rank:
            best, best_rank = cand, rank

    keep_reason: RepickKeepReason | None
    if evaluated == 0:
        keep_reason = "NO_ALTERNATIVE_IN_WINDOW"
    elif current_ror is None:
        keep_reason = "CURRENT_ROR_UNAVAILABLE"
    elif best is not None:
        keep_reason = None
    elif better_but_filtered:
        keep_reason = "BETTER_CANDIDATE_FILTERED"
    else:
        keep_reason = "NO_BETTER_CANDIDATE"

    chosen = best if (keep_reason is None and best is not None) else current
    if chosen is current:
        out_legs = legs
    else:
        out_legs = [
            leg
            if _row_key(row) == key
            else StrategyLegInput.model_validate({**leg.model_dump(), "strike": row.strike})
            for leg, row, key in zip(legs, chosen.rows, current_keys, strict=True)
        ]
    return StrategyRepickResult(
        strategy_kind=strategy_kind,
        underlying=underlying,
        underlying_price=round(eff_spot, 4),
        adjusted=chosen is not current,
        keep_reason=keep_reason,
        current_passes_filters=current_ok,
        candidates_evaluated=evaluated,
        legs=out_legs,
        evaluated_legs=evaluated_legs(chosen.states, qty_multiplier),
        pricing=chosen.pricing,
        risk=chosen.risk,
        net_greeks=net_position_greeks(chosen.states, qty_multiplier),
    )
