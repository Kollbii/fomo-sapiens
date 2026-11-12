"""FastAPI REST API router and orchestration service for the IBKR Options Analyzer."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import polars as pl
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analytics.anomalies import AnomaliesReport, scan_anomalies
from src.analytics.bsm_fallback import BSMFallbackEngine
from src.analytics.strategies import (
    PRESET_STRATEGIES,
    PresetStrategyKind,
    StrategyEvaluationResponse,
    StrategyKind,
    StrategyLegInput,
)
from src.analytics.strategies import (
    evaluate_strategy as evaluate_strategy_engine,
)
from src.analytics.term_greeks import MultiDTEProfilesReport, build_multi_dte_profiles
from src.analytics.walls import ChainWallsReport, compute_walls
from src.broker.client import IBKRBrokerClient
from src.broker.corridor import (
    CorridorLegRef,
    CorridorQuoteFrame,
    CorridorQuoteRow,
    CorridorQuoteService,
)
from src.broker.fetcher import PagedOptionChainFetcher
from src.core.config import Settings, get_settings
from src.core.exceptions import (
    GatewayConnectionError,
    InvalidTickerError,
    OptionChainFetchError,
    PacingViolationError,
    SecurityViolationError,
    SnapshotNotFoundError,
    StorageSchemaError,
    UnderlyingPriceUnavailableError,
)
from src.core.security import TickerValidator
from src.storage.csv_repository import CSVRepository
from src.storage.quality import evaluate_snapshot, measure_quote_quality
from src.storage.schema import (
    OPTION_CHAIN_POLARS_SCHEMA,
    CollectionMode,
    SnapshotMeta,
    SpotSource,
    infer_chain_step_and_range,
)

router = APIRouter(prefix="/api", tags=["options-analyzer"])
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ API schemas
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
    spot_source: SpotSource | None = None
    quoted_contract_count: int | None = None
    quote_coverage: float | None = None
    prior_close_only_count: int | None = None
    collection_mode: CollectionMode | None = None


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


def _parse_priority_legs_query(raw: str | None) -> list[CorridorLegRef]:
    """Parse compact query string `YYYY-MM-DD|strike|C,...` into `CorridorLegRef` items."""
    if not raw or not raw.strip():
        return []
    refs: list[CorridorLegRef] = []
    for token in raw.split(","):
        part = token.strip()
        if not part:
            continue
        sep = "|" if "|" in part else ":"
        fields = [item.strip() for item in part.split(sep)]
        if len(fields) != 3:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid priority_legs token {part!r}; expected EXPIRY|STRIKE|RIGHT",
            )
        exp_str, strike_str, right_str = fields
        try:
            strike_val = float(strike_str)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid strike in priority_legs: {strike_str!r}"
            ) from exc
        right_upper = right_str.upper()
        if right_upper not in ("C", "P"):
            raise HTTPException(
                status_code=422, detail=f"Invalid right in priority_legs: {right_str!r}"
            )
        right_lit: Literal["C", "P"] = "C" if right_upper == "C" else "P"
        refs.append(CorridorLegRef(expiry=exp_str, strike=strike_val, right=right_lit))
    return refs


def _format_sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


# ------------------------------------------------------------------ service orchestrator
class AnalyzerWebService:
    """Coordinates Broker -> BSM Fallback -> CSV Storage -> Analytics payloads."""

    def __init__(
        self,
        settings: Settings | None = None,
        repo: CSVRepository | None = None,
        broker_client: IBKRBrokerClient | None = None,
        fetcher: PagedOptionChainFetcher | None = None,
        corridor: CorridorQuoteService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repo = repo or CSVRepository(self.settings.storage)
        self.broker_client = broker_client or IBKRBrokerClient(self.settings)
        self.fetcher = fetcher or PagedOptionChainFetcher(self.broker_client, self.settings)
        self.corridor = corridor or CorridorQuoteService(
            self.broker_client,
            self.settings,
            clock=self.fetcher.clock,
            sleep=self.fetcher.sleep,
        )
        self.validator = TickerValidator(
            watchlist=self.settings.watchlist.hardcoded_tickers,
            allow_custom=self.settings.watchlist.allow_custom_ticker,
        )
        self._ticker_capabilities: dict[str, tuple[list[float], list[float], int]] = {}
        self._corridor_cache: dict[
            tuple[str, str], tuple[float, dict[tuple[str, float, str], CorridorQuoteRow]]
        ] = {}

    def validate_ticker(self, raw_ticker: str) -> str:
        return self.validator.validate_selection(raw_ticker)

    def get_status(self, ticker: str | None = None) -> GatewayStatusResponse:
        ibkr = self.settings.ibkr
        wl = self.settings.watchlist
        oc = self.settings.options_chain
        active_md = (
            self.broker_client.get_active_market_data_type()
            if hasattr(self.broker_client, "get_active_market_data_type")
            else int(self.broker_client.current_type)
        )
        cap_sym: str | None = None
        if ticker is not None and ticker.strip():
            try:
                cap_sym = self.validate_ticker(ticker)
            except InvalidTickerError:
                cap_sym = None
        caps = self._ticker_capabilities.get(cap_sym) if cap_sym else None
        avail_steps = list(caps[0]) if caps else list(oc.allowed_strike_steps)
        avail_ranges = list(caps[1]) if caps else list(oc.allowed_range_pcts)
        return GatewayStatusResponse(
            connected=self.broker_client.is_connected,
            host=ibkr.host,
            port=ibkr.port,
            client_id=ibkr.client_id,
            market_data_mode=ibkr.market_data_mode,
            default_market_data_type=ibkr.default_market_data_type,
            fallback_market_data_type=ibkr.fallback_market_data_type,
            active_market_data_type=active_md,
            live_subscription_missing=bool(
                getattr(self.broker_client, "live_subscription_missing", False)
            ),
            default_ticker=wl.default_ticker,
            watchlist=list(wl.hardcoded_tickers),
            allow_custom_ticker=wl.allow_custom_ticker,
            default_range_pct=oc.default_range_pct,
            allowed_range_pcts=list(oc.allowed_range_pcts),
            default_strike_step=oc.default_strike_step,
            allowed_strike_steps=list(oc.allowed_strike_steps),
            dte_min=oc.dte_min,
            dte_max=oc.dte_max,
            available_strike_steps=avail_steps,
            available_range_pcts=avail_ranges,
            active_capabilities_ticker=cap_sym if caps else None,
        )

    def list_snapshots(self, raw_ticker: str) -> SnapshotsListResponse:
        symbol = self.validate_ticker(raw_ticker)
        snaps = self.repo.list_snapshots(symbol)
        return SnapshotsListResponse(
            ticker=symbol,
            latest_snapshot_id=snaps[0].snapshot_id if snaps else None,
            snapshots=snaps,
        )

    def _enrich_if_needed(self, df: pl.DataFrame) -> pl.DataFrame:
        needs_bsm = (
            df.filter((pl.col("greeks_source") == "NONE") | pl.col("iv").is_null()).height > 0
        )
        if not needs_bsm:
            return df
        return BSMFallbackEngine.enrich_missing_greeks(
            df, risk_free_rate=self.settings.analytics.risk_free_rate
        )

    def build_analytics_payload(
        self,
        df: pl.DataFrame,
        *,
        source: Literal["CACHE", "LIVE_REFRESH"],
        notices: list[str] | None = None,
        range_pct: float | None = None,
        strike_step: float | None = None,
        requested_strike_step: float | None = None,
        exchange_validated_strike_count: int | None = None,
        exchange_native_verified: bool | None = None,
        available_strike_steps: list[float] | None = None,
        available_range_pcts: list[float] | None = None,
        spot_source: SpotSource | None = None,
        collection_mode: CollectionMode | None = None,
    ) -> ChainAnalyticsResponse:
        enriched = self._enrich_if_needed(df)
        quality = measure_quote_quality(enriched)
        first = enriched.row(0, named=True)
        source_counts = dict(Counter(str(s) for s in enriched["greeks_source"].to_list()))
        expirations = sorted(str(e) for e in enriched["expiry"].unique().to_list())
        dtes = sorted(int(d) for d in enriched["dte"].unique().to_list())

        inferred_step, inferred_range = infer_chain_step_and_range(enriched)
        resolved_range = range_pct if range_pct is not None else inferred_range
        resolved_step: float | None
        if inferred_step is not None and (
            strike_step is None or inferred_step > strike_step + 1e-6
        ):
            resolved_step = inferred_step
        else:
            resolved_step = strike_step if strike_step is not None else inferred_step
        resolved_requested_step = (
            requested_strike_step if requested_strike_step is not None else strike_step
        )

        underlying_sym = str(first["underlying"])
        caps = self._ticker_capabilities.get(underlying_sym)
        oc = self.settings.options_chain
        resolved_avail_steps = (
            list(available_strike_steps)
            if available_strike_steps is not None
            else (list(caps[0]) if caps else list(oc.allowed_strike_steps))
        )
        resolved_avail_ranges = (
            list(available_range_pcts)
            if available_range_pcts is not None
            else (list(caps[1]) if caps else list(oc.allowed_range_pcts))
        )
        resolved_validated_count = (
            exchange_validated_strike_count
            if exchange_validated_strike_count is not None
            else (caps[2] if caps else None)
        )
        resolved_verified = (
            exchange_native_verified if exchange_native_verified is not None else (caps is not None)
        )

        meta = ChainMetadataPayload(
            snapshot_id=str(first["snapshot_id"]),
            snapshot_ts_utc=str(first["snapshot_ts_utc"]),
            market_date=str(first["market_date"]),
            underlying=underlying_sym,
            underlying_conid=int(first["underlying_conid"]),
            underlying_price=float(first["underlying_price"]),
            market_data_type=int(first["market_data_type"]),
            contract_count=enriched.height,
            expirations=expirations,
            dtes=dtes,
            range_pct=resolved_range,
            strike_step=resolved_step,
            requested_strike_step=resolved_requested_step,
            exchange_validated_strike_count=resolved_validated_count,
            exchange_native_verified=resolved_verified,
            available_strike_steps=resolved_avail_steps,
            available_range_pcts=resolved_avail_ranges,
            greeks_source_counts=source_counts,
            source=source,
            notices=list(notices or []),
            spot_source=spot_source,
            quoted_contract_count=quality.quoted_contract_count,
            quote_coverage=quality.quote_coverage,
            prior_close_only_count=quality.prior_close_only_count,
            collection_mode=collection_mode,
        )

        walls = compute_walls(enriched, top_n=self.settings.analytics.top_walls_count)
        anomalies = scan_anomalies(
            enriched,
            zscore_threshold=self.settings.analytics.unusual_volume_zscore_threshold,
        )
        profiles = build_multi_dte_profiles(
            enriched,
            default_market_data_type=self.settings.ibkr.default_market_data_type,
        )
        contracts = enriched.sort(["dte", "strike", "right"]).to_dicts()

        return ChainAnalyticsResponse(
            meta=meta,
            walls=walls,
            anomalies=anomalies,
            profiles=profiles,
            contracts=contracts,
        )

    def load_chain_analytics(
        self,
        raw_ticker: str,
        snapshot_id: str | None = None,
        *,
        dte_min: int | None = None,
        dte_max: int | None = None,
    ) -> ChainAnalyticsResponse:
        symbol = self.validate_ticker(raw_ticker)
        if dte_min is not None and dte_max is not None and dte_min > dte_max:
            raise HTTPException(status_code=422, detail="dte_min must be <= dte_max")
        if snapshot_id is not None and snapshot_id.strip():
            df = self.repo.load_snapshot(symbol, snapshot_id.strip())
        else:
            df = self.repo.load_latest(symbol)
        loaded_snap_id = str(df["snapshot_id"][0])
        if dte_min is not None:
            df = df.filter(pl.col("dte") >= dte_min)
        if dte_max is not None:
            df = df.filter(pl.col("dte") <= dte_max)
        if df.height == 0:
            raise SnapshotNotFoundError(f"No contracts found in requested DTE range for {symbol}")
        snap_meta = next(
            (m for m in self.repo.list_snapshots(symbol) if m.snapshot_id == loaded_snap_id),
            None,
        )
        return self.build_analytics_payload(
            df,
            source="CACHE",
            range_pct=snap_meta.range_pct if snap_meta else None,
            strike_step=snap_meta.strike_step if snap_meta else None,
            requested_strike_step=snap_meta.requested_strike_step if snap_meta else None,
            exchange_validated_strike_count=(
                snap_meta.exchange_validated_strike_count if snap_meta else None
            ),
            exchange_native_verified=(snap_meta.exchange_native_verified if snap_meta else None),
            available_strike_steps=(snap_meta.available_strike_steps if snap_meta else None),
            available_range_pcts=(snap_meta.available_range_pcts if snap_meta else None),
            spot_source=snap_meta.spot_source if snap_meta else None,
            collection_mode=snap_meta.collection_mode if snap_meta else None,
        )

    def resolve_csv_path(self, raw_ticker: str, snapshot_id: str | None = None) -> Path:
        symbol = self.validate_ticker(raw_ticker)
        if snapshot_id is not None and snapshot_id.strip():
            path = self.repo.snapshot_path(symbol, snapshot_id.strip())
            if not path.is_file():
                raise SnapshotNotFoundError(f"Snapshot not found: {snapshot_id.strip()}")
            return path
        latest = self.repo.latest_path(symbol)
        if latest.is_file():
            return latest
        for meta in self.repo.list_snapshots(symbol):
            candidate = self.repo.snapshot_path(symbol, meta.snapshot_id)
            if candidate.is_file():
                return candidate
        raise SnapshotNotFoundError(f"No snapshots available for {symbol}")

    def _record_corridor_frame(self, frame: CorridorQuoteFrame) -> None:
        key = (frame.ticker, frame.snapshot_id)
        existing_spot, quote_map = self._corridor_cache.get(key, (0.0, {}))
        updated_map = dict(quote_map)
        for q in frame.quotes:
            updated_map[(q.expiry, round(float(q.strike), 6), q.right)] = q
        eff_spot = frame.underlying_price if frame.underlying_price > 0.0 else existing_spot
        self._corridor_cache[key] = (eff_spot, updated_map)

    def _apply_corridor_overlay(
        self, df: pl.DataFrame, symbol: str, snapshot_id: str
    ) -> pl.DataFrame:
        entry = self._corridor_cache.get((symbol, snapshot_id))
        if entry is None:
            return df
        live_spot, quote_map = entry
        if not quote_map:
            return df
        rows = df.to_dicts()
        changed = False
        for row in rows:
            if live_spot > 0.0:
                row["underlying_price"] = live_spot
                strike_f = float(row["strike"])
                row["moneyness_pct"] = ((strike_f - live_spot) / live_spot) * 100.0
                changed = True
            qkey = (str(row["expiry"]), round(float(row["strike"]), 6), str(row["right"]).upper())
            live_q = quote_map.get(qkey)
            if live_q is None:
                continue
            changed = True
            row["bid"] = live_q.bid
            row["ask"] = live_q.ask
            row["mid"] = live_q.mid
            row["last"] = live_q.last
            row["close"] = live_q.close
            if live_q.volume is not None:
                row["volume"] = live_q.volume
            if live_q.open_interest is not None:
                row["open_interest"] = live_q.open_interest
            if live_q.iv is not None:
                row["iv"] = live_q.iv
            if live_q.delta is not None:
                row["delta"] = live_q.delta
            if live_q.gamma is not None:
                row["gamma"] = live_q.gamma
            if live_q.theta is not None:
                row["theta"] = live_q.theta
            if live_q.vega is not None:
                row["vega"] = live_q.vega
            if live_q.greeks_source != "NONE":
                row["greeks_source"] = live_q.greeks_source
        if not changed:
            return df
        return pl.DataFrame(rows, schema=df.schema)

    def _load_enriched_chain(
        self,
        raw_ticker: str,
        snapshot_id: str | None = None,
        *,
        apply_corridor_overlay: bool = True,
    ) -> tuple[str, str, pl.DataFrame]:
        symbol = self.validate_ticker(raw_ticker)
        df = (
            self.repo.load_snapshot(symbol, snapshot_id.strip())
            if snapshot_id is not None and snapshot_id.strip()
            else self.repo.load_latest(symbol)
        )
        loaded_snap_id = str(df["snapshot_id"][0])
        enriched = self._enrich_if_needed(df)
        if apply_corridor_overlay:
            enriched = self._apply_corridor_overlay(enriched, symbol, loaded_snap_id)
        return symbol, loaded_snap_id, enriched

    async def fetch_corridor_quotes(self, payload: CorridorQuotesRequest) -> CorridorQuoteFrame:
        _, snap_id, enriched = self._load_enriched_chain(
            payload.ticker,
            payload.snapshot_id,
            apply_corridor_overlay=False,
        )
        try:
            return await self.corridor.fetch_corridor_quotes(
                enriched,
                snapshot_id=snap_id,
                target_expiry=payload.target_expiry,
                window_strikes=payload.window_strikes,
                max_contracts=payload.max_contracts,
                priority_legs=payload.priority_legs,
                on_frame=self._record_corridor_frame,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def prepare_corridor_stream(
        self,
        raw_ticker: str,
        *,
        snapshot_id: str | None = None,
        priority_legs_raw: str | None = None,
    ) -> tuple[str, str, pl.DataFrame, list[CorridorLegRef]]:
        symbol, snap_id, enriched = self._load_enriched_chain(
            raw_ticker,
            snapshot_id,
            apply_corridor_overlay=False,
        )
        legs = _parse_priority_legs_query(priority_legs_raw)
        await self.broker_client.ensure_connected()
        return symbol, snap_id, enriched, legs

    async def stop_corridor_streams(self) -> CorridorStopResponse:
        stopped = await self.corridor.stop_all_streams()
        return CorridorStopResponse(
            stopped_subscriptions=stopped,
            active_streams=self.corridor.active_stream_count,
        )

    def suggest_strategies(self, payload: StrategySuggestRequest) -> StrategySuggestResponse:
        symbol, snap_id, enriched = self._load_enriched_chain(payload.ticker, payload.snapshot_id)
        walls = compute_walls(enriched, top_n=self.settings.analytics.top_walls_count)
        kinds: tuple[PresetStrategyKind, ...] = (
            (payload.strategy_kind,) if payload.strategy_kind is not None else PRESET_STRATEGIES
        )
        evaluations: list[StrategyEvaluationResponse] = []
        errors: list[str] = []
        for kind in kinds:
            try:
                evaluations.append(
                    evaluate_strategy_engine(
                        enriched,
                        strategy_kind=kind,
                        target_expiry=payload.target_expiry,
                        days_forward=payload.days_forward,
                        iv_shift=payload.iv_shift,
                        qty_multiplier=payload.qty_multiplier,
                        risk_free_rate=self.settings.analytics.risk_free_rate,
                        walls_report=walls,
                    )
                )
            except ValueError as exc:
                if payload.strategy_kind is not None:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                errors.append(f"{kind}: {exc}")

        if not evaluations:
            detail = "; ".join(errors) or "Unable to construct any preset strategy from snapshot"
            raise HTTPException(status_code=422, detail=detail)

        return StrategySuggestResponse(
            ticker=symbol,
            snapshot_id=snap_id,
            evaluations=evaluations,
        )

    def evaluate_strategy(self, payload: StrategyEvaluateRequest) -> StrategyEvaluationResponse:
        _, _, enriched = self._load_enriched_chain(payload.ticker, payload.snapshot_id)
        try:
            return evaluate_strategy_engine(
                enriched,
                strategy_kind=payload.strategy_kind,
                target_expiry=payload.target_expiry,
                legs=payload.legs,
                days_forward=payload.days_forward,
                iv_shift=payload.iv_shift,
                qty_multiplier=payload.qty_multiplier,
                risk_free_rate=self.settings.analytics.risk_free_rate,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def refresh_chain(self, req: RefreshRequest) -> ChainAnalyticsResponse:
        symbol = self.validate_ticker(req.ticker)
        # IBKR allows 100 simultaneous market-data lines by default (shared with TWS). A page of
        # 80 contracts plus a corridor stream of up to 60 would exceed it, so stop streams first.
        stopped_lines = 0
        if self.corridor.active_stream_count:
            stopped_lines = await self.corridor.stop_all_streams()
            logger.info("refresh %s stopped corridor streams (%d lines)", symbol, stopped_lines)
        fetch_res = await self.fetcher.fetch_option_chain(
            symbol,
            range_pct=req.range_pct,
            strike_step=req.strike_step,
            dte_min=req.dte_min,
            dte_max=req.dte_max,
        )
        raw_df = pl.DataFrame(
            [r.model_dump() for r in fetch_res.rows],
            schema=OPTION_CHAIN_POLARS_SCHEMA,
        )
        enriched_df = BSMFallbackEngine.enrich_missing_greeks(
            raw_df, risk_free_rate=self.settings.analytics.risk_free_rate
        )
        inferred_step, _ = infer_chain_step_and_range(enriched_df)
        plan_step = fetch_res.plan.strikes.effective_step
        actual_step = (
            inferred_step
            if (inferred_step is not None and inferred_step > plan_step + 1e-6)
            else plan_step
        )
        req_step = fetch_res.plan.strikes.requested_step
        notices = list(fetch_res.notices)
        if stopped_lines:
            notices.append(
                f"Stopped the corridor stream ({stopped_lines} market-data lines) before the "
                "refresh to stay under the IBKR line limit."
            )
        if actual_step > plan_step + 1e-6:
            notices.append(
                f"IBKR strikes for {symbol} spaced at ${actual_step:g} (requested ${req_step:g})."
            )
        plan = fetch_res.plan
        avail_steps = (
            list(plan.available_strike_steps)
            if plan.available_strike_steps
            else list(self.settings.options_chain.allowed_strike_steps)
        )
        avail_ranges = (
            list(plan.available_range_pcts)
            if plan.available_range_pcts
            else list(self.settings.options_chain.allowed_range_pcts)
        )
        validated_count = (
            int(plan.exchange_validated_strike_count)
            if plan.exchange_validated_strike_count > 0
            else len({float(r.strike) for r in fetch_res.rows})
        )
        self._ticker_capabilities[symbol] = (avail_steps, avail_ranges, validated_count)
        saved_meta = self.repo.save_snapshot(
            symbol,
            enriched_df.to_dicts(),
            range_pct=plan.range_pct,
            strike_step=actual_step,
            requested_strike_step=req_step,
            spot_source=plan.underlying.spot_source,
            collection_mode="MANUAL",
        )
        quality_verdict = evaluate_snapshot(saved_meta)
        if not quality_verdict.usable:
            notices.append(
                "Snapshot saved but not usable for history: " + "; ".join(quality_verdict.reasons)
            )
        return self.build_analytics_payload(
            enriched_df,
            source="LIVE_REFRESH",
            notices=notices,
            range_pct=plan.range_pct,
            strike_step=actual_step,
            requested_strike_step=req_step,
            exchange_validated_strike_count=validated_count,
            exchange_native_verified=bool(plan.exchange_native_verified),
            available_strike_steps=avail_steps,
            available_range_pcts=avail_ranges,
            spot_source=saved_meta.spot_source,
            collection_mode=saved_meta.collection_mode,
        )


async def get_web_service(request: Request) -> AnalyzerWebService:
    """FastAPI dependency returning the application's `AnalyzerWebService` instance."""
    svc: AnalyzerWebService | None = getattr(request.app.state, "service", None)
    if svc is None:
        svc = AnalyzerWebService()
        request.app.state.service = svc
    return svc


# ------------------------------------------------------------------ routes
@router.get("/status", response_model=GatewayStatusResponse)
async def api_status(
    ticker: str | None = None,
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> GatewayStatusResponse:
    return svc.get_status(ticker=ticker)


@router.get("/snapshots/{ticker}", response_model=SnapshotsListResponse)
async def api_snapshots(
    ticker: str,
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> SnapshotsListResponse:
    return svc.list_snapshots(ticker)


@router.get("/chain/{ticker}", response_model=ChainAnalyticsResponse)
async def api_chain(
    ticker: str,
    snapshot_id: str | None = Query(default=None),
    dte_min: int | None = Query(default=None, ge=0, le=120),
    dte_max: int | None = Query(default=None, ge=1, le=120),
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> ChainAnalyticsResponse:
    return svc.load_chain_analytics(
        ticker,
        snapshot_id=snapshot_id,
        dte_min=dte_min,
        dte_max=dte_max,
    )


@router.get("/csv/{ticker}")
async def api_csv_download(
    ticker: str,
    snapshot_id: str | None = Query(default=None),
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> FileResponse:
    csv_path = svc.resolve_csv_path(ticker, snapshot_id=snapshot_id)
    return FileResponse(path=csv_path, media_type="text/csv", filename=csv_path.name)


@router.post("/refresh", response_model=ChainAnalyticsResponse)
async def api_refresh(
    payload: RefreshRequest,
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> ChainAnalyticsResponse:
    return await svc.refresh_chain(payload)


@router.post("/strategy/suggest", response_model=StrategySuggestResponse)
async def api_strategy_suggest(
    payload: StrategySuggestRequest,
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> StrategySuggestResponse:
    return svc.suggest_strategies(payload)


@router.post("/strategy/evaluate", response_model=StrategyEvaluationResponse)
async def api_strategy_evaluate(
    payload: StrategyEvaluateRequest,
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> StrategyEvaluationResponse:
    return svc.evaluate_strategy(payload)


@router.post("/corridor/quotes", response_model=CorridorQuoteFrame)
async def api_corridor_quotes(
    payload: CorridorQuotesRequest,
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> CorridorQuoteFrame:
    return await svc.fetch_corridor_quotes(payload)


@router.post("/corridor/stop", response_model=CorridorStopResponse)
async def api_corridor_stop(
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> CorridorStopResponse:
    return await svc.stop_corridor_streams()


@router.get("/corridor/stream/{ticker}")
async def api_corridor_stream(
    request: Request,
    ticker: str,
    snapshot_id: str | None = Query(default=None),
    target_expiry: str | None = Query(default=None),
    window_strikes: int = Query(default=5, ge=1, le=12),
    max_contracts: int = Query(default=56, ge=1, le=60),
    priority_legs: str | None = Query(default=None),
    max_frames: int | None = Query(default=None, ge=1, le=200),
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> StreamingResponse:
    _, snap_id, enriched, legs = await svc.prepare_corridor_stream(
        ticker,
        snapshot_id=snapshot_id,
        priority_legs_raw=priority_legs,
    )

    async def _event_stream() -> AsyncIterator[str]:
        gen = svc.corridor.stream_corridor_quotes(
            enriched,
            snapshot_id=snap_id,
            target_expiry=target_expiry,
            window_strikes=window_strikes,
            max_contracts=max_contracts,
            priority_legs=legs,
            is_disconnected=request.is_disconnected,
            max_frames=max_frames,
            on_frame=svc._record_corridor_frame,
        )
        try:
            async for frame in gen:
                yield _format_sse_event("frame", frame.model_dump())
        except GatewayConnectionError as exc:
            yield _format_sse_event(
                "error",
                {
                    "error_code": "GATEWAY_OFFLINE",
                    "status": 503,
                    "message": (
                        f"{exc} — IBKR Gateway connection lost during live corridor stream."
                    ),
                },
            )
        except (OptionChainFetchError, PacingViolationError) as exc:
            yield _format_sse_event(
                "error",
                {
                    "error_code": "CORRIDOR_STREAM_ERROR",
                    "status": 502,
                    "message": str(exc),
                },
            )
        except asyncio.CancelledError:
            raise
        finally:
            await gen.aclose()

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------ exception handlers
def register_exception_handlers(app: FastAPI) -> None:
    """Map domain exceptions in `src/core/exceptions.py` to structured HTTP JSON responses."""

    @app.exception_handler(InvalidTickerError)
    async def _handle_invalid_ticker(_req: Request, exc: InvalidTickerError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error_code": "INVALID_TICKER", "message": str(exc)},
        )

    @app.exception_handler(SnapshotNotFoundError)
    async def _handle_snapshot_not_found(_req: Request, exc: SnapshotNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error_code": "SNAPSHOT_NOT_FOUND", "message": str(exc)},
        )

    @app.exception_handler(GatewayConnectionError)
    async def _handle_gateway_offline(req: Request, exc: GatewayConnectionError) -> JSONResponse:
        svc: AnalyzerWebService | None = getattr(req.app.state, "service", None)
        host = svc.settings.ibkr.host if svc is not None else "127.0.0.1"
        port = svc.settings.ibkr.port if svc is not None else 4001
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "GATEWAY_OFFLINE",
                "message": (
                    f"{exc} — IBKR Gateway on {host}:{port} is unreachable. "
                    "Serving cached CSV snapshots remains available."
                ),
            },
        )

    @app.exception_handler(UnderlyingPriceUnavailableError)
    async def _handle_spot_unavailable(
        _req: Request, exc: UnderlyingPriceUnavailableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"error_code": "UNDERLYING_PRICE_UNAVAILABLE", "message": str(exc)},
        )

    @app.exception_handler(OptionChainFetchError)
    async def _handle_chain_fetch_error(_req: Request, exc: OptionChainFetchError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"error_code": "OPTION_CHAIN_FETCH_ERROR", "message": str(exc)},
        )

    @app.exception_handler(PacingViolationError)
    async def _handle_pacing_error(_req: Request, exc: PacingViolationError) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"error_code": "PACING_VIOLATION", "message": str(exc)},
        )

    @app.exception_handler(StorageSchemaError)
    async def _handle_storage_error(_req: Request, exc: StorageSchemaError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error_code": "STORAGE_SCHEMA_ERROR", "message": str(exc)},
        )

    @app.exception_handler(SecurityViolationError)
    async def _handle_security_error(_req: Request, exc: SecurityViolationError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"error_code": "SECURITY_VIOLATION", "message": str(exc)},
        )
