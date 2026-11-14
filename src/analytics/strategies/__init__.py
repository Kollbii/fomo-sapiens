"""Strategy Lab engine (pure Polars + NumPy/BSM over cached option chains).

- `models`: Pydantic request/response models, strategy kinds and re-pick filters/results.
- `chain`: row preparation and lookup helpers.
- `builders`: Wall-anchored preset leg construction (bull/bear verticals, condor, diagonal).
- `engine`: `evaluate_strategy` pricing, risk (RoR, EV/risk, break-even distance) and curves.
- `repick`: metrics-only live re-pick of preset legs inside a fixed +-1-strike quote window.
"""

from __future__ import annotations

from src.analytics.strategies.builders import (
    StrategyFilterError,
    StrategySuggestionOutcome,
    suggest_strategy_legs_from_walls,
    try_suggest_strategy_legs_from_walls,
)
from src.analytics.strategies.chain import EntryPriceBasis
from src.analytics.strategies.engine import (
    compute_expected_return_on_risk_pct,
    compute_nearest_breakeven_distance_pct,
    evaluate_strategy,
)
from src.analytics.strategies.models import (
    PRESET_STRATEGIES,
    EvaluatedLeg,
    LegSide,
    PositionGreeks,
    PresetStrategyKind,
    RepickKeepReason,
    StrategyCandidateFilters,
    StrategyEvaluationResponse,
    StrategyKind,
    StrategyLegInput,
    StrategyPayoffSeries,
    StrategyPricing,
    StrategyRepickResult,
    StrategyRiskMetrics,
)
from src.analytics.strategies.repick import (
    candidate_passes_filters,
    repick_strategy_legs,
    repick_window_strikes,
)

__all__ = [
    "EntryPriceBasis",
    "EvaluatedLeg",
    "LegSide",
    "PRESET_STRATEGIES",
    "PositionGreeks",
    "PresetStrategyKind",
    "RepickKeepReason",
    "StrategyCandidateFilters",
    "StrategyEvaluationResponse",
    "StrategyFilterError",
    "StrategyKind",
    "StrategyLegInput",
    "StrategyPayoffSeries",
    "StrategyPricing",
    "StrategyRepickResult",
    "StrategyRiskMetrics",
    "StrategySuggestionOutcome",
    "candidate_passes_filters",
    "compute_expected_return_on_risk_pct",
    "compute_nearest_breakeven_distance_pct",
    "evaluate_strategy",
    "repick_strategy_legs",
    "repick_window_strikes",
    "suggest_strategy_legs_from_walls",
    "try_suggest_strategy_legs_from_walls",
]
