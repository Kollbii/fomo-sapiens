"""2D parameter sensitivity surface, 3x3 plateau stability, and Deflated Sharpe Ratio."""

from __future__ import annotations

import math
from typing import Final, Literal

import numpy as np
import numpy.typing as npt
from scipy.special import erf  # type: ignore[import-untyped]

from src.analytics.backtest.cohorts import (
    SPREAD_STRATEGIES,
    CohortRunOutput,
    SpreadCohortMatrix,
    evaluate_cohort_matrix,
    get_or_build_spread_matrix,
    run_cohort_analysis,
)
from src.analytics.backtest.fast_cache import FastBarCache
from src.analytics.backtest.models import BacktestConfig, BacktestStrategyKind
from src.analytics.backtest.robustness_models import (
    SensitivityAxisParam,
    SensitivityCell,
    SensitivityConfig,
    SensitivityGridReport,
    SensitivityObjective,
    clean_float,
    finite_float,
)
from src.core.exceptions import BacktestValidationError

FloatArray = npt.NDArray[np.float64]

EULER_MASCHERONI: Final = 0.5772156649015329


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + float(erf(z / math.sqrt(2.0))))


def _norm_ppf(p: float) -> float:
    clamped = min(1.0 - 1e-12, max(1e-12, p))
    lo, hi = -8.0, 8.0
    for _ in range(52):
        mid = 0.5 * (lo + hi)
        if _norm_cdf(mid) < clamped:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def resolve_sensitivity_axes(
    sens_cfg: SensitivityConfig,
    *,
    strategy_kind: BacktestStrategyKind | None = None,
) -> tuple[SensitivityAxisParam, SensitivityAxisParam]:
    """Resolve non-colliding `(x_param, y_param)` defaults."""
    x_param = sens_cfg.x_param
    y_param = sens_cfg.y_param
    is_stateful = strategy_kind in ("COVERED_CALL", "WHEEL")
    if x_param is None and y_param is None:
        return (
            ("target_delta", "profit_target_pct")
            if is_stateful
            else ("profit_target_pct", "stop_loss_pct")
        )
    if x_param is None:
        if is_stateful:
            x_param = "target_delta" if y_param != "target_delta" else "profit_target_pct"
        else:
            x_param = "profit_target_pct" if y_param != "profit_target_pct" else "stop_loss_pct"
    if y_param is None:
        if is_stateful:
            y_param = "profit_target_pct" if x_param != "profit_target_pct" else "target_delta"
        else:
            y_param = "stop_loss_pct" if x_param != "stop_loss_pct" else "profit_target_pct"
    return x_param, y_param


def _parse_dte_bucket(raw: float | str) -> tuple[int, int]:
    if not isinstance(raw, str):
        raise BacktestValidationError(f"dte_bucket value must be a 'min-max' string, got {raw!r}.")
    parts = raw.replace(":", "-").split("-")
    if len(parts) != 2:
        raise BacktestValidationError(f"Invalid dte_bucket {raw!r}; expected 'min-max'.")
    try:
        lo, hi = int(parts[0].strip()), int(parts[1].strip())
    except ValueError as exc:
        raise BacktestValidationError(f"Invalid dte_bucket {raw!r}: {exc}") from exc
    if lo < 0 or hi > 120 or lo > hi:
        raise BacktestValidationError(f"dte_bucket {raw!r} out of bounds (0 <= min <= max <= 120).")
    return lo, hi


def default_axis_values(
    param: SensitivityAxisParam,
    density: Literal[5, 7],
) -> list[float | str]:
    """Return default 5- or 7-point parameter ladder for `param`."""
    ladders_5: dict[SensitivityAxisParam, list[float | str]] = {
        "profit_target_pct": [25.0, 35.0, 50.0, 65.0, 75.0],
        "stop_loss_pct": [50.0, 75.0, 100.0, 150.0, 200.0],
        "exit_dte": [0.0, 7.0, 14.0, 21.0, 28.0],
        "dte_bucket": ["14-28", "21-35", "30-45", "30-60", "45-75"],
        "target_delta": [0.15, 0.20, 0.25, 0.30, 0.40],
        "min_entry_credit_dollars": [0.0, 50.0, 100.0, 150.0, 200.0],
        "min_entry_ror_pct": [0.0, 10.0, 20.0, 30.0, 40.0],
    }
    ladders_7: dict[SensitivityAxisParam, list[float | str]] = {
        "profit_target_pct": [20.0, 30.0, 40.0, 50.0, 60.0, 75.0, 90.0],
        "stop_loss_pct": [50.0, 75.0, 100.0, 125.0, 150.0, 200.0, 300.0],
        "exit_dte": [0.0, 5.0, 7.0, 10.0, 14.0, 21.0, 28.0],
        "dte_bucket": ["7-21", "14-28", "21-35", "30-45", "30-60", "45-75", "60-90"],
        "target_delta": [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.45],
        "min_entry_credit_dollars": [0.0, 25.0, 50.0, 100.0, 150.0, 200.0, 300.0],
        "min_entry_ror_pct": [0.0, 5.0, 10.0, 15.0, 25.0, 35.0, 50.0],
    }
    return list(ladders_5[param] if density == 5 else ladders_7[param])


def _format_axis_label(param: SensitivityAxisParam, val: float | str) -> str:
    if param == "dte_bucket":
        return f"{val}d"
    if param == "exit_dte":
        return f"{int(round(float(val)))}d"
    if param == "target_delta":
        return f"{float(val):.2f}"
    if param == "min_entry_credit_dollars":
        return f"${float(val):.0f}"
    return f"{float(val):.0f}%"


def _param_update_dict(param: SensitivityAxisParam, val: float | str) -> dict[str, object]:
    if param == "dte_bucket":
        lo, hi = _parse_dte_bucket(val)
        return {"target_dte_min": lo, "target_dte_max": hi}
    if isinstance(val, str):
        raise BacktestValidationError(f"Numeric value required for {param}, got {val!r}.")
    num = float(val)
    if param == "profit_target_pct":
        return {"profit_target_pct": None if num <= 0.0 else min(1000.0, num)}
    if param == "stop_loss_pct":
        return {"stop_loss_pct": None if num <= 0.0 else min(2000.0, num)}
    if param == "exit_dte":
        return {"exit_dte": max(0, min(120, int(round(num))))}
    if param == "target_delta":
        return {"strike_selection_mode": "TARGET_DELTA", "target_delta": max(0.05, min(0.50, num))}
    if param == "min_entry_credit_dollars":
        return {"min_entry_credit_dollars": max(0.0, num)}
    return {"min_entry_ror_pct": max(0.0, min(1000.0, num))}


def apply_sensitivity_params(
    base_config: BacktestConfig, params: dict[str, float | str]
) -> BacktestConfig:
    """Return a validated `BacktestConfig` copy with sensitivity parameter overrides applied."""
    updates: dict[str, object] = {}
    for p_name, p_val in params.items():
        axis: SensitivityAxisParam = p_name  # type: ignore[assignment]
        updates.update(_param_update_dict(axis, p_val))
    return BacktestConfig.model_validate({**base_config.model_dump(), **updates})


def _baseline_value_for_param(config: BacktestConfig, param: SensitivityAxisParam) -> float | str:
    if param == "dte_bucket":
        return f"{config.target_dte_min}-{config.target_dte_max}"
    if param == "profit_target_pct":
        return float(config.profit_target_pct) if config.profit_target_pct is not None else 0.0
    if param == "stop_loss_pct":
        return float(config.stop_loss_pct) if config.stop_loss_pct is not None else 0.0
    if param == "exit_dte":
        return float(config.exit_dte) if config.exit_dte is not None else 0.0
    if param == "target_delta":
        return float(config.target_delta)
    if param == "min_entry_credit_dollars":
        return float(config.min_entry_credit_dollars or 0.0)
    return float(config.min_entry_ror_pct or 0.0)


def _nearest_index(
    param: SensitivityAxisParam, values: list[float | str], target: float | str
) -> int:
    if param == "dte_bucket":
        for idx, item in enumerate(values):
            if str(item) == str(target):
                return idx
        t_lo, t_hi = _parse_dte_bucket(target)
        t_mid = 0.5 * (t_lo + t_hi)
        return min(
            range(len(values)),
            key=lambda i: abs(0.5 * sum(_parse_dte_bucket(values[i])) - t_mid),
        )
    t_num = float(target)
    return min(range(len(values)), key=lambda i: abs(float(values[i]) - t_num))


def extract_objective_value(
    cohort_out: CohortRunOutput, objective: SensitivityObjective
) -> float | None:
    """Extract the requested `SensitivityObjective` scalar from a cohort run output."""
    rep = cohort_out.report
    if objective == "expectancy_dollars":
        return rep.expectancy_dollars
    if objective == "return_on_risk_pct":
        return rep.avg_return_on_risk_pct
    if objective == "total_return_pct":
        return rep.portfolio_total_return_pct
    if objective == "profit_factor":
        return rep.profit_factor
    if objective == "sharpe":
        return rep.portfolio_sharpe
    if objective == "calmar":
        return rep.portfolio_calmar
    return rep.win_rate_pct


def _compute_dsr(
    best_returns: FloatArray | None,
    trial_sharpes: list[float],
) -> float | None:
    if best_returns is None or len(best_returns) < 10 or len(trial_sharpes) < 2:
        return None
    mu = float(np.mean(best_returns))
    sigma = float(np.std(best_returns, ddof=1))
    if not math.isfinite(sigma) or sigma <= 1e-9:
        return None
    sr_hat = mu / sigma
    z = (best_returns - mu) / sigma
    skew = float(np.mean(z**3))
    kurt = float(np.mean(z**4))

    v_sr = float(np.var(np.array(trial_sharpes, dtype=np.float64), ddof=1))
    if not math.isfinite(v_sr) or v_sr <= 1e-12:
        sr_0 = 0.0
    else:
        n_eff = len(trial_sharpes)
        sr_0 = math.sqrt(v_sr) * (
            (1.0 - EULER_MASCHERONI) * _norm_ppf(1.0 - 1.0 / float(n_eff))
            + EULER_MASCHERONI * _norm_ppf(1.0 - 1.0 / (float(n_eff) * math.e))
        )
    denom_sq = max(1e-6, 1.0 - skew * sr_hat + ((kurt - 1.0) / 4.0) * sr_hat * sr_hat)
    z_dsr = ((sr_hat - sr_0) * math.sqrt(float(len(best_returns) - 1))) / math.sqrt(denom_sq)
    return clean_float(_norm_cdf(z_dsr), 4)


def run_sensitivity_grid(
    cache: FastBarCache,
    config: BacktestConfig,
    sens_config: SensitivityConfig | None = None,
    *,
    ticker: str,
    matrix_cache: dict[tuple[object, ...], SpreadCohortMatrix] | None = None,
    slice_bounds: tuple[int, int] | None = None,
) -> tuple[SensitivityGridReport, SensitivityCell | None]:
    """Evaluate a 2D sensitivity grid over `cache`, returning `(report, optimal_cell)`."""
    if config.strategy_kind == "CUSTOM":
        raise BacktestValidationError("Sensitivity grid does not support CUSTOM strategy kind.")
    s_cfg = sens_config or SensitivityConfig()
    x_param, y_param = resolve_sensitivity_axes(s_cfg, strategy_kind=config.strategy_kind)
    x_values = (
        list(s_cfg.x_values)
        if s_cfg.x_values is not None
        else default_axis_values(x_param, s_cfg.grid_density)
    )
    y_values = (
        list(s_cfg.y_values)
        if s_cfg.y_values is not None
        else default_axis_values(y_param, s_cfg.grid_density)
    )

    base_ix = _nearest_index(x_param, x_values, _baseline_value_for_param(config, x_param))
    base_iy = _nearest_index(y_param, y_values, _baseline_value_for_param(config, y_param))

    m_cache: dict[tuple[object, ...], SpreadCohortMatrix] = (
        matrix_cache if matrix_cache is not None else {}
    )
    is_spread = config.strategy_kind in SPREAD_STRATEGIES
    eval_cache = (
        cache.slice(slice_bounds[0], slice_bounds[1]) if slice_bounds is not None else cache
    )

    nx = len(x_values)
    ny = len(y_values)
    raw_grid: list[list[tuple[CohortRunOutput, float | None]]] = []
    trial_per_bar_sharpes: list[float] = []

    for x_val in x_values:
        row_runs: list[tuple[CohortRunOutput, float | None]] = []
        for y_val in y_values:
            cell_cfg = apply_sensitivity_params(config, {x_param: x_val, y_param: y_val})
            if is_spread:
                full_matrix = get_or_build_spread_matrix(cache, cell_cfg, m_cache)
                matrix = (
                    full_matrix.slice(slice_bounds[0], slice_bounds[1])
                    if slice_bounds is not None
                    else full_matrix
                )
                out = evaluate_cohort_matrix(matrix, cell_cfg, cache=eval_cache)
            else:
                out = run_cohort_analysis(
                    eval_cache, cell_cfg, ticker=ticker, include_rolling=False
                )
            obj_val = extract_objective_value(out, s_cfg.objective)
            row_runs.append((out, obj_val))
            r = out.daily_returns
            if len(r) >= 2:
                std_r = float(np.std(r, ddof=1))
                if math.isfinite(std_r) and std_r > 1e-9:
                    trial_per_bar_sharpes.append(float(np.mean(r)) / std_r)
        raw_grid.append(row_runs)

    # 3x3 neighbor smoothing
    smoothed_grid: list[list[float | None]] = [[None] * ny for _ in range(nx)]
    for ix in range(nx):
        for iy in range(ny):
            center_obj = raw_grid[ix][iy][1]
            if center_obj is None:
                continue
            nbr_vals: list[float] = []
            for jx in range(max(0, ix - 1), min(nx, ix + 2)):
                for jy in range(max(0, iy - 1), min(ny, iy + 2)):
                    val = raw_grid[jx][jy][1]
                    if val is not None and math.isfinite(val):
                        nbr_vals.append(val)
            if nbr_vals:
                smoothed_grid[ix][iy] = clean_float(sum(nbr_vals) / len(nbr_vals), 4)

    # Select optimal cell (require at least 1 closed trade and finite objective)
    best_coord: tuple[int, int] | None = None
    best_key: tuple[float, float, int] | None = None
    for ix in range(nx):
        for iy in range(ny):
            out, obj_val = raw_grid[ix][iy]
            sm = smoothed_grid[ix][iy]
            if obj_val is None or sm is None or out.report.closed_cohorts < 1:
                continue
            # Deterministic tie-break: closer to baseline preferred
            dist_base = -(abs(ix - base_ix) + abs(iy - base_iy))
            cand_key = (sm, obj_val, dist_base)
            if best_key is None or cand_key > best_key:
                best_key = cand_key
                best_coord = (ix, iy)

    cells: list[SensitivityCell] = []
    baseline_cell: SensitivityCell | None = None
    optimal_cell: SensitivityCell | None = None
    profitable_count = 0
    valid_count = 0

    for ix, x_val in enumerate(x_values):
        for iy, y_val in enumerate(y_values):
            out, obj_val = raw_grid[ix][iy]
            rep = out.report
            if obj_val is not None and rep.closed_cohorts > 0:
                valid_count += 1
            if rep.closed_cohorts > 0 and rep.portfolio_total_return_pct > 0.0:
                profitable_count += 1
            is_base = ix == base_ix and iy == base_iy
            is_opt = best_coord == (ix, iy)
            cell = SensitivityCell(
                x_index=ix,
                y_index=iy,
                x_value=x_val,
                y_value=y_val,
                x_label=_format_axis_label(x_param, x_val),
                y_label=_format_axis_label(y_param, y_val),
                objective_value=clean_float(obj_val, 4),
                smoothed_score=smoothed_grid[ix][iy],
                trade_count=rep.entered_cohorts,
                closed_trades=rep.closed_cohorts,
                win_rate_pct=rep.win_rate_pct,
                expectancy_dollars=rep.expectancy_dollars,
                return_on_risk_pct=rep.avg_return_on_risk_pct,
                total_return_pct=rep.portfolio_total_return_pct,
                profit_factor=rep.profit_factor,
                sharpe=rep.portfolio_sharpe,
                calmar=rep.portfolio_calmar,
                max_drawdown_pct=rep.portfolio_max_drawdown_pct,
                is_baseline=is_base,
                is_optimal=is_opt,
            )
            cells.append(cell)
            if is_base:
                baseline_cell = cell
            if is_opt:
                optimal_cell = cell

    total_cells = nx * ny
    profitable_pct = finite_float((profitable_count / max(1, total_cells)) * 100.0, 2)

    if best_coord is None or optimal_cell is None or (optimal_cell.objective_value or 0.0) <= 0.0:
        plateau_score = finite_float(0.5 * profitable_pct, 2)
    else:
        ox, oy = best_coord
        opt_obj = float(optimal_cell.objective_value or 0.0)
        ratios: list[float] = []
        for jx in range(max(0, ox - 1), min(nx, ox + 2)):
            for jy in range(max(0, oy - 1), min(ny, oy + 2)):
                nbr_obj = raw_grid[jx][jy][1]
                if nbr_obj is None:
                    ratios.append(0.0)
                else:
                    ratios.append(max(0.0, min(1.0, nbr_obj / opt_obj)))
        nbr_retention = (sum(ratios) / len(ratios)) * 100.0 if ratios else 0.0
        plateau_score = finite_float(
            max(0.0, min(100.0, 0.6 * nbr_retention + 0.4 * profitable_pct)), 2
        )

    best_returns = raw_grid[best_coord[0]][best_coord[1]][0].daily_returns if best_coord else None
    dsr = _compute_dsr(best_returns, trial_per_bar_sharpes)
    mt_warning = dsr is not None and dsr < 0.50
    warn_msg = (
        f"Deflated Sharpe Ratio ({dsr:.2f} < 0.50) across {total_cells} grid trials suggests "
        "elevated multiple-testing / overfitting risk."
        if mt_warning and dsr is not None
        else None
    )

    notes: list[str] = []
    for axis_param in dict.fromkeys((x_param, y_param)):
        if axis_param == "target_delta" and config.strategy_kind not in ("COVERED_CALL", "WHEEL"):
            notes.append(
                "Axis 'target_delta' only affects COVERED_CALL and WHEEL; "
                "cells across this axis are invariant."
            )
        elif axis_param == "min_entry_ror_pct" and config.strategy_kind != "BULL_PUT_SPREAD":
            notes.append(
                "Axis 'min_entry_ror_pct' only filters BULL_PUT_SPREAD entries; "
                "cells across this axis are invariant."
            )
        elif axis_param == "min_entry_credit_dollars" and config.strategy_kind not in (
            "BULL_PUT_SPREAD",
            "COVERED_CALL",
            "WHEEL",
        ):
            notes.append(
                "Axis 'min_entry_credit_dollars' only filters BULL_PUT_SPREAD, "
                "COVERED_CALL, and WHEEL entries."
            )
    if valid_count == 0:
        notes.append("No sensitivity grid cells produced closed trades.")

    report = SensitivityGridReport(
        x_param=x_param,
        y_param=y_param,
        objective=s_cfg.objective,
        x_values=x_values,
        y_values=y_values,
        cells=cells,
        baseline_cell=baseline_cell,
        optimal_cell=optimal_cell,
        plateau_stability_score=plateau_score,
        profitable_cells_pct=profitable_pct,
        valid_cells_count=valid_count,
        total_cells_count=total_cells,
        deflated_sharpe_ratio=dsr,
        multiple_testing_warning=mt_warning,
        warning_message=warn_msg,
        notes=notes,
    )
    return report, optimal_cell
