"""Purged Walk-Forward Optimization and Fixed-Window OOS validation (strictly zero-lookahead)."""

from __future__ import annotations

import numpy as np

from src.analytics.backtest.cohorts import (
    SPREAD_STRATEGIES,
    SpreadCohortMatrix,
    compute_return_risk_ratios,
    evaluate_cohort_matrix,
    get_or_build_spread_matrix,
    run_cohort_analysis,
)
from src.analytics.backtest.fast_cache import FastBarCache
from src.analytics.backtest.models import BacktestConfig, EquityCurvePoint
from src.analytics.backtest.robustness_models import (
    MarketRegimeLabel,
    SensitivityConfig,
    WalkForwardConfig,
    WalkForwardFold,
    WalkForwardReport,
    clean_float,
    finite_float,
)
from src.analytics.backtest.rules import DENOM_EPS
from src.analytics.backtest.sensitivity import (
    apply_sensitivity_params,
    extract_objective_value,
    run_sensitivity_grid,
)
from src.core.exceptions import BacktestValidationError

MAX_WFO_FOLDS: int = 24
MIN_TAIL_WINDOW_BARS: int = 15


def _classify_regime(cache: FastBarCache) -> tuple[MarketRegimeLabel, float | None]:
    if len(cache) < 2:
        return "UNKNOWN", None
    s0 = cache.entries[0].ctx.spot
    s1 = cache.entries[-1].ctx.spot
    if s0 is None or s1 is None or s0 <= 0.0 or s1 <= 0.0:
        return "UNKNOWN", None
    ret_pct = ((s1 - s0) / s0) * 100.0
    if ret_pct >= 3.0:
        label: MarketRegimeLabel = "BULL"
    elif ret_pct <= -3.0:
        label = "BEAR"
    else:
        label = "FLAT"
    return label, clean_float(ret_pct, 2)


def _append_stitched_returns(
    curve: list[EquityCurvePoint],
    cache: FastBarCache,
    returns: np.ndarray,
    state: list[float],
) -> None:
    """Append multiplicative equity points; `state = [eq, peak, max_dd_pct, init_cap]`."""
    eq, peak, max_dd_pct, initial_capital = state
    for idx, entry in enumerate(cache.entries):
        r = float(returns[idx]) if idx < len(returns) else 0.0
        eq = max(0.0, eq * (1.0 + max(-0.9999, r)))
        eq_r = finite_float(eq, 2)
        peak = max(peak, eq_r)
        dd_d = finite_float(max(0.0, peak - eq_r), 2)
        dd_p = finite_float((dd_d / peak) * 100.0 if peak > DENOM_EPS else 0.0, 2)
        max_dd_pct = max(max_dd_pct, dd_p)
        bar = entry.bar
        curve.append(
            EquityCurvePoint(
                market_date=bar.market_date,
                snapshot_id=bar.snapshot_id,
                ts_utc=bar.ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                equity_dollars=eq_r,
                realized_pnl_dollars=finite_float(eq - initial_capital, 2),
                unrealized_pnl_dollars=0.0,
                drawdown_dollars=dd_d,
                drawdown_pct=dd_p,
                open_trades=0,
                underlying_price=clean_float(entry.ctx.spot, 4),
                flags=[],
            )
        )
    state[0], state[1], state[2] = eq, peak, max_dd_pct


def run_walk_forward_analysis(
    cache: FastBarCache,
    config: BacktestConfig,
    wf_config: WalkForwardConfig | None = None,
    *,
    ticker: str,
    sensitivity_config: SensitivityConfig | None = None,
    matrix_cache: dict[tuple[object, ...], SpreadCohortMatrix] | None = None,
) -> WalkForwardReport:
    """Run Fixed-Window or Purged WFO with strict `cache.slice(...)` IS/OOS isolation."""
    if config.strategy_kind == "CUSTOM":
        raise BacktestValidationError("Walk-forward analysis does not support CUSTOM strategy.")
    wf_cfg = wf_config or WalkForwardConfig()
    s_base = sensitivity_config or SensitivityConfig()
    is_sens_cfg = SensitivityConfig(
        x_param=s_base.x_param,
        y_param=s_base.y_param,
        x_values=s_base.x_values,
        y_values=s_base.y_values,
        grid_density=s_base.grid_density,
        objective=wf_cfg.objective,
    )
    m_cache: dict[tuple[object, ...], SpreadCohortMatrix] = (
        matrix_cache if matrix_cache is not None else {}
    )
    is_spread = config.strategy_kind in SPREAD_STRATEGIES

    total_bars = len(cache)
    entries = cache.entries
    folds: list[WalkForwardFold] = []
    stitched_oos_curve: list[EquityCurvePoint] = []
    stitched_base_curve: list[EquityCurvePoint] = []
    oos_state = [config.initial_capital, config.initial_capital, 0.0, config.initial_capital]
    base_state = [config.initial_capital, config.initial_capital, 0.0, config.initial_capital]
    stitched_returns_parts: list[np.ndarray] = []

    if wf_cfg.mode == "FIXED_WINDOWS":
        step = wf_cfg.fixed_window_bars
        min_w = min(MIN_TAIL_WINDOW_BARS, step)
        w_start = 0
        full_fixed_matrix = (
            get_or_build_spread_matrix(cache, config, m_cache) if is_spread else None
        )
        while w_start < total_bars and len(folds) < MAX_WFO_FOLDS:
            w_end = min(total_bars, w_start + step)
            if (w_end - w_start) < min_w:
                break
            oos_cache = cache.slice(w_start, w_end)
            if full_fixed_matrix is not None:
                oos_out = evaluate_cohort_matrix(
                    full_fixed_matrix.slice(w_start, w_end), config, cache=oos_cache
                )
            else:
                oos_out = run_cohort_analysis(
                    oos_cache, config, ticker=ticker, include_rolling=False
                )
            regime, und_ret = _classify_regime(oos_cache)
            oos_rep = oos_out.report
            folds.append(
                WalkForwardFold(
                    fold_index=len(folds),
                    is_start_date=oos_cache.entries[0].bar.market_date,
                    is_end_date=oos_cache.entries[-1].bar.market_date,
                    oos_start_date=oos_cache.entries[0].bar.market_date,
                    oos_end_date=oos_cache.entries[-1].bar.market_date,
                    is_bars=0,
                    oos_bars=len(oos_cache),
                    regime=regime,
                    underlying_return_pct=und_ret,
                    selected_params={},
                    is_objective_score=None,
                    oos_objective_score=clean_float(
                        extract_objective_value(oos_out, wf_cfg.objective), 4
                    ),
                    is_return_pct=0.0,
                    oos_return_pct=oos_rep.portfolio_total_return_pct,
                    baseline_oos_return_pct=oos_rep.portfolio_total_return_pct,
                    oos_max_drawdown_pct=oos_rep.portfolio_max_drawdown_pct,
                    oos_trades=oos_rep.closed_cohorts,
                    oos_win_rate_pct=oos_rep.win_rate_pct,
                    oos_sharpe=oos_rep.portfolio_sharpe,
                )
            )
            _append_stitched_returns(
                stitched_oos_curve, oos_cache, oos_out.daily_returns, oos_state
            )
            _append_stitched_returns(
                stitched_base_curve, oos_cache, oos_out.daily_returns, base_state
            )
            stitched_returns_parts.append(oos_out.daily_returns)
            w_start = w_end
    else:
        is_len = wf_cfg.is_bars
        oos_len = wf_cfg.oos_bars
        embargo = wf_cfg.embargo_bars
        min_oos = min(MIN_TAIL_WINDOW_BARS, oos_len)
        fold_idx = 0
        while len(folds) < MAX_WFO_FOLDS:
            if wf_cfg.window_type == "ANCHORED":
                is_start = 0
                is_end = is_len + fold_idx * oos_len
            else:
                is_start = fold_idx * oos_len
                is_end = is_start + is_len
            oos_start = is_end + embargo
            oos_end = min(total_bars, oos_start + oos_len)
            if oos_start >= total_bars or (oos_end - oos_start) < min_oos:
                break

            # Strict isolation: IS optimization only ever receives [is_start:is_end]
            oos_cache = cache.slice(oos_start, oos_end)

            if is_spread:
                is_report, opt_cell = run_sensitivity_grid(
                    cache,
                    config,
                    is_sens_cfg,
                    ticker=ticker,
                    matrix_cache=m_cache,
                    slice_bounds=(is_start, is_end),
                )
            else:
                is_cache = cache.slice(is_start, is_end)
                is_report, opt_cell = run_sensitivity_grid(
                    is_cache, config, is_sens_cfg, ticker=ticker
                )
            if opt_cell is not None:
                selected_params: dict[str, float | str] = {
                    is_report.x_param: opt_cell.x_value,
                    is_report.y_param: opt_cell.y_value,
                }
                oos_cfg = apply_sensitivity_params(config, selected_params)
                is_ret_pct = opt_cell.total_return_pct
                is_obj_score = opt_cell.objective_value
            else:
                selected_params = {}
                oos_cfg = config
                is_ret_pct = 0.0
                is_obj_score = None

            if is_spread:
                oos_full_matrix = get_or_build_spread_matrix(cache, oos_cfg, m_cache)
                oos_out = evaluate_cohort_matrix(
                    oos_full_matrix.slice(oos_start, oos_end), oos_cfg, cache=oos_cache
                )
                base_full_matrix = get_or_build_spread_matrix(cache, config, m_cache)
                base_oos_out = evaluate_cohort_matrix(
                    base_full_matrix.slice(oos_start, oos_end), config, cache=oos_cache
                )
            else:
                oos_out = run_cohort_analysis(
                    oos_cache, oos_cfg, ticker=ticker, include_rolling=False
                )
                base_oos_out = run_cohort_analysis(
                    oos_cache, config, ticker=ticker, include_rolling=False
                )
            regime, und_ret = _classify_regime(oos_cache)
            oos_rep = oos_out.report
            folds.append(
                WalkForwardFold(
                    fold_index=fold_idx,
                    is_start_date=entries[is_start].bar.market_date,
                    is_end_date=entries[is_end - 1].bar.market_date,
                    oos_start_date=entries[oos_start].bar.market_date,
                    oos_end_date=entries[oos_end - 1].bar.market_date,
                    is_bars=is_end - is_start,
                    oos_bars=len(oos_cache),
                    regime=regime,
                    underlying_return_pct=und_ret,
                    selected_params=selected_params,
                    is_objective_score=is_obj_score,
                    oos_objective_score=clean_float(
                        extract_objective_value(oos_out, wf_cfg.objective), 4
                    ),
                    is_return_pct=finite_float(is_ret_pct, 2),
                    oos_return_pct=oos_rep.portfolio_total_return_pct,
                    baseline_oos_return_pct=base_oos_out.report.portfolio_total_return_pct,
                    oos_max_drawdown_pct=oos_rep.portfolio_max_drawdown_pct,
                    oos_trades=oos_rep.closed_cohorts,
                    oos_win_rate_pct=oos_rep.win_rate_pct,
                    oos_sharpe=oos_rep.portfolio_sharpe,
                )
            )
            _append_stitched_returns(
                stitched_oos_curve, oos_cache, oos_out.daily_returns, oos_state
            )
            _append_stitched_returns(
                stitched_base_curve, oos_cache, base_oos_out.daily_returns, base_state
            )
            stitched_returns_parts.append(oos_out.daily_returns)
            fold_idx += 1

    oos_total_ret = finite_float(
        ((oos_state[0] - config.initial_capital) / config.initial_capital) * 100.0, 2
    )
    base_total_ret = finite_float(
        ((base_state[0] - config.initial_capital) / config.initial_capital) * 100.0, 2
    )
    oos_max_dd = finite_float(oos_state[2], 2)

    all_stitched_r = (
        np.concatenate(stitched_returns_parts)
        if stitched_returns_parts
        else np.empty(0, dtype=np.float64)
    )
    oos_sharpe, _sortino, _calmar = compute_return_risk_ratios(
        all_stitched_r,
        total_return_pct=oos_total_ret,
        max_drawdown_pct=oos_max_dd,
    )

    is_avg_ret = (
        clean_float(sum(f.is_return_pct for f in folds) / len(folds), 2)
        if (folds and wf_cfg.mode == "PURGED_WFO")
        else None
    )
    oos_avg_ret = (
        clean_float(sum(f.oos_return_pct for f in folds) / len(folds), 2) if folds else None
    )
    pos_win_pct = (
        clean_float((sum(1 for f in folds if f.oos_return_pct > 0.0) / len(folds)) * 100.0, 2)
        if folds
        else None
    )

    wfe_pct: float | None = None
    if wf_cfg.mode == "PURGED_WFO" and folds:
        is_rates = [f.is_return_pct / float(f.is_bars) for f in folds if f.is_bars > 0]
        oos_rates = [f.oos_return_pct / float(f.oos_bars) for f in folds if f.is_bars > 0]
        if is_rates:
            mean_is = sum(is_rates) / len(is_rates)
            mean_oos = sum(oos_rates) / len(oos_rates)
            if mean_is > 1e-6:
                wfe_pct = clean_float((mean_oos / mean_is) * 100.0, 2)

    drift_ratio: float | None = None
    if wf_cfg.mode == "PURGED_WFO" and folds:
        if len(folds) == 1:
            drift_ratio = 0.0
        else:
            changes = sum(
                1
                for i in range(1, len(folds))
                if folds[i].selected_params != folds[i - 1].selected_params
            )
            drift_ratio = clean_float(changes / float(len(folds) - 1), 4)

    sample_depth_warning = total_bars < 120 or len(folds) < 2
    warn_msg: str | None = None
    notes: list[str] = []
    if not folds:
        warn_msg = (
            f"Insufficient bar history ({total_bars} bars) for configured walk-forward "
            f"windows (mode={wf_cfg.mode})."
        )
        notes.append(warn_msg)
    elif sample_depth_warning:
        warn_msg = (
            f"Limited walk-forward depth ({total_bars} bars, {len(folds)} fold(s); "
            "recommend >= 120 bars and >= 2 folds)."
        )

    return WalkForwardReport(
        mode=wf_cfg.mode,
        window_type=wf_cfg.window_type,
        objective=wf_cfg.objective,
        folds=folds,
        stitched_oos_curve=stitched_oos_curve,
        stitched_baseline_curve=stitched_base_curve,
        oos_total_return_pct=oos_total_ret,
        baseline_oos_total_return_pct=base_total_ret,
        oos_max_drawdown_pct=oos_max_dd,
        oos_sharpe=oos_sharpe,
        is_avg_return_pct=is_avg_ret,
        oos_avg_return_pct=oos_avg_ret,
        wfe_pct=wfe_pct,
        positive_windows_pct=pos_win_pct,
        parameter_drift_ratio=drift_ratio,
        sample_depth_warning=sample_depth_warning,
        warning_message=warn_msg,
        notes=notes,
    )
