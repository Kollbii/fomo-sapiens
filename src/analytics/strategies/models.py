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
