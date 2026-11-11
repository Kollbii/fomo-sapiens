"""FastAPI REST API router and orchestration service for the IBKR Options Analyzer."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Literal

import polars as pl
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from src.analytics.anomalies import AnomaliesReport, scan_anomalies
from src.analytics.bsm_fallback import BSMFallbackEngine
from src.analytics.term_greeks import MultiDTEProfilesReport, build_multi_dte_profiles
from src.analytics.walls import ChainWallsReport, compute_walls
from src.broker.client import IBKRBrokerClient
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
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, SnapshotMeta, infer_chain_step_and_range

router = APIRouter(prefix="/api", tags=["options-analyzer"])


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
    active_market_data_type: int
    default_ticker: str
    watchlist: list[str]
    allow_custom_ticker: bool
    default_range_pct: float
    allowed_range_pcts: list[float]
    default_strike_step: float
    allowed_strike_steps: list[float]


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
    strike_step: float | None = Field(default=5.0, gt=0.0)


# ------------------------------------------------------------------ service orchestrator
class AnalyzerWebService:
    """Coordinates Broker -> BSM Fallback -> CSV Storage -> Analytics payloads."""

    def __init__(
        self,
        settings: Settings | None = None,
        repo: CSVRepository | None = None,
        broker_client: IBKRBrokerClient | None = None,
        fetcher: PagedOptionChainFetcher | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repo = repo or CSVRepository(self.settings.storage)
        self.broker_client = broker_client or IBKRBrokerClient(self.settings)
        self.fetcher = fetcher or PagedOptionChainFetcher(self.broker_client, self.settings)
        self.validator = TickerValidator(
            watchlist=self.settings.watchlist.hardcoded_tickers,
            allow_custom=self.settings.watchlist.allow_custom_ticker,
        )

    def validate_ticker(self, raw_ticker: str) -> str:
        return self.validator.validate_selection(raw_ticker)

    def get_status(self) -> GatewayStatusResponse:
        ibkr = self.settings.ibkr
        wl = self.settings.watchlist
        oc = self.settings.options_chain
        return GatewayStatusResponse(
            connected=self.broker_client.is_connected,
            host=ibkr.host,
            port=ibkr.port,
            client_id=ibkr.client_id,
            market_data_mode=ibkr.market_data_mode,
            default_market_data_type=ibkr.default_market_data_type,
            active_market_data_type=self.broker_client.current_type,
            default_ticker=wl.default_ticker,
            watchlist=list(wl.hardcoded_tickers),
            allow_custom_ticker=wl.allow_custom_ticker,
            default_range_pct=oc.default_range_pct,
            allowed_range_pcts=list(oc.allowed_range_pcts),
            default_strike_step=oc.default_strike_step,
            allowed_strike_steps=list(oc.allowed_strike_steps),
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
    ) -> ChainAnalyticsResponse:
        enriched = self._enrich_if_needed(df)
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

        meta = ChainMetadataPayload(
            snapshot_id=str(first["snapshot_id"]),
            snapshot_ts_utc=str(first["snapshot_ts_utc"]),
            market_date=str(first["market_date"]),
            underlying=str(first["underlying"]),
            underlying_conid=int(first["underlying_conid"]),
            underlying_price=float(first["underlying_price"]),
            market_data_type=int(first["market_data_type"]),
            contract_count=enriched.height,
            expirations=expirations,
            dtes=dtes,
            range_pct=resolved_range,
            strike_step=resolved_step,
            requested_strike_step=resolved_requested_step,
            greeks_source_counts=source_counts,
            source=source,
            notices=list(notices or []),
        )

        walls = compute_walls(enriched, top_n=self.settings.analytics.top_walls_count)
        anomalies = scan_anomalies(
            enriched,
            zscore_threshold=self.settings.analytics.unusual_volume_zscore_threshold,
        )
        profiles = build_multi_dte_profiles(enriched)
        contracts = enriched.sort(["dte", "strike", "right"]).to_dicts()

        return ChainAnalyticsResponse(
            meta=meta,
            walls=walls,
            anomalies=anomalies,
            profiles=profiles,
            contracts=contracts,
        )

    def load_chain_analytics(
        self, raw_ticker: str, snapshot_id: str | None = None
    ) -> ChainAnalyticsResponse:
        symbol = self.validate_ticker(raw_ticker)
        if snapshot_id is not None and snapshot_id.strip():
            df = self.repo.load_snapshot(symbol, snapshot_id.strip())
        else:
            df = self.repo.load_latest(symbol)
        loaded_snap_id = str(df["snapshot_id"][0])
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

    async def refresh_chain(self, req: RefreshRequest) -> ChainAnalyticsResponse:
        symbol = self.validate_ticker(req.ticker)
        fetch_res = await self.fetcher.fetch_option_chain(
            symbol, range_pct=req.range_pct, strike_step=req.strike_step
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
        if actual_step > plan_step + 1e-6:
            notices.append(
                f"IBKR strikes for {symbol} spaced at ${actual_step:g} (requested ${req_step:g})."
            )
        self.repo.save_snapshot(
            symbol,
            enriched_df.to_dicts(),
            range_pct=fetch_res.plan.range_pct,
            strike_step=actual_step,
            requested_strike_step=req_step,
        )
        return self.build_analytics_payload(
            enriched_df,
            source="LIVE_REFRESH",
            notices=notices,
            range_pct=fetch_res.plan.range_pct,
            strike_step=actual_step,
            requested_strike_step=req_step,
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
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> GatewayStatusResponse:
    return svc.get_status()


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
    svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
) -> ChainAnalyticsResponse:
    return svc.load_chain_analytics(ticker, snapshot_id=snapshot_id)


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
    async def _handle_gateway_offline(_req: Request, exc: GatewayConnectionError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "GATEWAY_OFFLINE",
                "message": (
                    f"{exc} — IBKR Gateway on Port 4001 is unreachable. "
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
