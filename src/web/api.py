"""FastAPI routes (`/api/*`). Thin adapters: parse HTTP input, call `AnalyzerService`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from src.core.exceptions import GatewayConnectionError, OptionChainFetchError, PacingViolationError
from src.services.analyzer import AnalyzerService
from src.services.schemas import (
    ChainAnalyticsResponse,
    CorridorLegRef,
    CorridorQuoteFrame,
    CorridorQuotesRequest,
    CorridorStopResponse,
    GatewayStatusResponse,
    RefreshRequest,
    SnapshotsListResponse,
    StrategyEvaluateRequest,
    StrategyEvaluationResponse,
    StrategySuggestRequest,
    StrategySuggestResponse,
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
    dte_max: int | None = Query(default=None, ge=1, le=120),
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
    window_strikes: int = Query(default=5, ge=1, le=12),
    max_contracts: int = Query(default=56, ge=1, le=60),
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
