"""Pydantic v2 configuration and report models for the Backtester Robustness Suite."""

from __future__ import annotations

import math
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analytics.backtest.models import BacktestStrategyKind, EquityCurvePoint

RobustnessSection = Literal["COHORTS", "SENSITIVITY", "WALK_FORWARD", "MONTE_CARLO"]
DataSufficiencyTier = Literal["HIGH", "MODERATE", "LIMITED"]
SensitivityAxisParam = Literal[
    "profit_target_pct",
    "stop_loss_pct",
    "exit_dte",
    "dte_bucket",
    "target_delta",
    "min_entry_credit_dollars",
    "min_entry_ror_pct",
]
SensitivityObjective = Literal[
    "expectancy_dollars",
    "return_on_risk_pct",
    "total_return_pct",
    "profit_factor",
    "sharpe",
    "calmar",
    "win_rate_pct",
]
WalkForwardMode = Literal["FIXED_WINDOWS", "PURGED_WFO"]
WalkForwardWindowType = Literal["ROLLING", "ANCHORED"]
MarketRegimeLabel = Literal["BULL", "FLAT", "BEAR", "UNKNOWN"]
VerdictStatus = Literal["PASS", "WARN", "FAIL", "SKIPPED"]


def clean_float(val: float | None, digits: int = 4) -> float | None:
    """Return rounded finite float, else `None`."""
    if val is None or not math.isfinite(val):
        return None
    out = round(float(val), digits)
    return 0.0 if out == 0.0 else out


def finite_float(val: float, digits: int = 4, default: float = 0.0) -> float:
    """Return rounded finite float, falling back to `default` if non-finite."""
    if not math.isfinite(val):
        return default
    out = round(float(val), digits)
    return 0.0 if out == 0.0 else out


# ------------------------------------------------------------------ Configurations
class CohortConfig(BaseModel):
    """Configuration for All-Days spread cohorts and rolling stateful cohorts."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    rolling_horizon_bars: int | None = Field(default=None, ge=20, le=252)
    max_rolling_starts: int = Field(default=40, ge=5, le=60)


class SensitivityConfig(BaseModel):
    """Configuration for 2D parameter sensitivity & plateau stability evaluation."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    x_param: SensitivityAxisParam | None = None
    y_param: SensitivityAxisParam | None = None
    x_values: list[float | str] | None = None
    y_values: list[float | str] | None = None
    grid_density: Literal[5, 7] = 5
    objective: SensitivityObjective = "expectancy_dollars"

    @model_validator(mode="after")
    def _validate_axes_and_grid(self) -> Self:
        if self.x_param is not None and self.y_param is not None and self.x_param == self.y_param:
            raise ValueError(f"x_param and y_param must differ (both were {self.x_param!r}).")
        for label, vals in (("x_values", self.x_values), ("y_values", self.y_values)):
            if vals is not None:
                if len(vals) < 2:
                    raise ValueError(f"{label} must contain at least 2 values.")
                for item in vals:
                    if isinstance(item, (int, float)) and not math.isfinite(float(item)):
                        raise ValueError(f"{label} contains non-finite numeric value.")
        nx = len(self.x_values) if self.x_values is not None else self.grid_density
        ny = len(self.y_values) if self.y_values is not None else self.grid_density
        if nx * ny > 49:
            raise ValueError(f"Sensitivity grid size ({nx} x {ny} = {nx * ny}) exceeds limit 49.")
        return self


class WalkForwardConfig(BaseModel):
    """Configuration for Fixed-Window or Purged Walk-Forward Optimization."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: WalkForwardMode = "PURGED_WFO"
    window_type: WalkForwardWindowType = "ROLLING"
    fixed_window_bars: int = Field(default=40, ge=20, le=120)
    is_bars: int = Field(default=120, ge=40, le=360)
    oos_bars: int = Field(default=40, ge=15, le=120)
    embargo_bars: int = Field(default=0, ge=0, le=30)
    objective: SensitivityObjective = "expectancy_dollars"


class MonteCarloConfig(BaseModel):
    """Configuration for Stationary Block Bootstrap, Trade Bootstrap, and Slippage Stress."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    num_paths: int = Field(default=2000, ge=200, le=5000)
    block_size: int = Field(default=5, ge=1, le=20)
    horizon_bars: int | None = Field(default=None, ge=20, le=504)
    ruin_drawdown_pct: float = Field(default=25.0, gt=0.0, le=95.0)
    seed: int | None = Field(default=42, ge=0, le=2_147_483_647)
    include_trade_bootstrap: bool = True
    include_slippage_stress: bool = True
    slippage_min: float = Field(default=0.0, ge=0.0, le=1.0)
    slippage_max: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_slippage_bounds(self) -> Self:
        if self.slippage_min > self.slippage_max:
            raise ValueError(
                f"slippage_min ({self.slippage_min}) must be <= slippage_max ({self.slippage_max})."
            )
        return self


class RobustnessConfig(BaseModel):
    """Unified configuration for all four sections of the Backtester Robustness Suite."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cohorts: CohortConfig = Field(default_factory=CohortConfig)
    sensitivity: SensitivityConfig = Field(default_factory=SensitivityConfig)
    walk_forward: WalkForwardConfig = Field(default_factory=WalkForwardConfig)
    monte_carlo: MonteCarloConfig = Field(default_factory=MonteCarloConfig)


# ------------------------------------------------------------------ Reports
class WeekdayStabilityRow(BaseModel):
    """Mon-Fri entry-day stability breakdown across cohorts."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    weekday: int = Field(ge=0, le=4)
    weekday_name: str
    attempted_entries: int = Field(ge=0)
    closed_trades: int = Field(ge=0)
    open_trades: int = Field(ge=0)
    win_rate_pct: float | None = None
    expectancy_dollars: float | None = None
    total_net_pnl_dollars: float = 0.0
    avg_return_on_risk_pct: float | None = None
    profit_factor: float | None = None


class RollingStartCohortPoint(BaseModel):
    """One rolling start-date simulation outcome for stateful COVERED_CALL / WHEEL."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    start_idx: int = Field(ge=0)
    start_date: str
    end_date: str
    bars_count: int = Field(ge=1)
    trade_count: int = Field(ge=0)
    closed_trades: int = Field(ge=0)
    total_return_pct: float
    net_pnl_dollars: float
    max_drawdown_pct: float
    win_rate_pct: float | None = None
    expectancy_dollars: float | None = None


class AllDaysCohortReport(BaseModel):
    """All-Days entry-date invariance (spreads) or rolling-start cohorts (CC/Wheel)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: Literal["ALL_DAYS_SPREADS", "ROLLING_START_STATEFUL"]
    total_bars: int = Field(ge=0)
    attempted_entries: int = Field(ge=0)
    entered_cohorts: int = Field(ge=0)
    closed_cohorts: int = Field(ge=0)
    open_cohorts: int = Field(ge=0)
    unresolved_cohorts: int = Field(ge=0)
    skipped_entries_count: int = Field(default=0, ge=0)
    skipped_by_reason: dict[str, int] = Field(default_factory=dict)
    avg_concurrent_positions: float = Field(ge=1.0)
    win_rate_pct: float | None = None
    expectancy_dollars: float | None = None
    avg_return_on_risk_pct: float | None = None
    profit_factor: float | None = None
    total_net_pnl_dollars: float = 0.0
    portfolio_total_return_pct: float = 0.0
    portfolio_max_drawdown_pct: float = 0.0
    portfolio_sharpe: float | None = None
    portfolio_sortino: float | None = None
    portfolio_calmar: float | None = None
    equity_curve: list[EquityCurvePoint] = Field(default_factory=list)
    weekday_stability: list[WeekdayStabilityRow] = Field(default_factory=list)
    weekday_skew_detected: bool = False
    weekday_skew_message: str | None = None
    rolling_cohorts: list[RollingStartCohortPoint] = Field(default_factory=list)
    rolling_return_p10_pct: float | None = None
    rolling_return_p50_pct: float | None = None
    rolling_return_p90_pct: float | None = None
    rolling_max_dd_p50_pct: float | None = None
    rolling_max_dd_p90_pct: float | None = None
    notes: list[str] = Field(default_factory=list)


class SensitivityCell(BaseModel):
    """Single `(x, y)` coordinate evaluation in a 2D sensitivity surface."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    x_index: int = Field(ge=0)
    y_index: int = Field(ge=0)
    x_value: float | str
    y_value: float | str
    x_label: str
    y_label: str
    objective_value: float | None = None
    smoothed_score: float | None = None
    trade_count: int = Field(ge=0)
    closed_trades: int = Field(ge=0)
    win_rate_pct: float | None = None
    expectancy_dollars: float | None = None
    return_on_risk_pct: float | None = None
    total_return_pct: float = 0.0
    profit_factor: float | None = None
    sharpe: float | None = None
    calmar: float | None = None
    max_drawdown_pct: float = 0.0
    is_baseline: bool = False
    is_optimal: bool = False


class SensitivityGridReport(BaseModel):
    """2D parameter sensitivity surface, plateau stability, and Deflated Sharpe Ratio."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    x_param: SensitivityAxisParam
    y_param: SensitivityAxisParam
    objective: SensitivityObjective
    x_values: list[float | str]
    y_values: list[float | str]
    cells: list[SensitivityCell]
    baseline_cell: SensitivityCell | None = None
    optimal_cell: SensitivityCell | None = None
    plateau_stability_score: float = Field(ge=0.0, le=100.0)
    profitable_cells_pct: float = Field(ge=0.0, le=100.0)
    valid_cells_count: int = Field(ge=0)
    total_cells_count: int = Field(ge=1)
    deflated_sharpe_ratio: float | None = None
    multiple_testing_warning: bool = False
    warning_message: str | None = None
    notes: list[str] = Field(default_factory=list)


class WalkForwardFold(BaseModel):
    """One IS->OOS fold or fixed evaluation window in walk-forward analysis."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    fold_index: int = Field(ge=0)
    is_start_date: str
    is_end_date: str
    oos_start_date: str
    oos_end_date: str
    is_bars: int = Field(ge=0)
    oos_bars: int = Field(ge=1)
    regime: MarketRegimeLabel = "UNKNOWN"
    underlying_return_pct: float | None = None
    selected_params: dict[str, float | str] = Field(default_factory=dict)
    is_objective_score: float | None = None
    oos_objective_score: float | None = None
    is_return_pct: float = 0.0
    oos_return_pct: float = 0.0
    baseline_oos_return_pct: float = 0.0
    oos_max_drawdown_pct: float = 0.0
    oos_trades: int = Field(ge=0)
    oos_win_rate_pct: float | None = None
    oos_sharpe: float | None = None


class WalkForwardReport(BaseModel):
    """Walk-forward stitched OOS curve, efficiency ratio, and parameter drift diagnostics."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: WalkForwardMode
    window_type: WalkForwardWindowType
    objective: SensitivityObjective
    folds: list[WalkForwardFold] = Field(default_factory=list)
    stitched_oos_curve: list[EquityCurvePoint] = Field(default_factory=list)
    stitched_baseline_curve: list[EquityCurvePoint] = Field(default_factory=list)
    oos_total_return_pct: float = 0.0
    baseline_oos_total_return_pct: float = 0.0
    oos_max_drawdown_pct: float = 0.0
    oos_sharpe: float | None = None
    is_avg_return_pct: float | None = None
    oos_avg_return_pct: float | None = None
    wfe_pct: float | None = None
    positive_windows_pct: float | None = None
    parameter_drift_ratio: float | None = None
    sample_depth_warning: bool = False
    warning_message: str | None = None
    notes: list[str] = Field(default_factory=list)


class MonteCarloFanSeries(BaseModel):
    """Percentile equity fan chart bands (downsampled to <= 250 steps)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    bar_offsets: list[int]
    p05: list[float]
    p25: list[float]
    p50: list[float]
    p75: list[float]
    p95: list[float]


class HistogramBin(BaseModel):
    """One bin of the simulated path maximum-drawdown distribution."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    bin_start_pct: float
    bin_end_pct: float
    count: int = Field(ge=0)
    frequency_pct: float = Field(ge=0.0, le=100.0)


class SlippageScenarioRow(BaseModel):
    """Re-priced spread cohort performance at a given Mid->Natural fill fraction."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    natural_fraction: float = Field(ge=0.0, le=1.0)
    label: str
    closed_trades: int = Field(ge=0)
    total_net_pnl_dollars: float
    expectancy_dollars: float | None = None
    win_rate_pct: float | None = None
    profit_factor: float | None = None
    total_return_pct: float


class SlippageStressSummary(BaseModel):
    """Mid-to-Natural fill stress summary across closed spread cohorts."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    scenarios: list[SlippageScenarioRow] = Field(default_factory=list)
    mid_expectancy_dollars: float | None = None
    blended_expectancy_dollars: float | None = None
    natural_expectancy_dollars: float | None = None
    breakeven_slippage_fraction: float | None = None
    edge_survives_50pct_slippage: bool | None = None


class MonteCarloReport(BaseModel):
    """Stationary Block Bootstrap, trade-level bootstrap, and slippage stress report."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    num_paths: int = Field(ge=1)
    block_size: int = Field(ge=1)
    horizon_bars: int = Field(ge=1)
    source_bars: int = Field(ge=0)
    ruin_drawdown_pct: float
    seed: int | None = None
    fan_series: MonteCarloFanSeries
    max_dd_histogram: list[HistogramBin] = Field(default_factory=list)
    terminal_return_p05_pct: float
    terminal_return_p25_pct: float
    terminal_return_p50_pct: float
    terminal_return_p75_pct: float
    terminal_return_p95_pct: float
    max_dd_p50_pct: float
    max_dd_p95_pct: float
    max_dd_p99_pct: float
    prob_profit_pct: float = Field(ge=0.0, le=100.0)
    risk_of_ruin_pct: float = Field(ge=0.0, le=100.0)
    cvar_5_return_pct: float
    trade_count: int = Field(default=0, ge=0)
    trade_expectancy_ci_low_dollars: float | None = None
    trade_expectancy_ci_high_dollars: float | None = None
    trade_win_rate_ci_low_pct: float | None = None
    trade_win_rate_ci_high_pct: float | None = None
    max_losing_streak_p95: int | None = None
    slippage_stress: SlippageStressSummary | None = None
    notes: list[str] = Field(default_factory=list)


class VerdictItem(BaseModel):
    """Pass/Warn/Fail signal card for one robustness section."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    section: RobustnessSection
    status: VerdictStatus
    headline: str
    detail: str
    metric_value: float | None = None


class BacktestRobustnessResult(BaseModel):
    """Complete Backtester Robustness Suite output payload."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    ticker: str
    strategy_kind: BacktestStrategyKind
    total_bars: int = Field(ge=0)
    data_sufficiency_tier: DataSufficiencyTier
    requested_sections: list[RobustnessSection]
    cohorts: AllDaysCohortReport | None = None
    sensitivity: SensitivityGridReport | None = None
    walk_forward: WalkForwardReport | None = None
    monte_carlo: MonteCarloReport | None = None
    verdicts: list[VerdictItem] = Field(default_factory=list)
    compute_ms: float = Field(ge=0.0)
    notes: list[str] = Field(default_factory=list)
