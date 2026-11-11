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

from src.broker.client import IBKRBrokerClient
from src.broker.fetcher import PagedOptionChainFetcher
from src.core.config import Settings, get_settings
from src.storage.csv_repository import CSVRepository
from src.web.api import AnalyzerWebService, get_web_service, register_exception_handlers, router

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def create_app(
    settings: Settings | None = None,
    repo: CSVRepository | None = None,
    broker_client: IBKRBrokerClient | None = None,
    fetcher: PagedOptionChainFetcher | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application in Offline-First mode."""
    cfg = settings or get_settings()
    service = AnalyzerWebService(
        settings=cfg,
        repo=repo,
        broker_client=broker_client,
        fetcher=fetcher,
    )

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        app_instance.state.service = service
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
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    register_exception_handlers(application)
    application.include_router(router)

    @application.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        svc: AnalyzerWebService = Depends(get_web_service),  # noqa: B008
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
                "ibkr_port": svc.settings.ibkr.port,
            },
        )

    return application


app = create_app()
