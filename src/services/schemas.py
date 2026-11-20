"""Request/response models of the service layer (also the public HTTP API contract).

`src/web/` imports every model it serves from here, including the broker/analytics models
re-exported below, so the web layer never depends on lower layers directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from src.analytics.anomalies import AnomaliesReport
from src.analytics.backtest import (
    BacktestConfig,
    BacktestLegMark,
    BacktestMode,
    BacktestSimulationResult,
    BacktestStrategyKind,
    BacktestTradeRecord,
    CcItmMode,
    CcStrikeFloorMode,
    ClosePriceMode,
    EntryCadence,
    EquityCurvePoint,
    ExitReason,
    LegMarkStatus,
    LegPriceSource,
    LegQuoteSource,
    PortfolioSummary,
    PriceBasis,
    RollCreditMode,
    StepStatus,
    StockStopMode,
    StrikeSelectionMode,
    TpSlBasis,
    TradeDailyStep,
    TradeStatus,
    WheelPhase,
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
    EvaluatedLeg,
    PositionGreeks,
    PresetStrategyKind,
    RepickKeepReason,
    StrategyEvaluationResponse,
    StrategyKind,
    StrategyLegInput,
    StrategyPricing,
    StrategyRiskMetrics,
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
from src.core.config import WATCHLIST_MAX_SIZE_LIMIT
from src.storage.paper_trade_store import (
    MAX_PAPER_TRADE_LEGS,
    PaperQuoteRecord,
    PaperTrade,
    PaperTradeLeg,
    PaperTradeStatus,
)
from src.storage.schema import PartialReason, SnapshotMeta, SpotSource

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
    "BacktestStrategyKind",
    "BacktestTradeRecord",
    "BatchRefreshRequest",
    "BatchRefreshStatus",
    "BatchState",
    "BatchTickerResult",
    "BatchTickerState",
    "CcItmMode",
    "CcStrikeFloorMode",
    "ChainAnalyticsResponse",
    "ChainMetadataPayload",
    "ClosePriceMode",
    "CorridorLegRef",
    "CorridorQuoteFrame",
    "CorridorQuotesRequest",
    "CorridorStopResponse",
    "EntryCadence",
    "EquityCurvePoint",
    "EvaluatedLeg",
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
    "MAX_TICKER_INPUT_CHARS",
    "PaperQuoteRecord",
    "PaperTrade",
    "PaperTradeLeg",
    "PaperTradeStatus",
    "PaperTradesLedgerSyncResponse",
    "PaperTradesListResponse",
    "PortfolioSummary",
    "PositionGreeks",
    "PriceBasis",
    "RefreshRequest",
    "RegisterPaperTradeRequest",
    "RepickKeepReason",
    "RollCreditMode",
    "RollStep",
    "SCREENER_LIVE_MAX_ROWS",
    "ScreenerFilterName",
    "ScreenerLiveFrame",
    "ScreenerLiveReallocateRequest",
    "ScreenerLiveReallocateResponse",
    "ScreenerLiveRequest",
    "ScreenerLiveRow",
    "ScreenerLiveRowAllocation",
    "ScreenerLiveStatus",
    "ScreenerLiveStopReason",
    "ScreenerLiveStopResponse",
    "ScreenerMode",
    "ScreenerParams",
    "ScreenerRow",
    "ScreenerRowId",
    "ScreenerRowStatus",
    "ScreenerRunRequest",
    "ScreenerRunResponse",
    "ScreenerStatusCounts",
    "SnapshotsListResponse",
    "StepStatus",
    "StockStopMode",
    "StrategyEvaluateRequest",
    "StrategyEvaluationResponse",
    "StrategyPricing",
    "StrategyRiskMetrics",
    "StrategySuggestRequest",
    "StrategySuggestResponse",
    "StrikeSelectionMode",
    "SystemStatusResponse",
    "TpSlBasis",
    "TradeDailyStep",
    "TradeStatus",
    "WatchlistEntry",
    "WatchlistResponse",
    "WatchlistTickerRequest",
    "WheelPhase",
]


class GatewayStatusResponse(BaseModel):
    """Response model for `GET /api/status`."""

    model_config = ConfigDict(extra="forbid")

    connected: bool
    connection_state: Literal["connected", "timeout_reset", "connection_lost", "offline"] = (
        "offline"
    )
    connection_notice: str | None = None
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
    """Unified analytics payload returned by `GET /api/chain/{ticker}` and `POST /api/refresh`.

    `partial` / `partial_reason` mirror the loaded snapshot's index entry: True when it is a
    partial snapshot (an interrupted refresh that archived only its finished pages).
    """

    model_config = ConfigDict(extra="forbid")

    meta: ChainMetadataPayload
    walls: ChainWallsReport
    anomalies: AnomaliesReport
    profiles: MultiDTEProfilesReport
    contracts: list[dict[str, Any]]
    partial: bool = False
    partial_reason: PartialReason | None = None


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


class PaperTradesLedgerSyncResponse(BaseModel):
    """Response body for syncing Trade Ledger open positions into Paper Trades."""

    model_config = ConfigDict(extra="forbid")

    created_count: int
    updated_count: int
    closed_count: int
    active_ledger_count: int
    active_count: int
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


# ------------------------------------------------------------------ watchlist & batch refresh
MAX_TICKER_INPUT_CHARS = 32  # generous bound on raw input; `TickerValidator` is the authority

TickerInput = Annotated[str, Field(min_length=1, max_length=MAX_TICKER_INPUT_CHARS)]
# `aborted`: stopped by IBKR Gateway trouble (connection lost / request or per-ticker time-out).
BatchState = Literal["running", "cancelling", "completed", "cancelled", "aborted"]
# `not_started`: left over when the batch was aborted (Retry re-runs every non-`done` ticker).
BatchTickerState = Literal["queued", "running", "done", "failed", "cancelled", "not_started"]


class WatchlistEntry(BaseModel):
    """One watchlist row with archive freshness (from the snapshot index, no IBKR call)."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    latest_snapshot_id: str | None = None
    latest_snapshot_utc: str | None = None
    is_today_et: bool = False


class WatchlistResponse(BaseModel):
    """Response for `GET /api/watchlist` and the watchlist add / remove routes."""

    model_config = ConfigDict(extra="forbid")

    tickers: list[str]
    entries: list[WatchlistEntry]
    count: int
    max_size: int
    is_full: bool
    allow_custom: bool
    writable: bool
    notice: str | None = None
    today_et: str
    ticker: str | None = None
    changed: bool = False


class WatchlistTickerRequest(BaseModel):
    """Request body for `POST /api/watchlist/tickers`."""

    model_config = ConfigDict(extra="forbid")

    ticker: TickerInput


class BatchRefreshRequest(BaseModel):
    """Request body for `POST /api/refresh/batch` (`strike_step` is always Auto for batches).

    `tickers` is bounded here by the absolute config limit; the service enforces
    `watchlist.max_size` after validation + dedupe.
    """

    model_config = ConfigDict(extra="forbid")

    tickers: list[TickerInput] = Field(min_length=1, max_length=WATCHLIST_MAX_SIZE_LIMIT)
    range_pct: float = Field(default=0.20, gt=0.0, le=1.0)
    dte_min: int | None = Field(default=None, ge=0, le=120)
    dte_max: int | None = Field(default=None, ge=1, le=120)
    include_short_dte: bool | None = None

    @model_validator(mode="after")
    def _validate_dte_bounds(self) -> BatchRefreshRequest:
        if self.dte_min is not None and self.dte_max is not None and self.dte_min > self.dte_max:
            raise ValueError("dte_min must be <= dte_max")
        return self


class BatchTickerResult(BaseModel):
    """Per-ticker progress of a batch refresh; `error` is always a sanitized message.

    `partial=True` (state `cancelled` or `failed`): the interrupted ticker's finished pages were
    archived as partial snapshot `snapshot_id` (`contract_count` rows; `data/latest` untouched).
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    state: BatchTickerState
    partial: bool = False
    snapshot_id: str | None = None
    contract_count: int | None = None
    error_code: str | None = None
    error: str | None = None
    duration_sec: float | None = None
    notices: list[str] = Field(default_factory=list)


class BatchRefreshStatus(BaseModel):
    """Status of the running or most recent batch refresh (in memory only)."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    state: BatchState
    total: int
    done_count: int
    failed_count: int
    cancelled_count: int
    not_started_count: int = 0
    current_ticker: str | None = None
    started_at_utc: str
    finished_at_utc: str | None = None
    notices: list[str] = Field(default_factory=list)
    items: list[BatchTickerResult]


# ------------------------------------------------------------------ best option screener
# `selected`: one row per ticker for `strategy_kind`; `best`: one row per ticker, the passing
# preset with the highest RoR; `all`: every passing ticker x preset.
ScreenerMode = Literal["selected", "best", "all"]
ScreenerRowStatus = Literal["ok", "filtered", "not_buildable", "no_data"]
# Entry floors named by their request field.
ScreenerFilterName = Literal["min_net_credit_dollars", "min_ror_pct", "min_pop_pct"]


class ScreenerRunRequest(BaseModel):
    """Request body of a Best Option screener run over the watchlist (cached CSVs, no IBKR).

    - `mode` defaults to `best`. `strategy_kind` is required for `selected` and ignored by
      `best` / `all` (echoed as `null` in `ScreenerParams`).
    - `dte_min` / `dte_max`: same bounds as `StrategySuggestRequest`; `None` falls back to the
      configured primary window (`options_chain.dte_min` / `dte_max`, 30-120 by default).
    - Floors (`None` or `0` = off): `min_net_credit_dollars` applies to the credit presets only
      (Bull Put, Bear Call, Iron Condor); `min_ror_pct` and `min_pop_pct` apply to every preset.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: ScreenerMode = "best"
    strategy_kind: PresetStrategyKind | None = None
    dte_min: int | None = Field(default=None, ge=0, le=120)
    dte_max: int | None = Field(default=None, ge=1, le=120)
    min_net_credit_dollars: float | None = Field(default=None, ge=0.0)
    min_ror_pct: float | None = Field(default=None, ge=0.0, le=1000.0)
    min_pop_pct: float | None = Field(default=None, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def _validate_mode_and_dte(self) -> ScreenerRunRequest:
        if self.dte_min is not None and self.dte_max is not None and self.dte_min > self.dte_max:
            raise ValueError("dte_min must be <= dte_max")
        if self.mode == "selected" and self.strategy_kind is None:
            raise ValueError("strategy_kind is required when mode is 'selected'")
        return self


class ScreenerParams(BaseModel):
    """Effective screener inputs echoed by `ScreenerRunResponse` (after normalization).

    DTE bounds are resolved (config defaults applied), floors set to `0` are `None`, and
    `strategy_kind` is `None` unless `mode == "selected"`. `qty_multiplier`, `days_forward` and
    `iv_shift` are the fixed Strategy Lab defaults the candidates were built with (target expiry
    is always Auto, as in Autobuild).
    """

    model_config = ConfigDict(extra="forbid")

    mode: ScreenerMode
    strategy_kind: PresetStrategyKind | None = None
    dte_min: int
    dte_max: int
    min_net_credit_dollars: float | None = None
    min_ror_pct: float | None = None
    min_pop_pct: float | None = None
    qty_multiplier: int = 1
    days_forward: int = 0
    iv_shift: float = 0.0


class ScreenerRow(BaseModel):
    """One screener row: a passing Autobuild candidate or a per-ticker status row (no curves).

    `status`:
    - `ok`: a candidate passing every active floor; all candidate fields are set and equal the
      Strategy Lab Autobuild evaluation for the same ticker, snapshot and inputs.
    - `filtered`: the candidate(s) failed `failed_filters`; `status_reason` names the floors
      (e.g. `min POP 60%`).
    - `not_buildable`: Autobuild could not construct the preset; `status_reason` is a sanitized
      single-line reason and `skip_reason` the builder's code when it reported one.
    - `no_data`: no complete snapshot is archived (or it could not be read).
    Status rows carry the ticker, the status fields and, when a snapshot was found, its metadata
    and spot; candidate fields stay empty. `row_id` (`{ticker}:{strategy_kind}`, or `{ticker}`
    for a `best` status row) is unique within a response. `legs_label` is e.g. `−P150 / +P145`
    (`src.services.screener.format_legs_label`); `min_leg_open_interest` is the lowest leg open
    interest, `None` when any leg's open interest is unknown.
    """

    model_config = ConfigDict(extra="forbid")

    row_id: str
    ticker: str
    status: ScreenerRowStatus
    status_reason: str | None = None
    failed_filters: list[ScreenerFilterName] = Field(default_factory=list)
    skip_reason: str | None = None
    strategy_kind: PresetStrategyKind | None = None
    snapshot_id: str | None = None
    snapshot_ts_utc: str | None = None
    is_today_et: bool = False
    market_data_type: int | None = None
    underlying_price: float | None = None
    short_expiry: str | None = None
    short_dte: int | None = None
    long_expiry: str | None = None
    long_dte: int | None = None
    legs: list[EvaluatedLeg] = Field(default_factory=list)
    legs_label: str | None = None
    pricing: StrategyPricing | None = None
    risk: StrategyRiskMetrics | None = None
    net_greeks: PositionGreeks | None = None
    min_leg_open_interest: int | None = None
    rationale: list[str] = Field(default_factory=list)
    primary_call_wall: float | None = None
    primary_put_wall: float | None = None


class ScreenerStatusCounts(BaseModel):
    """Row counts per status. In `selected` / `best` they count the returned rows (one per
    ticker); in `all` they count every ticker x preset (one `no_data` per ticker without
    usable data) before the status rows are dropped."""

    model_config = ConfigDict(extra="forbid")

    ok: int = 0
    filtered: int = 0
    not_buildable: int = 0
    no_data: int = 0


class ScreenerRunResponse(BaseModel):
    """Best Option screener result. `rows` come in the default order (RoR desc, rows without
    an RoR, including every status row, last); the UI sorts and filters them client-side."""

    model_config = ConfigDict(extra="forbid")

    mode: ScreenerMode
    params: ScreenerParams
    generated_at_utc: str
    today_et: str
    tickers: list[str]
    rows: list[ScreenerRow]
    counts: ScreenerStatusCounts
    notices: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ live best option screener
#: Most screener rows one live request may list (20 tickers x 6 presets).
SCREENER_LIVE_MAX_ROWS = 120
ScreenerRowId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]
# `live`: allocated and re-priced from live quotes; `no_quotes`: allocated but a current leg has no
# live quote yet (or its snapshot is unavailable), showing the last values; `not_live_budget`: did
# not fit in the IBKR line budget.
ScreenerLiveStatus = Literal["live", "not_live_budget", "no_quotes"]
# Why a live screener stream ended server-side (the reason of the `refresh_lock` holder, the
# other live feed, a newer screener stream, the UI teardown or the server shutdown).
ScreenerLiveStopReason = Literal[
    "manual_stop",
    "superseded",
    "corridor_stream",
    "corridor_snapshot",
    "refresh_chain",
    "batch_refresh",
    "ledger",
    "ledger_sync",
    "ledger_mtm",
    "shutdown",
]


class ScreenerLiveRequest(ScreenerRunRequest):
    """Opens a live screener stream: the screener run inputs plus the client's row order.

    `row_ids` are the rows of `POST /api/screener/run` (same inputs) in the client's current
    sort / filter order (duplicates ignored, unknown ids and non-`ok` rows skipped); line budget
    allocation walks them in this order.
    """

    row_ids: list[ScreenerRowId] = Field(default_factory=list, max_length=SCREENER_LIVE_MAX_ROWS)

    def run_request(self) -> ScreenerRunRequest:
        """The screener run inputs (without `row_ids`)."""
        return ScreenerRunRequest.model_validate(self.model_dump(exclude={"row_ids"}))


class ScreenerLiveReallocateRequest(BaseModel):
    """Re-allocates the line budget of a running stream to `row_ids` (new sort / filter order).

    The stream keeps the run inputs it was opened with; new inputs (Recalculate) re-open it.
    """

    model_config = ConfigDict(extra="forbid")

    stream_id: str = Field(min_length=1, max_length=64)
    row_ids: list[ScreenerRowId] = Field(default_factory=list, max_length=SCREENER_LIVE_MAX_ROWS)


class ScreenerLiveRowAllocation(BaseModel):
    """Planned live status of one requested `ok` row."""

    model_config = ConfigDict(extra="forbid")

    row_id: str
    ticker: str
    live_status: ScreenerLiveStatus


class ScreenerLiveReallocateResponse(BaseModel):
    """Result of a re-allocation; frames switch to it at `version` (lines change paced).

    `lines_planned` / `lines_budget` count options + spots (budget 90); `option_lines_budget`
    caps the option lines alone (the Corridor option cap, 88).
    """

    model_config = ConfigDict(extra="forbid")

    stream_id: str
    version: int
    lines_planned: int
    lines_budget: int
    option_lines_budget: int
    rows: list[ScreenerLiveRowAllocation]
    notices: list[str] = Field(default_factory=list)


class ScreenerLiveRow(BaseModel):
    """Live state of one requested `ok` screener row inside a frame.

    - `live` / `no_quotes`: every candidate field is set. `adjusted` is `True` while the streamed
      legs differ from the screener row's legs (a strictly higher-RoR neighbour combination
      inside the subscribed +-1 window that passes the screener floors); `keep_reason` is the
      latest re-pick's reason for keeping the legs (`None` when it switched). `passes_filters`
      tells whether the shown legs still meet the floors with live quotes.
    - `not_live_budget`: only the identity fields are set (the client keeps the snapshot row).
    """

    model_config = ConfigDict(extra="forbid")

    row_id: str
    ticker: str
    strategy_kind: PresetStrategyKind
    live_status: ScreenerLiveStatus
    adjusted: bool = False
    keep_reason: RepickKeepReason | None = None
    passes_filters: bool | None = None
    underlying_price: float | None = None
    legs: list[EvaluatedLeg] = Field(default_factory=list)
    legs_label: str | None = None
    pricing: StrategyPricing | None = None
    risk: StrategyRiskMetrics | None = None
    net_greeks: PositionGreeks | None = None
    min_leg_open_interest: int | None = None


class ScreenerLiveFrame(BaseModel):
    """One live screener frame (about every second); display-only, never persisted.

    `rows` lists the requested `ok` rows in request order. `version` is the applied allocation
    (`ScreenerLiveReallocateResponse.version`). `lines_used` / `lines_budget` count options +
    spots (budget 90); `option_lines_budget` caps the option lines alone (88). A stream stopped
    server-side ends with one frame with `stopped=True`, `stop_reason` and no rows; a client
    disconnect ends it silently.
    """

    model_config = ConfigDict(extra="forbid")

    stream_id: str
    sequence: int
    version: int
    updated_ts_utc: str
    market_data_type: int
    market_data_type_label: str
    lines_used: int
    lines_budget: int
    option_lines_budget: int
    rows: list[ScreenerLiveRow] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    stopped: bool = False
    stop_reason: ScreenerLiveStopReason | None = None


class ScreenerLiveStopResponse(BaseModel):
    """Response of a live screener stop (mirrors `CorridorStopResponse`)."""

    model_config = ConfigDict(extra="forbid")

    stopped_subscriptions: int
    active_streams: int
