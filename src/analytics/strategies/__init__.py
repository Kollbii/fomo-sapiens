"""Strategy Lab engine (pure Polars + NumPy/BSM over cached option chains).

- `models`: Pydantic request/response models.
- `chain`: row preparation and lookup helpers.
- `builders`: Wall-anchored preset leg construction.
- `engine`: `evaluate_strategy` pricing, risk and payoff curves.
"""

from __future__ import annotations

from src.analytics.strategies.builders import suggest_strategy_legs_from_walls
from src.analytics.strategies.engine import evaluate_strategy
from src.analytics.strategies.models import (
    PRESET_STRATEGIES,
    EvaluatedLeg,
    LegSide,
    PositionGreeks,
    PresetStrategyKind,
    StrategyEvaluationResponse,
    StrategyKind,
    StrategyLegInput,
    StrategyPayoffSeries,
    StrategyPricing,
    StrategyRiskMetrics,
)

__all__ = [
    "EvaluatedLeg",
    "LegSide",
    "PRESET_STRATEGIES",
    "PositionGreeks",
    "PresetStrategyKind",
    "StrategyEvaluationResponse",
    "StrategyKind",
    "StrategyLegInput",
    "StrategyPayoffSeries",
    "StrategyPricing",
    "StrategyRiskMetrics",
    "evaluate_strategy",
    "suggest_strategy_legs_from_walls",
]
