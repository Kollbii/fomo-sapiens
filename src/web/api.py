"""FastAPI routes (`/api/*`). Thin adapters: parse HTTP input, call `AnalyzerService`."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal, ParamSpec, TypeVar

from fastapi import APIRouter, Body, Depends, File, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import ValidationError

from src.core.exceptions import (
    CorridorBusyError,
    GatewayConnectionError,
    GatewayTimeoutError,
    InvalidRequestError,
    OptionChainFetchError,
    PacingViolationError,
    ScreenerLiveBusyError,
)
from src.services.analyzer import AnalyzerService
from src.services.batch_refresh import BATCH_ID_PATTERN, BatchRefreshService
from src.services.error_messages import GENERIC_ERROR_CODE, GENERIC_ERROR_MESSAGE, describe_error
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
    ScreenerLiveReallocateRequest,
    ScreenerLiveReallocateResponse,
    ScreenerLiveRequest,
    ScreenerLiveStopResponse,
    ScreenerRunRequest,
    ScreenerRunResponse,
    SnapshotsListResponse,
    StrategyEvaluateRequest,
    StrategyEvaluationResponse,
    StrategySuggestRequest,
    StrategySuggestResponse,
    WatchlistResponse,
    WatchlistTickerRequest,
)

router = APIRouter(prefix="/api", tags=["options-analyzer"])
logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")


async def _in_thread(fn: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Offload blocking disk/CSV/analytics service work off the asyncio event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


#: Cap of the JSON `q` query parameter of `GET /api/screener/stream` (120 row ids x 64 chars).
SCREENER_STREAM_QUERY_MAX_CHARS = 12_000


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


class _ClosingStreamingResponse(StreamingResponse):
    """`StreamingResponse` that always closes its body generator (frees IBKR lines on cancel)."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()


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
    return await _in_thread(svc.get_status, ticker=ticker)


@router.get("/snapshots/{ticker}", response_model=SnapshotsListResponse)
async def api_snapshots(
    ticker: str,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> SnapshotsListResponse:
    return await _in_thread(svc.list_snapshots, ticker)


@router.get("/chain/{ticker}", response_model=ChainAnalyticsResponse)
async def api_chain(
    ticker: str,
    snapshot_id: str | None = Query(default=None),
    dte_min: int | None = Query(default=None, ge=0, le=120),
    dte_max: int | None = Query(default=None, ge=0, le=120),
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> ChainAnalyticsResponse:
    return await _in_thread(
        svc.load_chain_analytics,
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
    csv_path = await _in_thread(svc.resolve_csv_path, ticker, snapshot_id=snapshot_id)
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


@router.get("/logo/{ticker}")
async def api_ticker_logo(
    ticker: str,
    refresh: bool = Query(default=False),
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> Response:
    logo = await svc.logos.get_logo(ticker, refresh=refresh)
    if logo is None:
        raise HTTPException(status_code=404, detail="Logo not found")
    headers = {
        "Cache-Control": "public, max-age=604800",
        "X-Content-Type-Options": "nosniff",
    }
    if logo.media_type == "image/svg+xml":
        headers["Content-Security-Policy"] = "sandbox"
    return Response(content=logo.data, media_type=logo.media_type, headers=headers)


@router.post("/strategy/suggest", response_model=StrategySuggestResponse)
async def api_strategy_suggest(
    payload: StrategySuggestRequest,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> StrategySuggestResponse:
    return await _in_thread(svc.suggest_strategies, payload)


@router.post("/strategy/evaluate", response_model=StrategyEvaluationResponse)
async def api_strategy_evaluate(
    payload: StrategyEvaluateRequest,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> StrategyEvaluationResponse:
    return await _in_thread(svc.evaluate_strategy, payload)


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
        except GatewayTimeoutError as exc:
            safe = describe_error(exc, ibkr=svc.settings.ibkr)
            yield _format_sse_event(
                "error",
                {"error_code": safe.code, "status": 503, "message": safe.message},
            )
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

    return _ClosingStreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/screener/run", response_model=ScreenerRunResponse)
async def api_screener_run(
    payload: ScreenerRunRequest,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> ScreenerRunResponse:
    return await svc.screener.run(payload)


def _parse_screener_live_query(raw: str) -> ScreenerLiveRequest:
    """Strictly parse the JSON `q` query parameter of the live screener SSE endpoint."""
    if len(raw) > SCREENER_STREAM_QUERY_MAX_CHARS:
        raise InvalidRequestError("Live screener request is too large.")
    if len(raw.strip()) < 2:
        raise InvalidRequestError("Live screener request is empty.")
    try:
        return ScreenerLiveRequest.model_validate_json(raw)
    except ValidationError as exc:
        # Never echo client input: only fixed tokens, int indexes and known field names.
        first = exc.errors(include_input=False, include_url=False)[0]
        if first.get("type") == "json_invalid":
            raise InvalidRequestError("Live screener request is not valid JSON.") from None
        if first.get("type") == "extra_forbidden":
            raise InvalidRequestError("Live screener request has an unexpected field.") from None
        known = ScreenerLiveRequest.model_fields
        parts = [
            str(part) if isinstance(part, int) or part in known else "<field>"
            for part in first.get("loc", ())
        ]
        loc = ".".join(parts)
        msg = str(first.get("msg", "invalid value"))
        raise InvalidRequestError(
            f"Invalid live screener request ({loc or 'body'}): {msg}"[:300]
        ) from None


@router.get("/screener/stream")
async def api_screener_stream(
    request: Request,
    q: str = Query(),
    max_frames: int | None = Query(default=None, ge=1, le=200),
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> StreamingResponse:
    live_request = _parse_screener_live_query(q)
    stream = await svc.screener_live.open_live_stream(
        live_request,
        is_disconnected=request.is_disconnected,
        max_frames=max_frames,
    )

    def _error(code: str, status: int, message: str) -> str:
        return _format_sse_event(
            "error", {"error_code": code, "status": status, "message": message}
        )

    async def _event_stream() -> AsyncIterator[str]:
        try:
            yield _format_sse_event(
                "open",
                {
                    "stream_id": stream.stream_id,
                    "lines_budget": svc.screener_live.lines_budget,
                    "option_lines_budget": svc.screener_live.option_lines_budget,
                },
            )
            async for frame in stream:
                if frame.stopped:
                    yield _format_sse_event("stopped", frame.model_dump())
                    break
                yield _format_sse_event("frame", frame.model_dump())
        except GatewayTimeoutError as exc:
            safe = describe_error(exc, ibkr=svc.settings.ibkr)
            yield _error(safe.code, 503, safe.message)
        except GatewayConnectionError as exc:
            safe = describe_error(exc, ibkr=svc.settings.ibkr)
            yield _error("GATEWAY_OFFLINE", 503, safe.message)
        except ScreenerLiveBusyError as exc:
            yield _error("REFRESH_BUSY", 409, str(exc))
        except (OptionChainFetchError, PacingViolationError) as exc:
            yield _error("SCREENER_STREAM_ERROR", 502, describe_error(exc).message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("live screener stream failed unexpectedly")
            yield _error(GENERIC_ERROR_CODE, 500, GENERIC_ERROR_MESSAGE)
        finally:
            await stream.aclose()

    return _ClosingStreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/screener/stream/reallocate", response_model=ScreenerLiveReallocateResponse)
async def api_screener_reallocate(
    payload: ScreenerLiveReallocateRequest,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> ScreenerLiveReallocateResponse:
    return await svc.screener_live.reallocate(payload)


@router.post("/screener/stop", response_model=ScreenerLiveStopResponse)
async def api_screener_stop(
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> ScreenerLiveStopResponse:
    return await svc.screener_live.stop_live_streams(reason="manual_stop")


@router.get("/ledger", response_model=LedgerDashboardResponse)
async def api_ledger(
    window: LedgerWindow = "1M",
    ticker: str = "ALL",
    group_by: LedgerGroupBy = "campaigns",
    view: LedgerView = "realized",
    include_stock_gain: bool = False,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> LedgerDashboardResponse:
    return await _in_thread(
        svc.get_ledger_dashboard,
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
        return await _in_thread(svc.import_ledger_file, file.filename or "upload.csv", raw_bytes)
    except UnicodeDecodeError as exc:
        raise InvalidRequestError("Uploaded ledger file is not valid UTF-8 text.") from exc


@router.post("/ledger/mtm", response_model=LedgerMTMRefreshResponse)
async def api_ledger_mtm(
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> LedgerMTMRefreshResponse:
    return await svc.refresh_open_ledger_mtm()
