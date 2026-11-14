"""FastAPI application entrypoint and offline-first lifespan management."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import Response
from starlette.types import Scope

from src.broker.client import IBKRBrokerClient
from src.broker.fetcher import PagedOptionChainFetcher
from src.core.config import Settings, get_settings
from src.services.analyzer import AnalyzerService
from src.services.ledger import LedgerService
from src.storage.csv_repository import CSVRepository
from src.web.api import get_web_service, router
from src.web.backtest_api import backtest_router
from src.web.errors import register_exception_handlers

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


class RevalidatingStaticFiles(StaticFiles):
    """Static files served with `Cache-Control: no-cache` (ETag revalidation on every load).

    The dashboard is split into native ES modules imported without `?v=` cache-busters, so
    browsers must revalidate each module to never mix stale and fresh files; unchanged
    files still return cheap `304 Not Modified` responses.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def _format_feed_summary(cfg: Settings) -> str:
    mode_lbl = "Live" if cfg.ibkr.market_data_mode == "LIVE" else "Delayed"
    def_t = cfg.ibkr.default_market_data_type
    fb_t = cfg.ibkr.fallback_market_data_type
    if def_t != fb_t:
        return f"{mode_lbl} Type {def_t}/{fb_t} Feed"
    return f"{mode_lbl} Type {def_t} Feed"


def create_app(
    settings: Settings | None = None,
    repo: CSVRepository | None = None,
    broker_client: IBKRBrokerClient | None = None,
    fetcher: PagedOptionChainFetcher | None = None,
    ledger_service: LedgerService | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application in Offline-First mode."""
    cfg = settings or get_settings()
    service = AnalyzerService(
        settings=cfg,
        repo=repo,
        broker_client=broker_client,
        fetcher=fetcher,
        ledger_service=ledger_service,
    )

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        app_instance.state.service = service
        service.ensure_layout()
        try:
            service.repo.load_index()
        except Exception:  # pragma: no cover - defensive
            logger.warning("snapshot index initialization warning; continuing in offline mode")
        logger.info(
            "starting %s (offline-first cache ready; IBKR target=%s:%d mode=%s)",
            cfg.app.name,
            cfg.ibkr.host,
            cfg.ibkr.port,
            cfg.ibkr.market_data_mode,
        )
        try:
            yield
        finally:
            try:
                await service.stop_corridor_streams()
            except Exception:  # pragma: no cover - defensive shutdown
                logger.exception("error stopping corridor streams during shutdown")
            service.broker_client.disconnect()

    application = FastAPI(
        title=cfg.app.name,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.service = service

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    application.mount("/static", RevalidatingStaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    register_exception_handlers(application)
    application.include_router(router)
    application.include_router(backtest_router)

    @application.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        svc: AnalyzerService = Depends(get_web_service),  # noqa: B008
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "app_name": svc.settings.app.name,
                "default_theme": svc.settings.app.default_theme,
                "default_ticker": svc.settings.watchlist.default_ticker,
                "watchlist": list(svc.settings.watchlist.hardcoded_tickers),
                "default_range_pct": svc.settings.options_chain.default_range_pct,
                "allowed_range_pcts": list(svc.settings.options_chain.allowed_range_pcts),
                "default_strike_step": svc.settings.options_chain.default_strike_step,
                "allowed_strike_steps": list(svc.settings.options_chain.allowed_strike_steps),
                "ibkr_host": svc.settings.ibkr.host,
                "ibkr_port": svc.settings.ibkr.port,
                "market_data_mode": svc.settings.ibkr.market_data_mode,
                "default_market_data_type": svc.settings.ibkr.default_market_data_type,
                "fallback_market_data_type": svc.settings.ibkr.fallback_market_data_type,
                "dte_min": svc.settings.options_chain.dte_min,
                "dte_max": svc.settings.options_chain.dte_max,
                "include_short_dte_for_backtest": (
                    svc.settings.options_chain.include_short_dte_for_backtest
                ),
                "feed_summary": _format_feed_summary(svc.settings),
            },
        )

    return application


app = create_app()
