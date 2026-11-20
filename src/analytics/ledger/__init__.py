"""Pure Polars Trade Ledger analytics package (classification, FIFO/rolls, MTM)."""

from __future__ import annotations

from src.analytics.ledger.classifier import (
    AggregatedLeg,
    NormalizedExecution,
    NormalizedPosition,
    classify_option_legs,
    cluster_option_episodes,
)
from src.analytics.ledger.engine import (
    CompiledLedgerData,
    build_ledger_report,
    compile_ledger_data,
    compute_window_start,
)
from src.analytics.ledger.models import (
    CampaignStatus,
    LedgerAnalyticsReport,
    LedgerBreakdownRow,
    LedgerCampaignRow,
    LedgerChartPoint,
    LedgerExecutionView,
    LedgerGroupBy,
    LedgerKPIs,
    LedgerOpenPositionView,
    LedgerStrategyType,
    LedgerView,
    LedgerWindow,
    MarkSource,
    RollAction,
    RollStep,
)
from src.analytics.ledger.rolls import compute_roll_label

__all__ = [
    "AggregatedLeg",
    "CampaignStatus",
    "CompiledLedgerData",
    "LedgerAnalyticsReport",
    "LedgerBreakdownRow",
    "LedgerCampaignRow",
    "LedgerChartPoint",
    "LedgerExecutionView",
    "LedgerGroupBy",
    "LedgerKPIs",
    "LedgerOpenPositionView",
    "LedgerStrategyType",
    "LedgerView",
    "LedgerWindow",
    "MarkSource",
    "NormalizedExecution",
    "NormalizedPosition",
    "RollAction",
    "RollStep",
    "build_ledger_report",
    "classify_option_legs",
    "cluster_option_episodes",
    "compile_ledger_data",
    "compute_roll_label",
    "compute_window_start",
]
