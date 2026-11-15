"""Pydantic v2 report models for the Trade Ledger analytics engine."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.storage.ledger_schema import (
    LedgerAssetClass,
    LedgerEventCode,
    LedgerOpenClose,
    LedgerSide,
    LedgerSource,
)

LedgerWindow = Literal["1W", "1M", "3M", "YTD", "1Y", "ALL"]
LedgerGroupBy = Literal["campaigns", "strategy", "ticker", "executions"]
LedgerView = Literal["realized", "open_mtm"]

LedgerStrategyType = Literal[
    "BULL_PUT_SPREAD",
    "BEAR_CALL_SPREAD",
    "BULL_CALL_SPREAD",
    "BEAR_PUT_SPREAD",
    "IRON_CONDOR",
    "IRON_BUTTERFLY",
    "DIAGONAL_SPREAD",
    "CALENDAR_SPREAD",
    "STRANGLE",
    "STRADDLE",
    "COVERED_CALL",
    "NAKED_CALL",
    "SHORT_PUT",
    "LONG_CALL",
    "LONG_PUT",
    "CUSTOM_COMBO",
    "STOCK_LOT",
]

RollAction = Literal["OPEN", "ADD", "TRIM", "CLOSE", "ROLL", "EXPIRE", "ASSIGN", "EXERCISE"]
CampaignStatus = Literal["OPEN", "CLOSED", "ASSIGNED", "EXPIRED"]
MarkSource = Literal["LIVE_GATEWAY", "FLEX_MARK", "NONE"]


class RollStep(BaseModel):
    """Single chronological action or roll hop within a trade campaign."""

    model_config = ConfigDict(extra="forbid")

    step_index: int
    executed_at: str
    action: RollAction
    roll_label: str | None = None
    summary: str
    expiry: str | None = None
    strike_summary: str
    contracts: float
    cashflow_ex_fee: float
    realized_pnl_ex_fee: float
    fees: float
    ib_order_id: str | None = None


class LedgerCampaignRow(BaseModel):
    """Grouped strategy / roll-chain campaign row (View 1 primary unit)."""

    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    underlying: str
    strategy_type: LedgerStrategyType
    status: CampaignStatus
    opened_at: str
    last_activity_at: str
    roll_count: int
    initial_summary: str
    current_summary: str
    contracts_total: float
    contracts_open: float
    net_premium_ex_fee: float
    realized_pnl_ex_fee: float
    stock_realized_pnl_ex_fee: float = 0.0
    unrealized_mtm_ex_fee: float | None = None
    fees: float
    steps: list[RollStep] = Field(default_factory=list)


class LedgerBreakdownRow(BaseModel):
    """Strategy-only or Ticker-only aggregate performance row."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    trade_count: int
    roll_count: int
    win_count: int
    loss_count: int
    win_rate_pct: float | None
    net_premium_ex_fee: float
    realized_pnl_ex_fee: float
    stock_realized_pnl_ex_fee: float = 0.0
    fees: float


class LedgerOpenPositionView(BaseModel):
    """Open position & unrealized mark-to-market valuation row (View 2)."""

    model_config = ConfigDict(extra="forbid")

    position_key: str
    underlying: str
    asset_class: LedgerAssetClass
    strategy_type: LedgerStrategyType
    symbol: str
    expiry: str | None = None
    strike: float | None = None
    right: Literal["C", "P"] | None = None
    quantity: float
    avg_cost_price: float
    mark_price: float | None = None
    mark_source: MarkSource
    cost_basis_ex_fee: float
    market_value: float | None = None
    unrealized_pnl_ex_fee: float | None = None
    fees: float
    campaign_id: str | None = None
    conid: int | None = None
    opened_at: str | None = None


class LedgerExecutionView(BaseModel):
    """Normalized execution row for `group_by='executions'` inspection."""

    model_config = ConfigDict(extra="forbid")

    exec_id: str
    ib_order_id: str | None = None
    executed_at: str
    underlying: str
    asset_class: LedgerAssetClass
    symbol: str
    expiry: str | None = None
    strike: float | None = None
    right: Literal["C", "P"] | None = None
    side: LedgerSide
    quantity: float
    price: float
    proceeds_ex_fee: float
    commission: float
    realized_pnl_ex_fee: float
    open_close: LedgerOpenClose | None = None
    event_code: LedgerEventCode
    strategy_type: LedgerStrategyType
    campaign_id: str | None = None
    source: LedgerSource | str


class LedgerChartPoint(BaseModel):
    """Daily cumulative P&L and option premium series point."""

    model_config = ConfigDict(extra="forbid")

    date: str
    label: str
    cumulative_realized_ex_fee: float
    cumulative_premium_ex_fee: float
    period_pnl_ex_fee: float
    fees: float


class LedgerKPIs(BaseModel):
    """Top-line Trade Ledger KPI summary strictly separating closed P&L and MTM."""

    model_config = ConfigDict(extra="forbid")

    realized_pnl_ex_fee: float
    stock_pnl_ex_fee: float
    total_pnl_ex_fee: float
    net_premium_ex_fee: float
    fees: float
    win_rate_pct: float | None
    closed_count: int
    open_count: int
    roll_count: int
    unrealized_mtm_ex_fee: float | None


class LedgerAnalyticsReport(BaseModel):
    """Complete Trade Ledger analytics payload returned by `build_ledger_report`."""

    model_config = ConfigDict(extra="forbid")

    as_of: str
    window: LedgerWindow
    window_start: str | None = None
    ticker: str
    group_by: LedgerGroupBy
    view: LedgerView
    include_stock_gain: bool
    available_tickers: list[str] = Field(default_factory=list)
    kpis: LedgerKPIs
    campaigns: list[LedgerCampaignRow] = Field(default_factory=list)
    breakdowns: list[LedgerBreakdownRow] = Field(default_factory=list)
    executions: list[LedgerExecutionView] = Field(default_factory=list)
    open_positions: list[LedgerOpenPositionView] = Field(default_factory=list)
    chart_points: list[LedgerChartPoint] = Field(default_factory=list)
