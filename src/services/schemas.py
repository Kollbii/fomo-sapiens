"""Request/response models of the service layer (also the public HTTP API contract).

`src/web/` imports every model it serves from here, including the broker/analytics models
re-exported below, so the web layer never depends on lower layers directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analytics.anomalies import AnomaliesReport
from src.analytics.backtest import (
    BacktestConfig,
    BacktestLegMark,
    BacktestMode,
    BacktestSimulationResult,
    BacktestTradeRecord,
    ClosePriceMode,
    EntryCadence,
    EquityCurvePoint,
    ExitReason,
    LegMarkStatus,
    LegPriceSource,
    LegQuoteSource,
    PortfolioSummary,
    PriceBasis,
    StepStatus,
    TpSlBasis,
    TradeDailyStep,
    TradeStatus,
)
from src.analytics.ledger import (
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
    RollStep,
)
from src.analytics.strategies import (
    PresetStrategyKind,
    StrategyEvaluationResponse,
    StrategyKind,
    StrategyLegInput,
)
from src.analytics.term_greeks import MultiDTEProfilesReport
from src.analytics.walls import ChainWallsReport
from src.broker.corridor import (
    CORRIDOR_MAX_CONTRACTS,
    DEFAULT_CORRIDOR_MAX_CONTRACTS,
    DEFAULT_WINDOW_STRIKES,
    CorridorLegRef,
    CorridorQuoteFrame,
)
from src.storage.paper_trade_store import (
    MAX_PAPER_TRADE_LEGS,
    PaperQuoteRecord,
    PaperTrade,
    PaperTradeLeg,
    PaperTradeStatus,
)
from src.storage.schema import SnapshotMeta, SpotSource

__all__ = [
    "BacktestConfig",
    "BacktestEntriesResponse",
    "BacktestEntry",
    "BacktestLegMark",
    "BacktestMode",
    "BacktestRunRequest",
    "BacktestRunResponse",
    "BacktestSimulationResult",
    "BacktestSingleTradeSource",
    "BacktestTradeRecord",
    "ChainAnalyticsResponse",
    "ChainMetadataPayload",
    "ClosePriceMode",
    "CorridorLegRef",
    "CorridorQuoteFrame",
    "CorridorQuotesRequest",
    "CorridorStopResponse",
    "EntryCadence",
    "EquityCurvePoint",
    "ExitReason",
    "GatewayStatusResponse",
    "LedgerAnalyticsReport",
    "LedgerBreakdownRow",
    "LedgerCampaignRow",
    "LedgerChartPoint",
    "LedgerDashboardResponse",
    "LedgerExecutionView",
    "LedgerGroupBy",
    "LedgerImportResponse",
    "LedgerKPIs",
    "LedgerMTMRefreshResponse",
    "LedgerOpenPositionView",
    "LedgerStatusSummary",
    "LedgerStrategyType",
    "LedgerSyncMode",
    "LedgerSyncRequest",
    "LedgerSyncResponse",
    "LedgerView",
    "LedgerWindow",
    "LegMarkStatus",
    "LegPriceSource",
    "LegQuoteSource",
    "MAX_LEDGER_IMPORT_BYTES",
    "MAX_PAPER_TRADE_LEGS",
    "PaperQuoteRecord",
    "PaperTrade",
    "PaperTradeLeg",
    "PaperTradeStatus",
    "PaperTradesListResponse",
    "PortfolioSummary",
    "PriceBasis",
    "RefreshRequest",
    "RegisterPaperTradeRequest",
    "RollStep",
    "SnapshotsListResponse",
    "StepStatus",
    "StrategyEvaluateRequest",
    "StrategyEvaluationResponse",
    "StrategySuggestRequest",
    "StrategySuggestResponse",
    "SystemStatusResponse",
    "TpSlBasis",
    "TradeDailyStep",
    "TradeStatus",
]


class GatewayStatusResponse(BaseModel):
    """Response model for `GET /api/status`."""

    model_config = ConfigDict(extra="forbid")

    connected: bool
    host: str
    port: int
    client_id: int
    market_data_mode: Literal["DELAYED", "LIVE"]
    default_market_data_type: int
    fallback_market_data_type: int = 4
    active_market_data_type: int
    live_subscription_missing: bool = False
    default_ticker: str
    watchlist: list[str]
    allow_custom_ticker: bool
    default_range_pct: float
    allowed_range_pcts: list[float]
    default_strike_step: float | None = None
    allowed_strike_steps: list[float]
    dte_min: int = 30
    dte_max: int = 120
    include_short_dte_for_backtest: bool = True
    available_strike_steps: list[float] = Field(default_factory=list)
    available_range_pcts: list[float] = Field(default_factory=list)
    active_capabilities_ticker: str | None = None
    flex_configured: bool = False
    flex_monthly_configured: bool = False
    flex_trade_conf_configured: bool = False
    gateway_ledger_sync_enabled: bool = False
    corridor_default_window_strikes: int = Field(default=DEFAULT_WINDOW_STRIKES, ge=1, le=15)
    corridor_default_max_contracts: int = Field(
        default=DEFAULT_CORRIDOR_MAX_CONTRACTS, ge=1, le=CORRIDOR_MAX_CONTRACTS
    )
    corridor_hard_max_contracts: int = Field(
        default=CORRIDOR_MAX_CONTRACTS, ge=1, le=CORRIDOR_MAX_CONTRACTS
    )


SystemStatusResponse = GatewayStatusResponse


class SnapshotsListResponse(BaseModel):
    """Response model for `GET /api/snapshots/{ticker}`."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    latest_snapshot_id: str | None
    snapshots: list[SnapshotMeta]


class ChainMetadataPayload(BaseModel):
    """Snapshot-level metadata included in `ChainAnalyticsResponse`."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    snapshot_ts_utc: str
    market_date: str
    underlying: str
    underlying_conid: int
    underlying_price: float
    market_data_type: int
    contract_count: int
    expirations: list[str]
    dtes: list[int]
    range_pct: float | None = None
    strike_step: float | None = None
    requested_strike_step: float | None = None
    exchange_validated_strike_count: int | None = None
    exchange_native_verified: bool = False
    available_strike_steps: list[float] = Field(default_factory=list)
    available_range_pcts: list[float] = Field(default_factory=list)
    spot_source: SpotSource | None = None
    quoted_contract_count: int | None = None
    quote_coverage: float | None = None
    prior_close_only_count: int | None = None
    greeks_source_counts: dict[str, int]
    source: Literal["CACHE", "LIVE_REFRESH"]
    notices: list[str] = Field(default_factory=list)


class ChainAnalyticsResponse(BaseModel):
    """Unified analytics payload returned by `GET /api/chain/{ticker}` and `POST /api/refresh`."""

    model_config = ConfigDict(extra="forbid")

    meta: ChainMetadataPayload
    walls: ChainWallsReport
    anomalies: AnomaliesReport
    profiles: MultiDTEProfilesReport
    contracts: list[dict[str, Any]]


class RefreshRequest(BaseModel):
    """Request body for `POST /api/refresh`."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    range_pct: float = Field(default=0.20, gt=0.0, le=1.0)
    strike_step: float | None = Field(default=None, gt=0.0)
    dte_min: int | None = Field(default=None, ge=0, le=120)
    dte_max: int | None = Field(default=None, ge=1, le=120)
    include_short_dte: bool | None = None
    display_dte_min: int | None = Field(default=None, ge=0, le=120)
    display_dte_max: int | None = Field(default=None, ge=1, le=120)

    @model_validator(mode="after")
    def _validate_dte_bounds(self) -> RefreshRequest:
        if self.dte_min is not None and self.dte_max is not None and self.dte_min > self.dte_max:
            raise ValueError("dte_min must be <= dte_max")
        if (
            self.display_dte_min is not None
            and self.display_dte_max is not None
            and self.display_dte_min > self.display_dte_max
        ):
            raise ValueError("display_dte_min must be <= display_dte_max")
        return self


class StrategySuggestRequest(BaseModel):
    """Request body for `POST /api/strategy/suggest`."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    ticker: str
    snapshot_id: str | None = None
    strategy_kind: PresetStrategyKind | None = None
    target_expiry: str | None = None
    dte_min: int | None = Field(default=None, ge=0, le=120)
    dte_max: int | None = Field(default=None, ge=1, le=120)
    days_forward: int = Field(default=0, ge=0, le=365)
    iv_shift: float = Field(default=0.0, ge=-0.80, le=2.00)
    qty_multiplier: int = Field(default=1, ge=1, le=100)
    min_net_credit_dollars: float | None = Field(default=None, ge=0.0)
    min_ror_pct: float | None = Field(default=None, ge=0.0, le=1000.0)

    @model_validator(mode="after")
    def _validate_dte_bounds(self) -> StrategySuggestRequest:
        if self.dte_min is not None and self.dte_max is not None and self.dte_min > self.dte_max:
            raise ValueError("dte_min must be <= dte_max")
        return self


class StrategyEvaluateRequest(BaseModel):
    """Request body for `POST /api/strategy/evaluate`."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    ticker: str
    snapshot_id: str | None = None
    strategy_kind: StrategyKind = "BULL_PUT_SPREAD"
    target_expiry: str | None = None
    legs: list[StrategyLegInput] | None = None
    dte_min: int | None = Field(default=None, ge=0, le=120)
    dte_max: int | None = Field(default=None, ge=1, le=120)
    days_forward: int = Field(default=0, ge=0, le=365)
    iv_shift: float = Field(default=0.0, ge=-0.80, le=2.00)
    qty_multiplier: int = Field(default=1, ge=1, le=100)
    min_net_credit_dollars: float | None = Field(default=None, ge=0.0)
    min_ror_pct: float | None = Field(default=None, ge=0.0, le=1000.0)

    @model_validator(mode="after")
    def _validate_dte_bounds(self) -> StrategyEvaluateRequest:
        if self.dte_min is not None and self.dte_max is not None and self.dte_min > self.dte_max:
            raise ValueError("dte_min must be <= dte_max")
        return self


class StrategySuggestResponse(BaseModel):
    """Response body for `POST /api/strategy/suggest`."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    snapshot_id: str
    evaluations: list[StrategyEvaluationResponse]


class CorridorQuotesRequest(BaseModel):
    """Request body for `POST /api/corridor/quotes`."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    snapshot_id: str | None = None
    target_expiry: str | None = None
    window_strikes: int = Field(default=DEFAULT_WINDOW_STRIKES, ge=1, le=15)
    max_contracts: int = Field(
        default=DEFAULT_CORRIDOR_MAX_CONTRACTS, ge=1, le=CORRIDOR_MAX_CONTRACTS
    )
    priority_legs: list[CorridorLegRef] = Field(default_factory=list)


class CorridorStopResponse(BaseModel):
    """Response body for `POST /api/corridor/stop`."""

    model_config = ConfigDict(extra="forbid")

    stopped_subscriptions: int
    active_streams: int


MAX_LEDGER_IMPORT_BYTES = 20 * 1024 * 1024

LedgerSyncMode = Literal["auto", "monthly", "full"]


class LedgerSyncRequest(BaseModel):
    """Request body for `POST /api/ledger/sync`."""

    model_config = ConfigDict(extra="forbid")

    mode: LedgerSyncMode = "auto"
    include_gateway: bool | None = None


class LedgerStatusSummary(BaseModel):
    """Safe non-secret configuration and sync status summary for the Trade Ledger."""

    model_config = ConfigDict(extra="forbid")

    flex_configured: bool
    flex_monthly_configured: bool
    flex_trade_conf_configured: bool
    gateway_sync_enabled: bool
    recommended_sync_mode: Literal["monthly", "full"]
    last_synced_at: str | None = None
    last_sync_mode: str | None = None
    execution_count: int = 0
    open_position_count: int = 0


class LedgerSyncResponse(BaseModel):
    """Response body for `POST /api/ledger/sync`."""

    model_config = ConfigDict(extra="forbid")

    mode_used: str
    queries_run: list[str]
    gateway_synced: bool
    gateway_note: str | None = None
    executions_inserted: int
    executions_updated: int
    total_executions: int
    open_positions_count: int
    synced_at: str
    notices: list[str] = Field(default_factory=list)


class LedgerImportResponse(BaseModel):
    """Response body for `POST /api/ledger/import`."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    file_format: Literal["XML", "CSV"]
    executions_inserted: int
    executions_updated: int
    open_positions_imported: int
    total_executions: int
    notices: list[str] = Field(default_factory=list)


class LedgerMTMRefreshResponse(BaseModel):
    """Response body for `POST /api/ledger/mtm/refresh`."""

    model_config = ConfigDict(extra="forbid")

    refreshed_count: int
    skipped_count: int
    marks_by_conid: dict[int, float]
    updated_at: str
    notices: list[str] = Field(default_factory=list)


class LedgerDashboardResponse(BaseModel):
    """Response body for `GET /api/ledger`."""

    model_config = ConfigDict(extra="forbid")

    report: LedgerAnalyticsReport
    status: LedgerStatusSummary


BacktestSingleTradeSource = Literal["LEGS", "AUTO_BUILD", "PAPER_TRADE"]


class BacktestEntry(BaseModel):
    """One archived snapshot formatted as a candidate backtest entry bar."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    ts_utc: str
    ts_et: str
    ts_poland: str
    contract_count: int
    expirations: list[str]
    quote_coverage: float | None = None
    spot_source: SpotSource | None = None
    market_data_type: int
    usable: bool
    quality_reasons: list[str] = Field(default_factory=list)


class BacktestEntriesResponse(BaseModel):
    """Response body for `GET /api/backtest/entries/{ticker}`."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    entries: list[BacktestEntry]


class PaperTradesListResponse(BaseModel):
    """Response body for `GET /api/paper-trades`."""

    model_config = ConfigDict(extra="forbid")

    active_count: int
    max_active_trades: int
    trades: list[PaperTrade]


class RegisterPaperTradeRequest(BaseModel):
    """Request body for `POST /api/paper-trades`."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ticker: str
    snapshot_id: str
    strategy_kind: StrategyKind = "CUSTOM"
    legs: list[StrategyLegInput] = Field(min_length=1, max_length=MAX_PAPER_TRADE_LEGS)
    qty: int = Field(default=1, ge=1, le=100)


class BacktestRunRequest(BaseModel):
    """Request body for `POST /api/backtest/run` (supports nested or flat `BacktestConfig`)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: BacktestMode = "SYSTEMATIC"
    ticker: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    source: BacktestSingleTradeSource | None = None
    entry_snapshot_id: str | None = None
    end_snapshot_id: str | None = None
    legs: list[StrategyLegInput] | None = Field(
        default=None, min_length=1, max_length=MAX_PAPER_TRADE_LEGS
    )
    paper_trade_id: str | None = None
    config: BacktestConfig = Field(default_factory=BacktestConfig)

    @model_validator(mode="before")
    @classmethod
    def _fold_flat_config(cls, data: Any) -> Any:
        """Accept either nested `config={...}` or flat `BacktestConfig` keys at top level."""
        if not isinstance(data, Mapping):
            return data
        raw = dict(data)
        config_keys = BacktestConfig.model_fields
        request_keys = cls.model_fields
        flat_cfg = {
            key: raw.pop(key) for key in list(raw) if key in config_keys and key not in request_keys
        }
        if not flat_cfg:
            return raw
        existing = raw.get("config")
        if existing is None:
            raw["config"] = flat_cfg
        elif isinstance(existing, BacktestConfig):
            merged = existing.model_dump()
            merged.update(flat_cfg)
            raw["config"] = merged
        elif isinstance(existing, Mapping):
            merged = dict(existing)
            merged.update(flat_cfg)
            raw["config"] = merged
        return raw

    @model_validator(mode="after")
    def _validate_date_bounds(self) -> BacktestRunRequest:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError(
                f"start_date ({self.start_date.isoformat()}) must be <= "
                f"end_date ({self.end_date.isoformat()})"
            )
        return self


class BacktestRunResponse(BaseModel):
    """Response body for `POST /api/backtest/run`."""

    model_config = ConfigDict(extra="forbid")

    mode: BacktestMode
    source: str | None = None
    paper_trade_id: str | None = None
    simulation: BacktestSimulationResult
    matching_paper_trade: PaperTrade | None = None
    history_snapshot_count: int
    daily_bar_count: int
    warnings: list[str] = Field(default_factory=list)
