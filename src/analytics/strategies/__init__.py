"""Strategy Lab engine (pure Polars + NumPy/BSM over cached option chains).

- `models`: Pydantic request/response models.
- `chain`: row preparation and lookup helpers.
- `builders`: Wall-anchored preset leg construction.
- `engine`: `evaluate_strategy` pricing, risk and payoff curves.
"""

from __future__ import annotations

from src.analytics.strategies.builders import (
    StrategyFilterError,
    StrategySuggestionOutcome,
    suggest_strategy_legs_from_walls,
    try_suggest_strategy_legs_from_walls,
)
from src.analytics.strategies.chain import EntryPriceBasis
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
    "EntryPriceBasis",
    "EvaluatedLeg",
    "LegSide",
    "PRESET_STRATEGIES",
    "PositionGreeks",
    "PresetStrategyKind",
    "StrategyEvaluationResponse",
    "StrategyFilterError",
    "StrategyKind",
    "StrategyLegInput",
    "StrategyPayoffSeries",
    "StrategyPricing",
    "StrategyRiskMetrics",
    "StrategySuggestionOutcome",
    "evaluate_strategy",
    "suggest_strategy_legs_from_walls",
    "try_suggest_strategy_legs_from_walls",
]
