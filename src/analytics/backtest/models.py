"""Pydantic v2 configuration, step, trade, equity curve, and summary models for backtesting."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analytics.bsm_fallback import DEFAULT_RISK_FREE_RATE
from src.analytics.strategies.models import EvaluatedLeg, LegSide, StrategyKind
from src.storage.schema import OptionRight, SpotSource

FLAG_LEG_FALLBACK_PRICE: Final = "LEG_FALLBACK_PRICE"
FLAG_NO_LAST_PRINT: Final = "NO_LAST_PRINT"
FLAG_TRACKED_LEG_USED: Final = "TRACKED_LEG_USED"
FLAG_MISSING_CONTRACT: Final = "MISSING_CONTRACT"
FLAG_MISSING_PRICE: Final = "MISSING_PRICE"
FLAG_UNRESOLVED_EXPIRY_SPOT: Final = "UNRESOLVED_EXPIRY_SPOT"
FLAG_UNRESOLVED_EXPIRY_LEG: Final = "UNRESOLVED_EXPIRY_LEG"
FLAG_LATE_EXPIRY_SETTLEMENT: Final = "LATE_EXPIRY_SETTLEMENT"
FLAG_NO_USABLE_DAILY_SNAPSHOT: Final = "NO_USABLE_DAILY_SNAPSHOT"
FLAG_LOW_QUOTE_COVERAGE: Final = "LOW_QUOTE_COVERAGE"
FLAG_CLOSE_SPOT_ONLY: Final = "CLOSE_SPOT_ONLY"
FLAG_FROZEN_MARKET_DATA: Final = "FROZEN_MARKET_DATA"
FLAG_SNAPSHOT_META_MISSING: Final = "SNAPSHOT_META_MISSING"
FLAG_EMPTY_CHAIN: Final = "EMPTY_CHAIN"


def deduplicate_flags(flags: Sequence[str]) -> list[str]:
    """Preserve order while removing duplicate flag strings."""
    return list(dict.fromkeys(flags))


BacktestMode = Literal["SYSTEMATIC", "SINGLE_TRADE"]
PriceBasis = Literal["AUTO_MID_LAST_CLOSE", "LAST_CLOSE_ONLY"]
ClosePriceMode = Literal["MID", "NATURAL_SPREAD"]
TpSlBasis = Literal["CREDIT_DEBIT_PCT", "MAX_RISK_PCT"]
EntryCadence = Literal["WEEKLY_DAY", "EVERY_N_DAYS", "WHEN_FLAT"]
ExitReason = Literal["PROFIT_TARGET", "STOP_LOSS", "EXIT_DTE", "EXPIRY_SETTLED", "END_OF_DATA"]
StepStatus = Literal["OK", "UNRELIABLE", "INCOMPLETE"]
StepMarkSource = Literal[
    "MID",
    "LAST",
    "CLOSE",
    "NATURAL_SPREAD",
    "NATURAL_FALLBACK",
    "MIXED",
    "TRACKED",
    "PARTIAL_SETTLEMENT",
    "SETTLEMENT",
    "MISSING",
]
TradeStatus = Literal["CLOSED", "OPEN", "UNRESOLVED"]

LegPriceSource = Literal["MID", "LAST", "CLOSE", "BID", "ASK", "INTRINSIC", "NO_PRICE"]
LegQuoteSource = Literal["CHAIN", "TRACKED", "SETTLEMENT", "NONE"]
LegMarkStatus = Literal[
    "OK", "FALLBACK_PRICE", "MISSING_CONTRACT", "MISSING_PRICE", "SETTLED", "UNRESOLVED"
]


class BacktestConfig(BaseModel):
    """User-configurable parameters governing a deterministic daily options backtest."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    strategy_kind: StrategyKind = "BULL_PUT_SPREAD"
    target_dte_min: int = Field(default=30, ge=0, le=120)
    target_dte_max: int = Field(default=60, ge=0, le=120)
    entry_cadence: EntryCadence = "WEEKLY_DAY"
    entry_weekday: int = Field(default=4, ge=0, le=4)
    entry_every_n_days: int = Field(default=5, ge=1, le=60)
    initial_capital: float = Field(default=10000.0, gt=0.0)
    max_open_trades: int = Field(default=1, ge=1, le=20)
    qty: int = Field(default=1, ge=1, le=100)
    commission_per_contract: float = Field(default=0.65, ge=0.0, le=50.0)
    price_basis: PriceBasis = "AUTO_MID_LAST_CLOSE"
    close_price_mode: ClosePriceMode = "MID"
    tp_sl_basis: TpSlBasis = "CREDIT_DEBIT_PCT"
    profit_target_pct: float | None = Field(default=50.0, gt=0.0, le=1000.0)
    stop_loss_pct: float | None = Field(default=100.0, gt=0.0, le=2000.0)
    exit_dte: int | None = Field(default=None, ge=0, le=120)
    risk_free_rate: float = Field(default=DEFAULT_RISK_FREE_RATE, ge=0.0, le=1.0)
    walls_top_n: int = Field(default=3, ge=1, le=20)
    min_entry_credit_dollars: float | None = Field(
        default=100.0,
        ge=0.0,
        description=(
            "Minimum net credit in dollars per 1-spread baseline (credit_per_share * 100, "
            "independent of qty) for auto-built BULL_PUT_SPREAD entries; 0.0 or None disables."
        ),
    )
    min_entry_ror_pct: float | None = Field(
        default=None,
        ge=0.0,
        le=1000.0,
        description=(
            "Minimum entry Return on Risk percentage (net_credit / max_loss * 100) for "
            "auto-built BULL_PUT_SPREAD entries; 0.0 or None disables."
        ),
    )

    @model_validator(mode="after")
    def _validate_dte_window(self) -> Self:
        if self.target_dte_min > self.target_dte_max:
            raise ValueError(
                f"target_dte_min ({self.target_dte_min}) must be <= "
                f"target_dte_max ({self.target_dte_max})"
            )
        return self


class BacktestLegMark(BaseModel):
    """Per-leg mark valuation and provenance for one daily backtest bar."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    side: LegSide
    right: OptionRight
    expiry: str
    strike: float
    qty: int
    price_used: float | None = None
    natural_price: float | None = None
    price_source: LegPriceSource
    quote_source: LegQuoteSource
    status: LegMarkStatus
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


class TradeDailyStep(BaseModel):
    """Single daily mark-to-market / settlement snapshot of an active backtest trade."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    market_date: str
    snapshot_id: str
    ts_utc: str
    ts_et: str
    dte_min_leg: int
    spot: float | None = None
    spot_source: SpotSource | None = None
    market_data_type: int | None = None
    quote_coverage: float | None = None
    legs: list[BacktestLegMark]
    mark_value_dollars: float | None = None
    natural_close_value_dollars: float | None = None
    gross_pnl_dollars: float | None = None
    net_pnl_dollars: float | None = None
    pnl_pct_of_basis: float | None = None
    quote_missing: bool = False
    mark_source: StepMarkSource | None = None
    status: StepStatus
    flags: list[str] = Field(default_factory=list)
    tracked_snapshot_ids: list[str] = Field(default_factory=list)


class BacktestTradeRecord(BaseModel):
    """Complete trade lifecycle record from entry snapshot through exit or end of data."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    trade_id: str
    entry_date: str
    entry_snapshot_id: str
    exit_date: str
    exit_snapshot_id: str
    exit_reason: ExitReason
    strategy_kind: StrategyKind
    legs: list[EvaluatedLeg]
    qty: int
    entry_cash_mid_dollars: float
    entry_cash_natural_dollars: float
    entry_basis_dollars: float
    max_profit_dollars: float | None = None
    max_loss_dollars: float | None = None
    max_loss_unbounded: bool = False
    profit_target_dollars: float | None = None
    stop_loss_dollars: float | None = None
    exit_value_dollars: float | None = None
    commissions_dollars: float
    gross_pnl_dollars: float | None = None
    net_pnl_dollars: float | None = None
    return_on_basis_pct: float | None = None
    return_on_risk_pct: float | None = None
    days_held: int
    status: TradeStatus
    selection_notes: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    steps: list[TradeDailyStep] = Field(default_factory=list)


class EquityCurvePoint(BaseModel):
    """Portfolio equity, realized/unrealized P&L, and drawdown on one daily bar."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    market_date: str
    snapshot_id: str
    ts_utc: str
    equity_dollars: float
    realized_pnl_dollars: float
    unrealized_pnl_dollars: float
    drawdown_dollars: float
    drawdown_pct: float
    open_trades: int
    underlying_price: float | None = None
    flags: list[str] = Field(default_factory=list)


class PortfolioSummary(BaseModel):
    """Aggregate portfolio performance KPIs (`avg_loss_dollars` is signed negative P&L)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    initial_capital: float
    final_equity: float
    gross_pnl_dollars: float
    net_pnl_dollars: float
    realized_pnl_dollars: float = 0.0
    unrealized_pnl_dollars: float = 0.0
    total_commissions_dollars: float
    total_return_pct: float
    trade_count: int
    closed_trades: int
    open_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float | None = None
    max_drawdown_dollars: float
    max_drawdown_pct: float
    profit_factor: float | None = None
    expectancy_dollars: float | None = None
    avg_win_dollars: float | None = None
    avg_loss_dollars: float | None = None
    ok_bars: int
    flagged_bars: int
    skipped_entries_count: int = 0
    skipped_by_reason: dict[str, int] = Field(default_factory=dict)


class BacktestSimulationResult(BaseModel):
    """Full output of a systematic backtest or single-trade replay."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    ticker: str
    mode: BacktestMode
    config: BacktestConfig
    summary: PortfolioSummary
    equity_curve: list[EquityCurvePoint] = Field(default_factory=list)
    trades: list[BacktestTradeRecord] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
