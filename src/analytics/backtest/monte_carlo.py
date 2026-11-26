"""Stationary Block Bootstrap, Trade Bootstrap CI, and Mid-to-Natural Slippage Stress."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from src.analytics.backtest.robustness_models import (
    HistogramBin,
    MonteCarloConfig,
    MonteCarloFanSeries,
    MonteCarloReport,
    SlippageScenarioRow,
    SlippageStressSummary,
    clean_float,
    finite_float,
)
from src.analytics.backtest.rules import DENOM_EPS

FloatArray = npt.NDArray[np.float64]


def _scenario_fractions(slippage_min: float, slippage_max: float) -> list[float]:
    anchors = [slippage_min, 0.25, 0.50, 0.75, slippage_max]
    filtered = sorted(
        {round(u, 4) for u in anchors if slippage_min - 1e-9 <= u <= slippage_max + 1e-9}
    )
    return filtered or [round(slippage_min, 4)]


def _slippage_label(u: float) -> str:
    if abs(u) <= 1e-6:
        return "Mid (0%)"
    if abs(u - 1.0) <= 1e-6:
        return "Natural (100%)"
    return f"Blended ({u * 100.0:.0f}% Nat)"


def _compute_slippage_stress(
    slippage_pairs: Sequence[tuple[float, float, float, float, float, float]],
    *,
    initial_capital: float,
    slippage_min: float,
    slippage_max: float,
) -> SlippageStressSummary:
    n_trades = len(slippage_pairs)
    arr = np.array(slippage_pairs, dtype=np.float64)
    mid_gross = arr[:, 0] + arr[:, 2]
    nat_gross = arr[:, 1] + arr[:, 3]
    comms = arr[:, 4] + arr[:, 5]

    def _eval_at(u: float) -> SlippageScenarioRow:
        pnls = ((1.0 - u) * mid_gross + u * nat_gross) - comms
        wins = pnls[pnls > 0.0]
        losses = pnls[pnls < 0.0]
        sum_w = float(np.sum(wins)) if len(wins) else 0.0
        sum_l_abs = float(np.sum(-losses)) if len(losses) else 0.0
        total_pnl = float(np.sum(pnls))
        pf = clean_float(sum_w / sum_l_abs, 4) if sum_l_abs > DENOM_EPS else None
        return SlippageScenarioRow(
            natural_fraction=finite_float(u, 4),
            label=_slippage_label(u),
            closed_trades=n_trades,
            total_net_pnl_dollars=finite_float(total_pnl, 2),
            expectancy_dollars=clean_float(total_pnl / float(n_trades), 2),
            win_rate_pct=clean_float((len(wins) / float(n_trades)) * 100.0, 2),
            profit_factor=pf,
            total_return_pct=finite_float((total_pnl / initial_capital) * 100.0, 2),
        )

    scenarios = [_eval_at(u) for u in _scenario_fractions(slippage_min, slippage_max)]
    row_mid = _eval_at(0.0)
    row_50 = _eval_at(0.5)
    row_nat = _eval_at(1.0)

    p0 = row_mid.total_net_pnl_dollars
    p1 = row_nat.total_net_pnl_dollars
    breakeven_u: float | None
    if p0 <= 0.0:
        breakeven_u = 0.0
    elif p1 > 0.0:
        breakeven_u = None
    elif abs(p0 - p1) > DENOM_EPS:
        breakeven_u = clean_float(min(1.0, max(0.0, p0 / (p0 - p1))), 4)
    else:
        breakeven_u = None

    return SlippageStressSummary(
        scenarios=scenarios,
        mid_expectancy_dollars=row_mid.expectancy_dollars,
        blended_expectancy_dollars=row_50.expectancy_dollars,
        natural_expectancy_dollars=row_nat.expectancy_dollars,
        breakeven_slippage_fraction=breakeven_u,
        edge_survives_50pct_slippage=(
            row_50.expectancy_dollars > 0.0 if row_50.expectancy_dollars is not None else None
        ),
    )


def _max_losing_streak_p95(boot_pnls: FloatArray) -> int:
    loss_mask = boot_pnls < 0.0
    n_paths, n_cols = loss_mask.shape
    cur = np.zeros(n_paths, dtype=np.int64)
    best = np.zeros(n_paths, dtype=np.int64)
    for col in range(n_cols):
        col_loss = loss_mask[:, col]
        cur = np.where(col_loss, cur + 1, 0)
        best = np.maximum(best, cur)
    return int(round(float(np.percentile(best, 95.0))))


def run_monte_carlo_analysis(
    daily_returns: FloatArray,
    closed_trade_pnls: FloatArray,
    *,
    initial_capital: float,
    mc_config: MonteCarloConfig | None = None,
    slippage_pairs: Sequence[tuple[float, float, float, float, float, float]] | None = None,
) -> MonteCarloReport:
    """Run Stationary Block Bootstrap, Trade Bootstrap CIs, and Mid<->Natural Slippage Stress."""
    mc_cfg = mc_config or MonteCarloConfig()
    n_ret = len(daily_returns)
    horizon = mc_cfg.horizon_bars if mc_cfg.horizon_bars is not None else max(20, n_ret)
    num_paths = mc_cfg.num_paths

    rng = np.random.default_rng(mc_cfg.seed)
    notes: list[str] = []

    if n_ret < 2:
        notes.append(
            f"Insufficient daily return observations ({n_ret}) for block bootstrap; "
            "returning flat projection."
        )
        col_idx = (
            np.arange(horizon + 1, dtype=np.int64)
            if horizon + 1 <= 250
            else np.unique(np.round(np.linspace(0, horizon, 250)).astype(np.int64))
        )
        flat = [finite_float(initial_capital, 2)] * len(col_idx)
        fan = MonteCarloFanSeries(
            bar_offsets=[int(i) for i in col_idx],
            p05=flat,
            p25=list(flat),
            p50=list(flat),
            p75=list(flat),
            p95=list(flat),
        )
        early_slippage = (
            _compute_slippage_stress(
                slippage_pairs,
                initial_capital=initial_capital,
                slippage_min=mc_cfg.slippage_min,
                slippage_max=mc_cfg.slippage_max,
            )
            if (mc_cfg.include_slippage_stress and slippage_pairs)
            else None
        )
        return MonteCarloReport(
            num_paths=num_paths,
            block_size=mc_cfg.block_size,
            horizon_bars=horizon,
            source_bars=n_ret,
            ruin_drawdown_pct=mc_cfg.ruin_drawdown_pct,
            seed=mc_cfg.seed,
            fan_series=fan,
            max_dd_histogram=[],
            terminal_return_p05_pct=0.0,
            terminal_return_p25_pct=0.0,
            terminal_return_p50_pct=0.0,
            terminal_return_p75_pct=0.0,
            terminal_return_p95_pct=0.0,
            max_dd_p50_pct=0.0,
            max_dd_p95_pct=0.0,
            max_dd_p99_pct=0.0,
            prob_profit_pct=0.0,
            risk_of_ruin_pct=0.0,
            cvar_5_return_pct=0.0,
            trade_count=len(closed_trade_pnls),
            slippage_stress=early_slippage,
            notes=notes,
        )

    # Politis & Romano Stationary Block Bootstrap (fully vectorized)
    p_new = 1.0 / float(mc_cfg.block_size)
    starts = rng.integers(0, n_ret, size=(num_paths, horizon))
    is_new = rng.random(size=(num_paths, horizon)) < p_new
    is_new[:, 0] = True
    step_idx = np.arange(horizon, dtype=np.int64)
    last_reset = np.maximum.accumulate(np.where(is_new, step_idx[None, :], 0), axis=1)
    offsets = step_idx[None, :] - last_reset
    start_vals = np.take_along_axis(starts, last_reset, axis=1)
    indices = (start_vals + offsets) % n_ret

    clean_r = np.where(np.isfinite(daily_returns), np.maximum(-0.9999, daily_returns), 0.0)
    sampled_r = clean_r[indices]
    log_factors = np.log1p(sampled_r)
    cum_wealth = initial_capital * np.exp(np.cumsum(log_factors, axis=1))
    wealth_with_0 = np.concatenate(
        [np.full((num_paths, 1), initial_capital, dtype=np.float64), cum_wealth], axis=1
    )

    peaks = np.maximum.accumulate(wealth_with_0, axis=1)
    dd_pct_paths = ((peaks - wealth_with_0) / np.where(peaks > 1e-9, peaks, 1.0)) * 100.0
    path_max_dd = np.max(dd_pct_paths, axis=1)
    term_ret_pct = ((cum_wealth[:, -1] / initial_capital) - 1.0) * 100.0

    # Downsample fan series to <= 250 steps
    if horizon + 1 <= 250:
        col_idx = np.arange(horizon + 1, dtype=np.int64)
    else:
        col_idx = np.unique(np.round(np.linspace(0, horizon, 250)).astype(np.int64))
    fan_q = np.percentile(wealth_with_0[:, col_idx], [5.0, 25.0, 50.0, 75.0, 95.0], axis=0)
    fan = MonteCarloFanSeries(
        bar_offsets=[int(i) for i in col_idx],
        p05=[finite_float(float(v), 2) for v in fan_q[0]],
        p25=[finite_float(float(v), 2) for v in fan_q[1]],
        p50=[finite_float(float(v), 2) for v in fan_q[2]],
        p75=[finite_float(float(v), 2) for v in fan_q[3]],
        p95=[finite_float(float(v), 2) for v in fan_q[4]],
    )

    hist_counts, hist_edges = np.histogram(path_max_dd, bins=12)
    histogram = [
        HistogramBin(
            bin_start_pct=finite_float(float(hist_edges[i]), 2),
            bin_end_pct=finite_float(float(hist_edges[i + 1]), 2),
            count=int(hist_counts[i]),
            frequency_pct=finite_float((int(hist_counts[i]) / float(num_paths)) * 100.0, 2),
        )
        for i in range(len(hist_counts))
    ]

    ret_q = np.percentile(term_ret_pct, [5.0, 25.0, 50.0, 75.0, 95.0])
    dd_q = np.percentile(path_max_dd, [50.0, 95.0, 99.0])
    prob_profit = finite_float(float(np.mean(term_ret_pct > 0.0) * 100.0), 2)
    risk_ruin = finite_float(float(np.mean(path_max_dd >= mc_cfg.ruin_drawdown_pct) * 100.0), 2)

    cutoff_5 = float(np.percentile(term_ret_pct, 5.0))
    tail_5 = term_ret_pct[term_ret_pct <= cutoff_5]
    cvar_5 = finite_float(float(np.mean(tail_5)) if len(tail_5) else cutoff_5, 2)

    exp_lo: float | None = None
    exp_hi: float | None = None
    wr_lo: float | None = None
    wr_hi: float | None = None
    streak_p95: int | None = None
    n_trades = len(closed_trade_pnls)
    if mc_cfg.include_trade_bootstrap:
        if n_trades >= 2:
            t_idx = rng.integers(0, n_trades, size=(num_paths, n_trades))
            boot_pnls = closed_trade_pnls[t_idx]
            exp_paths = np.mean(boot_pnls, axis=1)
            wr_paths = np.mean(boot_pnls > 0.0, axis=1) * 100.0
            exp_ci = np.percentile(exp_paths, [2.5, 97.5])
            wr_ci = np.percentile(wr_paths, [2.5, 97.5])
            exp_lo = clean_float(float(exp_ci[0]), 2)
            exp_hi = clean_float(float(exp_ci[1]), 2)
            wr_lo = clean_float(float(wr_ci[0]), 2)
            wr_hi = clean_float(float(wr_ci[1]), 2)
            streak_p95 = _max_losing_streak_p95(boot_pnls)
        else:
            notes.append(
                f"Need at least 2 closed trades for trade-level bootstrap CIs (got {n_trades})."
            )

    slippage_summary: SlippageStressSummary | None = None
    if mc_cfg.include_slippage_stress:
        if slippage_pairs:
            slippage_summary = _compute_slippage_stress(
                slippage_pairs,
                initial_capital=initial_capital,
                slippage_min=mc_cfg.slippage_min,
                slippage_max=mc_cfg.slippage_max,
            )
        else:
            notes.append("Slippage stress skipped (no closed spread cohort fill pairs available).")

    # Guard against any edge rounding pushing prob_profit / risk_ruin above 100.0
    if not math.isfinite(prob_profit):
        prob_profit = 0.0
    if not math.isfinite(risk_ruin):
        risk_ruin = 0.0

    return MonteCarloReport(
        num_paths=num_paths,
        block_size=mc_cfg.block_size,
        horizon_bars=horizon,
        source_bars=n_ret,
        ruin_drawdown_pct=mc_cfg.ruin_drawdown_pct,
        seed=mc_cfg.seed,
        fan_series=fan,
        max_dd_histogram=histogram,
        terminal_return_p05_pct=finite_float(float(ret_q[0]), 2),
        terminal_return_p25_pct=finite_float(float(ret_q[1]), 2),
        terminal_return_p50_pct=finite_float(float(ret_q[2]), 2),
        terminal_return_p75_pct=finite_float(float(ret_q[3]), 2),
        terminal_return_p95_pct=finite_float(float(ret_q[4]), 2),
        max_dd_p50_pct=finite_float(float(dd_q[0]), 2),
        max_dd_p95_pct=finite_float(float(dd_q[1]), 2),
        max_dd_p99_pct=finite_float(float(dd_q[2]), 2),
        prob_profit_pct=min(100.0, max(0.0, prob_profit)),
        risk_of_ruin_pct=min(100.0, max(0.0, risk_ruin)),
        cvar_5_return_pct=cvar_5,
        trade_count=n_trades,
        trade_expectancy_ci_low_dollars=exp_lo,
        trade_expectancy_ci_high_dollars=exp_hi,
        trade_win_rate_ci_low_pct=wr_lo,
        trade_win_rate_ci_high_pct=wr_hi,
        max_losing_streak_p95=streak_p95,
        slippage_stress=slippage_summary,
        notes=notes,
    )
