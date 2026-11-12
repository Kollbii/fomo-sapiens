"""Request/response models of the service layer (also the public HTTP API contract).

`src/web/` imports every model it serves from here, including the broker/analytics models
re-exported below, so the web layer never depends on lower layers directly.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analytics.anomalies import AnomaliesReport
from src.analytics.strategies import (
    PresetStrategyKind,
    StrategyEvaluationResponse,
    StrategyKind,
    StrategyLegInput,
)
from src.analytics.term_greeks import MultiDTEProfilesReport
from src.analytics.walls import ChainWallsReport
from src.broker.corridor import CorridorLegRef, CorridorQuoteFrame
from src.storage.schema import SnapshotMeta

__all__ = [
    "ChainAnalyticsResponse",
    "ChainMetadataPayload",
    "CorridorLegRef",
    "CorridorQuoteFrame",
    "CorridorQuotesRequest",
    "CorridorStopResponse",
    "GatewayStatusResponse",
    "RefreshRequest",
    "SnapshotsListResponse",
    "StrategyEvaluateRequest",
    "StrategyEvaluationResponse",
    "StrategySuggestRequest",
    "StrategySuggestResponse",
    "SystemStatusResponse",
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
    available_strike_steps: list[float] = Field(default_factory=list)
    available_range_pcts: list[float] = Field(default_factory=list)
    active_capabilities_ticker: str | None = None


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

    @model_validator(mode="after")
    def _validate_dte_bounds(self) -> RefreshRequest:
        if self.dte_min is not None and self.dte_max is not None and self.dte_min > self.dte_max:
            raise ValueError("dte_min must be <= dte_max")
        return self


class StrategySuggestRequest(BaseModel):
    """Request body for `POST /api/strategy/suggest`."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    snapshot_id: str | None = None
    strategy_kind: PresetStrategyKind | None = None
    target_expiry: str | None = None
    days_forward: int = Field(default=0, ge=0, le=365)
    iv_shift: float = Field(default=0.0, ge=-0.80, le=2.00)
    qty_multiplier: int = Field(default=1, ge=1, le=100)


class StrategyEvaluateRequest(BaseModel):
    """Request body for `POST /api/strategy/evaluate`."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    snapshot_id: str | None = None
    strategy_kind: StrategyKind = "BULL_PUT_SPREAD"
    target_expiry: str | None = None
    legs: list[StrategyLegInput] | None = None
    days_forward: int = Field(default=0, ge=0, le=365)
    iv_shift: float = Field(default=0.0, ge=-0.80, le=2.00)
    qty_multiplier: int = Field(default=1, ge=1, le=100)


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
    window_strikes: int = Field(default=5, ge=1, le=12)
    max_contracts: int = Field(default=56, ge=1, le=60)
    priority_legs: list[CorridorLegRef] = Field(default_factory=list)


class CorridorStopResponse(BaseModel):
    """Response body for `POST /api/corridor/stop`."""

    model_config = ConfigDict(extra="forbid")

    stopped_subscriptions: int
    active_streams: int
