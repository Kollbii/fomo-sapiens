"""Unified Backtester Robustness Suite orchestrator, tier classifier, and 4-signal verdicts."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Final

from src.analytics.backtest.cohorts import (
    CohortRunOutput,
    SpreadCohortMatrix,
    run_cohort_analysis,
)
from src.analytics.backtest.fast_cache import FastBarCache
from src.analytics.backtest.models import BacktestConfig, deduplicate_flags
from src.analytics.backtest.monte_carlo import run_monte_carlo_analysis
from src.analytics.backtest.robustness_models import (
    AllDaysCohortReport,
    BacktestRobustnessResult,
    DataSufficiencyTier,
    MonteCarloReport,
    RobustnessConfig,
    RobustnessSection,
    SensitivityGridReport,
    VerdictItem,
    VerdictStatus,
    WalkForwardReport,
    clean_float,
    finite_float,
)
from src.analytics.backtest.sensitivity import run_sensitivity_grid
from src.analytics.backtest.walk_forward import run_walk_forward_analysis
from src.core.exceptions import BacktestValidationError
from src.core.security import TickerValidator
from src.storage.backtest_store import DailyChainBar

ALL_ROBUSTNESS_SECTIONS: Final[tuple[RobustnessSection, ...]] = (
    "COHORTS",
    "SENSITIVITY",
    "WALK_FORWARD",
    "MONTE_CARLO",
)


def classify_data_sufficiency(total_bars: int) -> DataSufficiencyTier:
    """Classify sample depth as `HIGH` (>=180), `MODERATE` (60..179), or `LIMITED` (<60)."""
    if total_bars >= 180:
        return "HIGH"
    if total_bars >= 60:
        return "MODERATE"
    return "LIMITED"


def _cohort_verdict(rep: AllDaysCohortReport | None) -> VerdictItem:
    if rep is None:
        return VerdictItem(
            section="COHORTS",
            status="SKIPPED",
            headline="Cohorts not requested",
            detail="Section excluded from robustness run.",
        )
    if rep.closed_cohorts == 0:
        return VerdictItem(
            section="COHORTS",
            status="WARN",
            headline="No closed cohorts",
            detail="No cohorts reached exit or expiration in the available window.",
        )
    exp = rep.expectancy_dollars or 0.0
    if rep.mode == "ROLLING_START_STATEFUL" and rep.rolling_cohorts:
        p50 = rep.rolling_return_p50_pct or 0.0
        status: VerdictStatus = (
            "PASS"
            if (p50 > 0.0 and exp > 0.0 and not rep.weekday_skew_detected)
            else ("WARN" if (p50 > 0.0 or exp > 0.0) else "FAIL")
        )
        return VerdictItem(
            section="COHORTS",
            status=status,
            headline=f"Rolling median {p50:+.2f}%",
            detail=(
                f"{len(rep.rolling_cohorts)} rolling start windows "
                f"(P10 {rep.rolling_return_p10_pct:+.2f}% .. "
                f"P90 {rep.rolling_return_p90_pct:+.2f}%)."
                if rep.rolling_return_p10_pct is not None and rep.rolling_return_p90_pct is not None
                else f"Expectancy ${exp:+.2f} over {rep.closed_cohorts} closed trades."
            ),
            metric_value=clean_float(p50, 2),
        )

    if exp > 0.0 and not rep.weekday_skew_detected:
        status = "PASS"
        headline = f"Time-invariant +${exp:.2f}/trade"
    elif exp > 0.0:
        status = "WARN"
        headline = f"Weekday-skewed +${exp:.2f}/trade"
    else:
        status = "FAIL"
        headline = f"Negative cohort edge ${exp:+.2f}/trade"
    detail = (
        rep.weekday_skew_message
        if rep.weekday_skew_message
        else (
            f"{rep.closed_cohorts} closed cohorts across all entry weekdays "
            f"(WR {rep.win_rate_pct or 0.0:.1f}%)."
        )
    )
    return VerdictItem(
        section="COHORTS",
        status=status,
        headline=headline,
        detail=detail,
        metric_value=clean_float(exp, 2),
    )


def _sensitivity_verdict(rep: SensitivityGridReport | None) -> VerdictItem:
    if rep is None:
        return VerdictItem(
            section="SENSITIVITY",
            status="SKIPPED",
            headline="Sensitivity not requested",
            detail="Section excluded from robustness run.",
        )
    if rep.valid_cells_count == 0:
        return VerdictItem(
            section="SENSITIVITY",
            status="WARN",
            headline="No valid grid cells",
            detail="No parameter combination produced closed trades.",
            metric_value=0.0,
        )
    score = rep.plateau_stability_score
    if score >= 60.0 and rep.profitable_cells_pct >= 50.0 and not rep.multiple_testing_warning:
        status: VerdictStatus = "PASS"
        headline = f"Stable plateau ({score:.0f}/100)"
    elif score >= 35.0 or rep.profitable_cells_pct >= 35.0:
        status = "WARN"
        headline = f"Moderate plateau ({score:.0f}/100)"
    else:
        status = "FAIL"
        headline = f"Fragile surface ({score:.0f}/100)"
    dsr_txt = (
        f", DSR {rep.deflated_sharpe_ratio:.2f}" if rep.deflated_sharpe_ratio is not None else ""
    )
    return VerdictItem(
        section="SENSITIVITY",
        status=status,
        headline=headline,
        detail=(
            f"{rep.profitable_cells_pct:.1f}% profitable cells across "
            f"{rep.total_cells_count} trials{dsr_txt}."
        ),
        metric_value=clean_float(score, 2),
    )


def _walk_forward_verdict(rep: WalkForwardReport | None) -> VerdictItem:
    if rep is None:
        return VerdictItem(
            section="WALK_FORWARD",
            status="SKIPPED",
            headline="Walk-Forward not requested",
            detail="Section excluded from robustness run.",
        )
    if not rep.folds:
        return VerdictItem(
            section="WALK_FORWARD",
            status="WARN",
            headline="Insufficient bars for WFO",
            detail=rep.warning_message or "Short history prevented walk-forward folding.",
        )
    pos_pct = rep.positive_windows_pct or 0.0
    if rep.oos_total_return_pct > 0.0 and pos_pct >= 50.0 and not rep.sample_depth_warning:
        status: VerdictStatus = "PASS"
        headline = f"OOS {rep.oos_total_return_pct:+.2f}% ({pos_pct:.0f}% pos)"
    elif rep.oos_total_return_pct > 0.0 or pos_pct >= 50.0:
        status = "WARN"
        headline = f"Mixed OOS {rep.oos_total_return_pct:+.2f}%"
    else:
        status = "FAIL"
        headline = f"Negative OOS {rep.oos_total_return_pct:+.2f}%"
    wfe_txt = f", WFE {rep.wfe_pct:.1f}%" if rep.wfe_pct is not None else ""
    return VerdictItem(
        section="WALK_FORWARD",
        status=status,
        headline=headline,
        detail=(
            f"{len(rep.folds)} fold(s), baseline OOS "
            f"{rep.baseline_oos_total_return_pct:+.2f}%{wfe_txt}."
        ),
        metric_value=clean_float(rep.oos_total_return_pct, 2),
    )


def _monte_carlo_verdict(rep: MonteCarloReport | None) -> VerdictItem:
    if rep is None:
        return VerdictItem(
            section="MONTE_CARLO",
            status="SKIPPED",
            headline="Monte Carlo not requested",
            detail="Section excluded from robustness run.",
        )
    if rep.source_bars < 2:
        return VerdictItem(
            section="MONTE_CARLO",
            status="WARN",
            headline="Insufficient return history",
            detail="Need at least 2 daily return bars for block bootstrap simulation.",
        )
    slip_ok = (
        rep.slippage_stress.edge_survives_50pct_slippage is not False
        if rep.slippage_stress is not None
        else True
    )
    if rep.prob_profit_pct >= 60.0 and rep.risk_of_ruin_pct <= 15.0 and slip_ok:
        status: VerdictStatus = "PASS"
        headline = f"P(Profit) {rep.prob_profit_pct:.1f}% / Ruin {rep.risk_of_ruin_pct:.1f}%"
    elif rep.prob_profit_pct >= 45.0 and rep.risk_of_ruin_pct <= 35.0:
        status = "WARN"
        headline = f"Marginal MC {rep.prob_profit_pct:.1f}% / Ruin {rep.risk_of_ruin_pct:.1f}%"
    else:
        status = "FAIL"
        headline = f"Elevated risk ({rep.risk_of_ruin_pct:.1f}% ruin)"
    return VerdictItem(
        section="MONTE_CARLO",
        status=status,
        headline=headline,
        detail=(
            f"Median terminal {rep.terminal_return_p50_pct:+.2f}%, "
            f"P95 MaxDD {rep.max_dd_p95_pct:.1f}%, CVaR 5% {rep.cvar_5_return_pct:+.2f}%."
        ),
        metric_value=clean_float(rep.prob_profit_pct, 2),
    )


def run_robustness_suite(
    bars: Sequence[DailyChainBar],
    config: BacktestConfig,
    *,
    ticker: str,
    robustness_config: RobustnessConfig | None = None,
    sections: Sequence[RobustnessSection] | None = None,
    fast_cache: FastBarCache | None = None,
) -> BacktestRobustnessResult:
    """Run requested sections of the Backtester Robustness Suite over `bars`."""
    t0 = time.perf_counter()
    symbol = TickerValidator.validate(ticker)
    if config.strategy_kind == "CUSTOM":
        raise BacktestValidationError(
            "Backtester Robustness Suite does not support CUSTOM strategy kind."
        )

    r_cfg = robustness_config or RobustnessConfig()
    if sections is None:
        requested: list[RobustnessSection] = list(ALL_ROBUSTNESS_SECTIONS)
    else:
        seen: set[RobustnessSection] = set()
        requested = []
        for sec in sections:
            if sec not in seen:
                seen.add(sec)
                requested.append(sec)

    cache = fast_cache if fast_cache is not None else FastBarCache.from_bars(bars)
    total_bars = len(cache)
    tier = classify_data_sufficiency(total_bars)
    notes: list[str] = []
    if tier == "LIMITED":
        notes.append(
            f"Limited sample depth ({total_bars} bars < 60); statistical confidence is reduced."
        )

    want_cohorts = "COHORTS" in requested
    want_sensitivity = "SENSITIVITY" in requested
    want_wfo = "WALK_FORWARD" in requested
    want_mc = "MONTE_CARLO" in requested

    matrix_cache: dict[tuple[object, ...], SpreadCohortMatrix] = {}
    cohort_out: CohortRunOutput | None = None
    if want_cohorts or want_mc:
        cohort_out = run_cohort_analysis(
            cache,
            config,
            ticker=symbol,
            cohort_config=r_cfg.cohorts,
            include_rolling=want_cohorts,
            matrix_cache=matrix_cache,
        )

    cohorts_rep = cohort_out.report if (want_cohorts and cohort_out is not None) else None

    sensitivity_rep: SensitivityGridReport | None = None
    if want_sensitivity:
        sensitivity_rep, _opt = run_sensitivity_grid(
            cache,
            config,
            r_cfg.sensitivity,
            ticker=symbol,
            matrix_cache=matrix_cache,
        )

    wf_rep: WalkForwardReport | None = None
    if want_wfo:
        wf_rep = run_walk_forward_analysis(
            cache,
            config,
            r_cfg.walk_forward,
            ticker=symbol,
            sensitivity_config=r_cfg.sensitivity,
            matrix_cache=matrix_cache,
        )

    mc_rep: MonteCarloReport | None = None
    if want_mc and cohort_out is not None:
        mc_rep = run_monte_carlo_analysis(
            cohort_out.daily_returns,
            cohort_out.closed_trade_pnls,
            initial_capital=config.initial_capital,
            mc_config=r_cfg.monte_carlo,
            slippage_pairs=cohort_out.slippage_pairs,
        )

    verdicts = [
        _cohort_verdict(cohorts_rep),
        _sensitivity_verdict(sensitivity_rep),
        _walk_forward_verdict(wf_rep),
        _monte_carlo_verdict(mc_rep),
    ]
    compute_ms = finite_float((time.perf_counter() - t0) * 1000.0, 2)

    return BacktestRobustnessResult(
        ticker=symbol,
        strategy_kind=config.strategy_kind,
        total_bars=total_bars,
        data_sufficiency_tier=tier,
        requested_sections=requested,
        cohorts=cohorts_rep,
        sensitivity=sensitivity_rep,
        walk_forward=wf_rep,
        monte_carlo=mc_rep,
        verdicts=verdicts,
        compute_ms=compute_ms,
        notes=deduplicate_flags(notes),
    )
