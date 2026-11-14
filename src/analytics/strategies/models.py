"""Strategy Lab request/response models and strategy kind literals."""

from __future__ import annotations

from typing import Literal

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field

from src.storage.schema import OptionRight

FloatArray = npt.NDArray[np.float64]

StrategyKind = Literal[
    "BULL_PUT_SPREAD",
    "BULL_CALL_SPREAD",
    "DIAGONAL_SPREAD",
    "IRON_CONDOR",
    "BEAR_CALL_SPREAD",
    "BEAR_PUT_SPREAD",
    "CUSTOM",
]

PresetStrategyKind = Literal[
    "BULL_PUT_SPREAD",
    "BULL_CALL_SPREAD",
    "DIAGONAL_SPREAD",
    "IRON_CONDOR",
    "BEAR_CALL_SPREAD",
    "BEAR_PUT_SPREAD",
]

LegSide = Literal["BUY", "SELL"]

# Autobuild / screener order (Strategy Lab suggest evaluates presets in this order).
PRESET_STRATEGIES: tuple[PresetStrategyKind, ...] = (
    "BULL_PUT_SPREAD",
    "BULL_CALL_SPREAD",
    "DIAGONAL_SPREAD",
    "IRON_CONDOR",
    "BEAR_CALL_SPREAD",
    "BEAR_PUT_SPREAD",
)

# Why `repick_strategy_legs` kept the current legs (`None` on the result when it adjusted).
RepickKeepReason = Literal[
    "NO_BETTER_CANDIDATE",
    "BETTER_CANDIDATE_FILTERED",
    "NO_ALTERNATIVE_IN_WINDOW",
    "CURRENT_ROR_UNAVAILABLE",
]


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
    """Risk/reward bounds, breakevens, POP, EV/risk, and 1-sigma expected move envelope.

    `max_profit` and `max_loss` are non-negative dollar magnitudes for the whole position
    (leg qty x `qty_multiplier` x 100) read off the short-expiry P&L curve: a loss is reported as
    a positive `max_loss`, never as a negative number. `return_on_risk_pct` is
    `max_profit / max_loss x 100`; `expected_return_on_risk_pct` (EV/risk) is
    `(POP x max_profit - (1 - POP) x max_loss) / max_loss x 100` with POP as a fraction;
    `nearest_breakeven_distance_pct` is `min |BE - spot| / spot x 100`. The two newest fields
    default to `None` so payloads built before they existed stay valid.
    """

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
    expected_return_on_risk_pct: float | None = None
    nearest_breakeven_distance_pct: float | None = None


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


class StrategyCandidateFilters(BaseModel):
    """Entry floors checked against an evaluated candidate's reported metrics.

    Each floor is off when `None` or `0.0` (same convention as the builders and
    `AnalyzerService._normalize_strategy_filters`) and applies to every structure:
    - `min_net_credit_dollars`: per-unit Mid net credit `pricing.mid_cash_total / qty_multiplier`
      (one spread when legs carry qty 1); a debit (negative credit) never meets an active floor.
    - `min_ror_pct`: `risk.return_on_risk_pct` must be reported (not `None`) and `>=` the floor.
    - `min_pop_pct`: `risk.prob_of_profit_pct` must be reported and `>=` the floor.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    min_net_credit_dollars: float | None = Field(default=None, ge=0.0)
    min_ror_pct: float | None = Field(default=None, ge=0.0)
    min_pop_pct: float | None = Field(default=None, ge=0.0, le=100.0)


class StrategyRepickResult(BaseModel):
    """Outcome of one live re-pick: the legs to show now plus their metrics-only evaluation.

    `adjusted` is `True` only when this call replaced `current_legs` with a strictly higher-RoR
    neighbour that passes the filters; otherwise `keep_reason` says why the current legs stayed.
    `candidates_evaluated` counts priced, structurally valid alternatives (current excluded).
    """

    model_config = ConfigDict(extra="forbid")

    strategy_kind: PresetStrategyKind
    underlying: str
    underlying_price: float
    adjusted: bool
    keep_reason: RepickKeepReason | None
    current_passes_filters: bool
    candidates_evaluated: int
    legs: list[StrategyLegInput]
    evaluated_legs: list[EvaluatedLeg]
    pricing: StrategyPricing
    risk: StrategyRiskMetrics
    net_greeks: PositionGreeks
