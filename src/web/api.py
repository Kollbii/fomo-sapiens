"""FastAPI routes (`/api/*`). Thin adapters: parse HTTP input, call `AnalyzerService`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, File, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from src.core.exceptions import (
    CorridorBusyError,
    GatewayConnectionError,
    InvalidRequestError,
    OptionChainFetchError,
    PacingViolationError,
)
from src.services.analyzer import AnalyzerService
from src.services.batch_refresh import BATCH_ID_PATTERN, BatchRefreshService
from src.services.schemas import (
    MAX_LEDGER_IMPORT_BYTES,
    BatchRefreshRequest,
    BatchRefreshStatus,
    ChainAnalyticsResponse,
    CorridorLegRef,
    CorridorQuoteFrame,
    CorridorQuotesRequest,
    CorridorStopResponse,
    GatewayStatusResponse,
    LedgerDashboardResponse,
    LedgerGroupBy,
    LedgerImportResponse,
    LedgerMTMRefreshResponse,
    LedgerSyncRequest,
    LedgerSyncResponse,
    LedgerView,
    LedgerWindow,
    RefreshRequest,
    SnapshotsListResponse,
    StrategyEvaluateRequest,
    StrategyEvaluationResponse,
    StrategySuggestRequest,
    StrategySuggestResponse,
    WatchlistResponse,
    WatchlistTickerRequest,
)

router = APIRouter(prefix="/api", tags=["options-analyzer"])


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


async def get_web_service(request: Request) -> AnalyzerService:
    """FastAPI dependency returning the application's `AnalyzerService` instance."""
    svc: AnalyzerService | None = getattr(request.app.state, "service", None)
    if svc is None:
        svc = AnalyzerService()
        request.app.state.service = svc
    return svc


async def get_batch_service(request: Request) -> BatchRefreshService:
    """FastAPI dependency returning the application's `BatchRefreshService` instance."""
    batch: BatchRefreshService | None = getattr(request.app.state, "batch_refresh", None)
    if batch is None:
        svc = await get_web_service(request)
        batch = BatchRefreshService(svc)
        request.app.state.batch_refresh = batch
    return batch


BatchIdPath = Path(pattern=BATCH_ID_PATTERN)


# ------------------------------------------------------------------ routes
@router.get("/status", response_model=GatewayStatusResponse)
async def api_status(
    ticker: str | None = None,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> GatewayStatusResponse:
    return svc.get_status(ticker=ticker)


@router.get("/snapshots/{ticker}", response_model=SnapshotsListResponse)
async def api_snapshots(
    ticker: str,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> SnapshotsListResponse:
    return svc.list_snapshots(ticker)


@router.get("/chain/{ticker}", response_model=ChainAnalyticsResponse)
async def api_chain(
    ticker: str,
    snapshot_id: str | None = Query(default=None),
    dte_min: int | None = Query(default=None, ge=0, le=120),
    dte_max: int | None = Query(default=None, ge=0, le=120),
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
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
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> FileResponse:
    csv_path = svc.resolve_csv_path(ticker, snapshot_id=snapshot_id)
    return FileResponse(path=csv_path, media_type="text/csv", filename=csv_path.name)


@router.post("/refresh", response_model=ChainAnalyticsResponse)
async def api_refresh(
    payload: RefreshRequest,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> ChainAnalyticsResponse:
    return await svc.refresh_chain(payload)


@router.post("/refresh/batch", response_model=BatchRefreshStatus, status_code=202)
async def api_batch_start(
    payload: BatchRefreshRequest,
    batch: BatchRefreshService = Depends(get_batch_service),  # noqa: B008
) -> BatchRefreshStatus:
    return await batch.start(payload)


@router.get("/refresh/batch/current", response_model=BatchRefreshStatus | None)
async def api_batch_current(
    batch: BatchRefreshService = Depends(get_batch_service),  # noqa: B008
) -> BatchRefreshStatus | None:
    return batch.current()


@router.get("/refresh/batch/{batch_id}", response_model=BatchRefreshStatus)
async def api_batch_get(
    batch_id: str = BatchIdPath,
    batch: BatchRefreshService = Depends(get_batch_service),  # noqa: B008
) -> BatchRefreshStatus:
    return batch.get(batch_id)


@router.post("/refresh/batch/{batch_id}/cancel", response_model=BatchRefreshStatus)
async def api_batch_cancel(
    batch_id: str = BatchIdPath,
    batch: BatchRefreshService = Depends(get_batch_service),  # noqa: B008
) -> BatchRefreshStatus:
    return batch.cancel(batch_id)


@router.get("/watchlist", response_model=WatchlistResponse)
async def api_watchlist(
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> WatchlistResponse:
    return await svc.watchlist.get_watchlist()


@router.post("/watchlist/tickers", response_model=WatchlistResponse)
async def api_watchlist_add(
    payload: WatchlistTickerRequest,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> WatchlistResponse:
    return await svc.watchlist.add_ticker(payload.ticker)


@router.delete("/watchlist/tickers/{ticker}", response_model=WatchlistResponse)
async def api_watchlist_remove(
    ticker: str,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> WatchlistResponse:
    return await svc.watchlist.remove_ticker(ticker)


@router.post("/strategy/suggest", response_model=StrategySuggestResponse)
async def api_strategy_suggest(
    payload: StrategySuggestRequest,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> StrategySuggestResponse:
    return svc.suggest_strategies(payload)


@router.post("/strategy/evaluate", response_model=StrategyEvaluationResponse)
async def api_strategy_evaluate(
    payload: StrategyEvaluateRequest,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> StrategyEvaluationResponse:
    return svc.evaluate_strategy(payload)


@router.post("/corridor/quotes", response_model=CorridorQuoteFrame)
async def api_corridor_quotes(
    payload: CorridorQuotesRequest,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> CorridorQuoteFrame:
    return await svc.fetch_corridor_quotes(payload)


@router.post("/corridor/stop", response_model=CorridorStopResponse)
async def api_corridor_stop(
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> CorridorStopResponse:
    return await svc.stop_corridor_streams()


@router.get("/corridor/stream/{ticker}")
async def api_corridor_stream(
    request: Request,
    ticker: str,
    snapshot_id: str | None = Query(default=None),
    target_expiry: str | None = Query(default=None),
    window_strikes: int = Query(default=10, ge=1, le=15),
    max_contracts: int = Query(default=88, ge=1, le=90),
    priority_legs: str | None = Query(default=None),
    max_frames: int | None = Query(default=None, ge=1, le=200),
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> StreamingResponse:
    gen = await svc.open_corridor_stream(
        ticker,
        snapshot_id=snapshot_id,
        target_expiry=target_expiry,
        window_strikes=window_strikes,
        max_contracts=max_contracts,
        priority_legs=_parse_priority_legs_query(priority_legs),
        is_disconnected=request.is_disconnected,
        max_frames=max_frames,
    )

    async def _event_stream() -> AsyncIterator[str]:
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
        except CorridorBusyError as exc:
            yield _format_sse_event(
                "error",
                {
                    "error_code": "REFRESH_BUSY",
                    "status": 409,
                    "message": str(exc),
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


@router.get("/ledger", response_model=LedgerDashboardResponse)
async def api_ledger(
    window: LedgerWindow = "1M",
    ticker: str = "ALL",
    group_by: LedgerGroupBy = "campaigns",
    view: LedgerView = "realized",
    include_stock_gain: bool = False,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> LedgerDashboardResponse:
    return svc.get_ledger_dashboard(
        window=window,
        ticker=ticker,
        group_by=group_by,
        view=view,
        include_stock_gain=include_stock_gain,
    )


@router.post("/ledger/sync", response_model=LedgerSyncResponse)
async def api_ledger_sync(
    payload: LedgerSyncRequest = Body(default_factory=LedgerSyncRequest),  # noqa: B008
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> LedgerSyncResponse:
    return await svc.sync_ledger(
        mode=payload.mode,
        include_gateway=payload.include_gateway,
    )


@router.post("/ledger/import", response_model=LedgerImportResponse)
async def api_ledger_import(
    file: UploadFile = File(...),  # noqa: B008
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> LedgerImportResponse:
    try:
        raw_bytes = await file.read(MAX_LEDGER_IMPORT_BYTES + 1)
    finally:
        await file.close()
    if len(raw_bytes) > MAX_LEDGER_IMPORT_BYTES:
        raise InvalidRequestError("Uploaded ledger file exceeds 20 MB limit.")
    try:
        return svc.import_ledger_file(file.filename or "upload.csv", raw_bytes)
    except UnicodeDecodeError as exc:
        raise InvalidRequestError("Uploaded ledger file is not valid UTF-8 text.") from exc


@router.post("/ledger/mtm", response_model=LedgerMTMRefreshResponse)
async def api_ledger_mtm(
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> LedgerMTMRefreshResponse:
    return await svc.refresh_open_ledger_mtm()
