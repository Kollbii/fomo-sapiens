"""FastAPI routes (`/api/backtest/*`). Thin adapters over service-layer helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from fastapi import APIRouter, Depends, Path, Query

from src.services.analyzer import AnalyzerService
from src.services.backtest import list_backtest_entries, run_backtest
from src.services.schemas import (
    BacktestEntriesResponse,
    BacktestRunRequest,
    BacktestRunResponse,
    PaperTrade,
    PaperTradesLedgerSyncResponse,
    PaperTradesListResponse,
    PaperTradeStatus,
    RegisterPaperTradeRequest,
)
from src.web.api import get_web_service

_P = ParamSpec("_P")
_T = TypeVar("_T")

_PAPER_TRADE_ID_PATH_PATTERN = r"^[A-Z]{1,5}-\d{8}-[0-9a-f]{8}$"

backtest_router = APIRouter(prefix="/api/backtest", tags=["backtest"])


async def _in_thread(fn: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Offload blocking backtest analytics or store I/O off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


@backtest_router.get("/entries/{ticker}", response_model=BacktestEntriesResponse)
async def api_backtest_entries(
    ticker: str,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> BacktestEntriesResponse:
    return await _in_thread(list_backtest_entries, svc, ticker)


@backtest_router.post("/run", response_model=BacktestRunResponse)
async def api_backtest_run(
    payload: BacktestRunRequest,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> BacktestRunResponse:
    return await _in_thread(run_backtest, svc, payload)


@backtest_router.get("/paper-trades", response_model=PaperTradesListResponse)
async def api_backtest_list_paper_trades(
    status: PaperTradeStatus | None = Query(default=None),  # noqa: B008
    ticker: str | None = Query(default=None),  # noqa: B008
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> PaperTradesListResponse:
    return await _in_thread(
        svc.paper_trades.list_trades_response,
        status=status,
        ticker=ticker,
    )


@backtest_router.post("/paper-trades", response_model=PaperTrade, status_code=201)
async def api_backtest_create_paper_trade(
    payload: RegisterPaperTradeRequest,
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> PaperTrade:
    return await _in_thread(svc.paper_trades.register_from_request, payload)


@backtest_router.post("/paper-trades/{trade_id}/stop", response_model=PaperTrade)
async def api_backtest_stop_paper_trade(
    trade_id: str = Path(pattern=_PAPER_TRADE_ID_PATH_PATTERN),
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> PaperTrade:
    return await _in_thread(svc.paper_trades.stop_trade, trade_id)


@backtest_router.post("/paper-trades/sync-ledger", response_model=PaperTradesLedgerSyncResponse)
async def api_backtest_sync_paper_trades_from_ledger(
    svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
) -> PaperTradesLedgerSyncResponse:
    return await _in_thread(svc.sync_paper_trades_from_ledger)
