"""Option Backtest Analyzer routes (`/api/backtest/*`): thin adapters over the backtest service.

- `GET  /api/backtest/entries/{ticker}`: entry-step choices, newest first, with quote coverage
  and the usable-for-history verdict.
- `POST /api/backtest/run`: one trade from `LEGS`, `AUTO_BUILD` or `TRACKED`.

The blocking service functions run in a worker thread (`_in_thread` -> `asyncio.to_thread`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Depends

from src.services.analyzer import AnalyzerService
from src.services.backtest import (
    BacktestEntriesResponse,
    BacktestRunRequest,
    BacktestRunResponse,
    list_backtest_entries,
    run_backtest,
)
from src.web.api import get_web_service

backtest_router = APIRouter(prefix="/api/backtest", tags=["backtest"])
_T = TypeVar("_T")


async def _in_thread(func: Callable[..., _T], *args: Any) -> _T:
    """Run blocking file / engine work off the event loop (tests replace this to run inline)."""
    return await asyncio.to_thread(func, *args)


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
