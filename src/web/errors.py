"""Map domain exceptions (`src/core/exceptions.py`) to structured JSON HTTP responses.

Stack traces never reach the browser; every error body carries `error_code` + `message`.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.core.exceptions import (
    GatewayConnectionError,
    InvalidRequestError,
    InvalidTickerError,
    OptionChainFetchError,
    PacingViolationError,
    SecurityViolationError,
    SnapshotNotFoundError,
    StorageSchemaError,
    UnderlyingPriceUnavailableError,
)
from src.services.analyzer import AnalyzerService


def register_exception_handlers(app: FastAPI) -> None:
    """Map domain exceptions in `src/core/exceptions.py` to structured HTTP JSON responses."""

    @app.exception_handler(InvalidTickerError)
    async def _handle_invalid_ticker(_req: Request, exc: InvalidTickerError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error_code": "INVALID_TICKER", "message": str(exc)},
        )

    @app.exception_handler(InvalidRequestError)
    async def _handle_invalid_request(_req: Request, exc: InvalidRequestError) -> JSONResponse:
        # `detail` mirrors FastAPI's HTTPException body, which the dashboard reads first.
        return JSONResponse(
            status_code=422,
            content={"error_code": "INVALID_REQUEST", "message": str(exc), "detail": str(exc)},
        )

    @app.exception_handler(SnapshotNotFoundError)
    async def _handle_snapshot_not_found(_req: Request, exc: SnapshotNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error_code": "SNAPSHOT_NOT_FOUND", "message": str(exc)},
        )

    @app.exception_handler(GatewayConnectionError)
    async def _handle_gateway_offline(req: Request, exc: GatewayConnectionError) -> JSONResponse:
        svc: AnalyzerService | None = getattr(req.app.state, "service", None)
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
